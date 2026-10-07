"""WHAT WOULD COPYING AN LP WALLET HAVE MADE? Paper copy of its Uniswap v4 positions, from the chain (docs/LPWALLETS.md).

  python -m copydesk.lpcopy <address> [--days 7] [--delays 0,70,300] [--json]

Each of the wallet's closed positions (lpwallet.book) is replayed as ours: same pool, same ticks, same USD, entered `d`
blocks after its mint and exited `d` blocks after its close (100 ms blocks: 70 = 7 s, a typical copy lag).
- the position's pool, ticks and liquidity come from its mint receipt (ModifyLiquidity, salt = tokenId);
- the pool's price at any block is the sqrtPrice of its last Swap log at or before it (exactly slot0);
- fees are earned swap by swap: while the swap starts or ends inside our range, fee = input x the swap's own lpFee x our
  share of the active liquidity the log reports (plus our own liquidity when we are added on top of it);
- at exit the principal comes back at the pool price and the token side is sold at that price minus the pool fee;
- gas: the wallet's own LP gas per position.

d = 0 with the wallet's own liquidity is the VALIDATION: the model must reproduce the wallet's real realized P&L (from
lpwallet, which is checked against its balances) before any copy number is believed.
"""
import bisect, collections, json, os, statistics as S, sys, time

from eth_utils import keccak
from discovery import forensics as F
from copydesk import lpwallet as W

MOD = '0x' + keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
SWAP4 = '0x' + keccak(text='Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)').hex()


def _s(h):
    v = int(h, 16)
    return v - (1 << 256) if v >> 255 else v


def legs(D, rows):
    """tokenId -> (pid, tickLower, tickUpper, liquidity) from each ladder's mint receipt (public batch RPC, no archive)."""
    mints = sorted({D['mint'][t] for r in rows for t in r['tokenIds']})
    rec = dict(zip(mints, W.batch([('eth_getTransactionReceipt', [h]) for h in mints])))
    out = {}
    for h, r in rec.items():
        for l in (r or {}).get('logs', []):
            if l['topics'][0] == MOD and l['address'].lower() == F.PM and l['topics'][2][-40:] == W.POSM[2:]:
                d = l['data'][2:]
                dl = _s(d[128:192])
                if dl > 0:
                    out[int(d[192:256], 16)] = (l['topics'][1], _s(d[0:64]), _s(d[64:128]), dl)
    return out


def keys(pids):
    """pid -> (currency0, currency1, hooks) via PositionManager.poolKeys(bytes25), latest state (written once, never cleared)."""
    sel = keccak(text='poolKeys(bytes25)')[:4].hex()
    r = W.batch([W.call(W.POSM, '0x' + sel + p[2:52].ljust(64, '0'), pin=False) for p in pids])
    return {p: ('0x' + x[26:66].lower(), '0x' + x[90:130].lower(), '0x' + x[282:322].lower() if len(x) >= 322 else '0x0')
            for p, x in zip(pids, r) if x and len(x) >= 130}


def node_swaps(pids, frm, to):
    """The same as swaps() from an rpc: eth_getLogs in 1-day chunks, 25 pools a call - our node when there is one, else its public
    fallbacks (lpfollow._node_batch). ~1.4 s for 7 days of a pool on the node, against minutes on hypersync."""
    from copydesk import lpfollow as L
    rq = lambda p: dict(result=L._node_batch([('eth_getLogs', [p])], timeout=120)[0])
    s = collections.defaultdict(list)
    for i in range(0, len(pids), 25):
        for lo in range(frm, to + 1, W.DAY):
            r = rq(dict(fromBlock=hex(lo), toBlock=hex(min(to, lo + W.DAY - 1)), address=F.PM, topics=[SWAP4, pids[i:i + 25]]))
            if 'result' not in r:
                raise RuntimeError('node: %s' % r.get('error'))
            for l in r['result']:
                w = l['data'][2:]
                s[l['topics'][1]].append((int(l['blockNumber'], 16), int(l['logIndex'], 16), _s(w[0:64]), _s(w[64:128]),
                                          int(w[128:192], 16) / 2 ** 96, int(w[192:256], 16), int(w[320:384], 16)))
    for v in s.values():
        v.sort()
    return s


CHUNK = 100_000                          # blocks per getLogs (~2.8 h): a 1-3M block window timed out on the node
PAD = 300                                # blocks (30 s) before a position opens: its pool's last swap there is the entry price


