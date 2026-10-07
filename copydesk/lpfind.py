"""FIND LP WALLETS WORTH COPYING: active, steady, small wins that compound (docs/LPWALLETS.md).

  python -m copydesk.lpfind candidates [--days 3] [--min-per-day 10]      who mints the most v4 positions
  python -m copydesk.lpfind rank [--days 30] [--top 30] [addr ...]       LP P&L per candidate (copydesk/lpwallet.py)
  python -m copydesk.lpfind show                                         the ranking so far

candidates: one hypersync pass over PositionManager mints (Transfer from 0x0) -> positions per recipient; plain wallets
only (a contract recipient is a vault or a router: its P&L is someone else's); the known leaders dropped.
rank: lpwallet.report per candidate, saved as it goes (var/lpfind.json), so a stopped run keeps what it measured.
The score is the owner's brief, in that order of weight:
  - very active: closed positions per active day
  - little drawdown: max drawdown and worst day, each against the total LP P&L
  - track record: green days (after gas) over the days it was active, and the last 14 days positive
  - small wins that compound: median position > 0, the worst 10% shallow, return on the capital it actually uses
A wallet must pass the check (its parts reconcile with its own balance change) to be ranked at all.
"""
import collections, json, os, statistics as S, sys, time

from discovery import forensics as F
from copydesk import lpwallet as W

KNOWN = {'0x78a1003e12f833f04ac176304f8d47720f4948ee', '0x0860cd45bff5900efa93e8830b8a9da0178cff76',
         '0xc3488384c4cbe1ee96d7e3e499f8dfefdc2cef65', '0xe7d8f314443ad2a3f0041c8e9bf727680482932b',
         '0xa2161edcd83e979e806acb550686bc265828bdd9'}
LIMIT_S = 150
OUT = os.path.join(os.path.dirname(F.INDEX), 'lpfind.json')


def candidates(days=3.0, min_per_day=10.0, log=print):
    head = int(W._batch([('eth_blockNumber', [])])[0], 16)
    zero = '0x' + '0' * 64
    n = collections.Counter()
    # our node, 100k-block chunks, 8 in flight: hypersync pages ~1k mints and waits 50 s every ~25 pages on the shared
    # 30/min token - 3 days (~100k mints) took minutes; hypersync stays the fallback when the node is unreachable
    try:
        from concurrent.futures import ThreadPoolExecutor
        from copydesk.lpfollow import _node_batch
        lo = head - int(days * W.DAY)
        q = [(a, min(head, a + 99_999)) for a in range(lo, head + 1, 100_000)]
        get = lambda w: _node_batch([('eth_getLogs', [dict(fromBlock=hex(w[0]), toBlock=hex(w[1]), address=W.POSM, topics=[W.XFR, zero])])],
                                    timeout=120)[0] or []
        with ThreadPoolExecutor(8) as ex:
            for logs in ex.map(get, q):
                for l in logs:
                    n['0x' + l['topics'][2][-40:]] += 1
    except Exception as e:
        log('candidates: node failed (%s), hypersync' % str(e)[:80])
        n.clear()
        body = dict(from_block=head - int(days * W.DAY), logs=[dict(address=[W.POSM], topics=[[W.XFR], [zero]])],
                    field_selection=dict(log=['topic2']))
        for r in F._pages(body, F._key()):
            for d in r['data']:
                for l in d.get('logs') or []:
                    n['0x' + l['topic2'][-40:]] += 1
    top = [a for a, c in n.most_common() if c >= min_per_day * days and a not in KNOWN]
    code = W._batch([('eth_getCode', [a, 'latest']) for a in top])
    out = [(a, n[a]) for a, c in zip(top, code) if c in ('0x', None)]
    log('%d wallets minted >= %.0f positions/day over %g days; %d are plain wallets (contracts dropped)' % (
        len(top), min_per_day, days, len(out)))
    return out


