"""WHO SHOULD THE COPY DESK FOLLOW, AND WHEN? (`hookbot <chain> lp optimize`, docs/LPWALLETS.md)

1 candidates: the top `n` wallets of the screen's v2 board (copydesk/lpfind.py), refreshed first (warm, our node).
2 options: every set of 1..k leaders; per leader two variants - all hours, or only the 3-hour UTC blocks in which its
  copies earned in the TRAINING half (a leader busy at the wrong hours is copied when it is good, not always).
3 each option is simulated at OUR size and capital: every leader position in the window, first come first served
  inside max_deploy_usd, size_usd a copy, the leader's own realized % x the copy lag factor, minus our gas a cycle.
4 walk-forward: the options are ranked on the first half of the window and judged on the second, which the ranking
  never saw; an option that only shines in-sample falls. objective = test return on capital - 0.5 x test max
  drawdown / capital; ties -> more green days.
5 every run is stored (lpdb 'optimize'); --notify pages only when the recommendation differs from the desk's leaders.
It never changes the desk: switching leaders stays the owner's call ([lp.follow] leader + a restart).
"""
import collections, itertools, json, os, time

BLOCK_H = 3
CAP_PCT = 100.0             # one copy counts at most +-100% of its size (2026-09-30: 0xa518's week was ONE +$2,645 copy of a
                            # pumping launch token; sums let a single lottery ticket outrank steady copies in pick, bench, knobs)
COPY_GAS = 0.19             # $ per copy cycle (mint + burn + sell), MEASURED 2026-09-30: lp-1 paid $6.50 for 110 txs in 9 h
LAG = 0.9                   # a 2-7 s copy keeps ~87-92% of the leader (the two replays of 2026-09-28)


TAKE = {}                   # wallet -> the share of its replayed copies the LIVE desk actually takes (lpdb 'take_rates');
TAKE_ALL = 0.62             # ... and all wallets together, for one without enough history (measured 2026-09-30: 113 of 183)
COPIED = set()              # leaders priced from their replayed copies (lpdb.copy_trades): already at our delay, keep 1


def _positions(con, owner, t0, t1):
    """(open, close, invested, pct) per position. A REPLAYED leader: its copies at our size and delay (pct = copy $ / size),
    marked in COPIED so no keep rate is applied twice; otherwise the leader's own positions (times its keep rate later)."""
    # a replayed wallet is priced from its copies ONLY, even in a window they do not cover (then it has none there): mixing
    # its own positions into one half and copies into the other judged two halves by two different measures
    if con.execute('SELECT 1 FROM copy_trades WHERE owner = ? LIMIT 1', (owner,)).fetchone():
        COPIED.add(owner)
        # capped, and weighted by how well the replay reproduces the leader (lpdb.copy_score's trust): a 2x-hot replay
        # counts half, one that matches counts in full
        real, m0 = con.execute('SELECT SUM(real), SUM(pnl0) FROM copy_trades WHERE owner = ? AND real IS NOT NULL', (owner,)).fetchone()
        tr = min(m0 / real, real / m0) if real and m0 and real > 0 and m0 > 0 else 0.0
        # THE TAKE RATE: live, the desk takes only part of a wallet's replayed copies (leader positions under $20, cooled
        # tokens, a price crossing the range before our mint, reverts): each copy is kept or dropped deterministically at
        # the wallet's measured rate, so the simulator deploys what the desk really deploys
        import zlib
        take = TAKE.get(owner, TAKE_ALL)
        keep = lambda t: (zlib.crc32(('%s%.0f' % (owner, t)).encode()) % 10000) / 10000.0 < take
        return [(a, b, z, max(-CAP_PCT, min(CAP_PCT, pct)) * (tr if pct > 0 else 1.0)) for a, b, z, pct in con.execute(
            'SELECT t0, t1, size_usd, 100.0 * pnl / size_usd FROM copy_trades WHERE owner = ? AND t0 >= ? AND t1 <= ?', (owner, t0, t1))
            if keep(a)]
    return [r for r in con.execute(
        'SELECT open_ts, close_ts, invested_usd, realized_pct FROM positions WHERE owner = ? AND closed = 1 AND skip IS NULL '
        'AND realized_pct IS NOT NULL AND invested_usd >= 20 AND open_ts >= ? AND close_ts <= ?', (owner, t0, t1))]


def _positions_t(con, owner, t0, t1):
    """_positions with the token as a 5th field (the token rules need it), same take rate, cap and trust."""
    pos = _positions(con, owner, t0, t1)
    tok = dict(con.execute('SELECT t0, token FROM copy_trades WHERE owner = ? AND t0 >= ? AND t1 <= ?', (owner, t0, t1)).fetchall())
    return [(a, b, z, pct, tok.get(a)) for a, b, z, pct in pos]