def window_swaps(want, pad=PAD):
    """pid -> sorted swaps, read from our node ONLY inside the windows given: [(pid, lo, hi)]. The wallets we replay hold
    positions for minutes, so this is a few thousand blocks each, where a day-long eth_getLogs over 25 pools nine days
    back timed out on the node (2026-09-29: every replay then fell back to hypersync, 475 s of a 535 s run). Windows on
    one pool are merged; one query per request, 8 in flight (the node's CPU is the limit, not the count)."""
    from concurrent.futures import ThreadPoolExecutor
    by = collections.defaultdict(list)
    for pid, lo, hi in want:
        by[pid].append((max(0, lo), hi))
    q = []
    for pid, ws in by.items():
        ws.sort()
        cur = list(ws[0])
        for lo, hi in ws[1:]:
            if lo <= cur[1] + pad:
                cur[1] = max(cur[1], hi)
            else:
                q.append((pid, *cur))
                cur = [lo, hi]
        q.append((pid, *cur))
    from copydesk.lpfollow import _post
    call = lambda pid, lo, hi: dict(jsonrpc='2.0', id=0, method='eth_getLogs',
                                    params=[dict(fromBlock=hex(lo), toBlock=hex(hi), address=F.PM, topics=[SWAP4, pid])])

    def get(w):
        """One window -> its logs. One query per request (batches hit the node's response cap and then retried item by
        item: 40 s where single queries took 19 s); a window over the cap alone is split in half until it fits."""
        x = _post(json.dumps(call(*w)), 120)
        if 'result' in x:
            return x['result']
        if w[2] - w[1] > 1 and ('too large' in str(x.get('error')) or 'timed out' in str(x.get('error'))):
            m = (w[1] + w[2]) // 2
            return get((w[0], w[1], m)) + get((w[0], m + 1, w[2]))
        raise RuntimeError('node: %s' % str(x.get('error'))[:120])
    # a position held for days in a busy pool is millions of blocks: one getLogs over it times out on the node, so every
    # window goes in chunks of at most CHUNK blocks (and a chunk that still fails is halved above)
    q = [(pid, a, min(hi, a + CHUNK - 1)) for pid, lo, hi in q for a in range(lo, hi + 1, CHUNK)]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(get, q))
    s = collections.defaultdict(dict)
    for logs in res:
        for l in logs or []:
            w = l['data'][2:]
            b, li = int(l['blockNumber'], 16), int(l['logIndex'], 16)
            s[l['topics'][1]][(b, li)] = (b, li, _s(w[0:64]), _s(w[64:128]), int(w[128:192], 16) / 2 ** 96, int(w[192:256], 16),
                                          int(w[320:384], 16))
    return {k: sorted(v.values()) for k, v in s.items()}


def pos_swaps(pos, dmax):
    """Swaps for every position's life (+ dmax blocks of copy lag); a pool with no swap in the PAD before a position opens
    is asked again further back, so every position has an entry price."""
    sw = window_swaps([(pid, p['open'] - PAD, p['close'] + dmax + 5) for p in pos for pid, *_ in p['legs']])
    # further back in steps (5 min, 50 min, 8 h, 3.5 d) for a pool with no swap that close to the open
    reach = PAD
    for step in (10, 100, 1000, 10000):
        miss = [(pid, p['open'] - step * PAD, p['open'] - reach) for p in pos for pid, *_ in p['legs']
                if not sw.get(pid) or sw[pid][0][0] > p['open']]
        if not miss:
            break
        for k, v in window_swaps(miss).items():
            seen = {(x[0], x[1]) for x in sw.get(k, [])}
            sw[k] = sorted(sw.get(k, []) + [x for x in v if (x[0], x[1]) not in seen])
        reach = step * PAD
    return sw


def swaps(pids, frm, to):
    try:
        return node_swaps(pids, frm, to)
    except Exception:
        pass                                    # no tunnel, or the node refused: hypersync, slower but always there
    return hyper_swaps(pids, frm, to)


def hyper_swaps(pids, frm, to):
    """pid -> sorted [(block, log_index, amount0, amount1, sqrtP after, active L, lpFee pips)] - one hypersync pass."""
    s = collections.defaultdict(list)
    body = dict(from_block=frm, to_block=to, logs=[dict(address=[F.PM], topics=[[SWAP4], pids])],
                field_selection=dict(log=['block_number', 'log_index', 'topic1', 'data']))
    for r in F._pages(body, F._key()):
        for d in r['data']:
            for l in d.get('logs') or []:
                w = l['data'][2:]
                a0, a1 = _s(w[0:64]), _s(w[64:128])
                s[l['topic1']].append((l['block_number'], l['log_index'], a0, a1, int(w[128:192], 16) / 2 ** 96,
                                       int(w[192:256], 16), int(w[320:384], 16)))
    for v in s.values():
        v.sort()
    return s


