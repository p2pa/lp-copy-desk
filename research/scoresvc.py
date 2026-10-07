"""THE POOL-SCORE SERVICE (owner 2026-10-03, Squad Robot page, phase 1, approved with four changes). Research, home only, no desk
code: the desk neither reads nor imports this. Cron every 15 minutes (`tick`).

tick      1. replay the watched wallets that closed a position since the last pass (lpfind.replay_wallets, the nightly's own
             incremental path), unless the nightly or the hourly repick holds the squad lock: then no replay, and the
             snapshot says so (its watermark does not move, so its staleness shows it);
          2. write ONE immutable snapshot: write time, data WATERMARK (the history store's last block time when the last
             complete replay pass started - every position closed before it is in the copies), score version, replay
             seconds, wallets replayed / failed, the hash of the copies it used, and every scored pool's rank, score, decile.
             Written to a temp file, renamed, chmod 0444, its sha256 appended to index.jsonl. Never rewritten.
join      every live copy (follow.jsonl) gets the snapshot with the latest WRITE TIME <= its entry time - asserted per row,
          never the watermark, never a snapshot rebuilt later. Effective staleness = entry time - that snapshot's watermark.
verify    every snapshot's sha256 and mode against the index; exit 1 on any change (the immutability test).
gate      phase 1 gate: each snapshot against research/poolscore.Scores rebuilt NOW from the copies closed before the snapshot's
          watermark - the same timestamp, the backtest's own score - mismatched deciles counted.
report    the page's daily line: achieved staleness p50/p95, live copies joined, top decile - rest for the day, cumulative
          days in the sealed window (from SEALED_FROM), day-level interval.
`python -m research.scoresvc tick|join|verify|gate|report|selftest`."""
import bisect, calendar, collections, hashlib, json, math, os, random, statistics as S, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from research import poolscore as PS                                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, 'chains/robinhood/var/robot/scores')
STATE = os.path.join(ROOT, 'chains/robinhood/var/robot/scoresvc.json')
VERSION = 'squad-copy-v1: copies closed in the last 24 h, ($82 capped +-100% - $0.033) (mean - SE) x copies/day, rank among scored pools'
SEALED_FROM = 1790985600.0                    # 2026-10-03 00:00 UTC: the sealed window (owner, phase 1 change 4)
TOP = 0.9                                     # top decile


def _state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def _watermark_now():
    """(block, unix time) of the history store's last ingested block."""
    from core import chain as cm
    from copydesk import lphist
    R = lphist.Reader(cm.load('robinhood'))
    b = R.span()[1]
    return b, R.tsof(b), R


def replay_pass(log=print):
    """Replay the watched wallets with a close since the last pass. Returns (watermark block, watermark ts, n, failed, secs)
    or None when the squad lock is held (the nightly or the hourly repick is replaying; they write the same copies)."""
    import fcntl
    from copydesk import lpdb, lpfind
    lk = open(os.path.join(ROOT, 'chains/robinhood/var/lp/nightly.lock'), 'w')
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    try:
        t0 = time.time()
        wb, wts, R = _watermark_now()
        st = _state()
        since_b = st.get('watermark_block') or (wb - 36000)
        con = lpdb.connect()
        watched = {o for (o,) in con.execute('SELECT DISTINCT owner FROM copy_trades WHERE t1 >= ?', (time.time() - 8 * 86400,))}
        con.close()
        moved = {o for (o,) in R.con.execute('SELECT DISTINCT n.owner FROM positions p JOIN nfts n ON n.tid = p.tid '
                                             'WHERE p.close_blk > ? AND p.close_blk <= ?', (since_b, wb))}
        # a wallet whose replay failed is retried on every later pass until it succeeds; the watermark still moves (the
        # repick fails 4-5 of ~3,160 wallets every hour - holding the watermark for them would freeze it for good)
        ws = sorted((watched & moved) | set(st.get('retry', [])))
        done = lpfind.replay_wallets(ws, workers=3, log=lambda m: None) if ws else []
        failed = sorted(a for a, _, e in done if e)
        return wb, wts, len(ws), failed, time.time() - t0
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)


