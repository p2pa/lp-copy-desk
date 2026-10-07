#!/usr/bin/env python3
"""ticks.py - EXACT concentrated-liquidity swap maths, so the local model matches the chain.

  ticks.py selftest      pure asserts, no network

WHY. The first search loop priced pools as constant product using virtual reserves L/sqrtP and L*sqrtP.
That is exact only INSIDE the current tick. Measured 2026-09-15 against our own deployed contract:
    cycle 3807  LOCAL +$21.84  CHAIN -$100.03   (the route returns 0.005% of its input)
    cycle 3982  LOCAL  +$0.14  CHAIN   -$9.71   (the route returns ONE wei)
Liquidity is not flat: it holds only until the next initialized tick, and a size that crosses out of
range falls off a cliff the flat model cannot see. Carrying the tick bitmap and liquidityNet, and running
the real swap loop, is what removes the need to ask the chain at all - which is the whole latency budget.

This is Uniswap's own maths (TickMath + SwapMath), integer-exact, including the rounding directions.
Rounding the wrong way is not cosmetic here: it is the difference between a route that clears its floor
and one that reverts.
"""
import sys
from functools import lru_cache

Q96, Q128 = 2 ** 96, 2 ** 128
MIN_TICK, MAX_TICK = -887272, 887272
MIN_SQRT, MAX_SQRT = 4295128739, 1461446703485210103287273052203988822378723970342

_M = [0xfffcb933bd6fad37aa2d162d1a594001, 0xfff97272373d413259a46990580e213a,
      0xfff2e50f5f656932ef12357cf3c7fdcc, 0xffe5caca7e10e4e61c3624eaa0941cd0,
      0xffcb9843d60f6159c9db58835c926644, 0xff973b41fa98c081472e6896dfb254c0,
      0xff2ea16466c96a3843ec78b326b52861, 0xfe5dee046a99a2a811c461f1969c3053,
      0xfcbe86c7900a88aedcffc83b479aa3a4, 0xf987a7253ac413176f2b074cf7815e54,
      0xf3392b0822b70005940c7a398e4b70f3, 0xe7159475a2c29b7443b29c7fa6e889d9,
      0xd097f3bdfd2022b8845ad8f792aa5825, 0xa9f746462d870fdf8a65dc1f90e061e5,
      0x70d869a156d2a1b890bb3df62baf32f7, 0x31be135f97d08fd981231505542fcfa6,
      0x9aa508b5b7a84e1c677de54f3e99bc9,  0x5d6af8dedb81196699c329225ee604,
      0x2216e584f5fa1ea926041bedfe98,     0x48a170391f7dc42444e8fa2]

# MEMOISED. Pure int -> int, and the hot path calls it 57,124 times per 400 sizings (1.4 per swap step)
# across a few thousand distinct ticks: profiled 2026-09-15 it was the single largest term in
# best_size_exact at 22% of its total time. The cache changes no value - the selftest below proves the
# cached and uncached results are identical over the whole tick range.
@lru_cache(maxsize=1 << 16)
def sqrt_at_tick(tick):
    """TickMath.getSqrtRatioAtTick, integer-exact."""
    a = abs(tick)
    if a > MAX_TICK: raise ValueError('tick out of range %d' % tick)
    r = _M[0] if a & 1 else 0x100000000000000000000000000000000
    for i in range(1, 20):
        if a & (1 << i): r = (r * _M[i]) >> 128
    if tick > 0: r = ((1 << 256) - 1) // r
    return (r >> 32) + (1 if r % (1 << 32) else 0)