def replay(pos, key, sw, dec, eusd, d, own_l, size=None, marks=None):
    """One position copied `d` blocks late. pos: dict(legs=[(pid, tl, tu, L)], open, close, invested). Returns USD P&L
    before gas, or None when a pool has no price at entry.
    marks (research/lpml, sorted blocks): a 4th value, the copy's exit value at each mark - principal at that block's
    price (the token side less the pool fee, as the exit below) plus the fees earned by then; a mark past the close holds
    the close's value. Same engine, no second copy of the P&L logic."""
    total_in = total_out = fees = 0.0
    mv = [0.0] * len(marks) if marks else None
    legs_ = pos['legs']
    b0, b1 = pos['open'] + d, pos['close'] + d
    # size: the wallet's own liquidity (validation) or the same USD at our entry price, split across legs as the wallet did
    plan = []
    for pid, tl, tu, L in legs_:
        c0, c1 = key[pid][:2]
        v = sw.get(pid, [])
        i = bisect.bisect_left(v, (b0, 0))
        if not i:
            return None
        sp = v[i - 1][4]
        a0, a1 = _amounts(L, sp, tl, tu)
        plan.append([pid, tl, tu, L, sp, a0, a1, c0, c1])
    usd = lambda c0, c1, x0, x1, sp: _usd(c0, c1, x0, x1, sp, dec, eusd, b0)
    base = sum(usd(p[7], p[8], p[5], p[6], p[4]) or 0 for p in plan)
    if not base or (not own_l and base < _min_leader()):          # the leader's position at our entry, as the desk sees it
        return None
    # our copy's USD: the leader's own size (the old default), or OURS - a big leader in a thin pool shares its fees with
    # an equal-size copy, which halved every 'copy' of the first-LP wallets; a $25 copy barely dilutes it
    scale = 1.0 if own_l else (size or pos['invested'] or base) / base
    for p in plan:
        pid, tl, tu, L, sp, a0, a1, c0, c1 = p
        L = L * scale
        total_in += (usd(c0, c1, a0, a1, sp) or 0) * scale
        sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
        v = sw[pid]
        cl = pos.get('close_li') if d == 0 else None                  # the leader's own exit: only the swaps before its burn
        end = (b1, cl if cl is not None else 1 << 30)
        i, j = bisect.bisect_left(v, (b0, 0)), bisect.bisect_right(v, end)
        prev, pact = v[i - 1][4], v[i - 1][5]
        q0 = c0 in (W.USDG, W.WETH, NATIVE)
        f0, lfee, m = fees, v[i - 1][6], 0

        def mark_to(upto):
            """Record this leg's value at every mark before block `upto` (exclusive) at the current state."""
            nonlocal m
            while m < len(marks) and marks[m] < upto:
                e0_, e1_ = _amounts(L, prev, tl, tu)
                ct = 1 - lfee / 1e6
                mv[m] += (fees - f0) + (usd(c0, c1, e0_ if q0 else e0_ * ct, e1_ if not q0 else e1_ * ct, prev) or 0)
                m += 1
        for blk, li, s0, s1, spn, act, fee in v[i:j]:
            if mv is not None:
                mark_to(blk)
            lfee = fee
            lo_, hi_ = max(min(prev, spn), sa), min(max(prev, spn), sb)
            if hi_ > lo_ and fee < 1e6:
                # THE FEE THIS POSITION EARNED, EXACTLY (2026-10-01): its liquidity times the sqrt-price distance the swap
                # travelled INSIDE its range is the input that went through it (token0 in when the price falls: L(1/lo - 1/hi);
                # token1 in when it rises: L(hi - lo)), and the fee is charged on top of that input. It replaced "the whole swap
                # fee x L / the active liquidity at the swap's ends", which gave a narrow bin at a swap's start or end the fee of
                # the whole swap: a 38-bin Delta ladder modelled $385 of fees on $496 in 11 h, its exit paid $0 over principal
                # THE LPs' PART of the swap fee (2026-09-30): the log's fee is the whole swap fee, the protocol's cut
                # included (v4 ProtocolFeeLibrary: swapFee = pf + lpFee - pf*lpFee/1e6); robinhood's pools take pf = 1000
                # pips each way, which the replay booked as LP fees (5% of the fees of a 2% pool)
                lpf = max(0, fee - PF.get(pid, (0, 0))[0 if s0 > 0 else 1])
                r_ = lpf / (1e6 - fee)
                x0, x1 = (L * (1 / lo_ - 1 / hi_) * r_, 0.0) if spn < prev else (0.0, L * (hi_ - lo_) * r_)
                # a copy (not the leader's own liquidity) deepens the pool: the same swap travels less, our part is L / (pool + L)
                dil = 1.0 if own_l else (lambda a_: a_ / (a_ + L) if a_ + L else 0.0)(max(pact, act))
                fees += (usd(c0, c1, x0, x1, spn) or 0) * dil
            prev, pact = spn, act
        if mv is not None:
            mark_to(b1 + 1)                                           # marks up to the close: the state after its last swap
        k = bisect.bisect_right(v, end)
        spx = v[k - 1][4] if k else sp
        e0, e1 = _amounts(L, spx, tl, tu)
        cut = 1 - (v[k - 1][6] / 1e6 if k else 0.01)                # selling the token side costs the pool fee
        leg_out = usd(c0, c1, e0 if q0 else e0 * cut, e1 if not q0 else e1 * cut, spx) or 0
        total_out += leg_out
        if mv is not None:
            for m_ in range(m, len(marks)):                           # past the close: what the close paid
                mv[m_] += (fees - f0) + leg_out
    # an ETH-quoted copy converts USDG -> ETH in and ETH -> USDG out (copydesk/treasury): both conversions' cost
    if any(NATIVE in (p[7], p[8]) for p in plan):
        total_out -= CONV * total_in
        if mv is not None:
            mv = [x - CONV * total_in for x in mv]
    if mv is not None:
        return total_out + fees - total_in, fees, total_in, mv
    return total_out + fees - total_in, fees, total_in