def metrics(R):
    rows = [r for r in R['rows'] if 'realized_usd' in r and r['closed'] and r.get('age_h') is not None and r['invested_usd'] > 0]
    d = [(k, v['usd'] - v['gas']) for k, v in R['daily'].items() if v['n'] > 0]
    if not rows or not d:
        return None
    net = [x for _, x in d]
    cum = peak = dd = 0.0
    for x in net:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    ev = []
    for r in rows:
        ev += [(r['t_open'], r['invested_usd']), (r['t_open'] + r['age_h'] * 3600, -r['invested_usd'])]
    c = cap = 0.0
    for _, v in sorted(ev):
        c += v
        cap = max(cap, c)
    pct = sorted(100 * r['realized_usd'] / r['invested_usd'] for r in rows)
    last = net[-14:]
    pnl = R['lp_pnl']
    return dict(address=R['address'], lp_pnl=round(pnl, 2), positions=len(rows), days=len(d),
                per_day=round(len(rows) / len(d), 1), green=round(sum(x > 0 for x in net) / len(net), 3),
                worst_day=round(min(net), 2), max_dd=round(dd, 2), dd_share=round(-dd / pnl, 3) if pnl > 0 else None,
                last14=round(sum(last), 2), last14_green=round(sum(x > 0 for x in last) / len(last), 3),
                median_pct=round(S.median(pct), 3), p10_pct=round(pct[len(pct) // 10], 2), p1_pct=round(pct[len(pct) // 100], 1),
                peak_capital=round(cap), ret_on_capital=round(pnl / cap, 3) if cap else None,
                size_median=round(S.median(r['invested_usd'] for r in rows)),
                hold_min=round(60 * S.median(r['age_h'] for r in rows), 1),
                reconciled=R['wallet']['reconciled'], unexplained=round(R['wallet']['unexplained'], 1),
                native_skipped=R['skipped'].get('native-ETH pool (not covered)', 0), secs=R['secs'])


def score(m):
    """None = does not qualify; else higher is better. Hard gates first, then a weighted sum of the four asks."""
    # a failed check is allowed only when native-ETH positions explain it (a known coverage gap, and we copy USDG pools
    # only): 2/3 of the first 27 active LPers also use native pools. Such a wallet carries a penalty and a flag.
    if not m or (not m['reconciled'] and not m.get('native_skipped')) or m['lp_pnl'] <= 0 or m['per_day'] < 5 or m['days'] < min(10, 0.5 * m.get('days_window', 30)):
        return None
    if m['green'] < 0.65 or (m['dd_share'] or 1) > 0.35 or m['median_pct'] <= 0 or m['last14'] <= 0:
        return None
    active = min(1.0, m['per_day'] / 30)
    calm = 1 - min(1.0, m['dd_share'] or 0) - min(0.5, -m['worst_day'] / m['lp_pnl'])
    record = (m['green'] + m['last14_green']) / 2
    compound = min(1.0, (m['ret_on_capital'] or 0) / 3) + (0.2 if m['p10_pct'] > -1 else 0)
    return round(active + 1.5 * calm + 1.5 * record + compound - (0 if m['reconciled'] else 0.5), 3)


def load():
    """Results from the store (copydesk/lpdb.py); the old var/lpfind.json is migrated into it once."""
    from copydesk import lpdb
    if os.path.exists(OUT):
        for m in json.load(open(OUT)).values():
            lpdb.save_result(m)
        os.replace(OUT, OUT + '.migrated')
    return lpdb.results()


def rank(addrs, days=30.0, log=print, force=False):
    done = load()
    # wallets never measured first; the ones that failed before (bot-scale, timed out) are retried last
    addrs = [a for a in addrs if a.lower() not in done] + [a for a in addrs if a.lower() in done]
    for i, a in enumerate(addrs):
        a = a.lower()
        if a in done and not done[a].get('error') and done[a].get('days_window', 0) >= days and (not force or 'raw' in done[a] or 'score2_why' in done[a]):
            continue                                                    # --force redoes only wallets not yet done under these rules
        t = time.time()
        import signal

        def too_slow(*_):
            raise TimeoutError('too slow (> %d s): a bot-scale wallet, not our profile' % LIMIT_S)
        signal.signal(signal.SIGALRM, too_slow)
        signal.alarm(LIMIT_S)
        W.DEADLINE[0] = time.time() + LIMIT_S                           # the alarm cannot reach a thread pool waiting on reads
        try:
            m = metrics(W.report(a, days)) or dict(address=a, empty=True)
        except Exception as e:
            m = dict(address=a, error=str(e)[:160])
        finally:
            signal.alarm(0)
            W.DEADLINE[0] = 0.0
        m['days_window'] = days
        m['score'] = score(m) if 'lp_pnl' in m else None                 # after days_window: the gate reads it
        from copydesk import lpdb
        # the verdict on the data and the v2 score, then raw data only for wallets worth it (owner 2026-09-28: the raw
        # store runs ~25 MB a wallet; 460 candidates would take most of the disk)
        if 'lp_pnl' in m:
            try:
                m['solid'], _ = lpdb.verify(a)
                con = lpdb.connect()
                m['score2'], m['score2_why'] = score2(metrics_db(con, a, 14.0))
                con.close()
            except Exception as e:
                m['score2_why'] = 'scoring failed: %s' % str(e)[:120]
        if m.get('score2') is None and a not in KNOWN:
            lpdb.prune_raw(a)
            m['raw'] = 'pruned'
        done[a] = m
        lpdb.save_result(m)
        log('[%d/%d] %s  %s  (%.0f s)' % (i + 1, len(addrs), a, line(m), time.time() - t))
    return done


def line(m):
    if 'lp_pnl' not in m:
        return m.get('error') or 'no closed LP positions'
    return 'LP $%+.0f  %.1f/day  green %.0f%%  worst day $%.0f  maxDD $%.0f  last14 $%+.0f  med %+.2f%%  p10 %+.2f%%  cap $%d  ret/cap %s  %s  score %s' % (
        m['lp_pnl'], m['per_day'], 100 * m['green'], m['worst_day'], m['max_dd'], m['last14'], m['median_pct'], m['p10_pct'],
        m['peak_capital'], m['ret_on_capital'], 'ok' if m['reconciled'] else 'CHECK FAILED %+.0f' % m['unexplained'], m['score'])


def _replay_one(a, deadline=0.0):
    from copydesk import lpfollow
    if deadline and time.time() > deadline:
        return a, 0, 'skipped: past the night\'s replay budget (next night)'
    t = time.time()
    try:
        # incremental: only what closed since the last copy. 9.5 days, not the 7 the score reads: the walk-forward tests need
        # a full 7-day lookback before the days they judge, and the history store keeps 10
        lpfollow.replay_store(a, 9.5)
        return a, round(time.time() - t), None
    except Exception as e:
        return a, round(time.time() - t), str(e)[:120]


def replay_all(top=0, workers=4, fresh_h=20.0, log=print, everyone=False, budget_s=0):
    """Replay (lpfollow.grid: 7 days, our delay and size) every wallet the board ranks, best first, skipping one replayed in
    the last `fresh_h` hours. Processes, not threads: the replay keeps module state. ~20-70 s a wallet."""
    from multiprocessing import Pool
    from copydesk import lpdb
    rows, _ = board()
    # everyone=True: the whole store, not only the wallets the old score's gates pass - those gates read the LEADER's
    # history and locked out wallets whose copies win (0x12c2 fails the worst-day gate and is our best leader by copies)
    todo = [r['address'] for r in rows if everyone or r['score2'] is not None][:top or None]
    if everyone:
        con = lpdb.connect()
        # discovered, not booked yet: the most active first (7-day mint count), so the first nights replay the busiest
        seen = set(todo)
        todo += [r[0] for r in con.execute(
            'SELECT w.owner FROM watch w LEFT JOIN (SELECT owner, SUM(n) AS n FROM mint_counts GROUP BY owner) m ON m.owner = w.owner '
            'ORDER BY COALESCE(m.n, 0) DESC') if r[0] not in seen]
        con.close()
    try:
        from core import chain as cm
        from copydesk import lpfollow
        c = cm.load('robinhood')
        follow = lpfollow.leaders(c) + lpfollow.bench(c) + lpfollow.filler(c)
        todo = follow + [a for a in todo if a not in follow]         # who we follow now, always first
    except Exception:
        pass
    ev = lpdb.replay_ev(max_age_d=fresh_h / 24)
    todo = [a for a in todo if a not in ev]
    log('replaying %d wallets, %d workers' % (len(todo), workers))
    done = []
    import functools
    one = functools.partial(_replay_one, deadline=time.time() + budget_s if budget_s else 0.0)
    with Pool(workers) as p:
        for a, secs, err in p.imap_unordered(one, todo):
            done.append((a, secs, err))
            log('  %3d/%d %s %4ds %s' % (len(done), len(todo), a[:10], secs, err or ''))
    bad = [a for a, _, err in done if err and not err.startswith('skipped')]
    if bad:                                                             # a busy node fails a few: once more, one at a time
        log('retrying %d' % len(bad))
        for a in bad:
            done.append(_replay_one(a))
            log('  retry %s %4ds %s' % (a[:10], done[-1][1], done[-1][2] or 'ok'))
    return done


def measure_lag(chain, hours=24.0, min_n=5):
    """The live desk's copy lag in blocks, per kind of copy (lpcopy.copy_kind), from our own copies of the last `hours`: our
    mint's block minus the leader's. Medians; a kind with under min_n copies keeps its default. Saved to lpdb kv
    'copy_lag' for the replay (lpcopy.lag_table). Returns it."""
    import statistics as S
    from copydesk import lpcopy as C, lpdb
    from copydesk import lpfollow as L
    from copydesk import lphist
    st = json.load(open(L._state_path(chain)))
    ours = sorted((o for o in st['ours'].values() if o.get('token_id') and o.get('entry_ts', 0) > time.time() - hours * 3600), key=lambda o: o['entry_ts'])
    # both mints' blocks from the history store (every PositionManager mint is in it): no rpc - the getLogs this made went to
    # public nodes that refuse log reads once rh-node was gone (2026-10-01)
    hc = lphist.connect(chain)
    tids = [int(t) for o in ours for t in (o['leader_tid'], o['token_id'])]
    mb = {}
    for i in range(0, len(tids), 900):
        q_ = tids[i:i + 900]
        mb.update(hc.execute('SELECT tid, mint_blk FROM nfts WHERE tid IN (%s)' % ','.join('?' * len(q_)), q_).fetchall())
    hc.close()
    blk = [(mb[int(o['leader_tid'])], mb[int(o['token_id'])]) if int(o['leader_tid']) in mb and int(o['token_id']) in mb else None for o in ours]
    opens = sorted(b[0] for b in blk if b)
    by = collections.defaultdict(list)
    import bisect
    for o, b in zip(ours, blk):
        if not b:
            continue
        j = bisect.bisect_left(opens, b[0])
        kind = 'range' if not o.get('quote_only') else 'burst' if j and b[0] - opens[j - 1] <= C.BURST else 'bid'
        by[kind].append(b[1] - b[0])
    lag = {k: int(round(S.median(v))) if len(v) >= min_n else C.LAG_DEFAULT[k] for k, v in
           ((k, by.get(k, [])) for k in C.LAG_DEFAULT)}
    con = lpdb.connect()
    con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES ('copy_lag', ?)", (json.dumps(lag),))
    con.commit()
    con.close()
    return lag


def discover(days=7.0, min_mints=7, min_days=2, max_per_day=None, log=print):
    """New LP wallets onto the watch list - INCREMENTAL and WIDE (owner 2026-09-30):
    - every v4 PositionManager mint since the last scan (lpdb kv 'mint_scan_block') is counted per owner per day
      (lpdb mint_counts, 14 days kept), read in 100k-block chunks (lpfollow._node_batch), 8 in flight - a night costs one day of
      blocks, seconds, where the old screen re-read 3 days every night and forgot the rest;
    - a wallet qualifies with >= min_mints positions over a rolling `days` on >= min_days different days (the old bar,
      10 a day over 3 days, missed steady wallets doing a few a day and anyone busy earlier in the week);
    - contract owners count too (smart wallets, EIP-7702 wallets: the desk copies whoever holds the NFT), except the
      protocol's own contracts (forensics.ROUTERS: routers, Permit2, the PositionManager).
    Returns the new addresses, most active first."""
    from concurrent.futures import ThreadPoolExecutor
    from copydesk import lpdb
    from copydesk.lpfollow import _node_batch
    zero = '0x' + '0' * 64
    con = lpdb.connect()
    head = int(_node_batch([('eth_blockNumber', [])])[0], 16)
    r = con.execute("SELECT v FROM kv WHERE k = 'mint_scan_block'").fetchone()
    lo = int(r[0]) + 1 if r else head - int(days * W.DAY)
    cnt, per = collections.Counter(), collections.Counter()
    # FROM THE LOCAL HISTORY STORE (copydesk/lphist: every PositionManager mint, kept by the ingest cron) when it covers
    # the range; the node only for what it does not
    from copydesk import lpcopy as C
    R = C._hist()
    if R is not None:
        first, last = R.span()
        if first <= lo <= last:
            for o, b in R.con.execute('SELECT owner, mint_blk FROM nfts WHERE mint_blk BETWEEN ? AND ?', (lo, last)):
                cnt[(o, b)] += 1
                per[(o, time.strftime('%Y-%m-%d', time.gmtime(R.tsof(b))))] += 1
            head = last
    if not cnt:
        q = [(a, min(head, a + 99_999)) for a in range(lo, head + 1, 100_000)]
        get = lambda w: _node_batch([('eth_getLogs', [dict(fromBlock=hex(w[0]), toBlock=hex(w[1]), address=W.POSM, topics=[W.XFR, zero])])],
                                    timeout=120)[0] or []
        blks = set()
        with ThreadPoolExecutor(8) as ex:
            for logs in ex.map(get, q):
                for l in logs:
                    b = int(l['blockNumber'], 16)
                    blks.add(b)
                    cnt[('0x' + l['topics'][2][-40:], b)] += 1
        # a mint's day from its block: one timestamp per 100k blocks is plenty (~2.8 h resolution is below a day)
        marks = sorted({b - b % 100_000 for b in blks} | {head})
        ts = dict(zip(marks, [int(x['timestamp'], 16) for x in _node_batch([('eth_getBlockByNumber', [hex(m), False]) for m in marks])]))
        day = lambda b: time.strftime('%Y-%m-%d', time.gmtime(ts[b - b % 100_000] + (b % 100_000) * 0.1))
        for (o, b), n in cnt.items():
            per[(o, day(b))] += n
    lpdb.begin(con)
    con.executemany('INSERT INTO mint_counts(owner, day, n) VALUES (?,?,?) ON CONFLICT(owner, day) DO UPDATE SET n = n + excluded.n',
                    [(o, d, n) for (o, d), n in per.items()])
    con.execute("INSERT OR REPLACE INTO kv(k, v) VALUES ('mint_scan_block', ?)", (str(head),))
    con.execute('DELETE FROM mint_counts WHERE day < ?', (time.strftime('%Y-%m-%d', time.gmtime(time.time() - 14 * 86400)),))
    lpdb.commit(con)
    since = time.strftime('%Y-%m-%d', time.gmtime(time.time() - days * 86400))
    # NO UPPER CAP (owner 2026-09-30): a busy wallet is judged like any other - the copy score charges every copy its gas
    # and the desk copies only what fits its slots. Its first replay is long (0x8355: 4,081 mints a day, ~28k positions a
    # week), the nightly ones only replay what closed since. max_per_day stays as an option, off by default
    rows = con.execute('SELECT owner, SUM(n), COUNT(day) FROM mint_counts WHERE day >= ? GROUP BY owner HAVING SUM(n) >= ? AND COUNT(day) >= ? '
                       'AND SUM(n) <= ? ORDER BY SUM(n) DESC', (since, min_mints, min_days, int(max_per_day * days) if max_per_day else 10 ** 12)).fetchall()
    have = {r[0] for r in con.execute('SELECT DISTINCT owner FROM runs')} | {r[0] for r in con.execute('SELECT owner FROM watch')}
    from copydesk.lpfollow import own_wallets
    skip = {x.lower() for x in F.ROUTERS} | {'0x' + '0' * 40, '0x000000000000000000000000000000000000dead'} | own_wallets()
    new = [o for o, n, d in rows if o not in have and o not in skip]
    con.executemany('INSERT OR IGNORE INTO watch(owner, added) VALUES (?, ?)', [(a, time.time()) for a in new])
    con.commit()
    con.close()
    log('discover: scanned %d blocks, %d mints; %d wallets qualify (>= %d in %g d on >= %d days), %d new on the watch list' % (
        head - lo + 1, sum(cnt.values()), len(rows), min_mints, days, min_days, len(new)))
    return new


def measured_gas(chain, hours=48.0):
    """OUR gas per copy, measured: every lp-1 tx of the last `hours` (mints, exits, sells, approvals) over the copies
    opened in that time (2026-09-30: $8.70 / 113 copies = $0.077; the flat $0.19 over-charged busy leaders). Never the
    leaders' own gas - theirs is not ours. None when there is not enough to measure."""
    from core import lpbook
    from discovery import forensics as F
    from copydesk.lpfollow import _node_batch, _state_path
    me = lpbook.account(chain).address.lower()
    head = int(_node_batch([('eth_blockNumber', [])])[0], 16)
    iv = lambda x: int(x, 16) if isinstance(x, str) else x
    wei = 0
    for r in F._pages(dict(from_block=head - int(hours * 36000), transactions=[{'from': [me]}],
                           field_selection=dict(transaction=['gas_used', 'effective_gas_price'])), F._key()):
        for d in r['data']:
            wei += sum(iv(t['gas_used']) * iv(t['effective_gas_price']) for t in d.get('transactions') or [])
    st = json.load(open(_state_path(chain)))
    n = sum(1 for o in st['ours'].values() if o.get('entry_ts', 0) > time.time() - hours * 3600)
    return round(wei / 1e18 * chain.eth_usd / n, 4) if n >= 20 else None


def nightly(chain, log=print, dry=False):
    """THE COPY DESK'S NIGHT (cron 03:00 UTC): discover new wallets -> replay the whole store at our size and delay ->
    lp pick --apply (main leaders + bench from replayed copies; the desk restarts itself on a change) -> the token-rule
    knobs backtest. Posts one Telegram note with what happened."""
    from copydesk import lpopt, lpfollow as L
    import fcntl
    lk = open(os.path.join(L._dir(chain), 'nightly.lock'), 'w')
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)             # one night at a time: a second run would double the node load
    except OSError:
        return 'lp nightly: another run holds the lock, skipped', False
    t = time.time()
    out = []
    # THE STORE FIRST (copydesk/lphist): up to the node's head, then trimmed to its window - every step below reads it
    try:
        from copydesk import lphist
        lphist.ingest(chain, log=log)
        lphist.prune(chain)
        s = lphist.status(chain)
        out.append('history store: %d swaps, %d positions, %.0f MB' % (s['swaps'], s['positions'], s['mb']))
    except Exception as e:
        log('history ingest failed: %s (replays read the node)' % str(e)[:120])
    new = discover(log=log)
    try:
        lag = measure_lag(chain)
        out.append('copy lag (blocks, from our own copies): %s' % ', '.join('%s %s' % kv for kv in sorted(lag.items())))
    except Exception as e:
        log('lag measure failed: %s' % str(e)[:120])
    done = replay_all(workers=3, fresh_h=1.0, log=log, everyone=True, budget_s=95 * 60)   # incremental: ~a day of replay a wallet
    bad = sum(1 for _, _, e in done if e and not e.startswith('skipped'))
    left = sum(1 for _, _, e in done if e and e.startswith('skipped'))
    try:
        chain.refresh_eth_usd()
        gas = measured_gas(chain) or lpopt.COPY_GAS
    except Exception as e:
        log('gas measure failed: %s' % str(e)[:120])
        gas = lpopt.COPY_GAS
    out.append('gas $%.3f a copy (measured, 48 h)' % gas)
    try:
        tk, ta = lpopt.measure_take(chain)                          # the simulator takes what the live desk really takes
        out.append('take %.0f%% of replayed copies (%d wallets measured)' % (100 * ta, len(tk)))
    except Exception as e:
        log('take measure failed: %s' % str(e)[:120])
    before = lineup(chain)
    lead, bench, changed = lpopt.pick(chain, apply=not dry, log=out.append, gas=gas)
    chain.squad = squad_diff(chain, before, lineup(chain), gas) if changed and not dry else None
    live = (int(L.cfg(chain).get('max_per_token', 0)), int(L.cfg(chain).get('cool_n', 0)), float(L.cfg(chain).get('cool_h', 24.0)))
    rows = lpopt.knobs(L.leaders(chain), L.bench(chain), log=lambda m: None, gas=gas, size=L.live_size(chain)[0], live=live)
    best = max(rows, key=lambda r: r[1]['pnl'])
    kn = 'knobs: live %s; best on days 1-3.5 %s (judged %+.2f vs live %+.2f)' % (
        live, best[0], best[2]['pnl'], next((r[2]['pnl'] for r in rows if r[0] == live), float('nan')))
    # GAS: lp-1 ran dry on 2026-09-30 (a cycle costs ~$0.19, 110 txs burned $6.50 in 9 h) and every exit was refused
    try:
        from core.exec import lp as X
        from core import lpbook
        gas = int(chain.node.rpc('eth_getBalance', [lpbook.account(chain).address, 'latest']), 16) / 1e18 * chain.eth_usd
    except Exception:
        gas = float('nan')
    kn += ' · lp-1 gas $%.2f%s' % (gas, ' - REFUEL: under $5, about a day of copies' if gas < 5 else '')
    msg = 'lp nightly (%.0f min)\n%d new wallets · %d replayed (%d failed, %d left for tomorrow)\n%s\n%s' % (
        (time.time() - t) / 60, len(new), len(done) - left, bad, left, ('squad: %d changes (card below)' % len(chain.squad['changes'])
        if getattr(chain, 'squad', None) else 'squad: unchanged' if not changed else 'squad: filler only'), kn)
    log(msg)
    return msg, changed


def lineup(chain):
    """wallet -> its place in the squad as chain.toml has it now: 'main', 'bench', 'spec HH' (quiet-hour specialist), 'filler'."""
    from core import chain as cm
    from copydesk import lpfollow as L
    c = cm.load(chain.name)
    hrs = {k.lower(): v for k, v in (L.cfg(c).get('hours') or {}).items()}
    out = {a: 'filler' for a in L.filler(c)}
    out.update({a: 'bench' for a in L.bench(c)})
    out.update({a: 'spec %s UTC' % ','.join('%02d' % h for h in sorted(v)) for a, v in hrs.items()})
    out.update({a: 'main' for a in L.leaders(c)})
    return out


def squad_diff(chain, before, after, gas, foot=''):
    """What changed among the mains, bench and specialists (the filler only as a count), each with its why: the score of
    a wallet that came in, QC's flags / a lost edge / a better wallet for one that left. None when only the filler moved."""
    from copydesk import lpdb, lpqc, lpfollow as L
    core = lambda d: {a: t for a, t in d.items() if t != 'filler'}
    b, a = core(before), core(after)
    ws = [w for w in list(b) + [x for x in a if x not in b] if before.get(w) != after.get(w)]
    if not ws:
        return None
    sc = lpdb.copy_score(7, size=L.live_size(chain)[0], gas=gas)
    down = lpqc.turned_down(chain, [w for w in ws if after.get(w) in (None, 'filler')])
    per = lambda w: '%+.1f $/day' % sc[w][0] if w in sc else 'no score'
    ch = []
    for w in ws:
        new = after.get(w)
        if new in (None, 'filler') and w in b:
            off = (before.get(w) or '').startswith('spec') and not int(L.cfg(chain).get('specialists_per_block', 1))
            why = 'QC: ' + ', '.join(down[w]) if w in down else 'lost its edge (%s)' % per(w) if sc.get(w, (0,))[0] <= 0 else \
                ('specialists are off (specialists_per_block = 0)%s (%s)' % ('' if new else ', under the filler bar', per(w))) if off else \
                'a better wallet took the place (%s)' % per(w)
        else:
            why = per(w)
        ch.append((w, before.get(w), new, why))
    return dict(changes=ch, mains=[x for x, t in after.items() if t == 'main'],
                filler=(sum(1 for t in before.values() if t == 'filler'), sum(1 for t in after.values() if t == 'filler')), foot=foot)


def repick(chain, log=print, dry=False):
    """THE LIVING SQUAD (owner 2026-09-30, cron hourly beside the nightly; walk forward: 1 h +$2.31/day over 8 days, 6 h +$1.04): the replay brought up to the history store for
    every wallet with copies in the window (incremental: only what closed since its last copy), then lp pick --apply
    (QC inside, at most pick_swaps bench swaps). The desk sees the new chain.toml within 30 s and restarts in place."""
    from copydesk import lpdb, lpopt, lpfollow as L
    import fcntl
    lk = open(os.path.join(L._dir(chain), 'nightly.lock'), 'w')
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)             # never beside the nightly: both write the squad
    except OSError:
        log('lp repick: another repick or the nightly is running, skipped')
        return 'lp repick: skipped (another repick or the nightly is running)', False, None
    t = time.time()
    con = lpdb.connect()
    ws = [o for (o,) in con.execute('SELECT DISTINCT owner FROM copy_trades WHERE t1 >= ?', (time.time() - 8 * 86400,))]
    # AND EVERY WALLET DISCOVERY QUALIFIES (>= 7 positions on >= 2 days in 7), copies or not yet: on 2026-09-30 741 of 2,975
    # had never been replayed (the nightly's budget ran out while replays still read the node) and this list alone never
    # reached them. From the history store that costs ~1.5 min
    since = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 7 * 86400))
    have = set(ws)
    ws += [o for (o,) in con.execute('SELECT owner FROM mint_counts WHERE day >= ? GROUP BY owner HAVING SUM(n) >= 7 AND COUNT(day) >= 2',
                                     (since,)) if o not in have and o not in L.own_wallets()]
    con.close()
    done = replay_wallets(ws, workers=3, log=lambda m: None)
    bad = sum(1 for _, _, e in done if e)
    try:
        gas = measured_gas(chain) or lpopt.COPY_GAS
    except Exception:
        gas = lpopt.COPY_GAS
    out = []
    before = lineup(chain)
    # A REPICK ON A BROKEN REPLAY DOES NOT APPLY (2026-10-01 09:20: rh-node's box was reclaimed mid-run, 616 of 3,157 replays
    # failed, and the pick still moved four squad places on what was left): over 10% failed = a dry pick, said so
    if bad > 0.1 * max(1, len(done)) and not dry:
        dry = True
        out.append('NOT APPLIED: %d of %d replays failed (the node?) - this pick is dry' % (bad, len(done)))
    lead, bench, changed = lpopt.pick(chain, apply=not dry, log=out.append, gas=gas)
    foot = '%d wallets replayed%s · %.1f min%s' % (len(done), ' (%d failed)' % bad if bad else '', (time.time() - t) / 60, ' · DRY' if dry else '')
    d = squad_diff(chain, before, lineup(chain), gas, foot) if changed and not dry else None
    msg = 'lp repick: %s · %s' % ('%d squad change%s' % (len(d['changes']), 's' * (len(d['changes']) != 1)) if d else
                                  'filler only' if changed else 'unchanged', foot)
    for l in out:
        log(l)
    log(msg)
    return msg, changed, d