def _blocks(pos, size, gas):
    """3-hour UTC blocks in which copies of these positions earned (mean copy P&L > 0, at least 3 copies)."""
    by = collections.defaultdict(list)
    for o, c, inv, pct in pos:
        by[int(time.gmtime(o).tm_hour // BLOCK_H)].append(size * pct / 100 * LAG - gas)
    return frozenset(b for b, v in by.items() if len(v) >= 3 and sum(v) > 0)


KEEP = {}                   # leader -> its measured copy keep rate (lpdb replays); set by optimize()


def simulate(streams, size, cap, gas, t0, t1, bucket=86400):
    """streams: [(leader, positions, allowed blocks or None)]. Our desk over [t0, t1): P&L, copies, skipped, daily nets."""
    ev = sorted((o, c, pct, a) for a, pos, blocks in streams for o, c, inv, pct in pos
                if blocks is None or int(time.gmtime(o).tm_hour // BLOCK_H) in blocks)
    open_, pnl, n, skip, day = [], 0.0, 0, 0, collections.Counter()
    for o, c, pct, a in ev:
        open_ = [x for x in open_ if x > o]
        if (len(open_) + 1) * size > cap:
            skip += 1
            continue
        r = size * pct / 100 * (1.0 if a in COPIED else KEEP.get(a, LAG)) - gas
        pnl += r
        n += 1
        open_.append(c)
        day[int((c - t0) // bucket)] += r
    nd = max(1, int(round((t1 - t0) / bucket)))
    net = [day.get(i, 0.0) for i in range(nd)]
    cum = peak = dd = 0.0
    for x in net:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dict(pnl=pnl, copies=n, skipped=skip, green=sum(x > 0 for x in net), days=nd, worst=min(net), max_dd=dd, net=net,
                obj=pnl / cap + 0.5 * dd / cap)


def simulate_bench(mains, bench, size, cap, gas, t0, t1, reserve=1):
    """The live desk's priority rule (lpfollow): MAIN copies always go in - when the book is full they close bench copies
    (booked at the share of their P&L earned by then, minus the early exit's gas) - and a BENCH copy opens only while one
    more main copy would still fit beside it. mains / bench: [(leader, positions)]. Returns our P&L over [t0, t1)."""
    ev = sorted([(o, c, pct, a, 0) for a, pos in mains for o, c, inv, pct in pos] +
                [(o, c, pct, a, 1) for a, pos in bench for o, c, inv, pct in pos])
    slots = int(cap // size)
    open_, pnl = [], 0.0                                              # open_: [close_ts, booked P&L, is_bench, open_ts]
    for o, c, pct, a, is_b in ev:
        for x in [x for x in open_ if x[0] <= o]:
            pnl += x[1]
            open_.remove(x)
        r = size * pct / 100 * (1.0 if a in COPIED else KEEP.get(a, LAG)) - gas
        if is_b:
            if len(open_) + 1 + reserve > slots:
                continue                                              # room is kept for a main copy
        else:
            while len(open_) + 1 > slots and any(x[2] for x in open_):
                v = next(x for x in open_ if x[2])
                open_.remove(v)
                frac = max(0.0, min(1.0, (o - v[3]) / (v[0] - v[3]))) if v[0] > v[3] else 0.0
                pnl += (v[1] + gas) * frac - gas                      # closed early: what it had earned so far, minus gas
            if len(open_) + 1 > slots:
                continue
        open_.append([c, r, is_b, o])
    return pnl + sum(x[1] for x in open_ if x[0] <= t1)


def bench_value(mains, cands, days=7.0, size=25.0, cap=125.0, gas=COPY_GAS):
    """candidate -> what it ADDS, in $/day, as bench beside `mains` under our capital (simulate_bench): only the capital
    the main leaders leave idle earns, so this values activity in exactly the hours they are quiet."""
    from copydesk import lpdb
    con = lpdb.connect()
    t1 = time.time()
    t0 = t1 - days * 86400
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    m = [(a, _positions(con, a, t0, t1)) for a in mains]
    base = simulate_bench(m, [], size, cap, gas, t0, t1)
    out = {a: (simulate_bench(m, [(a, _positions(con, a, t0, t1))], size, cap, gas, t0, t1) - base) / days for a in cands}
    con.close()
    return out


def measure_take(chain, min_n=30):
    """wallet -> live take rate: the desk's closed copies of it over the replayed copies it had while that wallet was
    watched (follow.jsonl start events say who was watched when). Needs min_n replayed copies, else the all-wallet rate.
    Sets TAKE / TAKE_ALL and returns them."""
    import bisect, json, os
    from copydesk import lpdb
    from copydesk.lpfollow import _state_path, _log_path
    st = json.load(open(_state_path(chain)))
    starts = sorted((e['ts'], set((e.get('leader') or '').split(',')) | set(x for x in (e.get('bench') or '').split(',') if x))
                    for e in (json.loads(l) for l in open(_log_path(chain)) if l.strip()) if e.get('ev') == 'start')
    st_ts = [s[0] for s in starts]
    watched = lambda a, t: (lambda i: i >= 0 and a in starts[i][1])(bisect.bisect_right(st_ts, t) - 1)
    live = collections.Counter(o['leader'].lower() for o in st['ours'].values() if o.get('leader'))
    first = min((o['entry_ts'] for o in st['ours'].values() if o.get('entry_ts')), default=time.time())
    con = lpdb.connect()
    rep = collections.Counter()
    for a in live:
        rep[a] = sum(1 for (t,) in con.execute('SELECT t0 FROM copy_trades WHERE owner = ? AND t0 >= ?', (a, first)) if watched(a, t))
    con.close()
    tot_r, tot_l = sum(rep.values()), sum(min(live[a], rep[a]) for a in rep)
    TAKE_ALL_ = tot_l / tot_r if tot_r else TAKE_ALL
    TAKE.clear()
    TAKE.update({a: min(1.0, live[a] / rep[a]) for a in rep if rep[a] >= min_n})
    globals()['TAKE_ALL'] = TAKE_ALL_
    return dict(TAKE), TAKE_ALL_


def specialists(act, bsc, taken, per=1):
    """(quiet blocks, {wallet: [block start hours]}). act: the squad's copies per 3-hour UTC block; a block under the median
    is quiet and gets the `per` best wallets by their score in that block (bsc: lpdb.copy_score_blocks), positive only,
    none of `taken`. One rule for lp pick and lp tune."""
    med = sorted(act.get(h, 0) for h in range(0, 24, 3))[4]
    quiet = [h for h in range(0, 24, 3) if act.get(h, 0) < med]
    hours = {}
    for h in quiet:
        for o, v in sorted(((o, v[h]) for o, v in bsc.items() if h in v and v[h] > 0 and o not in taken), key=lambda x: -x[1])[:per]:
            hours.setdefault(o, []).append(h)
    return quiet, hours


def bench_pick(mains, cands, n=2, days=7.0, recent=3.0, size=25.0, cap=125.0, gas=COPY_GAS, keep=(), end=None):
    """THE BENCH SCORE (owner 2026-09-30: score what we DO with the bench): the bench fills capital the main leaders leave
    idle, never blocks them and is closed early when they need room - so a bench is scored as the $/day it ADDS beside
    the mains under exactly that rule (simulate_bench), and chosen as a SET: the first wallet adds the most, the second
    the most given the first (two wallets busy in the same idle hours compete for the same slots). Recent days weigh
    more: 60% the last `recent` days, 40% the whole window. Returns [(wallet, $/day it adds)] best first."""
    from copydesk import lpdb
    con = lpdb.connect()
    t1 = end or time.time()                                           # end: pick on a past window (walk-forward tests)
    wins = [(t1 - recent * 86400, recent, 0.6), (t1 - days * 86400, days, 0.4)]
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    pos = {a: {w[0]: _positions(con, a, w[0], t1) for w in wins} for a in set(mains) | set(cands) | set(keep)}
    con.close()

    def value(bench):
        return sum(wt * (simulate_bench([(a, pos[a][t0]) for a in mains], [(b, pos[b][t0]) for b in bench], size, cap, gas, t0, t1)
                         - simulate_bench([(a, pos[a][t0]) for a in mains], [], size, cap, gas, t0, t1)) / d for t0, d, wt in wins)
    chosen, out = list(keep), []                                      # keep: bench wallets that stay, the rest is added beside them
    base = value(chosen) if chosen else 0.0
    for _ in range(n - len(chosen)):
        best = max(((c, value(chosen + [c]) - base) for c in cands if c not in chosen), key=lambda x: x[1], default=None)
        if not best or best[1] <= 0:
            break
        chosen.append(best[0])
        out.append(best)
        base += best[1]
    return out


def optimize(chain=None, n=12, k=3, days=14.0, size=25.0, cap=125.0, gas=COPY_GAS, refresh=True, current=(), log=print, replay=0, pool=None):
    from copydesk import lpdb, lpfind
    rows, _ = lpfind.board(days)
    pool = list(pool) if pool is not None else [r['address'] for r in rows if r.get('score2') is not None]
    for a in current:
        if a not in pool:
            pool.append(a)                                              # the leaders we follow are always in the running
    con = lpdb.connect()
    t1 = time.time()
    t0 = t1 - days * 86400
    mid = (t0 + t1) / 2
    # NO LEAKAGE: the candidates are chosen on the TRAINING half only (each one's own copy result there); the board's
    # score covers the whole window, test half included, so picking its top n would leak the answer into the test
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    solo = {a: simulate([(a, _positions(con, a, t0, mid), None)], size, cap, gas, t0, mid)['obj'] for a in pool}
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())                                      # each leader's own copy keep rate, measured
    if replay:
        # the top `replay` candidates whose copy was never replayed get one now (our node, ~5-20 min each)
        from copydesk import lpfollow
        for a in [a for a in sorted(pool, key=lambda a: -solo[a]) if a not in KEEP][:replay]:
            log('replaying %s ...' % a)
            try:
                lpfollow.grid(a, 7.0, log=lambda m: None)
            except Exception as e:
                log('  replay failed: %s' % str(e)[:120])
        KEEP.clear()
        KEEP.update(lpdb.keep_rates())
    cands = sorted(pool, key=lambda a: -solo[a])[:n]
    for a in current:
        if a not in cands:
            cands.append(a)
    con.close()
    if refresh and cands:
        log('refreshing %d candidates (warm, our node)...' % len(cands))
        lpdb.refresh(cands, days, log=lambda m: None)
    con = lpdb.connect()
    train = {a: _positions(con, a, t0, mid) for a in cands}
    test = {a: _positions(con, a, mid, t1) for a in cands}
    blocks = {a: _blocks(train[a], size, gas) for a in cands}
    blocks = {a: (b if len(b) < 24 // BLOCK_H else frozenset()) for a, b in blocks.items()}   # every block = all hours: no variant
    opts = []
    for kk in range(1, k + 1):
        for combo in itertools.combinations(cands, kk):
            for mask in itertools.product((False, True), repeat=kk):   # per leader: all hours / its good blocks only
                if any(m and not blocks[a] for a, m in zip(combo, mask)):
                    continue
                sched = [blocks[a] if m else None for a, m in zip(combo, mask)]
                tr = simulate([(a, train[a], s) for a, s in zip(combo, sched)], size, cap, gas, t0, mid)
                opts.append(dict(leaders=list(combo), blocks=[sorted(s) if s else None for s in sched], train=tr))
    opts.sort(key=lambda o: (-o['train']['obj'], -o['train']['green']))
    # judged out of sample: the ranking never saw these days. The 40 best in training, plus every option whose leaders
    # are all replayed (the only ones that can be recommended, wherever they rank in training)
    top = opts[:40] + [o for o in opts[40:] if all(a in KEEP for a in o['leaders'])]
    for o in top:
        o['test'] = simulate([(a, test[a], set(b) if b else None) for a, b in zip(o['leaders'], o['blocks'])], size, cap, gas, mid, t1)
    top.sort(key=lambda o: (-o['test']['obj'], -o['test']['green']))
    cur = None
    if current:
        cur = dict(leaders=list(current), blocks=[None] * len(current),
                   train=simulate([(a, train.get(a, []), None) for a in current], size, cap, gas, t0, mid),
                   test=simulate([(a, test.get(a, []), None) for a in current], size, cap, gas, mid, t1))
    # a leader whose copy was never replayed is priced at the default keep rate and can NOT be recommended (0x1017f961
    # made $1,990 and kept 14% copied 7 s late: our desk would have lost $27 a week) - it is listed as needing a replay
    bad = set(lpdb.unreproducible())
    for o in top:
        o['unreplayed'] = [a for a in o['leaders'] if a not in KEEP]
    best = next((o for o in top if not o['unreplayed'] and o['train']['pnl'] > 0 and o['test']['pnl'] > 0), None)
    need = []
    for o in top:
        for a in o['unreplayed']:
            if a not in need and a not in bad:
                need.append(a)
    res = dict(at=time.time(), days=days, size=size, cap=cap, candidates=cands, options=len(opts), best=best, current=cur, top=top[:10],
               keep={a: KEEP[a] for a in cands if a in KEEP}, needs_replay=need[:5], unreproducible=[a for a in cands if a in bad])
    con.execute('CREATE TABLE IF NOT EXISTS optimize(at REAL PRIMARY KEY, j TEXT)')
    con.execute('INSERT OR REPLACE INTO optimize VALUES (?,?)', (res['at'], json.dumps(res)))
    con.commit()
    con.close()
    return res


def fmt_opt(o, nm=lambda a: a[:6] + '..' + a[-4:]):
    lead = ' + '.join('%s%s' % (nm(a), '' if not b else ' [%s UTC]' % ','.join('%02d-%02d' % (x * BLOCK_H, x * BLOCK_H + BLOCK_H) for x in b))
                      for a, b in zip(o['leaders'], o['blocks']))
    f = lambda r: '$%+7.2f %3d copies %2d/%d green worst day $%+.2f dd $%.2f' % (r['pnl'], r['copies'], r['green'], r['days'], r['worst'], -r['max_dd'])
    return lead, f(o['train']), f(o['test'])


def lines(res):
    out = ['%d candidates, %d options; ranked on the first %g days, judged on the last %g ($%.0f copies, $%.0f capital; each leader at its measured copy keep rate, %.0f%% where not replayed yet)' % (
        len(res['candidates']), res['options'], res['days'] / 2, res['days'] / 2, res['size'], res['cap'], 100 * LAG)]
    if res['best']:
        l, tr, te = fmt_opt(res['best'])
        out += ['RECOMMENDED  %s' % l, '   first half %s' % tr, '   last half  %s  <- out of sample' % te]
    if res['current']:
        l, tr, te = fmt_opt(res['current'])
        out += ['CURRENT      %s' % l, '   first half %s' % tr, '   last half  %s' % te]
    out.append('copy keep rates (measured): ' + ', '.join('%s %.0f%%' % (a[:6], 100 * k) for a, k in res.get('keep', {}).items()))
    if res.get('unreproducible'):
        out.append('the replay cannot reproduce (never recommended): ' + ', '.join(a[:10] for a in res['unreproducible']))
    if res.get('needs_replay'):
        out.append('NEEDS A REPLAY before it can be recommended (lp copy <addr>): ' + ', '.join(res['needs_replay']))
    out.append('top options (out of sample; * = contains an unreplayed leader):')
    for o in res['top']:
        l, tr, te = fmt_opt(o)
        out.append('  %s%s\n      test %s   | train %s' % ('* ' if o.get('unreplayed') else '', l, te, tr))
    return out


def desk_7d(mains, bench, filler, size, book, gas, cfg, days=None, shake=None, by_wallet=None):
    # bench: the bench wallets; the specialists among them ([lp.follow.hours]) are copied only in their 3-hour UTC blocks.
    # shake = seed: the same replay with a fixed random 20% of every wallet's copies left out (clear_swap)
    """The whole desk replayed over the last `days` with these mains, bench and filler and the live rules: $ per day."""
    from copydesk import lpdb
    days = days or float(cfg.get('pick_days', 7.0))                  # the pick's window ([lp.follow] pick_days)
    if LIVE_REPLAY:
        # EVERY LIVE RULE (owner 2026-10-04): copies with their fee paths through the entry gates, make-room and the drawdown
        # pause (copydesk/lplive). 10 days of the old squad replayed +$196; the live desk booked +$188 in pools >= 2 h
        from copydesk import lplive
        from core import chain as cm
        return lplive.desk(cm.load('robinhood'), list(mains), list(bench), list(filler), size, book, gas, cfg, days, shake=shake, by_wallet=by_wallet)
    con = lpdb.connect()
    now, D = time.time(), 86400.0
    t0 = now - days * D
    P = _DESK_P.setdefault(round(t0 / 3600), {})
    pos = lambda a: P.setdefault(a, _positions_t(con, a, t0, now))
    hrs = {k.lower(): set(v) for k, v in (cfg.get('hours') or {}).items()}
    import zlib
    kept = (lambda a, x: True) if shake is None else (lambda a, x: zlib.crc32(('%s%.0f%d' % (a, x[0], shake)).encode()) % 100 >= 20)
    pos2 = lambda a: [x for x in pos(a) if kept(a, x)]
    only = lambda a: [x for x in pos2(a) if a not in hrs or time.gmtime(x[0]).tm_hour // 3 * 3 in hrs[a]]
    T = [[(a, pos2(a)) for a in mains], [(a, only(a)) for a in bench if a not in mains],
         [(a, pos2(a)) for a in filler if a not in mains and a not in bench]]
    led = []
    simulate_tiers(T, size, book, gas, t0, now, reserve=int(cfg.get('reserve', 0)), div=float(cfg.get('size_free_div', 0) or 0),
                   slots=int(cfg.get('max_open', 5)), per_token=int(cfg.get('max_per_token', 0) or 0), cool_n=int(cfg.get('cool_n', 8)),
                   cool_h=float(cfg.get('cool_h', 24)), per_wallet={1: int(cfg.get('bench_per_wallet', 0) or 0)},
                   min_frac=float(cfg.get('last_slot_frac', 1) or 1), ledger=led, by_wallet=by_wallet)
    con.close()
    return [sum(v for ts, v in led if now - (k + 1) * D <= ts < now - k * D) for k in range(int(days) - 1, -1, -1)]


_DESK_P = {}
_LAST = {}                          # the last pick's live-rules values (tests read them)
LIVE_REPLAY = True                  # desk_7d = copydesk/lplive (every live rule); False = the old open/close/% replay


def clear_gain(new, old):
    """A change clearly pays: more in total AND on at least two days in three (5 of 7): a 4-of-7 lead is a coin flip, and
    hourly re-picks would trade one for the next."""
    d = [x - y for x, y in zip(new, old)]
    return sum(d) > 0 and sum(1 for x in d if x > 0) >= 2 * len(d) / 3


def pick_mains(chain, cur, ok, sc, bench, filler, size, book, gas, cfg, n_cand=12):
    """(mains, log lines): see pick. Candidates = the top n_cand clean wallets by copy score."""
    n = int(cfg.get('mains_n', 2))
    sw = int(cfg.get('pick_swaps', 0) or 0) or n
    cands = [o for o in sorted(ok, key=lambda o: -sc[o][0]) if o not in cur][:n_cand]
    nb = len(bench)

    def desk(m, bw=None):
        """The desk with mains m: a wallet that leaves the mains goes to the bench (in the place of the weakest), a promoted
        one leaves it - the real alternative, not the wallet vanishing (pickworld 2026-09-30: dropping a +1.1 main outright
        for a +0.7 one looked +$237 better because the dropped main stopped competing for slots)."""
        b_ = [a for a in bench if a not in m]
        b_ = ([a for a in cur if a not in m and a in ok] + sorted(b_, key=lambda a: -sc.get(a, (0,))[0]))[:nb]
        return desk_7d(m, b_, filler, size, book, gas, cfg, by_wallet=bw)

    def holds(new, old, n=6):
        """A swap that is real survives shaking the data: in n replays each missing a different 20% of the copies it still
        wins in at least n-1 (pickworld 2026-09-30: the best of 24 swaps on one replay cleared 5-of-7 days by luck, and
        the next run swapped back)."""
        b_ = lambda m: ([a for a in cur if a not in m and a in ok] + sorted([a for a in bench if a not in m], key=lambda a: -sc.get(a, (0,))[0]))[:nb]
        return sum(1 for k in range(n) if sum(desk_7d(new, b_(new), filler, size, book, gas, cfg, shake=k)) >
                   sum(desk_7d(old, b_(old), filler, size, book, gas, cfg, shake=k))) >= n - 1
    lead, out = [a for a in cur if a in ok], []
    for a in cur:
        if a not in ok:
            out.append('%s leaves: QC or no edge' % a[:6])
    while len(lead) < n and cands:                                    # an empty place: the candidate the desk gains most from
        best = max(cands, key=lambda o: sum(desk(lead + [o])))
        out.append('%s fills an empty place (desk %+.0f over %d d)' % (best[:6], sum(desk(lead + [best])), len(desk(lead + [best]))))
        lead.append(best)
        cands.remove(best)
    base = desk(lead)
    out.append('desk with mains %s: %+.0f over %d d (%s)' % (' + '.join(a[:6] for a in lead), sum(base), len(base), ' '.join('%+.0f' % x for x in base)))
    for _ in range(sw):
        # ONE BAR FOR EVERY TIER: a newcomer scores 1.5x the main it replaces, AND the whole desk replayed agrees (clearly,
        # and on shaken data). The desk replay alone is chaotic - in pickworld it traded a +1.2 main for a +0.7 one and a
        # main for a 3%-better twin; it is the veto that keeps a slot-flooding wallet out, not the judge of who is better
        # THE 1.5x BAR, AS A MAIN (2026-10-04): the newcomer's own booked $ in the desk with it as a main is at least 1.5x what
        # the main it replaces books in today's desk - both measured as mains in the same replay (the copy score compared a
        # bench-only value with a main's share; without any bar, pickworld churned 7 of 18 worlds)
        bw0 = {}
        desk(lead, bw0)
        tries = []
        for m in lead:
            for o in cands:
                bw = {}
                r = desk([o if x == m else x for x in lead], bw)
                if bw.get(o, 0.0) > 0 and bw.get(o, 0.0) >= 1.5 * max(bw0.get(m, 0.0), 0.0):
                    tries.append((sum(r) - sum(base), m, o, r))
        if not tries:
            break
        tries.sort(key=lambda z: -z[0])
        for g_, m_, o_, r_ in tries[:5]:
            out.append('  try %s -> %s: %+5.0f  ahead %d/%d days  %s' % (m_[:6], o_[:6], g_, sum(1 for x, y in zip(r_, base) if x > y), len(r_),
                                                                     ' '.join('%+.0f' % (x - y) for x, y in zip(r_, base))))
        g, m, o, r = tries[0]
        new_ = [o if x == m else x for x in lead]
        if not clear_gain(r, base) or not holds(new_, lead):
            out.append('best swap %s -> %s: %+.0f over %d d, ahead %d of %d days - %s, kept' % (
                m[:6], o[:6], g, len(r), sum(1 for x, y in zip(r, base) if x > y), len(r), 'not clear' if not clear_gain(r, base) else 'does not survive shaking the data'))
            break
        out.append('swap %s -> %s: desk %+.0f over %d d, ahead %d of %d days' % (m[:6], o[:6], g, len(r), sum(1 for x, y in zip(r, base) if x > y), len(r)))
        lead = [o if x == m else x for x in lead]
        cands = [x for x in cands if x != o] + [m]
        base = r
    return lead, out


def pick(chain, apply=False, n_bench=2, log=print, gas=None):
    """Who the copy desk follows (owner 2026-09-29): MAIN leaders = the optimizer's best out-of-sample set of replayed wallets;
    BENCH = the best replayed copy $/day at our size among the rest (lpfind.bench_board), copied while the main leaders
    are quiet. apply=True rewrites [lp.follow] leader / bench in chain.toml; the running desk restarts itself on it.
    Returns (leaders, bench, changed)."""
    import re
    from copydesk import lpfind, lpfollow as L
    cur_l, cur_b = L.leaders(chain), L.bench(chain)
    cfg = L.cfg(chain)
    # 7 days = the replays' window, both halves priced from replayed copies; no refresh (replay-all keeps the store current)
    from copydesk import lpdb
    # THE POOL: every replayed wallet whose copy score (lpdb.copy_score: capped copies after gas, minus a standard error,
    # times replay trust) is positive - a wallet we expect to lose money copying is not a candidate, nothing else is cut
    if gas is None:                                                  # OUR measured gas per copy, whoever calls the pick
        try:
            from copydesk.lpfind import measured_gas
            gas = measured_gas(chain) or COPY_GAS
        except Exception:
            gas = COPY_GAS
    measure_take(chain)                                              # the desk replay takes what the desk really takes
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    size, book = L.live_size(chain)                                  # the copy size the desk uses now, not a fixed $25
    sc = lpdb.copy_score(float(cfg.get('pick_days', 7.0)), size=size, gas=gas)
    from copydesk.lpfollow import own_wallets
    # never ourselves, never a wallet quality control turned down ([lp.follow] qc_exclude, `lp qc`: the owner's call)
    qcx = {x.lower() for x in (cfg.get('qc_exclude') or [])}
    # QUALITY CONTROL IS PART OF THE PICK (owner 2026-09-30): every candidate for any tier - positive 7-day copy score, or
    # positive in a 3-hour block (the specialists) - that fails a hard flag (lpqc.HARD) is never picked
    from copydesk import lpqc
    bsc = lpdb.copy_score_blocks(float(cfg.get('pick_days', 7.0)), size=size, gas=gas)
    cand = ({o for o, (v, _) in sc.items() if v > 0} | {o for o, v in bsc.items() if any(x > 0 for x in v.values())}) - own_wallets() - qcx
    down = lpqc.turned_down(chain, cand)
    log('quality control: %d of %d candidates turned down (%s)' % (len(down), len(cand), ', '.join(
        '%s %d' % (n, sum(1 for f in down.values() if n in f)) for n in lpqc.HARD if any(n in f for f in down.values())) or '-'))
    for a in [a for a in cur_l + cur_b if a in down]:
        log('   %s leaves: %s' % (a[:10], ', '.join(down[a])))
    qcx |= set(down)
    ok = {o for o, (v, _) in sc.items() if v > 0} - own_wallets() - qcx
    # THE MAINS (owner 2026-09-30): judged by what they do for THE WHOLE DESK - the last 7 days replayed with the live rules
    # and the current bench and filler (desk_7d) - not by their score alone: a wallet minting 240 times a day tops the score
    # and, as a main, fills every slot (0x87ef + 0x7777: +$46 over 7 days vs +$400 now). A main that lost its edge or fails
    # QC leaves and its place goes to the candidate that makes the desk the most; then at most pick_swaps mains are swapped
    # for a candidate, and only when the desk makes more on most days (clear_gain)
    # MAIN CANDIDATES BY WHAT THE DESK MAKES COPYING THEM (owner 2026-10-04): each clean wallet replayed as the only bench beside
    # the current mains through every live rule, the mains by their own share of that desk - not by the raw copy score, which
    # ranked 0x12c2 out (+$90.9 of the old squad's +$196 in the live replay) and kept 0xf56e in
    pd0 = float(cfg.get('pick_days', 7.0))
    m0 = [a for a in cur_l if a in ok]
    mv = {}
    desk_7d(m0, [], [], size, book, gas, cfg, days=pd0, by_wallet=mv)
    sl = {a: (mv.get(a, 0.0) / pd0, sc[a][1]) for a in m0}
    for a in sorted(ok - set(m0)):
        bw_ = {}
        desk_7d(m0, [a], [], size, book, gas, cfg, days=pd0, by_wallet=bw_)
        sl[a] = (bw_.get(a, 0.0) / pd0, sc[a][1])
    lead, mlog = pick_mains(chain, cur_l, ok, sl, [b for b in cur_b if b not in cur_l], L.filler(chain), size, book, gas, cfg)
    untrusted = [a for a in cur_l if a not in ok]
    # THE BENCH AND THE FILLER ARE JUDGED ON THE COPIES THE DESK WOULD TAKE (owner 2026-10-04: "replay should just look at rules
    # that exist on live and apply them"): each candidate replayed as the only bench wallet beside the mains, through the live
    # rules (desk_7d: slots, sizing, bench_per_wallet, token cap and cool-off, take rate) - its own booked $ a day. The raw copy
    # score judged a wallet on every copy it makes: 0x4b2d's 503 replayed copies at +0.39% scored $1.81/day and it left the
    # bench, while the 68 the desk took earned +0.73% (+$45.56); 0xf56e scored $9.87 on 43 copies held for days, and the desk's
    # 14 copies of it lost $57.43. ponytail: one candidate at a time, so two bench wallets crowding each other is not priced
    pd_ = float(cfg.get('pick_days', 7.0))
    lv = {}
    for a in sorted(ok - set(lead)):
        bw_ = {}
        desk_7d(lead, [a], [], size, book, gas, cfg, days=pd_, by_wallet=bw_)
        lv[a] = (bw_.get(a, 0.0) / pd_, sc[a][1])
    _LAST.update(lv=lv, lead=list(lead))
    log('live-rules value: %d candidates beside the mains, top %s' % (len(lv), ', '.join(
        '%s %+.1f (score %+.1f)' % (a[:6], v, sc[a][0]) for a, (v, _) in sorted(lv.items(), key=lambda kv: -kv[1][0])[:8])))
    # the bench must ADD money beside the main leaders, not only make money alone
    # the bench: scored for its job (bench_pick) among wallets we expect to earn copying (positive copy score), replayed lately
    cands = [o for o in lpdb.replay_ev(3) if o in ok and o not in lead]
    if int(cfg.get('bench_n', n_bench)) >= 10:
        cands = [o for o in ok if o not in lead]                       # the wide bench: every positive wallet, fresh or not
    # bench_n (owner 2026-09-30, idle capital): 48 h at 20% deployed, the 5 slots full 1% of the time - the limit is copyable
    # positions, not capital; a bench wallet only uses slots the mains leave free and is added only if it adds money
    nb = int(cfg.get('bench_n', n_bench))
    # TOP N BY COPY SCORE (owner 2026-09-30: no idle capital, but none wasted on losers either). Five one-day folds, each
    # picked blind on the days before it: top 5 mean +$134.57/day, worst day -$5.56; every wider bench lost $98-144 on
    # the bad day (score >= $3: mean +$141.69, worst -$138.72; top 10: +$76.59, -$127.76). Greedy selection by what
    # each adds did not beat the plain ranking, so the ranking it is.
    if True:
        # WIDE BENCH (owner 2026-09-30, idle capital): a few hand-picked wallets are bursty - at 08:00 UTC the 9-wallet squad
        # minted 9 times in 2 h while 28 positively scored wallets minted 299. The bench is every wallet we expect to earn
        # copying (copy score > 0 after gas), best first; it only ever uses slots the mains leave free (Follow.room)
        bp = [(a, v) for a, v in sorted(((a, lv[a][0]) for a in cands), key=lambda x: -x[1]) if v > 0][:nb]
    else:
        bp = bench_pick(lead, cands, n=nb, size=size, cap=book, gas=gas)
    bench = [a for a, _ in bp]
    # A LIVING SQUAD, CHANGED SLIGHTLY (owner 2026-09-30, lp tune squad, walk forward): re-picked every 6 h, at most
    # pick_swaps bench wallets swapped a run - the weakest kept one for the best newcomer; a wallet that lost its edge or
    # fails QC always leaves and an empty place is always filled. Re-picking every 1-6 h beat the nightly pick on 3 and 5
    # days; a limit of 1 swap cost nothing
    sw = int(cfg.get('pick_swaps', 0) or 0)
    if sw:
        cur_hours = {k.lower() for k in (cfg.get('hours') or {})}
        prev = [a for a in cur_b if a in ok and a not in lead and a not in cur_hours and lv[a][0] > 0]
        # empty places are filled; a held place changes only for a newcomer scoring 1.5x the wallet it replaces
        # (2026-09-30: 0x12c2 +28.2 and 0x0708 +27.4 swapped places every hour)
        ins = [a for a in bench if a not in prev]                    # best first
        outs = sorted((a for a in prev if a not in bench), key=lambda a: lv[a][0])
        vac = max(0, nb - len(prev))
        keep_, new = list(prev), ins[:vac]
        for o_, i_ in list(zip(outs, ins[vac:]))[:sw]:
            if lv[i_][0] < 1.5 * lv[o_][0]:
                break
            keep_.remove(o_)
            new.append(i_)
        bench = (keep_ + new)[:nb]
        bv = dict(bp)
        bp = [(a, bv.get(a, lv[a][0])) for a in bench]
    # QUIET-HOUR SPECIALISTS (owner 2026-09-30: no 8-hour silences, without paying for it in quality): the 3-hour UTC
    # blocks where the squad (mains + bench) copies less than its median get ONE more wallet each - the best one BY ITS
    # SCORE IN THAT BLOCK (lpdb.copy_score_blocks, positive only), copied only in that block ([lp.follow.hours]). Five
    # blind one-day folds: top 5 + 1 specialist per quiet block mean +$143.91/day, worst day +$5.93, vs top 5 alone
    # +$136.06 / -$5.56; 2 or 3 specialists a block dropped the worst day to about -$62.
    hours = {}
    per = int(cfg.get('specialists_per_block', 1))
    if per > 0:
        d = float(cfg.get('pick_days', 7.0))
        con = lpdb.connect()
        act = collections.Counter(time.gmtime(t).tm_hour // 3 * 3 for a in lead + bench for (t,) in con.execute(
            'SELECT t0 FROM copy_trades WHERE owner = ? AND t0 >= ?', (a, time.time() - d * 86400)))
        con.close()
        taken = set(lead) | set(bench) | own_wallets() | qcx
        quiet, hours = specialists(act, bsc, taken, per)
        # A SPECIALIST KEEPS ITS BLOCK while it still earns there and is clean; at most pick_swaps blocks change a run, the
        # biggest gain first (hourly re-picks moved the 03:00 block three times on 2026-09-30)
        sw_ = int(cfg.get('pick_swaps', 0) or 0)
        if sw_ and per == 1:
            now_b = {h: o for o, hs in hours.items() for h in hs}
            was = {h: o.lower() for o, hs in (cfg.get('hours') or {}).items() for h in hs}
            sc_b = lambda o, h: bsc.get(o, {}).get(h, 0.0)
            # a held block stays held while its specialist earns there, quiet or not: the quiet blocks move with the squad
            # (pickworld: one main swapped, the quiet blocks shifted, and every specialist changed)
            keep_b = {h: o for h, o in was.items() if o not in taken and sc_b(o, h) > 0}
            # a challenger takes a held block only at 1.5x the holder's score there (2026-09-30: +6.2 vs +5.7 was churn)
            moves = sorted((h for h in set(quiet) | set(keep_b) if now_b.get(h) and now_b[h] != keep_b.get(h)
                            and (h not in keep_b or sc_b(now_b[h], h) >= 1.5 * sc_b(keep_b[h], h))),
                           key=lambda h: -(sc_b(now_b[h], h) - (sc_b(keep_b[h], h) if h in keep_b else 0.0)))
            final = dict(keep_b)
            for h in moves:
                if h not in keep_b or sw_ > 0:
                    if h in keep_b:
                        sw_ -= 1
                    final[h] = now_b[h]
            for h in sorted(final):
                if was.get(h) and was[h] != final[h]:
                    log('   specialist @%02d: %s -> %s (%+.1f vs %+.1f $/day in the block)' % (h, was[h][:6], final[h][:6], sc_b(final[h], h), sc_b(was[h], h)))
            hours = {}
            for h, o in sorted(final.items()):
                hours.setdefault(o, []).append(h)
        bench += [o for o in hours if o not in bench]
        log('quiet blocks %s UTC: specialists %s' % (','.join('%02d' % h for h in quiet) or '-',
            ', '.join('%s @%s' % (o[:6], ','.join('%02d' % h for h in sorted(v))) for o, v in hours.items()) or '-'))
    # THE FILLER (owner 2026-09-30, idle capital): every other wallet scoring filler_min_score or more at the live size, best
    # first; copied only into slots nobody above needs and closed first when they do (Follow.room). Off without the key.
    fill, fmin = [], cfg.get('filler_min_score')
    if fmin is not None:
        used = set(lead) | set(bench) | own_wallets() | qcx
        fill = [o for o, (v, _) in sorted(lv.items(), key=lambda kv: -kv[1][0]) if v >= float(fmin) and o not in used][:int(cfg.get('filler_n', 40))]
        log('filler: %d wallets making >= $%g/day under the live rules at $%.0f a copy%s' % (len(fill), float(fmin), size, '' if fill != L.filler(chain) else ' (unchanged)'))
    changed = sorted(lead) != sorted(cur_l) or bench != cur_b or fill != L.filler(chain)
    log('main leaders: %s%s' % (', '.join(a[:10] for a in lead), '' if sorted(lead) != sorted(cur_l) else ' (unchanged)'))
    for l in mlog:
        log('   ' + l)
    log('bench: %d wallets%s: %s' % (len(bp), '' if bench != cur_b else ' (unchanged)', ', '.join('%s %+.1f' % (a[:6], v) for a, v in bp[:8]) + (' ...' if len(bp) > 8 else '')))
    cur_h = {k.lower(): sorted(v) for k, v in (cfg.get('hours') or {}).items()}
    changed = changed or cur_h != {k: sorted(v) for k, v in hours.items()}
    # A BROKEN INPUT NEVER EMPTIES THE SQUAD (2026-10-01 08:59: replay v10's 11-character mark broke lpqc's fixed substr, every
    # wallet read 'not replayed', and the repick wrote no main, no bench, no filler; the desk stopped copying). The tell: wallets
    # leaving while their copy score is still POSITIVE - a squad that really lost its edge leaves on negative scores (pickworld
    # 'neg', 'empty'). Over half of the mains + bench leaving that way: refused, the squad kept
    gone = [a for a in cur_l + cur_b if a not in lead + bench and sc.get(a, (0.0,))[0] > 0]
    if apply and changed and len(gone) > (len(cur_l) + len(cur_b)) / 2:
        log('REFUSED: this pick would drop %d of %d mains + bench that still score above zero - kept the squad (a broken input, not a pick)'
            % (len(gone), len(cur_l) + len(cur_b)))
        return cur_l, cur_b, False
    if apply and changed:
        import os
        p = chain.dir + '/chain.toml'
        open(p + '.tmp', 'w').write(write_follow(open(p).read(), lead, bench, hours, fill if fmin is not None else None))
        os.replace(p + '.tmp', p)
        log('applied to %s' % p)
    return lead, bench, changed


def write_follow(s, lead, bench, hours, filler=None):
    """chain.toml text -> the same text with [lp.follow] leader / bench and the [lp.follow.hours] table rewritten. The
    section and its hours table are one span: it ends at the first table header that is neither, and a header inside a
    comment does not count (2026-09-30: matching '[lp.follow.hours]' in a comment broke a commit's TOML)."""
    import re
    i = re.search(r'(?m)^\[lp\.follow\]', s).start()
    j = next((m.start() for m in re.finditer(r'(?m)^\[', s) if m.start() > i and not s.startswith('[lp.follow.hours]', m.start())), len(s))
    sec = s[i:j]
    q = lambda xs: '[' + ', '.join('"%s"' % x for x in xs) + ']'
    sec = re.sub(r'(?m)^leader = .*$', 'leader = %s   # lp pick %s' % (q(lead), time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())), sec)
    sec = re.sub(r'(?m)^bench = .*$', 'bench = %s' % q(bench), sec)
    if filler is not None:
        if re.search(r'(?m)^filler = .*$', sec):
            sec = re.sub(r'(?m)^filler = .*$', 'filler = %s' % q(filler), sec)
        else:
            sec = re.sub(r'(?m)^(bench = .*)$', lambda m: m.group(1) + '\nfiller = %s' % q(filler), sec, count=1)
    sec = re.sub(r'(?ms)^\[lp\.follow\.hours\]\n(?:"0x[0-9a-f]+" = \[[^\]]*\]\n)*', '', sec).rstrip('\n') + '\n'
    if hours:
        sec += '\n[lp.follow.hours]\n' + ''.join('"%s" = [%s]\n' % (o, ', '.join(str(h) for h in sorted(v))) for o, v in hours.items())
    return s[:i] + sec + '\n' + s[j:]


def simulate_desk(mains, bench, t0, t1, slots=5, size=25.0, gas=COPY_GAS, per_token=2, cool_n=8, cool_h=24.0):
    """The live copy desk over [t0, t1) from replayed copies (lpdb.copy_trades rows (t0, t1, pnl at $25, token)), every
    rule it runs (lpfollow): bench under the priority rule (Follow.room), at most per_token copies of a token open, a
    token whose last >= cool_n closed copies inside cool_h net below zero is skipped. Returns P&L, copies, skips by rule."""
    ev = sorted([(o, c, p, tok, 0) for a in mains for o, c, p, tok in a] + [(o, c, p, tok, 1) for a in bench for o, c, p, tok in a])
    open_, closed, pnl, n = [], [], 0.0, 0
    skip = collections.Counter()
    for o, c, p, tok, is_b in ev:
        if o < t0 or c > t1:
            continue
        for x in [x for x in open_ if x[0] <= o]:
            pnl += x[1]
            closed.append((x[0], x[2], x[1]))
            open_.remove(x)
        if per_token and sum(1 for x in open_ if x[2] == tok) >= per_token:
            skip['token cap'] += 1
            continue
        rec = [r for cc, t, r in closed if t == tok and o - cc < cool_h * 3600]
        if cool_n and len(rec) >= cool_n and sum(rec) < 0:
            skip['cool-off'] += 1
            continue
        r = max(-CAP_PCT / 100, min(CAP_PCT / 100, p / 25.0)) * size - gas      # capped like _positions
        if is_b:
            if len(open_) + 2 > slots:
                skip['bench room'] += 1
                continue
        else:
            while len(open_) + 1 > slots and any(x[3] for x in open_):
                v = next(x for x in open_ if x[3])
                open_.remove(v)
                pnl -= gas
                skip['bench closed'] += 1
            if len(open_) + 1 > slots:
                skip['capital'] += 1
                continue
        open_.append([c, r, tok, is_b])
        n += 1
    pnl += sum(x[1] for x in open_ if x[0] <= t1)
    return dict(pnl=pnl, copies=n, skips=dict(skip))


def knobs(mains, bench, days=7.0, log=print, gas=COPY_GAS, size=25.0, live=(2, 8, 24.0)):
    """Backtest the token rules on replayed copies: every (per_token, cool_n, cool_h), chosen on the first half, judged
    on the second (walk-forward), plus each knob's full-window result with the others at their best."""
    from copydesk import lpdb
    con = lpdb.connect()
    t1 = time.time()
    t0 = t1 - days * 86400
    mid = (t0 + t1) / 2
    get = lambda a: con.execute('SELECT t0, t1, pnl, token FROM copy_trades WHERE owner = ? AND token IS NOT NULL', (a,)).fetchall()
    M, B = [get(a) for a in mains], [get(a) for a in bench]
    con.close()
    log('copies stored: mains %s, bench %s' % ([len(x) for x in M], [len(x) for x in B]))
    grid = [(pt, cn, ch) for pt in (1, 2, 3, 0) for cn in (4, 6, 8, 12, 0) for ch in (6.0, 12.0, 24.0, 48.0) if cn or ch == 24.0]
    grid += [live] if live not in grid else []                         # the live setting is always judged (it was (4, 8, 24): nan)
    sim = lambda k, a, b: simulate_desk(M, B, a, b, size=size, per_token=k[0], cool_n=k[1], cool_h=k[2], gas=gas)
    rows = [(k, sim(k, t0, mid), sim(k, mid, t1), sim(k, t0, t1)) for k in grid]
    best = max(rows, key=lambda r: r[1]['pnl'])
    off = next(r for r in rows if r[0] == (0, 0, 24.0))
    live_k, live = live, next((r for r in rows if r[0] == live), None)
    name = lambda k: 'cap %s, cool %s' % (k[0] or 'off', '%d in %gh' % (k[1], k[2]) if k[1] else 'off')
    log('%-26s %10s %10s %10s  %s' % ('setting', 'train $', 'test $', 'week $', 'skips over the week'))
    for lab, r in (('BEST ON TRAIN: ' + name(best[0]), best), ('no rules', off), ('live now: ' + name(live_k), live)):
        log('%-40s %+9.2f %+9.2f %+9.2f  %s' % (lab, r[1]['pnl'], r[2]['pnl'], r[3]['pnl'], r[3]['skips']))
    log('each knob over the week (others free, best of them):')
    for i, nm in ((0, 'per_token'), (1, 'cool_n'), (2, 'cool_h')):
        vals = sorted({r[0][i] for r in rows})
        log('  %-9s %s' % (nm, '  '.join('%s: %+.2f' % (v or 'off', max(r[3]['pnl'] for r in rows if r[0][i] == v)) for v in vals)))
    return rows


def _selftest():
    """write_follow on a small config: hours added, removed, re-added; always valid TOML with one hours table, the rest
    untouched, and a '[lp.follow.hours]' inside a comment ignored."""
    import tomllib
    s = ('[a]\nx = 1\n\n[lp.follow]\n# (no [lp.follow.hours] yet)\nleader = []\nbench = []\nsize_frac = 0.2\n\n[lp.hook]\ny = 2\n')
    L, B = ['0x' + '1' * 40], ['0x' + '2' * 40, '0x' + '3' * 40]
    a = write_follow(s, L, B, {B[1]: [3, 6]}, ['0x' + 'f' * 40])
    b = write_follow(a, L, B[:1], {})
    c = write_follow(b, L, B, {B[1]: [9]})
    for t, h in ((a, {B[1]: [3, 6]}), (b, None), (c, {B[1]: [9]})):
        d = tomllib.loads(t)
        assert d['lp']['follow'].get('hours') == h, t
        assert t.count('\n[lp.follow.hours]') == (1 if h else 0)
        assert d['a'] == {'x': 1} and d['lp']['hook'] == {'y': 2} and d['lp']['follow']['size_frac'] == 0.2
        assert d['lp']['follow']['filler'] == ['0x' + 'f' * 40], 'the filler line is written once and kept'
        assert t.count('\nfiller = ') == 1
    print('lpopt selftest OK: write_follow keeps one hours table and valid TOML')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())


def _at(path, t):
    """A copy's (P&L %, fees %) of what went in at time t, from its path (ts, pnl %, fee %, trust)."""
    import bisect
    i = bisect.bisect_right(path[0], t) - 1
    return (path[1][i], path[2][i]) if i >= 0 else (0.0, 0.0)


def fee_rate(path, t, w):
    """CAPITAL AT WORK: fees earned over the trailing w seconds, in % of what went in per hour."""
    return (_at(path, t)[1] - _at(path, t - w)[1]) * 3600.0 / w


def simulate_tiers(tiers, size, cap, gas, t0, t1, reserve=1, div=0.0, slots=None, per_token=0, cool_n=0, cool_h=24.0, per_wallet=None,
                   min_frac=1.0, match=0.0, room=None, ledger=None, wroom=None, dd=None, cool_min=0.0, room_floor=None, tok_frac=None, promote=None,
                   by_wallet=None):
    """promote = idle seconds (owner 2026-10-02, "0x87ef inactive for 2 days: how is that capital efficient"): a wallet of
    tier k acts as tier k-1 while that many wallets of tier k-1 have opened nothing for `promote` seconds - the n-th ranked
    wallet of a tier steps up for the n-th idle wallet above it (bench for an idle main, filler for an idle bench wallet),
    and steps back the moment the upper wallet mints again. Ranks are the tier lists' order (the pick's own ranking).
    THE LIVE ROOM RULE (lpfollow.Follow.room) over replayed copies, three tiers: tiers = [mains, bench, filler], each
    [(wallet, positions)]. A lower tier opens only with `reserve` slots and their base size free; a copy that does not fit
    closes lower-tier copies (lowest tier, newest first; booked at the share of their P&L earned by then, minus the early
    exit's gas). div: a copy takes max(base, free/div). Each copy's return per dollar falls 0.1% per dollar over $25
    (fee dilution, measured 2026-09-30: 0.77-0.92x at $143 vs $25 on 5 wallets). THE TOKEN RULES (Follow.token_block), when
    positions carry a 5th field, the token: at most per_token copies of one token open (0 = off), and a token whose last
    >= cool_n closed copies inside cool_h hours net below zero is skipped. Positions with a 7th field, their value path
    (ts, pnl %, fee %, trust), are booked at their real value when closed early. room = (w seconds, r %/h): a copy that does
    not fit first closes the open copy (any tier) earning the least, if it earned <= r %/h over the last w and is w old.
    dd = (threshold $, mode, pause s, min losers): THE DRAWDOWN STOP - realized below its high by the threshold: 'exit' closes
    every copy, 'pause' keeps them; either way no new copy for the pause, then the high resets. min losers > 1: only when
    that many copies closed red since the high (a systemic loss, not one blow-up).
    wroom = (w seconds, r %/h): a wallet at its per_wallet limit first closes its own copy earning the least, if <= r %/h
    over the last w and w old, so its new copy can open.
    ledger: a list that gets (ts, $) for every booking, so one run over several days can be split by the day each copy closed.
    by_wallet: a dict that gets wallet -> $ booked on its copies (what the desk made copying it, under these rules).
    Returns (P&L, capital deployed 0-1)."""
    ev = sorted((x[0], x[1], x[3], a, k, x[4] if len(x) > 4 else None, x[5] if len(x) > 5 else None, x[6] if len(x) > 6 else None)
                for k, tier in enumerate(tiers) for a, pos in tier for x in pos)
    closed = []                                                      # (close ts, token, booked P&L): the cool-off's memory
    import bisect as _b
    acts = {a: sorted(x[0] for x in pos) for tier in tiers for a, pos in tier}
    ranks = [[a for a, _ in tier] for tier in tiers]

    def eff(k, a, o):
        if not promote or k == 0:
            return k
        up = ranks[k - 1]
        idle = sum(1 for u in up if (lambda v: not v or o - v[-1] > promote)(acts[u][:_b.bisect_left(acts[u], o)]))
        return k - 1 if idle and a in ranks[k][:idle] else k
    slots = slots or int(cap // size + 1e-9)
    open_, pnl, busy = [], 0.0, [0.0]                                # open_: [close, r, tier, open, size]

    def settle(x, until):
        busy[0] += max(0.0, min(until, t1) - max(x[3], t0)) * x[4]

    def early(v, o):
        """What an open copy books when we close it at o: its value then (path), else its share of the final P&L."""
        r = _early(v, o)
        if ledger is not None:
            ledger.append((o, r))
        if by_wallet is not None:
            by_wallet[v[6]] = by_wallet.get(v[6], 0.0) + r
        return r

    def _early(v, o):
        if v[7]:
            p = max(-CAP_PCT, min(CAP_PCT, _at(v[7], o)[0]))
            return v[8] * p * (v[7][3] if p > 0 else 1.0) - gas
        frac = max(0.0, min(1.0, (o - v[3]) / (v[0] - v[3]))) if v[0] > v[3] else 0.0
        return (v[1] + gas) * frac - gas
    st_ = dict(peak=0.0, until=0.0, losers=0)
    for o, cl, pct, a, k, tok, lead, path in ev:
        k = eff(k, a, o)
        for x in [x for x in open_ if x[0] <= o]:
            if x not in open_:
                continue                                              # closed early by a drawdown stop's exit-all in this same pass
            pnl += x[1] if x[0] <= t1 else 0.0
            if dd:
                st_['losers'] += x[1] < 0
                st_['peak'] = max(st_['peak'], pnl)
                if pnl < st_['peak'] - dd[0] and st_['losers'] >= dd[3] and o >= st_['until']:
                    if dd[1] == 'exit':
                        for v in list(open_):
                            if v is not x:
                                open_.remove(v)
                                pnl += early(v, x[0])
                                settle(v, x[0])
                    st_.update(until=x[0] + dd[2], peak=pnl, losers=0)
            if ledger is not None and x[0] <= t1:
                ledger.append((x[0], x[1]))
            if by_wallet is not None and x[0] <= t1:
                by_wallet[x[6]] = by_wallet.get(x[6], 0.0) + x[1]
            closed.append((x[0], x[5], x[1]))
            settle(x, x[0])
            open_.remove(x)
        if tok and per_token and sum(1 for x in open_ if x[5] == tok) >= per_token:
            low = [x for x in open_ if x[5] == tok and x[2] > k]        # the cap respects the tiers, as the desk does
            if not low:
                continue
            v = max(low, key=lambda x: (x[2], x[3]))
            open_.remove(v)
            pnl += early(v, o)
            settle(v, o)
        if dd and o < st_['until']:
            continue                                                  # stopped / paused: no new copy
        if per_wallet and per_wallet.get(k) and sum(1 for x in open_ if x[2] == k and x[6] == a) >= per_wallet[k]:
            mine = [(fee_rate(x[7], o, wroom[0]), x) for x in open_ if wroom and x[2] == k and x[6] == a and x[7] and o - x[3] >= wroom[0]]
            mine = [z for z in mine if z[0] <= wroom[1]]
            if not mine:
                continue                                              # at most per_wallet[tier] open copies of one wallet
            v = min(mine, key=lambda z: z[0])[1]                      # its own dead copy gives way to its new one
            open_.remove(v)
            pnl += early(v, o)
            settle(v, o)
        if tok and cool_n:
            rec = [r for c_, t_, r in closed if t_ == tok and o - c_ < cool_h * 3600]
            if len(rec) >= cool_n and sum(rec) < -cool_min:            # cool_min: the net loss that counts (0 = live)
                continue
        r_ = reserve if k else 0

        def plan():
            free = cap - sum(x[4] for x in open_)
            sz = min(max(size, free / div) if div else size, free - r_ * size)
            # min_frac < 1: the last of the capital is used when it is at least that share of a base copy
            return sz, len(open_) + 1 + r_ <= slots and sz >= min_frac * size - 0.01
        if tok_frac and tok:
            # $ PER TOKEN (night of 2026-10-02: one SGT ladder held ~90% of the book): at most tok_frac of the capital in one token
            tok_open = sum(x[4] for x in open_ if x[5] == tok)
            plan_ = plan

            def plan():
                sz_, ok_ = plan_()
                sz_ = min(sz_, tok_frac * cap - tok_open)
                return sz_, ok_ and sz_ >= min_frac * size - 0.01
        # room_floor (night of 2026-10-02: 'make room' closed CME at -38% to open VRAX): a copy below -room_floor % is never
        # closed to make room - it is held to its leader's exit instead of booking the loss
        keep_ = (lambda x: x[7] is not None and _at(x[7], o)[0] < -room_floor) if room_floor is not None else (lambda x: False)
        sz, ok = plan()
        while not ok and room:                                       # capital at waste gives way first, whatever its tier
            dead = [(fee_rate(x[7], o, room[0]), x) for x in open_ if x[7] and o - x[3] >= room[0] and not keep_(x)]
            dead = [z for z in dead if z[0] <= room[1]]
            if not dead:
                break
            v = min(dead, key=lambda z: z[0])[1]
            open_.remove(v)
            pnl += early(v, o)
            settle(v, o)
            sz, ok = plan()
        while not ok and any(x[2] > k and not keep_(x) for x in open_):
            v = max((x for x in open_ if x[2] > k and not keep_(x)), key=lambda x: (x[2], x[3]))
            open_.remove(v)
            pnl += early(v, o)
            settle(v, o)
            sz, ok = plan()
        if not ok:
            continue
        if match and lead:                                            # match: never more than match x the leader's own $
            sz = min(sz, match * lead)
        dil = max(0.5, 1 - 0.001 * (sz - 25))
        m_ = sz / 100 * dil * (1.0 if a in COPIED else KEEP.get(a, LAG))
        open_.append([cl, m_ * pct - gas, k, o, sz, tok, a, path, m_])
    for x in open_:
        pnl += x[1] if x[0] <= t1 else 0.0
        if ledger is not None and x[0] <= t1:
            ledger.append((x[0], x[1]))
        if by_wallet is not None and x[0] <= t1:
            by_wallet[x[6]] = by_wallet.get(x[6], 0.0) + x[1]
        settle(x, x[0])
    return pnl, busy[0] / (cap * (t1 - t0))


def tune(chain, log=print, folds=5, only=None):
    """lp tune (owner 2026-09-30, cron Mondays): re-run the blind fold tests behind the copy desk's settings on today's
    data. Each fold is one day, picked only on the days before it (from a week back), judged on that day, with the live
    take rates, measured gas and the live copy size (lpfollow.live_size). Tries every value of each setting with the
    others at their live values, and flags a value only if it beats live on the mean WITHOUT a worse worst day. The
    simulator is the live room rule (simulate_tiers): mains, bench + specialists, filler. Proposes; never applies."""
    import statistics as S
    from copydesk import lpdb
    from copydesk.lpfollow import cfg as fcfg, leaders, own_wallets, live_size
    c = fcfg(chain)
    measure_take(chain)
    try:
        from copydesk.lpfind import measured_gas
        chain.refresh_eth_usd()
        gas = measured_gas(chain) or COPY_GAS
    except Exception:
        gas = COPY_GAS
    size, book = live_size(chain)
    fmin = c.get('filler_min_score')
    live = dict(bench_n=int(c.get('bench_n', 5)), specialists=int(c.get('specialists_per_block', 1)), mains='live',
                reserve=int(c.get('reserve', 1)), slots=int(c.get('max_open', 5)), filler=float(fmin) if fmin is not None else 'off',
                size_div=float(c.get('size_free_div', 0) or 0), per_token=int(c.get('max_per_token', 0) or 0),
                bench_per_wallet=int(c.get('bench_per_wallet', 0) or 0))
    grid = dict(bench_n=(0, 2, 3, 5, 7, 10), specialists=(0, 1, 2), mains=('live', 1, 2, 3, 4), reserve=(0, 1, 2), slots=(5, 6, 8),
                filler=('off', 2.5, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0), size_div=(0.0, 2.0, 3.0, 4.0), per_token=(0, 2, 3, 4), bench_per_wallet=(0, 1, 2, 3))
    for k, v in live.items():
        if v not in grid[k]:
            grid[k] = tuple(grid[k]) + (v,)
    if only:                                                         # lp tune --only per_token,slots: just those knobs
        grid = {k: v for k, v in grid.items() if k in only}
    own, cur = own_wallets(), leaders(chain)
    con = lpdb.connect()
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    now, D = time.time(), 86400.0
    start = now - 7 * D
    res = {}
    for k in range(folds, 0, -1):
        b, cc = now - k * D, now - (k - 1) * D
        sc = lpdb.copy_score(days=(b - start) / D, size=size, gas=gas, end=b)
        ranked = [o for o, (v, _) in sorted(sc.items(), key=lambda kv: -kv[1][0]) if v > 0 and o not in own]
        bsc = lpdb.copy_score_blocks((b - start) / D, size=size, gas=gas, end=b)
        P, blk = {}, lambda t: time.gmtime(t).tm_hour // 3 * 3
        pos = lambda a, hrs=None: [x for x in P.setdefault(a, _positions_t(con, a, b, cc)) if hrs is None or blk(x[0]) in hrs]
        for key, vals in grid.items():
            for v in vals:
                s_ = dict(live, **{key: v})
                mains = cur if s_['mains'] == 'live' else ranked[:s_['mains']]
                bench = [o for o in ranked if o not in mains][:s_['bench_n']]
                hours = {}
                if s_['specialists']:
                    act = collections.Counter(blk(t) for a in mains + bench for (t,) in con.execute(
                        'SELECT t0 FROM copy_trades WHERE owner = ? AND t0 >= ? AND t0 < ?', (a, start, b)))
                    _, hours = specialists(act, bsc, set(mains) | set(bench) | own, s_['specialists'])
                used = set(mains) | set(bench) | set(hours)
                fill = [] if s_['filler'] == 'off' else [o for o in ranked if o not in used and sc[o][0] >= s_['filler']][:int(c.get('filler_n', 40))]
                T = [[(a, pos(a)) for a in mains], [(a, pos(a)) for a in bench] + [(a, pos(a, set(h))) for a, h in hours.items()],
                     [(a, pos(a)) for a in fill]]
                sz = book / s_['slots'] if s_['slots'] != live['slots'] else size
                r, u = simulate_tiers(T, sz, book, gas, b, cc, reserve=s_['reserve'], div=s_['size_div'], slots=s_['slots'],
                                      per_token=s_['per_token'], cool_n=int(c.get('cool_n', 8)), cool_h=float(c.get('cool_h', 24.0)),
                                      per_wallet={1: s_['bench_per_wallet']})
                res.setdefault((key, v), []).append((r / ((cc - b) / D), u))
    con.close()
    out, better = ['lp tune: %d blind one-day folds, book $%.0f, copy $%.0f, gas $%.3f, take %.0f%%  (mean $/day / worst day / capital deployed)' % (
        folds, book, size, gas, 100 * TAKE_ALL)], []
    for key, vals in grid.items():
        L_ = [r for r, _ in res[(key, live[key])]]
        lm, lw = S.mean(L_), min(L_)
        out.append('  %s' % key)
        for v in vals:
            r = [x for x, _ in res[(key, v)]]
            m, w, u = S.mean(r), min(r), S.mean(x for _, x in res[(key, v)])
            tag = ' <- live' if v == live[key] else (' BETTER' if m > lm and w >= lw else '')
            if tag == ' BETTER':
                better.append('%s %s (mean %+.2f vs %+.2f, worst %+.2f vs %+.2f)' % (key, v, m, lm, w, lw))
            beat = sum(1 for a_, b_ in zip(r, L_) if a_ > b_ + 0.005)
            out.append('    %-6s mean %+8.2f  worst %+8.2f  deployed %3.0f%%  beats live %d/%d   per day %s%s' % (
                v, m, w, 100 * u, beat, len(r), ' '.join('%+.0f' % x for x in r), tag))
    out.append('proposal: ' + ('; '.join(better) if better else 'keep every live setting'))
    for l in out:
        log(l)
    return better, out



RECENCY = [('1 day', 1, None), ('2 days', 2, None), ('3 days', 3, None), ('5 days', 5, None), ('all (7 d)', 7, None), ('9 days', 9, None), ('10 days', 10, None),
           ('fade half-life 1 d', None, 1.0), ('fade half-life 2 d', None, 2.0), ('fade half-life 3 d', None, 3.0)]


def squad_walk(chain, log=print, days=3.0, lookback=7.0, hs=(24, 12, 6, 3, 1)):
    """lp tune squad (owner 2026-09-30: a squad that lives, like a football squad): WALK FORWARD over the last `days`. Every
    H hours the squad is re-picked from the copies closed BEFORE that moment only - the 7-day copy score, the pick's hard QC
    flags, mains 2 / bench 5 / one specialist per quiet block / filler >= the live bar - changing at most S wallets per tier
    per run (a wallet that lost its edge or fails QC always leaves). The desk (simulate_tiers, live rules) trades each
    squad until the next re-pick. Against: the squad of the first moment, never changed."""
    import statistics as S
    from copydesk import lpdb, lpqc
    from copydesk.lpfollow import cfg as fcfg, own_wallets, live_size
    c = fcfg(chain)
    measure_take(chain)
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    try:
        from copydesk.lpfind import measured_gas
        gas = measured_gas(chain) or COPY_GAS
    except Exception:
        gas = COPY_GAS
    size, book = live_size(chain)
    own = own_wallets() | {x.lower() for x in (c.get('qc_exclude') or [])}
    fmin, fn = float(c.get('filler_min_score', 10.0)), int(c.get('filler_n', 40))
    con = lpdb.connect()
    first = con.execute('SELECT min(t0) FROM copy_trades').fetchone()[0]
    now, D = time.time(), 86400.0
    T0 = now - days * D
    blk = lambda t: time.gmtime(t).tm_hour // 3 * 3
    target_memo = {}

    def target(t):
        """The squad the pick would name at t, with no memory: (ok set, ranked, mains, bench, specialists {w: hours}, filler)."""
        if t in target_memo:
            return target_memo[t]
        lb = min(lookback, (t - first) / D)
        sc = lpdb.copy_score(lb, size=size, gas=gas, end=t)
        bsc = lpdb.copy_score_blocks(lb, size=size, gas=gas, end=t)
        cand = ({o for o, (v, _) in sc.items() if v > 0} | {o for o, v in bsc.items() if any(x > 0 for x in v.values())}) - own
        down = set(lpqc.turned_down(chain, cand, end=t))
        ok = {o for o, (v, _) in sc.items() if v > 0} - own - down
        ranked = sorted(ok, key=lambda o: -sc[o][0])
        target_memo[t] = (ok, ranked, sc, bsc, down)
        return target_memo[t]

    def step(t, prev, swaps):
        ok, ranked, sc, bsc, down = target(t)

        def tier(prev_l, n, elig, taken):
            keep = [a for a in prev_l if a in elig and a not in taken]
            want = [a for a in ranked if a in elig and a not in taken][:n]
            outs = sorted([a for a in keep if a not in want], key=lambda a: sc[a][0])
            ins = [a for a in want if a not in keep]
            k_ = len(ins) if swaps is None else min(swaps, len(outs), len(ins))
            keep = [a for a in keep if a not in outs[:k_]]
            return (keep + [a for a in ins if a not in keep])[:n] if len(keep) < n else keep[:n]
        mains = tier(prev[0], 2, ok, set())
        bench = tier(prev[1], int(c.get('bench_n', 5)), ok, set(mains))
        act = collections.Counter(blk(t_) for a in mains + bench for (t_,) in con.execute(
            'SELECT t0 FROM copy_trades WHERE owner = ? AND t0 >= ? AND t1 <= ?', (a, t - lookback * D, t)))
        _, hours = specialists(act, {o: v for o, v in bsc.items() if o not in down and o not in own}, set(mains) | set(bench), 1)
        fill = tier(prev[3], fn, {o for o in ok if sc[o][0] >= fmin}, set(mains) | set(bench) | set(hours))
        return (mains, bench, hours, fill)

    P = {}
    pos = lambda a: P.setdefault(a, _positions_t(con, a, T0 - D, now))

    def run(H, swaps):
        sched, sq = [], step(T0, ([], [], {}, []), None)
        t = T0
        while t < now:
            if t > T0 and H:
                sq = step(t, sq, swaps)
            sched.append((t, sq))
            t += (H or 1e9) * 3600
        changes = sum(len(set(b[0] + b[1] + b[3]) ^ set(a[0] + a[1] + a[3])) // 2 for (_, a), (_, b) in zip(sched, sched[1:]))

        def tiers(w0, w1):
            T = [[], [], []]
            for i, (ts, (m, b, h, f)) in enumerate(sched):
                te = sched[i + 1][0] if i + 1 < len(sched) else now
                lo, hi = max(ts, w0), min(te, w1)
                if lo >= hi:
                    continue
                for k, grp in ((0, m), (1, b), (2, f)):
                    T[k] += [(a, [x for x in pos(a) if lo <= x[0] < hi]) for a in grp]
                T[1] += [(a, [x for x in pos(a) if lo <= x[0] < hi and blk(x[0]) in hs]) for a, hs in h.items()]
            return T
        kw = dict(reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0), slots=int(c['max_open']),
                  per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)), cool_h=float(c.get('cool_h', 24.0)),
                  per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=float(c.get('last_slot_frac', 1.0) or 1.0))
        # ONE run over the whole span (a copy can live 45 h: cut by day, it held a slot all day and its P&L fell outside
        # every window), each booking counted on the day it happened
        led = []
        simulate_tiers(tiers(T0, now), size, book, gas, T0, now, ledger=led, **kw)
        per = [sum(v for ts, v in led if now - k * D <= ts < now - (k - 1) * D) for k in range(int(days), 0, -1)]
        return per, changes, len(sched)
    rows = [('frozen (the first squad, never changed)', run(0, None))]
    for H in hs:
        for sw in (1, 2, None):
            rows.append(('every %2dh, %s' % (H, 'any number of swaps' if sw is None else 'max %d swap%s a tier' % (sw, 's' if sw > 1 else '')), run(H, sw)))
    con.close()
    base = rows[0][1][0]
    out = ['lp tune squad: walk forward over the last %g days, re-picked from the copies closed before each moment (%g-day lookback),'
           ' $%.0f a copy, book $%.0f' % (days, lookback, size, book),
           '  %-42s %9s %9s %8s %8s  %s' % ('re-pick', 'mean/day', 'worst', 'beats', 'swaps', 'per day')]
    for nm, (per, ch, n) in rows:
        out.append('  %-42s %+9.2f %+9.2f %5d/%d %8d  %s' % (nm, S.mean(per), min(per), sum(1 for x, y in zip(per, base) if x > y + 0.005),
                   len(per), ch, ' '.join('%+.0f' % x for x in per)))
    for l in out:
        log(l)
    return out


def recency(chain, log=print, folds=5):
    """lp tune recency (owner 2026-09-30): does a shorter or fading scoring window predict TOMORROW's copy P&L better
    than the live 7 equal days? Blind: each of the last `folds` days is scored only on copies before it, with
    copy_score's rule (mean - one standard error, x copies a day, x trust) and each scheme's weights. Reports the rank
    correlation with the next day and what each scheme's top 5 / 10 / 30 made that day. Proposes nothing."""
    import math, statistics as S
    from copydesk import lpdb
    from copydesk.lpfollow import own_wallets, live_size
    size, book = live_size(chain)
    gas, own, day = COPY_GAS, own_wallets(), 86400.0
    try:
        from copydesk.lpfind import measured_gas
        gas = measured_gas(chain) or gas
    except Exception:
        pass
    con = lpdb.connect()
    by = collections.defaultdict(list)
    for o, t, p, sz, r, m in con.execute('SELECT owner, t0, pnl, size_usd, real, pnl0 FROM copy_trades'):
        if o not in own:
            by[o].append((t, max(-size, min(size, p * size / (sz or 25.0))) - gas, r, m))
    con.close()
    first = min(t for v in by.values() for t, *_ in v)

    def score(v, b, w, h):
        lo = max(first, b - (w or 7) * day)
        xs = [(x, 0.5 ** ((b - t) / (h * day)) if h else 1.0) for t, x, r, m in v if lo <= t < b]
        if not xs:
            return None
        W = sum(wt for _, wt in xs)
        mu = sum(x * wt for x, wt in xs) / W
        n_eff = W * W / sum(wt * wt for _, wt in xs)
        se = math.sqrt(sum(wt * (x - mu) ** 2 for x, wt in xs) / W / n_eff) if n_eff > 1 else abs(mu)
        span = (b - lo) / day
        per_day = (mu - se) * W / max((h / math.log(2) * (1 - 0.5 ** (span / h))) if h else span, 1e-9)
        R = sum(r for t, x, r, m in v if t < b and r is not None and m is not None)
        M = sum(m for t, x, r, m in v if t < b and r is not None and m is not None)
        tr = min(R / M, M / R) if R > 0 and M > 0 else 0.0
        return per_day * tr if per_day > 0 else per_day
    rank = lambda xs: [sorted(range(len(xs)), key=lambda i: xs[i]).index(i) for i in range(len(xs))]
    res = collections.defaultdict(list)
    today = time.time() // day * day
    for b in [today - k * day for k in range(folds, 0, -1)]:
        nxt = {o: sum(x for t, x, *_ in v if b <= t < b + day) for o, v in by.items()}
        for name, w, h in RECENCY:
            sc = {o: s for o, s in ((o, score(v, b, w, h)) for o, v in by.items()) if s is not None}
            ws = sorted(sc, key=lambda o: -sc[o])
            rho = S.correlation(rank([sc[o] for o in ws]), rank([nxt[o] for o in ws])) if len(ws) > 2 else float('nan')
            res[name].append((rho, sum(nxt[o] for o in ws[:5]), sum(nxt[o] for o in ws[:10]), sum(nxt[o] for o in ws[:30])))
    out = ["lp tune recency: %d blind days, copy $%.0f, gas $%.3f (the next day at the chosen wallets' replayed copies, uncapped by our slots)" % (folds, size, gas),
           '  %-20s %8s %8s %8s %8s   %s' % ('scoring', 'rank r', 'top-5', 'top-10', 'top-30', 'top-10 per day')]
    for name, *_ in RECENCY:
        r = res[name]
        out.append('  %-20s %8.3f %+8.0f %+8.0f %+8.0f   %s' % (name, S.mean(x[0] for x in r), S.mean(x[1] for x in r), S.mean(x[2] for x in r),
                                                            S.mean(x[3] for x in r), ' '.join('%+.0f' % x[2] for x in r)))
    for l in out:
        log(l)
    return out


TRAIL = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50]
STOP_KINDS = ('trail', 'stop', 'redtrail')      # from the peak | from entry | from the peak, only while under entry


def _stop_hit(pts, tin, kind, s_, react):
    """Where a stop of `kind` at level s_ closes a copy: (block + react, value there) or None.
    trail: value < (1 - s) x peak so far; stop: value < (1 - s) x entry; redtrail: the trail rule, but only while the copy
    is under its entry (a copy in profit is never cut)."""
    peak = tin
    for k, (blk, v) in enumerate(pts):
        peak = max(peak, v)
        if kind == 'trail':
            hit = v < (1 - s_) * peak
        elif kind == 'stop':
            hit = v < (1 - s_) * tin
        else:
            hit = v < tin and v < (1 - s_) * peak
        if hit:
            return blk + react, next((vv for bb, vv in pts[k:] if bb >= blk + react), v)
    return None


def trail(chain, days=3.0, log=print, react=2):
    """lp tune trail (owner 2026-09-30): would a TRAILING STOP of our own - close a copy when its value falls s under its
    own peak, `react` blocks after the trigger, at the value there - keep the upside and cut the downside? Every copy of
    the live squad over `days` from the local history store (lpcopy.value_path, our size, the live lag), per copy and at
    desk level (the live room rule, each day on its own; a stopped copy frees its slot early). Proposes nothing."""
    import statistics as S, zlib
    from copydesk import lpcopy as C, lpdb
    from copydesk.lpfollow import cfg as fcfg, leaders, bench, filler, live_size
    c = fcfg(chain)
    size, book = live_size(chain)
    gas = COPY_GAS
    try:
        from copydesk.lpfind import measured_gas
        gas = measured_gas(chain) or gas
    except Exception:
        pass
    mains, bn, fl = leaders(chain), [b for b in bench(chain) if b not in leaders(chain)], filler(chain)
    rows = {}
    for a in mains + bn + fl:
        C._PREP.clear()
        if C.run_store(a, days, ['live'], size=size) is None:
            log('lp tune trail: the history store does not cover %g days yet (lp hist)' % days)
            return None
        D, done, pos, key, sw = list(C._PREP.values())[0]
        opens = sorted(q['open'] for q in pos)
        out = []
        for p in pos:
            d = C.lag_table()[C.copy_kind(p, key, sw, opens)]
            r = C.value_path(p, key, sw, D['dec'], D['eusd'], d, size)
            if not r:
                continue
            pts, tin = r
            res = {'none': (pts[-1][1] - tin, D['tsof'](p['close'] + d))}
            for kind in STOP_KINDS:
                for s_ in TRAIL:
                    h = _stop_hit(pts, tin, kind, s_, react)
                    res[(kind, s_)] = (h[1] - tin, D['tsof'](h[0])) if h else res['none']
            out.append(dict(t0=D['tsof'](p['open'] + d), tin=tin, token=C._token(key[p['legs'][0][0]]), res=res))
        rows[a] = out
    allc = [r for v in rows.values() for r in v]
    keys_ = ['none'] + [(k_, s_) for k_ in STOP_KINDS for s_ in TRAIL]
    name = lambda k: k if k == 'none' else '%s %g%%' % (k[0], 100 * k[1])
    W_ = [r for r in allc if r['res']['none'][0] > 0]
    L_ = [r for r in allc if r['res']['none'][0] < 0]
    res_out = ['lp tune trail: %d copies of %d wallets, %g days, $%.0f a copy, the live lag, exit %d blocks after the trigger (before gas)' % (
        len(allc), len(rows), days, size, react),
        '  %-14s %10s %8s %13s %11s %9s %9s' % ('rule', 'total $', 'stopped', 'winners kept', 'losers', 'worst', 'p5 copy')]
    for s_ in keys_:
        p_ = sorted(r['res'][s_][0] for r in allc)
        st_ = sum(1 for r in allc if s_ != 'none' and abs(r['res'][s_][1] - r['res']['none'][1]) > 1)
        res_out.append('  %-14s %+10.2f %8d %+13.2f %+11.2f %+9.2f %+9.2f' % (name(s_), sum(p_), st_,
                       sum(r['res'][s_][0] for r in W_), sum(r['res'][s_][0] for r in L_), p_[0] if p_ else 0, p_[len(p_) // 20] if p_ else 0))
    # desk level
    measure_take(chain)
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    con = lpdb.connect()
    tr = {}
    for a in rows:
        R_, M_ = con.execute('SELECT SUM(real), SUM(pnl0) FROM copy_trades WHERE owner = ? AND real IS NOT NULL', (a,)).fetchone()
        tr[a] = min(M_ / R_, R_ / M_) if R_ and M_ and R_ > 0 and M_ > 0 else 0.0
        COPIED.add(a)
    con.close()
    hrs = {k.lower(): v for k, v in (c.get('hours') or {}).items()}

    def positions(a, s_, t0, t1):
        take = TAKE.get(a, TAKE_ALL)
        keep = lambda t: (zlib.crc32(('%s%.0f' % (a, t)).encode()) % 10000) / 10000.0 < take
        out = []
        for r in rows.get(a, []):
            pnl, te = r['res'][s_]
            if not (t0 <= r['t0'] < t1) or te > t1 or not keep(r['t0']):
                continue
            if a in hrs and time.gmtime(r['t0']).tm_hour // 3 * 3 not in hrs[a]:
                continue
            pct = max(-CAP_PCT, min(CAP_PCT, 100.0 * pnl / r['tin']))
            out.append((r['t0'], te, r['tin'], pct * (tr[a] if pct > 0 else 1.0), r['token']))
        return out
    now, D_ = time.time(), 86400.0
    dres = collections.defaultdict(list)
    for k in range(int(days), 0, -1):
        b, cc = now - k * D_, now - (k - 1) * D_
        for s_ in keys_:
            T = [[(a, positions(a, s_, b, cc)) for a in mains], [(a, positions(a, s_, b, cc)) for a in bn], [(a, positions(a, s_, b, cc)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)})
            dres[s_].append((p_ / ((cc - b) / D_), u))
    bn_ = [x for x, _ in dres['none']]
    res_out.append('  desk level (live room rule, live squad, take rate + trust, each day on its own):')
    for s_ in keys_:
        ps = [x for x, _ in dres[s_]]
        res_out.append('  %-14s mean %+8.2f/day  worst %+8.2f  beats no stop %d/%d   per day %s' % (
            name(s_), S.mean(ps), min(ps), sum(1 for x, y in zip(ps, bn_) if x > y + 0.005), len(ps),
            ' '.join('%+.0f' % x for x in ps)))
    for l in res_out:
        log(l)
    return res_out


COMBOS = [(0.50, 120), (0.50, 60), (0.25, 120), (0.25, 60)]
IDLE_T = [15, 30, 60, 120, 240]
DEAD = [(w, r) for w in (15, 30, 60) for r in (0.0, 0.1, 0.25, 0.5)]
LIVE_PLUS = [(None, 0, 1.0), (None, 0, 0.5)] + [(k, t, mf) for k in ('untouched', 'idle', 'oor', 'cap') for t in IDLE_T for mf in (1.0, 0.5)]
# untouched / idle: judged ONCE, T minutes after our entry. oor: out of range for T minutes in a row, at any point of the
# copy's life. cap: every copy still open T minutes after entry is closed, whatever it is doing
DOWNSIDE = [('untouched', IDLE_T), ('idle', IDLE_T), ('oor', IDLE_T), ('cap', IDLE_T), ('timeout', [15, 30, 60, 120]), ('fellthrough', [0.0, 0.05, 0.10, 0.20, 0.30]), ('feecover', [1.0, 2.0, 3.0, 5.0]),
            ('crash', [0.10, 0.20, 0.30]), ('knife', [0.10, 0.20, 0.30, 0.50]), ('thin', [0.10, 0.25, 0.50, 0.75]), ('tp', [0.05, 0.10, 0.20])]


def downside(chain, days=3.0, log=print, react=2):
    """lp tune downside (owner 2026-09-30): downside rules that should leave the UPSIDE alone. Exit rules on each copy's
    value path (lpcopy.value_path, detail): timeout T min (still under entry at T -> out), fellthrough X (a range held
    entirely in the token, the token X% past it -> out), feecover k (paper loss > k x fees earned -> out), crash X (token
    down X% inside a minute -> out). Entry filters: knife X (token down X% in the hour before -> skip), thin S (our copy
    over S of the pool's active liquidity -> skip). Per copy and at desk level, the store's live squad over `days`."""
    import statistics as S, zlib, bisect as B
    from copydesk import lpcopy as C, lpdb
    from copydesk.lpfollow import cfg as fcfg, leaders, bench, filler, live_size
    c = fcfg(chain)
    size, book = live_size(chain)
    gas = COPY_GAS
    try:
        from copydesk.lpfind import measured_gas
        gas = measured_gas(chain) or gas
    except Exception:
        pass
    mains, bn, fl = leaders(chain), [b for b in bench(chain) if b not in leaders(chain)], filler(chain)
    R = C._hist()
    rows = {}
    # THE POOL AT ENTRY (research/lpfeat: turnover, the hour's volume z-score, volatility - only the hours before entry)
    import bisect as _B, math as _M
    from research import lpfeat as FE
    _pc = {}

    _first = {}

    _tokfirst = {}

    def tok_age(tok, b0):
        """Hours since the token's first swap in ANY of its pools in the store (a token older than the store reads as old as it)."""
        if not _tokfirst:
            fs = dict(R.con.execute('SELECT pool, min(blk) FROM swaps GROUP BY pool').fetchall())
            for n_, v_ in R.pid.items():
                if n_ in fs:
                    t_ = v_[2] if v_[1] in (C.W.USDG, C.W.WETH, C.NATIVE) else v_[1]
                    _tokfirst[t_] = min(_tokfirst.get(t_, 1 << 62), fs[n_])
        f_ = _tokfirst.get(tok)
        return (b0 - f_) / 36000 if f_ is not None else None

    def pool_age(pid, b0):
        """Hours since the pool's first swap in the store (a pool older than the store reads as old as the store)."""
        if pid not in _first:
            n_ = next((k for k, v in R.pid.items() if v[0] == pid), None)
            _first[pid] = R.con.execute('SELECT min(blk) FROM swaps WHERE pool = ?', (n_,)).fetchone()[0] if n_ is not None else None
        return (b0 - _first[pid]) / 36000 if _first[pid] is not None else None

    _cre = {}

    def creator_in_at(pid, b0):
        """Was the pool's creator - its first PositionManager LP in the store - still in at block b0 (no closed position yet)?"""
        if pid not in _cre:
            n_ = next((k for k, v in R.pid.items() if v[0] == pid), None)
            ms = R.con.execute('SELECT p.close_blk, nf.owner FROM positions p JOIN nfts nf ON nf.tid = p.tid WHERE p.pool = ? ORDER BY p.open_blk',
                               (n_,)).fetchall() if n_ is not None else []
            _cre[pid] = (ms[0][1] if ms else None, [cb for cb, o in ms if ms and o == ms[0][1] and cb is not None])
        cr, closes = _cre[pid]
        return cr is not None and not any(cb <= b0 for cb in closes)

    def pool_state(pid, key_, b0):
        if pid not in _pc:
            sw_ = R.swaps([(pid, max(R.span()[0], b0 - 80 * 36000), R.span()[1])]).get(pid) or []
            _pc[pid] = (sw_, [x[0] for x in sw_], FE.pool_hours(R, pid, key_, sw_, 0, 0))
        sw_, bl_, hrs_ = _pc[pid]
        i = _B.bisect_right(bl_, b0) - 1
        if i < 0 or not sw_[i][4]:
            return None
        return FE.features(hrs_, int(R.tsof(b0) // 3600), sw_[i][4], sw_[i][5], key_, R, b0)
    C._min_leader()
    ml_live, C._NAT['min'] = C._NAT['min'], 0.0                   # every leader size in the rows; the floor is applied below
    for a in mains + bn + fl:
        C._PREP.clear()
        if C.run_store(a, days, ['live'], size=size) is None:
            log('lp tune downside: the history store does not cover %g days yet' % days)
            return None
        D, done, pos, key, sw = list(C._PREP.values())[0]
        opens = sorted(q['open'] for q in pos)
        out = []
        for p in pos:
            d = C.lag_table()[C.copy_kind(p, key, sw, opens)]
            r = C.value_path(p, key, sw, D['dec'], D['eusd'], d, size, detail=True)
            if not r:
                continue
            pts, tin, share, lead, ecost = r
            b0 = p['open'] + d
            # the token's price an hour before the copy (from the store): the falling-knife filter
            pid = p['legs'][0][0]
            n_ = next((k for k, v in R.pid.items() if v[0] == pid), None)
            old = R.con.execute('SELECT sp FROM swaps WHERE pool = ? AND blk <= ? ORDER BY blk DESC, li DESC LIMIT 1', (n_, b0 - 36000)).fetchone() if n_ else None
            q0 = key[pid][0] in (C.W.USDG, C.W.WETH, C.NATIVE)
            tp0 = pts[1][4] if len(pts) > 1 and pts[1][4] else None
            tp_old = (1 / (old[0] ** 2) if q0 else old[0] ** 2) if old and old[0] else None
            drop_1h = 1 - tp0 / tp_old if tp0 and tp_old else 0.0
            base = (pts[-1][1] - tin, D['tsof'](p['close'] + d))
            tsp = [D['tsof'](x[0]) for x in pts]
            ts0, t_end = tsp[0], base[1]
            res = {'none': base}
            ts = lambda blk: D['tsof'](blk)
            exit_at = lambda k: (next((x[1] for x in pts[k:] if x[0] >= pts[k][0] + react), pts[k][1]) - tin, ts(pts[k][0] + react))
            for kind, levels in DOWNSIDE:
                for lv in levels:
                    hit = None
                    if kind in ('untouched', 'idle', 'cap'):
                        # the copy's state AT T minutes (the last swap before it: nothing moves between swaps), if it is
                        # still open then. untouched = no fee earned (the price never reached the range); idle = under
                        # $0.10 of fees and within 1% of what went in; cap = always. Closed at T, at that value
                        t_ = ts0 + lv * 60
                        if t_ < t_end:
                            x = pts[max(i for i, x in enumerate(pts) if tsp[i] <= t_)]
                            if kind == 'cap' or (kind == 'untouched' and x[2] <= 0.0 and abs(x[1] - tin) < 0.01) or \
                                    (kind == 'idle' and x[2] < 0.10 and abs(x[1] - tin) < 0.01 * tin):
                                hit = (x[1] - tin, t_)
                    elif kind == 'oor':
                        # out of range T minutes in a row, anywhere in the copy's life: closed then, at that value
                        since = None
                        for i, x in enumerate(pts + [None]):
                            t_i = tsp[i] if x else t_end
                            if since is not None and t_i - since >= lv * 60:
                                t_ = since + lv * 60
                                if t_ < t_end:
                                    hit = (pts[i - 1][1] - tin, t_)
                                break
                            if x is None:
                                break
                            since = (since if since is not None else tsp[i]) if not x[5] else None
                    elif kind == 'timeout':
                        t_ = D['tsof'](b0) + lv * 60
                        k = next((i for i, x in enumerate(pts) if D['tsof'](x[0]) >= t_), None)
                        if k is not None and pts[k][1] < tin:
                            hit = exit_at(k)
                    elif kind == 'fellthrough':
                        k = next((i for i, x in enumerate(pts) if i and x[3] > lv), None)
                        hit = exit_at(k) if k is not None else None
                    elif kind == 'tp':
                        # TAKE PROFIT: closed the first time the copy is worth lv more than went in (fees included)
                        k = next((i for i, x in enumerate(pts) if i and x[1] - tin >= lv * tin), None)
                        hit = exit_at(k) if k is not None else None
                    elif kind == 'feecover':
                        k = next((i for i, x in enumerate(pts) if i and tin - (x[1] - x[2]) > lv * max(x[2], 0.01)), None)
                        hit = exit_at(k) if k is not None else None
                    elif kind == 'crash':
                        win = []
                        for i, x in enumerate(pts):
                            if not i or not x[4]:
                                continue
                            win.append((x[0], x[4]))
                            while win and win[0][0] < x[0] - 600:
                                win.pop(0)
                            if x[4] < (1 - lv) * max(v for _, v in win):
                                hit = exit_at(i)
                                break
                    elif kind == 'knife':
                        hit = 'skip' if drop_1h > lv else None
                    elif kind == 'thin':
                        hit = 'skip' if share > lv else None
                    res[(kind, lv)] = hit or base
            for kind_, tm, mf in LIVE_PLUS:                          # the live desk (thin 50%) + an idle close + the last slot
                res[('live+', (kind_, tm, mf))] = 'skip' if res[('thin', 0.50)] == 'skip' else (res[(kind_, tm)] if kind_ else base)
            for sh, tm in COMBOS:                                    # thin pools skipped AND stuck losers timed out
                res[('thin+timeout', (sh, tm))] = 'skip' if res[('thin', sh)] == 'skip' else res[('timeout', tm)]
            # THE PATH (capital at work): ts, P&L % and fees % of what went in, swap by swap
            path = ([ts0] + tsp[1:], [0.0] + [100 * (x[1] - tin) / tin for x in pts[1:]], [0.0] + [100 * x[2] / tin for x in pts[1:]])
            for w_, r_ in DEAD:
                # DEAD CAPITAL: closed at the first minute (from w_ after entry) its trailing w_ minutes earned <= r_ %/h
                hit = None
                t_ = ts0 + w_ * 60
                while t_ < t_end:
                    if fee_rate(path + (1.0,), t_, w_ * 60) <= r_:
                        hit = (_at(path + (1.0,), t_)[0] * tin / 100, t_)
                        break
                    t_ += 60
                res[('dead', (w_, r_))] = 'skip' if res[('thin', 0.50)] == 'skip' else (hit or base)
            out.append(dict(t0=D['tsof'](b0), tin=tin, token=C._token(key[pid]), res=res, lead=lead, path=path, feat=pool_state(pid, key[pid], b0),
                            ecost=ecost / tin if tin else 0.0, age_h=pool_age(pid, b0), tok_age_h=tok_age(C._token(key[pid]), b0), cin=creator_in_at(pid, b0),
                            rng=ecost > 0, share=share))
        rows[a] = out
    allc = [r for v in rows.values() for r in v]
    keys_ = ['none'] + [(k, lv) for k, levels in DOWNSIDE for lv in levels] + [('thin+timeout', x) for x in COMBOS] + [('live+', x) for x in LIVE_PLUS]

    def name(k):
        if k == 'none':
            return k
        if k[0] == 'thin+timeout':
            return 'thin %g%%+to %gm' % (100 * k[1][0], k[1][1])
        if k[0] == 'lastslot':
            return 'last slot %g%%' % (100 * k[1])
        if k[0] == 'fresh':
            return 'LIVE (cold|flat, room) %s' % ('as it runs' if k[1] is None else 'skip pools < %dh old' % k[1])
        if k[0] == 'ecost':
            return 'LIVE (cold|flat, room) %s' % ('as it runs' if k[1] is None else 'skip entry swap > %g%%' % (100 * k[1]))
        if k[0] == 'cold':
            fs, q = k[1]
            if isinstance(q, tuple):
                return 'LIVE (room 30m) skip %s' % ' OR '.join('%s < q%d' % (f, round(100 * qq)) for f, qq in zip(fs, q))
            if fs and 'nopop' in fs or fs and 'aimb24' in fs:
                return 'LIVE (room 30m) skip %s' % ' or '.join('no popular flow' if f == 'nopop' else '|imbalance| > q%d' % round(100 * q) for f in fs)
            return 'LIVE (room 30m) %s' % ('as it runs' if fs is None else 'skip %s < q%d' % ('+'.join(fs), round(100 * q)))
        if k[0] == 'wroom':
            return 'LIVE (room 30m) %s' % ('+ wallet room %dm <= %g%%/h' % k[1] if k[1] else 'as it runs')
        if k[0] == 'waste':
            return 'LIVE+last50 %s %dm <= %g%%/h' % k[1]
        if k[0] == 'floor':
            return 'LIVE+last50%s floor $%g %s' % (' +idle30' if k[1][2] else '', k[1][0], 'our size' if not k[1][1] else 'max %gx leader' % k[1][1])
        if k[0] == 'live+':
            kind_, tm, mf = k[1]
            return 'LIVE%s%s' % (' +%s %dm' % (kind_, tm) if kind_ else '', ' +last50' if mf < 1 else '')
        return '%s %s' % (k[0], ('%g min' % k[1]) if k[0] in ('timeout', 'untouched', 'idle', 'oor', 'cap') else ('%gx' % k[1]) if k[0] == 'feecover' else '%g%%' % (100 * k[1]))
    pnl = lambda r, k: 0.0 if r['res'][k] == 'skip' else r['res'][k][0]
    W_ = [r for r in allc if r['res']['none'][0] > 0]
    L_ = [r for r in allc if r['res']['none'][0] < 0]
    w0, l0 = sum(pnl(r, 'none') for r in W_), sum(pnl(r, 'none') for r in L_)
    res_out = ['lp tune downside: %d copies of %d wallets, %g days, $%.0f a copy, the live lag (before gas)' % (len(allc), len(rows), days, size),
               '  %-18s %10s %8s %14s %14s %9s' % ('rule', 'total $', 'acted', 'winners kept', 'losses cut', 'worst')]
    for k in keys_:
        p_ = [pnl(r, k) for r in allc]
        acted = sum(1 for r in allc if k != 'none' and r['res'][k] != r['res']['none'])
        wk, lc = sum(pnl(r, k) for r in W_), sum(pnl(r, k) for r in L_)
        res_out.append('  %-18s %+10.2f %8d %+9.2f (%3.0f%%) %+9.2f (%3.0f%%) %+9.2f' % (name(k), sum(p_), acted, wk, 100 * wk / w0 if w0 else 0,
                       lc - l0, 100 * (lc - l0) / -l0 if l0 else 0, min(p_) if p_ else 0))
    measure_take(chain)
    KEEP.clear()
    KEEP.update(lpdb.keep_rates())
    con = lpdb.connect()
    tr = {}
    for a in rows:
        R_, M_ = con.execute('SELECT SUM(real), SUM(pnl0) FROM copy_trades WHERE owner = ? AND real IS NOT NULL', (a,)).fetchone()
        tr[a] = min(M_ / R_, R_ / M_) if R_ and M_ and R_ > 0 and M_ > 0 else 0.0
        COPIED.add(a)
    con.close()
    hrs = {k_.lower(): v for k_, v in (c.get('hours') or {}).items()}

    def positions(a, k, t0, t1, floor=None, skip=None, young_cap=False, capk=None, young_tok=None, rng_cap=False, creator=None):
        floor = ml_live if floor is None else floor
        take = TAKE.get(a, TAKE_ALL)
        keep = lambda t: (zlib.crc32(('%s%.0f' % (a, t)).encode()) % 10000) / 10000.0 < take
        o = []
        for r in rows.get(a, []):
            if r['res'][k] == 'skip' or r['lead'] < floor or (skip and skip(r)):
                continue
            pl, te = r['res'][k]
            if not (t0 <= r['t0'] < t1) or te > t1 or not keep(r['t0']):
                continue
            if a in hrs and time.gmtime(r['t0']).tm_hour // 3 * 3 not in hrs[a]:
                continue
            pct = max(-CAP_PCT, min(CAP_PCT, 100.0 * pl / r['tin']))
            ld = r['lead'] if not young_cap else (r['lead'] if (r.get('age_h') is not None and r['age_h'] < 1) else None)
            if young_tok is not None and r.get('tok_age_h') is not None and r['tok_age_h'] < young_tok:
                ld = r['lead']                                       # a young TOKEN: never more than the leader (match = 1)
            if rng_cap and r.get('rng') and ld is None:
                ld = r['lead']                                       # a RANGE copy: never more than the leader
            if capk and ld is None:
                ld = r['lead'] * capk                                # elsewhere: at most capk x the leader
            if creator is not None and r.get('age_h') is not None and r['age_h'] < creator[1] and (r.get('cin') or not creator[2]):
                if creator[0] <= 0:
                    continue                                         # THE LAUNCH CREATOR GUARD: skipped
                ld = min(ld if ld is not None else r['lead'], creator[0] * size)   # ... or at most frac x our size
            o.append((r['t0'], te, r['tin'], pct * (tr[a] if pct > 0 else 1.0), r['token'], ld, r['path'] + (tr[a],)))
        return o
    now, D_ = time.time(), 86400.0
    dres = collections.defaultdict(list)
    for kd in range(int(days), 0, -1):
        b, cc = now - kd * D_, now - (kd - 1) * D_
        for k in keys_:
            T = [[(a, positions(a, k, b, cc)) for a in mains], [(a, positions(a, k, b, cc)) for a in bn], [(a, positions(a, k, b, cc)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)},
                                   min_frac=k[1][2] if k != 'none' and k[0] == 'live+' else 1.0)
            dres[k].append((p_ / ((cc - b) / D_), u))
    # THE LAST SLOT: a copy may take what is left when it is at least this share of a base copy (no exit rule)
    for mf in (0.5, 0.75):
        for kd in range(int(days), 0, -1):
            b, cc = now - kd * D_, now - (kd - 1) * D_
            T = [[(a, positions(a, 'none', b, cc)) for a in mains], [(a, positions(a, 'none', b, cc)) for a in bn], [(a, positions(a, 'none', b, cc)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=mf)
            dres[('lastslot', mf)].append((p_ / ((cc - b) / D_), u))
        keys_.append(('lastslot', mf))
    # THE LEADER FLOOR (min_leader_usd): the live desk (thin 50% + idle 30m + last slot 50%) with each floor, our copy at
    # the live size or matched (never more than 1x / 3x the leader's own position)
    for lk, fl_, mt in [(lk_, f_, m_) for lk_ in (('live+', (None, 0, 0.5)), ('live+', ('idle', 30, 0.5)))
                        for f_ in (0.0, 5.0, 10.0, 20.0) for m_ in (0.0, 3.0)]:
        if True:
            for kd in range(int(days), 0, -1):
                b, cc = now - kd * D_, now - (kd - 1) * D_
                T = [[(a, positions(a, lk, b, cc, fl_)) for a in mains], [(a, positions(a, lk, b, cc, fl_)) for a in bn], [(a, positions(a, lk, b, cc, fl_)) for a in fl]]
                p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                       slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                       cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5, match=mt)
                dres[('floor', (fl_, mt, lk[1][0]))].append((p_ / ((cc - b) / D_), u))
            keys_.append(('floor', (fl_, mt, lk[1][0])))
    C._NAT['min'] = ml_live
    # CAPITAL WASTE (owner 2026-09-30: a copy the price never reaches holds capital that earns nothing): on the live desk
    # (thin 50% + last slot 50%), 'dead' closes a copy whose trailing w minutes earned <= r %/h; 'room' closes it only
    # when a new copy needs its capital or slot
    lk = ('live+', (None, 0, 0.5))
    for w_, r_ in DEAD:
        for kind in ('dead', 'room'):
            kk = ('dead', (w_, r_)) if kind == 'dead' else lk
            for kd in range(int(days), 0, -1):
                b, cc = now - kd * D_, now - (kd - 1) * D_
                T = [[(a, positions(a, kk, b, cc)) for a in mains], [(a, positions(a, kk, b, cc)) for a in bn], [(a, positions(a, kk, b, cc)) for a in fl]]
                p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                       slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                       cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                                       room=(w_ * 60, r_) if kind == 'room' else None)
                dres[('waste', (kind, w_, r_))].append((p_ / ((cc - b) / D_), u))
            keys_.append(('waste', (kind, w_, r_)))
    # A COLD POOL AT ENTRY (owner 2026-09-30, research/lpfeat --squad: turnover, the hour's volume z-score and volatility
    # predict a squad copy 8 of 8 days): skip a copy whose pool is in the bottom share q of the squad's copies on a feature
    fv = lambda r, f: (r.get('feat') or {}).get(f)
    allr = [r for v in rows.values() for r in v]
    cut = {}
    for f in ('turn24', 'volz', 'rv24'):
        xs = sorted(x for x in (fv(r, f) for r in allr) if x is not None)
        for q in (0.1, 0.2, 0.3):
            cut[(f, q)] = xs[int(q * len(xs))] if xs else None
    xs = sorted(x for x in (fv(r, 'aimb24') for r in allr) if x is not None)
    for q in (0.8, 0.9):
        cut[('aimb24', q)] = xs[int(q * len(xs))] if xs else None
    COLD = [(None, None)] + [((f,), q) for f in ('turn24', 'volz', 'rv24') for q in (0.1, 0.2, 0.3)] + [(('turn24', 'volz'), q) for q in (0.2, 0.3)] + \
        [(('aimb24',), 0.8), (('aimb24',), 0.9), (('nopop',), 0), (('aimb24', 'nopop'), 0.8)] + \
        [(('volz', 'rv24'), qs) for qs in ((0.3, 0.1), (0.3, 0.2), (0.2, 0.1))]      # COLD OR PRICE-FLAT: the two canonical factors

    def cold(r, fs, q):
        if isinstance(q, tuple):                                         # either feature under its own cut
            return any(fv(r, f) is not None and fv(r, f) < cut[(f, qq)] for f, qq in zip(fs, q))
        # ONE-SIDED FLOW / NO RETAIL (research/lpfeat, retail flow): |imbalance| over its top share, or no popular-size trade in 24 h
        hits = []
        for f in fs:
            if f == 'nopop':
                hits.append(r.get('feat') is not None and not fv(r, 'pop24'))
            elif f == 'aimb24':
                hits.append(fv(r, f) is not None and fv(r, f) > cut[(f, q)])
            else:
                hits.append(fv(r, f) is not None and fv(r, f) < cut[(f, q)])
        return any(hits) if 'nopop' in fs and len(fs) > 1 else all(hits)
    for fs, q in COLD:
        sk = None if fs is None else (lambda r, fs=fs, q=q: cold(r, fs, q))
        for kd in range(int(days), 0, -1):
            b, cc = now - kd * D_, now - (kd - 1) * D_
            T = [[(a, positions(a, lk, b, cc, skip=sk)) for a in mains], [(a, positions(a, lk, b, cc, skip=sk)) for a in bn],
                 [(a, positions(a, lk, b, cc, skip=sk)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                                   room=(1800, 0.5))
            dres[('cold', (fs, q))].append((p_ / ((cc - b) / D_), u))
        keys_.append(('cold', (fs, q)))
    # OUR OWN ENTRY SWAP (owner 2026-09-30 night): the live desk (cold OR flat, make room) skipping a range copy whose entry
    # swap (pool fee + price impact, value_path) costs more than a share of the copy
    live_sk = lambda r: cold(r, ('volz', 'rv24'), (0.3, 0.1))
    ec = [r['ecost'] for r in allr if r.get('ecost')]
    for cap in (None, 0.02, 0.05, 0.10):
        sk = live_sk if cap is None else (lambda r, cap=cap: live_sk(r) or r.get('ecost', 0) > cap)
        for kd in range(int(days), 0, -1):
            b, cc = now - kd * D_, now - (kd - 1) * D_
            T = [[(a, positions(a, lk, b, cc, skip=sk)) for a in mains], [(a, positions(a, lk, b, cc, skip=sk)) for a in bn],
                 [(a, positions(a, lk, b, cc, skip=sk)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                                   room=(1800, 0.5))
            dres[('ecost', cap)].append((p_ / ((cc - b) / D_), u))
        keys_.append(('ecost', cap))
    res_out.append('  entry swaps: %d of %d copies swap on entry; their cost: median %.2f%%, over 2%%: %d, over 5%%: %d, over 10%%: %d' % (
        len(ec), len(allr), 100 * sorted(ec)[len(ec) // 2] if ec else 0, sum(1 for x in ec if x > 0.02), sum(1 for x in ec if x > 0.05), sum(1 for x in ec if x > 0.10)))
    # A FRESH POOL (owner 2026-10-01: a ZIP copy in a 30-min-old pool lost 98.7% in 6 min and tripped the drawdown stop): the
    # live desk (cold OR flat, make room) skipping copies into pools younger than h hours at entry
    ages = [r['age_h'] for r in allr if r.get('age_h') is not None]
    for h in (None, 1, 3, 6, 24):
        sk = live_sk if h is None else (lambda r, h=h: live_sk(r) or (r.get('age_h') is not None and r['age_h'] < h))
        for kd in range(int(days), 0, -1):
            b, cc = now - kd * D_, now - (kd - 1) * D_
            T = [[(a, positions(a, lk, b, cc, skip=sk)) for a in mains], [(a, positions(a, lk, b, cc, skip=sk)) for a in bn],
                 [(a, positions(a, lk, b, cc, skip=sk)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                                   room=(1800, 0.5))
            dres[('fresh', h)].append((p_ / ((cc - b) / D_), u))
        keys_.append(('fresh', h))
    young = lambda h: [r for r in allr if r.get('age_h') is not None and r['age_h'] < h]
    res_out.append('  fresh pools: copies into pools < 1 h old %d (P&L %+.0f, worst %+.0f), < 6 h %d (%+.0f), < 24 h %d (%+.0f) of %d' % (
        len(young(1)), sum(r['res']['none'][0] for r in young(1)), min([r['res']['none'][0] for r in young(1)] or [0]),
        len(young(6)), sum(r['res']['none'][0] for r in young(6)), len(young(24)), sum(r['res']['none'][0] for r in young(24)), len(allr)))
    # THE DRAWDOWN STOP AND THE LAUNCH SIZE GUARD (owner 2026-10-01, after ZIP: a launch dump of one copy tripped the stop, which
    # closed every copy and halted the desk until a human resumed it): the live desk run as ONE continuous stretch (a stop
    # and a pause cross days), each variant's P&L booked by close day
    led_runs = {}
    t0r, t1r = now - days * D_, now
    T0 = lambda yc=False: [[(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=yc)) for a in mains], [(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=yc)) for a in bn],
                           [(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=yc)) for a in fl]]
    thr = float(c.get('stop_drawdown', 0.3)) * book
    for nm, ddv, yc in (('no stop', None, False), ('stop: exit all, 2 h down', (thr, 'exit', 7200, 1), False), ('stop: exit all, 6 h down', (thr, 'exit', 21600, 1), False),
                        ('stop: pause 1 h, keep copies', (thr, 'pause', 3600, 1), False), ('stop: pause 3 h, keep copies', (thr, 'pause', 10800, 1), False),
                        ('stop: exit all 2 h, only if >= 3 losers', (thr, 'exit', 7200, 3), False), ('stop: pause 1 h, only if >= 3 losers', (thr, 'pause', 3600, 3), False),
                        ('no stop + launch size guard', None, True)):
        led = []
        simulate_tiers(T0(yc), size, book, gas, t0r, t1r, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                       slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                       cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                       room=(1800, 0.5), ledger=led, dd=ddv, match=1.0 if yc else 0.0)
        per = [sum(v for ts, v in led if now - (kd + 1) * D_ <= ts < now - kd * D_) for kd in range(int(days) - 1, -1, -1)]
        run_ = sorted(led)
        cum, pk, mdd = 0.0, 0.0, 0.0
        for ts, v in run_:
            cum += v; pk = max(pk, cum); mdd = max(mdd, pk - cum)
        led_runs[nm] = per
        res_out.append('  STOP %-40s total %+8.0f  mean %+7.2f/day  worst day %+7.2f  max drawdown %6.0f   %s' % (
            nm, sum(per), sum(per) / len(per), min(per), mdd, ' '.join('%+.0f' % x for x in per)))
    # THE LAUNCH CREATOR GUARD (owner 2026-10-03, "these drawdowns and losses cannot happen again"): in a pool under H hours old
    # whose creator (its first PositionManager LP) has not pulled liquidity, a copy at frac x our size (0 = skipped) - and the same
    # for EVERY copy in a pool under H hours (creator ignored). The live desk as it runs (launch guard, the 3 h pause stop)
    # + CONCENTRATION (the same night): smaller copies, more slots - (size x, free div, slots)
    sz0, dv0, sl0 = size, float(c.get('size_free_div', 0) or 0), int(c['max_open'])
    for nm, cg, sx in (('live (launch guard only)', None, None), ('creator-in <1h at 1/2', (0.5, 1, True), None), ('creator-in <1h at 1/4', (0.25, 1, True), None),
                   ('creator-in <1h at 1/10', (0.1, 1, True), None), ('creator-in <1h skipped', (0.0, 1, True), None), ('creator-in <2h skipped', (0.0, 2, True), None),
                   ('creator-in <6h skipped', (0.0, 6, True), None), ('every pool <1h at 1/4', (0.25, 1, False), None), ('every pool <1h skipped', (0.0, 1, False), None),
                   ('copies 10% of book, no free/div, 10 slots', None, (0.5, 0, 10)), ('copies 10%, free/4, 10 slots', None, (0.5, 4, 10)),
                   ('copies 13%, free/3, 8 slots', None, (0.67, 3, 8)), ('copies 15%, free/3, 7 slots', None, (0.75, 3, 7)),
                   ('copies 10%, 10 slots + creator-in <1h at 1/2', (0.5, 1, True), (0.5, 0, 10)),
                   ('copies 13%, 8 slots + creator-in <1h at 1/2', (0.5, 1, True), (0.67, 3, 8))):
        led = []
        size_, div_, slots_ = (sz0 * sx[0], sx[1], sx[2]) if sx else (sz0, dv0, sl0)
        T_ = [[(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=True, creator=cg)) for a in g] for g in (mains, bn, fl)]
        simulate_tiers(T_, size_, book, gas, t0r, t1r, reserve=int(c.get('reserve', 0)), div=div_,
                       slots=slots_, per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                       cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                       room=(1800, 0.5), ledger=led, dd=(thr, 'pause', 10800, 1), match=1.0)
        per = [sum(v for ts, v in led if now - (kd + 1) * D_ <= ts < now - kd * D_) for kd in range(int(days) - 1, -1, -1)]
        cum, pk, mdd, worst = 0.0, 0.0, 0.0, 0.0
        for ts, v in sorted(led):
            cum += v; pk = max(pk, cum); mdd = max(mdd, pk - cum); worst = min(worst, v)
        b0_ = led_runs.setdefault('launch live', per)
        res_out.append('  LAUNCH %-30s total %+8.0f  mean %+7.2f/day  worst day %+7.2f  max drawdown %6.0f  worst copy %+7.2f  ahead %d/%d   %s' % (
            nm, sum(per), sum(per) / len(per), min(per), mdd, worst, sum(1 for x, y in zip(per, b0_) if x > y + 0.5), len(per), ' '.join('%+.0f' % x for x in per)))
    # THE SQUAD SHAPE (owner 2026-10-01, research/deskscan: bench and specialists held ~half the capital-hours at a loss; on
    # desk_7d one copy per bench wallet and no specialists won 6/6 shaken runs): the live desk as it runs now (launch guard,
    # the 3 h pause stop) against the bench limit and the specialists, one continuous run each
    spec = {a for a in hrs}
    for nm, bpw, nospec, div_ in (('live (bench %s a wallet, specialists on)' % c.get('bench_per_wallet'), int(c.get('bench_per_wallet', 0) or 0), False, None),
                                  ('bench 1 a wallet', 1, False, None), ('no specialists', int(c.get('bench_per_wallet', 0) or 0), True, None),
                                  ('bench 1 a wallet + no specialists', 1, True, None),
                                  ('bench 1 a wallet + no specialists + free/1.5', 1, True, 1.5),
                                  ('bench 1 a wallet + no specialists + free/1.75', 1, True, 1.75)):
        led = []
        bn_ = [a for a in bn if not (nospec and a in spec)]
        T_ = [[(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=True)) for a in mains],
              [(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=True)) for a in bn_],
              [(a, positions(a, lk, t0r, t1r, skip=live_sk, young_cap=True)) for a in fl]]
        simulate_tiers(T_, size, book, gas, t0r, t1r, reserve=int(c.get('reserve', 0)), div=div_ or float(c.get('size_free_div', 0) or 0),
                       slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                       cool_h=float(c.get('cool_h', 24)), per_wallet={1: bpw}, min_frac=0.5, room=(1800, 0.5), ledger=led,
                       dd=(thr, 'pause', 10800, 1), match=1.0)
        per = [sum(v for ts, v in led if now - (kd + 1) * D_ <= ts < now - kd * D_) for kd in range(int(days) - 1, -1, -1)]
        cum, pk, mdd = 0.0, 0.0, 0.0
        for ts, v in sorted(led):
            cum += v; pk = max(pk, cum); mdd = max(mdd, pk - cum)
        b0 = led_runs.setdefault('squad live', per)
        res_out.append('  SQUAD %-46s total %+8.0f  mean %+7.2f/day  worst day %+7.2f  max drawdown %6.0f  ahead %d/%d   %s' % (
            nm, sum(per), sum(per) / len(per), min(per), mdd, sum(1 for x, y in zip(per, b0) if x > y + 0.5), len(per), ' '.join('%+.0f' % x for x in per)))
    # THE WALLET LIMIT AND DEAD CAPITAL (owner 2026-09-30: 0x7777's 16 mints refused at its limit of 2 while one of its two
    # copies had earned $0 in 54 min): the live desk (last slot 50% + make room 30 min / 0.5%/h), with and without a wallet
    # at its limit closing its own dead copy first
    for wr in (None, (30, 0.5), (15, 0.5), (60, 0.5), (30, 0.25), (30, 1.0)):
        for kd in range(int(days), 0, -1):
            b, cc = now - kd * D_, now - (kd - 1) * D_
            T = [[(a, positions(a, lk, b, cc)) for a in mains], [(a, positions(a, lk, b, cc)) for a in bn], [(a, positions(a, lk, b, cc)) for a in fl]]
            p_, u = simulate_tiers(T, size, book, gas, b, cc, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                                   slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=int(c.get('cool_n', 8)),
                                   cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                                   room=(1800, 0.5), wroom=(wr[0] * 60, wr[1]) if wr else None)
            dres[('wroom', wr)].append((p_ / ((cc - b) / D_), u))
        keys_.append(('wroom', wr))
    bn_ = [x for x, _ in dres['none']]
    res_out.append('  desk level (live room rule, live squad, take rate + trust, each day on its own):')
    for k in keys_:
        ps = [x for x, _ in dres[k]]
        res_out.append('  %-18s mean %+8.2f/day  worst %+8.2f  beats none %d/%d   per day %s' % (name(k), S.mean(ps), min(ps),
                       sum(1 for x, y in zip(ps, bn_) if x > y + 0.005), len(ps), ' '.join('%+.0f' % x for x in ps)))
    # THE SKIPS (owner 2026-10-01: "backtest if any of the skips were questionable"): the live desk as it runs (bench 1 a wallet,
    # no specialists, the 3 h pause, launch guard), one continuous run per variant: the cool-off needing a real net loss (a main's
    # copy was refused on 8 flat copies netting -$0.0001), the thin bar raised / off (0x651e: 53-95% of the pool), price-flat off
    flat_off = lambda r: cold(r, ('volz',), (0.3,))
    cn_ = int(c.get('cool_n', 8))
    for nm, key_, sk_, cn, cm in (('live (cool 8/24h at < $0, thin 50%, cold|flat)', lk, live_sk, cn_, 0.0), ('cool-off off', lk, live_sk, 0, 0.0),
                                  ('cool-off at < -$1', lk, live_sk, cn_, 1.0), ('cool-off at < -$2', lk, live_sk, cn_, 2.0),
                                  ('cool-off at < -$5', lk, live_sk, cn_, 5.0), ('thin 75%', ('thin', 0.75), live_sk, cn_, 0.0),
                                  ('thin off', 'none', live_sk, cn_, 0.0), ('price-flat off (cold only)', lk, flat_off, cn_, 0.0)) + \
                                 tuple(('falling knife: skip if the token fell > %d%% in the hour before' % (100 * kv), lk,
                                        (lambda r, kv=kv: live_sk(r) or r['res'].get(('knife', kv)) == 'skip'), cn_, 0.0) for kv in (0.10, 0.20, 0.30, 0.50)):
        led = []
        T_ = [[(a, positions(a, key_, t0r, t1r, skip=sk_, young_cap=True)) for a in g] for g in (mains, bn, fl)]
        simulate_tiers(T_, size, book, gas, t0r, t1r, reserve=int(c.get('reserve', 0)), div=float(c.get('size_free_div', 0) or 0),
                       slots=int(c['max_open']), per_token=int(c.get('max_per_token', 0) or 0), cool_n=cn, cool_min=cm,
                       cool_h=float(c.get('cool_h', 24)), per_wallet={1: int(c.get('bench_per_wallet', 0) or 0)}, min_frac=0.5,
                       room=(1800, 0.5), ledger=led, dd=(thr, 'pause', 10800, 1), match=1.0)
        per = [sum(v for ts, v in led if now - (kd + 1) * D_ <= ts < now - kd * D_) for kd in range(int(days) - 1, -1, -1)]
        cum, pk, mdd = 0.0, 0.0, 0.0
        for ts, v in sorted(led):
            cum += v; pk = max(pk, cum); mdd = max(mdd, pk - cum)
        b0 = led_runs.setdefault('skips live', per)
        res_out.append('  SKIP %-48s total %+8.0f  mean %+7.2f/day  worst day %+7.2f  max drawdown %6.0f  ahead %d/%d   %s' % (
            nm, sum(per), sum(per) / len(per), min(per), mdd, sum(1 for x, y in zip(per, b0) if x > y + 0.5), len(per), ' '.join('%+.0f' % x for x in per)))
    # THE NIGHT OF 2026-10-02 (owner: "we gave lots of our profits away by a few bad copies - prevent them without giving up
    # upside, backtested, be experimental"): six copies lost $196 after the realized high (SI range -$52.94 in a crashing
    # look-alike token, MOONLET -$41.41 at 7.9x its leader, CME -$37.50 closed by 'make room' while its leader held, MOONLET
    # -$33.49, SGT -$16.62 at 2.3x, VEIL -$14.26). Each candidate is the live desk with ONE change, one continuous run:
    #   size cap      a copy never above capk x the leader's own $ (the launch guard does this at 1x for pools < 1 h old)
    #   young TOKEN   the token's first swap in ANY pool < h hours ago: capped at the leader's $, or skipped, or ranges skipped
    #   room floor    'make room' never closes a copy below -x% (it is held to its leader's exit)
    #   $ per token   at most f of the capital in one token
    #   leader $      skip leaders risking under $X (conviction)
    #   exits         crash / fell-through / timeout on the copy's own path
    night = [('live', dict())]
    night += [('promote: an upper idle %gh -> the next tier steps up' % h_, dict(promote=h_ * 3600)) for h_ in (2, 4, 6, 12, 24)]
    night += [('bench 2 copies a wallet', dict(bpw=2)), ('bench 2 a wallet + promote 6h', dict(bpw=2, promote=6 * 3600))]
    # EXPERIMENTS (owner 2026-10-02, "experimental, without giving up other things"): filters on what the desk already sees at
    # entry - the pool's last 24 h (research/lpfeat), the other squad wallets, the hour, the leader's own habits. Inspired by
    # the LP literature: adverse selection of bids in one-sided flow, LP skill persisting, active vs passive LPs.
    for a_, v_r in rows.items():
        for r in v_r:
            r['_a'] = a_
    allr = [r for v_r in rows.values() for r in v_r]
    fv = lambda r, k_: (r.get('feat') or {}).get(k_)

    def qv(k_, p_):
        xs = sorted(x for x in (fv(r, k_) for r in allr) if x is not None)
        return xs[int(p_ * (len(xs) - 1))] if xs else None
    bytok = collections.defaultdict(list)
    for r in allr:
        bytok[r['token']].append((r['t0'], r['_a']))
    cons = lambda r, w: any(r['t0'] - w <= t_ < r['t0'] and a_ != r['_a'] for t_, a_ in bytok[r['token']])
    medlead = {a_: S.median([r['lead'] for r in v_r]) for a_, v_r in rows.items() if v_r}
    q = dict(rv50=qv('rv24', 0.5), ret30=qv('retail24', 0.3), trn30=qv('turnret24', 0.3), pop30=qv('pop24', 0.3))
    lt = lambda x, y: x is not None and y is not None and x < y
    night += [('EXP bids skipped in sell flow (imb24 < -0.2)', dict(ska=lambda r: not r.get('rng') and lt(fv(r, 'imb24'), -0.2))),
              ('EXP bids skipped in sell flow (imb24 < -0.4)', dict(ska=lambda r: not r.get('rng') and lt(fv(r, 'imb24'), -0.4))),
              ('EXP token down > 30% in 24h skipped', dict(ska=lambda r: lt(fv(r, 'mom24'), -0.357))),
              ('EXP token down > 50% in 24h skipped', dict(ska=lambda r: lt(fv(r, 'mom24'), -0.693))),
              ('EXP retail share bottom 30% skipped', dict(ska=lambda r: lt(fv(r, 'retail24'), q['ret30']))),
              ('EXP retail turnover bottom 30% skipped', dict(ska=lambda r: lt(fv(r, 'turnret24'), q['trn30']))),
              ('EXP fillers: only with a 2nd squad wallet in the token (2h)', dict(skf=lambda r: not cons(r, 7200))),
              ('EXP fillers: only with a 2nd squad wallet in the token (24h)', dict(skf=lambda r: not cons(r, 86400))),
              ('EXP fillers off 00-06 UTC', dict(skf=lambda r: time.gmtime(r['t0']).tm_hour < 6)),
              ('EXP fillers: leader above its own median size', dict(skf=lambda r: r['lead'] < medlead.get(r['_a'], 0))),
              ('EXP fillers: volatile pools only (rv24 >= median)', dict(skf=lambda r: lt(fv(r, 'rv24'), q['rv50'])))]
    # ROUND 2 (owner 2026-10-02, the same day; squad agreement for fillers is LIVE, so it is in every variant's base below):
    # the leader's own recent copies (skill persists, so does a cold patch), pool age, our share, entry cost, hot pools,
    # agreement for the bench, take profit (optimal-stopping theory says an LP should not exit early - checked here)
    outc = collections.defaultdict(list)
    for r in allr:
        o_ = r['res'].get(lk)
        if o_ and o_ != 'skip':
            outc[r['_a']].append((o_[1], o_[0]))
    for v_o in outc.values():
        v_o.sort()

    def cold_hand(r, n_, all_lose):
        prev = [pl for te, pl in outc.get(r['_a'], []) if te < r['t0']][-n_:]
        return len(prev) == n_ and (all(x < 0 for x in prev) if all_lose else sum(prev) < 0)
    n_agree = lambda r, w: len({a_ for t_, a_ in bytok[r['token']] if r['t0'] - w <= t_ < r['t0'] and a_ != r['_a']})
    night += [('R2 bench also needs squad agreement (2h)', dict(skb=lambda r: not cons(r, 7200))),
              ('R2 fillers need 2 other squad wallets (2h)', dict(skf=lambda r: n_agree(r, 7200) < 2)),
              ('R2 cold hand: leader\'s last 3 copies net < 0, skipped', dict(ska=lambda r: cold_hand(r, 3, False))),
              ('R2 cold hand: leader\'s last 3 copies all lost, skipped', dict(ska=lambda r: cold_hand(r, 3, True))),
              ('R2 fillers: pool trading >= 6 h', dict(skf=lambda r: r.get('age_h') is not None and r['age_h'] < 6)),
              ('R2 fillers: pool trading >= 24 h', dict(skf=lambda r: r.get('age_h') is not None and r['age_h'] < 24)),
              ('R2 fillers: our share of the pool <= 25%', dict(skf=lambda r: (r.get('share') or 0) > 0.25)),
              ('R2 ranges with entry cost > 1% skipped', dict(ska=lambda r: r.get('rng') and (r.get('ecost') or 0) > 0.01)),
              ('R2 ranges with entry cost > 0.5% skipped', dict(ska=lambda r: r.get('rng') and (r.get('ecost') or 0) > 0.005)),
              ('R2 ranges with entry cost > 2% skipped', dict(ska=lambda r: r.get('rng') and (r.get('ecost') or 0) > 0.02)),
              ('R2 ranges with entry cost > 3% skipped', dict(ska=lambda r: r.get('rng') and (r.get('ecost') or 0) > 0.03)),
              ('R2 fillers: hot pools only (volz >= 0.5)', dict(skf=lambda r: lt(fv(r, 'volz'), 0.5))),
              ('R2 exit: take profit at +5%', dict(key=('tp', 0.05))),
              ('R2 exit: take profit at +10%', dict(key=('tp', 0.10))),
              ('R2 exit: take profit at +20%', dict(key=('tp', 0.20)))]
    night += [('RANGE copies capped at the leader', dict(rng_cap=True)), ('RANGE copies from mains only', dict(rng_mains=True)),
              ('RANGE copies from mains only, capped at the leader', dict(rng_mains=True, rng_cap=True))]
    night += [('size cap %gx the leader' % k_, dict(capk=k_)) for k_ in (1.0, 1.5, 2.0, 3.0)]
    night += [('young token < %dh: capped at the leader' % h_, dict(young_tok=h_)) for h_ in (6, 24, 72)]
    night += [('young token < %dh: skipped' % h_, dict(skip_young=h_)) for h_ in (6, 24)]
    night += [('young token < %dh: RANGE copies skipped' % h_, dict(skip_young_rng=h_)) for h_ in (24, 72)]
    night += [('RANGE copies skipped (all)', dict(skip_rng=True))]
    night += [('room floor: never close below -%d%%' % x_, dict(room_floor=x_)) for x_ in (10, 20)]
    night += [('$ per token <= %d%% of the book' % (100 * f_), dict(tok_frac=f_)) for f_ in (0.3, 0.5)]
    night += [('leader floor $%d' % x_, dict(lead_min=x_)) for x_ in (25, 40, 50, 75, 100, 125, 150, 200)]
    night += [('exit: %s' % nm_, dict(key=kk)) for nm_, kk in (('token crash 20% in 10 min', ('crash', 0.2)), ('fell 10% through the range', ('fellthrough', 0.1)),
                                                               ('timeout 60 min under entry', ('timeout', 60)))]
    # ABLATION (owner 2026-10-02, "which rules do nothing or even harm - simplify"): the live desk with ONE rule switched off.
    # A rule earns its place only if the desk without it does worse
    night += [('ABL without %s' % n_, dict(abl={k_})) for k_, n_ in (
        ('thin', 'the thin-pool skip (50%)'), ('cold', 'the cold + price-flat pool skips'), ('flat', 'the price-flat skip (cold kept)'),
        ('agree', 'squad agreement for fillers'), ('ecost', 'the entry-cost skip (3%)'), ('cool', 'the token cool-off (8 copies / 24 h)'),
        ('tokcap', 'the per-token cap (4)'), ('bpw', 'the bench wallet limit (1)'), ('launch', 'the launch size guard'),
        ('room', 'the make-room rule'), ('dd', 'the drawdown stop (3 h pause)'), ('floor', 'the $100 leader floor (-> $10)'),
        ('lastslot', 'the last-slot rule (50% of a base copy)'), ('div', 'free/2 sizing (-> base size)'))]
    # SIMPLER (owner 2026-10-02): the rules the ablation found idle or harmful, taken out together, and the leader floor's
    # neighbours now that squad agreement and the entry-cost skip filter the weak copies
    night += [('SIM leader floor $%d' % x_, dict(floor_v=float(x_))) for x_ in (10, 25, 50, 75)]
    # THE TOKEN COOL-OFF ON ITS OWN (owner 2026-10-02: "proved, or not tested properly?"), at the live floor: off, and the
    # loss it needs before it blocks (live: any net loss of the last cool_n copies), and how many copies it looks at
    night += [('COOL off', dict(abl={'cool'})), ('COOL blocks only below -$1', dict(cool_min=1.0)), ('COOL blocks only below -$5', dict(cool_min=5.0)),
              ('COOL blocks only below -$20', dict(cool_min=20.0)), ('COOL last 5 copies', dict(cool_n=5)), ('COOL last 12 copies', dict(cool_n=12))]
    # THE GRID (owner 2026-10-02: "all different combinations, a lot"): floor x entry cost x squad agreement x cool-off
    for fl_ in (50, 75, 100):
        for ec_ in (0.0, 0.02, 0.03):
            for ag_ in (True, False):
                for co_ in (True, False):
                    night.append(('GRID floor $%d | entry cost %s | agreement %s | cool-off %s' % (fl_, '%g%%' % (100 * ec_) if ec_ else 'off',
                                  'on' if ag_ else 'off', 'on' if co_ else 'off'),
                                  dict(floor_v=float(fl_), ec=ec_, abl=set(([] if ag_ else ['agree']) + ([] if co_ else ['cool'])))))
    # RANDOM SETUPS (owner 2026-10-02: "we have more than 36 combinations"): 100 complete desks, each rule off with p = 0.3,
    # the floor and the entry cost drawn too; seeded, so the same 100 every run
    import random as _rnd
    _g = _rnd.Random(20261002)
    _sw = ('thin', 'cold', 'flat', 'agree', 'ecost', 'cool', 'tokcap', 'bpw', 'launch', 'room', 'dd', 'lastslot', 'div')
    for i_ in range(100):
        off_ = sorted(k_ for k_ in _sw if _g.random() < 0.3)
        fl_, ec_ = _g.choice((25, 50, 75, 100)), _g.choice((0.0, 0.02, 0.03))
        night.append(('RND%03d floor $%d ec %s off: %s' % (i_, fl_, '%g%%' % (100 * ec_) if ec_ else 'off', ','.join(off_) or '-'),
                      dict(floor_v=float(fl_), ec=ec_, abl=set(off_))))
    night += [('SIM simplified (no launch guard, cool-off, price-flat)', dict(abl={'launch', 'cool', 'flat'}))]
    night += [('SIM simplified + leader floor $%d' % x_, dict(abl={'launch', 'cool', 'flat'}, floor_v=float(x_))) for x_ in (10, 25, 50)]
    only_ = os.environ.get('LP_NIGHT_ONLY')                       # a quick rerun of some variants: LP_NIGHT_ONLY='entry cost'
    night = [x for x in night if not only_ or x[0] == 'live' or only_ in x[0]]
    ec_live = float(c.get('max_entry_cost', 0) or 0)
    for nm, v_ in night:
        abl = v_.get('abl') or set()
        key_ = v_.get('key', lk)
        if 'thin' in abl:
            key_ = 'none'
        sk_ = (lambda r: False) if 'cold' in abl else flat_off if 'flat' in abl else live_sk
        ec_v = v_['ec'] if 'ec' in v_ else ec_live
        if ec_v and 'ecost' not in abl:                               # LIVE: the entry-cost skip (a GRID run sets its own)
            sk_ = (lambda r, f=sk_, x=ec_v: f(r) or bool(r.get('rng') and (r.get('ecost') or 0) > x))
        if key_ not in (lk, 'none'):                                  # an exit rule: on top of the live thin-pool skip
            sk_ = (lambda r, f=sk_: f(r) or r['res'].get(('thin', 0.50)) == 'skip')
        if v_.get('skip_young'):
            sk_ = (lambda r, f=sk_, h=v_['skip_young']: f(r) or (r.get('tok_age_h') is not None and r['tok_age_h'] < h))
        if v_.get('skip_young_rng'):
            sk_ = (lambda r, f=sk_, h=v_['skip_young_rng']: f(r) or (r.get('rng') and r.get('tok_age_h') is not None and r['tok_age_h'] < h))
        if v_.get('skip_rng'):
            sk_ = (lambda r, f=sk_: f(r) or bool(r.get('rng')))
        if v_.get('lead_min'):
            sk_ = (lambda r, f=sk_, x=v_['lead_min']: f(r) or r['lead'] < x)
        if v_.get('ska'):
            sk_ = (lambda r, f=sk_, g=v_['ska']: f(r) or g(r))
        led = []
        sk_lo = (lambda r, f=sk_: f(r) or bool(r.get('rng'))) if v_.get('rng_mains') else sk_     # bench + filler: no range copies
        cons_h = 0.0 if 'agree' in abl else float(c.get('filler_consensus_h') or 0)
        sk_f0 = (lambda r, f=sk_lo, w=cons_h * 3600: f(r) or not cons(r, w)) if cons_h else sk_lo   # LIVE: squad agreement
        sk_fl = (lambda r, f=sk_f0, g=v_['skf']: f(r) or g(r)) if v_.get('skf') else sk_f0        # fillers only
        sk_b = (lambda r, f=sk_lo, g=v_['skb']: f(r) or g(r)) if v_.get('skb') else sk_lo         # bench only
        T_ = [[(a, positions(a, key_, t0r, t1r, skip=sk_ if g is mains else sk_fl if g is fl else sk_b, young_cap='launch' not in abl,
                             capk=v_.get('capk'), young_tok=v_.get('young_tok'), rng_cap=v_.get('rng_cap', False),
                             floor=10.0 if 'floor' in abl else v_.get('floor_v'))) for a in g] for g in (mains, bn, fl)]
        simulate_tiers(T_, size, book, gas, t0r, t1r, reserve=int(c.get('reserve', 0)),
                       div=0.0 if 'div' in abl else float(c.get('size_free_div', 0) or 0),
                       slots=int(c['max_open']), per_token=0 if 'tokcap' in abl else int(c.get('max_per_token', 0) or 0),
                       cool_n=0 if 'cool' in abl else v_.get('cool_n', cn_), cool_min=v_.get('cool_min', 0.0), cool_h=float(c.get('cool_h', 24)),
                       per_wallet={1: 0 if 'bpw' in abl else int(v_.get('bpw') or c.get('bench_per_wallet', 0) or 0)},
                       min_frac=1.0 if 'lastslot' in abl else 0.5, room=None if 'room' in abl else (1800, 0.5), ledger=led,
                       dd=None if 'dd' in abl else (thr, 'pause', 10800, 1), match=0.0 if 'launch' in abl else 1.0,
                       room_floor=v_.get('room_floor'), tok_frac=v_.get('tok_frac'), promote=v_.get('promote'))
        per = [sum(v for ts, v in led if now - (kd + 1) * D_ <= ts < now - kd * D_) for kd in range(int(days) - 1, -1, -1)]
        cum, pk, mdd = 0.0, 0.0, 0.0
        for ts, v in sorted(led):
            cum += v; pk = max(pk, cum); mdd = max(mdd, pk - cum)
        b0 = led_runs.setdefault('night live', per)
        ups = sum(v for _, v in led if v > 0)
        res_out.append('  NIGHT %-44s total %+8.0f  mean %+7.2f/day  worst day %+7.2f  max dd %5.0f  ahead %d/%d  wins kept $%7.0f   %s' % (
            nm, sum(per), sum(per) / len(per), min(per), mdd, sum(1 for x, y in zip(per, b0) if x > y + 0.5), len(per), ups, ' '.join('%+.0f' % x for x in per)))
    for l in res_out:
        log(l)
    return res_out

