"""lp pick in a MADE-UP WORLD (owner 2026-09-30: dry run on invented data, try to break it, harden it): a temp replay DB of
invented wallets with chosen behaviour, a temp chain.toml, no chain and no network. Every scenario runs the real
lpopt.pick(apply=True) into its world, checks the squad, then runs it again: a second run on the same data must change
nothing. `python -m tests.pickworld` (all scenarios) or `python -m tests.pickworld flood` (one)."""
import os, random, sys, tempfile, time, tomllib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from copydesk import lpdb, lpopt, lpqc, lpfollow as L, lpfind
lpopt.LIVE_REPLAY = False          # invented wallets have no history store: the pick logic on the open/close/% replay

D = 86400.0
NOW = time.time()
SIZE, BOOK, GAS = 80.0, 400.0, 0.06
BASE_TOML = """[chain]
name = "world"

[lp.follow]
leader = %s
bench = %s
filler = %s
size_frac = 0.2
max_open = 5
reserve = 0
size_free_div = 2.0
filler_min_score = 10.0
filler_n = 40
bench_n = 5
pick_swaps = 1
bench_per_wallet = 2
max_per_token = 4
cool_n = 8
cool_h = 24.0
last_slot_frac = 0.5
qc_exclude = %s
%s
[lp.hook]
x = 1
"""


