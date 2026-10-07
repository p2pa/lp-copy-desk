"""THE POOL SCORE (owner 2026-10-02, Model Wallet Study seventh response: "build the pool score"; research/persist.py found the
one trailing edge with an interval above zero: top-20 pools by 1-day trailing copy score earn +1.24% per copy, 5 of 5
independent days, in a universe that averages +0.07%).

score(pool, t) = the live copy score of the pool's replayed copies (lpdb.copy_trades, every watched wallet, our size, capped
+-100%, minus gas) that CLOSED in [t - lookback, t): (mean - SE) x copies a day. Pools with no closed copy in the window
have no score. rank(pool, t) = its percentile among every pool scored at t (1.0 = best).

`deciles`: the question a copy desk can act on (it cannot steer copies, only skip or resize them). Every live copy
(var/lp/follow.jsonl closes) gets its pool's rank at its own entry time; realized return per copy by decile, plus the
copies with no score. The achievable gain of a skip rule reads off the bottom rows.

`python -m research.poolscore [deciles|residual|selftest] [lookback days]`."""
import bisect, collections, json, math, os, statistics as S, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from copydesk import lpdb                                                   # noqa: E402

SIZE, CAP, GAS = 82.0, 1.0, 0.033                                           # research/persist.py's copy
DAY = 86400.0
FOLLOW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chains/robinhood/var/lp/follow.jsonl')


class Scores:
    """Every pool's closed replayed copies, sorted by close time, so score(pool, t) is two bisects and a sum."""

    def __init__(self, lookback=1.0, fresh=False):
        """fresh: how stale the score is allowed to be, in hours (True = 24: the 03:00 UTC nightly the live desk has; 0 or
        False = continuous). At time t the window ends at the last refresh before t, refreshes on a grid anchored at 03:00."""
        self.lb = lookback * DAY
        self.fresh = 24.0 if fresh is True else float(fresh or 0)
        con = lpdb.connect()
        rows = con.execute('SELECT pool, t1, pnl, size_usd FROM copy_trades WHERE pnl IS NOT NULL AND t1 IS NOT NULL AND pool IS NOT NULL').fetchall()
        con.close()
        by = collections.defaultdict(list)
        for p, t1, pnl, sz in rows:
            by[p.lower()].append((t1, max(-CAP * SIZE, min(CAP * SIZE, pnl * SIZE / (sz or 25.0))) - GAS))
        self.t, self.v = {}, {}
        for p, xs in by.items():
            xs.sort()
            self.t[p], self.v[p] = [x[0] for x in xs], [x[1] for x in xs]
        self.first = min(t[0] for t in self.t.values())

    def score(self, pool, t):
        ts = self.t.get(pool.lower())
        if not ts:
            return None
        if self.fresh:
            r = self.fresh * 3600                                         # the last refresh at or before t
            t = (t - 3 * 3600) // r * r + 3 * 3600
        i, j = bisect.bisect_left(ts, t - self.lb), bisect.bisect_left(ts, t)
        xs = self.v[pool.lower()][i:j]
        if not xs:
            return None
        n = len(xs)
        mu = sum(xs) / n
        se = S.stdev(xs) / math.sqrt(n) if n > 1 else abs(mu)
        return (mu - se) * n / (self.lb / DAY)

    def rank(self, pool, t):
        """(percentile among the pools scored at t, the score, how many pools were scored); None when the pool has none."""
        s = self.score(pool, t)
        if s is None:
            return None
        allv = [x for x in (self.score(p, t) for p in self.t) if x is not None]
        return sum(1 for x in allv if x <= s) / len(allv), s, len(allv)


def live_copies():
    opens, out = {}, []
    for l in open(FOLLOW):
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get('ev') == 'open':
            opens[str(r.get('leader_tid'))] = r
        elif r.get('ev') == 'close' and r.get('pnl_usd') is not None:
            o = opens.get(str(r.get('leader_tid')))
            sz = r.get('size_usd') or (o or {}).get('size_usd')
            if o and sz:
                out.append(dict(pool=r['pool'], t=o['ts'], pct=100 * r.get('pnl_gross_usd', r['pnl_usd']) / sz, usd=r.get('pnl_gross_usd', r['pnl_usd']), size=sz, tier=r.get('tier') or o.get('tier')))
    return out


