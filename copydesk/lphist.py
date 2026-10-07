"""THE LP HISTORY STORE (owner 2026-09-30: "history doesn't change - keep it once, compressed to what we need"): the chain's v4
LP history in a local database (chains/<chain>/var/lphist.db), ingested once from an rpc (lpfollow._node_batch: our node when there is one, else blockmachine / the official rpc) and then only the new blocks, so
every replay and backtest reads local rows instead of the node.

What is kept - transformed, not raw logs:
  pools      every pool the PositionManager knows: currencies, hooks, copyable (lpfollow.uncopyable), protocol fee
  tokens     decimals
  swaps      every swap in a COPYABLE pool (45% of the chain's): pool number, block, index, amount0, amount1, sqrtP, active
             liquidity, fee pips - what the replay reads, nothing else (hooked pools, the ETH/USDG pool's
             own swaps are kept too: they are the ETH price)
  nfts       every PositionManager NFT: owner, minted, burned (all pools: the discovery reads it)
  positions  every position in a copyable pool, one row per tokenId: pool, ticks, liquidity minted, open block, mint tx
             (a ladder = the same mint tx), topped up, liquidity left, closed block
  exits      each position's exit txs (decreases, burns): the payout is read from their receipts only when trust needs it
  blocks     block -> timestamp every 1,000 blocks (interpolated between)

Kept `keep_days` (14) days; `prune()` drops older swaps and closed positions. `ingest()` is incremental and resumable
(meta: the last block), fetches in parallel and applies in block order.

  hookbot robinhood lp hist [status]         what the store holds
  hookbot robinhood lp hist ingest           up to the node's head (cron: before the nightly replay)
  hookbot robinhood lp hist backfill <days>  fill the start of the window"""
import bisect, json, os, sqlite3, time
from concurrent.futures import ThreadPoolExecutor

from eth_utils import keccak

PM = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
NATIVE = '0x' + '0' * 40
ETH_POOL = '0x54f7883914619af9105355bf83ed678bcf9f63560218ac61c9963b9503d0ba32'
SWAP = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
MOD = '0x' + keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
INIT = '0x' + keccak(text='Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)').hex()
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
# DELTA LIQUIDITY (deltaliquidity.app, owner 2026-10-01): a Delta ladder's v4 NFTs are minted to and HELD by Delta's ladder
# manager; the LP is named only in the manager's ManagedOpenV4(owner, poolId, tokenIds). Credited to that owner here, so
# discovery, the replay, the payouts (trust) and the pick see the LP, not one contract mixing ~280 wallets (2026-10-01: v9
# held 40,431 v4 NFTs in 10.6 days, 8% of every mint, all named in ManagedOpenV4). Its v3 ladders are not tracked (v4 only)
MANAGERS = ['0x83551a233f6d7b3cb4defbf55abfd887c6811ce4', '0xbcb96b15dc2246d242c879316c86e25e846ad5fb', '0x56bf0f5b26d888b989a9a20ec2164f0ca560836c',
            '0xf321ed71750c3e627a0f435c8950429f231e3497', '0x077af3f17c0ef7bb04a9882e1955e9c23d2f9e91', '0xac6a35d097bb230eb9966ddd8e5970055a622d98',
            '0x5ca6214227d1195c4b7b4b96847b8966c688295d', '0xc5941433114bb47a9733cb31a0a3a3dbff45b418', '0x64680254bf644bbdde394b95129895c13317fed4']
