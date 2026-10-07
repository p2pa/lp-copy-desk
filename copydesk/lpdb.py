"""THE LP STORE (var/lpwallets.db): everything lpwallet / lpfind / lpfollow learn, kept for reruns and a dashboard.

RAW (fetched once, extended incrementally; lpwallet.fetch)
  cover(owner, lo, hi)                 the block range [lo, hi) each wallet's transfers are stored for
  logs / txs / blocks                  every Transfer to or from it, the txs behind them, block times
POSITIONS (rewritten per wallet on every report; lpwallet.report -> save_report)
  positions                            one row per position (a ladder of NFTs minted in one tx): pool, pair, fee, range,
                                       open/close, hold, invested, returned, fees, realized $ and %, flags
  legs                                 each NFT of a position: its pool, ticks and liquidity (from the mint receipt)
  pools / tokens                       pool keys and token symbols/decimals
TIME SERIES
  daily(owner, day)                    closed, won, realized, LP gas, net, turnover per UTC day
  runs(owner, at)                      every report: LP P&L, realized, open uPnL, gas, the check, open value
  open_marks(owner, at, token_id)      each open position at each report: value, fees, in range, price and range
SCREENING / COPYING
  results(owner, win)                  lpfind metrics per wallet and window
  wallets(owner)                       labels: leader / candidate / known, and a note
  copies(leader_tid)                   the copy desk's own trades, with the leader's result on the same position
"""
import json, os, sqlite3, time

from discovery import forensics as F

DB = os.path.join(os.path.dirname(F.INDEX), 'lpwallets.db')
SCHEMA = """
CREATE TABLE IF NOT EXISTS cover(owner TEXT PRIMARY KEY, lo INTEGER, hi INTEGER);
CREATE TABLE IF NOT EXISTS logs(owner TEXT, tx TEXT, li INTEGER, blk INTEGER, address TEXT, t1 TEXT, t2 TEXT, t3 TEXT, data TEXT,
                                PRIMARY KEY(owner, tx, li));
CREATE INDEX IF NOT EXISTS logs_by_block ON logs(owner, blk);
CREATE TABLE IF NOT EXISTS txs(owner TEXT, hash TEXT, j TEXT, PRIMARY KEY(owner, hash));
CREATE TABLE IF NOT EXISTS blocks(n INTEGER PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS positions(owner TEXT, ladder INTEGER, token_ids TEXT, n_legs INTEGER, pool TEXT, currency0 TEXT, currency1 TEXT,
    token TEXT, symbol TEXT, fee_pips INTEGER, hooks TEXT, tick_lower INTEGER, tick_upper INTEGER, range_lo_usd REAL, range_hi_usd REAL,
    quote_only INTEGER, open_blk INTEGER, open_ts REAL, close_blk INTEGER, close_ts REAL, age_h REAL, closed INTEGER,
    invested_usd REAL, invested_eth REAL, returned_usd REAL, fees_usd REAL, realized_usd REAL, realized_pct REAL,
    unsold TEXT, unpriced INTEGER, updated REAL, skip TEXT, PRIMARY KEY(owner, ladder));
CREATE INDEX IF NOT EXISTS positions_by_close ON positions(owner, close_ts);
CREATE INDEX IF NOT EXISTS positions_by_token ON positions(token);
CREATE TABLE IF NOT EXISTS legs(owner TEXT, ladder INTEGER, token_id INTEGER, pool TEXT, tick_lower INTEGER, tick_upper INTEGER,
    liquidity TEXT, PRIMARY KEY(owner, token_id));
CREATE TABLE IF NOT EXISTS pools(pid TEXT PRIMARY KEY, currency0 TEXT, currency1 TEXT, fee_pips INTEGER, tick_spacing INTEGER, hooks TEXT);
CREATE TABLE IF NOT EXISTS tokens(address TEXT PRIMARY KEY, symbol TEXT, decimals INTEGER);
CREATE TABLE IF NOT EXISTS daily(owner TEXT, day TEXT, closed INTEGER, won INTEGER, realized_usd REAL, gas_usd REAL, net_usd REAL,
    invested_usd REAL, PRIMARY KEY(owner, day));
CREATE TABLE IF NOT EXISTS runs(owner TEXT, at REAL, head INTEGER, win REAL, lp_pnl REAL, realized_usd REAL, open_upnl REAL, gas_usd REAL,
    open_usd REAL, positions INTEGER, reconciled INTEGER, unexplained REAL, skipped TEXT, secs REAL, PRIMARY KEY(owner, at));
CREATE TABLE IF NOT EXISTS open_marks(owner TEXT, at REAL, token_id INTEGER, pair TEXT, value_usd REAL, fee_usd REAL, in_range INTEGER,
    range_lo REAL, range_hi REAL, invested_usd REAL, upnl_usd REAL, PRIMARY KEY(owner, at, token_id));
CREATE TABLE IF NOT EXISTS results(owner TEXT, win REAL, at REAL, head INTEGER, score REAL, j TEXT, PRIMARY KEY(owner, win));
CREATE TABLE IF NOT EXISTS wallets(owner TEXT PRIMARY KEY, label TEXT, note TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS replays(owner TEXT, at REAL, days REAL, real_usd REAL, model0_usd REAL, copy2_usd REAL, copy7_usd REAL,
    keep7 REAL, delay_blocks INTEGER, size_usd REAL, PRIMARY KEY(owner, at));
CREATE TABLE IF NOT EXISTS watch(owner TEXT PRIMARY KEY, added REAL);
CREATE TABLE IF NOT EXISTS mint_counts(owner TEXT, day TEXT, n INTEGER, PRIMARY KEY(owner, day));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS copy_trades(owner TEXT, t0 REAL, t1 REAL, pnl REAL, size_usd REAL, at REAL, pool TEXT, token TEXT,
    pnl0 REAL, real REAL);
CREATE INDEX IF NOT EXISTS copy_trades_owner ON copy_trades(owner, t0);
CREATE TABLE IF NOT EXISTS copies(leader_tid INTEGER PRIMARY KEY, leader TEXT, our_tid INTEGER, pool TEXT, symbol TEXT, size_usd REAL,
    leader_usd REAL, quote_only INTEGER, tick_lower INTEGER, tick_upper INTEGER, entry_ts REAL, exit_ts REAL, held_min REAL,
    back_usd REAL, fees_usd REAL, pnl_usd REAL, pnl_pct REAL, leader_realized_usd REAL, leader_realized_pct REAL, status TEXT, updated REAL);
"""


