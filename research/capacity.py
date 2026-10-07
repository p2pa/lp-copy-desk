"""DOES THE EDGE HAVE A CAPACITY? (owner 2026-10-02, fifth response on the Model Wallet Study page: "bucket closed positions by
the position's share of in-range liquidity at mint, and show per-copy return by bucket. If return falls with share, the edge
has a capacity.")

Two populations, both REAL outcomes (no simulator):
  leaders  every closed position of every watched wallet (lpdb.positions: invested_usd, realized_pct), its liquidity
           (lpdb.legs) against the pool's active liquidity at the last swap before the mint (lphist.swaps.act)
  ours     every closed live copy (var/lp/follow.jsonl): pnl_pct by size_usd, and against its leader's own pnl_pct
share = L / (L + active), the fraction of the in-range book the position would own at the current tick (an out-of-range bid
owns nothing yet; its share is reported the same way, as size against the pool's depth). Buckets are share deciles.
`python -m research.capacity`."""
import bisect, collections, json, math, os, sqlite3, statistics as S, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from copydesk import lpdb                                                   # noqa: E402

HIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chains/robinhood/var/lphist.db')
FOLLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chains/robinhood/var/lp/follow.jsonl')
EDGES = [0, 1e-4, 1e-3, 0.01, 0.03, 0.1, 0.3, 1.01]                          # share buckets


def ci(xs):
    m = S.mean(xs)
    se = S.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float('nan')
    return m, se, S.median(xs)


def leaders(log=print):
    h = sqlite3.connect(HIST)
    pn = dict(h.execute('SELECT pid, n FROM pools').fetchall())
    acts = collections.defaultdict(lambda: ([], []))                     # pool n -> (blocks, act), loaded lazily
    con = lpdb.connect()
    L = collections.defaultdict(int)
    for o, lad, liq in con.execute('SELECT owner, ladder, liquidity FROM legs'):
        try:
            L[(o, lad)] += int(liq)
        except (TypeError, ValueError):
            pass
    rows = con.execute('SELECT owner, ladder, pool, open_blk, invested_usd, realized_pct, quote_only FROM positions '
                       'WHERE closed = 1 AND realized_pct IS NOT NULL AND invested_usd > 1 AND open_blk IS NOT NULL').fetchall()
    con.close()
    out, miss = [], 0
    for o, lad, pool, ob, inv, pct, qo in rows:
        n = pn.get((pool or '').lower())
        liq = L.get((o, lad))
        if n is None or not liq:
            miss += 1
            continue
        if n not in acts:
            acts[n] = tuple(map(list, zip(*h.execute('SELECT blk, act FROM swaps WHERE pool = ? ORDER BY blk, li', (n,)).fetchall()))) or ([], [])
        bl, ac = acts[n]
        i = bisect.bisect_left(bl, ob) - 1
        if i < 0:
            miss += 1
            continue
        a = int(ac[i] or 0)
        out.append(dict(share=liq / (liq + a), pct=max(-100.0, min(100.0, pct)), inv=inv, pool=n))
    log('leaders: %d closed positions with a share, %d without (pool or liquidity not in the store)' % (len(out), miss))
    return out


def ours():
    opens, out = {}, []
    for l in open(FOLLOW):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get('ev') == 'open':
            opens[str(r.get('leader_tid'))] = r
        elif r.get('ev') == 'close' and r.get('pnl_usd') is not None:
            o = opens.get(str(r.get('leader_tid')), {})
            sz = r.get('size_usd') or o.get('size_usd')
            if not sz:
                continue
            out.append(dict(size=sz, pct=100 * r.get('pnl_gross_usd', r['pnl_usd']) / sz, day=int(r['ts'] // 86400), tier=r.get('tier') or o.get('tier'),
                            lead_usd=o.get('leader_usd'), held=r.get('held_min')))
    return [x for x in out if x['size']]


def table(rows, key, edges, fmt, log, label):
    log('  %-18s %7s %9s %9s %9s %9s' % (label, 'n', 'mean %', 'se', 'median %', '$/copy@85'))
    for lo, hi in zip(edges, edges[1:]):
        xs = [r['pct'] for r in rows if lo <= r[key] < hi]
        if len(xs) < 5:
            continue
        m, se, md = ci(xs)
        log('  %-18s %7d %+9.2f %9.2f %+9.2f %+9.2f' % (fmt(lo, hi), len(xs), m, se, md, m * 0.85))


def run(log=print):
    P = leaders(log)
    sh = lambda lo, hi: '%.2g%% - %.2g%%' % (100 * lo, 100 * min(hi, 1))
    log('\nLEADERS, realized % per position by share of the pool at mint (capped +-100%):')
    table(P, 'share', EDGES, sh, log, 'share')
    # WITHIN A POOL: a big share is mostly a thin pool, and thin pools are their own regime. Each position's return minus
    # its pool's mean (pools with >= 5 positions), then the same buckets: what size does inside one pool
    by = collections.defaultdict(list)
    for p in P:
        by[p['pool']].append(p['pct'])
    W = [dict(p, pct=p['pct'] - S.mean(by[p['pool']])) for p in P if len(by[p['pool']]) >= 5]
    log('\n  within pool (return minus its pool\'s mean, %d positions in %d pools):' % (len(W), len({p['pool'] for p in W})))
    table(W, 'share', EDGES, sh, log, 'share')
    log('\n  by invested $:')
    table(P, 'inv', [0, 10, 30, 100, 300, 1000, 3000, 1e9], lambda lo, hi: '$%g - %s' % (lo, '$%g' % hi if hi < 1e8 else 'up'), log, 'invested')
    O = ours()
    log('\nOURS, live copies closed: %d' % len(O))
    se = [0, 30, 60, 90, 150, 1e9]
    sf = lambda lo, hi: '$%g - %s' % (lo, '$%g' % hi if hi < 1e8 else 'up')
    table(O, 'size', se, sf, log, 'our size')
    # the desk grew its size over time (free/2 sizing as the book grew): each copy minus its UTC day's mean
    bd = collections.defaultdict(list)
    for o in O:
        bd[o['day']].append(o['pct'])
    log('\n  within day (return minus that day\'s mean):')
    table([dict(o, pct=o['pct'] - S.mean(bd[o['day']])) for o in O], 'size', se, sf, log, 'our size')
    return dict(leaders=P, ours=O)


if __name__ == '__main__':
    run()
