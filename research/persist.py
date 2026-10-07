"""DOES WINNING PERSIST? (owner 2026-10-02, the critique on the Model Wallet Study page: "rank wallets - and separately pools - by
net P&L in window A, measure in window B, report the rank correlation with an interval. If it is near zero there is nothing
to copy and the squad's +$ is the right tail of a lottery.")

From the replayed copies (lpdb.copy_trades: every copy at our size and the live lag, each capped at +-100% of its size):
for every day d in the store after a `lookback`-day warm-up,
  A = the `lookback` days before d    B = day d
  wallets (and pools) are ranked in A by the live copy score ((mean - SE) x copies a day) and by plain net $;
  in B: the Spearman correlation of the A-rank with B's net $, and what the top 10 / top 20 by A made in B against the
  average wallet. Intervals: the day-to-day spread (t over days) and a block bootstrap over days.
`python -m research.persist [lookback days] [step days] [out.json]` (step = lookback + 1: independent tests)."""
import collections, math, random, statistics as S, sys, time

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
from copydesk import lpdb                                                   # noqa: E402

SIZE, CAP, GAS = 82.0, 1.0, 0.033
DAY = 86400.0


def spearman(x, y):
    if len(x) < 5:
        return None
    rk = lambda v: {i: r for r, i in enumerate(sorted(range(len(v)), key=lambda i: v[i]))}
    a, b = rk(x), rk(y)
    n = len(x)
    ma, mb = (n - 1) / 2, (n - 1) / 2
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    den = math.sqrt(sum((a[i] - ma) ** 2 for i in range(n)) * sum((b[i] - mb) ** 2 for i in range(n)))
    return num / den if den else None


def score(xs, days):
    n = len(xs)
    mu = sum(xs) / n
    se = S.stdev(xs) / math.sqrt(n) if n > 1 else abs(mu)
    return (mu - se) * n / days