def connect():
    con = sqlite3.connect(DB, timeout=300)
    con.execute('PRAGMA journal_mode=WAL')          # the desk, the screen and a human read and write at once
    con.execute('PRAGMA busy_timeout=300000')
    con.execute('PRAGMA synchronous=NORMAL')        # WAL + NORMAL: durable at every checkpoint, far fewer fsyncs
    con.executescript(SCHEMA)
    # a table made by an older schema gets the columns added since (CREATE IF NOT EXISTS keeps the old shape)
    import re
    for name, cols in re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)\((.*?)\);', SCHEMA, re.S):
        have = {r[1] for r in con.execute('PRAGMA table_info(%s)' % name)}
        for c in re.sub(r'PRIMARY KEY\s*\([^)]*\)', '', cols).split(','):
            c = c.strip().split()
            if c and c[0] not in have:
                con.execute('ALTER TABLE %s ADD COLUMN %s %s' % (name, c[0], c[1] if len(c) > 1 else ''))
    return con


class writing:
    """A short write transaction: BEGIN IMMEDIATE (take the write lock up front, so two writers queue instead of one
    failing mid-way), commit, retry on 'locked'. Never make a network call inside one: collect first, then write."""

    def __init__(self, con):
        self.con = con

    def __enter__(self):
        begin(self.con)
        return self.con

    def __exit__(self, et, ev, tb):
        if et:
            self.con.rollback()
            return False
        for a in range(20):
            try:
                self.con.commit()
                return False
            except sqlite3.OperationalError as e:
                if 'locked' not in str(e):
                    raise
                time.sleep(0.5 + a)
        raise sqlite3.OperationalError('database is locked (commit, 20 tries)')


def commit(con):
    for a in range(20):
        try:
            con.commit()
            return
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e):
                raise
            time.sleep(0.5 + a)
    raise sqlite3.OperationalError('database is locked (commit, 20 tries)')


def begin(con):
    """BEGIN IMMEDIATE with retries (an open transaction is committed first)."""
    if con.in_transaction:
        con.commit()
    for a in range(30):
        try:
            con.execute('BEGIN IMMEDIATE')
            return
        except sqlite3.OperationalError as e:
            if 'locked' not in str(e):
                raise
            time.sleep(0.5 + a)
    raise sqlite3.OperationalError('database is locked (begin, 30 tries)')


def tick_usd(tick, usdg_c0, d0, d1):
    """USDG per whole token at a tick of a USDG pair."""
    p = 1.0001 ** tick * 10 ** d0 / 10 ** d1                        # token1 per token0
    return 1 / p if usdg_c0 else p


