"""Pull the tick window every edge needs for exact local pricing. Copied from crypto_arc's tickfetch.py
(itself the desk4-arb one), with the only chain coupling - the StateView address and the multicall node -
passed in instead of imported, so one copy serves every chain.

An `edge` is {kind: 'v4'|'v3', id: poolId or pool address, ts: tickSpacing}. Three multicall rounds:
slot0+liquidity, tick-bitmap words either side, then the initialised ticks' liquidityNet. v4 reads
StateView by poolId; v3 reads slot0()/liquidity()/tickBitmap()/ticks() on the pool address. Both agree
with the chain to the wei (crypto_arc r/2026-09-17 §36.3)."""

S = {'v4bm': '0x1c7ccb4c', 'v4tl': '0xcaedab54', 'v3bm': '0x5339c296', 'v3tk': '0xf30dba93',
     'v4slot0': '0xc815641c', 'v4liq': '0xfa6793d5', 'v3slot0': '0x3850c7bd', 'v3liq': '0x1a686502', 'v3fee': '0xddca3f43',
     'algstate': '0xe76c01e4', 'algtable': '0xc677e3e0', 'v2res': '0x0902f1ac'}   # Algebra Integral: globalState(), tickTable(int16)


def w(x):
    return '%064x' % (x & (2 ** 256 - 1))


def s24(v):
    return v - 2 ** 256 if v >= 2 ** 255 else v


def s128(v):
    # ABI SIGN-EXTENDS an int128 to 256 bits. Without the mask every negative liquidityNet decoded as ~1.16e77
    # (14,438 of 30,200 robinhood ticks, 2026-09-25): a swap crossing one died (L < 0) or saw infinite depth.
    v &= 2 ** 128 - 1
    return v - 2 ** 128 if v >= 2 ** 127 else v


def head_calls(sv, e):
    if e['kind'] == 'v2':
        return [(e['id'], S['v2res'])]
    if e['kind'] == 'v4':
        pid = (e['id'] if e['id'].startswith('0x') else '0x' + e['id'])[2:]
        return [(sv, S['v4slot0'] + pid), (sv, S['v4liq'] + pid)]
    # fee() too: a v3 CLONE's fee is not always immutable - up.'s Slipstream (factory 0x1ac9db4a) asks a fee module on
    # every swap, and 6 of its 86 gauged pools moved fee() within an hour (11725 -> 10000) on 2026-09-25
    # Algebra: globalState() opens with the same (price, tick) words as slot0; fee() is its live (plugin) fee
    return [(e['id'], S['algstate' if e.get('alg') else 'v3slot0']), (e['id'], S['v3liq']), (e['id'], S['v3fee'])]


def parse_head(e, a, b, f=None):
    """(sqrtPriceX96, tick, liquidity, protocolFee_pair, lpFee). The 4th slot0 word carries a v4 dynamic
    pool's live lpFee; a v3 pool's lpFee is its live fee() (None when the read failed: the stored fee stands)."""
    if len(a) < 130 or len(b) < 66:
        return None
    sp = int(a[2:66], 16); tick = s24(int(a[66:130], 16)); L = int(b[2:66], 16)
    pf = (0, 0); lf = int(f[2:66], 16) if (e['kind'] != 'v4' and f and len(f) >= 66) else None
    if e['kind'] == 'v4' and len(a) >= 194:
        raw = int(a[130:194], 16)
        pf = (raw & 0xfff, (raw >> 12) & 0xfff)
        if len(a) >= 258:
            lf = int(a[194:258], 16)
    return (sp, tick, L, pf, lf) if sp else None


def word_calls(sv, e, tick, spacing, span):
    if e['kind'] == 'v2':
        return []
    comp = tick // spacing; wp = comp >> 8
    out = []
    for d in range(-span, span + 1):
        if e['kind'] == 'v4':
            pid = (e['id'] if e['id'].startswith('0x') else '0x' + e['id'])[2:]
            out.append(((sv, S['v4bm'] + pid + w(wp + d)), wp + d))
        else:
            out.append(((e['id'], S['algtable' if e.get('alg') else 'v3bm'] + w(wp + d)), wp + d))
    return out


def tick_calls(sv, e, ticks):
    out = []
    for t in ticks:
        if e['kind'] == 'v4':
            pid = (e['id'] if e['id'].startswith('0x') else '0x' + e['id'])[2:]
            out.append(((sv, S['v4tl'] + pid + w(t)), t))
        else:
            out.append(((e['id'], S['v3tk'] + w(t)), t))
    return out


def ticks_in(word, wp, spacing):
    return [((wp << 8) + i) * spacing for i in range(256) if word >> i & 1]


