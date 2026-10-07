"""The LP program's send path: approve, swap, mint, exit. Every send goes through core/exec/tx.send, so it is
DRY unless --go AND LIVE_OK=yes AND the wallet clears gas; a dry call answers with an eth_call of the same
calldata from the same wallet instead of a signature.

Contracts (chain.toml [lp]): v4 PositionManager, Permit2, UniversalRouter. Codes checked against
Uniswap/v4-periphery src/libraries/Actions.sol and universal-router Commands.sol (2026-09-24):
DECREASE_LIQUIDITY 0x01, MINT_POSITION 0x02, BURN_POSITION 0x03, SWAP_EXACT_IN_SINGLE 0x06, SETTLE_ALL 0x0c,
SETTLE_PAIR 0x0d, TAKE_ALL 0x0f, TAKE_PAIR 0x11; router command V4_SWAP 0x10. The robinhood router's bytecode
carries V4TooLittleReceivedPerHopSingle, i.e. the ExactInputSingleParams WITH minHopPriceX36 (0 = off).
The fork test (tests/lpfork.py) runs every call below against a fork of the real contracts.

Money path: USDG -> half into the token on the pick's own pool (minOut = exact-tick quote - slippage) ->
MINT_POSITION + SETTLE_PAIR with amount maxes = what the wallet holds, so a mint can never overdraw. Exit =
DECREASE_LIQUIDITY (full, amount mins = expected - lp_slippage) + BURN_POSITION + TAKE_PAIR, then the token
side is sold back on the same pool.

NATIVE ETH (owner 2026-09-30: 97% of the copy desk's leaders' positions sit in token/ETH pools). A v4 pool whose
currency0 is address(0) is quoted in native ETH: approvals are skipped for it, it is paid as the tx's value (a swap's
SETTLE_ALL and a mint's SETTLE_PAIR settle from msg.value), a mint appends SWEEP so what the pool did not take comes
back, and TAKE / TAKE_PAIR pay it out as plain ETH. quote_of(key) is the side a position is funded in. Every balance
of native ETH is measured net of the gas our own sends paid meanwhile (GAS_WEI), so a delta is the trade, not the gas."""
import time
from eth_abi import encode

DECREASE_LIQUIDITY, MINT_POSITION, BURN_POSITION = 0x01, 0x02, 0x03
SWAP_EXACT_IN_SINGLE, SETTLE_ALL, SETTLE_PAIR, TAKE_ALL, TAKE_PAIR = 0x06, 0x0c, 0x0d, 0x0f, 0x11
SWEEP = 0x14
NATIVE = '0x' + '0' * 40
GAS_WEI = [0]                  # gas our sends paid in this process, wei: native deltas add it back (see balance_net)
V4_SWAP = 0x10
Q96 = 2 ** 96
MAX160, MAX48, MAX256 = 2 ** 160 - 1, 2 ** 48 - 1, 2 ** 256 - 1
GAS_FLOOR_USD = 0.5
KEY_T = '(address,address,uint24,int24,address)'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
SWAP4 = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'     # core/events.py SWAP

# THE TRACE HOOK. core/lpbook.tracer() points this at var/lp/trace.jsonl for one run; every send, dry call and
# failure below writes one structured record through it (stage 'tx' / 'error'). None = silent (selftests).
TRACE = None


def _ev(stage, **kw):
    if TRACE:
        try:
            TRACE(stage, **kw)
        except Exception:
            pass


# errors the LP path can hit, decoded by name in the trace (Permit2, PositionManager, V4Router, v4 core)
_ERRS = ['AllowanceExpired(uint256)', 'InsufficientAllowance(uint256)', 'V4TooLittleReceived(uint256,uint256)',
         'V4TooMuchRequested(uint256,uint256)', 'MaximumAmountExceeded(uint128,uint128)',
         'MinimumAmountInsufficient(uint128,uint128)', 'DeadlinePassed(uint256)', 'NotApproved(address)',
         'TransactionDeadlinePassed()', 'ExecutionFailed(uint256,bytes)', 'PriceLimitAlreadyExceeded(uint160,uint160)',
         'TickLiquidityOverflow(int24)', 'CurrencyNotSettled()', 'InvalidTickRange()', 'TicksMisordered(int24,int24)',
         'CannotUpdateEmptyPosition()', 'Error(string)']


def revert_name(exc_or_hex):
    """'Name(args)' for a revert payload (or an eth_call exception carrying one), else the raw text."""
    import re
    from eth_abi import decode
    raw = exc_or_hex if isinstance(exc_or_hex, str) and exc_or_hex.startswith('0x') else None
    if raw is None:
        m = re.search(r"'data': '(0x[0-9a-fA-F]+)", str(exc_or_hex)) or re.search(r'custom error (0x[0-9a-fA-F]{8})', str(exc_or_hex))
        raw = m.group(1) if m else None
    if not raw:
        return str(exc_or_hex)[:200]
    for sig in _ERRS:
        if raw[:10] == _sel(sig):
            args = sig[sig.index('(') + 1:-1]
            try:
                vals = decode(args.split(','), bytes.fromhex(raw[10:])) if args else ()
                return '%s(%s)' % (sig[:sig.index('(')], ', '.join('0x' + v.hex() if isinstance(v, bytes) else str(v) for v in vals))
            except Exception:
                return sig
    return raw[:74]


def _sel(sig):
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256); k.update(sig.encode()); return '0x' + k.hexdigest()[:8]


def cfg(chain):
    return chain.cfg['lp']


def key_tuple(k):
    return (k['currency0'], k['currency1'], int(k['fee']), int(k['tickSpacing']), k['hooks'])


def pool_id(k):
    from core.model import Pool
    return Pool(None, k['currency0'], k['currency1'], k['fee'], k['tickSpacing'], k['hooks']).pid()


def usdg(chain):
    """The LP program's quote token: [lp] quote (a [quotes] symbol; USDG on robinhood, USDC on arc). An ERC-20, never
    the native coin: Permit2 cannot approve native, so native-quoted pools stay outside the program."""
    sym = (((getattr(chain, 'cfg', None) or {}).get('lp') or {}).get('quote') or 'USDG').upper()
    return next(a for a, v in chain.quotes.items() if v[0].upper() == sym and int(a, 16) != 0)


def deadline(chain, s=600):
    """`s` seconds past the later of the wall clock and the chain's own latest block time."""
    try:
        ts = int(chain.node.rpc('eth_getBlockByNumber', ['latest', False])['timestamp'], 16)
    except Exception:
        ts = 0
    return max(int(time.time()), ts) + s


def is_native(tok):
    return int(tok or '0x0', 16) == 0


def quote_of(chain, key):
    """The side a position in `key` is funded in: native ETH when the pool is quoted in it (currency0 = address(0)),
    else the program's ERC-20 quote (USDG)."""
    return NATIVE if is_native(key['currency0']) else usdg(chain)


def balance(chain, tok, addr):
    if is_native(tok):
        return int(chain.node.rpc('eth_getBalance', [addr, 'latest']), 16)
    return int(chain.node.call(tok, '0x70a08231' + addr[2:].lower().rjust(64, '0')), 16)


def balance_net(chain, tok, addr):
    """balance() plus, for native ETH, the gas our sends paid so far: the difference of two readings is then what the
    trades moved, not the gas (a native exit that returned 0.02 ETH read 0.0199 net of its own two sends)."""
    return balance(chain, tok, addr) + (GAS_WEI[0] if is_native(tok) else 0)