def build_snapshot(write_ts, wm_ts, sc=None):
    """Every scored pool's rank / score / decile from the copies closed before the watermark, and the hash of those copies."""
    sc = sc or PS.Scores(1.0)
    h = hashlib.sha256()
    n = 0
    for p in sorted(sc.t):
        k = bisect.bisect_left(sc.t[p], wm_ts)
        for t, v in zip(sc.t[p][:k], sc.v[p][:k]):
            if t >= wm_ts - sc.lb:
                h.update(('%s|%.1f|%.4f;' % (p, t, v)).encode())
                n += 1
    s = {p: sc.score(p, wm_ts) for p in sc.t}
    v = sorted(x for x in s.values() if x is not None)
    ranks = {p: [round(bisect.bisect_right(v, x) / len(v), 5), round(x, 4)] for p, x in s.items() if x is not None}
    return dict(ranks={p: r + [int(r[0] > TOP)] for p, r in ranks.items()}, copies=n, copies_sha256=h.hexdigest(), pools=len(ranks))


def write_snapshot(snap, d=DIR):
    """Immutable: temp file, fsync, rename, chmod 0444, sha256 appended to index.jsonl."""
    os.makedirs(d, exist_ok=True)
    name = '%d.json' % int(snap['write_ts'])
    path = os.path.join(d, name)
    if os.path.exists(path):
        raise RuntimeError('snapshot %s exists: never rewritten' % name)
    raw = json.dumps(snap, sort_keys=True).encode()
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
    os.chmod(path, 0o444)
    with open(os.path.join(d, 'index.jsonl'), 'a') as f:
        f.write(json.dumps(dict(file=name, write_ts=snap['write_ts'], sha256=hashlib.sha256(raw).hexdigest())) + '\n')
    return path


def tick(log=print):
    t0 = time.time()
    st = _state()
    r = replay_pass(log)
    if r:
        wb, wts, n, failed, secs = r
        st.update(watermark_block=wb, watermark_ts=wts, retry=failed)
        replay = dict(wallets=n, failed=len(failed), secs=round(secs, 1), skipped=False)
    else:
        replay = dict(skipped='squad lock held (nightly or repick)')
    wm_ts = st.get('watermark_ts')
    if wm_ts is None:
        log('scoresvc: no watermark yet (first pass failed or skipped); no snapshot')
        json.dump(st, open(STATE, 'w'))
        return None
    write_ts = time.time()
    snap = dict(write_ts=write_ts, watermark_ts=wm_ts, watermark_block=st['watermark_block'], version=VERSION, replay=replay,
                **build_snapshot(write_ts, wm_ts))
    snap['build_secs'] = round(time.time() - write_ts, 1)
    write_snapshot(snap)
    json.dump(st, open(STATE, 'w'))
    log('%s scoresvc: snapshot, watermark %s (%.0f s stale at write), %d pools, %d copies, replay %s, %.0f s total' % (
        time.strftime('%m-%d %H:%M', time.gmtime(write_ts)), time.strftime('%H:%M', time.gmtime(wm_ts)), write_ts - wm_ts,
        snap['pools'], snap['copies'], replay, time.time() - t0))
    return snap


def snapshots(d=DIR):
    """[(write_ts, file)] in write order, from the index."""
    try:
        return sorted((json.loads(l)['write_ts'], json.loads(l)['file']) for l in open(os.path.join(d, 'index.jsonl')))
    except OSError:
        return []


def verify(d=DIR, log=print):
    bad = []
    for l in open(os.path.join(d, 'index.jsonl')):
        e = json.loads(l)
        p = os.path.join(d, e['file'])
        if not os.path.exists(p):
            bad.append((e['file'], 'missing'))
        elif hashlib.sha256(open(p, 'rb').read()).hexdigest() != e['sha256']:
            bad.append((e['file'], 'content changed'))
        elif os.stat(p).st_mode & 0o222:
            bad.append((e['file'], 'writable'))
    log('scoresvc verify: %d snapshots, %d changed%s' % (len(snapshots(d)), len(bad), ': %s' % bad[:5] if bad else ''))
    return not bad