def enrich(con, D, rows, batch, usdg):
    """Pool, pair, fee, hooks and range of every position not yet enriched: its mint receipts (ModifyLiquidity: pool,
    ticks, liquidity per NFT) and PositionManager.poolKeys, public batch RPC, once per position ever."""
    from eth_utils import keccak
    mod = '0x' + keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
    have = {r[0] for r in con.execute('SELECT DISTINCT ladder FROM legs WHERE owner = ?', (D['owner'],))}
    todo = [r for r in rows if r['tokenId'] not in have]
    mints = sorted({D['mint'][t] for r in todo for t in r['tokenIds'] if t in D['mint']})
    rec = {}
    for i in range(0, len(mints), 50):
        rec.update(zip(mints[i:i + 50], batch([('eth_getTransactionReceipt', [h]) for h in mints[i:i + 50]])))
    sg = lambda x: (lambda v: v - (1 << 256) if v >> 255 else v)(int(x, 16))
    ladder_of = {t: r['tokenId'] for r in todo for t in r['tokenIds']}
    pids = set()
    legrows = []
    for h, r in rec.items():
        for l in (r or {}).get('logs', []):
            if l['topics'][0] != mod or l['topics'][2][-40:] != '58daec3116aae6d93017baaea7749052e8a04fa7':
                continue
            w = l['data'][2:]
            tid = int(w[192:256], 16)
            if tid in ladder_of and sg(w[128:192]) > 0:
                legrows.append((D['owner'], ladder_of[tid], tid, l['topics'][1], sg(w[0:64]), sg(w[64:128]), str(sg(w[128:192]))))
                pids.add(l['topics'][1])
    known = {r[0] for r in con.execute('SELECT pid FROM pools')}
    new = sorted(pids - known)
    sel = keccak(text='poolKeys(bytes25)')[:4].hex()
    poolrows = []
    for i in range(0, len(new), 50):
        got = batch([('eth_call', [dict(to='0x58daec3116aae6d93017baaea7749052e8a04fa7', data='0x' + sel + p[2:52].ljust(64, '0')), 'latest'])
                     for p in new[i:i + 50]])
        for p, x in zip(new[i:i + 50], got):
            if x and len(x) >= 2 + 64 * 5:
                wd = lambda k, x=x: int(x[2 + 64 * k:66 + 64 * k], 16)
                ts = wd(3) & 0xffffff
                poolrows.append((p, '0x%040x' % wd(0), '0x%040x' % wd(1), wd(2), ts - (1 << 24) if ts >> 23 else ts, '0x%040x' % wd(4)))
    with writing(con):
        con.executemany('INSERT OR REPLACE INTO legs VALUES (?,?,?,?,?,?,?)', legrows)
        con.executemany('INSERT OR REPLACE INTO pools VALUES (?,?,?,?,?,?)', poolrows)


def save_report(R, D, rows, opens, batch, symbol, usdg):
    """Write one lpwallet report: positions (with their pools and ranges), daily, the run, the open marks, tokens."""
    con = connect()
    now = time.time()
    enrich(con, D, rows, batch, usdg)
    pools = {r[0]: r[1:] for r in con.execute('SELECT pid, currency0, currency1, fee_pips, tick_spacing, hooks FROM pools')}
    legs = {}
    for lad, pid, tl, tu in con.execute('SELECT ladder, pool, tick_lower, tick_upper FROM legs WHERE owner = ?', (D['owner'],)):
        legs.setdefault(lad, []).append((pid, tl, tu))
    # every symbol read over the network BEFORE the write transaction opens
    syms = {}
    for r in rows:
        lg = legs.get(r['tokenId']) or []
        c0, c1 = (pools.get(lg[0][0]) or (None, None))[:2] if lg else (None, None)
        tok = c1 if c0 == usdg else c0 if c1 == usdg else None
        if tok and tok not in syms:
            syms[tok] = symbol(tok)
    begin(con)
    for t, dec in D['dec'].items():
        con.execute('INSERT OR IGNORE INTO tokens VALUES (?,?,?)', (t, None, dec))
    for r in rows:
        lg = legs.get(r['tokenId']) or []
        pid = lg[0][0] if lg else None
        c0, c1, fee, _, hooks = pools.get(pid, (None, None, None, None, None))
        tok = c1 if c0 == usdg else c0 if c1 == usdg else None
        sym = syms.get(tok) if tok else None
        tl = min((x[1] for x in lg), default=None)
        tu = max((x[2] for x in lg), default=None)
        lo = hi = None
        if tok and tl is not None:
            uc0 = c0 == usdg
            d0, d1 = D['dec'].get(c0, 18), D['dec'].get(c1, 18)
            lo, hi = sorted((tick_usd(tl, uc0, d0, d1), tick_usd(tu, uc0, d0, d1)))
            if sym:
                con.execute('UPDATE tokens SET symbol = ? WHERE address = ?', (sym, tok))
        close_ts = D['tsof'](r['close_blk']) if r.get('close_blk') else None
        con.execute('INSERT OR REPLACE INTO positions(owner, ladder, token_ids, n_legs, pool, currency0, currency1, token, symbol, '
                    'fee_pips, hooks, tick_lower, tick_upper, range_lo_usd, range_hi_usd, quote_only, open_blk, open_ts, close_blk, close_ts, '
                    'age_h, closed, invested_usd, invested_eth, returned_usd, fees_usd, realized_usd, realized_pct, unsold, unpriced, updated) '
                    'VALUES (%s)' % ','.join('?' * 31), (
            D['owner'], r['tokenId'], json.dumps(r['tokenIds']), len(r['tokenIds']), pid, c0, c1, tok, sym, fee, hooks, tl, tu, lo, hi,
            None, r['open_blk'], r['t_open'], r.get('close_blk'), close_ts, r.get('age_h'), int(bool(r['closed'])),
            r['invested_usd'], r['invested_eth'], r.get('returned_usd'), r.get('fee_usd'), r.get('realized_usd'), r.get('realized_pct'),
            json.dumps(r.get('unsold') or []), int(bool(r.get('unpriced'))), now))
    # skipped positions are stored too (their NFTs and why), so every NFT the wallet minted is accounted for
    for k in D.get('skips', []):
        con.execute('INSERT OR REPLACE INTO positions(owner, ladder, token_ids, n_legs, open_blk, open_ts, closed, skip, updated) '
                    'VALUES (?,?,?,?,?,?,?,?,?)', (D['owner'], k['tokenIds'][0], json.dumps(k['tokenIds']), len(k['tokenIds']), k['open_blk'],
                                                   D['tsof'](k['open_blk']) if k['open_blk'] else None, None, k['why'], now))
    for day, x in R['daily'].items():
        yr = time.strftime('%Y', time.gmtime())
        con.execute('INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?)', (
            D['owner'], '%s-%s' % (yr, day), x['n'], x['won'], x['usd'], x['gas'], x['net'], x['invested']))
    w = R['wallet']
    con.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        D['owner'], now, D['head'], R['days'], R['lp_pnl'], R['realized_usd'], w['explained']['open_upnl'], R['gas_usd'],
        R['open_usd'], R['positions'], int(bool(w['reconciled'])), w['unexplained'], json.dumps(R['skipped']), R['secs']))
    for o in opens:
        con.execute('INSERT OR REPLACE INTO open_marks VALUES (?,?,?,?,?,?,?,?,?,?,?)', (
            D['owner'], now, o['tokenId'], o['pair'], o['value_usd'], o['fee_usd'], int(bool(o['in_range'])),
            o['range_usdg'][0], o['range_usdg'][1], o.get('invested_usd'), o.get('upnl_usd')))
    commit(con)
    con.close()


