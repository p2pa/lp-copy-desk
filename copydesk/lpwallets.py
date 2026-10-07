"""WHO LPs WELL ON ROBINHOOD: realized P&L of every Uniswap v3 position opened AND closed in the window, per wallet, from
the chain alone (the v3 NonfungiblePositionManager logs exact amounts; v4 does not, so v4 is out of scope here).

  python -m copydesk.lpwallets scan DAYS      pull the window -> var/lpwallets/scan_<DAYS>d.pkl.gz
  python -m copydesk.lpwallets rank DAYS      per-wallet table: active, steady, small wins (docs/LPWALLETS.md)

Per position: deposits (IncreaseLiquidity amounts), withdrawals (DecreaseLiquidity amounts), fees (Collect minus the
decreased principal), each valued in the pool's QUOTE token at the pool's own price at that block (last v3 Swap sqrtPriceX96
at or before it). P&L = withdrawn + fees - deposited, in quote, converted to USD. Gas is not included (a later pass).
"""
import bisect, collections, gzip, json, os, pickle, statistics as S, sys, time, urllib.request

from discovery import forensics as F
from eth_utils import keccak

NPM = '0x73991a25c818bf1f1128deaab1492d45638de0d3'
INC = '0x' + keccak(text='IncreaseLiquidity(uint256,uint128,uint256,uint256)').hex()
DEC = '0x' + keccak(text='DecreaseLiquidity(uint256,uint128,uint256,uint256)').hex()
COL = '0x' + keccak(text='Collect(uint256,address,uint256,uint256)').hex()
XFR = '0x' + keccak(text='Transfer(address,address,uint256)').hex()
MINT = '0x' + keccak(text='Mint(address,address,int24,int24,uint128,uint256,uint256)').hex()
SWAP3 = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
WETH, USDG = '0x0bd7d308f8e1639fab988df18a8011f41eacad73', '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
QUOTES = {WETH: ('WETH', 18), USDG: ('USDG', 6)}
DIR = os.path.join(os.path.dirname(F.INDEX), 'lpwallets')


def _w(data, i):
    return int(data[2 + 64 * i:66 + 64 * i], 16)


