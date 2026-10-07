"""THE PICK'S LIVE REPLAY (owner 2026-10-04: "replay should just look at rules that exist on live and apply them").

lp pick judged wallets with lpopt.desk_7d: replayed copies as (open, close, %), through the slots, sizing and token rules -
but none of the rules that act on a copy's PATH or its POOL. This replays every candidate's copies from the history store
with their value paths (lpcopy.value_path at the live copy lag per kind of copy, lp tune downside's method) and runs them
through the desk's rules as [lp.follow] has them now:
  entry   min_leader_usd, min_pool_age_h (pool age from the store), cold_volz / flat_rv24 (the pool's hour volume z-score and
          24 h volatility before entry, research/lpfeat), max_entry_cost (our entry swap), thin_max_share (our share of the
          pool's active liquidity), quiet-hour specialists ([lp.follow.hours]), squad agreement for a filler copy
          (filler_consensus_h: another squad wallet minted the token in the hours before)
  desk    max_open, size_frac / size_free_div, last_slot_frac, the tiers and their room, bench_per_wallet, max_per_token,
          cool_n / cool_h, make-room (room_waste_min / room_waste_rate on the fee path), the drawdown pause (stop_drawdown,
          stop_pause_h) - one continuous run, booked by close day
Not modelled: reverts, a price crossing the range before our mint, leader exits we miss. No take rate: the rules it stood
in for are modelled here (lpopt.TAKE counted a bench wallet's 1-open-copy limit twice).
`python -m copydesk.lplive [wallet ...]` prints each wallet's booked $/day as the only bench beside the mains."""
import bisect, math, sys, time, zlib

HOUR_BLOCKS = 36_000
_ROWS = {}


def _ctx(chain):
    from copydesk import lpcopy as C, lpfollow as L
    from research import lpfeat as FE
    R = C._hist()
    C._min_leader()
    return C, L, FE, R


def _cache_path(chain):
    import os
    from copydesk import lpfollow as L
    return os.path.join(L._dir(chain), 'lplive_rows.pkl')