NATIVE = '0x' + '0' * 40
PF = {}              # pool id -> (protocol fee zeroForOne, oneForZero) in pips, from StateView slot0 (protocol_fees)


def protocol_fees(pids):
    """Each pool's protocol fee per direction (StateView getSlot0: the protocolFee word, 12 bits each way), cached."""
    from copydesk.lpfollow import _batch, _call, _w, SV
    want = [p for p in pids if p not in PF]
    for i in range(0, len(want), 100):
        for p, r in zip(want[i:i + 100], _batch([_call(SV, '0xc815641c' + p[2:]) for p in want[i:i + 100]])):
            w = _w(r, 2)
            PF[p] = (w & 0xfff, w >> 12)
    return PF


CONV = 0.0012        # USDG -> ETH -> USDG on the 0.046% ETH/USDG pool: two fees + slippage (treasury.jsonl measures it live)


def _token(k):
    """The non-quote side of a pool key (c0, c1): what the copy desk's token rules act on."""
    c0, c1 = k[0], k[1]
    return c1 if c0 in (W.USDG, W.WETH, '0x' + '0' * 40) else c0


def _amounts(L, sp, tl, tu):
    sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
    if sp <= sa:
        return L * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, L * (sb - sa)
    return L * (sb - sp) / (sp * sb), L * (sp - sa)


def _usd(c0, c1, x0, x1, sp, dec, eusd, blk):
    d0, d1 = dec.get(c0, 18), dec.get(c1, 18)
    p = sp * sp * 10 ** d0 / 10 ** d1
    h0, h1 = x0 / 10 ** d0, x1 / 10 ** d1
    if c0 == W.USDG:
        return h0 + h1 / p
    if c1 == W.USDG:
        return h1 + h0 * p
    if c0 in (W.WETH, '0x' + '0' * 40):
        return (h0 + h1 / p) * eusd(blk)
    if c1 in (W.WETH, '0x' + '0' * 40):
        return (h1 + h0 * p) * eusd(blk)
    return None


def _native():
    """[lp.follow] native: the replay scores ETH-pool copies only while the desk takes them."""
    if 'on' not in _NAT:
        try:
            from core import chain as cm
            from copydesk.lpfollow import cfg
            _NAT['on'] = bool(cfg(cm.load('robinhood')).get('native', True))
        except Exception:
            _NAT['on'] = False
    return _NAT['on']


def _min_leader():
    """[lp.follow] min_leader_usd: the desk skips a leader position worth less at copy time, so the replay does too
    (2026-09-30: main 0x9949 scored +$155 of 7 days on 30 copies under $20 the desk never took; -$25 on the 6 it would)."""
    if 'min' not in _NAT:
        try:
            from core import chain as cm
            from copydesk.lpfollow import cfg
            _NAT['min'] = float(cfg(cm.load('robinhood')).get('min_leader_usd', 0) or 0)
        except Exception:
            _NAT['min'] = 20.0
    return _NAT['min']


_NAT = {}
_PREP = {}                                      # (addr, days, dmax) -> everything run() reads: grid() calls run() 2-3 times


_HIST = {}


def _hist():
    """The local LP history store (copydesk/lphist), one reader a process; None when there is no store."""
    if 'r' not in _HIST:
        try:
            from core import chain as cm
            from copydesk import lphist
            c = cm.load('robinhood')
            _HIST['r'] = lphist.Reader(c) if os.path.exists(lphist.path(c)) else None
            _HIST['c'] = c
        except Exception:
            _HIST['r'] = None
    return _HIST['r']