def save_result(m):
    con = connect()
    begin(con)
    con.execute('INSERT OR REPLACE INTO results(owner, win, at, head, score, j) VALUES (?,?,?,?,?,?)', (
        m['address'], m.get('days_window'), time.time(), None, m.get('score'), json.dumps(m)))
    commit(con)
    con.close()


def results(win=None):
    con = connect()
    q = 'SELECT j FROM results' + (' WHERE win >= ?' if win else '')
    out = {}
    for (j,) in con.execute(q, (win,) if win else ()):
        m = json.loads(j)
        if m['address'] not in out or m.get('days_window', 0) > out[m['address']].get('days_window', 0):
            out[m['address']] = m
    con.close()
    return out


def label(owner, lab, note=''):
    con = connect()
    begin(con)
    con.execute('INSERT OR REPLACE INTO wallets VALUES (?,?,?,?)', (owner.lower(), lab, note, time.time()))
    commit(con)
    con.close()


def save_copy(leader, o, status):
    """One copy desk trade (lpfollow calls this on open and on close)."""
    con = connect()
    begin(con)
    # the leader's realized side is filled later by link_leader; a re-save of the copy must not wipe it
    lr = con.execute('SELECT leader_realized_usd, leader_realized_pct FROM copies WHERE leader_tid = ?', (int(o['leader_tid']),)).fetchone() or (None, None)
    con.execute('INSERT OR REPLACE INTO copies VALUES (%s)' % ','.join('?' * 21), (
        int(o['leader_tid']), leader, o.get('token_id'), o['pool'], o.get('symbol'), o['size_usd'], o.get('leader_usd'),
        int(bool(o.get('quote_only'))), o['tick_lower'], o['tick_upper'], o['entry_ts'], o.get('exit_ts'),
        (o['exit_ts'] - o['entry_ts']) / 60 if o.get('exit_ts') else None, o.get('back_usd'), o.get('fees_usd'), o.get('pnl_usd'),
        100 * o['pnl_usd'] / o['size_usd'] if o.get('pnl_usd') is not None else None, lr[0], lr[1], status, time.time()))
    commit(con)
    con.close()


def link_leader(con=None):
    """Fill each copy's leader_realized_* from the leader's own position (the ladder holding that NFT), once lpwallet has
    measured it: the copy-vs-leader comparison the dashboard needs."""
    own = con is None
    con = con or connect()
    for tid, leader in con.execute('SELECT leader_tid, leader FROM copies WHERE leader_realized_usd IS NULL AND status = ?', ('closed',)).fetchall():
        r = con.execute("SELECT realized_usd, realized_pct FROM positions WHERE owner = ? AND (ladder = ? OR token_ids LIKE ?)",
                        (leader, tid, '%%%d%%' % tid)).fetchone()
        if r and r[0] is not None:
            con.execute('UPDATE copies SET leader_realized_usd = ?, leader_realized_pct = ? WHERE leader_tid = ?', (r[0], r[1], tid))
    con.commit()
    if own:
        con.close()


