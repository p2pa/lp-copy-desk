"""lp qc: QUALITY CONTROL of wallets before they join the squad (owner 2026-09-30). Per wallet, 7 days at the live copy size:
score and trust (7 / 3 / 1 days: a trust that swings with the window is a noisy one), green days, P&L without its 5 best
copies, the share of its winnings from one pool / one token, median hold, share of gas-only ($0) copies, overlap with the
current squad's pools, the worst burst (copies in one minute: the desk copies one at a time), and whether it is a contract.
Flags what fails. Without addresses: every wallet not followed that clears the filler bar and was replayed in the last day."""
import collections, statistics as S, time

from copydesk import lpdb, lpfollow as L


def quality(chain, addrs=None, hours=24.0, gas=0.069, end=None, code=True):
    """end: judge as of that time from the copies closed before it (walk-forward tests); code=False skips the contract read."""
    size, book = L.live_size(chain)
    own = L.own_wallets()
    mains, bench, fill = L.leaders(chain), [b for b in L.bench(chain) if b not in L.leaders(chain)], L.filler(chain)
    con = lpdb.connect()
    sc7, sc3, sc1 = (lpdb.copy_score(d, size=size, gas=gas, end=end) for d in (7, 3, 1))
    if not addrs:
        fresh = {o for (o,) in con.execute("SELECT substr(k, -42) FROM kv WHERE k LIKE ? AND CAST(v AS REAL) >= ?",
                                           (lpdb.REPLAY_V + '%', time.time() - hours * 3600))}
        bar = float(L.cfg(chain).get('filler_min_score', 10.0))
        addrs = [o for o in fresh if o in sc7 and sc7[o][0] >= bar and o not in set(mains + bench + fill) | own]
    new = [a.lower() for a in addrs]
    now, day = end or time.time(), 86400.0
    squad = mains + bench
    squad_pools = {p for (p,) in con.execute('SELECT DISTINCT pool FROM copy_trades WHERE t0 >= ? AND owner IN (%s)' % ','.join('?' * len(squad)),
                                              [now - 7 * day] + squad)} if squad else set()
    code = dict(zip(new, L._batch([('eth_getCode', [a, 'latest']) for a in new]))) if new and code else {}
    v3 = {o for (o,) in con.execute("SELECT substr(k, -42) FROM kv WHERE k LIKE ?", (lpdb.REPLAY_V + '%',))}

    def qc(a):
        rows = con.execute('SELECT t0, t1, pnl, size_usd, pool, token FROM copy_trades WHERE owner = ? AND t0 >= ? AND t1 <= ?',
                           (a, now - 7 * day, now)).fetchall()
        if not rows:
            return None
        xs = [(t0, t1, max(-size, min(size, p * size / (sz or 25))) - gas, pool, tok) for t0, t1, p, sz, pool, tok in rows]
        daily = collections.Counter()
        for t0, t1, x, *_ in xs:
            daily[int((now - t0) // day)] += x
        days = [daily.get(k, 0.0) for k in range(6, -1, -1)]
        xv = sorted((x for _, _, x, *_ in xs), reverse=True)
        pos = sum(x for x in xv if x > 0) or 1e-9
        bp, bt = collections.Counter(), collections.Counter()
        for t0, t1, x, pool, tok in xs:
            if x > 0:
                bp[pool] += x
                bt[tok] += x
        mins = collections.Counter(int(t0 // 60) for t0, *_ in xs)
        g = lambda s, k: s.get(a, (float('nan'), {}))[1].get(k, float('nan')) if a in s else float('nan')
        return dict(
            score=sc7[a][0] if a in sc7 else float('nan'), trust7=g(sc7, 'trust'), trust3=g(sc3, 'trust'), trust1=g(sc1, 'trust'),
            n=len(xs), total=sum(xv), without5=sum(xv[5:]), green=sum(1 for d in days if d > 0), days=days,
            top_pool=max(bp.values()) / pos if bp else 0, top_tok=max(bt.values()) / pos if bt else 0,
            hold=S.median((t1 - t0) / 60 for t0, t1, *_ in xs), zero=sum(1 for x in xv if abs(x + gas) < 0.05) / len(xv),
            overlap=sum(1 for *_, pool, tok in xs if pool in squad_pools) / len(xs), burst=max(mins.values()),
            contract=len(code.get(a) or '0x') > 2, v3=a in v3)



    rows = [(a, qc(a)) for a in new]
    con.close()
    return sorted([(a, q) for a, q in rows if q], key=lambda z: -(z[1]['score'] if z[1]['score'] == z[1]['score'] else -1e9)), size


FLAGS = [('not replayed by the current replay', lambda q: not q['v3']), ('low trust', lambda q: q['trust7'] < 0.6),
         ('trust unstable', lambda q: max(abs(q['trust1'] - q['trust7']), abs(q['trust3'] - q['trust7'])) > 0.3),
         ('lives on 5 copies', lambda q: q['without5'] <= 0), ('one pool', lambda q: q['top_pool'] > 0.5),
         ('few green days', lambda q: q['green'] <= 3), ('mostly $0 copies', lambda q: q['zero'] > 0.5), ('contract', lambda q: q['contract'])]


# THE FLAGS lp pick ENFORCES (owner 2026-09-30: pick does quality control itself): a wallet failing any of these is never
# picked for any tier. The others (trust unstable, $0 copies, contract) are shown by `lp qc` but do not block.
HARD = ('not replayed by the current replay', 'low trust', 'lives on 5 copies', 'one pool', 'few green days')


def turned_down(chain, addrs, end=None):
    """{wallet: [its hard flags]} for the wallets among addrs that fail quality control."""
    rows, _ = quality(chain, addrs=sorted(addrs), end=end, code=end is None) if addrs else ([], 0)
    return {a: fl for a, q in rows for fl in [[n for n, f in FLAGS if n in HARD and f(q)]] if fl}


def lines(rows, size):
    out = ['QUALITY CONTROL - 7 days at $%.0f a copy (trust = the replay vs what the exits paid)' % size,
           '%-8s %8s %5s %5s %5s %6s %6s %8s %9s %6s %6s %6s %6s %5s %5s %s' % (
               'wallet', '$/day', 'tr7', 'tr3', 'tr1', 'green', 'copies', '7d $', 'w/o top5', 'toppl', 'toptok', 'hold', '$0 cp', 'ovlap', 'burst', 'flags')]
    for a, q in rows:
        fl = [n for n, f in FLAGS if f(q)]
        out.append('%-8s %8.2f %5.2f %5.2f %5.2f %4d/7 %6d %+8.0f %+9.0f %5.0f%% %5.0f%% %5.0fm %5.0f%% %4.0f%% %5d %s' % (
            a[:8], q['score'], q['trust7'], q['trust3'], q['trust1'], q['green'], q['n'], q['total'], q['without5'], 100 * q['top_pool'],
            100 * q['top_tok'], q['hold'], 100 * q['zero'], 100 * q['overlap'], q['burst'], ', '.join(fl) or 'clean'))
        out.append('%8s daily, oldest first: %s' % ('', ' '.join('%+.0f' % d for d in q['days'])))
    return out
