"""LAUNCH EVIDENCE FOR THE ROBOT (owner 2026-10-03: "find a way to include them safely, there is tons of money in there. Be experimental
and unique"). research/young + stink: alone, a wall in a pool under 1 h old buys the launch dump (-25 %/bid at 15-30 min); the live
copy desk wins there by following people. So the robot gets evidence about WHO is in a young pool, from the history store, at the
candidate's block (nothing later):
  lp15 / lp60     distinct LP wallets that minted in the pool in the last 15 / 60 min, the pool's first LP (its creator) excluded
  sq60            of them, wallets of the copy desk's squad ([lp.follow] leader + bench + filler) - the live desk's own signal
  creator_out     the pool's first LP has already removed liquidity (a rug tell)
  dd_peak         % under the highest price since the pool's first swap; peak_min: minutes since that peak
`features` (venv) writes them for research/young's candidates (BW_TAG=stink: paths for 7 ranges); `test` (python3) scores."""
import bisect, collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('BW_TAG', 'stink')
os.environ.setdefault('BW_RANGES', '0_70,20_50,30_60,40_60,40_70,50_75,60_85')
from research import bidwall as BW                                              # noqa: E402

OUT = BW.BASE + '_launch.json'


def features(log=print):
    import tomllib
    R = BW._reader()
    T = json.load(open(BW.BASE + '.meta.json'))['T']
    c = tomllib.load(open(os.path.join(os.path.dirname(BW.OUT), '..', 'chain.toml'), 'rb'))['lp']['follow']
    sq = {w.lower() for w in c['leader'] + c['bench'] + c['filler']}
    pools = sorted({t['n'] for t in T if t['age_h'] < 6})
    mints, swaps = {}, {}
    for n in pools:
        mints[n] = R.con.execute('SELECT p.open_blk, p.close_blk, n.owner FROM positions p JOIN nfts n ON n.tid = p.tid WHERE p.pool = ? ORDER BY p.open_blk', (n,)).fetchall()
        swaps[n] = R.con.execute('SELECT blk, sp FROM swaps WHERE pool = ? ORDER BY blk, li', (n,)).fetchall()
    out = []
    for t in T:
        if t['age_h'] >= 6:
            out.append(None)
            continue
        n, b = t['n'], t['b']
        ms = mints.get(n) or []
        creator = ms[0][2] if ms else None
        seen = [(ob, cb, (o or '').lower()) for ob, cb, o in ms if ob <= b]
        lp = lambda h: {o for ob, cb, o in seen if ob > b - int(h * BW.HOUR) and o != creator}
        l15, l60 = lp(0.25), lp(1)
        out_ = any(o == creator and cb is not None and cb <= b for ob, cb, o in seen)
        sw = swaps[n]
        k = bisect.bisect_right([x[0] for x in sw], b)
        tp = (lambda s: 1 / (s * s)) if t['q0'] else (lambda s: s * s)
        ps = [(x[0], tp(x[1])) for x in sw[:k] if x[1]]
        if ps:
            pk = max(ps, key=lambda z: z[1])
            dd, pm = 100 * (1 - ps[-1][1] / pk[1]), (b - pk[0]) / (BW.HOUR / 60)
        else:
            dd, pm = 0.0, 0.0
        out.append(dict(lp15=len(l15), lp60=len(l60), sq60=len(l60 & sq), creator_out=out_, dd_peak=round(dd, 2), peak_min=round(pm, 1)))
    json.dump(out, open(OUT, 'w'))
    log('LAUNCH features: %d young candidates in %d pools -> %s' % (sum(1 for o in out if o), len(pools), OUT))