def verify(owner):
    """Is this wallet's stored data solid? Each check names what it compares; any failure means do not chart it yet.
    1 the stored positions add up to the latest run's realized P&L (same window)
    2 the daily table adds up to it too
    3 every stored log lies inside the wallet's covered block range, and the range has no hole (lo < hi)
    4 every position NFT minted to it in the window is a stored position or a skip the run counted
    5 every USDG-pair position has its pool, pair, fee and range (the enrichment)"""
    from copydesk import lpwallet as W
    con = connect()
    out, ok = [], True
    run = con.execute('SELECT at, head, win, realized_usd, positions, skipped FROM runs WHERE owner = ? ORDER BY at DESC LIMIT 1',
                      (owner,)).fetchone()
    if not run:
        return False, ['no run stored']
    at, head, win, realized, npos, skipped = run
    frm = head - int(win * W.DAY)
    s = con.execute('SELECT COALESCE(SUM(realized_usd), 0), COUNT(*) FROM positions WHERE owner = ? AND closed = 1 AND open_blk >= ? '
                    'AND realized_usd IS NOT NULL', (owner, frm)).fetchone()
    c1 = abs(s[0] - realized) < 0.01
    out.append('%s 1 positions sum $%.2f vs run $%.2f (%d positions)' % ('ok  ' if c1 else 'FAIL', s[0], realized, s[1]))
    # the run's own window only: the daily table also holds days from longer runs
    day0 = time.strftime('%Y-%m-%d', time.gmtime(at - win * 86400))
    d = con.execute('SELECT COALESCE(SUM(realized_usd), 0) FROM daily WHERE owner = ? AND day >= ?', (owner, day0)).fetchone()[0]
    c2 = abs(d - realized) < 0.01 * max(1, abs(realized)) + 0.05
    out.append('%s 2 daily sum $%.2f vs run $%.2f' % ('ok  ' if c2 else 'FAIL', d, realized))
    lo, hi = con.execute('SELECT lo, hi FROM cover WHERE owner = ?', (owner,)).fetchone()
    mn, mx = con.execute('SELECT MIN(blk), MAX(blk) FROM logs WHERE owner = ?', (owner,)).fetchone()
    c3 = lo < hi and (mn is None or (lo <= mn and mx < hi))
    out.append('%s 3 cover [%d, %d) holds logs %s..%s' % ('ok  ' if c3 else 'FAIL', lo, hi, mn, mx))
    pad = owner[2:]
    minted = {int(t3, 16) for (t3,) in con.execute(
        "SELECT t3 FROM logs WHERE owner = ? AND address = ? AND blk >= ? AND t3 IS NOT NULL AND t1 = ? AND substr(t2, 27) = ?",
        (owner, W.POSM, frm, '0x' + '0' * 64, pad))}
    stored, skipped_n = set(), 0
    for ids, sk in con.execute('SELECT token_ids, skip FROM positions WHERE owner = ?', (owner,)):
        stored |= set(json.loads(ids))
        skipped_n += bool(sk)
    missing = minted - stored
    c4 = not missing
    out.append('%s 4 NFTs minted %d: in priced positions or stored skips %d, missing %d%s' % (
        'ok  ' if c4 else 'FAIL', len(minted), len(minted & stored), len(missing), ' e.g. %s' % sorted(missing)[:3] if missing else ''))
    tot, enr = con.execute("SELECT COUNT(*), SUM(pool IS NOT NULL AND range_lo_usd IS NOT NULL) FROM positions WHERE owner = ? AND open_blk >= ? "
                           "AND skip IS NULL", (owner, frm)).fetchone()
    c5 = tot == 0 or (enr or 0) / tot >= 0.95
    out.append('%s 5 enriched %d / %d positions (pool, pair, fee, range)' % ('ok  ' if c5 else 'FAIL', enr or 0, tot))
    con.close()
    return c1 and c2 and c3 and c4 and c5, out


def refresh(owners, days, log=print):
    """Re-report every wallet (warm: only new blocks are fetched) and verify it; returns the owners that are not solid."""
    from copydesk import lpwallet as W
    bad = []
    for i, o in enumerate(owners):
        t = time.time()
        try:
            R = W.report(o, days)
            good, lines = verify(o)
            if R.get('store_error'):
                good, lines = False, ['store error: ' + R['store_error']] + lines
        except Exception as e:
            good, lines = False, ['report failed: %s' % str(e)[:160]]
        if not good:
            bad.append(o)
        log('[%d/%d] %s %s (%.0f s)%s' % (i + 1, len(owners), o, 'SOLID' if good else 'NOT SOLID', time.time() - t,
                                          '' if good else '\n   ' + '\n   '.join(lines)))
    link_leader()                                               # the copies of these leaders get their comparison now
    return bad


if __name__ == '__main__':
    import sys
    con = connect()
    if len(sys.argv) > 1 and sys.argv[1] == 'refresh':
        days = float(sys.argv[2])
        owners = sys.argv[3:] or [r[0] for r in con.execute('SELECT owner FROM cover')]
        bad = refresh([o.lower() for o in owners], days)
        print('%d of %d not solid: %s' % (len(bad), len(owners), bad))
        sys.exit(1 if bad else 0)
    owners = sys.argv[2:] if len(sys.argv) > 2 else [r[0] for r in con.execute('SELECT DISTINCT owner FROM runs')]
    for o in owners:
        good, lines = verify(o.lower())
        print('%s  %s' % (o, 'SOLID' if good else 'NOT SOLID'))
        for l in lines:
            print('   ' + l)