def bench_board(exclude=(), max_age_d=3, limit=15, days=7.0, gas=None, size=25.0, cap=125.0):
    """The bench score (owner 2026-09-29): what a replayed wallet ADDS in $/day beside the main leaders `exclude`, under our
    capital and the desk's priority rule (lpopt.bench_value) - activity in the hours the main leaders leave capital idle.
    Only wallets replayed lately whose copy makes money on its own after gas are candidates."""
    from copydesk import lpdb, lpopt
    ex = {x.lower() for x in exclude}
    # NET OF GAS: replay_ev's copy $ is before gas (0xe12e: +$26.88 a week before, ~$0 after its ~210 copies), so a
    # candidate must earn on its own in the simulator, which books our gas on every copy
    gas = lpopt.COPY_GAS if gas is None else gas
    ok = {o for o, (v, _) in lpdb.copy_score(days, size=size, gas=gas).items() if v > 0}
    fresh = [o for o in lpdb.replay_ev(max_age_d) if o not in ex and o in ok]      # positive expected copy value
    solo = {o: v for o, v in lpopt.bench_value([], fresh, days, size=size, cap=cap, gas=gas).items() if v > 0} if fresh else {}
    add = lpopt.bench_value(sorted(ex), sorted(solo), days, size=size, cap=cap, gas=gas) if solo else {}
    keep = lpdb.keep_rates()
    rows = sorted(solo, key=lambda o: -add.get(o, 0))
    return [dict(address=o, adds=add.get(o, 0.0), per_day=solo[o], keep=keep.get(o)) for o in rows[:limit]]