def scan(days):
    key = F._key()
    head = int(F._rpc('eth_blockNumber', []), 16)
    frm = head - int(days * 864000)
    npm_t = '0x' + NPM[2:].rjust(64, '0')
    ev, mints, txf, bt = [], [], {}, {}
    body = dict(from_block=frm, logs=[dict(address=[NPM], topics=[[INC, DEC, COL]]),
                                      dict(address=[NPM], topics=[[XFR], ['0x' + '0' * 64]]),      # NFT mints: owner + tokenId
                                      dict(topics=[[MINT], [npm_t]])],                                 # pool Mint by the NPM: pool + ticks
                field_selection=dict(log=['address', 'block_number', 'log_index', 'transaction_hash', 'topic0', 'topic1', 'topic2', 'topic3', 'data'],
                                     transaction=['hash', 'from'], block=['number', 'timestamp']))
    for r in F._pages(body, key):
        for d in r['data']:
            for t in d.get('transactions') or []:
                txf[t['hash']] = t['from'].lower()
            for b in d.get('blocks') or []:
                bt[b['number']] = int(b['timestamp'], 16) if isinstance(b['timestamp'], str) else b['timestamp']
            for lg in d.get('logs') or []:
                a = lg['address'].lower()
                if lg['topic0'] == MINT:
                    mints.append((lg['transaction_hash'], a, lg['topic2'], lg['topic3'], _w(lg['data'], 1)))
                elif a == NPM:
                    ev.append((lg['block_number'], lg['log_index'], lg['topic0'], lg['topic1'], lg.get('topic2'), lg.get('topic3'),
                               lg['data'], lg['transaction_hash']))
        print('  npm page', r.get('next_block'), 'events', len(ev), 'mints', len(mints), flush=True)
    # tokenId -> pool: the pool Mint in the same tx with the same liquidity as the first IncreaseLiquidity
    by_tx = collections.defaultdict(list)
    for h, pool, tl, tu, liq in mints:
        by_tx[h].append((pool, liq, tl, tu))
    pos = {}
    for blk, li, t0, t1, t2, t3, data, h in sorted(ev):
        if t0 == INC:
            tid = int(t1, 16)
            if tid not in pos:
                liq = _w(data, 0)
                m_ = next((x for x in by_tx.get(h, ()) if x[1] == liq), None)
                pool = m_[0] if m_ else None
                sg = lambda x: (lambda v: v - (1 << 256) if v >> 255 else v)(int(x, 16)) if x else None
                # sender: who sent the mint tx - the real LP when a contract (Delta, a third-party manager) holds the NFT
                pos[tid] = dict(pool=pool, owner=txf.get(h), sender=txf.get(h), open_blk=blk, events=[],
                                tl=sg(m_[2]) if m_ else None, tu=sg(m_[3]) if m_ else None)
    for blk, li, t0, t1, t2, t3, data, h in sorted(ev):
        if t0 == XFR:
            tid = int(t3, 16)
            if tid in pos:
                pos[tid]['owner'] = '0x' + t2[-40:]
            continue
        tid = int(t1, 16)
        if tid not in pos:
            continue                                    # opened before the window: no deposit record, skipped
        if t0 in (INC, DEC):
            pos[tid]['events'].append((blk, li, 'inc' if t0 == INC else 'dec', _w(data, 0), _w(data, 1), _w(data, 2)))
        else:
            pos[tid]['events'].append((blk, li, 'col', 0, _w(data, 1), _w(data, 2)))
    pools = sorted({p['pool'] for p in pos.values() if p['pool']})
    print('positions opened in window', len(pos), 'pools', len(pools), flush=True)
    toks = {}
    for pl in pools:
        try:
            toks[pl] = (_call(pl, 'token0()'), _call(pl, 'token1()'))
        except Exception:
            pass
    qpools = [p for p, (a, b) in toks.items() if a in QUOTES or b in QUOTES]
    prices = collections.defaultdict(list)                   # pool -> [(block, li, sqrtP)]
    for i in range(0, len(qpools), 300):
        body = dict(from_block=frm - 36000, logs=[dict(address=qpools[i:i + 300], topics=[[SWAP3]])],
                    field_selection=dict(log=['address', 'block_number', 'log_index', 'data']))
        for r in F._pages(body, key):
            for d in r['data']:
                for lg in d.get('logs') or []:
                    prices[lg['address'].lower()].append((lg['block_number'], lg['log_index'], _w(lg['data'], 2)))
            print('  swap page', i, r.get('next_block'), flush=True)
    for v in prices.values():
        v.sort()
    os.makedirs(DIR, exist_ok=True)
    out = os.path.join(DIR, 'scan_%dd.pkl.gz' % days)
    with gzip.open(out, 'wb') as fh:
        pickle.dump(dict(pos=pos, toks=toks, prices=dict(prices), bt=bt, frm=frm, head=head, days=days), fh)
    print('saved', out)


def _call(to, sig):
    q = urllib.request.Request('https://robinhood-rpc.publicnode.com', json.dumps(dict(jsonrpc='2.0', id=1, method='eth_call', params=[
        dict(to=to, data='0x' + keccak(text=sig)[:4].hex()), 'latest'])).encode(), {'Content-Type': 'application/json', 'User-Agent': 'x'})
    return '0x' + json.loads(urllib.request.urlopen(q, timeout=15).read())['result'][-40:].lower()


def load(days):
    with gzip.open(os.path.join(DIR, 'scan_%dd.pkl.gz' % days), 'rb') as fh:
        return pickle.load(fh)


def positions(days, eth_usd=2690.0):
    """Closed positions with realized P&L in USD."""
    return positions_of(load(days), eth_usd)