def run_store(addr, days, delays, size=None, since=None):
    """THE REPLAY FROM THE LOCAL STORE (owner 2026-09-30: history does not change): the wallet's positions, pool keys,
    swaps, decimals, block times, the ETH price and exit payouts all come from copydesk/lphist - no wallet fetch, no
    receipts for legs, no swaps from the node. None when the store does not cover the window (run() then reads the node)."""
    R = _hist()
    if R is None:
        return None
    t = time.time()
    first, last = R.span()
    frm = last - int(days * 864_000)
    if first > frm:
        return None
    dmax = max([d for d in delays if d != 'live'] + [max(lag_table().values()) if 'live' in delays else 0])
    k_ = ('store', addr, days, dmax, since)
    if k_ in _PREP:
        D, done, pos, key, sw = _PREP[k_]
        return _replay_all(addr, days, delays, size, D, done, pos, key, sw, t)
    ps = [p for p in R.positions(addr, frm) if p['close'] <= last]
    if since:
        ps = [p for p in ps if R.tsof(p['close']) > since]
    key = R.keys(sorted({x[0] for p in ps for x in p['legs']}))
    PF.update(R.pf())
    from copydesk.lpfollow import uncopyable
    from copydesk import lphist
    ps = [p for p in ps if all(x[0] in key for x in p['legs']) and not any(uncopyable(*key[x[0]], x[0], W.USDG, native=_native()) for x in p['legs'])]
    lphist.payouts(_HIST['c'], [tid for p in ps for tid in p['tokenIds']])        # the digester: once per closed position
    paid = {}
    if ps:
        con = R.con
        tids = [tid for p in ps for tid in p['tokenIds']]
        for i in range(0, len(tids), 900):
            q = tids[i:i + 900]
            paid.update({r[0]: (r[1], r[2]) for r in con.execute('SELECT tid, paid0, paid1 FROM positions WHERE tid IN (%s)' % ','.join('?' * len(q)), q)})
    pos = []
    for p in ps:
        pp = [paid.get(tid) for tid in p['tokenIds']]
        one_pool = len({x[0] for x in p['legs']}) == 1
        pd = (sum(x[0] for x in pp), sum(x[1] for x in pp)) if one_pool and not p['topped'] and all(x and x[0] is not None for x in pp) else None
        q = dict(legs=p['legs'], open=p['open'], close=p['close'], invested=None, real=None, paid=pd,
                 t0=R.tsof(p['open']), t1=R.tsof(p['close']))
        u = union(p['legs']) if p.get('managed') else None
        if u:
            q.update(legs=u, legs0=p['legs'])
        pos.append(q)
    sw = R.swaps([(pid, p['open'] - PAD, p['close'] + dmax + 5) for p in pos for pid, *_ in p['legs']])
    # the trust replay values the leader's exit at the swaps BEFORE its burn (lphist.exit_li) where its pool traded in that block
    busy = lambda pid, b: (lambda v, k: k < len(v) and v[k][0] == b)(sw.get(pid, []), bisect.bisect_left(sw.get(pid, []), (b, 0)))
    want = {i: (pp['tokenIds'], q['close']) for i, (pp, q) in enumerate(zip(ps, pos))
            if any(busy(x[0], q['close']) for x in q.get('legs0') or q['legs'])}
    for i, li in lphist.exit_li(_HIST['c'], want).items():
        pos[i]['close_li'] = li
    D = dict(dec=R.dec, eusd=R.eusd, tsof=R.tsof, frm=frm)
    _PREP.clear()
    _PREP[k_] = (D, ps, pos, key, sw)
    return _replay_all(addr, days, delays, size, D, ps, pos, key, sw, t)


def run(addr, days, delays, size=None, since=None):
    """since (unix ts): replay only the positions that CLOSED after it - the nightly's incremental run; the rest of the
    wallet's copies are already stored (lpdb.copy_trades). From the local history store when it covers the window."""
    R = run_store(addr, days, delays, size, since)
    if R is not None:
        return R
    t = time.time()
    dmax = max([d for d in delays if d != 'live'] + [max(lag_table().values()) if 'live' in delays else 0])
    k_ = (addr, days, dmax, since)
    if k_ in _PREP:
        D, done, pos, key, sw = _PREP[k_]
        return _replay_all(addr, days, delays, size, D, done, pos, key, sw, t)
    D = W.fetch(addr, days)
    rows, _, _ = W.book(D)
    done = [r for r in rows if r['closed'] and 'realized_usd' in r and not r.get('native') and r['open_blk'] >= D['frm']]
    # NATIVE-ETH POSITIONS (owner 2026-09-30): replayed like any other, the leader's own P&L unknown (None) - the copy
    # is priced from the pool's swaps alone, and replay trust comes from the wallet's USDG positions
    done += [dict(n, closed=True, invested_usd=None, realized_usd=None) for n in D.get('native', []) if n['open_blk'] >= D['frm']]
    if since:
        done = [r for r in done if D['tsof'](r['close_blk']) > since]      # before any node work: receipts, swaps
    lg = legs(D, done)
    pids = sorted({x[0] for x in lg.values()})
    key = keys(pids)
    pos = []
    for r in done:
        ls = [lg[t] for t in r['tokenIds'] if t in lg]
        if len(ls) != len(r['tokenIds']) or any(x[0] not in key for x in ls):
            continue
        # ONLY WHAT THE DESK WOULD COPY (lpfollow.uncopyable, the same rule): a position in an ETH pair, a native-ETH or a
        # hooked pool is not replayed - the desk skips it, so it must not score
        from copydesk.lpfollow import uncopyable
        if any(uncopyable(*key[x[0]], x[0], W.USDG, native=_native()) for x in ls):
            continue
        pos.append(dict(legs=ls, open=r['open_blk'], close=r['close_blk'], invested=r['invested_usd'], real=r['realized_usd'],
                        t0=D['tsof'](r['open_blk']), t1=D['tsof'](r['close_blk']), paid=_paid(D, r, ls, key)))
    # our node only: the old fallback to a full hypersync scan hung replay-all for 26 minutes on rate limits (2026-09-29);
    # a failure now fails this replay loudly and replay-all retries it at the end
    sw = pos_swaps(pos, dmax)
    protocol_fees(sorted({x[0] for p in pos for x in p['legs']}))
    _PREP.clear()
    _PREP[k_] = (D, done, pos, key, sw)
    return _replay_all(addr, days, delays, size, D, done, pos, key, sw, t)