def rows(chain, wallets, days, size, log=None):
    """wallet -> its replayed copies over `days` with what the live gates need. A closed copy never changes, so every row is kept
    on disk (var/lp/lplive_rows.pkl, per wallet and copy size rounded to $5) and a run replays only what closed since the last
    one (lpcopy.run_store since=): ~5-8 min for 145 wallets from cold, seconds after."""
    import os, pickle
    C, L, FE, R = _ctx(chain)
    size = max(5.0, round(size / 5.0) * 5.0)                        # the cache key: a few % of size changes no copy's %
    lo, hi = R.span()
    now = R.tsof(hi)
    if not _ROWS:
        try:
            _ROWS.update(pickle.load(open(_cache_path(chain), 'rb')))
        except (OSError, EOFError, pickle.UnpicklingError):
            pass
    disk = _ROWS
    first = dict(R.con.execute('SELECT pool, MIN(blk) FROM swaps GROUP BY pool').fetchall())
    n_of = {v[0]: k for k, v in R.pid.items()}
    pc = {}

    def feat(pid, key_, b0):
        if pid not in pc:
            sw_ = R.swaps([(pid, max(lo, b0 - 80 * HOUR_BLOCKS), hi)]).get(pid) or []
            pc[pid] = (sw_, [x[0] for x in sw_], FE.pool_hours(R, pid, key_, sw_, 0, 0))
        sw_, bl_, hrs_ = pc[pid]
        i = bisect.bisect_right(bl_, b0) - 1
        if i < 0 or not sw_[i][4]:
            return {}
        return FE.features(hrs_, int(R.tsof(b0) // 3600), sw_[i][4], sw_[i][5], key_, R, b0) or {}

    ml_live, C._NAT['min'] = C._NAT['min'], 0.0                   # every leader size in; min_leader_usd is a gate below
    dirty = False
    try:
        for n_, a in enumerate(wallets):
            k_ = (a, size)
            ent = disk.get(k_) or dict(upto=None, rows=[])
            if ent['upto'] == now:
                continue
            for i in range(5):                                      # exit payouts read receipts once per position: public
                try:                                                # nodes throttle a cold wallet (429); a pick on a missing
                    C._PREP.clear()                                 # wallet would be a wrong pick, so the last failure raises
                    ok_ = C.run_store(a, days, ['live'], size=size, since=ent['upto'])
                    break
                except RuntimeError:
                    if i == 4:
                        raise
                    time.sleep(10 * (i + 1))
            if ok_ is None:
                raise RuntimeError('the history store does not cover %g days' % days)
            D, done, pos, key, sw = list(C._PREP.values())[0]
            have = {r['id'] for r in ent['rows']}
            opens = sorted(q['open'] for q in pos)
            for p in pos:
                pid_ = '%s' % (p.get('tokenIds') or (p['open'], p['legs'][0][0]),)
                if pid_ in have:
                    continue
                d = C.lag_table()[C.copy_kind(p, key, sw, opens)]
                r = C.value_path(p, key, sw, D['dec'], D['eusd'], d, size, detail=True)
                if not r:
                    continue
                pts, tin, share, lead, ecost = r
                b0 = p['open'] + d
                pid = p['legs'][0][0]
                f0 = first.get(n_of.get(pid))
                # a pool whose first swap in the store is within 2 h of the store's start was born before it: old
                age = None if f0 is None or f0 <= lo + 2 * HOUR_BLOCKS else (b0 - f0) / HOUR_BLOCKS
                ft = feat(pid, key[pid], b0)
                tsp = [D['tsof'](x[0]) for x in pts]
                path = ([tsp[0]] + tsp[1:], [0.0] + [100 * (x[1] - tin) / tin for x in pts[1:]], [0.0] + [100 * x[2] / tin for x in pts[1:]])
                ent['rows'].append(dict(id=pid_, t0=D['tsof'](b0), te=D['tsof'](p['close'] + d), tin=tin, pnl=pts[-1][1] - tin, lead=lead,
                                        share=share, ecost=ecost / tin if tin else 0.0, age_h=age, token=C._token(key[pid]), path=path,
                                        volz=ft.get('volz'), rv24=ft.get('rv24')))
            ent['rows'] = [r for r in ent['rows'] if r['t0'] >= now - 16 * 86400]     # the store keeps 14 days
            ent['upto'] = now
            disk[k_] = ent
            dirty = True
            if log and n_ % 25 == 24:
                log('   live replay: %d of %d wallets' % (n_ + 1, len(wallets)))
    finally:
        C._NAT['min'] = ml_live
        if dirty:
            tmp = _cache_path(chain) + '.%d' % os.getpid()
            pickle.dump(disk, open(tmp, 'wb'))
            os.replace(tmp, _cache_path(chain))
    t0 = now - days * 86400
    return {a: [r for r in disk[(a, size)]['rows'] if r['t0'] >= t0] for a in wallets}


def _trust(wallets):
    from copydesk import lpdb
    con = lpdb.connect()
    tr = {}
    for a in wallets:
        R_, M_ = con.execute('SELECT SUM(real), SUM(pnl0) FROM copy_trades WHERE owner = ? AND real IS NOT NULL', (a,)).fetchone()
        tr[a] = min(M_ / R_, R_ / M_) if R_ and M_ and R_ > 0 and M_ > 0 else 0.0
    con.close()
    return tr


def gate(r, cfg):
    """The live entry rule that refuses this copy, or None (lpfollow.Follow.on_leader_mint's order)."""
    if r['lead'] < float(cfg.get('min_leader_usd', 0) or 0):
        return 'leader'
    ma = float(cfg.get('min_pool_age_h', 0) or 0)
    if ma and r['age_h'] is not None and r['age_h'] < ma:
        return 'young'
    cv, fr = cfg.get('cold_volz'), cfg.get('flat_rv24')
    if cv is not None and r['volz'] is not None and r['volz'] < float(cv):
        return 'cold'
    if fr is not None and r['rv24'] is not None and r['rv24'] < float(fr):
        return 'flat'
    if float(cfg.get('thin_max_share', 0) or 0) and r['share'] > float(cfg['thin_max_share']):
        return 'thin'
    if float(cfg.get('max_entry_cost', 0) or 0) and r['ecost'] > float(cfg['max_entry_cost']):
        return 'ecost'
    return None


def positions(a, rs, cfg, t0, t1, trust, shake=None, agree=None):
    """simulate_tiers' tuples for one wallet: (open, close, in, %, token, leader $, path + trust), gated."""
    from copydesk.lpopt import CAP_PCT
    hrs = {k.lower(): set(v) for k, v in (cfg.get('hours') or {}).items()}
    out = []
    for r in rs:
        if not (t0 <= r['t0'] < t1) or gate(r, cfg):
            continue
        if a in hrs and time.gmtime(r['t0']).tm_hour // 3 * 3 not in hrs[a]:
            continue
        if agree and not agree(a, r):
            continue
        if shake is not None and zlib.crc32(('%s%.0f%d' % (a, r['t0'], shake)).encode()) % 100 < 20:
            continue
        pct = max(-CAP_PCT, min(CAP_PCT, 100.0 * r['pnl'] / r['tin']))
        out.append((r['t0'], r['te'], r['tin'], pct * (trust if pct > 0 else 1.0), r['token'], None, r['path'] + (trust,)))
    return out


def desk(chain, mains, bench, filler, size, book, gas, cfg, days, shake=None, by_wallet=None, log=None, stats=None):
    """$ per day (oldest first) of the desk with these mains, bench and filler, every live rule, one continuous run."""
    from copydesk import lpopt as O
    ws = list(dict.fromkeys(mains + bench + filler))
    rs = rows(chain, ws, days, size, log)
    tr = _trust(ws)
    for a in ws:
        O.COPIED.add(a)                                              # replayed copies: no keep rate on top
    now, D = time.time(), 86400.0
    t0 = now - days * D
    # SQUAD AGREEMENT: a filler copy needs another squad wallet's mint of the token in the filler_consensus_h hours before
    h = float(cfg.get('filler_consensus_h', 0) or 0) * 3600
    mints = {}
    for a in ws:
        for r in rs[a]:
            mints.setdefault(r['token'], []).append((r['t0'], a))
    for v in mints.values():
        v.sort()
    agree = (lambda a, r: any(w != a and r['t0'] - h <= t <= r['t0'] for t, w in mints.get(r['token'], ()))) if h else None
    fl = [a for a in filler if a not in mains and a not in bench]
    T = [[(a, positions(a, rs[a], cfg, t0, now, tr[a], shake, agree if g is fl else None)) for a in g] for g in
         (mains, [a for a in bench if a not in mains], fl)]
    led = []
    stop = float(cfg.get('stop_drawdown', 0) or 0)
    _, util = O.simulate_tiers(T, size, book, gas, t0, now, reserve=int(cfg.get('reserve', 0)), div=float(cfg.get('size_free_div', 0) or 0),
                     slots=int(cfg.get('max_open', 5)), per_token=int(cfg.get('max_per_token', 0) or 0), cool_n=int(cfg.get('cool_n', 8)),
                     cool_h=float(cfg.get('cool_h', 24)), per_wallet={1: int(cfg.get('bench_per_wallet', 0) or 0)},
                     min_frac=float(cfg.get('last_slot_frac', 1) or 1), ledger=led, by_wallet=by_wallet,
                     room=(float(cfg['room_waste_min']) * 60, float(cfg['room_waste_rate'])) if cfg.get('room_waste_min') else None,
                     dd=(stop * book, 'pause', float(cfg.get('stop_pause_h', 3)) * 3600, 1) if stop else None)
    if stats is not None:                                            # capital at work (0-1) and the max drawdown of the run
        cum = pk = mdd = 0.0
        for ts, v in sorted(led):
            cum += v
            pk = max(pk, cum)
            mdd = max(mdd, pk - cum)
        stats.update(util=util, dd=mdd, copies=sum(len(x) for g in T for _, x in g))
    return [sum(v for ts, v in led if now - (k + 1) * D <= ts < now - k * D) for k in range(int(days) - 1, -1, -1)]


def _selftest():
    cfg = dict(min_leader_usd=10, min_pool_age_h=2, cold_volz=-0.2, flat_rv24=0.03, thin_max_share=0.5, max_entry_cost=0.03)
    r = dict(lead=50, age_h=5, volz=0.1, rv24=0.05, share=0.1, ecost=0.01)
    assert gate(r, cfg) is None
    for k, v, why in (('lead', 5, 'leader'), ('age_h', 1, 'young'), ('volz', -1, 'cold'), ('rv24', 0.01, 'flat'), ('share', 0.9, 'thin'), ('ecost', 0.1, 'ecost')):
        assert gate(dict(r, **{k: v}), cfg) == why, k
    assert gate(dict(r, age_h=None, volz=None, rv24=None), cfg) is None        # unknown reads as passing, as live does
    print('lplive selftest: 8 asserts')


if __name__ == '__main__':
    if sys.argv[1:] == ['selftest']:
        _selftest()
        sys.exit()
    from core import chain as cm
    from copydesk import lpfollow as L
    from copydesk.lpfind import measured_gas
    c = cm.load('robinhood')
    cfg = L.cfg(c)
    size, book = L.live_size(c)
    gas = measured_gas(c) or 0.04
    days = float(cfg.get('pick_days', 7))
    mains = L.leaders(c)
    for a in [x.lower() for x in sys.argv[1:]] or L.bench(c):
        bw = {}
        t = time.time()
        per = desk(c, mains, [a], [], size, book, gas, cfg, days, by_wallet=bw, log=print)
        print('%s  as bench: %+.2f $/day of its own  desk %+.0f over %g d (%s)  %.1fs' % (
            a[:10], bw.get(a, 0.0) / days, sum(per), days, ' '.join('%+.0f' % x for x in per), time.time() - t))
