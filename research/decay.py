"""HOW FRESH DOES THE POOL SCORE HAVE TO BE? (owner 2026-10-03, Squad Robot page, build path phase 0: "rerun the frozen rule with
the score refreshed every 1, 3, 6, 12 and 24 h ... same for the 633-copy split ... this is the freshness requirement as a
number"; plus the contradiction test: the persistence result was day-stale and positive, the nightly split on live copies
is not - is that the population or a defective score?)

For each refresh interval R (hours; 0 = continuous, 24 = the 03:00 nightly the desk has today), with the score at time t
built only from copies closed before the last refresh at or before t:
  robot     the frozen rule (commit 017d912: bid 65%, a mint in the last hour, tp +8% or 24 h), engine bias-corrected:
            % per position, days positive, the fill-state split at exit, the top-5-pool share
  live      our live copies, top decile at entry vs the rest: % per copy, and the DAY-LEVEL difference (mean over days of
            top minus rest, a bootstrap interval over days, days positive)
  universe  every replayed copy of every watched wallet (lpdb.copy_trades), ranked at its open: the same split. Live copies
            are already squad picks (82% sit in the top decile); the universe is what research/persist.py measured.
`python -m research.decay [R ...]`."""
import collections, json, math, os, random, statistics as S, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research import poolscore as PS, robot as RB                          # noqa: E402

RULE = ((0, 0, 9, 1), 'bid65', ('tp', 8, 24))
GRID = [0, 1, 3, 6, 12, 24]


def day_diff(rows, log_name, log):
    """rows: (day, is_top, pct). Day-level: per day mean(top) - mean(rest), then mean, bootstrap 95% over days, days > 0."""
    by = collections.defaultdict(lambda: ([], []))
    for d, top, x in rows:
        by[d][0 if top else 1].append(x)
    diffs = [S.mean(a) - S.mean(b) for a, b in by.values() if len(a) >= 3 and len(b) >= 3]
    top = [x for _, t, x in rows if t]
    rest = [x for _, t, x in rows if not t]
    if len(diffs) < 2 or len(top) < 2 or len(rest) < 2:
        log('    %-9s too few' % log_name)
        return None
    rng = random.Random(11)
    bs = sorted(S.mean(rng.choice(diffs) for _ in diffs) for _ in range(2000))
    out = dict(n_top=len(top), top=S.mean(top), n_rest=len(rest), rest=S.mean(rest), diff=S.mean(diffs), lo=bs[49], hi=bs[1949],
               days=len(diffs), pos=sum(1 for x in diffs if x > 0))
    log('    %-9s top %4d %+6.2f | rest %5d %+6.2f | day-level diff %+6.2f [%+.2f, %+.2f], %d/%d days' % (
        log_name, out['n_top'], out['top'], out['n_rest'], out['rest'], out['diff'], out['lo'], out['hi'], out['pos'], out['days']))
    return out


SCORER = PS.Scores                           # research/payscore.PayScores for score (b): `python -m research.decay pay R ...`
TAG = ''


def robot(R, log):
    path = os.path.join(RB.OUT, 'cand%s_r%g.jsonl' % (TAG, R))
    if not os.path.exists(path):
        RB.build(15, log=lambda m: None, fresh=R, path=path, scorer=SCORER)
    cs = sorted(RB.load(path), key=lambda c: c['ts'])
    RB.CORRECT = True
    sh, tpl, cr = RULE
    busy, rows, fills = {}, [], collections.defaultdict(list)
    for c in cs:
        if tpl not in c['r'] or not RB.shoot(c, sh) or busy.get(c['pool'], 0) > c['ts']:
            continue
        x, h = RB.close_at(c['r'][tpl], c['rp'], cr)
        f = c['fl'][tpl][h - 1]
        y = RB.corrected(x, f)
        busy[c['pool']] = c['ts'] + 3600 * h
        rows.append((c['day'], c['pool'], y))
        fills['filled' if f > 0.5 else ('part' if f > 0 else 'unfilled')].append(y)
    if len(rows) < 3:
        log('    robot     too few')
        return None
    xs = [y for _, _, y in rows]
    byd = collections.defaultdict(float)
    byp = collections.defaultdict(float)
    for d, p, y in rows:
        byd[d] += y
        byp[p] += y
    top5 = sorted(byp, key=lambda p: -byp[p])[:5]
    rest = [y for _, p, y in rows if p not in top5]
    out = dict(n=len(xs), mean=S.mean(xs), se=S.stdev(xs) / math.sqrt(len(xs)), pos=sum(1 for v in byd.values() if v > 0), days=len(byd),
               fills={k: (len(v), S.mean(v)) for k, v in fills.items()}, wo_top5=S.mean(rest) if rest else None,
               top5_share=sum(byp[p] for p in top5) / sum(xs) if sum(xs) else None)
    log('    robot     n %3d %+6.2f%% (se %.2f) %d/%d days | at exit %s | without top 5 pools %+.2f%%' % (
        out['n'], out['mean'], out['se'], out['pos'], out['days'],
        ' '.join('%s %d %+.1f' % (k, *fills_) for k, fills_ in sorted(out['fills'].items())), out['wo_top5'] or 0))
    return out


def live(R, log):
    sc = SCORER(1.0, fresh=R)
    rows = []
    for c in PS.live_copies():
        r = sc.rank(c['pool'], c['t'])
        rows.append((int(c['t'] // PS.DAY), r is not None and r[0] > 0.9, c['pct']))
    return day_diff(rows, 'live', log)


def universe(R, log, sample=40000):
    from copydesk import lpdb
    sc = SCORER(1.0, fresh=R)
    con = lpdb.connect()
    cut = sc.first + 2 * PS.DAY
    q = con.execute('SELECT pool, t0, pnl, size_usd FROM copy_trades WHERE pnl IS NOT NULL AND pool IS NOT NULL AND t0 > ?', (cut,)).fetchall()
    con.close()
    rng = random.Random(5)
    q = q if len(q) <= sample else rng.sample(q, sample)
    rows, cache = [], {}
    for p, t0, pnl, sz in q:
        k = (p.lower(), int(t0 // 3600))
        if k not in cache:
            r = sc.rank(p, t0)
            cache[k] = r[0] if r else None
        rk = cache[k]
        rows.append((int(t0 // PS.DAY), rk is not None and rk > 0.9, max(-100.0, min(100.0, 100.0 * pnl / (sz or 25.0)))))
    return day_diff(rows, 'universe', log)


def run(grid=GRID, log=print):
    out = {}
    for R in grid:
        t0 = time.time()
        log('REFRESH every %s' % ('continuous' if R == 0 else '%g h' % R))
        out[R] = dict(robot=robot(R, log), live=live(R, log), universe=universe(R, log))
        log('    (%.0f s)' % (time.time() - t0))
    json.dump(out, open(os.path.join(RB.OUT, 'decay%s.json' % TAG), 'w'), default=str)
    return out


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == 'pay':
        from research import payscore
        SCORER, TAG, a = payscore.PayScores, '_pay', a[1:]
    run([float(x) for x in a] or GRID)
