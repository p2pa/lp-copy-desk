"""SCORE (b): WHAT THE POOL REALLY PAID ITS LPs (owner 2026-10-03, Squad Robot page, build path phase 0: "a score with no replay
at all ... it includes every LP, deployers included. It needs its own persistence evidence").

For every closed position in the history store with a clean payout (one NFT per mint tx and per exit tx, not topped up, the
pool's own payout digested by lphist.payouts): in = the position's principal at the pool's price when it opened, out = the
payout valued at the price when it closed (the token side less the pool fee, as lpcopy values an exit), return capped at
+-100%. Scaled to research/poolscore's units ($82 a position, minus gas) so the same (mean - SE) x positions a day and the
same ranks apply; PayScores is a drop-in for poolscore.Scores. Native-ETH pools have no Transfer log for a payout and are
not scored. Every LP counts, deployers and bots included: a different signal from the squad-copy score, not a cheaper copy.
`python -m research.payscore` prints coverage."""
import collections, math, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from copydesk import lpcopy as C                                            # noqa: E402
from research import poolscore as PS                                        # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chains/robinhood/var/robot/payscore.tsv')


def returns(log=print):
    """[(pool pid, close ts, return %)] for every clean closed position; cached (a closed payout never changes)."""
    if os.path.exists(CACHE):
        out = []
        for l in open(CACHE):
            p, t, r = l.split('\t')
            out.append((p, float(t), float(r)))
        return out
    from core import chain as cm
    from copydesk import lphist
    R = lphist.Reader(cm.load('robinhood'))
    con = R.con
    shared = set()
    for q in ('SELECT tid FROM positions WHERE mint_tx IN (SELECT mint_tx FROM positions GROUP BY mint_tx HAVING COUNT(*) > 1)',
              'SELECT tid FROM exits WHERE tx IN (SELECT tx FROM exits GROUP BY tx HAVING COUNT(DISTINCT tid) > 1)'):
        shared.update(r[0] for r in con.execute(q))
    rows = con.execute('SELECT tid, pool, tl, tu, L, open_blk, close_blk, paid0, paid1 FROM positions WHERE close_blk IS NOT NULL '
                       'AND paid0 IS NOT NULL AND NOT topped').fetchall()
    out, miss, t0 = [], collections.Counter(), time.time()
    by = collections.defaultdict(list)
    for r in rows:
        if r[0] not in shared:
            by[r[1]].append(r)
    for k, (n, ps) in enumerate(by.items()):
        if n not in R.pid:
            continue
        pid, c0, c1 = R.pid[n][0], R.pid[n][1].lower(), R.pid[n][2].lower()
        if C.NATIVE in (c0, c1) or not ({c0, c1} & {C.W.USDG.lower(), C.W.WETH.lower()}):
            miss['no USDG / WETH side'] += len(ps)
            continue
        sw = con.execute('SELECT blk, li, sp, fee FROM swaps WHERE pool = ? ORDER BY blk, li', (n,)).fetchall()
        if not sw:
            continue
        import bisect
        keys = [(b, l) for b, l, _, _ in sw]
        q0 = c0 in (C.W.USDG.lower(), C.W.WETH.lower())
        for tid, _, tl, tu, L, ob, cb, p0, p1 in ps:
            i, j = bisect.bisect_left(keys, (ob, 0)), bisect.bisect_right(keys, (cb, 1 << 30))
            if not i or not j:
                miss['no price'] += 1
                continue
            sp0, (sp1, fee1) = sw[i - 1][2], (sw[j - 1][2], sw[j - 1][3])
            a0, a1 = C._amounts(L, sp0, tl, tu)
            tin = C._usd(c0, c1, a0, a1, sp0, R.dec, R.eusd, ob)
            cut = 1 - fee1 / 1e6
            paid = C._usd(c0, c1, p0 if q0 else p0 * cut, p1 if not q0 else p1 * cut, sp1, R.dec, R.eusd, cb)
            if not tin or tin < 1 or paid is None or not C._sane(paid, tin):
                miss['tiny or unreadable'] += 1
                continue
            out.append((pid.lower(), R.tsof(cb), max(-100.0, min(100.0, 100.0 * (paid - tin) / tin))))
        if k % 500 == 0:
            log('  payscore: %d pools, %d positions (%.0f s)' % (k, len(out), time.time() - t0))
    with open(CACHE, 'w') as f:
        for p, t, r in out:
            f.write('%s\t%.1f\t%.4f\n' % (p, t, r))
    log('  payscore: %d positions scored (%s)' % (len(out), ', '.join('%s %d' % kv for kv in miss.items())))
    return out


class PayScores(PS.Scores):
    """poolscore.Scores over what pools really paid their LPs instead of replayed squad copies."""

    def __init__(self, lookback=1.0, fresh=False):
        self.lb = lookback * PS.DAY
        self.fresh = 24.0 if fresh is True else float(fresh or 0)
        by = collections.defaultdict(list)
        for p, t, r in returns():
            by[p].append((t, PS.SIZE * r / 100 - PS.GAS))
        self.t, self.v = {}, {}
        for p, xs in by.items():
            xs.sort()
            self.t[p], self.v[p] = [x[0] for x in xs], [x[1] for x in xs]
        self.first = min(t[0] for t in self.t.values())


if __name__ == '__main__':
    rs = returns()
    import statistics as S
    print('payscore: %d positions in %d pools, mean return %+.2f%%, median %+.2f%%' % (
        len(rs), len({p for p, _, _ in rs}), S.mean(r for _, _, r in rs), S.median(r for _, _, r in rs)))
