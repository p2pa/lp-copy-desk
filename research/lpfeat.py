"""DOES THE POOL PREDICT THE COPY? (owner 2026-09-30, the factor recipe: zscore / X-over-size / ts_mean / ts_std / ts_sharpe on
hourly bars). For every replayed copy (lpdb.copy_trades, our size and lag) the pool's state at entry, from the history store
(copydesk/lphist), using only the hours BEFORE the entry hour:

  lvol24 / lvol72   log $ volume over 24 / 72 h
  turn24 / turn72   volume / the $ depth of the active liquidity within +-2% of the price at entry (X over size)
  volz              the last hour's volume as a z-score of the hourly volumes over 72 h (the store keeps 10 days, not 30)
  rv24 / rv72       std of the hourly log price changes (ts_std)
  mom24             the token's log return over 24 h (ts_mean x 24)
  shp72             mean / std of the token's hourly log returns over 72 h (ts_sharpe)
  ldepth            log $ depth at entry
  nsw24             swaps in 24 h

SAMPLED PROPERLY: copies of one pool opened in the same hour are one sample (their mean), so a burst of 30 copies of one
ladder counts once. Judged BLIND BY DAY: each UTC day's rank correlation (Spearman) of feature vs copy P&L %, then the
mean over days, its t-stat and on how many days it had the same sign; and the top-minus-bottom quintile per day.
`python -m research.lpfeat [--days 9]`."""
import bisect, collections, math, statistics as S, sys, time

sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__))))
from copydesk import lpcopy as C, lpdb

H = 3600
FEATS = ['lvol24', 'lvol72', 'turn24', 'turn72', 'volz', 'rv24', 'rv72', 'mom24', 'shp72', 'ldepth', 'nsw24', 'retail24', 'lretail24', 'turnret24',
         'imb24', 'aimb24', 'pop24', 'popimb24', 'apopimb24', 'fee', 'edgevol', 'feeyield', 'netlvr']
POP = {}


def popular_sizes(R, min_n=30, min_tok_n=8):
    """THE SIZES PEOPLE TYPE, FOUND IN THE DATA (owner 2026-09-30: sample unusually popular sizes empirically rather than
    hardcode round numbers): exact input amounts that repeat. A continuous amount almost never repeats exactly, so any
    input seen min_n times on one quote token (USDG / ETH) is a preset or a typed number - including the 99 / 148.5 / 198
    USDG of a router that takes 1% before the swap, which round-number rules miss. Sells: token amounts repeated
    min_tok_n times within one token. Sets POP[(quote or token)] = set of raw amounts."""
    if POP:
        return POP
    Q = (C.W.USDG, C.W.WETH, C.NATIVE)
    for side, col in ((0, 'c0'), (1, 'c1')):
        a = 'a%d' % side
        for tok, amt, n in R.con.execute('SELECT p.%s, s.%s, count(*) FROM swaps s JOIN pools p ON s.pool = p.n WHERE s.%s < 0 '
                                         'GROUP BY p.%s, s.%s HAVING count(*) >= ?' % (col, a, a, col, a), (min_tok_n,)):
            if tok in Q and n < min_n:
                continue
            POP.setdefault(tok, set()).add(-amt)
    return POP


def round_buy(q_raw, quote, eusd):
    """$ of a quote-in swap whose input is a ROUND amount a person types - a whole multiple of $5 of USDG, or of 0.005 ETH
    (exact-input swaps log exactly what was typed) - else 0. The retail signature (owner 2026-09-30)."""
    if quote == C.W.USDG:
        v = q_raw / 1e6
        return v if v >= 5 and q_raw % 5_000_000 == 0 else 0.0
    v = q_raw / 1e18
    return v * eusd if v >= 0.005 and q_raw % 5 * 10 ** 15 == 0 and q_raw % (5 * 10 ** 15) == 0 else 0.0


def rank(v):
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
            j += 1
        for k in range(i, j + 1):
            r[o[k]] = (i + j) / 2.0
        i = j + 1
    return r