def replay_wallets(addrs, workers=3, log=print):
    """Replay these wallets now - the nightly's own path (lpfollow.replay_store: our size, the live lag, incremental when
    the store is current) - `workers` at a time. Returns [(address, seconds, error)]."""
    from multiprocessing import Pool
    done = []
    with Pool(workers) as p:
        for a, secs, err in p.imap_unordered(_replay_one, [x.lower() for x in addrs]):
            done.append((a, secs, err))
            log('  %3d/%d %s %4ds %s' % (len(done), len(addrs), a[:10], secs, err or 'ok'))
    return done


def main(a):
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    if a and a[0] == 'replay':
        # lp find replay <addr ...> [--workers 3]: these wallets, now (a list can also come from --from <file>)
        addrs = [x for x in a[1:] if x.startswith('0x')]
        if '--from' in a:
            addrs += [l.split()[0] for l in open(a[a.index('--from') + 1]) if l.startswith('0x')]
        replay_wallets(addrs, opt('--workers', 3), log=lambda m: print(m, flush=True))
        return
    if a and a[0] == 'candidates':
        for x, c in candidates(opt('--days', 3.0), opt('--min-per-day', 10.0)):
            print(x, c)
        return
    if a and a[0] == 'calibrate':
        for l in calibrate_lines(calibrate(opt('--days', 14.0))):
            print(l)
        return
    if a and a[0] == 'replay-all':
        replay_all(opt('--top', 0), opt('--workers', 4), opt('--fresh-h', 20.0))
        a = ['bench']
    if a and a[0] == 'bench':
        from core import chain as cm
        from copydesk import lpfollow
        ch = cm.load('robinhood')
        mains = lpfollow.leaders(ch)
        size, book = lpfollow.live_size(ch)
        print('BENCH beside %s: $/day it ADDS under our capital (desk priority rule, 7 d, after gas) | alone' % ', '.join(m[:6] for m in mains))
        for r in bench_board(exclude=mains, limit=opt('--limit', 15), size=size, cap=book):
            print('  %s  adds %+7.2f/day | alone %+7.2f/day  keep %s' % (r['address'], r['adds'], r['per_day'],
                  '%.0f%%' % (100 * r['keep']) if r['keep'] is not None else '-'))
        return
    if a and a[0] == 'board':
        show_board(opt('--days', 14.0), opt('--limit', 30))
        return
    if a and a[0] == 'rank':
        addrs = [x for x in a[1:] if x.startswith('0x')]
        if '--from' in a:
            addrs = [l.split()[0] for l in open(a[a.index('--from') + 1]) if l.startswith('0x')]
        if not addrs:
            addrs = [x for x, _ in candidates(3.0, opt('--min-per-day', 10.0))][:opt('--top', 30)]
        rank(addrs, opt('--days', 30.0), force='--force' in a)
        from copydesk import lpdb
        try:
            lpdb.compact()
        except Exception as e:                                          # another worker is writing: the next pass compacts
            print('compact skipped: %s' % e)
    done = load()
    for m in done.values():
        m['score'] = score(m) if 'lp_pnl' in m else None                 # always under the current gates
    ok = sorted([m for m in done.values() if m.get('score') is not None], key=lambda m: -m['score'])
    print('\nqualify (%d of %d measured):' % (len(ok), len(done)))
    for m in ok:
        print('  %s  %s' % (m['address'], line(m)))