def prune_raw(owner):
    """Drop a wallet's raw transfers and txs (the bulk of the store) and its cover, so a later run refetches them; the
    derived tables (positions, legs, daily, runs, open marks, results) stay. Used for wallets that do not qualify."""
    con = connect()
    begin(con)
    for t in ('logs', 'txs', 'cover'):
        con.execute('DELETE FROM %s WHERE owner = ?' % t, (owner,))
    commit(con)
    con.close()


def compact():
    """Block times no stored log uses, then VACUUM: SQLite only gives space back to the disk on a VACUUM."""
    con = connect()
    con.execute('DELETE FROM blocks WHERE n NOT IN (SELECT DISTINCT blk FROM logs)')
    con.commit()
    con.execute('VACUUM')
    con.close()


def save_replay(owner, days, real, model0, copy2, copy_at, delay_blocks=70, size_usd=None):
    """One copy replay (lpfollow.grid): keep = what a copy `delay_blocks` late keeps of the model at no delay, in [0, 1]
    (stored in keep7 for history; delay_blocks says which delay it was)."""
    con = connect()
    begin(con)
    keep = max(0.0, min(1.0, copy_at / model0)) if model0 > 0 else 0.0
    con.execute('INSERT OR REPLACE INTO replays(owner, at, days, real_usd, model0_usd, copy2_usd, copy7_usd, keep7, delay_blocks, size_usd) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)', (owner, time.time(), days, real, model0, copy2, copy_at, keep, delay_blocks, size_usd))
    commit(con)
    con.close()


def unreproducible():
    """Wallets whose replay model reproduces under half of their real P&L."""
    con = connect()
    rows = con.execute('SELECT owner, model0_usd, real_usd FROM replays WHERE size_usd IS NULL ORDER BY at').fetchall()
    con.close()
    latest = {o: (m, r) for o, m, r in rows}
    return sorted(o for o, (m, r) in latest.items() if (r or 0) > 0 and (m or 0) < 0.5 * r)


def last_copy(owner):
    """The close time of the wallet's latest stored copy, or None: where its next replay starts."""
    con = connect()
    r = con.execute('SELECT MAX(t1), SUM(pnl0 IS NULL) FROM copy_trades WHERE owner = ?', (owner,)).fetchone()
    cur = con.execute('SELECT 1 FROM kv WHERE k = ?', (REPLAY_V + owner,)).fetchone()
    con.close()
    # copies stored before pnl0 / real existed (2026-09-29 22:00) cannot make a summary: that wallet is replayed in full once;
    # so is a wallet not yet replayed by the current replay (REPLAY_V mark): its stored copies were priced another way. A
    # mark, not a date: a replay still running old code must not count as current
    return r[0] if r and r[0] and not r[1] and cur else None


REPLAY_V = 'replay:v12:'      # v3 (2026-09-30): native-ETH positions, each copy at the live lag of its kind (lpcopy.lag_table),
                             # trust against each exit's actual payout (lpcopy._paid) instead of the wallet's realized P&L
                             # v4 (2026-09-30): leader positions under min_leader_usd are not scored (the desk skips them)
                             # v5: min_leader_usd 20 -> 10
                             # v6: 9.5 days kept (walk-forward tests)
                             # v7: Delta ladders credited to their LPs; ladder payouts split across their bins (trust)
                             # v8 (2026-10-01): fees exact per range (L x the sqrt-price distance travelled inside it), not
                             # the whole swap's fee to a bin at its start or end; managed (Delta) payouts read gross from the
                             # PoolManager, before the manager's cut. Trust 0.31 -> 0.97 (0x620a), mains 0.91 -> 0.99, 0.90 -> 1.00
                             # v9: a managed (Delta) ladder is copied as ONE range over its bins (lpcopy.union), as the live desk does
                             # v10: the trust replay values the leader's exit at the swaps before its burn (lphist.exit_li)
                             # v11 (2026-10-02): min_leader_usd 10 -> 100
                             # v12 (2026-10-02 evening): min_leader_usd 100 -> 75


def mark_replayed(owner):
    """This wallet's store is the current replay's (lpfollow.replay_store, after a replay by this code)."""
    con = connect()
    con.execute('INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)', (REPLAY_V + owner, str(time.time())))
    con.commit()
    con.close()