def spearman(x, y):
    if len(x) < 10:
        return None
    rx, ry = rank(x), rank(y)
    mx, my = S.mean(rx), S.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def pool_hours(R, pid, key, sw, t_lo, t_hi):
    """hour -> [volume $, swaps, last log price of the TOKEN in its quote], and the depth/price state per swap for entry."""
    c0, c1 = key[0], key[1]
    q0 = c0 in (C.W.USDG, C.W.WETH, C.NATIVE)
    hrs = collections.defaultdict(lambda: [0.0, 0, None, 0.0, 0.0, 0.0, 0.0, 0.0])   # vol, n, px, round buys, buy, sell, pop buy, pop sell
    pop = popular_sizes(R)
    qt, tk = (c0, c1) if q0 else (c1, c0)
    pq, pt = pop.get(qt, set()), pop.get(tk, set())
    for blk, li, a0, a1, sp, act, fee in sw:
        t = R.tsof(blk)
        if not sp:
            continue
        h = int(t // H)
        usd = C._usd(c0, c1, abs(a0), 0.0, sp, R.dec, R.eusd, blk) if q0 else C._usd(c0, c1, 0.0, abs(a1), sp, R.dec, R.eusd, blk)
        b = hrs[h]
        b[0] += usd or 0.0
        b[1] += 1
        b[2] = (-2 if q0 else 2) * math.log(sp)                          # log token price (up to a constant), in the quote
        qa = a0 if q0 else a1                                           # the swapper's quote delta: negative = paid in (a buy)
        if qa < 0:
            b[3] += round_buy(int(round(-qa)), c0 if q0 else c1, R.eusd(blk))
            b[4] += usd or 0.0
            if -qa in pq:
                b[6] += usd or 0.0
        else:
            b[5] += usd or 0.0
            ta = a1 if q0 else a0                                       # a sell: the token paid in
            if ta < 0 and -ta in pt:
                b[7] += usd or 0.0
    return hrs


def features(hrs, H0, sp, act, key, R, blk, fee=None):
    """The pool at the start of hour H0 (only hours < H0)."""
    c0, c1 = key[0], key[1]
    v = lambda w: [hrs[h][0] if h in hrs else 0.0 for h in range(H0 - w, H0)]
    n = lambda w: sum(hrs[h][1] for h in range(H0 - w, H0) if h in hrs)
    px, last = [], None                                                 # hourly last price, carried forward
    for h in range(H0 - 73, H0):
        if h in hrs and hrs[h][2] is not None:
            last = hrs[h][2]
        px.append(last)
    ret = [b - a for a, b in zip(px, px[1:]) if a is not None and b is not None]
    tick = 2 * math.log(sp) / math.log(1.0001)
    a0, a1 = C._amounts(act, sp, tick - 200, tick + 200)
    depth = C._usd(c0, c1, a0, a1, sp, R.dec, R.eusd, blk) or 0.0
    v24, v72 = sum(v(24)), sum(v(72))
    rt24 = sum(hrs[h][3] for h in range(H0 - 24, H0) if h in hrs)
    sm = lambda j: sum(hrs[h][j] for h in range(H0 - 24, H0) if h in hrs)
    buy, sell, pb, ps = sm(4), sm(5), sm(6), sm(7)
    imb = (buy - sell) / (buy + sell) if buy + sell > 1 else None
    pimb = (pb - ps) / (pb + ps) if pb + ps > 1 else None
    hv = v(72)
    sd = S.pstdev(hv) if len(hv) > 1 else 0.0
    r24 = ret[-24:]
    return dict(lvol24=math.log1p(v24), lvol72=math.log1p(v72), turn24=v24 / depth if depth > 1 else None,
                turn72=v72 / depth if depth > 1 else None, volz=(hv[-1] - S.mean(hv)) / sd if sd else None,
                rv24=S.pstdev(r24) if len(r24) > 3 else None, rv72=S.pstdev(ret) if len(ret) > 3 else None,
                mom24=sum(r24) if r24 else None, shp72=(S.mean(ret) / S.pstdev(ret)) if len(ret) > 3 and S.pstdev(ret) > 0 else None,
                ldepth=math.log1p(depth), nsw24=n(24), retail24=rt24 / v24 if v24 > 1 else None, lretail24=math.log1p(rt24),
                turnret24=rt24 / depth if depth > 1 else None, imb24=imb, aimb24=abs(imb) if imb is not None else None,
                pop24=(pb + ps) / v24 if v24 > 1 else None, popimb24=pimb, apopimb24=abs(pimb) if pimb is not None else None,
                # EDGE VS THE VOLATILITY PENALTY (qfex, 'from fair value to a bid and ask'): the fee tier is an LP's edge per side;
                # an LP loses to arbitrage about sigma^2/8 a period (LVR, full range: a lower bound for a concentrated range)
                **edge(fee, v24 / depth if depth > 1 else None, S.pstdev(r24) if len(r24) > 3 else None))


def edge(fee, turn24, rv24):
    f = fee / 1e6 if fee is not None else None
    return dict(fee=f, edgevol=f / rv24 if f is not None and rv24 else None, feeyield=f * turn24 if f is not None and turn24 is not None else None,
                netlvr=f * turn24 - 24 * rv24 ** 2 / 8 if f is not None and turn24 is not None and rv24 is not None else None)


def main(days=9.0, log=print, owners=None):
    R = C._hist()
    first, last = R.span()
    t_first, t_last = R.tsof(first), R.tsof(last)
    con = lpdb.connect()
    rows = con.execute('SELECT pool, t0, pnl, size_usd, owner FROM copy_trades WHERE t0 >= ? AND t0 >= ?', (time.time() - days * 86400, t_first + 3 * 86400)).fetchall()
    rows = [r[:4] for r in rows if owners is None or r[4] in owners]         # owners: only these wallets' copies (the squad)
    con.close()
    by_pool = collections.defaultdict(list)
    for pid, t0, p, sz in rows:
        by_pool[pid].append((t0, max(-100.0, min(100.0, 100.0 * p / (sz or 25.0)))))
    bl = R.con.execute('SELECT blk, ts FROM blocks ORDER BY blk').fetchall()
    bts = [t for _, t in bl]
    tblk = lambda t: (lambda i: bl[max(0, i - 1)][0] + int((t - bl[max(0, i - 1)][1]) * 10))(bisect.bisect_left(bts, t))
    key = R.keys(list(by_pool))
    samples, t_ = [], time.time()
    for n_, (pid, cs) in enumerate(by_pool.items()):
        if pid not in key:
            continue
        lo, hi = tblk(min(t for t, _ in cs) - 74 * H), tblk(max(t for t, _ in cs))
        sw = R.swaps([(pid, lo, hi)]).get(pid) or []
        if not sw:
            continue
        hrs = pool_hours(R, pid, key[pid], sw, lo, hi)
        blks = [x[0] for x in sw]
        groups = collections.defaultdict(list)
        for t0, y in cs:
            groups[int(t0 // H)].append((t0, y))
        for H0, g in groups.items():
            t0 = min(t for t, _ in g)
            b0 = tblk(t0)
            i = bisect.bisect_right(blks, b0) - 1
            if i < 0:
                continue
            f = features(hrs, H0, sw[i][4], sw[i][5], key[pid], R, b0, fee=sw[i][6])
            f['y'] = S.mean(y for _, y in g)
            f['day'] = time.strftime('%m-%d', time.gmtime(t0))
            f['pid'], f['t0'] = pid, t0                                  # the sample's pool and entry time (research/lpfactor consensus)
            samples.append(f)
        if n_ % 500 == 0:
            log('  %d/%d pools, %d samples, %.0f s' % (n_, len(by_pool), len(samples), time.time() - t_))
    days_ = sorted({s['day'] for s in samples})
    out = ['lp feature study: %d copies -> %d samples (one per pool and entry hour), %d days (%s..%s); target = copy P&L %% at our size and lag'
           % (len(rows), len(samples), len(days_), days_[0], days_[-1]),
           '  %-7s %8s %7s %9s %11s   %s' % ('feature', 'mean IC', 't', 'same sign', 'Q5-Q1 days', 'P&L % by quintile, low -> high (all days)')]
    for fe in FEATS:
        ics, qd = [], []
        for d in days_:
            xs = [(s[fe], s['y']) for s in samples if s['day'] == d and s[fe] is not None]
            ic = spearman([a for a, _ in xs], [b for _, b in xs])
            if ic is not None:
                ics.append(ic)
            if len(xs) >= 50:
                xs.sort()
                k = len(xs) // 5
                qd.append(S.mean(b for _, b in xs[-k:]) - S.mean(b for _, b in xs[:k]))
        allx = sorted((s[fe], s['y']) for s in samples if s[fe] is not None)
        k = len(allx) // 5
        qs = [S.mean(b for _, b in allx[i * k:(i + 1) * k]) for i in range(5)] if k else []
        m = S.mean(ics) if ics else 0.0
        t = m / (S.stdev(ics) / math.sqrt(len(ics))) if len(ics) > 2 and S.stdev(ics) > 0 else 0.0
        same = sum(1 for x in ics if (x > 0) == (m > 0))
        out.append('  %-7s %+8.3f %+7.2f %6d/%-2d %7d/%-2d   %s' % (fe, m, t, same, len(ics), sum(1 for x in qd if (x > 0) == (m > 0)), len(qd),
                   ' '.join('%+6.2f' % q for q in qs)))
    for l in out:
        log(l)
    return out, samples


if __name__ == '__main__':
    a = sys.argv[1:]
    own = None
    if '--squad' in a:
        from copydesk import lpfollow as L
        from core import chain as cm
        c = cm.load('robinhood')
        own = set(L.leaders(c) + L.bench(c) + L.filler(c))
    main(days=float(a[a.index('--days') + 1]) if '--days' in a else 9.0, owners=own)