# --------------------------------------------------------------------------- THE SCORE (v4): from the store, supplementary
# Every raw metric stays beside it. Gates first (a wallet that fails one is not ranked, with the reason), then five
# components in [0, 1], weighted, times a trust factor and the measured copy keep rate, x 100.
# v3 (2026-09-28, `lp find calibrate`): scored on days 1-7, judged by a $25 copy on days 8-14, 130 wallets. Return on
# capital was the only strong predictor (spearman +0.38); drawdown, consistency, profit factor and win rate ~0 or
# negative (win rate -0.22 vs next-week roc). So return carries the weight, risk became kill-line gates, quality went.
# One fold, in-sample weights: re-run calibrate weekly before trusting the decimals.
WEIGHTS = dict(ret=0.45, activity=0.20, smooth=0.15, dd=0.10, consist=0.10)
SHRINK_N = 50               # ret x n/(n+50): the top of ~400 screened wallets is inflated by selection (deflated-Sharpe logic)
# v4 (2026-09-29): the wallet's P&L is not what a COPY earns. 0x1017 held 83% of its positions under 5 minutes, so 65% of our
# $25 copies were gas-only, and v3 could not see it. `calibrate` over 109 wallets (week 1 scored, week 2 copied at $25 / $125
# cap / $0.125 gas): the mean $ per $25 copy after live gas predicts next-week copy P&L best (spearman +0.43 vs v3 +0.31; top 10
# 10/10 green). Dropping the top 5% of copies made it worse (+0.32): the big wins persist. Median hold time predicts nothing.
COPY_SIZE, COPY_GAS, COPY_FULL = 25.0, 0.125, 2.0      # our clip, a measured round trip, $ per copy that earns full marks
V4_COPY = 0.7                                          # score = 0.7 x copy economics + 0.3 x the v3 blend (0.7/0.3 and 0.5/0.5 tied)