def heads(node, sv, edges_subset, block='latest', workers=None):
    """Round 1 alone: {edge_key: (sqrtPriceX96, tick, liquidity, protocolFee_pair, lpFee)}. One multicall
    round, no bitmaps - what the websocket engine re-reads every tick to stay a superset of the poll (a
    dynamic lpFee has no PoolManager event, so only a read carries it)."""
    c1, m1 = [], []
    for k in edges_subset:
        hc = head_calls(sv, edges_subset[k])
        m1.append((k, len(c1), len(hc)))
        c1 += hc
    r1 = node.multicall(c1, block=block, chunk=100, **({'workers': workers} if workers is not None else {}))   # 200 calls = ~90 KB: a robinhood endpoint refused it ("request exceeds size limit")
    out = {}
    for k, i, n in m1:
        if n == 1:
            # V2: getReserves -> (reserve0, 0, reserve1, (0, 0), None). A pair has no price word or ticks; carrying
            # its reserves in the sp/L slots keeps the tick refresh, the drift counter and Sync events unchanged.
            # core/price reads them back for key v=2 only.
            ok, a = r1[i]
            if ok and len(a) >= 130 and int(a[2:66], 16) and int(a[66:130], 16):
                out[k] = (int(a[2:66], 16), 0, int(a[66:130], 16), (0, 0), None)
            continue
        (oa, a), (ob, b) = r1[i], r1[i + 1]
        f = r1[i + 2][1] if n > 2 and r1[i + 2][0] else None
        if oa and ob:
            p = parse_head(edges_subset[k], a, b, f)
            if p:
                out[k] = p
    return out


def fetch(node, sv, edges_subset, span=2, block='latest', workers=None):
    """{edge_key: dict(sp, tick, L, pf, lf, bitmaps, net, blk)} - three multicall rounds, all pinned to
    `block`. Reading at a stale block is what let old log-derived state get overwritten; the caller pins."""
    head = heads(node, sv, edges_subset, block=block, workers=workers)
    kw = {'workers': workers} if workers is not None else {}
    c2, m2 = [], []
    for k, (sp, tick, L, pf, _lf) in head.items():
        for call, wp in word_calls(sv, edges_subset[k], tick, edges_subset[k]['ts'], span):
            c2.append(call); m2.append((k, wp))
    r2 = node.multicall(c2, block=block, chunk=100, **kw)
    bm = {}
    for (k, wp), (ok, d) in zip(m2, r2):
        if ok and len(d) >= 66:
            bm.setdefault(k, {})[wp] = int(d[2:66], 16)
    c3, m3 = [], []
    for k, words in bm.items():
        want = []
        for wp, word in words.items():
            if word:
                want += ticks_in(word, wp, edges_subset[k]['ts'])
        # NEAREST FIRST. The cap used to keep the lowest 400 ticks of the window, so on a dense pool (base cbBTC/USDC,
        # ts=1) the ticks just ABOVE the price were dropped and a $50 swap that way priced None.
        want.sort(key=lambda t: abs(t - head[k][1]))
        for call, t in tick_calls(sv, edges_subset[k], want[:400]):
            c3.append(call); m3.append((k, t))
    r3 = node.multicall(c3, block=block, chunk=100, **kw) if c3 else []
    net = {}
    for (k, t), (ok, d) in zip(m3, r3):
        if ok and len(d) >= 130:
            net.setdefault(k, {})[t] = s128(int(d[66:130], 16))
    out = {}
    for k, (sp, tick, L, pf, lf) in head.items():
        out[k] = dict(sp=sp, tick=tick, L=L, pf=pf, lf=lf, bitmaps=bm.get(k, {}), net=net.get(k, {}),
                      blk=int(block, 16) if isinstance(block, str) and block.startswith('0x') else 0)
    return out


def _selftest():
    for x in (0, 1, -1, 5, -5, 2 ** 127 - 1, -2 ** 127):
        assert s128(x % 2 ** 256) == x, x          # sign-extended, as the ABI returns it
        assert s128(x % 2 ** 128) == x, x          # truncated to 128 bits
    assert s24((-209830) % 2 ** 256) == -209830
    a = dict(kind='v3', alg=True, id='0x' + 'ab' * 20, ts=1)
    assert [c[1][:10] for c in head_calls(None, a)] == [S['algstate'], S['v3liq'], S['v3fee']]
    assert word_calls(None, a, -209830, 1, 0)[0][0][1][:10] == S['algtable']
    assert [c[1][:10] for c in head_calls(None, dict(a, alg=False))][0] == S['v3slot0']
    print('tickfetch selftest OK: 18 asserts')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