def join(log=print, d=DIR):
    """Live copies with the snapshot that existed at their entry (write time <= entry time, asserted)."""
    idx = snapshots(d)
    wts = [w for w, _ in idx]
    cache, rows = {}, []
    for c in PS.live_copies():
        i = bisect.bisect_right(wts, c['t'])
        if not i:
            continue
        w, f = idx[i - 1]
        assert w <= c['t'], 'a snapshot written after the entry was joined'
        if f not in cache:
            cache[f] = json.load(open(os.path.join(d, f)))
        s = cache[f]
        r = s['ranks'].get(c['pool'].lower())
        rows.append(dict(c, snap=f, write_ts=w, stale=c['t'] - s['watermark_ts'], rank=r[0] if r else None,
                         top=bool(r and r[2]), day=time.strftime('%m-%d', time.gmtime(c['t']))))
    return rows


def gate(log=print, limit=None, d=DIR):
    """Each snapshot against the backtest's own score at its watermark, rebuilt now: mismatched deciles."""
    sc = PS.Scores(1.0)
    tot = mis = 0
    for w, f in snapshots(d)[-limit:] if limit else snapshots(d):
        s = json.load(open(os.path.join(d, f)))
        now = {p: sc.score(p, s['watermark_ts']) for p in sc.t}
        v = sorted(x for x in now.values() if x is not None)
        for p, (rk, _, dec) in s['ranks'].items():
            x = now.get(p)
            d2 = int(x is not None and bisect.bisect_right(v, x) / len(v) > TOP)
            tot += 1
            mis += d2 != dec
    log('scoresvc gate: %d pool ranks in %d snapshots, %d deciles differ from the backtest score at the same watermark' % (tot, len(snapshots(d)), mis))
    return tot, mis


