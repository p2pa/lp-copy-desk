"""EVERY EVENT THIS ENGINE LISTENS TO, in one place. `./hookbot <chain> events` prints this table plus
which strategy is woken by what, so "what do we subscribe to" is a command, not an archaeology dig.

Two layers, and the split is the point:

  CORE events keep the shared STATE true. Core subscribes to them for every chain, applies them to the
  StateCache (or the chain's header cache) and never asks a strategy's permission. A strategy cannot opt
  out of them, because every strategy prices off the same state.

  SIGNALS are what a strategy is WOKEN by. A strategy declares the signals it cares about in its own
  strategies/<name>/strategy.toml; a waiver can add one it needs (the calendar needs `l1`). Pool-scoped
  signals (swap / modify / protocol_fee) only wake a strategy when the pool is one it routes through.

THE PARITY RULE. The live desks poll every 3s: each poll reads the head header (block, basefee, l1) and
the full state of every pool, then runs EVERY strategy. The websocket engine must hand strategies at least
that. So `tick` fires every chain.poll_s and wakes every strategy, and on it core re-reads the header and
slot0+liquidity of every hot pool (and counts how far the socket-built cache had drifted from that read -
the standing proof the socket is not lying). Events then add everything faster than a poll can see.

What a Swap log cannot carry, and how core covers it:
  tick bitmaps / liquidityNet  -> refetched on ModifyLiquidity / Mint / Burn, and when a swap walks the
                                  tick to the edge of the fetched window (the pricer refuses outside it)
  dynamic lpFee                -> no PoolManager event exists; the tick's slot0 re-read carries it
  l1 block (calendar)          -> NOT in newHeads on 4663 (checked on all four sockets, 2026-09-19): read
                                  from eth_getBlockByNumber once per tick, exactly where the live desk reads it
  a reorged log (removed=true) -> treated as `modify`: refetch that pool rather than trust the log
"""

# -- v4 PoolManager logs (pool id is topic1) ----------------------------------------------------------
SWAP = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'
MODIFY_LIQUIDITY = '0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec'
PROTOCOL_FEE = '0xe9c42593e71f84403b84352cd168d693e2c9fcd1fdbcc3feb21d92b43e6696f9'  # ProtocolFeeUpdated
# Initialize(id, currency0, currency1, fee, tickSpacing, hooks, sqrtPriceX96, tick) - a pool is born. NOT
# in PM_TOPICS: it changes no state core holds, and the runner has no use for a pool it does not target.
# discovery/watch.py subscribes to it alone, because a fee-waiver pool can be worth $14 and live 80 minutes
# (docs/FRESH-SENDER-TOLL.md) and a 20-minute rotation finds it, when it finds it, with a third of its life gone.
INITIALIZE = '0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'
# -- v3 pools speak for THEMSELVES: their own address, their own topics --------------------------------
V3_SWAP = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
V3_BURN = '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c'
V3_MINT = '0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde'
ALG_BURN = '0x932214d4a69c27c086643126ed97c32681da179064c678836c173f99bd13ca01'   # Algebra Integral 1.2 Burn(..., uint24 pluginFee)

PM_TOPICS = (SWAP, MODIFY_LIQUIDITY, PROTOCOL_FEE)
# Pancake v3 and Giga ('extended') v3 pools (robinhood): the v3 Swap plus two protocol-fee words. The first
# five data words are v3's, so decode_v3_swap reads them unchanged. Mint/Burn share v3's topics.
PANCAKE_SWAP = '0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83'
# v2 pairs: Sync carries BOTH reserves after every swap/mint/burn - the whole state, so it is applied as a swap
# (sp/L carry reserve0/reserve1, core/tickfetch). Uniswap v2 packs uint112s, Solidly/Aerodrome uint256s.
V2_SYNC = '0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1'
SOL_SYNC = '0xcf2aa50876cdfbb541206f89af0ee78d44a2abf8d328e37fa4917f982149848a'
V3_TOPICS = (V3_SWAP, V3_MINT, V3_BURN, ALG_BURN, PANCAKE_SWAP, V2_SYNC, SOL_SYNC)

# The signal vocabulary a strategy may subscribe to. POOL_SIGNALS are scoped to the strategy's own pools.
POOL_SIGNALS = ('swap', 'modify', 'protocol_fee')
SIGNALS = POOL_SIGNALS + ('head', 'l1', 'tick')