def deciles(lookback=1.0, log=print):
    sc = Scores(lookback)
    C = live_copies()
    for c in C:
        r = sc.rank(c['pool'], c['t'])
        c['rank'], c['score'], c['npools'] = r if r else (None, None, None)
    days = sorted({int(c['t'] // DAY) for c in C})
    log('POOL SCORE: lookback %g d, %d live copies closed over %d UTC days (%s .. %s); replayed copies from %s' % (
        lookback, len(C), len(days), time.strftime('%m-%d', time.gmtime(days[0] * DAY)), time.strftime('%m-%d', time.gmtime(days[-1] * DAY)),
        time.strftime('%m-%d %H:%M', time.gmtime(sc.first))))
    log('  %-16s %5s %9s %7s %9s %9s %9s' % ('pool rank at entry', 'n', 'mean %', 'se', 'median %', 'sum $', 'win %'))
    rows = [('no score', [c for c in C if c['rank'] is None])]
    for k in range(10):
        lo, hi = k / 10, (k + 1) / 10
        rows.append(('%d-%d%%' % (100 * lo, 100 * hi), [c for c in C if c['rank'] is not None and lo < c['rank'] <= hi + (1e-9 if k == 9 else 0)]))
    out = []
    for name, g in rows:
        if not g:
            log('  %-16s %5d' % (name, 0))
            continue
        xs = [c['pct'] for c in g]
        se = S.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float('nan')
        out.append(dict(bucket=name, n=len(g), mean=S.mean(xs), se=se, median=S.median(xs), usd=sum(c['usd'] for c in g),
                        win=100 * sum(1 for x in xs if x > 0) / len(xs)))
        log('  %-16s %5d %+9.2f %7.2f %+9.2f %+9.2f %8.0f%%' % (name, len(g), out[-1]['mean'], se, out[-1]['median'], out[-1]['usd'], out[-1]['win']))
    # what a skip rule would have kept: drop the copies below each cut, everything else as booked
    log('  skip below rank:  ' + '  '.join('%d%% -> %+.2f$ (%d left)' % (100 * cut, sum(c['usd'] for c in C if c['rank'] is None or c['rank'] > cut),
                                                                       sum(1 for c in C if c['rank'] is None or c['rank'] > cut)) for cut in (0, 0.1, 0.2, 0.3, 0.5)))
    return dict(rows=out, copies=C)


def residual(lookback=1.0, log=print):
    """Is the pool score anything beyond the canonical pool factors? research/lpfactor's residual test on the squad's
    samples, with volz (the live cold-pool rule) added to the factors it must beat. pscore = the rank at the sample's
    entry, pscore_n = how many copies closed in the pool in the window (is the score just activity?)."""
    from copydesk import lpfollow as L
    from core import chain as cm
    from research import lpfactor as LF
    sc = Scores(lookback)

    def add(sm):
        for x in sm:
            r = sc.rank(x['pid'], x['t0'])
            x['pscore'] = r[0] if r else None
            ts = sc.t.get(x['pid'].lower(), [])
            x['pscore_n'] = bisect.bisect_left(ts, x['t0']) - bisect.bisect_left(ts, x['t0'] - sc.lb)
    c = cm.load('robinhood')
    return LF.features(set(L.leaders(c) + L.bench(c) + L.filler(c)), log=log, extra=add,
                       canon=LF.CANON + ['volz'], cand=['pscore', 'pscore_n'])


def _selftest():
    sc = Scores.__new__(Scores)
    sc.fresh = 0.0
    sc.lb, sc.t, sc.v, sc.first = DAY, {'a': [10.0, 20.0, 30.0], 'b': [15.0]}, {'a': [1.0, 2.0, 3.0], 'b': [-5.0]}, 10.0
    assert sc.score('a', 25.0) is not None and sc.score('a', 5.0) is None, 'only copies closed before t'
    assert abs(sc.score('a', 25.0) - (1.5 - S.stdev([1.0, 2.0]) / math.sqrt(2)) * 2) < 1e-9, '(mean - SE) x copies a day'
    assert sc.rank('a', 25.0)[0] == 1.0 and sc.rank('b', 25.0)[0] == 0.5, 'percentile among scored pools'
    assert sc.score('a', 30.0 + DAY + 1) is None, 'the window ends'
    print('poolscore selftest: 4 asserts')


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == 'selftest':
        _selftest()
    elif a and a[0] == 'residual':
        residual(float(a[1]) if len(a) > 1 else 1.0)
    else:
        deciles(float(a[1]) if len(a) > 1 else 1.0)
