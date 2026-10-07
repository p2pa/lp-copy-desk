"""IS THIS LP WALLET WORTH COPYING? Any address, any window, in seconds (docs/LPWALLETS.md).

  python -m copydesk.lpwallet <address> [--days 7] [--json]
  python -m copydesk.lpwallet rank <addr> <addr> ... [--days 7]     one line per wallet, best first
  python -m copydesk.lpwallet check                                  vs LP Agent's view (copydesk/fixtures/lpagent_0x78a1003e.json)

A position is a cash flow, so read the cash. ONE topic-indexed hypersync query - every Transfer to or from the wallet,
which includes its PositionManager NFT mints and burns - carries the whole book: what each mint tx took out of the wallet
(invested), what each burn tx gave back, fee collections, and the wallet's own sales of the returned token (their price
values the token side). No pool state, no archive: the older version priced every liquidity change from slot0 and spent
minutes on archive reads and PoolManager swap scans; this one reads ~1.5k logs a week in ~5 s. Everything current
(balances, open positions, decimals) is ONE batched `latest` JSON-RPC request per kind.

Realized per position = quote back + the returned token at the wallet's own next sale price (today's price while it is
still held) - invested. Gas comes off at the wallet level, through its native ETH balance at both ends. What this cannot see: native ETH sent to the wallet by others (no traces), and a position opened
before the scan (reported as skipped, never guessed).
"""
import bisect, collections, json, re, os, statistics as S, sys, time, urllib.request

from eth_utils import keccak
from discovery import forensics as F

POSM = '0x58daec3116aae6d93017baaea7749052e8a04fa7'
PM = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
SWAP4 = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
SV = '0xF3334192D15450CdD385c8B70e03f9A6bD9E673b'
XFR = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
USDG, WETH = '0x5fc5360d0400a0fd4f2af552add042d716f1d168', '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
# BENCHMARKED FROM THE DESK'S BOX (2026-10-01, research: 13 public endpoints): eth_blockNumber p50 publicnode 31 ms, blxrbdn 14 ms,
# official 30 ms (60 calls a minute: last resort), blockreq 101 ms; all four answer 20-call batches. drpc (500), tatum / routemesh /
# uniblock (429), solidrpc (batch errors), arrowrpc and triport (down) fail what the desk needs
LATEST = ['https://robinhood-rpc.publicnode.com', 'https://robinhood.rpc.blxrbdn.com',      # never blockmachine: the desk's quota
          'https://rpc.mainnet.chain.robinhood.com', 'https://robinhood-mainnet-rpc.blockreq.com/v1/rpc/public']
_DOWN = {}                                             # endpoint -> skipped until (a timeout or a refusal: 60 s)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'lpagent_0x78a1003e.json')
DAY = 864000                                                    # blocks (100 ms)
PRE = DAY // 2                                                  # extra history so a position open at the window start is seen


NODE_ERR = re.compile(r'header not found|unknown block|missing trie|not available|pruned|rate|limit|timeout|busy|capacity', re.I)
PIN = {}                                                        # block hash every "now" read is pinned to (see fetch)


SNAP = {}                                                       # pinned reads, taken in one burst right after the ledger


def _pinned(m, p):
    return PIN.get('at') is not None and PIN['at'] in (p if isinstance(p, list) else [])


def batch(calls):
    """Pinned reads are served from SNAP when the snapshot already holds them (see snapshot())."""
    keys = [json.dumps([m, p], sort_keys=True) for m, p in calls]
    miss = [(k, c) for k, c in zip(keys, calls) if k not in SNAP]
    if miss:
        got = _batch([c for _, c in miss])
        for (k, c), r in zip(miss, got):
            if _pinned(*c):
                SNAP[k] = r
            else:
                SNAP.setdefault('_tmp', {})[k] = r
    tmp = SNAP.pop('_tmp', {})
    return [SNAP[k] if k in SNAP else tmp.get(k) for k in keys]


DEADLINE = [0.0]                                                # a caller's time budget (lpfind): past it, every read gives up


def _batch(calls):
    """Every read: our node while it is fresh (rh-node keeps recent state, so pinned snapshots work there), public nodes
    otherwise or when it fails."""
    if DEADLINE[0] and time.time() > DEADLINE[0]:
        raise TimeoutError('over the time budget')
    try:
        from copydesk import lpfollow as L
        if L.node_fresh():
            return L._node_batch(calls, timeout=60)
    except Exception:
        pass
    return _public_batch(calls)


def _public_batch(calls):
    """[(method, params)] -> results, 50 per HTTP request. A NODE error (unknown block, missing state, rate limit) retries the
    whole chunk on the other endpoint; any other error is the contract's answer (None): a revert, an invalid opcode."""
    if len(calls) > 50:
        # chunks of 50, four in flight: a fresh block's state lives only seconds on public nodes
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(4) as ex:
            return [x for part in ex.map(_batch, [calls[i:i + 50] for i in range(0, len(calls), 50)]) for x in part]
    out = []
    for i in range(0, len(calls), 50):
        chunk = calls[i:i + 50]
        for a in range(8):
            if DEADLINE[0] and time.time() > DEADLINE[0]:
                raise TimeoutError('over the time budget')
            # A HEALTHY ENDPOINT FIRST (2026-10-01 15:33: publicnode's http timed out from the desk's box; every read waited
            # 30 s a try and a poll pass took 41 s): one that failed is skipped for 60 s, a try waits 6 s at most
            up = [u for u in LATEST if _DOWN.get(u, 0) < time.time()] or LATEST
            url = up[a % len(up)]
            try:
                body = [dict(jsonrpc='2.0', id=k, method=m, params=p) for k, (m, p) in enumerate(chunk)]
                try:
                    r = json.loads(urllib.request.urlopen(urllib.request.Request(
                        url, json.dumps(body).encode(), {'Content-Type': 'application/json', 'User-Agent': 'x'}), timeout=6).read())
                except Exception:
                    _DOWN[url] = time.time() + 60
                    raise
                # a revert is the contract's answer even when its reason says "limit" (a token's own error text matched the
                # node-error pattern and retried a chunk forever)
                if not isinstance(r, list) or any(x.get('error') and 'revert' not in str(x['error']).lower() and NODE_ERR.search(str(x['error'])) for x in r):
                    raise RuntimeError(str(r)[:200])
                r = {x['id']: x.get('result') for x in r}
                out += [r.get(k) for k in range(len(chunk))]
                break
            except Exception:
                time.sleep(0 if any(_DOWN.get(u, 0) < time.time() for u in LATEST) else 1 + a)    # another endpoint is up: at once
        else:
            raise RuntimeError('batch rpc failed on every endpoint')
    return out


def block_hash(n):
    return batch([('eth_getBlockByNumber', [hex(n), False])])[0]['hash']


def archive_balance(owner, n):
    """The wallet's ETH at block n, asked BY HASH (EIP-1898). blockmachine load-balances over a node that ignores the block
    number (it answered 0.121589 ETH for this wallet at every block from 44M to 70M); asked by hash, a node without that
    block must error, and the retry lands on an honest one. One call per report (60 CU/min per IP, shared with the desk)."""
    h = block_hash(n)
    for a in range(6):
        try:
            r = json.loads(urllib.request.urlopen(urllib.request.Request(
                'https://rpc-robinhood.blockmachine.io', json.dumps(dict(jsonrpc='2.0', id=1, method='eth_getBalance',
                                                                        params=[owner, {'blockHash': h}])).encode(),
                {'Content-Type': 'application/json', 'User-Agent': 'x'}), timeout=20).read())
            if r.get('result'):
                return int(r['result'], 16)
        except Exception:
            pass
        time.sleep(2 + 2 * a)
    return None