def metrics_db(con, owner, win=14.0, end=None):
    """Everything the score reads, from lpdb's tables for the wallet's latest run of at least `win` days
    (or the `win` days before `end`, for calibrate's walk-forward)."""
    import math
    from copydesk import lpwallet as W
    run = con.execute('SELECT at, head, win, lp_pnl, reconciled, unexplained, skipped FROM runs WHERE owner = ? AND win >= ? '
                      'ORDER BY at DESC LIMIT 1', (owner, win)).fetchone()
    if not run:
        return None
    at, head, rwin, lp_pnl, rec, unexpl, skipped = run
    if end:
        at = end
    frm_blk, frm_ts = head - int(win * W.DAY), at - win * 86400
    if end:
        pos = con.execute('SELECT open_ts, close_ts, invested_usd, realized_usd, closed FROM positions WHERE owner = ? AND open_ts >= ? '
                          'AND open_ts < ? AND skip IS NULL AND invested_usd > 0', (owner, frm_ts, end)).fetchall()
        pos = [(o, c, inv, r, cl and c is not None and c <= end) for o, c, inv, r, cl in pos]   # closed later = open at `end`
    else:
        pos = con.execute('SELECT open_ts, close_ts, invested_usd, realized_usd, closed FROM positions WHERE owner = ? AND open_blk >= ? '
                          'AND skip IS NULL AND invested_usd > 0', (owner, frm_blk)).fetchall()
    closed = [p for p in pos if p[4] and p[3] is not None]
    big = [p for p in closed if p[2] >= 20]                        # what a $25 copy would have followed (dust tests excluded)
    day0 = time.strftime('%Y-%m-%d', time.gmtime(frm_ts))
    daily = dict(con.execute('SELECT day, net_usd FROM daily WHERE owner = ? AND day >= ?', (owner, day0)).fetchall())
    days = [time.strftime('%Y-%m-%d', time.gmtime(frm_ts + 86400 * (i + 1))) for i in range(int(win))]
    net = [daily.get(d, 0.0) for d in days]                       # a quiet day is a zero day, not a missing one
    active = sum(1 for d in days if d in daily)
    if not closed or not active:
        return None
    ev = []
    for o, c, inv, _, cl in pos:
        ev += [(o, inv), ((c if cl and c else at), -inv)]
    cur = cap = 0.0
    for _, v in sorted(ev):
        cur += v
        cap = max(cap, cur)
    r = [x / cap for x in net] if cap else [0.0] * len(net)
    growth = 1.0
    for x in r:
        growth *= 1 + x
    mu = sum(r) / len(r)
    sd = (sum((x - mu) ** 2 for x in r) / max(1, len(r) - 1)) ** 0.5
    peak = cum = dd = 0.0
    for x in net:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    streak = run_ = 0
    for x in net:
        run_ = run_ + 1 if x < 0 else 0
        streak = max(streak, run_)
    wins = sum(p[3] for p in closed if p[3] > 0)
    losses = -sum(p[3] for p in closed if p[3] < 0)
    pct = sorted(100 * p[3] / p[2] for p in closed)
    native = json.loads(skipped or '{}').get('native-ETH pool (not covered)', 0)
    # the P&L of THIS window (the latest run may cover a longer one): the daily nets, LP gas already off
    return dict(address=owner, win=win, lp_pnl=round(sum(net), 2), capital=round(cap), growth=round(growth - 1, 4), ret_on_capital=round(sum(net) / cap, 4) if cap else None,
                sharpe=round(mu / sd * math.sqrt(len(r)), 3) if sd else None, max_dd=round(dd, 2), worst_day=round(min(net), 2),
                green=round(sum(x > 0 for x in net) / active, 3), active_days=active, red_streak=streak,
                profit_factor=round(wins / losses, 3) if losses else None, p1_pct=round(pct[len(pct) // 100], 2),
                mean_pct=round(sum(pct) / len(pct), 3), median_pct=round(pct[len(pct) // 2], 3), won=round(sum(x > 0 for x in pct) / len(pct), 3),
                per_day=round(len(closed) / active, 1), positions=len(closed), reconciled=bool(rec), unexplained=unexpl, native_skipped=native,
                copy_mean=round(S.mean(COPY_SIZE * p[3] / p[2] - COPY_GAS for p in big), 4) if big else None,
                copy_gasonly=round(sum(COPY_SIZE * p[3] / p[2] <= COPY_GAS for p in big) / len(big), 3) if big else None)


def score2(m, keep=None):
    """(score 0-100 or None, why-not or the component breakdown). keep = the wallet's measured copy keep rate, if replayed."""
    import math
    if not m:
        return None, 'no data in the store (needs a backfill run)'
    gates = [(m['lp_pnl'] > 0, 'not profitable'), (m['reconciled'] or m['native_skipped'] > 0, 'data check failed, not explained by native ETH'),
             (m['active_days'] >= m['win'] / 2, 'active %d of %g days' % (m['active_days'], m['win'])), (m['per_day'] >= 3, '%.1f positions/day' % m['per_day']),
             (m['capital'] > 0, 'no capital measured'),
             (m['worst_day'] >= -0.2 * m['capital'], 'a day lost %.0f%% of capital' % (-100 * m['worst_day'] / max(1, m['capital']))),
             ((m['profit_factor'] or 10) >= 1.2, 'profit factor %.2f' % (m['profit_factor'] or 0))]
    for ok, why in gates:
        if not ok:
            return None, why
    cl = lambda x: max(0.0, min(1.0, x))
    c = dict(
        ret=cl(math.log1p(max(-0.99, m['growth'])) / math.log(3)) * m['positions'] / (m['positions'] + SHRINK_N),  # +200% compounded = 1, shrunk by n
        smooth=cl((m['sharpe'] or 0) / 4),                                               # Sharpe-like 4 over the window = 1
        dd=cl(1 - max(-m['max_dd'] / m['lp_pnl'], -m['worst_day'] / m['capital'] * 5)),  # a drawdown of the whole P&L, or a day losing 20% of capital = 0
        consist=cl((m['green'] - 0.5) * 2) * (1 - cl((m['red_streak'] - 1) / 4)),       # 50% green = 0; a 5-day red run = 0
        activity=cl(math.log(max(1, m['per_day']) / 5) / math.log(10)))              # 5 positions/day = 0, 50 = 1
    trust = (1.0 if m['reconciled'] else 0.8) * (min(1.0, keep) if keep is not None else 1.0)   # a replayed copy's keep rate is what we earn
    c['copy'] = cl((m.get('copy_mean') or 0.0) / COPY_FULL)
    v3 = sum(WEIGHTS[k] * c[k] for k in WEIGHTS)
    return round(100 * trust * (V4_COPY * c['copy'] + (1 - V4_COPY) * v3), 1), dict({k: round(v, 2) for k, v in c.items()}, trust=round(trust, 2))


def board(win=14.0):
    """Every wallet in the store, scored; plus the screened wallets whose data is not in the store yet."""
    from copydesk import lpdb
    con = lpdb.connect()
    owners = [r[0] for r in con.execute('SELECT DISTINCT owner FROM runs')]
    keep = lpdb.keep_rates()
    rows = []
    for o in owners:
        m = metrics_db(con, o, win)
        s, why = score2(m, keep.get(o))
        rows.append(dict(m or dict(address=o), score2=s, why=why))
    missing = [a for a in lpdb.results() if a not in set(owners)]
    return sorted(rows, key=lambda r: -(r['score2'] or -1)), missing


def show_board(win=14.0, limit=30):
    rows, missing = board(win)
    ok = [r for r in rows if r['score2'] is not None]
    print('SCORE v4 (%g d), %d ranked of %d in the store; %d screened wallets not in the store yet (backfill)' % (win, len(ok), len(rows), len(missing)))
    print('  %-44s %5s  %8s  %6s  %7s  %6s  %6s  %5s  %5s  %6s  %s' % ('wallet', 'score', 'LP P&L', 'cap', 'growth', 'sharpe', 'green', 'pos/d', 'PF', 'worstD', 'components'))
    for r in ok[:limit]:
        print('  %-44s %5.1f  %+8.0f  %6d  %+6.0f%%  %6.2f  %5.0f%%  %5.1f  %5s  %6.0f  %s' % (
            r['address'], r['score2'], r['lp_pnl'], r['capital'], 100 * r['growth'], r['sharpe'] or 0, 100 * r['green'], r['per_day'],
            '%.1f' % r['profit_factor'] if r['profit_factor'] else 'inf', r['worst_day'],
            ' '.join('%s %.2f' % kv for kv in r['why'].items())))
    out = collections.Counter(r['why'] for r in rows if r['score2'] is None)
    print('  not ranked: ' + ', '.join('%s %d' % kv for kv in out.most_common()))


def _spearman(a, b):
    rk = lambda v: {i: k for k, i in enumerate(sorted(range(len(v)), key=lambda i: v[i]))}
    ra, rb, n = rk(a), rk(b), len(a)
    m = (n - 1) / 2
    num = sum((ra[i] - m) * (rb[i] - m) for i in range(n))
    den = (sum((ra[i] - m) ** 2 for i in range(n)) * sum((rb[i] - m) ** 2 for i in range(n))) ** 0.5
    return num / den if den else 0.0


def calibrate(days=14.0, size=25.0, cap=125.0, gas=0.125):
    """Walk-forward check of the score: every wallet scored on the first half of the window, judged by what a copy at
    OUR size earned in the second half (lpopt.simulate). Per component and raw metric: spearman vs that copy P&L."""
    from copydesk import lpdb, lpopt
    con = lpdb.connect()
    t1 = time.time()
    mid = t1 - days / 2 * 86400
    lpopt.KEEP.clear()
    rows = []
    for o in [r[0] for r in con.execute('SELECT DISTINCT owner FROM runs')]:
        m = metrics_db(con, o, days / 2, end=mid)
        s, c = score2(m)
        if s is None:
            continue
        te = lpopt.simulate([(o, lpopt._positions(con, o, mid, t1), None)], size, cap, gas, mid, t1)
        rows.append(dict(o=o, s=s, c=c, m=m, copy=te['pnl']))
    con.close()
    if len(rows) < 10:
        return dict(n=len(rows))
    y = [r['copy'] for r in rows]
    feats = dict(score=[r['s'] for r in rows], **{'c_' + k: [r['c'][k] for r in rows] for k in WEIGHTS},
                 **{k: [r['m'][k] if r['m'][k] is not None else 99 for r in rows]
                    for k in ('ret_on_capital', 'sharpe', 'green', 'profit_factor', 'won', 'per_day', 'capital', 'max_dd', 'worst_day', 'p1_pct', 'copy_mean', 'copy_gasonly')})
    rho = {k: round(_spearman(v, y), 3) for k, v in feats.items()}
    top = sorted(rows, key=lambda r: -r['s'])
    tops = {k: dict(mean=round(sum(r['copy'] for r in top[:k]) / k, 2), green=sum(r['copy'] > 0 for r in top[:k])) for k in (5, 10, 20) if len(top) >= k}
    res = dict(at=t1, days=days, n=len(rows), weights=WEIGHTS, rho=rho, top=tops, rest=round(sum(y) / len(y), 2),
               best=[dict(address=r['o'], score=r['s'], copy=round(r['copy'], 2)) for r in top[:15]])
    con = lpdb.connect()
    con.execute('CREATE TABLE IF NOT EXISTS calibrate(at REAL PRIMARY KEY, j TEXT)')
    con.execute('INSERT OR REPLACE INTO calibrate VALUES (?,?)', (t1, json.dumps(res)))
    con.commit()
    con.close()
    return res


def calibrate_lines(res):
    if res['n'] < 10:
        return ['calibrate: %d wallets pass the gates on the first half, too few' % res['n']]
    out = ['CALIBRATE: %d wallets scored on the first %g days, judged by a $25 copy on the last %g' % (res['n'], res['days'] / 2, res['days'] / 2),
           'spearman vs next-half copy P&L: ' + '  '.join('%s %+.2f' % kv for kv in sorted(res['rho'].items(), key=lambda kv: -kv[1]))]
    for k, t in res['top'].items():
        out.append('top %2s by score: copy mean $%+.2f, %d/%s green' % (k, t['mean'], t['green'], k))
    out.append('all wallets: copy mean $%+.2f' % res['rest'])
    return out


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
