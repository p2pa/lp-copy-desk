"""FACTORS FOR THE COPY DESK (owner 2026-09-30, 'factors are the alphas you deem canonical; judge a new alpha on what they do
not explain'). Two questions, from the replayed copies (lpdb.copy_trades) and the history store:

  features  a new pool feature is only new if it predicts the copy P&L LEFT after the canonical pool factors. Per day, the
            squad's copies (one sample per pool and hour, research/lpfeat) are regressed on activity (lvol24), volatility
            (rv24) and depth (ldepth); each candidate's rank IC is measured on the residual.
  wallets   a wallet's daily copy P&L = beta x the market (every replayed copy that day, all wallets) + alpha. Blind, day by
            day: rank wallets on the days before, by raw mean (what the copy score does) or by alpha, take the top 20, and
            measure their copies on the next day.

`python -m research.lpfactor [features|wallets]`."""
import collections, math, statistics as S, sys, time

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
from copydesk import lpdb
from research import lpfeat as F

CANON = ['lvol24', 'rv24', 'ldepth']
CAND = ['volz', 'turn24', 'aimb24', 'pop24', 'popimb24', 'shp72', 'mom24', 'retail24', 'nsw24', 'fee', 'edgevol', 'feeyield', 'netlvr']


def ols_resid(X, y):
    """Residuals of y on [1, X] (normal equations, Gauss-Jordan; a handful of regressors)."""
    k = len(X[0]) + 1
    A = [[0.0] * (k + 1) for _ in range(k)]
    for row, t in zip(X, y):
        z = [1.0] + list(row)
        for i in range(k):
            for j in range(k):
                A[i][j] += z[i] * z[j]
            A[i][k] += z[i] * t
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(A[r][c]))
        A[c], A[p] = A[p], A[c]
        if abs(A[c][c]) < 1e-12:
            return None
        for r in range(k):
            if r != c:
                f = A[r][c] / A[c][c]
                A[r] = [a - f * b for a, b in zip(A[r], A[c])]
    beta = [A[i][k] / A[i][i] for i in range(k)]
    return [t - sum(b * v for b, v in zip(beta, [1.0] + list(row))) for row, t in zip(X, y)]


def features(owners, log=print, extra=None, canon=None, cand=None):
    """`extra(samples)` adds a candidate's key to every sample (research/poolscore: the trailing pool score); `canon` widens
    the factors a candidate must beat (e.g. + volz, the live cold-pool rule)."""
    CANON = canon or globals()['CANON']
    _, sm = F.main(days=9.0, log=lambda m: None, owners=owners)
    if extra:
        extra(sm)
    days = sorted({s['day'] for s in sm})
    feats = CANON + (cand or CAND)
    corr = {}
    for a in feats:                                                       # how much the candidates are the canon again
        for b in CANON:
            xs = [(s[a], s[b]) for s in sm if s[a] is not None and s[b] is not None]
            corr[(a, b)] = F.spearman([x for x, _ in xs], [y for _, y in xs]) or 0.0
    out = ['FEATURES: %d squad samples, %d days; canonical pool factors %s' % (len(sm), len(days), ', '.join(CANON)),
           '  %-9s %8s %8s %8s   %9s %6s   %9s %6s' % ('feature', 'r lvol24', 'r rv24', 'r ldepth', 'raw IC', 't', 'resid IC', 't'),
           '  (days with >= 20 samples: %d)' % len([d for d in days if sum(1 for s in sm if s['day'] == d) >= 20])]
    for fe in feats:
        raw, res = [], []
        for d in days:
            g = [s for s in sm if s['day'] == d and all(s[c] is not None for c in CANON) and s[fe] is not None]
            if len(g) < 20:
                continue
            y = [s['y'] for s in g]
            r = ols_resid([[s[c] for c in CANON] for s in g], y)
            a = F.spearman([s[fe] for s in g], y)
            b = F.spearman([s[fe] for s in g], r) if r else None
            if a is not None:
                raw.append(a)
            if b is not None:
                res.append(b)
        t = lambda v: S.mean(v) / (S.stdev(v) / math.sqrt(len(v))) if len(v) > 2 and S.stdev(v) > 0 else 0.0
        out.append('  %-9s %+8.2f %+8.2f %+8.2f   %+9.3f %+6.2f   %+9.3f %+6.2f' % (fe, corr[(fe, 'lvol24')], corr[(fe, 'rv24')], corr[(fe, 'ldepth')],
                   S.mean(raw) if raw else 0, t(raw), S.mean(res) if res else 0, t(res)))
    for l in out:
        log(l)
    return out