def copy_score_blocks(days=7.0, size=25.0, gas=0.19, cap=1.0, k=1.0, end=None, block_h=3):
    """copy_score per 3-hour UTC block: owner -> {block start hour: $/day}, from only the copies opened in that block
    (the same cap, gas, standard-error penalty and replay trust). Fewer copies a block, a bigger penalty: a specialist
    must be good in its block, not just busy (lpopt.pick's quiet-hour specialists)."""
    con = connect()
    t1 = end or time.time()
    t0 = t1 - days * 86400
    rows = con.execute('SELECT owner, t0, pnl * ? / COALESCE(size_usd, ?), real, pnl0 FROM copy_trades WHERE t0 >= ? AND t1 <= ? '
                       'AND pnl0 IS NOT NULL', (size, size, t0, t1)).fetchall()       # each copy at `size` (stored at $25)
    con.close()
    tr, by = {}, {}
    for o, t, p, r, m in rows:
        a = tr.setdefault(o, [0.0, 0.0])
        if r is not None:                                           # trust from copies whose leader P&L is known (not ETH pools)
            a[0] += r
            a[1] += m or 0.0
        by.setdefault(o, {}).setdefault(time.gmtime(t).tm_hour // block_h * block_h, []).append(max(-cap * size, min(cap * size, p)) - gas)
    out = {}
    for o, bb in by.items():
        real, m0 = tr[o]
        trust = min(m0 / real, real / m0) if real > 0 and m0 > 0 else 0.0
        for b, xs in bb.items():
            n = len(xs)
            mu = sum(xs) / n
            se = (sum((x - mu) ** 2 for x in xs) / (n - 1)) ** 0.5 / n ** 0.5 if n > 1 else abs(mu)
            v = (mu - k * se) * n / days
            out.setdefault(o, {})[b] = v * trust if v > 0 else v
    return out


def copy_score(days=7.0, size=25.0, gas=0.19, cap=1.0, k=1.0, end=None):
    """THE COPY SCORE (owner 2026-09-30: fix the score, not a gate): owner -> (score $/day, dict of the parts). What copying
    the wallet can be expected to earn a day, after uncertainty, from its replayed copies at our size and delay:
    - each copy capped at +-cap x size (a pumping launch token counts as a good copy, not a jackpot) minus our gas;
    - (mean - k x standard error) x copies a day: a steady wallet keeps its mean, a week that is one lucky copy has a
      standard error as big as its mean and sinks by itself;
    - x trust = min(model/real, real/model) of the no-delay replay vs the leader's own P&L: a replay that cannot
      reproduce its leader counts partly (a 2x model counts half), never nothing and never in full."""
    import math
    con = connect()
    t1 = end or time.time()                                         # end: score a past window (walk-forward tests)
    t0 = t1 - days * 86400
    # each copy scaled to `size` (replays are stored at $25): LP P&L scales with size, our gas a copy does not
    rows = con.execute('SELECT owner, pnl * ? / COALESCE(size_usd, ?), real, pnl0 FROM copy_trades WHERE t0 >= ? AND t1 <= ? '
                       'AND pnl0 IS NOT NULL', (size, size, t0, t1)).fetchall()
    con.close()
    by = {}
    for o, p, r, m in rows:
        b = by.setdefault(o, [[], 0.0, 0.0])
        b[0].append(max(-cap * size, min(cap * size, p)) - gas)
        if r is not None:                                           # trust from copies whose leader P&L is known (not ETH pools)
            b[1] += r
            b[2] += m or 0.0
    out = {}
    for o, (xs, real, m0) in by.items():
        n = len(xs)
        mean = sum(xs) / n
        se = (sum((x - mean) ** 2 for x in xs) / (n - 1)) ** 0.5 / n ** 0.5 if n > 1 else abs(mean)
        trust = min(m0 / real, real / m0) if real > 0 and m0 > 0 else 0.0
        per_day = (mean - k * se) * n / days
        out[o] = (per_day * trust if per_day > 0 else per_day, dict(n=n, mean=mean, se=se, trust=trust, per_day=per_day))
    return out


def copy_summary(owner, days, size):
    """The wallet's replay summary FROM ITS STORED COPIES over the last `days` (same numbers whether the copies came from
    a full or an incremental replay): (leader real $, our copy $ at no delay, our copy $ at our delay)."""
    con = connect()
    r = con.execute('SELECT SUM(real), SUM(CASE WHEN real IS NOT NULL THEN pnl0 END), SUM(pnl) FROM copy_trades WHERE owner = ? AND t0 >= ?',
                    (owner, time.time() - days * 86400)).fetchone()
    con.close()
    return tuple(x or 0.0 for x in r)


_BORN = dict(at=0.0, h=None, born={})


def young_at(pool, t):
    """NO LAUNCHES, in the replay too (owner 2026-10-04): was `pool` younger than [lp.follow] min_pool_age_h at time t? The
    live desk skips those copies, so a wallet must not be scored on them (the repick kept main 0xdf4c, whose replay looked
    fine, through HOODAGENTS -$86.20 at 14 min). Birth = the pool's first swap in the history store (copydesk/lphist); a
    pool the store does not see being born (older than it, or unknown) is not young. One query per 5 min (~1.3 s)."""
    if time.time() - _BORN['at'] > 300 or (pool and pool.lower() not in _BORN['born'] and time.time() - _BORN['at'] > 60):
        from core import chain as cm
        from copydesk import lphist
        c = cm.load('robinhood')
        _BORN['h'] = float(c.cfg['lp']['follow'].get('min_pool_age_h') or 0)
        R = lphist.Reader(c)
        lo = R.bt[0][0] if R.bt else 0
        bb, tt = [b for b, _ in R.bt], [t_ for _, t_ in R.bt]
        import bisect

        def ts(b):
            i = min(max(bisect.bisect_left(bb, b), 1), len(bb) - 1)
            return tt[i - 1] + (tt[i] - tt[i - 1]) * (b - bb[i - 1]) / max(bb[i] - bb[i - 1], 1)
        pid = {k: v[0].lower() for k, v in R.pid.items()}
        _BORN['born'] = {pid[n]: ts(b) for n, b in R.con.execute('SELECT pool, MIN(blk) FROM swaps GROUP BY pool')
                         if n in pid and b > lo + 72000}           # first swap 2 h+ after the store starts: a birth we saw
        _BORN['at'] = time.time()
    b = _BORN['born'].get((pool or '').lower())
    return bool(_BORN['h']) and b is not None and t - b < _BORN['h'] * 3600


def drop_young():
    """Move stored copies the live desk would now refuse (young_at) into copy_trades_young (kept for research, read by no
    scorer). Run once when the rule changed; save_trades filters after."""
    con = connect()
    rows = con.execute('SELECT rowid, pool, t0 FROM copy_trades').fetchall()
    gone = [(r,) for r, p, t in rows if young_at(p, t)]
    begin(con)
    con.execute('CREATE TABLE IF NOT EXISTS copy_trades_young AS SELECT * FROM copy_trades WHERE 0')
    con.executemany('INSERT INTO copy_trades_young SELECT * FROM copy_trades WHERE rowid = ?', gone)
    con.executemany('DELETE FROM copy_trades WHERE rowid = ?', gone)
    commit(con)
    con.close()
    return len(gone), len(rows)


def save_trades(owner, trades, size, since=None):
    """A replay's copies at OUR size and delay, one row each (lpfollow.grid): the simulators price a leader from these
    instead of its own % x a keep rate, which cannot see that a small copy earns a different % than the leader."""
    con = connect()
    begin(con)
    cols = {r[1] for r in con.execute('PRAGMA table_info(copy_trades)')}
    for c, tp in (('pool', 'TEXT'), ('token', 'TEXT'), ('pnl0', 'REAL'), ('real', 'REAL')):
        if c not in cols:
            con.execute('ALTER TABLE copy_trades ADD COLUMN %s %s' % (c, tp))
    # KEEP THE HISTORY: only the days this replay covers are replaced, older copies stay, so the store grows a day each
    # night at no extra node work and lp pick can judge more than the replay's own 7 days (owner 2026-09-29)
    # since: an incremental replay re-did the copies that closed after it - exactly those are replaced
    if since:
        con.execute('DELETE FROM copy_trades WHERE owner = ? AND t1 > ?', (owner, since))
    else:
        lo = min((t['t0'] for t in trades), default=None)
        if lo is not None:                                          # a full replay: its window, and any row from before pnl0
            con.execute('DELETE FROM copy_trades WHERE owner = ? AND (t0 >= ? OR pnl0 IS NULL)', (owner, lo))
    con.executemany('INSERT INTO copy_trades(owner, t0, t1, pnl, size_usd, at, pool, token, pnl0, real) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    [(owner, t['t0'], t['t1'], t['model'], size, time.time(), t.get('pool'), t.get('token'), t.get('model0'), t.get('real_ours'))
                     for t in trades if not young_at(t.get('pool'), t['t0'])])
    commit(con)
    con.close()