# THE LIVE DESK'S COPY LAG, per kind of copy, in blocks (100 ms): measured on our own copies (lpfind.measure_lag, nightly,
# lpdb kv 'copy_lag'); these defaults are the 2026-09-30 measurement on 107 copies from the AWS desk: a lone bid is one
# mint tx (median 2 blocks), a bid in a burst of the wallet's mints waits its turn (13), a range swaps then mints (58).
# The replay used a flat 33 blocks before - measured on 09-28 when the desk polled from home, ~p90 of the lag today.
LAG_DEFAULT = dict(bid=2, burst=13, range=58)
BURST = 20                                     # blocks: a mint this close after another of the wallet's is in a burst
_LAG = {}


def lag_table():
    if not _LAG:
        try:
            from copydesk import lpdb
            con = lpdb.connect()
            r = con.execute("SELECT v FROM kv WHERE k = 'copy_lag'").fetchone()
            con.close()
            _LAG.update(dict(LAG_DEFAULT, **(json.loads(r[0]) if r else {})))
        except Exception:
            _LAG.update(LAG_DEFAULT)
    return _LAG


def copy_kind(p, key, sw, opens):
    """'bid' (every leg holds only the quote at the leader's open: one mint tx), 'range' (a swap first), or 'burst' (a bid
    that follows another of the wallet's mints within BURST blocks: the desk copies one at a time)."""
    for pid, tl, tu, L in p['legs']:
        v = sw.get(pid, [])
        i = bisect.bisect_left(v, (p['open'], 0))
        if not i:
            return 'range'
        c0, c1 = key[pid][:2]
        a0, a1 = _amounts(L, v[i - 1][4], tl, tu)
        qc0 = c0 in (W.USDG, W.WETH, NATIVE)
        if (a1 if qc0 else a0) > 0:
            return 'range'
    j = bisect.bisect_left(opens, p['open'])
    return 'burst' if j and p['open'] - opens[j - 1] <= BURST else 'bid'


def _paid(D, r, ls, key):
    """What the position's exit PAID the wallet, raw per currency of its pool: the net token flows of its burn txs
    (lpwallet's per-tx flows). None when that cannot be read cleanly: not every NFT burned (a decrease to zero), a burn
    tx that also mints (a re-range: the tx's flows are not this position's alone), several pools, or native ETH (no
    Transfer log). THE VALIDATION TARGET for trust (2026-09-30): the pool's own payout, principal + fees, not the
    wallet's later trading - which the old target (its realized P&L) mixed in."""
    pids = {x[0] for x in ls}
    if len(pids) != 1 or any(t not in D['burn'] for t in r['tokenIds']):
        return None
    c0, c1 = key[next(iter(pids))][:2]
    if NATIVE in (c0, c1):
        return None
    # every tx that paid this position out: its burns AND the decreases / collects before them (many wallets decrease
    # first and burn the empty NFT later: the burn alone read ~$0 back, 0x4b2d at -$51,302)
    ends = {D['burn'][t] for t in r['tokenIds']} | {h for t in r['tokenIds'] for h in (D.get('touch') or {}).get(t, [])}
    if ends & set(D['mint'].values()):
        return None
    per = [(D['flow'][h].get(c0, 0), D['flow'][h].get(c1, 0)) for h in ends]
    if any(a < 0 or b < 0 for a, b in per):
        return None                                                  # liquidity added later: the replay models the mint's size only
    return sum(a for a, _ in per), sum(b for _, b in per)


def _paid_usd(p, key, sw, dec, eusd):
    """The payout (_paid) in dollars, valued exactly as replay() values its own exit: the pool's last price at the close,
    the token side less the pool fee."""
    if not p.get('paid'):
        return None
    pid = p['legs'][0][0]
    c0, c1 = key[pid][:2]
    v = sw.get(pid, [])
    k = bisect.bisect_right(v, (p['close'], 1 << 30))
    if not k:
        return None
    spx, cut = v[k - 1][4], 1 - v[k - 1][6] / 1e6
    x0, x1 = p['paid']
    q0 = c0 in (W.USDG, W.WETH, NATIVE)
    return _usd(c0, c1, x0 if q0 else x0 * cut, x1 if not q0 else x1 * cut, spx, dec, eusd, p['close'])


def _sane(paid, model_in):
    """A payout over 10x what went in, or under nothing, is a price read at a drained pool's extreme tick, not a result."""
    return paid is not None and model_in and 0 <= paid <= 10 * model_in