def _a0(sa, sb, L, up):
    """amount0 between two prices. up=True rounds UP (what the pool takes from you)."""
    if sa > sb: sa, sb = sb, sa
    if sa == 0: raise ValueError('zero price')
    num = (L << 96) * (sb - sa)
    return (-(-num // sb) + (sa - 1)) // sa if up else (num // sb) // sa

def _a1(sa, sb, L, up):
    if sa > sb: sa, sb = sb, sa
    n = L * (sb - sa)
    return -(-n // Q96) if up else n // Q96

def _next_from_a0(sp, L, amt, add):
    """New sqrt price after adding/removing `amt` of token0."""
    if amt == 0: return sp
    n = L << 96
    if add:
        p = amt * sp
        if p // amt == sp and n + p > n: return -(-(n * sp) // (n + p))
        return -(-n // ((n // sp) + amt))
    p = amt * sp
    return -(-(n * sp) // (n - p))

def _next_from_a1(sp, L, amt, add):
    if add: return sp + (amt << 96) // L
    q = -(-(amt << 96) // L)
    return sp - q

def next_sqrt_from_input(sp, L, amt_in, zero_for_one):
    if sp <= 0 or L <= 0: raise ValueError('bad pool state')
    return _next_from_a0(sp, L, amt_in, True) if zero_for_one else _next_from_a1(sp, L, amt_in, True)

def compute_swap_step(sp, target, L, remaining, fee):
    """One step of SwapMath, EXACT-INPUT only (remaining > 0).

    Returns (sqrt_next, amount_in_used, amount_out, fee_amount). `fee` is hundredths of a bip.
    """
    zfo = sp >= target
    amt_less_fee = (remaining * (10 ** 6 - fee)) // 10 ** 6
    amount_in = _a0(target, sp, L, True) if zfo else _a1(sp, target, L, True)
    if amt_less_fee >= amount_in:
        nxt = target                                   # we reach the tick boundary
    else:
        nxt = next_sqrt_from_input(sp, L, amt_less_fee, zfo)
        amount_in = amt_less_fee
    maxed = nxt == target
    if zfo:
        if not maxed: amount_in = _a0(nxt, sp, L, True)
        amount_out = _a1(nxt, sp, L, False)
    else:
        if not maxed: amount_in = _a1(sp, nxt, L, True)
        amount_out = _a0(sp, nxt, L, False)
    # Uniswap's exact branch: if we did NOT reach the target, the fee is whatever is left over; only when
    # we DID reach it is the fee computed from the input. The first version added an extra condition here
    # and it biased every leg high by ~0.05%.
    fee_amt = (remaining - amount_in) if not maxed else -(-(amount_in * fee) // (10 ** 6 - fee))
    return nxt, amount_in, amount_out, fee_amt

def next_initialized(bitmaps, tick, spacing, lte):
    """Next initialized tick at or beyond `tick`, from cached bitmap WORDS {wordPos: uint256}.

    Returns (tick, initialized). If the word is not cached, returns the word edge uninitialised - the
    caller must then decide to refetch rather than pretend liquidity continues.
    """
    comp = tick // spacing
    if lte:
        wp, bp = comp >> 8, comp & 0xff
        word = bitmaps.get(wp)
        if word is None: return ((comp - bp) * spacing, None)
        mask = (1 << bp) - 1 + (1 << bp)
        masked = word & mask
        if masked: return ((comp - (bp - masked.bit_length() + 1)) * spacing, True)
        return ((comp - bp) * spacing, False)
    comp += 1
    wp, bp = comp >> 8, comp & 0xff
    word = bitmaps.get(wp)
    if word is None: return ((comp + (255 - bp)) * spacing, None)
    mask = ~((1 << bp) - 1) & ((1 << 256) - 1)
    masked = word & mask
    if masked:
        lsb = (masked & -masked).bit_length() - 1
        return ((comp + (lsb - bp)) * spacing, True)
    return ((comp + (255 - bp)) * spacing, False)

def swap_fee(lp_fee, protocol_fee):
    """v4 charges the protocol fee FIRST and the LP fee on the remainder, so the swapper pays
    pf + lp - pf*lp/1e6. Ignoring it makes every v4 leg read slightly cheap. v3 takes its protocol fee
    out of the LP's share instead, so the swapper is unaffected there - pass protocol_fee=0."""
    if not protocol_fee: return lp_fee
    return protocol_fee + lp_fee - (protocol_fee * lp_fee) // 10 ** 6

def swap(sp, L, tick, spacing, fee, amount_in, zero_for_one, bitmaps, liq_net):
    """The real swap loop. Returns (amount_out, sqrt_after, liquidity_after, tick_after) or None if it
    ran past the cached tick window - in which case we do NOT have the data and must not guess."""
    if amount_in <= 0 or L <= 0: return None
    limit = MIN_SQRT + 1 if zero_for_one else MAX_SQRT - 1
    remaining, out = amount_in, 0
    guard = 0
    while remaining > 0 and sp != limit:
        guard += 1
        if guard > 256: return None                    # a pathological route is not a priced route
        nxt_tick, init = next_initialized(bitmaps, tick, spacing, zero_for_one)
        if init is None: return None                   # off the cached window: unknown, not zero
        nxt_tick = max(MIN_TICK, min(MAX_TICK, nxt_tick))
        nxt_sqrt = sqrt_at_tick(nxt_tick)
        target = limit if ((nxt_sqrt < limit) if zero_for_one else (nxt_sqrt > limit)) else nxt_sqrt
        sp, used, got, fee_amt = compute_swap_step(sp, target, L, remaining, fee)
        remaining -= used + fee_amt
        out += got
        if sp == nxt_sqrt:
            if init:
                net = liq_net.get(nxt_tick)
                if net is None: return None            # we know a tick is crossed but not by how much
                L += -net if zero_for_one else net
                if L <= 0: return None
            tick = nxt_tick - 1 if zero_for_one else nxt_tick
        elif remaining > 0:
            break                                      # price moved without reaching a tick: done
    return out, sp, L, tick

def in_range_depth(sp, L, tick, spacing, zero_for_one, bitmaps):
    """How much input this pool absorbs before the price reaches the NEXT initialised tick.

    This is the right upper bound for a size search and the flat-liquidity reserve is not. Measured
    2026-09-15: bounding by the flat reserve made a 26-step ternary search necessary, and at 18 steps or
    fewer the search returned ZERO profit - not less profit, none - because the profitable band is narrow
    and the bracket never reached it. Bound the search where the liquidity actually ends.
    Returns None when the next tick is outside the cached window: unknown, not unlimited.
    """
    nxt, init = next_initialized(bitmaps, tick, spacing, zero_for_one)
    if init is None: return None
    nxt = max(MIN_TICK, min(MAX_TICK, nxt))
    target = sqrt_at_tick(nxt)
    if zero_for_one:
        if target >= sp: return None
        return _a0(target, sp, L, True)
    if target <= sp: return None
    return _a1(sp, target, L, True)

def _sqrt_at_tick_uncached(tick):
    """The same function without the cache, kept for the selftest only."""
    a = abs(tick)
    if a > MAX_TICK: raise ValueError('tick out of range %d' % tick)
    r = _M[0] if a & 1 else 0x100000000000000000000000000000000
    for i in range(1, 20):
        if a & (1 << i): r = (r * _M[i]) >> 128
    if tick > 0: r = ((1 << 256) - 1) // r
    return (r >> 32) + (1 if r % (1 << 32) else 0)

def v2_out(r_in, r_out, amount_in, fee, sol=False):
    """A constant-product (v2) pair, to the wei. `fee` in millionths like v3 (3000 = 0.30%).
    Uniswap v2: amountIn*(1-f)*R_out / (R_in + amountIn*(1-f)), one floor at the end (UniswapV2Library).
    Solidly/Aerodrome volatile: the fee is FLOORED off the input first, then x*y (Pool.getAmountOut).
    The two round differently, and the CALL leg's amountOut must never exceed what K allows."""
    if amount_in <= 0 or r_in <= 0 or r_out <= 0:
        return 0
    if sol:
        a = amount_in - amount_in * fee // 10 ** 6
        return a * r_out // (r_in + a)
    a = amount_in * (10 ** 6 - fee)
    return a * r_out // (r_in * 10 ** 6 + a)


def _selftest():
    import math, random
    # TickMath against the real function it implements - a single wrong magic constant shows here
    for t in (0, 1, -1, 100, -100, 60000, -60000, 276324, -276326, MAX_TICK, MIN_TICK):
        got = sqrt_at_tick(t); want = math.sqrt(1.0001 ** t) * Q96
        assert abs(got - want) / want < 1e-9, (t, got, want)
    assert sqrt_at_tick(MIN_TICK) == MIN_SQRT, sqrt_at_tick(MIN_TICK)
    assert sqrt_at_tick(0) == Q96
    # v2: UniswapV2Library.getAmountOut with 997/1000 is the same rational as fee 3000 in millionths
    for (ri, ro, x) in ((10 ** 21, 3 * 10 ** 24, 12345678901234567), (777, 999, 5), (10 ** 30, 10 ** 6, 10 ** 26)):
        assert v2_out(ri, ro, x, 3000) == x * 997 * ro // (ri * 1000 + x * 997)
        a = x - x * 30 // 10000
        assert v2_out(ri, ro, x, 3000, sol=True) == a * ro // (ri + a)
    assert v2_out(0, 5, 5, 3000) == 0 and v2_out(5, 5, 0, 3000) == 0
    assert sqrt_at_tick(1) > sqrt_at_tick(0) > sqrt_at_tick(-1), 'price must rise with tick'

    # inside ONE tick range with huge liquidity, the exact loop must agree with constant product
    L, fee = 10 ** 22, 3000
    sp = sqrt_at_tick(0)
    bm = {0: 1 << 200, -1: 1 << 3}                     # far-away ticks: we stay inside the range
    ln = {}
    r = swap(sp, L, 0, 1, fee, 10 ** 15, True, bm, ln)
    assert r, 'a swap that stays in range must price'
    out = r[0]
    cp = (10**15 * (1 - fee/1e6)) * (L * sp / Q96) / ((L * Q96 / sp) + 10**15 * (1 - fee/1e6))
    assert abs(out - cp) / cp < 1e-3, (out, cp)

    # a missing bitmap word must return None, NEVER a number
    assert swap(sp, L, 0, 1, fee, 10 ** 15, True, {}, {}) is None, 'unknown liquidity must not be priced'
    # a crossed tick with unknown liquidityNet must also refuse
    bm2 = {-1: 1 << 255}
    assert swap(sp, L, 0, 1, fee, 10 ** 24, True, bm2, {}) is None, 'unknown liquidityNet must not be priced'

    # bitmap search: find the tick we planted, in both directions
    t, i = next_initialized({0: 1 << 5}, 0, 1, False)
    assert i and t == 5, (t, i)
    t, i = next_initialized({0: 1 << 5}, 9, 1, True)
    assert i and t == 5, (t, i)
    t, i = next_initialized({0: 0}, 0, 1, False)
    assert i is False, 'an empty word is initialised-false, not unknown'
    assert next_initialized({}, 0, 1, False)[1] is None, 'an ABSENT word is unknown, not empty'

    # the in-range bound must be finite, positive, and refuse when the window is not cached.
    # Sitting EXACTLY on an initialised tick gives zero depth downward, which is correct, not a bug -
    # so the fixture puts the price at tick 100 with the next initialised tick below it at 5.
    L2 = 10 ** 22
    dep = in_range_depth(sqrt_at_tick(100), L2, 100, 1, True, {0: 1 << 5})
    assert dep and dep > 0, dep
    assert in_range_depth(sqrt_at_tick(100), L2, 100, 1, True, {}) is None, 'uncached is unknown, not unlimited'
    assert in_range_depth(sqrt_at_tick(0), L2, 0, 1, True, {0: 1}) is None, 'no room below an occupied tick'
    # a swap of exactly that depth must price, and land on the boundary
    r = swap(sqrt_at_tick(100), L2, 100, 1, 0, dep, True, {0: 1 << 5}, {5: 0})
    assert r, 'a swap sized to the tick boundary must price'
    assert r[1] <= sqrt_at_tick(100), 'price must have moved down'

    # the memoised sqrt_at_tick must return EXACTLY what the uncached maths returns, everywhere
    rnd = random.Random(4663)
    probe = [0, 1, -1, MAX_TICK, MIN_TICK, MAX_TICK - 1, MIN_TICK + 1] + \
            [rnd.randint(MIN_TICK, MAX_TICK) for _ in range(1000)]
    for t in probe:
        assert sqrt_at_tick(t) == _sqrt_at_tick_uncached(t), t
    for t in probe[:50]:
        assert sqrt_at_tick(t) == _sqrt_at_tick_uncached(t), ('cache hit differs', t)
    try: sqrt_at_tick(MAX_TICK + 1); raise AssertionError('out of range must raise')
    except ValueError: pass

    # the protocol fee compounds with the LP fee, it does not replace it
    assert swap_fee(3000, 0) == 3000
    assert swap_fee(3000, 25) == 25 + 3000 - (25 * 3000) // 10 ** 6, swap_fee(3000, 25)
    assert swap_fee(100, 1000) > 1100 - 1, 'a 0.1% protocol fee on a 0.01% pool must dominate it'

    # more input must always give more output, and the fee must always cost
    a = swap(sqrt_at_tick(0), L, 0, 1, 0, 10**15, True, bm, ln)[0]
    b = swap(sqrt_at_tick(0), L, 0, 1, 0, 2 * 10**15, True, bm, ln)[0]
    c = swap(sqrt_at_tick(0), L, 0, 1, 10000, 10**15, True, bm, ln)[0]
    assert b > a > c > 0, (a, b, c)
    print('selftest OK: 31 asserts + 1,057 memo-vs-maths (TickMath vs sqrt(1.0001^t), in-range vs CP, and refusing to guess)')
    return 0

if __name__ == '__main__': sys.exit(_selftest())
