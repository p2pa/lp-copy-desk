"""THE ROBOT'S POOL SCORE, trained on the squad's winners (owner 2026-10-03: "focus on pool selection, improve that pool selection
score. train it on all winners from squad"). The robot (research/paperlive, bid wall 0.3-70 %) picks pools by hard gates only
(ft24/ft30 >= 5 %/day, trend >= 0.8, ret15 >= -20) and the first pool through takes the cash. Here a model learns, from the
robot's own candidate features plus what the squad did in that pool BEFORE the moment, how likely a squad copy opened in that
pool in the next hour wins - and the robot bids only on pools that score high.

Data: research/bidwall's full 15-min grid (BW_TAG=gap: 27,753 candidates, paths for the 0_70 wall), the squad's replayed copies
(lpwallets.db copy_trades of [lp.follow] leader + bench + filler). Judged blind: train on one half, score the other, the $1,000
account (bidwall.account) with the live exits, against the live gates on the same half.
`python3 -m research.squadscore` (system python3: numpy + sklearn).

RESULT 2026-10-03 (9.4 days, 27,753 candidates, live gates +$94.09 / +$79.94 a day per half): NOTHING PASSES, the robot is unchanged.
- squad-trained score: predicts squad winners blind (AUC 0.88) but not the robot's own outcome (AUC 0.544, corr 0.03); every
  threshold loses to the live gates on half B. The squad wins by its range and timing, not by pool features the robot can see.
- trained on the robot's own wins: drop-bottom 30/40/45/50 % beat live on both halves, 35/55/60 % do not - a jumpy frontier.
- squad presence (a squad copy opened in the pool in the last 30-180 min) is real: +1.2-1.5 %/bid vs +0.9-1.0 % under the
  live gates, but on 1/4 of the bids, so as a filter it cuts $/day. As 1.5-2x size it beats live in the flat-P&L view; with
  real cash (bigger bids block others) only 30 min / 1.5x beats live on both halves, its neighbours do not."""
import bisect, collections, os, sqlite3, sys, tomllib

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('BW_TAG', 'gap')
os.environ.setdefault('BW_RANGES', '0_70,5_45,10_45,12_45,15_45,10_70,15_70')
from research import bidwall as BW                                              # noqa: E402

LIVE_EX = dict(stop=None, win_min=10, miss_min=20, below_min=60, max_h=12)      # paperbot bw_main
LIVE_G = dict(ft24=5, ft30=5, trend=0.8, ret15=-20, fee=(0, 99))
VAR = os.path.join(ROOT, 'chains/robinhood/var')
FEATS = ['ft24', 'ft30', 'trend', 'ret15', 'tvl', 'vol30', 'fee', 'hi_pct', 'lo_pct', 'age_h']
SQF = ['sq_n24', 'sq_win24', 'sq_pnl24', 'sq_n3', 'sq_w', 'sq_tok24']


def squad():
    c = tomllib.load(open(os.path.join(ROOT, 'chains/robinhood/chain.toml'), 'rb'))['lp']['follow']
    return {w.lower() for w in c['leader'] + c['bench'] + c['filler']}


def clock():
    """block -> unix ts (lphist's block times)."""
    rows = sqlite3.connect(os.path.join(VAR, 'lphist.db')).execute('SELECT blk, ts FROM blocks ORDER BY blk').fetchall()
    b, t = np.array([r[0] for r in rows], float), np.array([r[1] for r in rows], float)
    return lambda x: np.interp(x, b, t)


def dataset():
    T, P, F = BW.load()
    x, idx = BW.exit_eval(P[:, 0, :], F[:, 0, :], LIVE_EX)                        # the robot's own outcome, live wall + exits
    ts = clock()(np.array([t['b'] for t in T], float))
    sq = squad()
    db = sqlite3.connect(os.path.join(VAR, 'lpwallets.db'))
    by_pool, by_tok = collections.defaultdict(list), collections.defaultdict(list)
    for o, t0, pnl, size, pool, tok in db.execute('SELECT owner, t0, pnl, size_usd, pool, token FROM copy_trades'):
        if o.lower() in sq and pool:
            by_pool[pool.lower()].append((t0, pnl / size if size else 0.0))
            by_tok[(tok or '').lower()].append(t0)
    for v in by_pool.values():
        v.sort()
    for v in by_tok.values():
        v.sort()
    X, y, ysq = [], [], []
    for t, s in zip(T, ts):
        ev = by_pool.get(t['pid'].lower(), [])
        tt = [e[0] for e in ev]
        i24, i3, i0, i1 = bisect.bisect_left(tt, s - 86400), bisect.bisect_left(tt, s - 3 * 3600), bisect.bisect_left(tt, s), bisect.bisect_left(tt, s + 3600)
        past = [e[1] for e in ev[i24:i0]]
        nxt = [e[1] for e in ev[i0:i1]]                                             # the label: squad copies opened in the next hour
        tk = by_tok.get(t['tok'].lower(), [])
        X.append([t[k] for k in FEATS] + [len(past), sum(1 for p in past if p > 0), sum(past), i0 - i3,
                                          (sum(1 for p in past if p > 0) + 1) / (len(past) + 2),
                                          bisect.bisect_left(tk, s) - bisect.bisect_left(tk, s - 86400)])
        y.append(1 if any(p > 0 for p in nxt) else 0)
        ysq.append(len(nxt))
    X = np.array(X, float)
    X[:, FEATS.index('tvl')] = np.log10(X[:, FEATS.index('tvl')] + 1)
    X[:, FEATS.index('vol30')] = np.log10(X[:, FEATS.index('vol30')] + 1)
    return T, x, idx, X, np.array(y), np.array(ysq)