def value_path(p, key, sw, dec, eusd, d, size, detail=False):
    """Our copy's value along its life, swap by swap: [(block, $)] from entry (d blocks after the leader) to the leader's
    exit (+ d), and the $ in. Principal at each swap's price (the token side less the pool fee, as replay() values its
    exit) plus the LPs' part of the fees earned so far. None if unpriced. What stop and exit rules are tested on.
    detail: points are (block, $, fees $ so far, depth, token price in the quote, in range) - depth = how far the first
    leg's token has fallen past a range it now holds entirely (0 while any quote is left), in range = the price sits
    inside any leg's range; the result gains our share of the pool's active liquidity at entry and the leader's own $."""
    b0, b1 = p['open'] + d, p['close'] + d
    plan = []
    for pid, tl, tu, L in p['legs']:
        v = sw.get(pid, [])
        i = bisect.bisect_left(v, (b0, 0))
        if not i:
            return None
        sp = v[i - 1][4]
        a0, a1 = _amounts(L, sp, tl, tu)
        plan.append([pid, tl, tu, L, sp, a0, a1] + list(key[pid][:2]))
    usd = lambda c0, c1, x0, x1, sp_, b: _usd(c0, c1, x0, x1, sp_, dec, eusd, b)
    base = sum(usd(q[7], q[8], q[5], q[6], q[4], b0) or 0 for q in plan)
    if not base or base < _min_leader():
        return None
    scale = size / base
    ev, tin = [], 0.0
    for n, (pid, tl, tu, L, sp, a0, a1, c0, c1) in enumerate(plan):
        tin += (usd(c0, c1, a0, a1, sp, b0) or 0) * scale
        v = sw[pid]
        i, j = bisect.bisect_left(v, (b0, 0)), bisect.bisect_right(v, (b1, 1 << 30))
        ev += [(blk, li, n, s0, s1, spn, act, fee) for blk, li, s0, s1, spn, act, fee in v[i:j]]
    ev.sort()
    cur = [q[4] for q in plan]
    pact = {n: sw[q[0]][bisect.bisect_left(sw[q[0]], (b0, 0)) - 1][5] for n, q in enumerate(plan)}
    inr = lambda: any(1.0001 ** (q[1] / 2) <= cur[m] < 1.0001 ** (q[2] / 2) for m, q in enumerate(plan))
    fees, out = 0.0, [(b0, tin, 0.0, 0.0, None, inr()) if detail else (b0, tin)]
    q0_ = plan[0][7] in (W.USDG, W.WETH, NATIVE)
    sa0, sb0 = 1.0001 ** (plan[0][1] / 2), 1.0001 ** (plan[0][2] / 2)

    def state(sp_):
        """(depth past a filled range, token price in the quote) for the first leg at sqrtP sp_."""
        if q0_:                                            # token = currency1: token price 1 / p; filled when sp >= sb
            return (1 - (sb0 / sp_) ** 2 if sp_ >= sb0 else 0.0), 1 / (sp_ * sp_)
        return (1 - (sp_ / sa0) ** 2 if sp_ <= sa0 else 0.0), sp_ * sp_
    for blk, li, n, s0, s1, spn, act, fee in ev:
        pid, tl, tu, L, sp, a0, a1, c0, c1 = plan[n]
        Ls = L * scale
        sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
        if sa <= cur[n] < sb or sa <= spn < sb:
            den = max(pact[n], act) + Ls
            lpf = max(0, fee - PF.get(pid, (0, 0))[0 if s0 > 0 else 1])
            x0, x1 = (-s0 if s0 < 0 else 0), (-s1 if s1 < 0 else 0)
            fees += (usd(c0, c1, x0 * lpf / 1e6, x1 * lpf / 1e6, spn, blk) or 0) * (min(1.0, Ls / den) if den else 0.0)
        cur[n], pact[n] = spn, act
        val = fees
        for m, q in enumerate(plan):
            e0, e1 = _amounts(q[3] * scale, cur[m], q[1], q[2])
            cut = 1 - fee / 1e6
            q0 = q[7] in (W.USDG, W.WETH, NATIVE)
            val += usd(q[7], q[8], e0 if q0 else e0 * cut, e1 if not q0 else e1 * cut, cur[m], blk) or 0
        if detail:
            dp, tp = state(cur[0])
            out.append((blk, val, fees, dp, tp, inr()))
        else:
            out.append((blk, val))
    if detail:
        # OUR OWN ENTRY SWAP (owner 2026-09-30 night: the replay entered every copy free at the leader's price; a full-range RAIL
        # copy in a 10%-fee pool the leader nearly was opened 7% under water): a leg holding the token at entry is bought
        # first - the pool fee on it plus the price impact of that buy on the pool's active liquidity then (one tick range,
        # a lower bound past it), charged on every point of the path
        ecost = entry_cost(plan, scale, sw, b0, usd)
        out = [(x[0], x[1] - ecost) + tuple(x[2:]) for x in out]
        v0 = sw[plan[0][0]]
        act0 = v0[bisect.bisect_left(v0, (b0, 0)) - 1][5]
        return out, tin, (plan[0][3] * scale) / (act0 + plan[0][3] * scale) if act0 + plan[0][3] * scale else 1.0, base, ecost
    return out, tin