class World:
    def __init__(self, name):
        self.dir = tempfile.mkdtemp(prefix='pickworld-%s-' % name)
        self.var = os.path.join(self.dir, 'var')
        os.makedirs(self.var)
        self.name = 'world'
        lpdb.DB = os.path.join(self.dir, 'lpwallets.db')
        lpopt._DESK_P.clear()
        self.con = lpdb.connect()
        self.rng = random.Random(name)
        self.pools = {}

    def config(self, leader=(), bench=(), filler=(), qc_exclude=(), hours=None, extra=''):
        q = lambda xs: '[%s]' % ', '.join('"%s"' % x for x in xs)
        hrs = '' if not hours else '\n[lp.follow.hours]\n' + ''.join('"%s" = %s\n' % (a, list(h)) for a, h in hours.items())
        open(os.path.join(self.dir, 'chain.toml'), 'w').write(BASE_TOML % (q(leader), q(bench), q(filler), q(qc_exclude), extra + hrs))
        self.reload()

    def reload(self):
        self.cfg = tomllib.load(open(os.path.join(self.dir, 'chain.toml'), 'rb'))

    def wallet(self, a, per_day=20, mean=0.8, sd=1.5, pools=8, trust=1.0, days=None, hours=None, hold_min=20.0, replayed=True):
        """Invent a wallet's replayed copies over 7 days: per_day copies, each P&L ~ N(mean, sd) $ at $25, spread over `pools`
        pools (the first pool takes a share when pools < 1: e.g. 0.9 = 90% of copies in one pool), the leader's own P&L =
        model / trust. days: a per-day multiplier on the mean (oldest first); hours: only those 3-hour UTC blocks."""
        a = a.lower()
        rows = []
        for d in range(7):
            m = mean * (days[d] if days else 1.0)
            for _ in range(int(per_day)):
                t0 = NOW - (7 - d) * D + self.rng.random() * D
                if hours is not None and time.gmtime(t0).tm_hour // 3 * 3 not in hours:
                    continue
                t1 = min(NOW - 60, t0 + self.rng.expovariate(1.0 / (hold_min * 60)))
                if pools < 1:
                    pool = 'p-%s-0' % a[:6] if self.rng.random() < pools else 'p-%s-%d' % (a[:6], self.rng.randrange(1, 9))
                else:
                    pool = 'p-%s-%d' % (a[:6], self.rng.randrange(int(pools)))
                p = self.rng.gauss(m, sd)
                rows.append((a, t0, t1, p, 25.0, NOW, pool, 'tok-' + pool, p, p / trust))
        self.con.executemany('INSERT INTO copy_trades VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
        if replayed:
            self.con.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (lpdb.REPLAY_V + a, str(NOW)))
            c7 = sum(r[3] for r in rows)
            self.con.execute('INSERT OR REPLACE INTO replays(owner, at, days, real_usd, model0_usd, copy2_usd, copy7_usd, keep7, delay_blocks, size_usd) '
                             'VALUES (?,?,?,?,?,?,?,?,?,?)', (a, NOW, 7.0, c7 / trust, c7, c7, c7, 1.0, 33, 25.0))
        self.con.commit()
        return a

    def hour(self, per_day=15, mean=0.8, sd=1.5):
        """An hour of ordinary new copies for every wallet already here (the next hourly repick's new data)."""
        ws = [o for (o,) in self.con.execute('SELECT DISTINCT owner FROM copy_trades')]
        rows = []
        for a in ws:
            for _ in range(max(1, int(per_day / 24))):
                t0 = NOW - 3600 + self.rng.random() * 3000
                p = self.rng.gauss(mean, sd)
                pool = 'p-%s-%d' % (a[:6], self.rng.randrange(8))
                rows.append((a, t0, min(NOW - 30, t0 + 600), p, 25.0, NOW, pool, 'tok-' + pool, p, p))
        self.con.executemany('INSERT INTO copy_trades VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
        self.con.commit()

    def pick(self):
        out = []
        lead, bench, changed = lpopt.pick(self, apply=True, log=out.append, gas=GAS)
        self.reload()
        f = L.cfg(self)
        return dict(lead=L.leaders(self), bench=[b for b in L.bench(self) if b not in {k.lower() for k in (f.get('hours') or {})}],
                    hours={k.lower(): v for k, v in (f.get('hours') or {}).items()}, filler=L.filler(self), changed=changed, log=out)


def stubs():
    """No chain, no network: the live size, our wallets, the take rates, gas and contract reads are fixed."""
    L.live_size = lambda chain: (SIZE, BOOK)
    L.own_wallets = lambda: {OURS}
    lpqc.L.live_size = L.live_size
    lpqc.L.own_wallets = L.own_wallets
    L._batch = lambda calls: ['0x' for _ in calls]
    lpqc.L._batch = L._batch

    def take(chain, min_n=30):
        lpopt.TAKE.clear()
        lpopt.TAKE_ALL = 1.0
        return lpopt.TAKE, lpopt.TAKE_ALL
    lpopt.measure_take = take
    lpfind.measured_gas = lambda chain, hours=48.0: GAS


OURS = '0x' + 'ee' * 20
w = lambda n: '0x%040x' % n                                            # wallet n


def check_squad(r, where):
    """What must hold for ANY squad: no wallet in two tiers, no duplicates, lower case, never ours."""
    tiers = [r['lead'], r['bench'], list(r['hours']), r['filler']]
    flat = [a for t in tiers for a in t]
    assert len(flat) == len(set(flat)), '%s: a wallet in two places: %s' % (where, [a for a in flat if flat.count(a) > 1])
    assert all(a == a.lower() for a in flat), '%s: not lower case' % where
    assert OURS not in flat, '%s: our own wallet picked' % where


def run(name, world, expect, between=None):
    r1 = world.pick()
    check_squad(r1, name + ' run 1')
    expect(r1)
    if between:
        between(world)
    r2 = world.pick()
    check_squad(r2, name + ' run 2')
    t = lambda xs: [a[-2:] for a in xs]
    diff = ['%s %s -> %s' % (k, t(r1[k]) if k != 'hours' else {a[-2:]: h for a, h in r1[k].items()}, t(r2[k]) if k != 'hours' else {a[-2:]: h for a, h in r2[k].items()})
            for k in ('lead', 'bench', 'hours', 'filler') if (sorted(r1[k]) if k != 'hours' else r1[k]) != (sorted(r2[k]) if k != 'hours' else r2[k])]
    assert not r2['changed'], '%s: run 2 changed the squad again: %s' % (name, '; '.join(diff) or 'order only')
    return r1


def s_fresh():
    """An empty squad and 12 good wallets: 2 mains, 5 bench, the rest filler; the best by score lead."""
    W = World('fresh'); W.config()
    for i in range(12):
        W.wallet(w(i + 1), per_day=15, mean=0.6 + 0.15 * i)
    return W, lambda r: (len(r['lead']) == 2 or fail('fresh: %d mains' % len(r['lead'])),
                         len(r['bench']) == 5 or fail('fresh: %d bench' % len(r['bench'])))


def s_main_goes_bad():
    """A main that turned into a loser (last 4 days deep red) leaves, the best other wallet takes its place."""
    W = World('bad')
    A = W.wallet(w(1), per_day=20, mean=1.2, days=[1, 1, 1, -3, -3, -3, -3])
    B = W.wallet(w(2), per_day=15, mean=1.0)
    C = W.wallet(w(3), per_day=15, mean=1.1)
    for i in range(4, 10):
        W.wallet(w(i), per_day=10, mean=0.5)
    W.config(leader=[A, B])
    return W, lambda r: (A not in r['lead'] or fail('bad: the loser is still a main'), B in r['lead'] or fail('bad: a good main was dropped'))


def s_main_fails_qc():
    """A main earning 90% in one pool (the 'one pool' QC flag) leaves, whatever its score."""
    W = World('qc')
    A = W.wallet(w(1), per_day=20, mean=1.5, pools=0.95)
    B = W.wallet(w(2), per_day=15, mean=1.0)
    for i in range(3, 10):
        W.wallet(w(i), per_day=12, mean=0.7)
    W.config(leader=[A, B])
    return W, lambda r: A not in r['lead'] + r['bench'] + list(r['hours']) + r['filler'] or fail('qc: a one-pool wallet is still in the squad')


def s_near_tie():
    """A challenger a hair better than a main: no swap (the 5-of-7-days rule)."""
    W = World('tie')
    A = W.wallet(w(1), per_day=15, mean=1.0)
    B = W.wallet(w(2), per_day=15, mean=1.0)
    C = W.wallet(w(3), per_day=15, mean=1.03)
    for i in range(4, 10):
        W.wallet(w(i), per_day=10, mean=0.5)
    W.config(leader=[A, B])
    return W, lambda r: sorted(r['lead']) == sorted([A, B]) or fail('tie: swapped a main for a near-equal: %s' % r['lead'])


def s_flood():
    """A wallet minting 300 times a day, each a hair positive, tops the score; as a main it would fill every slot."""
    W = World('flood')
    F = W.wallet(w(1), per_day=300, mean=0.12, sd=0.5, pools=40, hold_min=240)
    A = W.wallet(w(2), per_day=15, mean=1.2)
    B = W.wallet(w(3), per_day=15, mean=1.1)
    for i in range(4, 10):
        W.wallet(w(i), per_day=10, mean=0.6)
    W.config()
    return W, lambda r: F not in r['lead'] or fail('flood: the flooding wallet became a main (%s)' % r['log'][-6:])


def s_all_negative():
    """Nobody earns: no crash, nobody picked (copying losers is worse than copying nothing)."""
    W = World('neg')
    for i in range(1, 8):
        W.wallet(w(i), per_day=15, mean=-0.5)
    W.config(leader=[w(1), w(2)], bench=[w(3)])
    return W, lambda r: (not r['lead'] or fail('neg: losers kept as mains: %s' % r['lead']), not r['bench'] or fail('neg: losers on the bench'))


def s_messy_config():
    """A config written by hand: upper case, the same wallet as main AND bench AND filler AND specialist."""
    W = World('messy')
    A = W.wallet(w(1), per_day=15, mean=1.2)
    B = W.wallet(w(2), per_day=15, mean=1.1)
    for i in range(3, 10):
        W.wallet(w(i), per_day=12, mean=0.7)
    up = lambda a: '0x' + a[2:].upper()
    W.config(leader=[up(A), up(B)], bench=[up(A), w(3)], filler=[up(B), w(4)], hours={up(A): [3]})
    return W, lambda r: None


def s_never_replayed_main():
    """A main with no replayed copies at all (new, or its replay failed): it cannot be judged, so it leaves."""
    W = World('unreplayed')
    A = w(1)
    B = W.wallet(w(2), per_day=15, mean=1.0)
    for i in range(3, 10):
        W.wallet(w(i), per_day=12, mean=0.8)
    W.config(leader=[A, B])
    return W, lambda r: A not in r['lead'] or fail('unreplayed: kept a main nobody can score')


def s_excluded():
    """The best two wallets are ours and one the owner turned down: neither is ever picked."""
    W = World('excluded')
    W.wallet(OURS, per_day=20, mean=3.0)
    X = W.wallet(w(1), per_day=20, mean=3.0)
    for i in range(2, 10):
        W.wallet(w(i), per_day=12, mean=0.8)
    W.config(qc_exclude=[X])
    return W, lambda r: X not in r['lead'] + r['bench'] + list(r['hours']) + r['filler'] or fail('excluded: picked a qc_exclude wallet')


def s_bench_all_bad():
    """Three bench wallets go bad at once: all three leave in one run (the swap limit is for good wallets only)."""
    W = World('benchbad')
    A = W.wallet(w(1), per_day=15, mean=1.2)
    B = W.wallet(w(2), per_day=15, mean=1.1)
    bad = [W.wallet(w(i), per_day=12, mean=-0.8) for i in (3, 4, 5)]
    good = [W.wallet(w(i), per_day=12, mean=0.9) for i in range(6, 12)]
    W.config(leader=[A, B], bench=bad + good[:2])
    return W, lambda r: (not set(bad) & set(r['bench']) or fail('benchbad: a losing bench wallet stayed: %s' % r['bench']),
                         len(r['bench']) == 5 or fail('benchbad: bench not refilled: %d' % len(r['bench'])))


def s_few():
    """Only one wallet earns anything: one main, no crash, no empty-list errors."""
    W = World('few')
    A = W.wallet(w(1), per_day=15, mean=1.0)
    W.wallet(w(2), per_day=15, mean=-1.0)
    W.config()
    return W, lambda r: r['lead'] == [A] or fail('few: %s' % r['lead'])


def s_drift():
    """An hour of ordinary new data between two runs: nothing in the mains, bench or specialists may move."""
    W = World('drift'); W.config()
    for i in range(12):
        W.wallet(w(i + 1), per_day=15, mean=0.6 + 0.1 * i)
    return W, lambda r: None, lambda W: W.hour()


def s_clear_upgrade():
    """A challenger at 2x the weaker main (and good for the desk) swaps in once; the next run holds."""
    W = World('upgrade')
    A = W.wallet(w(1), per_day=15, mean=1.2)
    B = W.wallet(w(2), per_day=15, mean=0.5)
    C = W.wallet(w(3), per_day=15, mean=1.4, sd=1.0)
    for i in range(4, 10):
        W.wallet(w(i), per_day=10, mean=0.4)
    W.config(leader=[A, B])
    return W, lambda r: sorted(r['lead']) == sorted([A, C]) or fail('upgrade: no swap to the clearly better wallet: %s' % [a[-2:] for a in r['lead']])


def s_three_mains():
    """mains_n = 3."""
    W = World('three'); W.config(extra='mains_n = 3\n')
    for i in range(10):
        W.wallet(w(i + 1), per_day=12, mean=0.5 + 0.1 * i)
    return W, lambda r: len(r['lead']) == 3 or fail('three: %d mains' % len(r['lead']))


def s_empty():
    """No replayed copies at all: no crash, nobody picked."""
    W = World('empty'); W.config(leader=[w(1)], bench=[w(2)])
    return W, lambda r: (not r['lead'] and not r['bench']) or fail('empty: %s' % r)


def s_vetoed_main():
    """The owner puts a main on qc_exclude: it leaves at once."""
    W = World('veto')
    A = W.wallet(w(1), per_day=15, mean=1.5)
    B = W.wallet(w(2), per_day=15, mean=1.0)
    for i in range(3, 10):
        W.wallet(w(i), per_day=12, mean=0.7)
    W.config(leader=[A, B], qc_exclude=[A])
    return W, lambda r: A not in r['lead'] + r['bench'] + list(r['hours']) + r['filler'] or fail('veto: an excluded main stayed')


def s_stale_specialist():
    """A specialist that stopped earning in its block loses it."""
    W = World('stale')
    A = W.wallet(w(1), per_day=15, mean=1.2)
    B = W.wallet(w(2), per_day=15, mean=1.0)
    for i in range(3, 9):
        W.wallet(w(i), per_day=12, mean=0.7)
    S = W.wallet(w(9), per_day=40, mean=-1.0, hours=[3])
    W.config(leader=[A, B], hours={S: [3]})
    return W, lambda r: S not in r['hours'] or fail('stale: a losing specialist kept its block')


def s_scale():
    """600 wallets: the pick must stay well inside the hourly budget."""
    W = World('scale'); W.config()
    for i in range(600):
        W.wallet(w(i + 1), per_day=8, mean=W.rng.uniform(-0.5, 1.5))
    t = time.time()
    return W, lambda r: (time.time() - t < 120 or fail('scale: %.0f s' % (time.time() - t)))


def fail(m):
    raise AssertionError(m)


SCENARIOS = dict(fresh=s_fresh, bad=s_main_goes_bad, qc=s_main_fails_qc, tie=s_near_tie, flood=s_flood, neg=s_all_negative,
                 messy=s_messy_config, unreplayed=s_never_replayed_main, excluded=s_excluded, benchbad=s_bench_all_bad, few=s_few,
                 drift=s_drift, upgrade=s_clear_upgrade, three=s_three_mains, empty=s_empty, veto=s_vetoed_main, stale=s_stale_specialist,
                 scale=s_scale)


if __name__ == '__main__':
    stubs()
    names = sys.argv[1:] or list(SCENARIOS)
    bad = 0
    for n in names:
        try:
            t0 = time.time()
            got = SCENARIOS[n]()
            r = run(n, *got)
            print('  ok    %-10s mains %s | bench %d | specialists %d | filler %d | %.1f s' % (n, ' + '.join(a[-4:] for a in r['lead']) or '-',
                  len(r['bench']), len(r['hours']), len(r['filler']), time.time() - t0))
        except Exception as e:
            bad += 1
            print('  FAIL  %-10s %s: %s' % (n, type(e).__name__, str(e)[:300]))
    print('pickworld: %d of %d scenarios passed' % (len(names) - bad, len(names)))
    sys.exit(1 if bad else 0)