def fit(Xa, ya):
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=80,
                                          l2_regularization=1.0, random_state=0).fit(Xa, ya)


def main(log=print):
    from sklearn.metrics import roc_auc_score
    T, x, idx, X, y, ysq = dataset()
    b = np.array([t['b'] for t in T])
    lo, hi = b.min(), b.max()
    mid = (lo + hi) // 2
    A, B = b < mid, b >= mid
    live = BW.mask(T, LIVE_G)
    log('POOLSCORE: %d candidates, %.1f days | squad winner in the pool next hour: %d (%.1f%%) | live gates pass %d' % (
        len(T), (hi - lo) / (24 * BW.HOUR), y.sum(), 100 * y.mean(), live.sum()))
    log('  robot outcome (live wall + exits): squad-winner rows %+.2f%% (n %d) vs others %+.2f%%' % (x[y == 1].mean(), y.sum(), x[y == 0].mean()))
    fmt = lambda r: '$%+7.2f/d %5.1f bids/d dd $%6.1f win %3.0f%% %+.2f/bid %d/%d days+' % (
        r['usd_day'], r['n_day'], r['dd'], 100 * r['win'], r['per'], r['days_pos'], r['ndays'])
    half = {'A': (lo, mid), 'B': (mid, hi + 1)}
    base = {h: BW.account(T, x, idx, live, lo=l_, hi=h_) for h, (l_, h_) in half.items()}
    for h in 'AB':
        log('  LIVE gates          %s %s' % (h, fmt(base[h])))
    out = {}
    for name, cols, lab in (('squad-trained, all features', slice(None), y),
                            ('squad-trained, robot features only', slice(0, len(FEATS)), y),
                            ('robot-trained (own win), all features', slice(None), (x > 0).astype(int))):
        sc = np.zeros(len(T))
        for tr, te in ((A, B), (B, A)):
            m = fit(X[tr][:, cols], lab[tr])
            sc[te] = m.predict_proba(X[te][:, cols])[:, 1]
        auc_sq = roc_auc_score(y, sc)
        auc_rb = roc_auc_score((x > 0).astype(int), sc)
        corr = np.corrcoef(sc, x)[0, 1]
        log('\n  %s: blind AUC squad-winner %.3f, robot-win %.3f, corr with robot %% %.3f' % (name, auc_sq, auc_rb, corr))
        q = np.quantile(sc, [0.2, 0.4, 0.5, 0.6, 0.7, 0.8])
        for gate_name, g in (('score only', np.ones(len(T), bool)), ('live gates + score', live)):
            for qq, th in zip((20, 40, 50, 60, 70, 80), q):
                m_ = g & (sc >= th)
                r = {h: BW.account(T, x, idx, m_, lo=l_, hi=h_) for h, (l_, h_) in half.items()}
                beat = all(r[h]['usd_day'] > base[h]['usd_day'] and r[h]['dd'] <= max(base['A']['dd'], base['B']['dd']) for h in 'AB')
                log('    %-18s top %2d%%  A %s\n    %-18s          B %s %s' % (gate_name, 100 - qq, fmt(r['A']), '', fmt(r['B']), ' <- BEATS LIVE' if beat else ''))
                out[(name, gate_name, qq)] = (r, beat)
        if name.startswith('squad-trained, all'):
            m = fit(X[:, cols], lab)
            from sklearn.inspection import permutation_importance
            pi = permutation_importance(m, X[:, cols], lab, n_repeats=3, random_state=0, scoring='roc_auc')
            names = FEATS + SQF
            log('    importance (in-sample AUC drop): ' + ', '.join('%s %.3f' % (names[i], pi.importances_mean[i])
                                                                   for i in np.argsort(-pi.importances_mean)[:8]))
    return out


if __name__ == '__main__':
    main()