OPEN_V4 = '0x' + keccak(text='ManagedOpenV4(address,bytes32,uint256[])').hex()
CHUNK = 10_000                      # blocks a getLogs (~27k swaps, ~27 MB of JSON through the tunnel)
CONFIRM = 20                        # blocks behind the head: nothing we store is reorged away
KEEP_DAYS = 14.0                     # 98 MB a day measured; 10 -> 14 (2026-10-04, owner): the pick scores 10 days, re-replays reach 14; ~1.4 GB, disk 85% (12 GB free)
BLOCKS_A_DAY = 864_000              # 100 ms blocks

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pools(n INTEGER PRIMARY KEY, pid TEXT UNIQUE, c0 TEXT, c1 TEXT, hooks TEXT, copyable INTEGER, pf0 INTEGER, pf1 INTEGER);
CREATE TABLE IF NOT EXISTS tokens(addr TEXT PRIMARY KEY, dec INTEGER);
CREATE TABLE IF NOT EXISTS swaps(pool INTEGER, blk INTEGER, li INTEGER, a0 REAL, a1 REAL, sp REAL, act REAL, fee INTEGER,
                                 PRIMARY KEY(pool, blk, li)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS nfts(tid INTEGER PRIMARY KEY, owner TEXT, mint_blk INTEGER, burn_blk INTEGER);
CREATE TABLE IF NOT EXISTS managed(tid INTEGER PRIMARY KEY, owner TEXT, manager TEXT, blk INTEGER);
CREATE INDEX IF NOT EXISTS nfts_owner ON nfts(owner);
CREATE TABLE IF NOT EXISTS positions(tid INTEGER PRIMARY KEY, pool INTEGER, tl INTEGER, tu INTEGER, L REAL, open_blk INTEGER,
                                     mint_tx TEXT, topped INTEGER DEFAULT 0, left REAL, close_blk INTEGER, paid0 REAL, paid1 REAL);
CREATE INDEX IF NOT EXISTS positions_open ON positions(open_blk);
CREATE TABLE IF NOT EXISTS exits(tid INTEGER, tx TEXT, blk INTEGER, PRIMARY KEY(tid, tx)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS blocks(blk INTEGER PRIMARY KEY, ts INTEGER);
'''


def path(chain):
    return os.path.join(chain.var, 'lphist.db')


def connect(chain):
    con = sqlite3.connect(path(chain), timeout=120)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript(SCHEMA)
    if 'li' not in [r[1] for r in con.execute('PRAGMA table_info(exits)')]:
        con.execute('ALTER TABLE exits ADD COLUMN li INTEGER')       # the exit's log index in its block (exit_li)
    return con


def _s(h):
    v = int(h, 16)
    return v - (1 << 256) if v >> 255 else v


def _node(calls, timeout=180):
    from copydesk.lpfollow import _node_batch
    return _node_batch(calls, timeout=timeout)


# --------------------------------------------------------------------------- pools and tokens
class Pools:
    """pid -> pool number (and copyable), learned from Initialize logs and, for older pools, the PositionManager's poolKeys."""

    def __init__(self, con):
        self.con = con
        self.n = {pid: (n, cp) for n, pid, cp in con.execute('SELECT n, pid, copyable FROM pools')}

    def add(self, pid, c0, c1, hooks):
        from copydesk.lpfollow import uncopyable
        cp = 1 if (not uncopyable(c0, c1, hooks, pid, USDG) or pid == ETH_POOL) else 0
        cur = self.con.execute('INSERT OR IGNORE INTO pools(pid, c0, c1, hooks, copyable) VALUES (?,?,?,?,?)', (pid, c0, c1, hooks, cp))
        n = cur.lastrowid if cur.rowcount else self.con.execute('SELECT n FROM pools WHERE pid = ?', (pid,)).fetchone()[0]
        self.n[pid] = (n, cp)
        return self.n[pid]

    def learn(self, pids):
        """Keys of pools not seen yet (a swap or a mod in a pool created before the window), from poolKeys."""
        from copydesk.lpcopy import keys
        new = [p for p in pids if p not in self.n]
        for i in range(0, len(new), 200):
            ks = keys(new[i:i + 200])
            for p in new[i:i + 200]:
                k = ks.get(p)
                if k and int(k[1], 16):
                    self.add(p, k[0], k[1], k[2])
                else:
                    self.add(p, NATIVE, NATIVE, NATIVE)            # not a PositionManager pool: never copyable


def decimals(con, tokens):
    have = {a for (a,) in con.execute('SELECT addr FROM tokens')}
    want = [t for t in tokens if t not in have and int(t, 16)]
    from copydesk.lpfollow import _call
    for i in range(0, len(want), 100):
        r = _node([_call(t, '0x313ce567') for t in want[i:i + 100]])
        con.executemany('INSERT OR REPLACE INTO tokens(addr, dec) VALUES (?,?)',
                        [(t, int(x, 16) if x and x != '0x' and len(x) <= 66 else 18) for t, x in zip(want[i:i + 100], r)])


# --------------------------------------------------------------------------- ingest
def _logs(flt, lo, hi):
    """One eth_getLogs, alone (batched, the four queries of a range pass the node's response cap): retried with backoff when
    the tunnel drops a response mid-read (2026-09-30: a ~27 MB answer died after 80 chunks), halved until it fits."""
    from copydesk import lpfollow as F
    r, err = None, None
    # the node while it answers, else its public fallbacks (lpfollow.NODE_FALLBACK): a refused connection moves on at once
    urls = ([None] if F.NODE and time.time() >= F._node['down_until'] else []) + list(F.NODE_FALLBACK)
    body = json.dumps(dict(jsonrpc='2.0', id=0, method='eth_getLogs', params=[dict(flt, fromBlock=hex(lo), toBlock=hex(hi))]))
    for url in urls:
        for attempt in range(6):
            try:
                r = F._post(body, 300, url)
                if '429' in str(r.get('error') or '') or 'Too Many' in str(r.get('error') or ''):
                    err, r = r.get('error'), None                       # rate limited: the same window again, later
                    time.sleep(min(30, 2 ** attempt))
                    continue
                break
            except (ConnectionRefusedError, OSError) as e:
                err = e
                if url is None and isinstance(e, ConnectionRefusedError):
                    F._node['down_until'] = time.time() + 60
                    break
                time.sleep(2 ** attempt)
            except Exception as e:
                err = e
                time.sleep(2 ** attempt)
        if r is not None and 'result' in r:
            break
    if r is None:
        if hi - lo > 100:
            m = (lo + hi) // 2
            return _logs(flt, lo, m) + _logs(flt, m + 1, hi)
        raise RuntimeError('node: %s' % str(err)[:160])
    if 'result' in r:
        return r['result']
    if hi > lo:
        m = (lo + hi) // 2
        return _logs(flt, lo, m) + _logs(flt, m + 1, hi)
    raise RuntimeError('node: %s' % str(r.get('error'))[:160])


def _fetch(lo, hi):
    """One block range: every log the store needs, as raw log dicts (the node does the filtering by topic)."""
    from copydesk.lpfollow import POSM
    sw = _logs(dict(address=PM, topics=[SWAP]), lo, hi)
    md = _logs(dict(address=PM, topics=[MOD, None, '0x' + '0' * 24 + POSM[2:]]), lo, hi)
    ini = _logs(dict(address=PM, topics=[INIT]), lo, hi)
    tr = _logs(dict(address=POSM, topics=[TRANSFER]), lo, hi)
    mg = _logs(dict(address=MANAGERS, topics=[OPEN_V4]), lo, hi)
    hdr = _node([('eth_getBlockByNumber', [hex(lo), False])])[0]
    return lo, hi, sw, md, ini, tr, int(hdr['timestamp'], 16) if hdr else None, mg


def managed(l):
    """A ManagedOpenV4 log -> (owner, manager, block, [tokenIds])."""
    d = l['data'][2:]
    w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
    return '0x' + l['topics'][1][-40:], l['address'].lower(), int(l['blockNumber'], 16), w[2:2 + w[1]]


def attribute(con, logs):
    """Credit the NFTs named in ManagedOpenV4 logs to their owner (only while the manager still holds them). Returns
    (NFTs re-credited, owners)."""
    n, owners = 0, set()
    for l in logs:
        owner, mgr, blk, tids = managed(l)
        owners.add(owner)
        for t in tids:
            con.execute('INSERT OR REPLACE INTO managed(tid, owner, manager, blk) VALUES (?,?,?,?)', (t, owner, mgr, blk))
            n += con.execute('UPDATE nfts SET owner = ? WHERE tid = ? AND owner = ?', (owner, t, mgr)).rowcount
    return n, owners


def _apply(con, pools, chunk):
    lo, hi, sw, md, ini, tr, ts, *more = chunk                       # more: the Delta ManagedOpenV4 logs (older chunks: none)
    if ts:
        con.execute('INSERT OR REPLACE INTO blocks(blk, ts) VALUES (?,?)', (lo, ts))
    for l in ini:                                                    # new pools first
        d = l['data'][2:]
        pools.add(l['topics'][1], '0x' + l['topics'][2][-40:], '0x' + l['topics'][3][-40:], '0x' + d[128 + 24:192])
    pools.learn(sorted({l['topics'][1] for l in sw} | {l['topics'][1] for l in md}))
    rows = []
    for l in sw:
        n, cp = pools.n[l['topics'][1]]
        if not cp:
            continue
        d = l['data'][2:]
        rows.append((n, int(l['blockNumber'], 16), int(l['logIndex'], 16), float(_s(d[0:64])), float(_s(d[64:128])),
                     int(d[128:192], 16) / 2 ** 96, float(int(d[192:256], 16)), int(d[320:384], 16)))
    con.executemany('INSERT OR IGNORE INTO swaps VALUES (?,?,?,?,?,?,?,?)', rows)
    # NFTs and positions in block order: a mint's Transfer and its ModifyLiquidity share a tx
    ev = sorted([(int(l['blockNumber'], 16), int(l['logIndex'], 16), 't', l) for l in tr] +
                [(int(l['blockNumber'], 16), int(l['logIndex'], 16), 'm', l) for l in md])
    for blk, li, kind, l in ev:
        if kind == 't':
            frm, to, tid = int(l['topics'][1], 16), '0x' + l['topics'][2][-40:], int(l['topics'][3], 16)
            if frm == 0:
                con.execute('INSERT OR IGNORE INTO nfts(tid, owner, mint_blk) VALUES (?,?,?)', (tid, to, blk))
            elif int(to, 16) == 0:
                con.execute('UPDATE nfts SET burn_blk = ? WHERE tid = ?', (blk, tid))
                con.execute('UPDATE positions SET close_blk = COALESCE(close_blk, ?) WHERE tid = ?', (blk, tid))
                con.execute('INSERT OR IGNORE INTO exits(tid, tx, blk, li) VALUES (?,?,?,?)', (tid, l['transactionHash'], blk, li))
            else:
                con.execute('UPDATE nfts SET owner = ? WHERE tid = ?', (to, tid))
            continue
        n, cp = pools.n[l['topics'][1]]
        d = l['data'][2:]
        tl, tu, dl, tid = _s(d[0:64]), _s(d[64:128]), _s(d[128:192]), int(d[192:256], 16)
        if not cp:
            continue
        cur = con.execute('SELECT L, left FROM positions WHERE tid = ?', (tid,)).fetchone()
        if cur is None:
            if dl > 0:
                con.execute('INSERT INTO positions(tid, pool, tl, tu, L, open_blk, mint_tx, left) VALUES (?,?,?,?,?,?,?,?)',
                            (tid, n, tl, tu, float(dl), blk, l['transactionHash'], float(dl)))
            continue
        left = cur[1] + dl
        if dl > 0:
            con.execute('UPDATE positions SET topped = 1, left = ? WHERE tid = ?', (left, tid))
        else:
            con.execute('INSERT OR IGNORE INTO exits(tid, tx, blk, li) VALUES (?,?,?,?)', (tid, l['transactionHash'], blk, li))
            con.execute('UPDATE positions SET left = ?, close_blk = CASE WHEN ? <= 0 THEN COALESCE(close_blk, ?) ELSE close_blk END WHERE tid = ?',
                        (left, left, blk, tid))
    if more:                                                         # a Delta ladder's NFTs to its LP (same chunk: same tx as the mint)
        attribute(con, more[0])


# payouts of NFTs closed together with others, digested before the split: read again
SHARED_RESET = ('UPDATE positions SET paid0 = NULL, paid1 = NULL WHERE paid0 IS NOT NULL AND tid IN '
                '(SELECT tid FROM exits WHERE tx IN (SELECT tx FROM exits GROUP BY tx HAVING count(*) > 1))')


def delta_backfill(chain, go=False, log=print):
    """`lp hist delta [--go]`: credit the Delta ladders already in the store (before the ingest knew them) to their LPs, then
    (go) recount discovery's mint counts for them and the managers, and drop the managers' mixed-up 'wallet' from the replay
    store. Dry by default: the same writes inside a transaction, measured, then rolled back."""
    con = connect(chain)
    first = int(con.execute("SELECT v FROM meta WHERE k = 'first'").fetchone()[0])
    last = int(con.execute("SELECT v FROM meta WHERE k = 'last'").fetchone()[0])
    # open ladders are never pruned: their NFTs can be older than the store's 'first' (2026-10-01: 229 of them)
    q = ','.join('?' * len(MANAGERS))
    first = min(first, con.execute('SELECT min(mint_blk) FROM nfts WHERE owner IN (%s)' % q, MANAGERS).fetchone()[0] or first)
    logs = []
    for lo in range(first, last + 1, CHUNK * 20):
        logs += _logs(dict(address=MANAGERS, topics=[OPEN_V4]), lo, min(last, lo + CHUNK * 20 - 1))
    before = con.execute('SELECT count(*) FROM nfts WHERE owner IN (%s)' % q, MANAGERS).fetchone()[0]
    n, owners = attribute(con, logs)
    # payouts digested while the manager was the owner measured ITS net flow (it forwards everything): read again for the LP
    con.execute('UPDATE positions SET paid0 = NULL, paid1 = NULL WHERE tid IN (SELECT tid FROM managed)')
    con.execute(SHARED_RESET)
    after = con.execute('SELECT count(*) FROM nfts WHERE owner IN (%s)' % q, MANAGERS).fetchone()[0]
    pos = con.execute('SELECT count(*), count(DISTINCT n.owner) FROM positions p JOIN nfts n ON n.tid = p.tid JOIN managed m ON m.tid = p.tid '
                      'WHERE p.close_blk IS NOT NULL').fetchone()
    out = dict(events=len(logs), owners=len(owners), recredited=n, manager_nfts_before=before, manager_nfts_after=after,
               closed_positions=pos[0], with_closed=pos[1])
    log('  delta: %d ManagedOpenV4 events, %d LPs; %d NFTs re-credited; managers held %d -> %d; %d closed positions in copyable pools by %d LPs%s' % (
        len(logs), len(owners), n, before, after, pos[0], pos[1], '' if go else '  (DRY: rolled back)'))
    if not go:
        con.rollback()
        con.close()
        return out
    con.commit()
    con.close()
    # DISCOVERY: the mint counts of these LPs and of the managers, recounted from the store for the days it covers
    import time as _t
    from copydesk import lpdb
    R = Reader(chain)
    who = sorted(owners | set(MANAGERS))
    day = lambda b: _t.strftime('%Y-%m-%d', _t.gmtime(R.tsof(b)))
    d0 = day(first)
    per = {}
    for o, b in R.con.execute('SELECT owner, mint_blk FROM nfts WHERE mint_blk >= ? AND owner IN (%s)' % ','.join('?' * len(who)), [first] + who):
        per[(o, day(b))] = per.get((o, day(b)), 0) + 1
    db = lpdb.connect()
    lpdb.begin(db)
    db.execute('DELETE FROM mint_counts WHERE day >= ? AND owner IN (%s)' % ','.join('?' * len(who)), [d0] + who)
    db.executemany('INSERT OR REPLACE INTO mint_counts(owner, day, n) VALUES (?,?,?)', [(o, d, k) for (o, d), k in per.items()])
    for t in ('copy_trades', 'replays', 'watch'):                    # the managers were scored as one wallet of ~280 LPs
        db.execute('DELETE FROM %s WHERE owner IN (%s)' % (t, q), MANAGERS)
    db.execute("DELETE FROM kv WHERE k LIKE 'replay:%%' AND substr(k, -42) IN (%s)" % q, MANAGERS)
    db.commit()
    db.close()
    log('  delta: mint counts recounted for %d wallets from %s; the managers dropped from the replay store' % (len(who), d0))
    return out


def ingest(chain, frm=None, to=None, workers=4, log=print, only=None):
    """Fetch [frm, to] (default: from the last stored block to head - CONFIRM) in CHUNK ranges, `workers` in flight, and
    apply them in block order. Resumable: the last applied chunk is recorded after each."""
    con = connect(chain)
    last = con.execute("SELECT v FROM meta WHERE k = 'last'").fetchone()
    head = int(_node([('eth_blockNumber', [])])[0], 16) - CONFIRM
    frm = frm if frm is not None else (int(last[0]) + 1 if last else head - BLOCKS_A_DAY)
    to = min(to or head, head)
    if frm > to:
        return 0
    pools = Pools(con)
    ranges = [(a, min(to, a + CHUNK - 1)) for a in range(frm, to + 1, CHUNK) if only is None or a in only]
    t, n = time.time(), 0
    with ThreadPoolExecutor(workers) as ex:
        for chunk in ex.map(lambda r: _fetch(*r), ranges):
            _apply(con, pools, chunk)
            if last is None or chunk[1] > int(last[0]):
                con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('last', ?)", (str(chunk[1]),))
            first = con.execute("SELECT v FROM meta WHERE k = 'first'").fetchone()
            if not first or chunk[0] < int(first[0]):
                con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('first', ?)", (str(chunk[0]),))
            con.commit()
            n += 1
            if n % 20 == 0 or n == len(ranges):
                log('  hist: %d/%d chunks, block %d, %.0f s' % (n, len(ranges), chunk[1], time.time() - t))
    decimals(con, sorted({t for c0, c1 in con.execute('SELECT c0, c1 FROM pools WHERE copyable = 1') for t in (c0, c1)}))
    protocol_fees(con)
    con.commit()
    con.close()
    return n


def protocol_fees(con):
    from copydesk.lpfollow import _batch, _call, _w, SV
    want = [(n, pid) for n, pid in con.execute('SELECT n, pid FROM pools WHERE copyable = 1 AND pf0 IS NULL')]
    for i in range(0, len(want), 100):
        r = _batch([_call(SV, '0xc815641c' + pid[2:]) for _, pid in want[i:i + 100]])
        con.executemany('UPDATE pools SET pf0 = ?, pf1 = ? WHERE n = ?', [(_w(x, 2) & 0xfff, _w(x, 2) >> 12, n) for (n, _), x in zip(want[i:i + 100], r)])


def backfill(chain, days, workers=4, log=print):
    """Fill every missing chunk of the last `days` up to the start of the unbroken stretch - RESUMABLE: chunks are aligned
    to CHUNK boundaries and one whose start block is already sampled (applied) is skipped, so a restart after a failure
    fetches only what is missing."""
    con = connect(chain)
    have = {b for (b,) in con.execute('SELECT blk FROM blocks')}
    con.close()
    head = int(_node([('eth_blockNumber', [])])[0], 16) - CONFIRM
    last = Reader(chain).span()[0] - 1 if have else head
    lo = (head - int(days * BLOCKS_A_DAY)) // CHUNK * CHUNK
    todo = [a for a in range(lo, last + 1, CHUNK) if a not in have]
    log('backfill: %d chunks missing between %d and %d' % (len(todo), lo, last))
    n = 0
    for i in range(0, len(todo), 40):                                # in batches: each is applied and committed in order
        part = todo[i:i + 40]
        n += ingest(chain, frm=part[0], to=min(part[-1] + CHUNK - 1, last), workers=workers, log=log, only=set(part))
    return n


def prune(chain, keep_days=KEEP_DAYS):
    con = connect(chain)
    head = int(con.execute("SELECT v FROM meta WHERE k = 'last'").fetchone()[0])
    cut = head - int(keep_days * BLOCKS_A_DAY)
    n = con.execute('DELETE FROM swaps WHERE blk < ?', (cut,)).rowcount
    m = con.execute('DELETE FROM positions WHERE close_blk IS NOT NULL AND close_blk < ?', (cut,)).rowcount
    con.execute('DELETE FROM exits WHERE blk < ?', (cut,))
    con.execute('DELETE FROM nfts WHERE burn_blk IS NOT NULL AND burn_blk < ?', (cut,))
    con.execute('DELETE FROM managed WHERE blk < ? AND tid NOT IN (SELECT tid FROM nfts)', (cut,))
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('first', ?)", (str(max(cut, int(con.execute("SELECT v FROM meta WHERE k = 'first'").fetchone()[0]))),))
    con.commit()
    con.close()
    return n, m


def status(chain):
    con = connect(chain)
    g = lambda k: (lambda r: int(r[0]) if r else None)(con.execute('SELECT v FROM meta WHERE k = ?', (k,)).fetchone())
    out = dict(first=g('first'), last=g('last'), pools=con.execute('SELECT COUNT(*), SUM(copyable) FROM pools').fetchone(),
               swaps=con.execute('SELECT COUNT(*) FROM swaps').fetchone()[0], positions=con.execute('SELECT COUNT(*) FROM positions').fetchone()[0],
               nfts=con.execute('SELECT COUNT(*) FROM nfts').fetchone()[0], mb=os.path.getsize(path(chain)) / 1e6)
    con.close()
    return out


# --------------------------------------------------------------------------- digest: exit payouts (trust)
def payouts(chain, tids):
    """Fill positions.paid0 / paid1 for these tokenIds (once: a closed position's payout never changes): the owner's net
    flow of the pool's two currencies over the position's exit txs, from their receipts. Left NULL (does not vote on
    trust) when it cannot be read cleanly: an exit tx that also mints (a re-range), native ETH (no Transfer log), a net
    outflow (liquidity added in the same tx)."""
    con = connect(chain)
    rows = con.execute('SELECT p.tid, p.mint_tx, pl.c0, pl.c1, n.owner, m.manager FROM positions p JOIN pools pl ON pl.n = p.pool JOIN nfts n ON n.tid = p.tid '
                       'LEFT JOIN managed m ON m.tid = p.tid '
                       'WHERE p.close_blk IS NOT NULL AND p.paid0 IS NULL AND p.tid IN (%s)' % ','.join('?' * len(tids)), list(tids)).fetchall() if tids else []
    ex = {}
    for tid, tx in con.execute('SELECT tid, tx FROM exits WHERE tid IN (%s)' % ','.join('?' * len(tids)), list(tids)) if tids else []:
        ex.setdefault(tid, set()).add(tx)
    mints = {r[1] for r in rows}
    want = sorted({tx for r in rows for tx in ex.get(r[0], ())} - mints)
    rc = {}

    def fetch(txs):
        """Receipts of txs; a batch over the node's response cap is split in halves down to single receipts (2026-10-01:
        0x8bb2's exits burn many positions in one tx and every 100-receipt batch came back 'response too large')."""
        try:
            return list(zip(txs, _node([('eth_getTransactionReceipt', [t]) for t in txs])))
        except RuntimeError as e:
            if 'too large' not in str(e) and 'response too large' not in str(e):
                raise
            if len(txs) == 1:
                return [(txs[0], None)]                                # one receipt over the cap: unread (no trust vote)
            h = len(txs) // 2
            return fetch(txs[:h]) + fetch(txs[h:])
    for i in range(0, len(want), 100):
        for tx, r in fetch(want[i:i + 100]):
            rc[tx] = (r or {}).get('logs') or []
    # A TX THAT CLOSES SEVERAL NFTS (a ladder's bins) pays them all at once: each gets its share (an equal split), so the bins of
    # a ladder add up to the payout once (2026-10-01: every bin was given the whole tx's flow and the replay summed them - a
    # 12-bin ladder's payout counted 12 times, the trust of every laddering wallet understated)
    k = dict(con.execute('SELECT tx, count(*) FROM exits WHERE tx IN (%s) GROUP BY tx' % ','.join('?' * len(want)), want)) if want else {}
    upd = []
    # A MANAGED NFT (a Delta ladder bin) pays the MANAGER, which keeps a cut (often 7.5% of a side, sometimes the whole token
    # side) before forwarding the rest to its LP (2026-10-01: 0x620a's trust read 0.44 on that skim). The replay models the
    # position, and our copy pays no manager: the payout is what the PoolManager sent the manager, gross
    pm = '0x' + '0' * 24 + PM[2:]
    for tid, mtx, c0, c1, owner, mgr in rows:
        txs = ex.get(tid, set())
        if not txs or txs & mints or NATIVE in (c0, c1):
            continue
        me = '0x' + '0' * 24 + (mgr or owner)[2:]
        net = {c0: 0, c1: 0}
        for tx in txs:
            for l in rc.get(tx, []):
                a = l['address'].lower()
                if a in net and l['topics'][0] == TRANSFER and len(l['topics']) == 3:
                    v = int(l['data'], 16) if l['data'] not in ('0x', '') else 0
                    t_, f_ = l['topics'][2].lower(), l['topics'][1].lower()
                    if mgr:
                        v = v if t_ == me and f_ == pm else 0
                    else:
                        v = v if t_ == me else -v if f_ == me else 0
                    net[a] += v / max(1, k.get(tx, 1))
        if net[c0] >= 0 and net[c1] >= 0:
            upd.append((float(net[c0]), float(net[c1]), tid))
    con.executemany('UPDATE positions SET paid0 = ?, paid1 = ? WHERE tid = ?', upd)
    con.commit()
    con.close()
    return len(upd)


def exit_li(chain, want):
    """WHERE IN ITS BLOCK A POSITION CLOSED (2026-10-01: bench 0x4b2d's $3,000 position was valued at a swap that came AFTER its
    burn in the same block - model -$2,986, its exit really paid +$6.63 - and its trust read 0.36). want: {key: ([tokenIds],
    close block)} -> {key: the last exit log index of those NFTs in that block}. Exits ingested before the li column are
    read once from their receipts (the PoolManager's ModifyLiquidity whose salt is the NFT) and stored."""
    if not want:
        return {}
    con = connect(chain)
    rows = {}
    for k, (tids, blk) in want.items():
        for tid, tx, li in con.execute('SELECT tid, tx, li FROM exits WHERE blk = ? AND tid IN (%s)' % ','.join('?' * len(tids)), [blk] + list(tids)):
            rows.setdefault(k, []).append((tid, tx, li))
    from copydesk.lpfollow import POSM
    miss = sorted({tx for v in rows.values() for tid, tx, li in v if li is None})
    found = {}
    for i in range(0, len(miss), 50):
        for tx, rc in zip(miss[i:i + 50], _node([('eth_getTransactionReceipt', [t]) for t in miss[i:i + 50]])):
            for l in (rc or {}).get('logs') or []:
                # only the PositionManager's own modifications: there the salt IS the tokenId (another router's salt is
                # any 32 bytes - 2026-10-01 it overflowed sqlite)
                if l['address'].lower() == PM and l['topics'][0] == MOD and len(l['data']) >= 2 + 256 \
                        and len(l['topics']) > 2 and l['topics'][2][-40:].lower() == POSM[2:].lower():
                    tid = int(l['data'][2 + 192:2 + 256], 16)
                    found[(tid, tx)] = max(found.get((tid, tx), -1), int(l['logIndex'], 16))
    con.executemany('UPDATE exits SET li = ? WHERE tid = ? AND tx = ?', [(li, tid, tx) for (tid, tx), li in found.items()])
    con.commit()
    con.close()
    out = {}
    for k, v in rows.items():
        lis = [li if li is not None else found.get((tid, tx)) for tid, tx, li in v]
        if lis and all(x is not None for x in lis):
            out[k] = max(lis)
    return out


# --------------------------------------------------------------------------- reads for the replay (lpcopy.run_store)
def heat(chain, log=print):
    """THE POOL HEAT TABLE (owner 2026-09-30, research/lpfeat 'volz'): every copyable pool that traded in the last 72 block-hours
    -> the last completed hour's $ volume as a z-score of its 72 hourly volumes. Written to var/lp_heat.json after every
    ingest (5 min); the mirror pushes var/ to the desk's box every 30 s, where Follow.cold_pool reads it. Pool volume in $
    is its quote side (USDG, or ETH at the store's price), both directions, as the study measured it."""
    import json, math, statistics as S_
    R = Reader(chain)
    first, last = R.span()
    H = 36000                                                          # blocks an hour (100 ms)
    top = last - last % H
    lo = top - 72 * H
    from copydesk.lpwallet import WETH
    usdg, weth, _N = USDG, WETH, NATIVE
    eusd = R.eusd(last)
    unit = {}
    for n, (pid, c0, c1, h, *_x) in R.pid.items():
        for side, t in ((0, c0), (1, c1)):
            if t == usdg:
                unit[n] = (pid, side, 1e-6)
            elif t in (weth, _N) and n not in unit:
                unit[n] = (pid, side, eusd / 1e18)
    vol = {}
    for n, hh, s0, s1 in R.con.execute('SELECT pool, (blk - ?) / ?, SUM(ABS(a0)), SUM(ABS(a1)) FROM swaps WHERE blk >= ? AND blk < ? '
                                       'GROUP BY pool, (blk - ?) / ?', (lo, H, lo, top, lo, H)):
        if n in unit:
            pid, side, k = unit[n]
            vol.setdefault(n, [0.0] * 72)[int(hh)] = (s0 if side == 0 else s1) * k
    out = {}
    for n, v in vol.items():
        sd = S_.pstdev(v)
        if sd > 0:
            out[unit[n][0]] = round((v[-1] - S_.mean(v)) / sd, 3)
    # PRICE-FLAT (research/lpfactor: volatility is the other canonical factor): the std of the pool's hourly log price moves
    # over the last 24 of its hours, the price carried forward through quiet hours from 73 hours back, as research/lpfeat
    px = {}
    for n, hh, b_, sp in R.con.execute('SELECT pool, (blk - ?) / ?, MAX(blk), sp FROM swaps WHERE blk >= ? AND blk < ? GROUP BY pool, (blk - ?) / ?',
                                       (lo - H, H, lo - H, top, lo - H, H)):
        if n in unit and sp:
            px.setdefault(n, {})[int(hh)] = 2 * math.log(sp)
    rv = {}
    for n, hp in px.items():
        seq, last_ = [], None
        for hh in range(73):
            last_ = hp.get(hh, last_)
            seq.append(last_)
        ret = [b - a for a, b in zip(seq, seq[1:]) if a is not None and b is not None][-24:]
        if len(ret) > 3:
            rv[unit[n][0]] = round(S_.pstdev(ret), 5)
    # EXIT ROUTES (smart exit routing): every token with 2+ hookless USDG / native-ETH pools that traded in 72 h -> those pool ids
    toks = {}
    for n in vol:
        pid, c0, c1, h = R.pid[n][:4]
        if int(h or '0x0', 16):
            continue
        t = c1 if c0 in (usdg, _N) else c0 if c1 in (usdg, _N) else None
        if t and (c0 in (usdg, _N) or c1 in (usdg, _N)):
            toks.setdefault(t, []).append(pid)
    routes = {t: ps for t, ps in toks.items() if len(ps) > 1}
    p = os.path.join(chain.var, 'lp_heat.json')
    json.dump(dict(ts=time.time(), block=last, hour_end=top, pools=out, rv24=rv, routes=routes), open(p + '.tmp', 'w'))
    os.replace(p + '.tmp', p)
    log('  heat: %d pools (volz), %d (rv24), %d tokens with exit routes, last hour ending at block %d' % (len(out), len(rv), len(routes), top))
    return out


class Reader:
    """What lpcopy's replay needs, from the store: a wallet's positions, pool keys, swaps per pool, decimals, block times
    and the ETH price series."""

    def __init__(self, chain):
        self.con = connect(chain)
        self.pools()
        self.dec = dict(self.con.execute('SELECT addr, dec FROM tokens'))
        self.bt = self.con.execute('SELECT blk, ts FROM blocks ORDER BY blk').fetchall()
        self._eth = None

    def pools(self):
        """The pool map, re-read when a position names a pool the ingest added after this reader started."""
        self.pid = {n: (pid, c0, c1, h, pf0, pf1) for n, pid, c0, c1, h, pf0, pf1 in self.con.execute('SELECT n, pid, c0, c1, hooks, pf0, pf1 FROM pools')}

    def span(self):
        """(first, last) of the UNBROKEN stretch ending at the last stored block: each ingested chunk leaves a block sample
        at its start, so a gap wider than a chunk (a backfill still filling in, a failed range) ends the stretch."""
        last = int(self.con.execute("SELECT v FROM meta WHERE k = 'last'").fetchone()[0])
        first = last
        for (b,) in self.con.execute('SELECT blk FROM blocks WHERE blk <= ? ORDER BY blk DESC', (last,)):
            if first - b > CHUNK:
                break
            first = b
        return first, last

    def tsof(self, b):
        """Block -> unix time, interpolated between the stored samples (100 ms a block past the last one)."""
        bt = self.bt
        i = bisect.bisect_right(bt, (b, 1 << 62))
        if i == 0:
            return bt[0][1] - (bt[0][0] - b) / 10.0
        if i == len(bt):
            return bt[-1][1] + (b - bt[-1][0]) / 10.0
        (b0, t0), (b1, t1) = bt[i - 1], bt[i]
        return t0 + (t1 - t0) * (b - b0) / (b1 - b0)

    def eusd(self, b):
        """USDG per ETH at block b, from the stored ETH/USDG swaps."""
        if self._eth is None:
            n = next((k for k, v in self.pid.items() if v[0] == ETH_POOL), None)
            self._eth = [(blk, sp * sp * 1e12) for blk, sp in self.con.execute('SELECT blk, sp FROM swaps WHERE pool = ? ORDER BY blk, li', (n,))] if n else []
        e = self._eth
        i = bisect.bisect_right(e, (b, float('inf')))
        return e[max(0, i - 1)][1] if e else 2500.0

    def positions(self, owner, frm, open_too=False):
        """The wallet's closed positions in copyable pools opened from block `frm`, grouped as lpwallet does: the NFTs one
        tx minted are one position (a ladder). [(tokenIds, legs [(pid, tl, tu, L)], open, close, topped, paid per leg)]
        open_too (research/lpml): positions not fully closed yet come too, close None - a label that only saw closed
        positions would drop exactly the long holds still running at the end of the data."""
        rows = self.con.execute(
            'SELECT p.tid, p.pool, p.tl, p.tu, p.L, p.open_blk, p.mint_tx, p.topped, p.close_blk, p.paid0, p.paid1, m.manager FROM positions p '
            'JOIN nfts n ON n.tid = p.tid LEFT JOIN managed m ON m.tid = p.tid WHERE n.owner = ? AND p.open_blk >= ?', (owner.lower(), frm)).fetchall()
        by = {}
        if any(r[1] not in self.pid for r in rows):
            self.pools()
        rows = [r for r in rows if r[1] in self.pid]
        for tid, pool, tl, tu, L, ob, tx, topped, cb, p0, p1, mg in rows:
            by.setdefault(tx, []).append((tid, self.pid[pool][0], tl, tu, L, ob, topped, cb, p0, p1, mg))
        out = []
        for tx, g in by.items():
            if any(x[7] is None for x in g) and not open_too:
                continue                                                 # not every NFT of it closed yet
            out.append(dict(tokenIds=[x[0] for x in g], legs=[(x[1], x[2], x[3], x[4]) for x in g], open=min(x[5] for x in g),
                            close=None if any(x[7] is None for x in g) else max(x[7] for x in g), topped=any(x[6] for x in g),
                            paid=[(x[8], x[9]) for x in g], managed=all(x[10] for x in g)))
        return sorted(out, key=lambda p: p['open'])

    def keys(self, pids):
        m = {v[0]: v for v in self.pid.values()}
        return {p: (m[p][1], m[p][2], m[p][3]) for p in pids if p in m}

    def pf(self):
        return {v[0]: (v[4] or 0, v[5] or 0) for v in self.pid.values()}

    def swaps(self, want, pad=300):
        """pid -> sorted [(blk, li, a0, a1, sp, act, fee)] inside the windows [(pid, lo, hi)] (+ `pad` blocks before each
        for the entry price, further back if the pool had no swap that close)."""
        n_of = {v[0]: k for k, v in self.pid.items()}
        out = {}
        for pid, lo, hi in want:
            n = n_of.get(pid)
            if n is None:
                continue
            rows = self.con.execute('SELECT blk, li, a0, a1, sp, act, fee FROM swaps WHERE pool = ? AND blk BETWEEN ? AND ? ORDER BY blk, li',
                                    (n, lo - pad, hi)).fetchall()
            if not rows or rows[0][0] > lo:
                prev = self.con.execute('SELECT blk, li, a0, a1, sp, act, fee FROM swaps WHERE pool = ? AND blk < ? ORDER BY blk DESC, li DESC LIMIT 1',
                                        (n, lo - pad)).fetchone()
                rows = ([prev] if prev else []) + rows
            cur = out.setdefault(pid, {})
            for r in rows:
                cur[(r[0], r[1])] = r
        return {k: sorted(v.values()) for k, v in out.items()}


def _selftest():
    import tempfile
    class Ch:
        var = tempfile.mkdtemp()
    con = connect(Ch)
    con.execute("INSERT INTO pools(pid, c0, c1, hooks, copyable) VALUES ('0xp', ?, '0xt', ?, 1)", (USDG, NATIVE))
    n = con.execute('SELECT n FROM pools').fetchone()[0]
    pools = Pools(con)
    mint = lambda tid, blk, dl, tx: dict(topics=['', '0xp', ''], data='0x' + '%064x' % (-100 % (1 << 256)) + '%064x' % 100 + '%064x' % (dl % (1 << 256)) + '%064x' % tid,
                                         blockNumber=hex(blk), logIndex='0x1', transactionHash=tx)
    tr = lambda frm, to, tid, blk, tx: dict(topics=[TRANSFER, '0x%064x' % frm, '0x%064x' % to, '0x%064x' % tid], blockNumber=hex(blk), logIndex='0x0',
                                            transactionHash=tx, data='0x')
    _apply(con, pools, (1, 9, [], [mint(7, 2, 500, '0xa'), mint(7, 5, 100, '0xb'), mint(7, 8, -600, '0xc')], [],
                        [tr(0, 0xbeef, 7, 2, '0xa')], 1000))
    r = con.execute('SELECT L, topped, left, close_blk FROM positions WHERE tid = 7').fetchone()
    assert r == (500.0, 1, 0.0, 8), r
    assert con.execute('SELECT owner FROM nfts WHERE tid = 7').fetchone()[0] == '0x' + '%040x' % 0xbeef
    assert con.execute('SELECT COUNT(*) FROM exits WHERE tid = 7').fetchone()[0] == 1
    # DELTA: a ladder's NFTs minted to the manager go to the LP named in ManagedOpenV4; an NFT the manager no longer holds is
    # left alone; the chunk applies the attribution itself (8th field)
    mgr, lp = MANAGERS[0], '0x' + 'ab' * 20
    for t in (21, 22, 23):
        con.execute('INSERT INTO nfts(tid, owner, mint_blk) VALUES (?,?,?)', (t, mgr if t < 23 else '0x' + 'cd' * 20, 5))
    log = dict(address=mgr, topics=[OPEN_V4, '0x' + '0' * 24 + lp[2:], '0x' + '11' * 32], blockNumber=hex(5),
               data='0x' + ''.join('%064x' % x for x in (32, 3, 21, 22, 23)))
    n, owners = attribute(con, [log])
    assert n == 2 and owners == {lp}, (n, owners)
    assert [r[0] for r in con.execute('SELECT owner FROM nfts WHERE tid IN (21, 22, 23) ORDER BY tid')] == [lp, lp, '0x' + 'cd' * 20]
    con.execute('INSERT INTO nfts(tid, owner, mint_blk) VALUES (31, ?, 6)', (mgr,))
    _apply(con, pools, (10, 19, [], [], [], [], None, [dict(log, data='0x' + ''.join('%064x' % x for x in (32, 1, 31)))]))
    assert con.execute('SELECT owner FROM nfts WHERE tid = 31').fetchone()[0] == lp
    return 6


if __name__ == '__main__':
    print('lphist selftest: %d asserts' % _selftest())