def entry_cost(plan, scale, sw, b0, usd):
    """OUR OWN ENTRY SWAP in $: a leg holding the token at entry is bought first - the pool fee on it plus the price impact of
    that buy on the pool's active liquidity then (one tick range, a lower bound past it). plan: [pid, tl, tu, L, sqrtP at
    entry, amount0, amount1, c0, c1] per leg at the leader's L; scale: our size over the leader's; usd(c0, c1, x0, x1, sp, b)."""
    ecost = 0.0
    for pid, tl, tu, L, sp, a0, a1, c0, c1 in plan:
        q0 = c0 in (W.USDG, W.WETH, NATIVE)
        tok = (a1 if q0 else a0) * scale
        if not tok:
            continue                                                   # a bid: holds only the quote, swaps nothing
        v = sw[pid]
        prev = v[bisect.bisect_left(v, (b0, 0)) - 1]
        act, fee = prev[5], prev[6] / 1e6
        tok_usd = usd(c0, c1, 0.0 if q0 else tok, tok if q0 else 0.0, sp, b0) or 0.0
        imp = 1.0
        if act > 0:
            if q0:                                                     # quote = token0 in, token1 out
                s2 = sp - tok / act
                imp = (act * (1 / s2 - 1 / sp)) / (tok / (sp * sp)) - 1 if s2 > 0 else 1.0
            else:                                                      # quote = token1 in, token0 out
                inv = 1 / sp - tok / act
                imp = (act * (1 / inv - sp)) / (tok * sp * sp) - 1 if inv > 0 else 1.0
        ecost += tok_usd * (fee + min(max(imp, 0.0), 1.0))
    return ecost


def union(legs):
    """A MANAGED LADDER IS COPIED AS ONE RANGE (owner 2026-10-01, research/laddershape: 117 Delta LPs, 2,273 ladders over 7 days at
    $67 a copy, the live lag and real gas - one position over the ladder's whole range beat every bin on 82% of ladders, -$14.88
    vs -$657.07; 0x620a +$194.94 vs +$144.65, 6 of 8 days, worst day -$4.63 vs -$1.63). The live desk mints that one range
    (lpfollow.on_ladder). [(pid, min tl, max tu, sum L)] for a one-pool ladder of 2+ bins, else None."""
    if len(legs) < 2 or len({x[0] for x in legs}) != 1:
        return None
    return [(legs[0][0], min(x[1] for x in legs), max(x[2] for x in legs), sum(x[3] for x in legs))]


def _replay_all(addr, days, delays, size, D, done, pos, key, sw, t):
    out = dict(address=addr, days=days, positions=len(done), replayed=len(pos), secs=None, delays={})
    opens = sorted(p['open'] for p in pos)
    for d in delays:
        res = []
        for p in pos:
            if d == 0 and p.get('legs0'):
                p = dict(p, legs=p['legs0'])                         # trust replays the leader's own ladder, bin by bin
            # d == 'live': each position at the lag the live desk really gets for its kind of copy
            dd = lag_table()[copy_kind(p, key, sw, opens)] if d == 'live' else d
            x = replay(p, key, sw, D['dec'], D['eusd'], dd, own_l=(d == 0), size=size)
            if x:
                res.append((p, x))
        pnl = [x[0] for p, x in res]
        kn = [(p, x) for p, x in res if p['real'] is not None]       # validation: only positions whose own P&L is known
        out['delays'][d] = dict(n=len(res), pnl=sum(pnl), fees=sum(x[1] for p, x in res), invested=sum(x[2] for p, x in res),
                                win=100 * sum(1 for v in pnl if v > 0) / len(pnl) if pnl else 0,
                                median_pct=S.median(100 * x[0] / x[2] for p, x in res if x[2]) if res else 0,
                                actual=sum(p['real'] for p, x in kn), pnl_known=sum(x[0] for p, x in kn),
                                pairs=[(p['real'], x[0], p['invested']) for p, x in kn],
                                trades=[dict(t0=p['t0'], t1=p['t1'], invested=p['invested'], model=x[0], real=p['real'], fees=x[1], model_in=x[2],
                                             paid=(lambda v: v if _sane(v, x[2]) else None)(_paid_usd(p, key, sw, D['dec'], D['eusd'])) if d == 0 else None,
                                             pool=p['legs'][0][0], token=_token(key[p['legs'][0][0]])) for p, x in res])
    out['secs'] = round(time.time() - t, 1)
    return out


def main(a):
    days = float(a[a.index('--days') + 1]) if '--days' in a else 7
    delays = [int(x) for x in a[a.index('--delays') + 1].split(',')] if '--delays' in a else [0, 70, 300]
    R = run(a[0].lower(), days, delays)
    if '--json' in a:
        print(json.dumps(R, default=str))
        return
    print('%s  %g days: %d closed positions, %d replayed  (%.0f s)' % (R['address'], days, R['positions'], R['replayed'], R['secs']))
    for d, x in R['delays'].items():
        print('  %s  n %4d  P&L before gas $%+9.2f (fees $%.2f)  win %3.0f%%  median %+.2f%%   the wallet\'s real P&L on the same positions $%+.2f' % (
            'validation d=0' if d == 0 else 'copy +%4.1f s ' % (d / 10), x['n'], x['pnl'], x['fees'], x['win'], x['median_pct'], x['actual']))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