def report(log=print, d=DIR, since=SEALED_FROM):
    """since: the first day counted in the window line (SEALED_FROM live; the backfill rehearsal counts every day)."""
    rows = join(d=d)
    if not rows:
        log('scoresvc report: no live copies joined yet')
        return None
    by = collections.defaultdict(list)
    for r in rows:
        by[r['day']].append(r)
    diffs = []
    for day in sorted(by):
        g = by[day]
        st = sorted(r['stale'] / 60 for r in g)
        top = [r['pct'] for r in g if r['top']]
        rest = [r['pct'] for r in g if not r['top']]
        df = S.mean(top) - S.mean(rest) if len(top) >= 3 and len(rest) >= 3 else None
        sealed = calendar.timegm(time.strptime('2026-' + day, '%Y-%m-%d')) >= since
        if df is not None and sealed:
            diffs.append(df)
        log('  %s%s  copies %3d  staleness p50 %4.0f min p95 %4.0f  top %3d %+6.2f  rest %3d %+6.2f  top - rest %s' % (
            day, ' sealed' if sealed else '       ', len(g), st[len(st) // 2], st[int(len(st) * 0.95)], len(top), S.mean(top) if top else 0,
            len(rest), S.mean(rest) if rest else 0, '%+.2f' % df if df is not None else 'n/a'))
    if len(diffs) >= 2:
        rng = random.Random(3)
        bs = sorted(S.mean(rng.choice(diffs) for _ in diffs) for _ in range(2000))
        log('  sealed window: %d days, top - rest %+.2f [%+.2f, %+.2f], %d/%d days positive' % (
            len(diffs), S.mean(diffs), bs[49], bs[1949], sum(1 for x in diffs if x > 0), len(diffs)))
    return rows


BACKFILL = os.path.join(ROOT, 'chains/robinhood/var/robot/scores_backfill')


def backfill(step_min=15, log=print, d=BACKFILL):
    """THE DRESS REHEARSAL (owner 2026-10-03: "can't we backfill so we can continue?"): snapshots every `step_min` from the
    first scorable day to now, built TODAY from today's copies, into their own folder. Not evidence - these are the days the
    score and the interval were chosen on, with today's replays and watch list - but the same report / robot code must
    reproduce phase 0 from them (live split +4.43, robot +6.17 at 15 min) before it judges the sealed days."""
    sc = PS.Scores(1.0)
    have = {w for w, _ in snapshots(d)}
    t = (sc.first + PS.DAY) // (step_min * 60) * (step_min * 60)
    n, t0 = 0, time.time()
    while t < time.time() - 60:
        if t not in have:
            snap = dict(write_ts=t, watermark_ts=t, watermark_block=None, version=VERSION + ' [BACKFILL, not evidence]',
                        replay=dict(skipped='backfill'), **build_snapshot(t, t, sc))
            write_snapshot(snap, d)
            n += 1
        t += step_min * 60
    log('scoresvc backfill: %d snapshots written to %s (%.0f s)' % (n, d, time.time() - t0))


class SnapshotScores:
    """poolscore.Scores read from a snapshot folder: score(p, ts) is the pool's score in the snapshot with the latest write
    time <= ts. For research/robot.build (the robot's paper positions judged on exactly what the service published)."""

    def __init__(self, lookback=1.0, fresh=False, d=DIR):
        self.d, self.idx = d, snapshots(d)
        self.w = [w for w, _ in self.idx]
        # the same anchor research/robot.build's hourly grid takes from poolscore.Scores (its first replayed copy), so a
        # snapshot run and a recomputed run enter at the same hours and compare entry by entry
        con = __import__('copydesk.lpdb', fromlist=['connect']).connect()
        self.first = con.execute('SELECT MIN(t1) FROM copy_trades WHERE pnl IS NOT NULL AND t1 IS NOT NULL AND pool IS NOT NULL').fetchone()[0]
        con.close()
        self._cache = {}
        pools = set()
        for _, f in self.idx:                                         # every snapshot: a pool scored in one is ranked in it
            pools.update(json.load(open(os.path.join(d, f)))['ranks'])
        self.t = {p: [] for p in pools}                              # robot.build iterates the pools; n24 reads 0

    def _load(self, f):
        if f not in self._cache:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[f] = json.load(open(os.path.join(self.d, f)))
        return self._cache[f]

    def score(self, p, ts):
        i = bisect.bisect_right(self.w, ts)
        if not i:
            return None
        r = self._load(self.idx[i - 1][1])['ranks'].get(p.lower())
        return r[1] if r else None

    def rank(self, p, ts):
        i = bisect.bisect_right(self.w, ts)
        if not i:
            return None
        s = self._load(self.idx[i - 1][1])
        r = s['ranks'].get(p.lower())
        return (r[0], r[1], s['pools']) if r else None


def _selftest():
    d = tempfile.mkdtemp()
    p = write_snapshot(dict(write_ts=100.0, watermark_ts=90.0, ranks={'0xa': [0.95, 1.0, 1]}), d)
    assert verify(d, log=lambda m: None), 'a fresh snapshot verifies'
    try:
        write_snapshot(dict(write_ts=100.0, watermark_ts=95.0, ranks={}), d)
        raise AssertionError('a snapshot was rewritten')
    except RuntimeError:
        pass
    os.chmod(p, 0o644)
    with open(p, 'a') as f:
        f.write(' ')
    assert not verify(d, log=lambda m: None), 'a modified snapshot fails verify'
    write_snapshot(dict(write_ts=200.0, watermark_ts=150.0, ranks={}), d)
    idx = snapshots(d)
    assert [w for w, _ in idx] == [100.0, 200.0] and bisect.bisect_right([w for w, _ in idx], 199.0) == 1, 'join takes write time <= entry'
    print('scoresvc selftest: 4 asserts')


if __name__ == '__main__':
    a = sys.argv[1:] or ['selftest']
    {'tick': tick, 'join': lambda: print(len(join()), 'copies joined'), 'verify': lambda: sys.exit(0 if verify() else 1),
     'gate': gate, 'report': report, 'selftest': _selftest, 'backfill': backfill,
     'rehearse': lambda: report(d=BACKFILL, since=0)}[a[0]]()