def replay_ev(max_age_d=3):
    """owner -> (replayed copy $ per day at OUR size and delay, when). The bench score (owner 2026-09-29): a wallet that
    opens more copyable positions earns more copies, so activity counts in dollars, not as a separate weight."""
    con = connect()
    rows = con.execute('SELECT owner, copy7_usd, days, at FROM replays WHERE size_usd IS NOT NULL ORDER BY at').fetchall()
    con.close()
    return {o: (c / d, at) for o, c, d, at in rows if d and time.time() - at <= max_age_d * 86400}


def keep_rates(max_age_d=14):
    """owner -> the latest replay's keep rate at our size (replays older than max_age_d do not count)."""
    con = connect()
    out = {}
    # the replay at OUR delay (33 blocks) when there is one; else the 2 s copy as the nearer estimate of an older replay
    # replays at OUR size are the truth; a leader-size replay (size_usd empty) only counts when there is none
    rows = con.execute('SELECT owner, keep7, at, delay_blocks, copy2_usd, model0_usd, size_usd, real_usd FROM replays ORDER BY at').fetchall()
    sized = {r[0] for r in rows if r[6]}
    # a replay whose no-delay model reproduces under half of the wallet's real P&L cannot vouch for copying it
    # (0xcc041520: model $1,111 vs real $5,675; negative per $25): such a wallet is never recommended
    latest = {r[0]: r for r in rows if not r[6]}                    # each wallet's latest leader-size replay decides
    bad = {o for o, r in latest.items() if (r[7] or 0) > 0 and (r[5] or 0) < 0.5 * r[7]}
    rows = [r[:7] for r in rows if r[0] not in bad]
    for o, k, at, d, c2, m0, sz in rows:
        if o in sized and not sz:
            continue
        if not sz and (d or 70) > 40 and m0 and m0 > 0:
            k = max(0.0, min(1.0, c2 / m0))
        if time.time() - at <= max_age_d * 86400:
            out[o] = k
    con.close()
    return out
