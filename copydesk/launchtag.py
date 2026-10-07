"""LAUNCH TAGS ON THE LIVE COPIES - measured, not enforced (owner 2026-10-03, after ASTRAL -$92.36 in a 3-minute-old pool: "should we
add that rule about the liquidity owner to the live desk?" - not on 7 days of live closes: young pools with the creator still in made
+$32.42 on 120 copies). Every closed copy is tagged from the history store with the pool's age at OUR entry and whether the pool's
creator (its first LP) had already pulled liquidity then, and the P&L is split by those tags - so in 2-3 weeks the data says whether
creator-in launches carry a tail worth a size cap (to backtest under the desk rule before anything changes).
Read-only; nothing in the desk calls this. `python -m copydesk.launchtag [days]` (home: the store is here)."""
import bisect, collections, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOUR = 36_000


def tags(days=7.0):
    from core import chain as cm
    from copydesk import lpfollow as L, lphist
    c = cm.load('robinhood')
    R = lphist.Reader(c)
    st = json.load(open(L._state_path(c)))
    now = time.time()
    n_of = {v[0].lower(): k for k, v in R.pid.items()}
    tt = [t for _, t in R.bt]
    bb = [b for b, _ in R.bt]

    def blk(ts):
        i = min(max(bisect.bisect_left(tt, ts), 1), len(tt) - 1)
        return int(bb[i - 1] + (bb[i] - bb[i - 1]) * (ts - tt[i - 1]) / max(tt[i] - tt[i - 1], 1e-9))
    first, pos, out = {}, {}, []
    for o in st['ours'].values():
        if not o.get('closed') or now - (o.get('exit_ts') or 0) > days * 86400:
            continue
        n = n_of.get(o['pool'].lower())
        if n is None:
            continue
        if n not in first:
            first[n] = R.con.execute('SELECT MIN(blk) FROM swaps WHERE pool = ?', (n,)).fetchone()[0]
            pos[n] = R.con.execute('SELECT p.close_blk, nf.owner FROM positions p JOIN nfts nf ON nf.tid = p.tid WHERE p.pool = ? ORDER BY p.open_blk',
                                   (n,)).fetchall()
        if first[n] is None:
            continue
        b = blk(o['entry_ts'])
        ms = pos[n]
        creator = ms[0][1] if ms else None
        out.append(dict(tid=o['leader_tid'], sym=o.get('symbol'), age_h=round((b - first[n]) / HOUR, 2),
                        creator_out=any(w == creator and cb is not None and cb <= b for cb, w in ms), pnl=o['pnl_usd'], size=o['size_usd'],
                        leader=o.get('leader'), exit_ts=o.get('exit_ts')))
    return out


def report(days=7.0, log=print):
    rows = tags(days)
    g = collections.defaultdict(list)
    for r in rows:
        k = ('< 1 h' if r['age_h'] < 1 else '1-6 h' if r['age_h'] < 6 else '6 h +', '' if r['age_h'] >= 6 else 'creator out' if r['creator_out'] else 'creator in')
        g[k].append(r)
    log('LAUNCH TAGS: %d closed live copies over %g days, by pool age and the creator at our entry' % (len(rows), days))
    for k in sorted(g):
        v = g[k]
        p = sum(r['pnl'] for r in v)
        worst = min(v, key=lambda r: r['pnl'])
        log('  %-6s %-12s n %4d  pnl %+8.2f (%+.2f%% of size)  win %3.0f%%  worst %s %+.2f' % (
            k[0], k[1], len(v), p, 100 * p / sum(r['size'] for r in v), 100 * sum(1 for r in v if r['pnl'] > 0) / len(v), worst['sym'], worst['pnl']))
    return rows


if __name__ == '__main__':
    report(float(sys.argv[1]) if len(sys.argv) > 1 else 7.0)