def run(lookback=3.0, step=1.0, log=print):
    """step = days between test days; step = lookback + 1 makes every (A, B) pair disjoint from every other: independent tests."""
    con = lpdb.connect()
    rows = con.execute('SELECT owner, pool, t0, t1, pnl, size_usd FROM copy_trades WHERE pnl IS NOT NULL').fetchall()
    con.close()
    cp = [(o, p, t0, t1, max(-CAP * SIZE, min(CAP * SIZE, pnl * SIZE / (sz or 25.0))) - GAS) for o, p, t0, t1, pnl, sz in rows if t1]
    first, last = min(r[3] for r in cp), max(r[3] for r in cp)
    days = [first + (lookback + k * step) * DAY for k in range(int((last - first) / DAY - lookback))]
    days = [d for d in days if d + DAY <= last + 1]
    out = {}
    for unit, ix in (('wallet', 0), ('pool', 1)):
        per = []
        for d in days:
            A, B, Bn = collections.defaultdict(list), collections.defaultdict(float), collections.Counter()
            for r in cp:
                if d - lookback * DAY <= r[3] < d:              # closed in A
                    A[r[ix]].append(r[4])
                elif d <= r[3] < d + DAY:                        # closed in B
                    B[r[ix]] += r[4]
                    Bn[r[ix]] += 1
            ks = [k for k in A if k in B]
            if len(ks) < 20:
                continue
            sc = {k: score(A[k], lookback) for k in ks}
            net = {k: sum(A[k]) for k in ks}
            rho_s = spearman([sc[k] for k in ks], [B[k] for k in ks])
            rho_n = spearman([net[k] for k in ks], [B[k] for k in ks])
            top = lambda m, n: sorted(ks, key=lambda k: -m[k])[:n]
            avg = S.mean(B[k] for k in ks)
            per.append(dict(day=time.strftime('%m-%d', time.gmtime(d)), n=len(ks), rho_score=rho_s, rho_net=rho_n, avg=avg,
                            top10=S.mean(B[k] for k in top(sc, 10)), top20=S.mean(B[k] for k in top(sc, 20)),
                            top10_net=S.mean(B[k] for k in top(net, 10)), bottom20=S.mean(B[k] for k in sorted(ks, key=lambda k: sc[k])[:20]),
                            top20_pct=100 * sum(B[k] for k in top(sc, 20)) / sum(Bn[k] for k in top(sc, 20)) / SIZE,
                            avg_pct=100 * sum(B.values()) / sum(Bn.values()) / SIZE))
        out[unit] = per
    # summaries with intervals over days (t) and a bootstrap over days
    rng = random.Random(7)
    summ = {}
    for unit, per in out.items():
        if not per:
            continue
        s = {}
        for key in ('rho_score', 'rho_net', 'top10', 'top20', 'top10_net', 'avg', 'bottom20', 'top20_pct', 'avg_pct'):
            v = [p[key] for p in per if p[key] is not None]
            m = S.mean(v)
            sd = S.stdev(v) if len(v) > 1 else 0.0
            boots = sorted(S.mean(rng.choice(v) for _ in v) for _ in range(2000))
            s[key] = dict(mean=m, t=m / (sd / math.sqrt(len(v))) if sd else 0.0, lo=boots[49], hi=boots[1949], days=len(v))
        for name, a, b in (('top20_minus_avg', 'top20', 'avg'), ('avg_minus_bottom20', 'avg', 'bottom20'), ('pct_edge', 'top20_pct', 'avg_pct')):
            edge = [p[a] - p[b] for p in per]
            boots = sorted(S.mean(rng.choice(edge) for _ in edge) for _ in range(2000))
            s[name] = dict(mean=S.mean(edge), lo=boots[49], hi=boots[1949], days=len(edge), ahead=sum(1 for e in edge if e > 0))
        summ[unit] = s
    log('PERSISTENCE: lookback %g d -> the next day, step %g d, %d test days, copies at $%.0f (capped +-100%%, gas $%.3f)' % (lookback, step, len(out.get('wallet', [])), SIZE, GAS))
    for unit, s in summ.items():
        log('  %s: rank corr (copy score) %+.3f [%+.3f, %+.3f] t %+.2f | rank corr (net $) %+.3f [%+.3f, %+.3f]' % (
            unit, s['rho_score']['mean'], s['rho_score']['lo'], s['rho_score']['hi'], s['rho_score']['t'],
            s['rho_net']['mean'], s['rho_net']['lo'], s['rho_net']['hi']))
        log('    next-day $ per %s: top 10 %+.2f, top 20 %+.2f, average %+.2f, bottom 20 %+.2f | top 20 - average %+.2f [%+.2f, %+.2f], ahead %d/%d days' % (
            unit, s['top10']['mean'], s['top20']['mean'], s['avg']['mean'], s['bottom20']['mean'],
            s['top20_minus_avg']['mean'], s['top20_minus_avg']['lo'], s['top20_minus_avg']['hi'], s['top20_minus_avg']['ahead'], s['top20_minus_avg']['days']))
        log('    per copy: top 20 %+.2f%%, average %+.2f%% | edge %+.2f pts [%+.2f, %+.2f] | average - bottom 20 %+.2f [%+.2f, %+.2f], ahead %d/%d' % (
            s['top20_pct']['mean'], s['avg_pct']['mean'], s['pct_edge']['mean'], s['pct_edge']['lo'], s['pct_edge']['hi'],
            s['avg_minus_bottom20']['mean'], s['avg_minus_bottom20']['lo'], s['avg_minus_bottom20']['hi'], s['avg_minus_bottom20']['ahead'], s['avg_minus_bottom20']['days']))
    for unit, per in out.items():
        for p in per:
            log('    %s %s n %4d  rho %+.3f  top10 %+7.2f  top20 %+7.2f  avg %+6.2f  bottom20 %+7.2f' % (
                unit, p['day'], p['n'], p['rho_score'] or 0, p['top10'], p['top20'], p['avg'], p['bottom20']))
    return dict(per=out, summary=summ, lookback=lookback)


if __name__ == '__main__':
    import json
    lb = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    st = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    r = run(lb, st)
    if len(sys.argv) > 3:
        json.dump(r, open(sys.argv[3], 'w'))