def posm_tids(inp):
    """tokenIds a PositionManager call touches: modifyLiquidities(unlockData, deadline), also inside multicall. unlockData
    is abi.encode(actions, params[]); INCREASE (0x00), DECREASE (0x01) and BURN (0x03) params start with the tokenId."""
    from eth_abi import decode
    try:
        sel, b = inp[:10], bytes.fromhex(inp[10:])
        if sel == '0xac9650d8':
            return {t for c in decode(['bytes[]'], b)[0] for t in posm_tids('0x' + c.hex())}
        if sel != '0xdd46508f':
            return set()
        actions, params = decode(['bytes', 'bytes[]'], decode(['bytes', 'uint256'], b)[0])
        return {int.from_bytes(p[:32], 'big') for a, p in zip(actions, params) if a in (0x00, 0x01, 0x03)}
    except Exception:
        return set()


def call(to, data, pin=True):
    return ('eth_call', [dict(to=to, data=data), PIN.get('at', 'latest') if pin else 'latest'])


def word(r, i):
    return int(r[2 + 64 * i:66 + 64 * i], 16) if r and len(r) >= 66 + 64 * i else 0


def eth_series(t0, t1):
    """Hourly ETH/USD (DefiLlama), fetched in 300-hour spans (a longer span is refused)."""
    out, t = [], t0 - 7200
    while t < t1:
        u = 'https://coins.llama.fi/chart/coingecko:ethereum?start=%d&span=300&period=1h' % t
        d = json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'x'}), timeout=30).read())
        out += [(x['timestamp'], x['price']) for x in d['coins']['coingecko:ethereum']['prices']]
        t += 300 * 3600
    return sorted(set(out))


def near(series, x):
    """Value of a sorted [(x, v)] series at x: linear between neighbours, flat past the ends."""
    i = min(max(1, bisect.bisect_left(series, (x, 0))), len(series) - 1)
    (x0, v0), (x1, v1) = series[i - 1], series[i]
    return v0 + (v1 - v0) * min(1.0, max(0.0, (x - x0) / (x1 - x0))) if x1 != x0 else v1


def store():
    """The local store: copydesk/lpdb.py (var/lpwallets.db)."""
    from copydesk import lpdb
    return lpdb.connect()


def _slim_tx(t):
    """Calldata is kept only for PositionManager calls (the only ones decoded: which tokenIds a decrease touched);
    it was 366 MB of the store for nothing else."""
    if (t.get('to') or '').lower() != POSM:
        t = dict(t, input='0x')
    return t


def _pull(con, owner, pad, lo, hi=None, forward=False):
    """Hypersync [lo, hi) into the store, page by page. forward: the range extends the covered one, so each page moves
    the wallet's cover.hi and a stopped fetch resumes from the last page."""
    body = dict(from_block=lo, logs=[dict(topics=[[XFR], [pad]]), dict(topics=[[XFR], [], [pad]])],
                field_selection=dict(log=['address', 'block_number', 'log_index', 'transaction_hash', 'topic1', 'topic2', 'topic3', 'data'],
                                     transaction=['hash', 'from', 'to', 'input', 'value', 'gas_used', 'effective_gas_price'],
                                     block=['number', 'timestamp']))
    if hi:
        body['to_block'] = hi
    top = lo
    from copydesk import lpdb
    for r in F._pages(body, F._key(), stop=hi):
        lpdb.begin(con)
        for d in r['data']:
            con.executemany('INSERT OR IGNORE INTO logs VALUES (?,?,?,?,?,?,?,?,?)', [
                (owner, l['transaction_hash'], l['log_index'], l['block_number'], l['address'].lower(), l.get('topic1'), l.get('topic2'),
                 l.get('topic3'), l['data']) for l in d.get('logs') or []])
            con.executemany('INSERT OR IGNORE INTO txs VALUES (?,?,?)', [(owner, t['hash'], json.dumps(_slim_tx(t))) for t in d.get('transactions') or []])
            con.executemany('INSERT OR IGNORE INTO blocks VALUES (?,?)', [
                (b['number'], int(b['timestamp'], 16) if isinstance(b['timestamp'], str) else b['timestamp']) for b in d.get('blocks') or []])
        nb = r.get('next_block') or top
        top = max(top, min(nb, hi) if hi else nb)
        if forward:
            con.execute('UPDATE cover SET hi = ? WHERE owner = ?', (top, owner))
        con.commit()
    return top


def _pull_node(con, owner, pad, lo, hi=None, forward=False):
    """The same as _pull from OUR node (rh-node through the tunnel; no rate limit): the wallet's transfers in 1-day
    getLogs chunks, 8 in flight; each new tx's sender, calldata and gas in one batched request. Returns the next block."""
    import concurrent.futures as cf
    from copydesk import lpfollow as L
    head = int(L._node_batch([('eth_blockNumber', [])])[0], 16)
    top = min(hi, head + 1) if hi else head + 1
    spans = [(a, min(top - 1, a + DAY - 1)) for a in range(lo, top, DAY)]

    def get(span):
        if DEADLINE[0] and time.time() > DEADLINE[0]:
            raise TimeoutError('over the time budget')
        out = []
        for tp in ([XFR, pad], [XFR, None, pad]):
            r = L._node_batch([('eth_getLogs', [dict(fromBlock=hex(span[0]), toBlock=hex(span[1]), topics=tp)])], timeout=120)[0]
            out += r or []
        return out
    with cf.ThreadPoolExecutor(6) as ex:                             # latency-bound, not CPU: the node sat at load 1.6 of 4
        logs = [l for part in ex.map(get, spans) for l in part]
    known = {h for (h,) in con.execute('SELECT hash FROM txs WHERE owner = ?', (owner,))}
    # EVERY network read first, then one short write: a write transaction held across node calls blocked the other
    # screen workers for minutes ("database is locked")
    hs = sorted({l['transactionHash'] for l in logs} - known)
    txrows = []
    for i in range(0, len(hs), 100):
        chunk = hs[i:i + 100]
        r = L._node_batch([('eth_getTransactionByHash', [h]) for h in chunk] + [('eth_getTransactionReceipt', [h]) for h in chunk])
        for h, t, rc in zip(chunk, r[:len(chunk)], r[len(chunk):]):
            if t and rc:
                txrows.append((owner, h, json.dumps(_slim_tx(dict(
                    hash=h, input=t['input'], value=t['value'], gas_used=rc['gasUsed'], effective_gas_price=rc['effectiveGasPrice'],
                    **{'from': t['from'], 'to': t['to']})))))
    blocks = {int(l['blockNumber'], 16): int(l['blockTimestamp'], 16) for l in logs if l.get('blockTimestamp')}
    miss = sorted({int(l['blockNumber'], 16) for l in logs} - set(blocks))
    for i in range(0, len(miss), 100):
        r = L._node_batch([('eth_getBlockByNumber', [hex(b), False]) for b in miss[i:i + 100]])
        blocks.update({b: int(x['timestamp'], 16) for b, x in zip(miss[i:i + 100], r) if x})
    from copydesk import lpdb
    with lpdb.writing(con):
        con.executemany('INSERT OR IGNORE INTO txs VALUES (?,?,?)', txrows)
        con.executemany('INSERT OR IGNORE INTO logs VALUES (?,?,?,?,?,?,?,?,?)', [
            (owner, l['transactionHash'], int(l['logIndex'], 16), int(l['blockNumber'], 16), l['address'].lower(),
             *((l['topics'] + [None] * 4)[1:4]), l['data']) for l in logs])
        con.executemany('INSERT OR IGNORE INTO blocks VALUES (?,?)', list(blocks.items()))
        if forward:
            con.execute('UPDATE cover SET hi = ? WHERE owner = ?', (top, owner))
    return top