def test(log=print):
    import itertools
    import numpy as np
    T, P, F = BW.load()
    fe = json.load(open(OUT))
    names = [r[0] for r in BW.RANGES]
    A_ = lambda k: np.array([t[k] for t in T], float)
    age, b = A_('age_h'), A_('b')
    lo, hi = b.min(), b.max()
    mid = (lo + hi) // 2
    H = b < mid
    G = dict(ft24=5, ft30=5, trend=0.8, ret15=-20, fee=(0, 99))
    live = BW.mask(T, G)
    young = (age >= 0.25) & (age < 6)
    f = lambda k, d=0: np.array([(o or {}).get(k, d) for o in fe], float)
    lp15, lp60, sq60, cout, dd, pm = f('lp15'), f('lp60'), f('sq60'), f('creator_out'), f('dd_peak'), f('peak_min')
    LIVE_EX = dict(stop=None, win_min=10, miss_min=20, below_min=60, max_h=12)
    EXS = [LIVE_EX, dict(stop=None, win_min=10, miss_min=20, below_min=720, max_h=12), dict(stop=None, win_min=2, miss_min=6, below_min=60, max_h=1),
           dict(stop=10.0, win_min=10, miss_min=20, below_min=60, max_h=3)]
    ev = {(v, k): BW.exit_eval(P[:, v, :], F[:, v, :], ex) for v in range(len(names)) for k, ex in enumerate(EXS)}
    x0, i0 = ev[(names.index('0_70'), 0)]
    base_m = live & (age >= 6)
    fmt = lambda r: '$%+7.2f/d %5.1f/d dd $%5.1f %3.0f%% %+.2f/bid %d/%d' % (r['usd_day'], r['n_day'], r['dd'], 100 * r['win'], r['per'], r['days_pos'], r['ndays'])
    acct = lambda x, idx, m: (BW.account(T, x, idx, m, lo=lo, hi=mid), BW.account(T, x, idx, m, lo=mid, hi=hi + 1))
    base = acct(x0, i0, base_m)
    ddm = max(base[0]['dd'], base[1]['dd'])
    Y = live & young
    log('LAUNCH TEST: %d young live-gated candidates; the live wall there %+.2f / %+.2f %%/bid' % (Y.sum(), x0[Y & H].mean(), x0[Y & ~H].mean()))
    log('  evidence alone (the live wall + live exits), mean %/bid A / B, n:')
    filt = {'lp15>=2': lp15 >= 2, 'lp15>=4': lp15 >= 4, 'lp60>=5': lp60 >= 5, 'lp60>=10': lp60 >= 10, 'lp60>=20': lp60 >= 20,
            'sq60>=1': sq60 >= 1, 'sq60>=2': sq60 >= 2, 'creator in': cout == 0, 'creator out': cout == 1,
            'dd_peak>=30': dd >= 30, 'dd_peak>=50': dd >= 50, 'dd_peak<20': dd < 20, 'peak>=30m ago': pm >= 30, 'peak<10m ago': pm < 10}
    for nm, m in filt.items():
        s = Y & m
        if s.sum() >= 20:
            log('    %-14s n %4d  %+6.2f / %+6.2f  win %.0f%%' % (nm, s.sum(), x0[s & H].mean() if (s & H).sum() else 0, x0[s & ~H].mean() if (s & ~H).sum() else 0, 100 * (x0[s] > 0).mean()))
    # policies: proven pools as live + young pools that pass a filter, with a chosen range and exit
    combos = {}
    for (a, ma), (bn, mb) in itertools.combinations(list(filt.items()), 2):
        combos['%s & %s' % (a, bn)] = ma & mb
    combos.update(filt)
    rows = []
    for nm, m in combos.items():
        s = Y & m
        if s.sum() < 30:
            continue
        for (v, k), (x, idx) in ev.items():
            mean_a, mean_b = x[s & H].mean() if (s & H).sum() else -99, x[s & ~H].mean() if (s & ~H).sum() else -99
            if min(mean_a, mean_b) <= 0:
                continue
            xx, ii = np.where(young, x, x0), np.where(young, idx, i0)
            r = acct(xx, ii, base_m | s)
            rows.append((nm, v, k, s.sum(), mean_a, mean_b, r))
    beat = lambda r: all(r[i]['usd_day'] > base[i]['usd_day'] for i in (0, 1)) and max(r[0]['dd'], r[1]['dd']) <= ddm
    log('  %-60s A %s | B %s' % ('LIVE (pools >= 6 h)', fmt(base[0]), fmt(base[1])))
    rows.sort(key=lambda z: -min(z[6][0]['usd_day'] - base[0]['usd_day'], z[6][1]['usd_day'] - base[1]['usd_day']))
    log('  young filters positive on both halves, through the account (top 15 by the worse half vs live):')
    for nm, v, k, n, ma, mb, r in rows[:15]:
        log('    + young [%s] %s %s n %d (%+.2f / %+.2f)\n        A %s | B %s%s' % (nm, names[v], {k_: v_ for k_, v_ in EXS[k].items() if v_ is not None and k_ != 'max_h' or k_ == 'max_h' and v_ != 12},
                                                                       n, ma, mb, fmt(r[0]), fmt(r[1]), '  <- BEATS LIVE' if beat(r) else ''))
    log('  %d of %d positive young slices beat live on both halves with drawdown <= $%.0f' % (sum(1 for z in rows if beat(z[6])), len(rows), ddm))
    return rows


if __name__ == '__main__':
    {'features': features, 'test': test}[(sys.argv[1:] or ['test'])[0]]()