# --------------------------------------------------------------------------- liquidity maths (LiquidityAmounts)
def l_from_a0(sa, sb, a0):
    sa, sb = min(sa, sb), max(sa, sb)
    return a0 * (sa * sb // Q96) // (sb - sa)


def l_from_a1(sa, sb, a1):
    sa, sb = min(sa, sb), max(sa, sb)
    return a1 * Q96 // (sb - sa)


def liquidity_for(sp, sa, sb, a0, a1):
    if sp <= sa:
        return l_from_a0(sa, sb, a0)
    if sp < sb:
        return min(l_from_a0(sp, sb, a0), l_from_a1(sa, sp, a1))
    return l_from_a1(sa, sb, a1)


def amounts_for(sp, sa, sb, L, up=False):
    """(amount0, amount1) of liquidity L on [sa, sb] at sqrtP sp (core/amm's exact rounding)."""
    from core import amm
    if sp <= sa:
        return amm._a0(sa, sb, L, up), 0
    if sp < sb:
        return amm._a0(sp, sb, L, up), amm._a1(sa, sp, L, up)
    return 0, amm._a1(sa, sb, L, up)


def token_share(sp, sa, sb, usdg_is_c0):
    """The share of a position's value on [sa, sb] at sqrtP sp that sits in the TOKEN (not USDG)."""
    a0, a1 = amounts_for(sp, sa, sb, 10 ** 24)
    p = (sp / Q96) ** 2
    v0, v1 = a0 * p, a1                          # both in currency1 units
    tok = v1 if usdg_is_c0 else v0
    return tok / (v0 + v1) if v0 + v1 else 0.5


def cost_frac(spent, got, sp, quote_is_c0, size_raw):
    """What an entry swap costs as a share of the copy: `spent` quote raw in, `got` token raw out, valued at the mid
    price sqrtP `sp` (X96) - the fee and the price impact together. 0 when nothing is swapped."""
    if not spent or not size_raw:
        return 0.0
    p = (sp / Q96) ** 2                          # token1 per token0
    val = got / p if quote_is_c0 else got * p    # the token we got, in quote raw at mid
    return max(0.0, (spent - val) / size_raw)


def entry_cost_frac(chain, key, tl, tu, size_raw, skey=None):
    """THE ENTRY COST of a range copy BEFORE it is sent (owner 2026-10-02): the swap _fund would make (the range's token
    share at this price, grossed up for the fee), quoted exactly on its pool, against its value at mid. A share of the
    copy, or None when the pool cannot be quoted (never a guess: an unreadable pool is not skipped for its cost)."""
    from core import amm
    skey = skey or key
    u = quote_of(chain, key)
    sp = slot0(chain, key)[0]
    q0 = key['currency0'].lower() == u
    r = token_share(sp, amm.sqrt_at_tick(tl), amm.sqrt_at_tick(tu), q0)
    if r <= 0:
        return 0.0                               # all quote at this price: a bid, nothing to swap
    fee = int(skey['fee']) / 1e6 if int(skey['fee']) < DYNAMIC_FEE else None
    if fee is None:
        return None
    half = int(size_raw * r / ((1 - fee) * (1 - r) + r))
    got = quote(chain, skey, skey['currency0'].lower() == u, half) if half else 0
    if got is None:
        return None
    return cost_frac(half, got, sp, q0, size_raw)


def tick_range(sqrt_price_x96, ts, rng=2.0):
    """[P/rng, P*rng] as ticks aligned OUTWARD to tickSpacing (so the range never shrinks below +-100%)."""
    import math
    t = math.log((sqrt_price_x96 / Q96) ** 2) / math.log(1.0001)
    half = math.log(rng) / math.log(1.0001)
    return int(math.floor((t - half) / ts) * ts), int(math.ceil((t + half) / ts) * ts)


# --------------------------------------------------------------------------- calldata
def swap_calldata(key, zfo, amount_in, min_out, deadline):
    """UniversalRouter.execute(V4_SWAP: SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL)."""
    cin, cout = (key['currency0'], key['currency1']) if zfo else (key['currency1'], key['currency0'])
    params = [encode(['(%s,bool,uint128,uint128,uint256,bytes)' % KEY_T], [(key_tuple(key), zfo, amount_in, min_out, 0, b'')]),
              encode(['address', 'uint256'], [cin, amount_in]),
              encode(['address', 'uint256'], [cout, min_out])]
    inp = encode(['bytes', 'bytes[]'], [bytes([SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL]), params])
    return _sel('execute(bytes,bytes[],uint256)') + encode(['bytes', 'bytes[]', 'uint256'], [bytes([V4_SWAP]), [inp], deadline]).hex()


def _modify(actions, params, deadline):
    return _sel('modifyLiquidities(bytes,uint256)') + encode(
        ['bytes', 'uint256'], [encode(['bytes', 'bytes[]'], [bytes(actions), params]), deadline]).hex()


def mint_calldata(key, tl, tu, liq, a0max, a1max, owner, deadline):
    acts = [MINT_POSITION, SETTLE_PAIR]
    prm = [encode([KEY_T, 'int24', 'int24', 'uint256', 'uint128', 'uint128', 'address', 'bytes'],
                  [key_tuple(key), tl, tu, liq, a0max, a1max, owner, b'']),
           encode(['address', 'address'], [key['currency0'], key['currency1']])]
    if is_native(key['currency0']):
        # the ETH sent as value that the pool did not take stays in the PositionManager unless swept back to us
        acts.append(SWEEP)
        prm.append(encode(['address', 'address'], [NATIVE, owner]))
    return _modify(acts, prm, deadline)


def exit_calldata(token_id, liq, a0min, a1min, key, recipient, deadline):
    return _modify([DECREASE_LIQUIDITY, BURN_POSITION, TAKE_PAIR], [
        encode(['uint256', 'uint256', 'uint128', 'uint128', 'bytes'], [token_id, liq, a0min, a1min, b'']),
        encode(['uint256', 'uint128', 'uint128', 'bytes'], [token_id, 0, 0, b'']),
        encode(['address', 'address', 'address'], [key['currency0'], key['currency1'], recipient])], deadline)


# --------------------------------------------------------------------------- reads
def slot0(chain, key):
    """(sqrtPriceX96, tick, lpFee, protocolFee pair) of the pool, from StateView."""
    from core import tickfetch
    h = tickfetch.heads(chain.node, chain.stateview, {'p': dict(kind='v4', id=pool_id(key), ts=key['tickSpacing'])})
    if 'p' not in h:
        raise RuntimeError('pool %s unreadable' % pool_id(key)[:12])
    sp, tick, L, pf, lf = h['p']
    return sp, tick, L, pf


def quote(chain, key, zfo, amount_in, fee=None):
    """Exact-tick output of an exact-in swap on this pool, or None (window unknown: never a guess). A dynamic-fee
    pool's key carries a flag, not a fee: its caller passes `fee` (our own hook's is known; nobody else's is)."""
    from core import tickfetch, amm
    e = {'p': dict(kind='v4', id=pool_id(key), ts=key['tickSpacing'])}
    st = tickfetch.fetch(chain.node, chain.stateview, e, span=4).get('p')
    if not st:
        return None
    lp_fee = int(key['fee']) if fee is None else int(fee)
    if lp_fee >= DYNAMIC_FEE:
        return None
    fee = amm.swap_fee(lp_fee, st['pf'][0 if zfo else 1])
    r = amm.swap(st['sp'], st['L'], st['tick'], key['tickSpacing'], fee, amount_in, zfo, st['bitmaps'], st['net'])
    return r[0] if r else None


def pool_key(chain, pid):
    """The PoolKey of a v4 pool id, from PositionManager.poolKeys(bytes25) (written by the first mint, never cleared)."""
    r = chain.node.call(cfg(chain)['positionmanager'], _sel('poolKeys(bytes25)') + pid[2:52].ljust(64, '0'))
    w = lambda i: int(r[2 + 64 * i:66 + 64 * i], 16)
    ts = w(3)
    k = dict(currency0='0x%040x' % w(0), currency1='0x%040x' % w(1), fee=w(2), tickSpacing=ts - (1 << 256) if ts >> 255 else ts,
             hooks='0x%040x' % w(4))
    # a pool this PositionManager never minted in answers ALL ZEROS - and a zero currency0 reads as native ETH. The key
    # is only a key if it hashes back to the pool id
    if pool_id(k).lower() != pid.lower():
        raise RuntimeError('pool %s: no PoolKey in the PositionManager (never minted through it)' % pid[:12])
    return k


def token_id_of(chain, rc):
    pm = cfg(chain)['positionmanager'].lower()
    for lg in rc.get('logs') or []:
        t = lg.get('topics') or []
        if lg['address'].lower() == pm and len(t) == 4 and t[0] == TRANSFER and int(t[1], 16) == 0:
            return int(t[3], 16)
    return None


# --------------------------------------------------------------------------- sends
# EVERY RECEIPT A SEND LANDS (2026-10-01): the copy desk books an entry's cost and an exit's return from the USDG Transfer logs of
# its own txs (usdg_flow) - balance reads before and after went to load-balanced public nodes at different heads and swapped
# $68.51 between two copies of one pool. The desk marks len() before a step and clears it between steps
RECEIPTS = []


def usdg_flow(chain, addr, receipts):
    """Net raw USDG into `addr` over these receipts' Transfer logs (in minus out)."""
    u, me = usdg(chain).lower(), '0x' + '0' * 24 + addr[2:].lower()
    t = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
    net = 0
    for rc in receipts:
        for l in (rc or {}).get('logs') or []:
            if l['address'].lower() == u and l['topics'][0] == t and len(l['topics']) == 3:
                v = int(l['data'], 16) if l['data'] not in ('0x', '') else 0
                net += v if l['topics'][2].lower() == me else -v if l['topics'][1].lower() == me else 0
    return net


def _send(chain, ac, to, data, go, log=print, what='', ctx=None, after=None, value=0):
    """One call. go=False: an eth_call from the same wallet (returns None; a revert is reported, not raised).
    go=True: sign through tx.send and return the receipt, raising unless status 1. Either way ONE trace record:
    stage 'tx' (action, tx, block, gas_used, gas_usd, ms, + ctx + after(receipt)) or stage 'error' (the decoded
    revert)."""
    from core.exec import tx
    ctx = dict(ctx or {})
    t0 = time.time()
    if not go:
        # a dry step that depends on an earlier one (a swap before its approve landed) reverts here: that is
        # reported, not raised, so the dry plan still shows every step
        try:
            chain.node.rpc('eth_call', [{'from': ac.address, 'to': to, 'data': data, 'value': hex(value)}, 'latest'])
            log('   dry %-10s eth_call OK' % what)
            _ev('tx', action=what, dry=True, ok=True, ms=round((time.time() - t0) * 1e3), **ctx)
        except Exception as e:
            log('   dry %-10s eth_call REVERTS: %s' % (what, revert_name(e)))
            _ev('tx', action=what, dry=True, ok=False, revert=revert_name(e), ms=round((time.time() - t0) * 1e3), **ctx)
        return None
    try:
        gas = int(int(chain.node.rpc('eth_estimateGas', [{'from': ac.address, 'to': to, 'data': data, 'value': hex(value)}]), 16) * 1.3)
    except Exception as e:
        _ev('error', action=what, at='estimateGas', revert=revert_name(e), **ctx)
        raise RuntimeError('%s would revert: %s' % (what, revert_name(e)))
    try:
        rc = tx.send(chain, ac, {'to': to, 'data': data, 'gas': gas, 'value': int(value)}, go, gas_floor=GAS_FLOOR_USD)
    except Exception as e:
        _ev('error', action=what, at='send', revert=revert_name(e), **ctx)
        raise
    ms = round((time.time() - t0) * 1e3)
    if not rc or int(rc['status'], 16) != 1:
        # name the revert: the same call replayed on the landing block's state (2026-10-02 V4 mint: the pool moved 13%
        # between our swap and the mint, MaximumAmountExceeded, and the page said only "mint failed: <tx>")
        why = None
        if rc:
            try:
                chain.node.rpc('eth_call', [{'from': ac.address, 'to': to, 'data': data, 'value': hex(value)}, rc['blockNumber']])
            except Exception as e:
                why = revert_name(e)
        _ev('error', action=what, at='receipt', tx=rc and rc.get('transactionHash'), refused=rc is None, ms=ms, revert=why, **ctx)
        raise RuntimeError('%s failed: %s%s' % (what, rc and rc.get('transactionHash') or 'refused before signing', ' (%s)' % why if why else ''))
    used = int(rc['gasUsed'], 16)
    price = int(rc.get('effectiveGasPrice') or '0x0', 16)
    GAS_WEI[0] += used * price
    RECEIPTS.append(rc)
    extra = {}
    if after:
        try:
            extra = after(rc)
        except Exception as e:
            extra = dict(after_error=str(e)[:120])
    _ev('tx', action=what, tx=rc['transactionHash'], block=int(rc['blockNumber'], 16), gas_used=used,
        gas_usd=round(used * price / 10 ** chain.native_dec * chain.eth_usd, 5), ms=ms, **ctx, **extra)
    log('   %-10s %s' % (what, rc['transactionHash']))
    return rc


def approve(chain, ac, token, go, log=print, ctx=None):
    """ERC-20 -> Permit2 (max), then Permit2 -> PositionManager and -> UniversalRouter (max, no expiry).
    Only what is missing is sent."""
    if is_native(token):
        return 0                                   # native ETH is paid as value: nothing to approve
    c = cfg(chain)
    p2, n = c['permit2'], 0
    allow = int(chain.node.call(token, _sel('allowance(address,address)') + encode(['address', 'address'], [ac.address, p2]).hex()), 16)
    if allow < 2 ** 200:
        _send(chain, ac, token, _sel('approve(address,uint256)') + encode(['address', 'uint256'], [p2, MAX256]).hex(), go, log, 'approve',
              dict(ctx or {}, token=token))
        n += 1
    for spender in (c['positionmanager'], c['universalrouter']):
        r = chain.node.call(p2, _sel('allowance(address,address,address)') + encode(['address'] * 3, [ac.address, token, spender]).hex())
        amt, exp = int(r[2:66], 16), int(r[66:130], 16)
        if amt < 2 ** 150 or exp < time.time() + 30 * 86400:
            _send(chain, ac, p2, _sel('approve(address,address,uint160,uint48)') + encode(
                ['address', 'address', 'uint160', 'uint48'], [token, spender, MAX160, MAX48]).hex(), go, log, 'permit2',
                dict(ctx or {}, token=token, spender=spender))
            n += 1
    return n


def swap(chain, ac, key, token_in, amount_in, go, log=print, ctx=None, fee=None):
    """Exact-in swap on the pool `key` through the UniversalRouter; minOut = quote - slippage.
    Returns (quoted_out, receipt). The trace carries quote vs what actually arrived (slip_bps; + = worse)."""
    zfo = token_in.lower() == key['currency0'].lower()
    tout = key['currency1'] if zfo else key['currency0']
    pid = pool_id(key)
    q = quote(chain, key, zfo, amount_in, fee)
    if not q:
        _ev('error', action='swap', at='quote', pool=pid, why='tick window unknown or empty', **(ctx or {}))
        raise RuntimeError('no quote on %s: tick window unknown or empty' % pid[:12])
    min_out = int(q * (1 - float(cfg(chain)['slippage'])))
    data = swap_calldata(key, zfo, amount_in, min_out, deadline(chain))
    b0 = balance_net(chain, tout, ac.address) if go else 0

    def fill(rc):
        got = balance_net(chain, tout, ac.address) - b0
        charged = [int(lg['data'][2 + 64 * 5:2 + 64 * 6], 16) for lg in rc.get('logs') or []
                   if lg['address'].lower() == chain.pm.lower() and lg['topics'][0] == SWAP4 and lg['topics'][1].lower() == pid]
        return dict(got_out=got, slip_bps=round((q - got) / q * 1e4, 1), fee_charged=charged[0] if charged else None)
    c = dict(ctx or {}, pool=pid, token_in=token_in, amount_in=amount_in, quote_out=q, min_out=min_out)
    return q, _send(chain, ac, cfg(chain)['universalrouter'], data, go, log, 'swap', c, after=fill,
                    value=amount_in if is_native(token_in) else 0)


def _fund(chain, ac, key, skey, tok, tl, tu, size_raw, go, log, cx, price, what='mint'):
    """Turn `size_raw` USDG into the two sides of a position on [tl, tu]: swap the range's token share (grossed
    up for the fee) on `skey`, then size the liquidity from what the wallet got. (amount0, amount1, L, sqrtP)."""
    from core import amm
    u = quote_of(chain, key)
    for t in (u, tok):
        approve(chain, ac, t, go, log, cx)
    tb0 = balance(chain, tok, ac.address)
    sa, sb = amm.sqrt_at_tick(tl), amm.sqrt_at_tick(tu)
    # swap the RANGE's own token share, not half: aligned ticks are not symmetric around the price, and a 50/50
    # swap left up to 310 bps of the size idle outside the range (fork trace 2026-09-24, Bucket)
    # ... and gross it up for the fee: the swap delivers (1 - f) of what goes in, so a share r of value in the token
    # needs s = r / ((1 - f)(1 - r) + r) of the USDG swapped (a plain r left fee x r idle: 150 bps on a 3% pool)
    r = token_share(price(), sa, sb, key['currency0'].lower() == u)
    fee = int(skey['fee']) / 1e6
    half = int(size_raw * r / ((1 - fee) * (1 - r) + r))
    q, _ = swap(chain, ac, skey, u, half, go, log, cx) if half else (0, None)
    got = (balance(chain, tok, ac.address) - tb0) if go else q
    sp = price()
    u_amt = size_raw - half
    a0, a1 = (u_amt, got) if key['currency0'].lower() == u else (got, u_amt)
    liq = int(liquidity_for(sp, sa, sb, a0, a1) * (1 - float(cfg(chain)['lp_slippage'])))
    if liq <= 0:
        _ev('error', action=what, at='liquidity', why='zero liquidity from %d / %d' % (a0, a1), **cx)
        raise RuntimeError('zero liquidity for %s' % cx.get('pool', '?')[:12])
    return a0, a1, liq, sp


def enter(chain, ac, pick, size_raw, go, log=print):
    """Swap half of `size_raw` USDG into the token on the pick's pool, then mint the pick's ticks with what the
    wallet holds of both sides (capped at the size). Returns the position record (tokenId None when dry)."""
    from core import amm
    key = pick['key']
    u = quote_of(chain, key)
    tok = pick['token']
    cx = dict(pool=pick['pool'], sym=pick.get('symbol', ''), leg='enter')
    skey = pick.get('swap_key') or key          # a hooked pool of ours is born empty: buy the token on the incumbent

    def price():
        try:
            return slot0(chain, key)[0]
        except RuntimeError:
            if go or skey is key:
                raise
            return slot0(chain, skey)[0]            # dry: the pool is not initialised yet - it will be, at this price
    a0, a1, liq, sp = _fund(chain, ac, key, skey, tok, pick['tick_lower'], pick['tick_upper'], size_raw, go, log, cx, price)
    rec = dict(pool=pick['pool'], key=key, token=tok, symbol=pick.get('symbol', ''), usdg_is_c0=key['currency0'].lower() == u,
               tick_lower=pick['tick_lower'], tick_upper=pick['tick_upper'], liquidity=liq, entry_raw=size_raw,
               entry_ts=time.time(), token_id=None, swap_key=skey, hooked=skey is not key)
    if not go:
        log('   dry mint     L %d on %d..%d (not simulated: it spends the swap output)' % (liq, pick['tick_lower'], pick['tick_upper']))
        return rec
    data = mint_calldata(key, pick['tick_lower'], pick['tick_upper'], liq, a0, a1, ac.address, deadline(chain))
    b = (balance_net(chain, key['currency0'], ac.address), balance_net(chain, key['currency1'], ac.address))

    def minted(rc):
        s0 = b[0] - balance_net(chain, key['currency0'], ac.address)
        s1 = b[1] - balance_net(chain, key['currency1'], ac.address)
        # what the mint really took vs the maxes offered: the rest stays idle in the wallet
        us, um = (s0, a0) if key['currency0'].lower() == u else (s1, a1)
        rec['mint'] = dict(spent0=s0, spent1=s1, max0=a0, max1=a1, sp=sp)
        return dict(token_id=token_id_of(chain, rc), liquidity=liq, spent0=s0, spent1=s1, max0=a0, max1=a1,
                    usdg_left_bps=round(1e4 * (1 - us / um), 1) if um else None)
    rc = _send(chain, ac, cfg(chain)['positionmanager'], data, go, log, 'mint', dict(cx, ticks=[pick['tick_lower'], pick['tick_upper']]),
               after=minted, value=a0 if is_native(key['currency0']) else 0)
    rec.update(token_id=token_id_of(chain, rc), entry_tx=rc['transactionHash'], entry_block=int(rc['blockNumber'], 16))
    return rec


INCREASE_LIQUIDITY = 0x00


def collect_calldata(token_id, key, recipient, deadline_):
    """Fees only: DECREASE_LIQUIDITY by 0 credits the position's owed fees, TAKE_PAIR sends them out."""
    return _modify([DECREASE_LIQUIDITY, TAKE_PAIR], [
        encode(['uint256', 'uint256', 'uint128', 'uint128', 'bytes'], [token_id, 0, 0, 0, b'']),
        encode(['address', 'address', 'address'], [key['currency0'], key['currency1'], recipient])], deadline_)


def increase_calldata(token_id, liq, a0max, a1max, key, deadline_):
    return _modify([INCREASE_LIQUIDITY, SETTLE_PAIR], [
        encode(['uint256', 'uint256', 'uint128', 'uint128', 'bytes'], [token_id, liq, a0max, a1max, b'']),
        encode(['address', 'address'], [key['currency0'], key['currency1']])], deadline_)


def collect(chain, ac, pos, go, log=print):
    """Take a kept position's fees into the wallet and sell the token side back to USDG. Returns the USDG raw
    the wallet gained (0 dry). ponytail: sell-then-top-up swaps the token fees twice (~fee x share each way);
    feeding them straight into INCREASE_LIQUIDITY saves that, worth it once positions are large."""
    key = pos['key']
    u = quote_of(chain, key)
    cx = dict(pool=pos['pool'], sym=pos.get('symbol', ''), leg='collect', token_id=pos['token_id'])
    u0 = balance(chain, u, ac.address)
    b = (balance(chain, key['currency0'], ac.address), balance(chain, key['currency1'], ac.address))

    def got(rc):
        return dict(fee0=balance(chain, key['currency0'], ac.address) - b[0], fee1=balance(chain, key['currency1'], ac.address) - b[1])
    _send(chain, ac, cfg(chain)['positionmanager'], collect_calldata(pos['token_id'], key, ac.address, deadline(chain)),
          go, log, 'collect', cx, after=got)
    if go:
        sell(chain, ac, pos.get('swap_key') or key, pos['token'], go, log, cx, fee=pos.get('swap_fee'))
    return (balance(chain, u, ac.address) - u0) if go else 0


def top_up(chain, ac, pos, size_raw, go, log=print):
    """Add `size_raw` USDG to an existing position (same ticks): the reinvest half of 'all fees reinvested'.
    Returns the liquidity added (0 dry)."""
    key, skey = pos['key'], pos.get('swap_key') or pos['key']
    cx = dict(pool=pos['pool'], sym=pos.get('symbol', ''), leg='top_up', token_id=pos['token_id'])
    price = lambda: slot0(chain, key)[0]
    a0, a1, liq, sp = _fund(chain, ac, key, skey, pos['token'], pos['tick_lower'], pos['tick_upper'], size_raw, go, log, cx,
                            price, 'increase')
    if not go:
        log('   dry increase L %d on #%s' % (liq, pos['token_id']))
        return 0
    _send(chain, ac, cfg(chain)['positionmanager'], increase_calldata(pos['token_id'], liq, a0, a1, key, deadline(chain)),
          go, log, 'increase', dict(cx, liquidity=liq, max0=a0, max1=a1, usd_in=size_raw), value=a0 if is_native(key['currency0']) else 0)
    pos['liquidity'] = int(pos.get('liquidity') or 0) + liq
    return liq


def exit(chain, ac, pos, go, log=print):
    """Close one position (decrease all, burn, take both) and sell the token side on the same pool.
    Returns the USDG raw received by the wallet (0 when dry)."""
    from core import amm
    c, key = cfg(chain), pos['key']
    u = quote_of(chain, key)
    pm = c['positionmanager']
    liq = int(chain.node.call(pm, _sel('getPositionLiquidity(uint256)') + encode(['uint256'], [pos['token_id']]).hex()), 16)
    k = 1 - float(c['lp_slippage'])
    u0 = balance_net(chain, u, ac.address)
    cx = dict(pool=pos['pool'], sym=pos.get('symbol', ''), leg='exit', token_id=pos['token_id'])
    b = (balance_net(chain, key['currency0'], ac.address), balance_net(chain, key['currency1'], ac.address))
    # A leader's exit is often followed by its sell, so the price can move between the slot0 read and the estimate
    # (DOTS 2026-09-29: MinimumAmountInsufficient(16304389, 16113350), 1.2% under a 0.5% bound). The refusal comes from
    # estimateGas, before any send, so re-reading the price and asking again costs no gas. The bound itself stays.
    for i in range(3):
        sp = slot0(chain, key)[0]
        e0, e1 = amounts_for(sp, amm.sqrt_at_tick(pos['tick_lower']), amm.sqrt_at_tick(pos['tick_upper']), liq)

        def taken(rc, e0=e0, e1=e1):
            return dict(liquidity=liq, expect0=e0, expect1=e1,
                        got0=balance_net(chain, key['currency0'], ac.address) - b[0], got1=balance_net(chain, key['currency1'], ac.address) - b[1])
        try:
            _send(chain, ac, pm, exit_calldata(pos['token_id'], liq, int(e0 * k), int(e1 * k), key, ac.address,
                                               deadline(chain)), go, log, 'exit', cx, after=taken)
            break
        except RuntimeError as e:
            if i == 2 or 'MinimumAmountInsufficient' not in str(e):
                raise
            log('   exit bound missed (%s), re-reading the price' % str(e)[:80])
            time.sleep(1.0)
    # THE BURN IS DONE, SO THE SELL MUST NOT LOSE THE POSITION. A sell refused at its own slippage floor (the quote moved
    # between estimate and send: DEADBEEF 2026-09-29, V4TooLittleReceived(14509555, 14400707)) used to raise here, after
    # the burn, and the caller kept a copy open that no longer existed. Re-quote and retry; if it still fails, the token
    # stays in the wallet (the next sell of that token sweeps it) and the USDG the burn returned is still reported.
    # SELL ONLY WHAT THIS EXIT RECEIVED (2026-09-30): selling the wallet's whole balance swept the token an earlier exit had
    # failed to sell into THIS copy's return (VRAX #3467322: +$68.47 in 24 s, fees $0; the earlier copy booked -$21.56
    # without its token). Leftovers are the treasury's dust (copydesk/treasury.settle), never a copy's P&L.
    ti = 0 if pos['token'].lower() == key['currency0'].lower() else 1
    got_tok = (balance_net(chain, pos['token'], ac.address) - b[ti]) if go else 0
    # SMART EXIT ROUTING: the token side is sold where it pays most - Relay, the copy's own pool or any other hookless
    # USDG/ETH pool of the token (pos['routes']) - by exact quote, and NEVER under the floor of its fair value (sell_best)
    sk = pos.get('swap_key') or key
    if got_tok > 0 and go:
        ok, pos['route'] = sell_best(chain, ac, pos['token'], got_tok, [sk] + [k for k in (pos.get('routes') or []) if pool_id(k) != pool_id(sk)],
                                     pos.get('eth_usd') or 0.0, pos.get('relay'), go, log, cx, floor=pos.get('floor', EXIT_FLOOR))
        if not ok:                                            # held: the caller books it and sells it when it pays (lpfollow.settle_unsold)
            pos['held'] = dict(token=pos['token'], amount=str(got_tok), fair_usd=pos['route'].get('fair_usd'))
    elif not go:
        sell(chain, ac, sk, pos['token'], go, log, cx)
    return (balance_net(chain, u, ac.address) - u0) if go else 0


# THE SALE FLOOR (owner 2026-10-03, after FIRM: "This can NEVER happen again. Smart routing should always work"). Copy
# #3567520's 872,979 FIRM were worth ~$52 at the pool's marginal price before the leaders pulled and Relay quoted $43.69; the Relay send would
# revert (its quote was stale: our other FIRM copy had sold 1.05M FIRM 29 s earlier and the leaders were pulling), and
# the fallback sold EVERYTHING into the copy's own pool, drained to $279 of depth, for $20.11 (-$27 on one sale).
# Now a sale goes through only at >= `floor` x its fair value (the best pool's marginal price - a 1/200 clip quoted
# exactly, fee included, impact not - or Relay's own quote if higher); a failed or refused Relay send is re-quoted
# fresh (`tries`, 2 s apart) instead of falling back blind; and when nothing pays the floor the token is HELD and
# retried every minute by the desk (lpfollow.settle_unsold), never dumped.
EXIT_FLOOR = 0.75


def fair_usd(chain, token, amount, keys, eth_usd, frac=200, quote_fn=None):
    """`amount` of `token` at the best pool's marginal price: a 1/frac clip quoted exactly, scaled up (fee in, impact out)."""
    small = max(1, int(amount) // frac)
    r = best_sale(chain, token, small, keys, eth_usd, quote_fn=quote_fn)
    return r[0][0] * int(amount) / small if r else None


def sell_best(chain, ac, token, amount, keys, eth_usd, rl, go, log=print, cx=None, floor=EXIT_FLOOR, tries=3,
              quote_fn=None, rq_fn=None, agg_send=None, pool_send=None, nap=2.0):
    """Sell `amount` of `token` where it pays most: EVERY aggregator (rl: dict(margin, slip_bps, timeout, aggs); None = our
    pools only) and the best of `keys` (keys[0] = the copy's own pool), but only at >= floor x its fair value. Each round
    quotes everything fresh; the best payer is sent first and a refused or reverting one falls to the next payer of the same
    round, and only after every aggregator of the LAST round failed does a pool take it. -> (sold, route); route['venue'] is
    the aggregator's name | 'pool' | 'held'. The *_fn / *_send arguments are the selftest's fakes."""
    rl = rl or None
    slip = (rl or {}).get('slip_bps', 100)
    # SLIPPAGE WIDENS PER ROUND (2026-10-03 ASTRAL: Kyber $43.32, Relay $42.71, LI.FI $42.51 all reverted on 1 % in a token falling
    # 90 %, and the best pool paid $40.77): round i allows min(slip x (i + 1), 300 bps), still only for an aggregator that beats the
    # pool and the floor
    rq_fn = rq_fn or (lambda errs, s_=slip: agg_quotes(chain, ac.address, token, amount, s_, (rl or {}).get('timeout', 3.0), (rl or {}).get('aggs', AGGS), errs))
    agg_send = agg_send or (lambda q: agg_sell(chain, ac, q, token, amount, go, log, cx))

    def _pool(k):
        for i in range(3):
            try:
                return sell(chain, ac, k, token, go, log, cx, amount=amount)
            except Exception as e:
                log('   exit sell attempt %d failed: %s' % (i + 1, str(e)[:120]))
                if i == 2:
                    raise
                time.sleep(1.0)
    pool_send = pool_send or _pool
    own = pool_id(keys[0]) if keys else None
    route = dict(pool=None, usd=None, own_usd=None, n=0, amount=str(int(amount)), venue='held', tries=0, errors={})
    for i in range(tries):
        route['tries'] = i + 1
        import threading
        aq, errs, th = {}, {}, None
        if rl:                                                # every aggregator in parallel with our pools' quotes
            si = min(slip * (i + 1), max(slip, 300))
            route['slip_bps'] = si
            th = threading.Thread(target=lambda: aq.update(q=rq_fn(errs, si) if rq_fn.__code__.co_argcount > 1 else rq_fn(errs)), daemon=True)
            th.start()
        ranked = best_sale(chain, token, amount, keys, eth_usd, quote_fn=quote_fn)
        fair = fair_usd(chain, token, amount, keys, eth_usd, quote_fn=quote_fn)
        if th:
            th.join((rl or {}).get('timeout', 3.0) * 2 + 2)
        aggs = aq.get('q') or []
        pool_usd = ranked[0][0] if ranked else None
        ref = max([v for v in [fair] + [q['usd'] for q in aggs] if v] or [0.0])
        bar = floor * ref
        rq = next((q for q in aggs if q['name'] == 'relay'), None)
        route.update(pool=pool_id(ranked[0][2]) if ranked else None, usd=round(pool_usd, 4) if pool_usd is not None else None,
                     own_usd=next((round(u_, 4) for u_, q, k in ranked if pool_id(k) == own), None), n=len(ranked),
                     quote_eth=bool(ranked) and ranked[0][2]['currency0'] == NATIVE, fair_usd=round(ref, 4) if ref else None,
                     relay_usd=round(rq['usd'], 4) if rq else None, relay_ms=rq and rq['ms'], floor=floor,
                     aggs={q['name']: round(q['usd'], 4) for q in aggs})
        route['errors'].update({k: v for k, v in errs.items()})
        log('   exit quotes: pool $%s · %s · fair $%.2f' % (route['usd'], ' '.join('%s $%.2f' % (q['name'], q['usd']) for q in aggs) or 'no aggregator', ref))
        if ref <= 0:
            log('   exit sale: nothing quotes %s' % token[:10])
        else:
            margin = float((rl or {}).get('margin', 0.05))
            last = i == tries - 1
            for q in aggs:                                    # best payer first; an aggregator must beat our best pool by `margin`
                if q['usd'] < bar or (pool_usd is not None and q['usd'] <= pool_usd + margin):
                    break
                try:
                    agg_send(q)
                    route['venue'] = q['name']
                    log('   exit route: %s pays $%.2f vs $%s in our best pool (fair $%.2f, %d ms)' % (q['name'], q['usd'], route['usd'], ref, q['ms']))
                    return True, route
                except Exception as e:                        # refused or would revert: the token is still here, next payer
                    route['errors'][q['name']] = str(e)[:120]
                    route['relay_error' if q['name'] == 'relay' else q['name'] + '_error'] = str(e)[:120]
                    route['agg_fails'] = route.get('agg_fails', 0) + 1
                    log('   %s sale failed (%s)' % (q['name'], str(e)[:100]))
            agg_beat = any(q['usd'] >= bar and (pool_usd is None or q['usd'] > pool_usd + margin) for q in aggs)
            # A FAILED AGGREGATOR THAT PAID MORE IS RE-QUOTED, NOT TRADED FOR A WORSE POOL (15 of 185 exits to 2026-10-03: every
            # Relay 'would revert' fell to the pool, -$55 vs its own quotes); the pool takes it then only on the last round
            if pool_usd is not None and pool_usd >= bar and (not agg_beat or last):
                try:
                    pool_send(ranked[0][2])
                    route['venue'] = 'pool'
                    return True, route
                except Exception as e:
                    route['pool_error'] = str(e)[:120]
            if not agg_beat or last:
                log('   exit sale HELD (round %d/%d): best pool $%s, aggregators %s, fair $%.2f - under the %.0f%% floor $%.2f' % (
                    i + 1, tries, route['usd'], route['aggs'] or '-', ref, 100 * floor, bar))
        if i < tries - 1 and go:
            time.sleep(nap)
    route['venue'] = 'held'
    return False, route


def _sale_selftest():
    """FIRM, replayed on fakes: the pool pays 39 % of fair; the best aggregator reverts, the next one pays; with no
    aggregator the drained pool is never sold into; a failing aggregator is re-quoted before a worse pool takes it."""
    k_own = dict(currency0='0x' + '11' * 20, currency1='0x' + '22' * 20, fee=88888, tickSpacing=889, hooks='0x' + '00' * 20)
    tok, n = k_own['currency1'], 872_979 * 10 ** 18
    U = type('C', (), dict(quotes={k_own['currency0']: ('USDG', 6)}))()
    fq = lambda k, zfo, a: int((52.1 * a / n) * (1 if a < n // 100 else 0.386) * 1e6)          # marginal $52.1, full size $20.11
    Q = lambda nm, usd: dict(name=nm, usd=usd, ms=1)
    sent = []

    def send(q):
        if q['name'] == 'relay':
            raise RuntimeError('relay would revert: Error(Insufficient output)')
        sent.append(q['name'])
    ok, r = sell_best(U, None, tok, n, [k_own], 0.0, dict(margin=0.05), True, lambda *a: None, quote_fn=fq,
                      rq_fn=lambda e: [Q('relay', 43.69), Q('kyber', 43.1), Q('lifi', 42.9)], agg_send=send, pool_send=lambda k: sent.append('pool'), nap=0)
    assert ok and sent == ['kyber'] and r['venue'] == 'kyber' and r['tries'] == 1 and r['agg_fails'] == 1, (ok, sent, r)
    sent.clear()                                              # no aggregator at all: the drained pool is never sold into
    ok, r = sell_best(U, None, tok, n, [k_own], 0.0, None, True, lambda *a: None, quote_fn=fq, pool_send=lambda k: sent.append('pool'), nap=0)
    assert not ok and not sent and r['venue'] == 'held' and abs(r['fair_usd'] - 52.1) < 0.5, r
    sent.clear()                                              # every aggregator reverts, the pool pays 90 %: re-quoted, the pool last
    ok, r = sell_best(U, None, tok, n, [k_own], 0.0, dict(margin=0.05), True, lambda *a: None, quote_fn=lambda k, zfo, a: int(52.1 * a / n * 0.9 * 1e6),
                      rq_fn=lambda e: [Q('relay', 50.0)], agg_send=send, pool_send=lambda k: sent.append('pool'), nap=0)
    assert ok and sent == ['pool'] and r['agg_fails'] == 3 and r['tries'] == 3, r
    seen = []                                                 # the slippage widens per round: 1 %, 2 %, 3 %
    sell_best(U, None, tok, n, [k_own], 0.0, dict(margin=0.05, slip_bps=100), True, lambda *a: None, quote_fn=lambda k, zfo, a: int(52.1 * a / n * 0.9 * 1e6),
              rq_fn=lambda e, s_: seen.append(s_) or [Q('relay', 50.0)], agg_send=send, pool_send=lambda k: None, nap=0)
    assert seen == [100, 200, 300], seen
    sent.clear()                                              # a deep pool, no aggregator above it: sold in the pool at once
    ok, r = sell_best(U, None, tok, n, [k_own], 0.0, dict(margin=0.05), True, lambda *a: None, quote_fn=lambda k, zfo, a: int(52.1 * a / n * 0.9 * 1e6),
                      rq_fn=lambda e: [Q('kyber', 40.0)], agg_send=send, pool_send=lambda k: sent.append('pool'), nap=0)
    assert ok and sent == ['pool'] and r['tries'] == 1, r
    me = '0x' + 'ab' * 20                                     # the calldata check
    good = dict(name='kyber', to=AGG_ROUTERS['kyber'], spender=AGG_ROUTERS['kyber'], value=0, data='0xe21fd0e9' + 'ab' * 20)
    agg_check(good, me)
    for bad in (dict(good, to='0x' + '99' * 20), dict(good, value=1), dict(good, data='0xe21fd0e9' + 'cd' * 20)):
        try:
            agg_check(bad, me)
            assert False, bad
        except ValueError:
            pass
    return 9


def best_sale(chain, token, amount, keys, eth_usd, conv=0.003, quote_fn=None):
    """THE EXIT ROUTE (owner 2026-10-01, after ZIP: our 954k ZIP sold into the copy's own pool - drained a second earlier by
    another LP - for $1.06, while ZIP's deep USDG pool would have paid ~$48): the exact-tick quote of selling `amount` of
    `token` in every pool of `keys`, valued in dollars (USDG 1:1; native ETH at eth_usd less the treasury's conversion),
    best first. [(usd, quoted_out, key)]; a pool that cannot quote is left out."""
    q_ = quote_fn or (lambda k, zfo, a: quote(chain, k, zfo, a))
    out = []
    for k in keys:
        zfo = token.lower() == k['currency0'].lower()
        try:
            q = q_(k, zfo, amount)
        except Exception:
            q = None
        if not q:
            continue
        qt = (k['currency1'] if zfo else k['currency0']).lower()
        usd = q / 1e6 if qt == usdg(chain).lower() else q / 1e18 * eth_usd * (1 - conv) if qt == NATIVE else None
        if usd is not None:
            out.append((usd, q, k))
    return sorted(out, key=lambda z: -z[0])


# --------------------------------------------------------------------------- Relay: an exit sale outside our own pools
# RELAY EXITS (owner 2026-10-02): every route_shadow since 10-01 put Relay's quote above our best pool on the losers that
# night (+$31 on five exits; +$50-60 on 91 exits replayed at quote). At the exit the token side is quoted by Relay IN
# PARALLEL with our pools and sold where it pays most. Relay's calldata is foreign, so it is checked before it is signed:
# one swap step, to RELAY_ROUTER only, from our wallet, on this chain, no value; any approve step it asks for must name
# that router. Anything else is refused and the sale falls back to our pools, as does a Relay send that would revert.
RELAY_API = 'api.relay.link'
RELAY_ROUTER = '0xccc88a9d1b4ed6b0eaba998850414b24f1c315be'   # Relay's router on robinhood (from its quote, 2026-10-02)
_RELAY = {}


def _relay_req(method, path, body=None, timeout=2.5):
    """One request on a KEPT-ALIVE https connection (a cold one costs a TLS handshake: 1.8 s vs ~0.2 s warm). The lock
    serialises the quote and the keep-warm ping; a dead connection is reopened once."""
    import http.client, json, threading
    lk = _RELAY.setdefault('lock', threading.Lock())
    with lk:
        for i in range(2):
            c = _RELAY.get('conn') or http.client.HTTPSConnection(RELAY_API, timeout=timeout)
            c.timeout = timeout
            if c.sock is not None:
                c.sock.settimeout(timeout)
            try:
                c.request(method, path, json.dumps(body) if body is not None else None, {'Content-Type': 'application/json'})
                r = c.getresponse()
                d = r.read()
                _RELAY['conn'] = c
                return r.status, d
            except (http.client.HTTPException, OSError):
                c.close()
                _RELAY['conn'] = None
                if i:
                    raise


def relay_warm(every=20):
    """Keep the Relay connection warm in the background (a 72-byte GET), so the exit's quote never pays the handshake."""
    import threading
    if _RELAY.get('warm'):
        return
    _RELAY['warm'] = True

    def go():
        while True:
            try:
                _relay_req('GET', '/', timeout=5)
            except Exception:
                pass
            time.sleep(every)
    threading.Thread(target=go, daemon=True).start()


def relay_check(r, chain_id, addr, router=RELAY_ROUTER):
    """Relay's quote -> (swap tx dict, needs_approve) after checking every step; raises ValueError on anything unexpected."""
    swaps, appr = [], False
    for s in r.get('steps') or []:
        if s.get('id') not in ('approve', 'swap'):
            raise ValueError('relay step %r is not a same-chain swap' % s.get('id'))
        for it in s.get('items') or []:
            d = it.get('data') or {}
            if s.get('id') == 'approve':
                if (d.get('data') or '')[:10] != '0x095ea7b3' or '0x' + d['data'][34:74].lower() != router:
                    raise ValueError('relay approve names another spender: %s' % (d.get('data') or '')[:74])
                appr = True
            elif s.get('id') == 'swap':
                swaps.append(d)
            else:
                raise ValueError('relay step %r is not a same-chain swap' % s.get('id'))
    if len(swaps) != 1:
        raise ValueError('relay quote has %d swap txs' % len(swaps))
    t = swaps[0]
    if (t.get('to') or '').lower() != router or (t.get('from') or '').lower() != addr.lower() or int(t.get('chainId') or 0) != chain_id \
            or int(t.get('value') or 0) != 0 or not (t.get('data') or '').startswith('0x'):
        raise ValueError('relay swap tx refused: to %s from %s chain %s value %s' % (t.get('to'), t.get('from'), t.get('chainId'), t.get('value')))
    return t, appr


def relay_quote(chain, addr, token, amount, slip_bps=100, timeout=2.5):
    """Relay's same-chain quote for selling `amount` raw of `token` into USDG, checked. -> dict(usd: what arrives net of
    Relay's fee, min_out raw, gas_usd, tx, approve, ms) or None (no quote, a refused quote, an error)."""
    import json
    t0 = time.time()
    try:
        st, d = _relay_req('POST', '/quote', dict(user=addr, originChainId=chain.chain_id, destinationChainId=chain.chain_id, originCurrency=token,
                                                  destinationCurrency=usdg(chain), amount=str(int(amount)), tradeType='EXACT_INPUT',
                                                  slippageTolerance=str(int(slip_bps))), timeout)
        if st != 200:
            return None
        r = json.loads(d)
        tx_, appr = relay_check(r, chain.chain_id, addr)
        out = r['details']['currencyOut']
        if (out.get('currency') or {}).get('address', '').lower() != usdg(chain).lower():
            return None
        return dict(usd=int(out['amount']) / 1e6, min_out=int(out['minimumAmount']), gas_usd=float(((r.get('fees') or {}).get('gas') or {}).get('amountUsd') or 0),
                    tx=tx_, approve=appr, ms=round((time.time() - t0) * 1e3))
    except Exception as e:
        _ev('error', action='relay', at='quote', why=str(e)[:160])
        return None


def relay_sell(chain, ac, q, token, amount, go, log=print, ctx=None):
    """Send Relay's checked swap tx (approving its router for the token first, max, once per token). The trace carries the
    quote vs what arrived. Raises when it would revert or fails: the token is then still in the wallet."""
    from eth_abi import encode as enc
    allow = int(chain.node.call(token, _sel('allowance(address,address)') + enc(['address', 'address'], [ac.address, RELAY_ROUTER]).hex()), 16)
    if allow < amount:
        _send(chain, ac, token, _sel('approve(address,uint256)') + enc(['address', 'uint256'], [RELAY_ROUTER, MAX256]).hex(), go, log,
              'approve', dict(ctx or {}, token=token, spender=RELAY_ROUTER))
    u = usdg(chain)
    b0 = balance_net(chain, u, ac.address) if go else 0

    def fill(rc):
        got = balance_net(chain, u, ac.address) - b0
        return dict(got_out=got, slip_bps=round((q['usd'] * 1e6 - got) / (q['usd'] * 1e6) * 1e4, 1) if q['usd'] else None)
    c = dict(ctx or {}, venue='relay', token_in=token, amount_in=int(amount), quote_out=round(q['usd'] * 1e6), min_out=q['min_out'])
    return _send(chain, ac, RELAY_ROUTER, q['tx']['data'], go, log, 'relay', c, after=fill)


# --------------------------------------------------------------------------- every aggregator, one quote round
# EVERY AGGREGATOR (owner 2026-10-03, after FIRM: "there is way more aggregators than relay ... find multiple quotes and
# choose the best"). Keyless on robinhood (4663), probed 2026-10-03 on 100k FIRM -> USDG: KyberSwap $6.5425, LI.FI $6.5415
# (its own 'nordstern' route), ParaSwap $6.2288, Relay; OpenOcean wants a key, 0x and 1inch too (not wired). Each answer is
# normalised to dict(name, usd, min_out, to, data, spender, ms) and CHECKED before it is signed: the router is the
# aggregator's known one (AGG_ROUTERS), no value, our address inside the calldata (the receiver), the spender the router.
AGG_ROUTERS = {'kyber': '0x6131b5fae19ea4f9d964eac0408e4408b66337b5', 'lifi': '0xb477751b76cf82d00a686a1232f5fcd772414af3',
               'paraswap': '0x6a000f20005980200259b80c5102003040001068', 'relay': RELAY_ROUTER}
AGGS = ('relay', 'kyber', 'lifi', 'paraswap')
_DEC = {}


def _http(url, body=None, timeout=3.0):
    import json, urllib.request
    h = {'Content-Type': 'application/json', 'User-Agent': 'lp-copy-desk/1', 'x-client-id': 'lp-copy-desk'}
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, h)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _decimals(chain, token):
    if token not in _DEC:
        _DEC[token] = int(chain.node.call(token, '0x313ce567'), 16)
    return _DEC[token]


def _agg_kyber(chain, addr, token, amount, slip_bps, timeout):
    base = 'https://aggregator-api.kyberswap.com/robinhood/api/v1/'
    r = _http(base + 'routes?tokenIn=%s&tokenOut=%s&amountIn=%d' % (token, usdg(chain), amount), timeout=timeout)['data']
    b = _http(base + 'route/build', dict(routeSummary=r['routeSummary'], sender=addr, recipient=addr, slippageTolerance=int(slip_bps)), timeout=timeout)['data']
    out = int(b['amountOut'])
    return dict(usd=out / 1e6, min_out=int(out * (1 - slip_bps / 1e4)), to=b['routerAddress'], data=b['data'], spender=b['routerAddress'], value=int(b.get('transactionValue') or 0))


def _agg_lifi(chain, addr, token, amount, slip_bps, timeout):
    r = _http('https://li.quest/v1/quote?fromChain=4663&toChain=4663&fromToken=%s&toToken=%s&fromAmount=%d&fromAddress=%s&slippage=%s&maxPriceImpact=0.95'
              % (token, usdg(chain), amount, addr, slip_bps / 1e4), timeout=timeout)
    t = r['transactionRequest']
    return dict(usd=int(r['estimate']['toAmount']) / 1e6, min_out=int(r['estimate']['toAmountMin']), to=t['to'], data=t['data'],
                spender=r['estimate'].get('approvalAddress') or t['to'], value=int(t.get('value') or '0x0', 16), tool=r.get('tool'))


def _agg_paraswap(chain, addr, token, amount, slip_bps, timeout):
    u = usdg(chain)
    pr = _http('https://api.paraswap.io/prices?srcToken=%s&destToken=%s&amount=%d&srcDecimals=%d&destDecimals=6&side=SELL&network=4663&version=6.2'
               '&ignoreBadUsdPrice=true' % (token, u, amount, _decimals(chain, token)), timeout=timeout)['priceRoute']
    t = _http('https://api.paraswap.io/transactions/4663?ignoreChecks=true&ignoreGasEstimate=true',
              dict(srcToken=token, destToken=u, srcAmount=str(amount), srcDecimals=_decimals(chain, token), destDecimals=6, priceRoute=pr,
                   slippage=int(slip_bps), userAddress=addr, receiver=addr), timeout=timeout)
    out = int(pr['destAmount'])
    return dict(usd=out / 1e6, min_out=int(out * (1 - slip_bps / 1e4)), to=t['to'], data=t['data'], spender=pr['tokenTransferProxy'], value=int(t.get('value') or 0))


def _agg_relay(chain, addr, token, amount, slip_bps, timeout):
    q = relay_quote(chain, addr, token, amount, slip_bps, timeout)     # relay_check'ed already
    if not q:
        return None
    return dict(usd=q['usd'], min_out=q['min_out'], to=q['tx']['to'], data=q['tx']['data'], spender=RELAY_ROUTER, value=0, gas_usd=q['gas_usd'])


def agg_check(q, addr):
    """Refuse anything but a plain sale through the aggregator's own router, paying us."""
    r = AGG_ROUTERS.get(q['name'])
    if not r or q['to'].lower() != r or q['spender'].lower() not in (r, AGG_ROUTERS['paraswap']) or q.get('value'):
        raise ValueError('%s quote refused: to %s spender %s value %s' % (q['name'], q['to'], q['spender'], q.get('value')))
    if addr[2:].lower() not in q['data'].lower():
        raise ValueError('%s quote refused: our address is not in its calldata' % q['name'])
    return q


def agg_quotes(chain, addr, token, amount, slip_bps=100, timeout=3.0, names=AGGS, errors=None):
    """Every aggregator in parallel -> checked quotes, best first. A failed or refused one lands in `errors`."""
    import threading
    fn = dict(relay=_agg_relay, kyber=_agg_kyber, lifi=_agg_lifi, paraswap=_agg_paraswap)
    out, errors = [], errors if errors is not None else {}

    def one(n):
        t0 = time.time()
        try:
            q = fn[n](chain, addr, token, int(amount), slip_bps, timeout)
            if q:
                q.update(name=n, ms=round((time.time() - t0) * 1e3))
                out.append(agg_check(q, addr))
        except Exception as e:
            errors[n] = str(e)[:120]
    ths = [threading.Thread(target=one, args=(n,), daemon=True) for n in names if n in fn]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout * 2 + 1)
    return sorted(out, key=lambda q: -q['usd'])


def agg_sell(chain, ac, q, token, amount, go, log=print, ctx=None):
    """Approve the aggregator's spender (max, once per token) and send its checked calldata. Raises on a revert."""
    from eth_abi import encode as enc
    allow = int(chain.node.call(token, _sel('allowance(address,address)') + enc(['address', 'address'], [ac.address, q['spender']]).hex()), 16)
    if allow < amount:
        _send(chain, ac, token, _sel('approve(address,uint256)') + enc(['address', 'uint256'], [q['spender'], MAX256]).hex(), go, log,
              'approve', dict(ctx or {}, token=token, spender=q['spender']))
    u = usdg(chain)
    b0 = balance_net(chain, u, ac.address) if go else 0

    def fill(rc):
        got = balance_net(chain, u, ac.address) - b0
        return dict(got_out=got, slip_bps=round((q['usd'] * 1e6 - got) / (q['usd'] * 1e6) * 1e4, 1) if q['usd'] else None)
    c = dict(ctx or {}, venue=q['name'], token_in=token, amount_in=int(amount), quote_out=round(q['usd'] * 1e6), min_out=q['min_out'])
    return _send(chain, ac, q['to'], q['data'], go, log, q['name'], c, after=fill)


def pick_sale(pool_usd, rq, margin):
    """'relay' when Relay's quote beats our best pool by more than `margin` dollars (plus its approve's gas when the router
    is not approved yet), else 'pool'. No pool quote (None) and a Relay quote = relay."""
    if not rq:
        return 'pool'
    bar = (pool_usd if pool_usd is not None else 0.0) + margin + (rq['gas_usd'] if rq.get('approve') else 0.0)
    return 'relay' if rq['usd'] > bar else 'pool'


def sell(chain, ac, key, token, go, log=print, ctx=None, fee=None, amount=None):
    """Sell `amount` of `token` (default: the wallet's whole balance, capped at it) into the pool's quote on `key`'s pool
    (nothing to sell = no send). `fee` for a dynamic-fee (hooked) pool, whose key carries a flag instead of a fee."""
    amt = balance(chain, token, ac.address)
    amt = min(amt, int(amount)) if amount is not None else amt
    if amt <= 0:
        return None
    approve(chain, ac, token, go, log, ctx)
    return swap(chain, ac, key, token, amt, go, log, ctx, fee=fee)


# --------------------------------------------------------------------------- the undercut: our own pool, quote only
def quote_only_ticks(sp, ts, usdg_is_c0, rng=1.5):
    """(tickLower, tickUpper) of a position holding ONLY the quote, on the side the price must cross: above the price
    when USDG is currency0 (a range above the tick is all token0), below it when USDG is currency1. Aligned to ts,
    the near edge one spacing clear of the current tick, the far edge at price x rng (or / rng)."""
    import math
    t = math.log((sp / Q96) ** 2) / math.log(1.0001)
    span = math.log(rng) / math.log(1.0001)
    if usdg_is_c0:
        lo = (math.floor(t / ts) + 1) * ts
        return lo, max(lo + ts, int(math.ceil((t + span) / ts) * ts))
    hi = math.floor(t / ts) * ts
    if hi >= t:
        hi -= ts
    return min(hi - ts, int(math.floor((t - span) / ts) * ts)), hi


def mint_quote_only(chain, ac, key, tl, tu, size_raw, go, log=print, ctx=None):
    """Mint `size_raw` USDG as a one-sided position on [tl, tu] of `key`. No swap. Returns (tokenId, liquidity)."""
    from core import amm
    u = quote_of(chain, key)
    uc0 = key['currency0'].lower() == u
    cx = dict(ctx or {}, leg='undercut')
    approve(chain, ac, u, go, log, cx)
    sp = slot0(chain, key)[0] if go else (ctx or {}).get('sp')
    sa, sb = amm.sqrt_at_tick(tl), amm.sqrt_at_tick(tu)
    a0, a1 = (size_raw, 0) if uc0 else (0, size_raw)
    liq = int(liquidity_for(sp, sa, sb, a0, a1) * (1 - float(cfg(chain)['lp_slippage'])))
    if liq <= 0:
        _ev('error', action='mint', at='liquidity', why='quote-only range holds no liquidity at this price', **cx)
        raise RuntimeError('quote-only range [%d, %d] is not one-sided at the current price' % (tl, tu))
    if not go:
        log('   dry mint     quote-only L %d on %d..%d' % (liq, tl, tu))
        return None, liq
    rc = _send(chain, ac, cfg(chain)['positionmanager'], mint_calldata(key, tl, tu, liq, a0, a1, ac.address, deadline(chain)),
               go, log, 'mint', dict(cx, ticks=[tl, tu]), after=lambda rc: dict(token_id=token_id_of(chain, rc), liquidity=liq),
               value=a0 if is_native(key['currency0']) else 0)
    return token_id_of(chain, rc), liq


# --------------------------------------------------------------------------- the own hook (contracts/src/VolFeeHook.sol)
DYNAMIC_FEE = 0x800000
HOOK_BITS, HOOK_FLAGS = 0x3FFF, 0x0080          # BEFORE_SWAP only: every other callback bit must be 0


def hook_cfg(chain):
    return chain.cfg['lp']['hook']


def hook_initcode(chain, desk):
    """Creation code + constructor args (manager, desk, base, surge, desk fee, trigger)."""
    import json
    h = hook_cfg(chain)
    art = json.load(open(h['artifact']))
    code = art['bytecode']['object']
    code = code[2:] if code.startswith('0x') else code
    return bytes.fromhex(code) + encode(['address', 'address', 'uint24', 'uint24', 'uint24', 'uint256'],
                                        [chain.pm, desk, h['base_fee'], h['surge_fee'], h['desk_fee'], h['trigger_bps']])


def create2_address(deployer, salt, init_hash):
    from eth_hash.auto import keccak
    return '0x' + keccak(b'\xff' + bytes.fromhex(deployer[2:]) + salt + init_hash)[12:].hex()


def mine_salt(deployer, initcode, start=0):
    """(salt, address) whose low 14 bits are exactly BEFORE_SWAP. ~16k keccaks on average."""
    from eth_hash.auto import keccak
    ih = keccak(initcode)
    pre = b'\xff' + bytes.fromhex(deployer[2:])
    i = start
    while True:
        salt = i.to_bytes(32, 'big')
        a = keccak(pre + salt + ih)[12:]
        if int.from_bytes(a[-2:], 'big') & HOOK_BITS == HOOK_FLAGS:
            return salt, '0x' + a.hex()
        i += 1


def hook_key(chain, hook, token, ts):
    u = usdg(chain)
    c0, c1 = sorted([u, token.lower()], key=lambda x: int(x, 16))
    return dict(currency0=c0, currency1=c1, fee=DYNAMIC_FEE, tickSpacing=int(ts), hooks=hook)


def deploy_hook(chain, ac, desk, go, log=print):
    """CREATE2 through the chain's standard deployer at a mined salt. Returns the hook address (the predicted
    one when dry). Idempotent: an address that already holds code is returned without a send."""
    h = hook_cfg(chain)
    ic = hook_initcode(chain, desk)
    salt, addr = mine_salt(h['create2'], ic)
    if (len(chain.node.rpc('eth_getCode', [addr, 'latest'])) - 2) // 2 > 0:
        log('   hook already at %s' % addr)
        return addr
    _send(chain, ac, h['create2'], '0x' + salt.hex() + ic.hex(), go, log, 'hook_deploy', dict(hook=addr, desk=desk))
    return addr


def initialize(chain, ac, key, sqrt_price, go, log=print):
    """PoolManager.initialize(key, sqrtPriceX96) - a new send: nothing else in the engine creates a pool."""
    data = _sel('initialize((address,address,uint24,int24,address),uint160)') + encode([KEY_T, 'uint160'], [key_tuple(key), sqrt_price]).hex()
    return _send(chain, ac, chain.pm, data, go, log, 'initialize', dict(pool=pool_id(key), hook=key['hooks']))


def _selftest():
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
    from eth_abi import decode
    from core import amm
    A(_sel('modifyLiquidities(bytes,uint256)') == '0xdd46508f', 'the selector found in the robinhood PM bytecode')
    # entry cost: 100 quote in at mid price 2 token1 per token0 (quote = token0) gives 200 token at no cost; 190 = 5% of 100 in
    sp2 = int((2 ** 0.5) * Q96)
    A(abs(cost_frac(100, 200, sp2, True, 100)) < 1e-9, 'a swap at mid costs nothing')
    A(abs(cost_frac(100, 190, sp2, True, 100) - 0.05) < 1e-9, '5 of 100 lost to fee + impact = 5%%')
    A(abs(cost_frac(50, 95, sp2, True, 100) - 0.025) < 1e-9, 'half the copy swapped at 5%% = 2.5%% of the copy')
    A(abs(cost_frac(100, 50 * 0.95, sp2, False, 100) - 0.05) < 1e-9, 'quote = token1: the token is valued x price')
    A(cost_frac(0, 0, sp2, True, 100) == 0.0, 'nothing swapped, nothing paid')
    A(_sel('execute(bytes,bytes[],uint256)') == '0x3593564c', 'the UR selector')
    key = dict(currency0='0x' + '11' * 20, currency1='0x' + '22' * 20, fee=10000, tickSpacing=200, hooks='0x' + '00' * 20)
    from eth_hash.auto import keccak
    A(pool_id(key) == '0x' + keccak(encode([KEY_T], [key_tuple(key)])).hex(), 'pool id = keccak(abi.encode(PoolKey))')
    # swap calldata round-trips
    d = swap_calldata(key, True, 5, 4, 99)
    cmds, inputs, dl = decode(['bytes', 'bytes[]', 'uint256'], bytes.fromhex(d[10:]))
    A(cmds == bytes([V4_SWAP]) and dl == 99)
    acts, prm = decode(['bytes', 'bytes[]'], inputs[0])
    A(acts == bytes([6, 12, 15]), acts)
    (p,) = decode(['(%s,bool,uint128,uint128,uint256,bytes)' % KEY_T], prm[0])
    A(p[1] is True and p[2] == 5 and p[3] == 4 and p[4] == 0, p)
    A(decode(['address', 'uint256'], prm[1])[0] == key['currency0'] and decode(['address', 'uint256'], prm[2])[0] == key['currency1'],
      'settle the input, take the output')
    # mint + exit calldata
    m = mint_calldata(key, -400, 600, 10 ** 9, 7, 8, '0x' + '33' * 20, 1)
    ud, _ = decode(['bytes', 'uint256'], bytes.fromhex(m[10:]))
    acts, prm = decode(['bytes', 'bytes[]'], ud)
    A(acts == bytes([2, 13]))
    mp = decode([KEY_T, 'int24', 'int24', 'uint256', 'uint128', 'uint128', 'address', 'bytes'], prm[0])
    A(mp[1] == -400 and mp[2] == 600 and mp[3] == 10 ** 9 and mp[4] == 7 and mp[5] == 8, mp)
    nk = dict(key, currency0=NATIVE)
    ud, _ = decode(['bytes', 'uint256'], bytes.fromhex(mint_calldata(nk, -400, 600, 10 ** 9, 7, 8, '0x' + '33' * 20, 1)[10:]))
    acts, prm = decode(['bytes', 'bytes[]'], ud)
    A(acts == bytes([2, 13, 0x14]) and decode(['address', 'address'], prm[2]) == (NATIVE, '0x' + '33' * 20),
      'a native-ETH mint sweeps the unused value back to the owner')
    A(is_native(NATIVE) and not is_native(key['currency0']), 'native test')
    x = exit_calldata(42, 10 ** 9, 1, 2, key, '0x' + '33' * 20, 1)
    ud, _ = decode(['bytes', 'uint256'], bytes.fromhex(x[10:]))
    acts, prm = decode(['bytes', 'bytes[]'], ud)
    A(acts == bytes([1, 3, 17]))
    A(decode(['uint256', 'uint256', 'uint128', 'uint128', 'bytes'], prm[0])[:4] == (42, 10 ** 9, 1, 2))
    # liquidity maths: L from amounts, amounts from L, consistent in range
    sp = amm.sqrt_at_tick(100)
    sa, sb = amm.sqrt_at_tick(-6800), amm.sqrt_at_tick(7000)
    L = liquidity_for(sp, sa, sb, 10 ** 18, 10 ** 18)
    a0, a1 = amounts_for(sp, sa, sb, L, up=True)
    A(a0 <= 10 ** 18 + 1 and a1 <= 10 ** 18 + 1 and max(a0, a1) > 10 ** 18 * 0.999, 'binding side spent: %d %d' % (a0, a1))
    A(amounts_for(sa - 1, sa, sb, L)[1] == 0 and amounts_for(sb + 1, sa, sb, L)[0] == 0, 'out of range = one side')
    # fees-only collect and increase: the right actions, zero liquidity on the collect
    ud, _ = decode(['bytes', 'uint256'], bytes.fromhex(collect_calldata(42, key, '0x' + '33' * 20, 1)[10:]))
    acts, prm = decode(['bytes', 'bytes[]'], ud)
    A(acts == bytes([1, 17]) and decode(['uint256', 'uint256', 'uint128', 'uint128', 'bytes'], prm[0])[:2] == (42, 0), 'collect = DECREASE 0 + TAKE_PAIR')
    ud, _ = decode(['bytes', 'uint256'], bytes.fromhex(increase_calldata(42, 7, 8, 9, key, 1)[10:]))
    acts, prm = decode(['bytes', 'bytes[]'], ud)
    A(acts == bytes([0, 13]) and decode(['uint256', 'uint256', 'uint128', 'uint128', 'bytes'], prm[0])[:4] == (42, 7, 8, 9), 'increase = INCREASE + SETTLE_PAIR')
    # the swap share: 1/2 for a symmetric range, the whole size on one side when the price sits at an edge
    mid = amm.sqrt_at_tick(0)
    A(abs(token_share(mid, amm.sqrt_at_tick(-6932), amm.sqrt_at_tick(6932), True) - 0.5) < 1e-3, 'symmetric: half')
    lo = amm.sqrt_at_tick(-6932)
    A(token_share(lo, lo, amm.sqrt_at_tick(6932), True) < 1e-3 and token_share(lo, lo, amm.sqrt_at_tick(6932), False) > 0.999,
      'at the lower edge the position is all currency0: no swap when USDG is currency0, all of it when it is currency1')
    A(token_share(mid, amm.sqrt_at_tick(-6932), amm.sqrt_at_tick(6932), True) ==
      1 - token_share(mid, amm.sqrt_at_tick(-6932), amm.sqrt_at_tick(6932), False), 'the two orientations complement')
    # the hook address: mined so ONLY the BEFORE_SWAP bit is set, and CREATE2 maths agrees with the miner
    ic = b'\x60\x00' * 8
    salt, addr = mine_salt('0x4e59b44847b379578588920cA78FbF26c0B4956C', ic)
    from eth_hash.auto import keccak as _k
    A(int(addr, 16) & HOOK_BITS == HOOK_FLAGS, addr)
    A(create2_address('0x4e59b44847b379578588920cA78FbF26c0B4956C', salt, _k(ic)) == addr, 'create2 address')
    class HC:
        quotes = {'0x5fc5360d0400a0fd4f2af552add042d716f1d168': ('USDG', 1.0, 6)}
    k = hook_key(HC, addr, '0x0000000000000000000000000000000000000001', 60)
    A(k['currency0'] == '0x0000000000000000000000000000000000000001' and k['fee'] == DYNAMIC_FEE, 'currencies sorted, dynamic fee')
    # quote-only ticks: USDG c0 -> strictly ABOVE the price (all token0); USDG c1 -> strictly BELOW (all token1)
    spq = amm.sqrt_at_tick(1234)
    lo, hi = quote_only_ticks(spq, 60, True)
    A(lo > 1234 and lo % 60 == 0 and hi > lo and amounts_for(spq, amm.sqrt_at_tick(lo), amm.sqrt_at_tick(hi), 10 ** 18)[1] == 0, (lo, hi))
    lo, hi = quote_only_ticks(spq, 60, False)
    A(hi <= 1234 and hi % 60 == 0 and lo < hi and amounts_for(spq, amm.sqrt_at_tick(lo), amm.sqrt_at_tick(hi), 10 ** 18)[0] == 0, (lo, hi))
    # receipt -> tokenId
    class C:
        cfg = {'lp': {'positionmanager': '0x' + 'aa' * 20}}
    rc = {'logs': [{'address': '0x' + 'aa' * 20, 'topics': [TRANSFER, '0x' + '0' * 64, '0x' + '0' * 24 + '33' * 20, '0x%064x' % 3205309]}]}
    A(token_id_of(C, rc) == 3205309)
    # THE EXIT ROUTE: the drained own pool pays $1, a deep USDG pool $48, an ETH pool 0.02 ETH at $2,500 less 0.3% -> ETH wins;
    # a pool that cannot quote drops out; an unknown quote currency is never priced
    class U:
        cfg = {'lp': {'quote': 'USDG'}}
        quotes = {'0x' + '55' * 20: ('USDG', 6)}
    tok = '0x' + 'cc' * 20
    own = dict(currency0='0x' + '55' * 20, currency1=tok, fee=200000, tickSpacing=200, hooks='0x' + '0' * 40)
    deep = dict(own, fee=70000)
    ethp = dict(currency0=NATIVE, currency1=tok, fee=10000, tickSpacing=200, hooks='0x' + '0' * 40)
    dead = dict(own, fee=3000)
    odd = dict(currency0='0x' + '11' * 20, currency1=tok, fee=3000, tickSpacing=60, hooks='0x' + '0' * 40)
    outs = {200000: 1_060_000, 70000: 48_130_000, 10000: 2 * 10 ** 16, 3000: None}
    qf = lambda k, zfo, a: (5 * 10 ** 18 if k is odd else outs[k['fee']]) if not zfo else None
    ranked = best_sale(U, tok, 954 * 10 ** 21, [own, deep, ethp, dead, odd], 2500.0, quote_fn=qf)
    A([round(u, 2) for u, q, k in ranked] == [49.85, 48.13, 1.06], [round(u, 2) for u, q, k in ranked])
    A(ranked[0][2] is ethp and ranked[-1][2] is own, 'the ETH pool first, the drained own pool last')
    n[0] += _sale_selftest()                                  # FIRM: never sold under the floor, a stale Relay quote re-quoted
    print('lp exec selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