def fetch(owner, days):
    owner = owner.lower()
    if len(owner) != 42 or int(owner, 16) == 0:
        raise ValueError('not a wallet address: %r' % owner)          # the zero address once stored 27k logs
    head = int(batch([('eth_blockNumber', [])])[0], 16)
    frm = head - int(days * DAY)
    pad = '0x' + owner[2:].rjust(64, '0')
    want = frm - PRE
    con = store()
    try:
        cv = con.execute('SELECT lo, hi FROM cover WHERE owner = ?', (owner,)).fetchone()
        if cv is None:
            con.execute('INSERT INTO cover VALUES (?,?,?)', (owner, want, want))
            con.commit()
            lo = hi = want
        else:
            lo, hi = cv
        from copydesk import lpfollow as L
        def pull(*a, **k):                                              # our node first; hypersync when it is not fresh or fails
            if L.node_fresh():
                try:
                    return _pull_node(*a, **k)
                except TimeoutError:
                    raise
                except Exception:
                    pass
            return _pull(*a, **k)
        if want < lo:                                                   # a longer window than ever fetched: the older blocks
            pull(con, owner, pad, want, lo)
            con.execute('UPDATE cover SET lo = ? WHERE owner = ?', (want, owner))
            con.commit()
        top = pull(con, owner, pad, hi, forward=True)                   # only what is new since the last run
        logs = [dict(address=a, block_number=b, log_index=i, transaction_hash=h, topic1=t1, topic2=t2, topic3=t3, data=dt)
                for h, i, b, a, t1, t2, t3, dt in con.execute(
                    'SELECT tx, li, blk, address, t1, t2, t3, data FROM logs WHERE owner = ? AND blk >= ?', (owner, want))]
        hs = {l['transaction_hash'] for l in logs}
        txs = {h: json.loads(j) for h, j in con.execute('SELECT hash, j FROM txs WHERE owner = ?', (owner,)) if h in hs}
        bl = sorted({l['block_number'] for l in logs})
        bt = {}
        for i in range(0, len(bl), 900):
            chunk = bl[i:i + 900]
            bt.update(con.execute('SELECT n, ts FROM blocks WHERE n IN (%s)' % ','.join('?' * len(chunk)), chunk).fetchall())
    except BaseException:
        con.rollback()                                             # a timed-out fetch must not keep a write lock open:
        raise                                                      # it blocked the screen's own cleanup (database is locked)
    finally:
        con.close()
    # every "now" read (balances, positions, pool prices) is pinned to the last block the ledger covers, by hash: one snapshot,
    # so a transfer can never be in the balance but missing from the ledger (or the reverse)
    D = dict(owner=owner, frm=frm, logs=logs, txs=txs, bt=bt)
    top = (top or head + 1) - 1
    for a in range(4):
        # public nodes keep state for only a few recent blocks (unevenly: 30 s back failed on both, 2 min back worked on
        # one), so pin to the RPC's newest block, close the gap since hypersync's last block with blockmachine's getLogs
        # (10k blocks a call), and take every "now" read in one burst right away
        head = int(_batch([('eth_blockNumber', [])])[0], 16)
        gap(D, top + 1, head, pad)
        top = head
        PIN['at'] = {'blockHash': block_hash(head)}
        SNAP.clear()
        D['head'] = head
        try:
            snapshot(D)
            return D
        except RuntimeError:
            continue
    raise RuntimeError('no node kept state for a fresh block four times running')


def gap(D, lo, hi, pad):
    """Transfers to/from the wallet in blocks lo..hi (the few seconds hypersync has not indexed yet), in hypersync's shape."""
    if hi < lo:
        return
    try:
        from copydesk import lpfollow as L
        if L.node_fresh():
            new = []
            for tp in ([XFR, pad], [XFR, None, pad]):
                new += L._node_batch([('eth_getLogs', [dict(fromBlock=hex(lo), toBlock=hex(hi), topics=tp)])], timeout=60)[0] or []
            return _merge_gap(D, new)
    except Exception:
        pass
    new = []
    for a in range(lo, hi + 1, 10000):
        for tp in ([XFR, pad], [XFR, None, pad]):
            for k in range(6):
                try:
                    r = json.loads(urllib.request.urlopen(urllib.request.Request(
                        'https://rpc-robinhood.blockmachine.io', json.dumps(dict(jsonrpc='2.0', id=1, method='eth_getLogs', params=[
                            dict(fromBlock=hex(a), toBlock=hex(min(hi, a + 9999)), topics=tp)])).encode(),
                        {'Content-Type': 'application/json', 'User-Agent': 'x'}), timeout=20).read())
                    if 'result' in r:
                        new += r['result']
                        break
                except Exception:
                    pass
                time.sleep(1 + k)
    return _merge_gap(D, new)


def _merge_gap(D, new):
    seen = {(l['transaction_hash'], l['log_index']) for l in D['logs']}
    hs = set()
    for l in new:
        t = l['topics'] + [None] * 4
        x = dict(address=l['address'], block_number=int(l['blockNumber'], 16), log_index=int(l['logIndex'], 16),
                 transaction_hash=l['transactionHash'], topic1=t[1], topic2=t[2], topic3=t[3], data=l['data'])
        if (x['transaction_hash'], x['log_index']) not in seen:
            seen.add((x['transaction_hash'], x['log_index']))
            D['logs'].append(x)
            hs.add(x['transaction_hash'])
            D['bt'].setdefault(x['block_number'], int(l['blockTimestamp'], 16) if l.get('blockTimestamp') else int(time.time()))
    hs = sorted(h for h in hs if h not in D['txs'])
    if hs:
        tx = _batch([('eth_getTransactionByHash', [h]) for h in hs])
        rc = _batch([('eth_getTransactionReceipt', [h]) for h in hs])
        for h, t, r in zip(hs, tx, rc):
            if t and r:
                D['txs'][h] = dict(hash=h, block_number=int(t['blockNumber'], 16), **{'from': t['from'], 'to': t['to'], 'input': t['input'],
                                   'value': t['value'], 'gas_used': r['gasUsed'], 'effective_gas_price': r['effectiveGasPrice']})


def traded(D):
    """Tokens the wallet moved in a tx it signed. Everything else only ever arrived unasked (spam airdrops)."""
    o = D['owner']
    own = {h for h, t in D['txs'].items() if (t.get('from') or '').lower() == o}
    return {l['address'].lower() for l in D['logs'] if l['transaction_hash'] in own and l['address'].lower() != POSM}


def snapshot(D):
    """Every pinned read the report will make, in one burst: balances of every token the wallet touched, its ETH, and each
    NFT it still holds (liquidity, pool key, ticks, pool price, fee growth). Later calls are served from SNAP."""
    o = D['owner'][2:]
    toks = sorted(traded(D) | {USDG, WETH})
    batch([call(t, '0x70a08231' + o.rjust(64, '0')) for t in toks] + [('eth_getBalance', [D['owner'], PIN['at']])])
    held = set()
    for l in sorted(D['logs'], key=lambda l: (l['block_number'], l['log_index'])):
        if l['address'].lower() == POSM and l.get('topic3'):
            tid = int(l['topic3'], 16)
            if (l.get('topic2') or '')[-40:] == o:
                held.add(tid)
            elif (l.get('topic1') or '')[-40:] == o:
                held.discard(tid)
    # empties never burned (one wallet holds 961): liquidity first, pool state only where some is left
    held = sorted(held)
    liq = batch([call(POSM, '0x1efeed33' + '%064x' % t) for t in held])
    open_reads([t for t, L in zip(held, liq) if L and int(L, 16)])