CATALOG = [
    # name          layer    source                                  what core does with it
    ('swap',         'core', 'PoolManager Swap (topic1 = poolId)',   'slot0 sp/L/tick from the log; fee word ignored'),
    ('swap',         'core', 'v3 pool Swap (by address)',            'same, for v3 twins'),
    ('modify',       'core', 'PoolManager ModifyLiquidity',          'mark pool dirty -> refetch bitmaps + liquidityNet'),
    ('modify',       'core', 'v3 pool Mint / Burn (by address)',     'same, for v3 twins'),
    ('modify',       'core', 'swap to the edge of the tick window',  'mark pool dirty (derived, not subscribed)'),
    ('modify',       'core', 'any log with removed=true (reorg)',    'mark pool dirty (derived, not subscribed)'),
    ('protocol_fee', 'core', 'PoolManager ProtocolFeeUpdated',       'pool pf pair from the log'),
    ('init',         'watch', 'PoolManager Initialize',               'discovery/watch.py only: a new pool under a known hook family'),
    ('head',         'core', 'eth_subscribe newHeads',               'head number + basefee (gas_usd reads it, no RPC)'),
    ('l1',           'signal', 'header read on tick, l1BlockNumber changed', 'chain l1 (the calendar hashes it)'),
    ('tick',         'signal', 'clock, every chain.poll_s',          'header + slot0 re-read, drift count, health, wake ALL'),
]


def _i(h):
    return int(h, 16)


def _signed(v, bits):
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def _i128(v):
    """An int128 event word. The ABI sign-extends it to 256 bits (what a node delivers: a negative amount read
    as 128-bit came out ~2^128 - the first undercut paper case showed $1.2e71 of volume, 2026-09-24); a word
    already in 128-bit two's complement is accepted too."""
    v = _signed(v, 256)
    return v - (1 << 128) if v >= (1 << 127) else v


def _meta(log):
    return dict(block=_i(log['blockNumber']) if log.get('blockNumber') else None, tx=log.get('transactionHash'),
                li=_i(log['logIndex']) if log.get('logIndex') else None)


def decode_swap(log):
    """A v4 Swap log -> the pool's new state. Data words: amount0, amount1 (int128), sqrtPriceX96,
    liquidity, tick (int24), fee."""
    tp = log.get('topics') or []
    if not tp or tp[0].lower() != SWAP or len(tp) < 2:
        return None
    d = log.get('data', '0x')[2:]
    if len(d) < 64 * 6:
        return None
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    return dict(kind='swap', pool=tp[1].lower(), sender=('0x' + tp[2][-40:]).lower() if len(tp) > 2 else None,
                amount0=_i128(_i(w[0])), amount1=_i128(_i(w[1])),
                sp=_i(w[2]), L=_i(w[3]), tick=_signed(_i(w[4]), 256), fee=_i(w[5]), **_meta(log))


def decode_v3_swap(log):
    """A v3 pool's own Swap. Five data words; the pool is the log ADDRESS (a v3 pool has no poolId)."""
    tp = log.get('topics') or []
    if not tp or tp[0].lower() not in (V3_SWAP, PANCAKE_SWAP):
        return None
    d = log.get('data', '0x')[2:]
    if len(d) < 64 * 5:
        return None
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    return dict(kind='swap', pool=(log.get('address') or '').lower(), v=3,
                amount0=_signed(_i(w[0]), 256), amount1=_signed(_i(w[1]), 256),
                sp=_i(w[2]), L=_i(w[3]), tick=_signed(_i(w[4]), 256), fee=None, **_meta(log))


def decode_v2_sync(log):
    tp = log.get('topics') or []
    if not tp or tp[0].lower() not in (V2_SYNC, SOL_SYNC):
        return None
    d = log.get('data', '0x')[2:]
    if len(d) < 128:
        return None
    return dict(kind='swap', pool=(log.get('address') or '').lower(), v=2, sp=_i(d[0:64]), L=_i(d[64:128]), tick=0,
                fee=None, **_meta(log))


def decode_modify(log):
    """ModifyLiquidity -> the pool's liquidity change: (tl, tu, dL) from data words tickLower, tickUpper, liquidityDelta,
    so core/feed applies it in place. None fields when the data is short: the pool is then refetched as before."""
    tp = log.get('topics') or []
    if not tp or tp[0].lower() != MODIFY_LIQUIDITY or len(tp) < 2:
        return None
    d = log.get('data', '0x')[2:]
    tl = tu = dl = None
    if len(d) >= 64 * 3:
        tl, tu, dl = _signed(_i(d[0:64]), 256), _signed(_i(d[64:128]), 256), _signed(_i(d[128:192]), 256)
    return dict(kind='modify', pool=tp[1].lower(), tl=tl, tu=tu, dl=dl, **_meta(log))