def wallets(log=print, top=20, min_copies=5):
    con = lpdb.connect()
    t_now = time.time()
    rows = con.execute('SELECT owner, t1, 100.0 * pnl / size_usd FROM copy_trades WHERE t1 >= ?', (t_now - 9.5 * 86400,)).fetchall()
    con.close()
    day = lambda t: int((t_now - t) // 86400)                            # 0 = the last 24 h
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    mkt = collections.defaultdict(list)
    for o, t1, p in rows:
        p = max(-100.0, min(100.0, p))
        per[o][day(t1)].append(p)
        mkt[day(t1)].append(p)
    D = sorted(mkt, reverse=True)                                        # oldest first
    m = {d: S.mean(v) for d, v in mkt.items()}
    out = ['WALLETS: %d wallets, %d copies, %d days; the market = the mean copy P&L %% of every replayed copy that day' % (len(per), len(rows), len(D)),
           '  market by day (oldest first): %s' % ' '.join('%+.2f' % m[d] for d in D)]
    res = collections.defaultdict(list)
    betas = []
    for i in range(3, len(D)):                                           # judge day D[i] on the days before it
        past, nxt = D[:i], D[i]
        stat = {}
        for o, byd in per.items():
            xs = [(S.mean(byd[d]), m[d], len(byd[d])) for d in past if byd.get(d)]
            n = sum(k for _, _, k in xs)
            if len(xs) < 2 or n < min_copies:
                continue
            ys, ms = [x for x, _, _ in xs], [x for _, x, _ in xs]
            mm = S.mean(ms)
            vm = sum((x - mm) ** 2 for x in ms)
            beta = sum((a - S.mean(ys)) * (b - mm) for a, b in zip(ys, ms)) / vm if vm > 0 else 0.0
            beta = max(-3.0, min(3.0, beta))
            alpha = S.mean(a - beta * b for a, b in zip(ys, ms))
            raw = sum(sum(byd[d]) for d in past if byd.get(d)) / n
            se = (S.stdev([p for d in past for p in byd.get(d, [])]) / math.sqrt(n)) if n > 1 else abs(raw)
            stat[o] = dict(raw=raw, score=(raw - se) * n / len(past), alpha=alpha, beta=beta)
            if i == len(D) - 1:
                betas.append(beta)
        nxt_pnl = lambda o: S.mean(per[o][nxt]) if per[o].get(nxt) else None
        for rule in ('raw', 'score', 'alpha'):
            best = sorted(stat, key=lambda o: -stat[o][rule])[:top]
            got = [x for x in (nxt_pnl(o) for o in best) if x is not None]
            res[rule].append(S.mean(got) if got else 0.0)
        res['market'].append(m[nxt])
    out.append('  next-day copy P&L %% of the top %d wallets, picked blind on the days before (per judged day, oldest first):' % top)
    for rule, nm in (('raw', 'raw mean per copy'), ('score', 'copy score (mean - SE) x copies/day'), ('alpha', 'alpha (market removed)'), ('market', 'the market (every copy)')):
        v = res[rule]
        out.append('    %-38s mean %+6.2f   %s' % (nm, S.mean(v), ' '.join('%+.2f' % x for x in v)))
    b = sorted(betas)
    out.append('  wallet betas to the market (last fit): median %.2f, q10 %.2f, q90 %.2f, %d wallets' % (b[len(b) // 2], b[len(b) // 10], b[9 * len(b) // 10], len(b)))
    for l in out:
        log(l)
    return out


if __name__ == '__main__':
    a = sys.argv[1:] or ['features', 'wallets']
    if 'features' in a:
        from copydesk import lpfollow as L
        from core import chain as cm
        c = cm.load('robinhood')
        features(set(L.leaders(c) + L.bench(c) + L.filler(c)))
    if 'wallets' in a:
        wallets()