def book(D):
    o = D['owner'][2:]
    flow = collections.defaultdict(collections.Counter)         # tx -> token -> the wallet's net raw amount
    from_pm = collections.defaultdict(collections.Counter)      # tx -> token -> paid to the wallet by the PoolManager
    blk, mint, burn, order = {}, {}, {}, {}
    for l in sorted(D['logs'], key=lambda l: (l['block_number'], l['log_index'])):
        h, a = l['transaction_hash'], l['address'].lower()
        blk[h] = l['block_number']
        order.setdefault(h, (l['block_number'], l['log_index']))
        t1, t2 = l.get('topic1') or '', l.get('topic2') or ''
        if a == POSM:
            if l.get('topic3'):
                tid = int(l['topic3'], 16)
                if int(t1, 16) == 0 and t2[-40:] == o:
                    mint[tid] = h
                elif t1[-40:] == o and int(t2, 16) == 0:
                    burn[tid] = h
            continue
        x = int(l['data'], 16) if l['data'] not in ('0x', None) else 0
        if t2[-40:] == o:
            flow[h][a] += x
            if t1[-40:] == F.PM[2:]:
                from_pm[h][a] += x
        if t1[-40:] == o:
            flow[h][a] -= x
    toks = sorted({a for f in flow.values() for a in f} | {USDG, WETH})
    dec = dict(zip(toks, [int(r, 16) if r and r != '0x' else 18 for r in batch([call(t, '0x313ce567', pin=False) for t in toks])]))
    ts = sorted((b, D['bt'][b]) for b in D['bt'])
    tsof = lambda b: near(ts, b)
    eth = eth_series(ts[0][1], int(time.time()))
    eusd = lambda b: near(eth, tsof(b))
    own = {h for h, t in D['txs'].items() if t['from'].lower() == D['owner']}
    nft = set(mint.values()) | set(burn.values())
    hum = lambda h, t: flow[h][t] / 10 ** dec[t]
    # the wallet's own trades (no NFT in the tx, USDG one way, one token the other): its sale/buy prices, by block
    trades = collections.defaultdict(list)
    for h in own - nft:
        f = {t: v for t, v in flow[h].items() if v and t != USDG}
        if flow[h][USDG] and len(f) == 1:
            (t, v), = f.items()
            if (v > 0) != (flow[h][USDG] > 0):
                trades[t].append((blk[h], abs(hum(h, USDG) / hum(h, t)), v < 0))
    for v in trades.values():
        v.sort()

    def usd(h, t, sign=1):
        """USD of the wallet's flow of token t in tx h (sign=-1: what went out)."""
        x = sign * hum(h, t)
        if t == USDG:
            return x
        if t == WETH:
            return x * eusd(blk[h])
        # the wallet's own nearest trade, and only within a day: a stale price is worse than none (HOOD10 valued at a
        # trade weeks away turned a $750 position into +$55,101)
        tr = [r for r in trades.get(t, []) if abs(r[0] - blk[h]) <= DAY]
        return x * min(tr, key=lambda r: abs(r[0] - blk[h]))[1] if tr else None

    # the unit is the MINT TX: a ladder of several NFTs minted together is one position. It is closed when every NFT is
    # burned OR empty (liquidity 0 at the pinned block: a DECREASE-to-zero never burned is closed too). Every other
    # PositionManager call of the wallet (increase, decrease, collect) is attributed to the tokenIds its calldata names.
    ladders = collections.defaultdict(list)
    for tid, h0 in mint.items():
        ladders[h0].append(tid)
    touch = collections.defaultdict(list)                       # tokenId -> its non-mint, non-burn PositionManager txs
    for h in own - nft:
        if (D['txs'][h].get('to') or '').lower() == POSM:
            for tid in posm_tids(D['txs'][h].get('input') or ''):
                touch[tid].append(h)
    # ladders that share a tx (one multicall decreasing NFTs of two ladders) become ONE position: transfers cannot split
    # that tx's cash between them, and giving it to both counted it twice
    root = {h0: h0 for h0 in ladders}

    def find(x):
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x
    by_tx = collections.defaultdict(set)
    for tid, hs in touch.items():
        if tid in mint:
            for h in hs:
                by_tx[h].add(mint[tid])
    for tid, h in burn.items():
        if tid in mint:
            by_tx[h].add(mint[tid])
            if h in ladders:                                       # a re-range: the tx that burns it mints its successor
                by_tx[h].add(h)
    for hs in by_tx.values():
        hs = sorted(hs)
        for x in hs[1:]:
            root[find(x)] = find(hs[0])
    merged = collections.defaultdict(list)
    for h0, tids in ladders.items():
        merged[find(h0)] += [(h0, t) for t in tids]
    burned_txs = set(burn.values())
    live = [t for t in mint if t not in burn]
    liq = dict(zip(live, [int(r, 16) if r else 0 for r in batch([call(POSM, '0x1efeed33' + '%064x' % t) for t in live])]))
    rows, skipped, burned_by, skipped_cash = [], collections.Counter(), {}, [0.0]
    for grp in merged.values():
        allm = sorted({h for h, _ in grp}, key=order.get)
        tids = [t for _, t in grp]
        # the chain opens with its pure mints; a re-range tx (burn + mint) is a later step: what it adds is more capital,
        # what it takes out is a return
        mints = [m for m in allm if m not in burned_txs]
        if not mints:
            skipped['re-range chain opened before the scan'] += 1
            D.setdefault('skips', []).append(dict(tokenIds=sorted(tids), why='re-range chain opened before the scan', open_blk=blk.get(allm[0])))  # stored, never lost
            continue
        h0 = mints[0]
        out = {t: -v for m in mints for t, v in flow[m].items() if v < 0}
        vals = [usd(m, t, -1) for m in mints for t, v in flow[m].items() if v < 0]
        inv = sum(v for v in vals if v) - sum(usd(m, t) or 0 for m in mints for t, v in flow[m].items() if v > 0)
        extra = sorted({h for t in tids for h in touch.get(t, [])}, key=order.get)
        nat = any(int(D['txs'].get(m, {}).get('value') or '0x0', 16) for m in mints)
        if nat or not out or None in vals or inv <= 0:
            # A POSITION THIS BOOK CANNOT PRICE, for the REPLAY (copydesk/lpcopy): paid in native ETH (tx value), or a
            # token-only range in a token/ETH pool (no ETH sent, and the token has no USDG trade to price it - 39 of
            # 0x12c2's last 40 mints on 2026-09-30). Its pool, ticks and life come from the chain; only its own P&L cannot,
            # so the replay prices our copy and leaves the leader's result unknown, and the copy rule decides if it counts.
            # Closed = every NFT burned or emptied, like any other position.
            ends_ = sorted({burn[t] for t in tids if t in burn} | set(extra) | (set(allm) - set(mints)), key=order.get)
            if ends_ and all(t in burn or liq.get(t, 1) == 0 for t in tids):
                D.setdefault('native', []).append(dict(tokenIds=sorted(tids), open_blk=blk[h0], close_blk=blk[ends_[-1]]))
        if not out and any(int(D['txs'].get(m, {}).get('value') or '0x0', 16) for m in mints):
            # paid in native ETH (tx value): a native-ETH pool. Native ETH moves without Transfer logs in and out, and this
            # chain has no free traces, so the position cannot be priced from logs. Counted, never guessed.
            skipped['native-ETH pool (not covered)'] += 1
            D.setdefault('skips', []).append(dict(tokenIds=sorted(tids), why='native-ETH pool (not covered)', open_blk=blk.get(allm[0])))  # stored, never lost
            continue
        if not out or None in vals or inv <= 0:
            skipped['open not priceable'] += 1
            D.setdefault('skips', []).append(dict(tokenIds=sorted(tids), why='open not priceable', open_blk=blk.get(allm[0])))  # stored, never lost
            if blk[h0] > D['frm']:
                skipped_cash[0] += sum(hum(x, USDG) for x in set(mints) | {burn[t] for t in tids if t in burn} | set(extra))
            continue
        b0 = blk[h0]
        ends = sorted({burn[t] for t in tids if t in burn} | set(extra) | (set(allm) - set(mints)), key=order.get)
        back, got = 0.0, set()
        for h in ends:
            burned_by[h] = len(rows)
            for t, v in flow[h].items():
                if v > 0:
                    got.add(t)
                    back += usd(h, t) if t in (USDG, WETH) else 0.0
                elif v < 0:                                       # an INCREASE: more capital in
                    inv += usd(h, t, -1) or 0.0
        closed = all(t in burn or liq.get(t, 1) == 0 for t in tids) and bool(ends)
        r = dict(tokenId=min(tids), tokenIds=sorted(tids), open_blk=b0, t_open=tsof(b0), invested_usd=inv, invested_eth=inv / eusd(b0),
                 closed=closed, back=back, tokens=sorted(t for t in set(out) | got if t not in (USDG, WETH)))
        if closed:
            b1 = blk[ends[-1]]
            r.update(close_blk=b1, age_h=(tsof(b1) - tsof(b0)) / 3600)
        if any(int(D['txs'].get(m, {}).get('value') or '0x0', 16) for m in mints):
            r['native'] = True                   # its ETH side is invisible to logs: in the wallet book, never replayed from here
        rows.append(r)
    # the price each position EXITED at, per returned token (USD per whole token): its decrease receipts give liquidity and
    # ticks, and the split it paid out pins the pool price inside its own range. A token that later leaves without a USDG
    # sale (sold for native ETH, sent on) is worth this, not the wallet's last trade: HOOD10 was bought at $4.76 six hours
    # before a crash that dumped 11,518 of it into a $750 position; the last trade priced that at +$55,101.
    exit_px = exit_prices(D, rows, burned_by, flow, dec, lambda h: eusd(blk[h]))
    # FIFO over every token lot the wallet held: a burn's returned tokens are that position's lot; each later outflow
    # consumes the oldest lots first. A sale (USDG in, that token out) pays its own price; a token that leaves any other way
    # (into a new mint, out for ETH, to another address) is valued at the wallet's nearest trade price. Lots still held at
    # the end are marked at today's price. Before: the whole return was priced at the NEXT sale, which flattered the book.
    lots = collections.defaultdict(collections.deque)          # token -> [row index or None, raw amount, unit cost]
    paid, direct, ptok = collections.Counter(), collections.Counter(), collections.Counter()
    lp_tx = set(nft) | set(burned_by)                              # txs that belong to the LP trade: mints, burns, decreases, sales
    unpriced = set()
    native = [0.0]                                             # USD credited above for tokens swapped against native ETH

    def consume(t, left, h, unit, into=None, per_lot=False):
        """Take `left` of token t off its oldest lots at `unit` USD each; with `into` = (token, amount in), the lots
        move into that token instead (a token-for-token swap: nothing is realized until the new token is sold). per_lot:
        a lot returned by a position is valued at THAT position's exit price when known, not the outflow's one unit."""
        total = left
        base = unit
        while left and lots[t]:
            lot = lots[t][0]
            take = min(left, lot[1])
            unit = exit_px.get((lot[0], t), base) if per_lot and lot[0] is not None else base
            if into:
                t2, got = into
                lots[t2].append([lot[0], take * got // total, lot[2] * (take / 10 ** dec[t]) / (take * got / total / 10 ** dec[t2]) if got else 0.0])
            elif lot[0] is not None and unit is None:
                unpriced.add(lot[0])                               # left the wallet with no price within a day: unknown
            elif unit is None:
                pass
            elif lot[0] is not None:
                lp_tx.add(h)                                       # selling a position's returned token is part of the LP trade
                paid[lot[0]] += take / 10 ** dec[t] * unit
                ptok[t] += take / 10 ** dec[t] * unit
            elif blk[h] > D['frm']:                                # a token the wallet bought (or was sent), not an LP return
                direct[t] += take / 10 ** dec[t] * (unit - lot[2])
            lot[1] -= take
            left -= take
            if not lot[1]:
                lots[t].popleft()

    for h in sorted(flow, key=order.get):
        f = {t: v for t, v in flow[h].items() if v}
        ins = {t: v for t, v in f.items() if v > 0 and t not in (USDG, WETH)}
        outs = {t: -v for t, v in f.items() if v < 0 and t not in (USDG, WETH)}
        quote = f.get(USDG, 0) or f.get(WETH, 0)
        mine = h in own and h not in nft
        if mine and not quote and len(ins) == 1 and len(outs) == 1:
            (t, left), = outs.items()
            consume(t, left, h, 0.0, into=next(iter(ins.items())))
            continue
        for t, left in outs.items():
            sale = mine and f.get(USDG, 0) > 0 and len(outs) == 1 and not ins
            v_ = None if sale else usd(h, t, -1)
            unit = hum(h, USDG) / (left / 10 ** dec[t]) if sale else (v_ / (left / 10 ** dec[t]) if v_ is not None else None)
            # EACH LOT AT ITS OWN POSITION'S EXIT PRICE. Only the oldest lot's owner was asked before: 0xcc041520 kept 2,205
            # CREDITS from a buy, so the 1.38M CREDITS its position returned were all valued at that buy's pre-crash price
            if mine and not quote and not ins and blk[h] > D['frm']:
                # sold for native ETH: its proceeds reach the native leg too - credited at the same per-lot prices
                q_, rest = 0.0, left
                for lot in lots[t]:
                    tk = min(rest, lot[1])
                    u_ = exit_px.get((lot[0], t), unit) if not sale and lot[0] is not None else unit
                    q_ += tk / 10 ** dec[t] * (u_ or 0.0)
                    rest -= tk
                    if not rest:
                        break
                if unit is not None or q_:
                    native[0] += q_
            consume(t, left, h, unit, per_lot=not sale)
        for t, v in ins.items():
            buy = mine and f.get(USDG, 0) < 0 and len(ins) == 1 and not outs
            cost = -hum(h, USDG) / (v / 10 ** dec[t]) if buy else 0.0
            if mine and not quote and not outs and blk[h] > D['frm']:
                cost = (usd(h, t) or 0) / (v / 10 ** dec[t])       # bought with native ETH: its cost is in the native leg
                native[0] -= v / 10 ** dec[t] * cost
            lots[t].append([burned_by.get(h), v, cost])
    # a Transfer log is a claim, the balance is the fact: spam tokens emit Transfer events that no balance backs (INVOKER
    # "sent" this wallet 9.9M tokens worth $931 at DexScreener's price; balanceOf is 0). Trim what is left of every token's
    # lots, oldest first, down to the real balance at the pinned block.
    # ponytail: tokens only ever airdropped (never in a tx it signed) are not its trade; one wallet had ~1,900 of them
    keep = traded(D)
    still = sorted(t for t, q in lots.items() if q and t in keep)
    for t in [t for t in lots if t not in keep]:
        lots[t].clear()
    bal = dict(zip(still, [int(r, 16) if r and r != '0x' else 0 for r in batch([call(t, '0x70a08231' + o.rjust(64, '0')) for t in still])]))
    for t in still:
        extra = sum(l[1] for l in lots[t]) - bal[t]
        while extra > 0 and lots[t]:
            take = min(extra, lots[t][0][1])
            lots[t][0][1] -= take
            extra -= take
            if not lots[t][0][1]:
                lots[t].popleft()
    still = {t: lots[t] for t in still if lots[t]}
    px = mark(D, sorted(still), trades)
    held = collections.defaultdict(list)
    for t, q in still.items():
        for i, amt, cost in q:
            if i is not None:
                paid[i] += amt / 10 ** dec[t] * px[t]
                ptok[t] += amt / 10 ** dec[t] * px[t]
                held[i].append(t)
            else:
                direct[t] += amt / 10 ** dec[t] * (px[t] - cost)
    for i, r in enumerate(rows):
        if not r['closed']:
            r['returned_usd'] = r.pop('back') + paid[i]             # partial exits (decreases) of a still-open ladder
            continue
        if i in unpriced:
            r.pop('back')
            r['unpriced'] = True
            skipped['closed, returned token left with no price (not counted)'] += 1
            continue
        if r['closed']:
            r['unsold'] = held.get(i, [])
            r['returned_usd'] = r['back'] + paid[i]
            r['realized_usd'] = r.pop('back') + paid[i] - r['invested_usd']
            r['realized_pct'] = 100 * r['realized_usd'] / r['invested_usd']
    # gas of the LP trade only: its mints, burns, decreases/collects and the sales of returned tokens
    gas = collections.Counter()
    for h in own & lp_tx:
        t = D['txs'][h]
        if t.get('gas_used'):
            gas[int(tsof(blk[h]) // 86400)] += int(t['gas_used'], 16) * int(t['effective_gas_price'], 16) / 1e18 * eusd(blk[h])
    D.update(touch=dict(touch))                                   # tokenId -> its decrease / collect / increase txs (lpcopy._paid)
    D.update(dec=dec, flow=flow, own=own, blk=blk, eusd=eusd, tsof=tsof, trades=trades, mint=mint, burn=burn,
             direct_usd=sum(direct.values()), skipped_cash=skipped_cash[0], native_credit=native[0], lp_txs=set(lp_tx), lots=lots, dexpx=px, paid=paid, direct=direct, ptok=ptok)
    return rows, gas, dict(skipped)


def open_value(D, tids):
    """Positions the wallet still holds, valued NOW: principal (liquidity at the pool price) + unclaimed fees (StateView fee
    growth inside the range minus the position's last snapshot), in USD and ETH, with the range as USDG per token.
    Three batched latest requests whatever the count."""
    if not tids:
        return 0.0, []
    tids = sorted(tids)
    info, liq, keys, st = open_reads(tids)
    e = D['eusd'](D['head'])
    tot, out = 0.0, []
    sg = lambda v: v - (1 << 24) if v >> 23 else v
    for i, (t, x, L, k) in enumerate(zip(tids, info, liq, keys)):
        L = int(L, 16) if L else 0
        if not (x and k):
            continue
        s0, fg, pinf = st[3 * i:3 * i + 3]
        pid, tl, tu = k
        c0, c1 = '0x%040x' % word(x, 0), '0x%040x' % word(x, 1)
        d0, d1 = D['dec'].get(c0, 18), D['dec'].get(c1, 18)
        sp = word(s0, 0) / 2 ** 96
        sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
        spc = min(max(sp, sa), sb)
        a0, a1 = L * (sb - spc) / (spc * sb), L * (spc - sa)
        f0 = ((word(fg, 0) - word(pinf, 1)) % (1 << 256)) * L >> 128
        f1 = ((word(fg, 1) - word(pinf, 2)) % (1 << 256)) * L >> 128
        px = sp * sp * 10 ** d0 / 10 ** d1                        # token1 per token0
        if c0 == USDG:
            usd = lambda x0, x1: x0 / 10 ** d0 + x1 / 10 ** d1 / px
            rng = (10 ** d0 / 10 ** d1 * 1.0001 ** tu, 10 ** d0 / 10 ** d1 * 1.0001 ** tl)
            rng = (1 / rng[0], 1 / rng[1])                         # USDG per token
        elif c1 == USDG:
            usd = lambda x0, x1: x1 / 10 ** d1 + x0 / 10 ** d0 * px
            rng = (1.0001 ** tl * 10 ** d0 / 10 ** d1, 1.0001 ** tu * 10 ** d0 / 10 ** d1)
        else:
            continue
        v, f = usd(a0, a1), usd(f0, f1)
        row = next((r for r in D.get('rows', []) if t in r['tokenIds']), None)
        tot += v + f
        o = dict(tokenId=t, pair='%s/%s' % (sym(D, c0), sym(D, c1)), value_usd=v, fee_usd=f, value_eth=v / e, fee_eth=f / e,
                 in_range=sa <= sp < sb, range_usdg=rng, liquidity=L)
        if row:
            o.update(invested_usd=row['invested_usd'], invested_eth=row['invested_eth'], age_h=(time.time() - row['t_open']) / 3600,
                     upnl_usd=v + f - row['invested_usd'], upnl_pct=100 * (v + f - row['invested_usd']) / row['invested_usd'])
        out.append(o)
    return tot, out


def exit_prices(D, rows, burned_by, flow, dec, eth_at):
    """(row index, token) -> USD per whole token at the position's exit, from its decrease/burn receipts: liquidity + ticks
    + the payout split (in range: sqrtP = sqrtPa + amount1 / L; one-sided: the range edge it sits beyond). The payout
    includes fees, a slight tilt toward the side the fees came in; the range bounds cap the error."""
    ends = collections.defaultdict(list)
    for h, i in burned_by.items():
        if any(v > 0 and t not in (USDG, WETH) for t, v in flow[h].items()):
            ends[i].append(h)
    hs = sorted({h for v in ends.values() for h in v})
    rec = dict(zip(hs, batch([('eth_getTransactionReceipt', [h]) for h in hs]))) if hs else {}
    mod = '0x' + keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
    sg = lambda x: (lambda v: v - (1 << 256) if v >> 255 else v)(int(x, 16))
    pids = sorted({l['topics'][1] for r in rec.values() for l in (r or {}).get('logs', []) if l['topics'][0] == mod})
    sel = keccak(text='poolKeys(bytes25)')[:4].hex()
    pk = {p: ('0x' + x[26:66].lower(), '0x' + x[90:130].lower())
          for p, x in zip(pids, batch([call(POSM, '0x' + sel + p[2:52].ljust(64, '0'), pin=False) for p in pids])) if x and len(x) >= 130}
    # THE MARKET PRICE AT THE EXIT, NOT THE PAYOUT SPLIT'S. The split is exact only while the position was in range: a
    # position that exited below its range pays out all token plus the fees it earned in the quote, and those fees make the
    # split look in range, anywhere inside a range that can span 4-5x. 0xcc041520's CREDITS exit (2026-09-23, tx
    # 0xf93e571c): 1.38M CREDITS valued at ~$2,607 by the split, sold 25 s later for $507.76, so a -$710 position booked
    # +$1,389. The pool's last Swap log before the exit carries the exact sqrtPriceX96; the split stays the fallback.
    last_sp = {}
    want = []
    for h in hs:
        r = rec.get(h) or {}
        for l in r.get('logs', []):
            if l['topics'][0] == mod:
                want.append((l['topics'][1], int(r['blockNumber'], 16), int(l['logIndex'], 16)))
    want = sorted(set(want))
    # our node, one query per window (lpcopy.window_swaps): a batch of 121 getLogs went over the node's response cap and
    # the public fallback refused it too, which failed every replay of a busy wallet (2026-09-29)
    try:
        from copydesk import lpcopy
        sw = lpcopy.window_swaps([(p, max(0, b - 3000), b) for p, b, _ in want]) if want else {}
    except Exception:
        sw = {}
        got = batch([('eth_getLogs', [dict(address=PM, topics=[SWAP4, p], fromBlock=hex(max(0, b - 3000)), toBlock=hex(b))])
                     for p, b, _ in want]) if want else []
        for (p, b, li), logs in zip(want, got):
            sw.setdefault(p, []).extend((int(lg['blockNumber'], 16), int(lg['logIndex'], 16), 0, 0,
                                         int(lg['data'][2 + 128:2 + 192], 16) / 2 ** 96) for lg in logs or [])
    for p, b, li in want:
        prior = [x for x in sw.get(p, []) if b - 3000 <= x[0] and (x[0], x[1]) < (b, li)]
        if prior:
            last_sp[(p, b)] = max(prior)[4]
    out = {}
    for i, hl in ends.items():
        tids = set(rows[i]['tokenIds'])
        px = collections.defaultdict(list)
        for h in hl:
            for l in (rec.get(h) or {}).get('logs', []):
                if l['topics'][0] != mod or int(l['data'][194:258], 16) not in tids:
                    continue
                w = l['data'][2:]
                tl, tu, L = sg(w[0:64]), sg(w[64:128]), -sg(w[128:192])
                if L <= 0:
                    continue
                c0, c1 = pk.get(l['topics'][1], (None, None))
                if not c0 or c0 == '0x' + '0' * 40:
                    continue                                       # unknown pool, or native ETH (its payout has no log)
                sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
                g0, g1 = max(0, flow[h].get(c0, 0)), max(0, flow[h].get(c1, 0))
                sp = last_sp.get((l['topics'][1], int((rec.get(h) or {}).get('blockNumber', '0x0'), 16)))
                if not sp or not (sa / 100 <= sp <= sb * 100):
                    # a drained pool's last swap sits at the price limit (GIT/USDG, 9.9% fee: 'USDG per GIT' read 5e55) - no
                    # market there; the split, bounded by the range, is the better number
                    sp = sa if not g1 else sb if not g0 else min(sb, max(sa, sa + g1 / L))
                p10 = sp * sp * 10 ** dec[c0] / 10 ** dec[c1]            # c1 per c0, whole tokens
                for t, q, per in ((c0, c1, p10), (c1, c0, 1 / p10 if p10 else 0)):
                    if t not in (USDG, WETH) and q in (USDG, WETH):
                        px[t].append(per * (1.0 if q == USDG else eth_at(h)))
        for t, v in px.items():
            out[(i, t)] = sorted(v)[len(v) // 2]
    return out


def open_reads(tids):
    """The pinned reads behind open_value: pool key + ticks + liquidity per NFT, then pool price, fee growth inside the
    range and the position's fee snapshot. Two batched requests; the second waits on the first's pool keys."""
    if not tids:
        return [], [], [], []
    r = batch([call(POSM, '0x7ba03aad' + '%064x' % t) for t in tids] + [call(POSM, '0x1efeed33' + '%064x' % t) for t in tids])
    info, liq = r[:len(tids)], r[len(tids):]
    sg = lambda v: v - (1 << 24) if v >> 23 else v
    enc = lambda v: '%064x' % (v % (1 << 256))
    keys = []
    for x in info:
        if not x:
            keys.append(None)
            continue
        pi = word(x, 5)
        keys.append(('0x' + keccak(bytes.fromhex(x[2:2 + 64 * 5])).hex(), sg((pi >> 8) & 0xffffff), sg((pi >> 32) & 0xffffff)))
    q = []
    for t, k in zip(tids, keys):
        if k:
            pid, tl, tu = k
            q += [call(SV, '0xc815641c' + pid[2:]),
                  call(SV, '0x53e9c1fb' + pid[2:] + enc(tl) + enc(tu)),
                  call(SV, '0xdacf1d2f' + pid[2:] + POSM[2:].rjust(64, '0') + enc(tl) + enc(tu) + '%064x' % t)]
        else:
            q += [('eth_blockNumber', [])] * 3
    return info, liq, keys, batch(q)


def sym(D, t):
    if t == USDG:
        return 'USDG'
    c = D.setdefault('sym', {})
    if t not in c:
        r = batch([call(t, '0x95d89b41', pin=False)])[0]
        try:
            n = int(r[66:130], 16)
            c[t] = bytes.fromhex(r[130:130 + 2 * n]).decode()
        except Exception:
            c[t] = t[:8]
    return c[t]


def dex_prices(toks):
    """USD price now per token (DexScreener, deepest pair, 30 tokens a request); 0 when it has none."""
    px = {}
    for i in range(0, len(toks), 30):
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(
                'https://api.dexscreener.com/latest/dex/tokens/' + ','.join(toks[i:i + 30]), headers={'User-Agent': 'x'}), timeout=20).read())
        except Exception:
            continue
        for p in sorted(d.get('pairs') or [], key=lambda p: (p.get('liquidity') or {}).get('usd', 0)):
            if p.get('priceUsd'):
                px[p['baseToken']['address'].lower()] = float(p['priceUsd'])
    return {t: px.get(t, 0.0) for t in toks}


def mark(D, toks, trades):
    """Today's price to mark a held token: DexScreener, or the wallet's own last trade price when DexScreener has none
    (a fresh launch has no pair there yet; marking it at $0 turned a $100 buy into a $100 loss)."""
    px = dex_prices(toks)
    return {t: px[t] or (trades[t][-1][1] if trades.get(t) else 0.0) for t in toks}


def wallet(D, rows, open_now):
    """The wallet's whole P&L over the window, LP and directional trades alike: USDG/WETH cash of its own txs + the change in
    tokens held (start at its own nearest trade price, end at DexScreener's) + open positions (start at cost, end at value)
    - gas. USDG/WETH arriving in txs it did not sign is a deposit, reported apart, never P&L."""
    frm, o, dec, fl, blk = D['frm'], D['owner'], D['dec'], D['flow'], D['blk']
    after = [h for h in fl if blk[h] > frm]
    e0, e1 = D['eusd'](frm), D['eusd'](D['head'])
    cash = sum(fl[h][USDG] / 1e6 + fl[h][WETH] / 1e18 * D['eusd'](blk[h]) for h in after if h in D['own'])
    dep = sum(fl[h][USDG] / 1e6 + fl[h][WETH] / 1e18 * D['eusd'](blk[h]) for h in after if h not in D['own'])
    toks = sorted(traded(D) - {USDG, WETH})
    now = dict(zip(toks, [int(r, 16) if r and r != '0x' else 0 for r in batch([call(t, '0x70a08231' + o[2:].rjust(64, '0')) for t in toks])]))
    px = mark(D, toks, D['trades'])
    inv0 = inv1 = 0.0
    for t in toks:
        b0 = now[t] - sum(fl[h][t] for h in after)
        tr = D['trades'].get(t)
        p0 = min(tr, key=lambda r: abs(r[0] - frm))[1] if tr else px[t]
        inv0 += max(0, b0) / 10 ** dec[t] * p0
        inv1 += now[t] / 10 ** dec[t] * px[t]
    open0 = sum(r['invested_usd'] for r in rows if r['open_blk'] <= frm < r.get('close_blk', 1 << 62))
    gas = sum(int(D['txs'][h]['gas_used'], 16) * int(D['txs'][h]['effective_gas_price'], 16) / 1e18 * D['eusd'](blk[h])
              for h in D['own'] if blk[h] > frm and D['txs'][h].get('gas_used'))
    # native ETH is invisible to Transfer logs (bought with USDG from routers, spent on gas), so take the balance at both
    # ends: gas is inside that change. Subtracting gas again double-counted it (the USDG that bought the ETH is in cash).
    n1 = int(batch([('eth_getBalance', [o, PIN['at']])])[0], 16) / 1e18 * e1
    n0 = archive_balance(o, frm)
    n0 = n0 / 1e18 * e0 if n0 is not None else None
    nft = set(D['mint'].values()) | set(D['burn'].values()) | D['lp_txs']    # a decrease paying only USDG is an LP return
    usdg_only = sum(fl[h][USDG] / 1e6 for h in after if h in D['own'] and h not in nft and not any(v for t, v in fl[h].items() if t != USDG))
    return dict(cash=cash, deposits=dep, tokens_start=inv0, tokens_end=inv1, open_start=open0, open_end=open_now, gas=gas, usdg_only=usdg_only,
                native_start=n0, native_end=n1, pnl=cash + inv1 - inv0 + open_now - open0 + n1 - (n0 if n0 is not None else n1))


def report(addr, days):
    t = time.time()
    D = fetch(addr, days)
    tf = time.time() - t
    rows, gas, skipped = book(D)
    D['rows'] = rows
    held = [t for r in rows if not r['closed'] for t in r['tokenIds'] if t not in D['burn']]
    lq = batch([call(POSM, '0x1efeed33' + '%064x' % t) for t in held])
    held = [t for t, L in zip(held, lq) if L and int(L, 16)]
    now_open, opens = open_value(D, held)
    win = [r for r in rows if r['open_blk'] >= D['frm']]
    done = [r for r in win if 'realized_usd' in r]
    daily = collections.defaultdict(lambda: dict(n=0, won=0, usd=0.0, invested=0.0))
    for r in done:
        d = daily[int(D['tsof'](r['close_blk']) // 86400)]
        d['n'] += 1
        d['won'] += r['realized_usd'] > 0
        d['usd'] += r['realized_usd']
        d['invested'] += r['invested_usd']
    days_ = sorted(set(daily) | {k for k in gas if k >= D['tsof'](D['frm']) // 86400})
    for k in days_:
        daily[k]['gas'] = gas.get(k, 0.0)
        daily[k]['net'] = daily[k]['usd'] - daily[k]['gas']
    pct = sorted(r['realized_pct'] for r in done)
    Wl = wallet(D, rows, now_open)
    # every dollar of the wallet's P&L attributed; what is left is what this tool cannot explain (report it, never hide it)
    pre = sum(r['realized_usd'] for r in rows if r['open_blk'] < D['frm'] and r.get('close_blk', 0) > D['frm'] and 'realized_usd' in r)
    # per ladder, not per NFT: a ladder's invested is subtracted once from the value of ALL its NFTs still held
    val = collections.Counter()
    for o in opens:
        val[next((i for i, r in enumerate(rows) if o['tokenId'] in r['tokenIds']), None)] += o['value_usd'] + o['fee_usd']
    upnl = sum(v + (rows[i].get('returned_usd', 0) - rows[i]['invested_usd'] if i is not None else 0)
               for i, v in val.items() if i is None or not rows[i]['closed'])
    parts = dict(lp_book=sum(r['realized_usd'] for r in done), opened_before=pre, open_upnl=upnl, directional=D['direct_usd'],
                 skipped_ladders=D['skipped_cash'],
                 native_leg=Wl['usdg_only'] + Wl['native_end'] - (Wl['native_start'] if Wl['native_start'] is not None else Wl['native_end'])
                 - D['native_credit'])
    Wl['explained'] = parts
    Wl['unexplained'] = Wl['pnl'] - sum(parts.values())
    # the guard: a report whose parts do not add up to the wallet's own change says so on its first line
    Wl['reconciled'] = abs(Wl['unexplained']) <= max(30.0, 0.001 * sum(r['invested_usd'] for r in rows))
    R = dict(address=addr.lower(), days=days, secs=round(time.time() - t, 1), fetch_secs=round(tf, 1), logs=len(D['logs']),
                txs=len(D['own']), positions=len(win), closed=sum(r['closed'] for r in win), realized=len(done),
                unsold=sum(1 for r in win if r.get('unsold')), skipped=skipped,
                win_pct=100 * sum(r['realized_usd'] > 0 for r in done) / len(done) if done else None,
                median_pct=S.median(pct) if pct else None, p10_pct=pct[len(pct) // 10] if pct else None,
                realized_usd=sum(r['realized_usd'] for r in done), gas_usd=sum(daily[k]['gas'] for k in days_),
                lp_pnl=sum(r['realized_usd'] for r in done) + Wl['explained']['open_upnl'] - sum(daily[k]['gas'] for k in days_),
                invested_usd=sum(r['invested_usd'] for r in done), open_usd=now_open, open=opens, wallet=Wl,
                green_days='%d/%d' % (sum(daily[k]['net'] > 0 for k in days_), len(days_)),
                daily={time.strftime('%m-%d', time.gmtime(k * 86400)): daily[k] for k in days_}, rows=rows)
    try:
        from copydesk import lpdb
        lpdb.save_report(R, D, rows, opens, _batch, lambda t: sym(D, t), USDG)   # the store behind a dashboard
    except Exception as e:
        R['store_error'] = str(e)[:200]
    return R


def line(R):
    """One wallet's LP trade in one line. The wallet's other activity (token bets, ETH routing) is left out; the wallet-level
    reconciliation only vouches for the numbers ('check ok')."""
    return '%s  LP P&L $%+8.2f  (realized $%+.2f, open $%+.2f, LP gas -$%.2f) | %d pos  win %.0f%%  med %+.2f%%  p10 %+.2f%%  green days %s  turnover $%.0f | %s  %.0f s' % (
        R['address'], R['lp_pnl'], R['realized_usd'], R['wallet']['explained']['open_upnl'], R['gas_usd'], R['realized'], R['win_pct'] or 0,
        R['median_pct'] or 0, R['p10_pct'] or 0, R['green_days'], R['invested_usd'],
        ('check ok' if R['wallet']['reconciled'] else 'CHECK FAILED ($%+.0f unexplained)%s' % (R['wallet']['unexplained'],
         ': %d native-ETH positions not covered' % R['skipped']['native-ETH pool (not covered)'] if R['skipped'].get('native-ETH pool (not covered)') else ': do not trust')), R['secs'])


def main(a):
    days = float(a[a.index('--days') + 1]) if '--days' in a else 7
    if a and a[0] == 'check':
        ref = json.load(open(FIX))
        R = report(ref['address'], 3)
        rows = {t: r for r in R['rows'] for t in r['tokenIds']}
        print('%.1f s (fetch %.1f s), %d logs, skipped %s' % (R['secs'], R['fetch_secs'], R['logs'], R['skipped']))
        bad = 0
        for h in ref['historical']:
            m = rows.get(h['tokenId'])
            if not m:
                print('%-9d missing' % h['tokenId']); bad += 1; continue
            ok = abs(m['age_h'] - h['age_h']) < 0.05 and abs(m['invested_eth'] - h['invested_eth']) < 0.001
            bad += not ok
            print('%-9d %s age %5.2f/%5.2f  invested %.4f/%.4f ETH  pnl%% LPA %6.2f  realized %s' % (
                h['tokenId'], 'ok ' if ok else 'BAD', h['age_h'], m['age_h'], h['invested_eth'], m['invested_eth'], h['pnl_pct'],
                '%6.2f' % m['realized_pct'] if 'realized_pct' in m else '   n/a %s' % m.get('unsold', '')))
        # open positions: invested and range are fixed at mint and must match; value and fees move with the market (shown only)
        now = {o['tokenId']: o for o in R['open']}
        for h in ref.get('open', []):
            m = now.get(h['tokenId'])
            if not m:
                print('%-9d open, missing (closed since the snapshot?)' % h['tokenId'])
                continue
            ok = abs(m['invested_eth'] - h['invested_eth']) < 0.001 and all(abs(x / y - 1) < 0.03 for x, y in zip(m['range_usdg'], h['range_usdg']))
            bad += not ok
            print('%-9d %s open  invested %.4f/%.4f ETH  range %.4f-%.4f / %.4f-%.4f  value %.4f/%.4f  fee %.4f/%.4f  uPnL LPA %s / ours %+.2f%%' % (
                h['tokenId'], 'ok ' if ok else 'BAD', h['invested_eth'], m['invested_eth'], *h['range_usdg'], *m['range_usdg'],
                h['value_eth'], m['value_eth'], h['fee_eth'], m['fee_eth'], h['upnl'], m['upnl_pct']))
        print('wallet', 'reconciled' if R['wallet']['reconciled'] else 'DOES NOT RECONCILE', '(unexplained $%+.2f)' % R['wallet']['unexplained'])
        bad += not R['wallet']['reconciled']
        return 1 if bad else 0
    if a and a[0] == 'rank':
        Rs = []
        for x in [x for x in a[1:] if x.startswith('0x')]:
            try:
                Rs.append(report(x, days))
            except Exception as e:
                print(x, 'error', e)
        for R in sorted(Rs, key=lambda R: -R['lp_pnl']):
            print(line(R))
        return
    R = report(a[0], days)
    if '--json' in a:
        print(json.dumps(R, default=str))
        return
    print(line(R))
    print('  %d positions opened, %d closed, %d realized (%d with a returned token still held, marked at today\'s price); skipped %s; open now $%.2f in %d' % (
        R['positions'], R['closed'], R['realized'], R['unsold'], R['skipped'], R['open_usd'], len(R['open'])))
    for x in R['open']:
        print('  OPEN #%d %-18s age %5.2fh  invested %s  value %.4f ETH  fee %.4f ETH  uPnL %s  range %.6g-%.6g USDG %s' % (
            x['tokenId'], x['pair'], x.get('age_h') or 0, '%.4f ETH' % x['invested_eth'] if 'invested_eth' in x else '?',
            x['value_eth'], x['fee_eth'], '%+.2f%%' % x['upnl_pct'] if 'upnl_pct' in x else '?', x['range_usdg'][0], x['range_usdg'][1],
            'in range' if x['in_range'] else 'OUT'))
    for d, x in R['daily'].items():
        print('  %s  %3d closed  won %3d  realized $%+8.2f  LP gas $%5.2f  net $%+8.2f  on $%7.0f' % (
            d, x['n'], x['won'], x['usd'], x['gas'], x['net'], x['invested']))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