def decode_v3_liquidity(log):
    """v3 / Algebra Mint or Burn -> (tl, tu, dL). Both carry the ticks as topics 2 and 3; Mint's data is
    (sender, amount, amount0, amount1), Burn's (amount, amount0, amount1[, pluginFee]). Burn's dL is negative."""
    tp = log.get('topics') or []
    if not tp or tp[0].lower() not in (V3_MINT, V3_BURN, ALG_BURN):
        return None
    d = log.get('data', '0x')[2:]
    tl = tu = dl = None
    if len(tp) >= 4:
        tl, tu = _signed(_i(tp[2]), 256), _signed(_i(tp[3]), 256)
        if tp[0].lower() == V3_MINT and len(d) >= 128:
            dl = _i(d[64:128])
        elif tp[0].lower() != V3_MINT and len(d) >= 64:
            dl = -_i(d[0:64])
    return dict(kind='modify', pool=(log.get('address') or '').lower(), v=3, tl=tl, tu=tu, dl=dl, **_meta(log))


def decode_protocol_fee(log):
    """ProtocolFeeUpdated(PoolId indexed id, uint24 protocolFee) -> the (zeroForOne, oneForZero) pair,
    packed 12 bits each exactly as slot0 packs it."""
    tp = log.get('topics') or []
    if not tp or tp[0].lower() != PROTOCOL_FEE or len(tp) < 2:
        return None
    raw = _i((log.get('data') or '0x0')[2:66] or '0')
    return dict(kind='protocol_fee', pool=tp[1].lower(), pf=(raw & 0xfff, (raw >> 12) & 0xfff), **_meta(log))


def decode_init(log):
    """Initialize -> the whole pool key, straight off the log. Three indexed args (id, currency0,
    currency1), so the data words are fee, tickSpacing, hooks, sqrtPriceX96, tick and the hook is word 2
    (the same off-by-one discovery/seed.py documents)."""
    tp = log.get('topics') or []
    if not tp or tp[0].lower() != INITIALIZE or len(tp) < 4:
        return None
    d = (log.get('data') or '0x')[2:]
    if len(d) < 64 * 5:
        return None
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    return dict(kind='init', pool=tp[1].lower(), c0=('0x' + tp[2][-40:]).lower(),
                c1=('0x' + tp[3][-40:]).lower(), fee=_i(w[0]), ts=_signed(_i(w[1]), 24),
                hook=('0x' + w[2][-40:]).lower(), sp=_i(w[3]), tick=_signed(_i(w[4]), 24), **_meta(log))


def decode_head(hdr):
    """A newHeads payload -> the header fields core keeps. l1 is None on every 4663 socket (see top)."""
    if not isinstance(hdr, dict) or 'number' not in hdr or 'topics' in hdr:
        return None
    bf = hdr.get('baseFeePerGas')
    l1 = hdr.get('l1BlockNumber')
    return dict(kind='head', number=_i(hdr['number']), basefee=_i(bf) if bf else None,
                l1=_i(l1) if l1 else None, ts=_i(hdr['timestamp']) if hdr.get('timestamp') else None,
                hash=(hdr.get('hash') or '').lower() or None, parent=(hdr.get('parentHash') or '').lower() or None)


def decode(msg):
    """Any socket payload -> one event dict, or None. A reorged log is never applied as state: it
    becomes a `modify` so that pool is re-read from the chain."""
    if not isinstance(msg, dict):
        return None
    if msg.get('removed'):
        tp = msg.get('topics') or []
        pool = tp[1].lower() if tp and tp[0].lower() in PM_TOPICS and len(tp) > 1 else (msg.get('address') or '').lower()
        return dict(kind='modify', pool=pool, removed=True, **_meta(msg)) if pool else None
    return (decode_swap(msg) or decode_modify(msg) or decode_protocol_fee(msg) or decode_init(msg)
            or decode_v3_swap(msg) or decode_v3_liquidity(msg) or decode_v2_sync(msg) or decode_head(msg))