def positions_of(D, eth_usd=2690.0):
    """positions() on any scan-shaped dict (pos, toks, prices, bt): research/lpml/deltav3 builds one for Delta's v3 ladders."""
    out = []
    for tid, p in D['pos'].items():
        pl = p['pool']
        if not pl or pl not in D['toks'] or pl not in D['prices']:
            continue
        t0, t1 = D['toks'][pl]
        if t1 in QUOTES:
            qside, qdec = 1, QUOTES[t1][1]
        elif t0 in QUOTES:
            qside, qdec = 0, QUOTES[t0][1]
        else:
            continue
        qusd = eth_usd if (t0 if qside == 0 else t1) == WETH else 1.0
        px = D['prices'][pl]
        keys = [(b, li) for b, li, _ in px]

        def val(blk, li, a0, a1):
            i = bisect.bisect_right(keys, (blk, li)) - 1
            if i < 0:
                i = 0
            sp = px[i][2] / 2 ** 96
            p1per0 = sp * sp                                   # token1 per token0 (raw units)
            q = a1 + a0 * p1per0 if qside == 1 else a0 + a1 / p1per0 if p1per0 else 0
            return q / 10 ** qdec * qusd
        ev = sorted(p['events'])
        liq, dep, wd, fees, dec_owed = 0, 0.0, 0.0, 0.0, [0, 0]
        for blk, li, k, L, a0, a1 in ev:
            if k == 'inc':
                liq += L
                dep += val(blk, li, a0, a1)
            elif k == 'dec':
                liq -= L
                dec_owed[0] += a0
                dec_owed[1] += a1
                wd += val(blk, li, a0, a1)
            else:
                f0, f1 = max(0, a0 - dec_owed[0]), max(0, a1 - dec_owed[1])
                dec_owed = [max(0, dec_owed[0] - a0), max(0, dec_owed[1] - a1)]
                fees += val(blk, li, f0, f1)
        closed = liq == 0 and any(e[2] == 'dec' for e in ev)
        if not closed or dep <= 0:
            continue
        t_open = D['bt'].get(ev[0][0])
        t_close = D['bt'].get(max(e[0] for e in ev if e[2] == 'dec'))
        out.append(dict(tid=tid, owner=p['owner'], sender=p.get('sender'), tl=p.get('tl'), tu=p.get('tu'), open_blk=p.get('open_blk'),
                        t_open=t_open, pool=pl, dep=dep, pnl=wd + fees - dep, fees=fees,
                        hold_h=((t_close or 0) - (t_open or 0)) / 3600 if t_open and t_close else None, t_close=t_close, n_ev=len(ev)))
    return out


def rank(days, min_n=20):
    ps = positions(days)
    by = collections.defaultdict(list)
    for p in ps:
        by[p['owner']].append(p)
    rows = []
    for o, xs in by.items():
        if len(xs) < min_n:
            continue
        r = [x['pnl'] / x['dep'] for x in xs]              # return per position
        usd = [x['pnl'] for x in xs]
        daily = collections.defaultdict(float)
        for x in xs:
            daily[time.strftime('%m-%d', time.gmtime(x['t_close'] or 0))] += x['pnl']
        rs = sorted(r)
        rows.append(dict(owner=o, n=len(xs), per_day=len(xs) / days, win=sum(1 for v in r if v > 0) / len(r), med_ret=S.median(r),
                         worst10=S.mean(rs[:max(1, len(rs) // 10)]), pnl_usd=sum(usd), dep_med=S.median(x['dep'] for x in xs),
                         hold_med_h=S.median(x['hold_h'] for x in xs if x['hold_h'] is not None) if any(x['hold_h'] for x in xs) else None,
                         days_pos=sum(1 for v in daily.values() if v > 0) / len(daily), days=len(daily),
                         pools=len({x['pool'] for x in xs}), fee_share=sum(x['fees'] for x in xs) / max(1e-9, sum(x['dep'] for x in xs))))
    return ps, rows


def main(a):
    cmd = a[0] if a else 'rank'
    days = int(a[1]) if len(a) > 1 else 7
    if cmd == 'scan':
        return scan(days)
    ps, rows = rank(days)
    print('closed v3 positions with a quote side: %d, wallets with >= 20: %d' % (len(ps), len(rows)))
    steady = [r for r in rows if r['win'] >= 0.7 and r['med_ret'] > 0 and r['pnl_usd'] > 0 and r['worst10'] > -0.10 and r['days_pos'] >= 0.7]
    print('STEADY (win >= 70%%, median > 0, total > 0, worst decile > -10%%, >= 70%% of days positive): %d' % len(steady))
    for r in sorted(steady, key=lambda r: -r['pnl_usd'])[:25]:
        print('  %s n %4d (%5.1f/day) win %3.0f%% med %+5.2f%% worst10 %+6.1f%% pnl $%8.2f dep_med $%7.0f hold %5.1fh days+ %3.0f%% of %d pools %d fees/dep %.1f%%' % (
            r['owner'], r['n'], r['per_day'], 100 * r['win'], 100 * r['med_ret'], 100 * r['worst10'], r['pnl_usd'], r['dep_med'],
            r['hold_med_h'] or 0, 100 * r['days_pos'], r['days'], r['pools'], 100 * r['fee_share']))
    json.dump(dict(rows=rows, days=days), open(os.path.join(DIR, 'rank_%dd.json' % days), 'w'), indent=1)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