def _selftest():
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    from eth_utils import keccak
    k = lambda s: '0x' + keccak(text=s).hex()
    # the topics are keccak of the signatures, not strings someone pasted
    A(SWAP == k('Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)'))
    A(MODIFY_LIQUIDITY == k('ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)'))
    A(PROTOCOL_FEE == k('ProtocolFeeUpdated(bytes32,uint24)'))
    A(V3_SWAP == k('Swap(address,address,int256,int256,uint160,uint128,int24)'))
    A(PANCAKE_SWAP == k('Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)'))
    pw = ''.join('%064x' % x for x in (5, (1 << 256) - 7, 2 ** 96, 10 ** 18, 60, 3, 4))
    ps = decode(dict(topics=[PANCAKE_SWAP, '0x' + '11' * 32, '0x' + '22' * 32], data='0x' + pw, address='0x' + 'AB' * 20))
    A(ps['kind'] == 'swap' and ps['pool'] == '0x' + 'ab' * 20 and ps['sp'] == 2 ** 96 and ps['amount1'] == -7
      and ps['tick'] == 60, 'a Pancake/Giga swap decodes as a v3 swap (protocol-fee words ignored)')
    A(V3_MINT == k('Mint(address,address,int24,int24,uint128,uint256,uint256)'))
    A(V3_BURN == k('Burn(address,int24,int24,uint128,uint256,uint256)'))
    A(ALG_BURN == k('Burn(address,int24,int24,uint128,uint256,uint256,uint24)'))
    A(V2_SYNC == k('Sync(uint112,uint112)') and SOL_SYNC == k('Sync(uint256,uint256)'))
    sy = decode(dict(topics=[SOL_SYNC], data='0x' + '%064x%064x' % (7, 9), address='0x' + 'CD' * 20))
    A(sy['kind'] == 'swap' and sy['pool'] == '0x' + 'cd' * 20 and (sy['sp'], sy['L'], sy['tick']) == (7, 9, 0), sy)
    A(INITIALIZE == k('Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)'))

    pf = decode(dict(topics=[PROTOCOL_FEE, '0x' + 'ab' * 32], data='0x' + '%064x' % (25 | (40 << 12))))
    A(pf['kind'] == 'protocol_fee' and pf['pf'] == (25, 40), 'protocol fee unpacks 12+12 bits like slot0')
    h = decode(dict(number='0x10', baseFeePerGas='0x3b0d300', timestamp='0x5'))
    A(h['kind'] == 'head' and h['number'] == 16 and h['basefee'] == 0x3b0d300 and h['l1'] is None)
    A(decode(dict(number='0x10', l1BlockNumber='0x7'))['l1'] == 7, 'an l1 in the header is taken when present')
    # a reorged swap is NEVER applied as state
    rm = decode(dict(topics=[SWAP, '0x' + 'cd' * 32], data='0x' + '00' * 192, removed=True))
    A(rm['kind'] == 'modify' and rm['pool'] == '0x' + 'cd' * 32 and rm['removed'], 'removed log -> refetch')
    rm3 = decode(dict(topics=[V3_SWAP], address='0x' + 'EE' * 20, removed=True))
    A(rm3['kind'] == 'modify' and rm3['pool'] == '0x' + 'ee' * 20, 'removed v3 log -> refetch by address')
    A(decode(dict(topics=['0xdead'], data='0x')) is None)
    # Initialize: the hook is data word 2, and a NEGATIVE tickSpacing must survive sign extension
    ini = decode(dict(topics=[INITIALIZE, '0x' + 'ab' * 32, '0x' + '00' * 12 + '11' * 20,
                              '0x' + '00' * 12 + '22' * 20],
                      data='0x' + '%064x' % 0 + '%064x' % ((1 << 24) - 3) + '00' * 12 + '33' * 20
                           + '%064x' % 79228162514264337593543950336 + '%064x' % 7,
                      blockNumber='0x2'))
    A(ini['kind'] == 'init' and ini['hook'] == '0x' + '33' * 20, 'the hook is data word 2, not word 1')
    A(ini['c0'] == '0x' + '11' * 20 and ini['c1'] == '0x' + '22' * 20 and ini['fee'] == 0)
    A(ini['ts'] == -3 and ini['tick'] == 7, 'int24 tickSpacing is sign-extended, not read raw')
    A(decode(dict(topics=[INITIALIZE, '0x' + 'ab' * 32], data='0x')) is None, 'a short Initialize is None')
    A(all(s in SIGNALS for s in POOL_SIGNALS)
      and {c[0] for c in CATALOG if c[1] != 'watch'} <= set(SIGNALS),
      'every catalogued SIGNAL is one a strategy can name (the watch layer is not one)')
    print('events selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
