"""Chain = a parsed chains/<name>/chain.toml + the two behaviours config cannot express.

Everything that differs between Robinhood (4663) and Arc (5042) is DATA in the toml - endpoints, gas
token, tx gas cap, quote tokens, contract addresses, wallet names, waiver hook sets, risk knobs. The only
things that are code, because they are not values, are:

  gas_usd(basefee)  - ETH*price (Robinhood, dynamic) vs a flat USDC amount (Arc). $ == USDC on Arc means
                      the whole `* ETH_USD` ladder the 4663 desk carried is a no-op here, not re-valued.
  l1_block(hdr)     - only the calendar waiver needs it; Robinhood exposes Arbitrum's l1BlockNumber, Arc
                      has no L1 (Malachite BFT, 1-conf final) so it degrades to the L2 number.

Adding a chain is writing a toml and, at most, overriding one of these two. `hookbot <chain> doctor`
checks the rest.
"""
import os, tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAT = '0x' + '0' * 40


class Chain:
    def __init__(self, cfg, backend='live', fixtures=None):
        self.cfg = cfg
        self.name = cfg['name']
        # chains/<name>/ is the ONE folder for everything specific to this chain: chain.toml (params),
        # rpc.toml (endpoints), pools.json (targets) and var/ (gitignored mutable state).
        self.dir = cfg.get('_dir') or os.path.join(ROOT, 'chains', self.name)
        self.chain_id = cfg['chain_id']
        self.native_dec = cfg.get('native_dec', 18)
        self.conf_dir = os.path.expanduser(cfg.get('config_dir', '~/.config/lp-copy-desk'))
        g = cfg['gas']
        self.gas_token = g['token']            # 'eth' | 'usdc'
        self.gas_mode = g['mode']              # 'dynamic' | 'flat'
        self.gwei = int(g.get('gwei', 0))
        self.eth_usd = float(g.get('eth_usd', 1.0))
        self.eth_usd_pool = g.get('eth_usd_pool')   # native/stable v4 pool the desk reads ETH's price from
        self.tx_cap = int(g['tx_cap'])
        self.gas_units = int(g.get('units', 420_000))
        # Block ORDERING decides whether a tip buys anything (core/exec/tx.py tip_wei). 'fcfs' = the tip is
        # ignored (robinhood: every tx in a block pays one price); 'priority' = sorted by price (arc).
        self.ordering = g.get('ordering', 'fcfs')
        self.tip_share = float(g.get('tip_share', 0.0))
        self.tip_max_gwei = float(g.get('tip_max_gwei', 0.0))
        self.tip_max_usd = float(g.get('tip_max_usd', 0.0))     # A4: the tip's worst case per send, tip x the route's gas
        c = cfg['contracts']
        self.pm = c['poolmanager']
        self.stateview = c['stateview']
        self.v3_factory = c.get('v3_factory', NAT)
        self.multicall3 = c.get('multicall3')
        self.weth = c['weth']
        self.artifact = c.get('artifact')                     # hook_bot's arb contract; the LP desk never uses it
        self.quotes = {k.lower(): (v['sym'], float(v['usd']), int(v['dec'])) for k, v in cfg['quotes'].items()}
        # LIVE-PRICED QUOTES: a [quotes] entry may name `pool` (a v4 pool id) and `vs` (the quote it trades against,
        # whose price we already hold). refresh_eth_usd() re-reads them after ETH. Robinhood, 2026-09-25: 27% of v4
        # swaps were on pairs with no listed quote (META, NVDA, AMD, ORBIO, musebook as the bases); a quote with a
        # typed-in price would book every fill in it wrong the moment the stock moved.
        self.quote_pools = {k.lower(): (v['pool'].lower(), v['vs'].lower()) for k, v in cfg['quotes'].items() if v.get('pool') and v.get('vs')}
        self.wallet_file = os.path.expanduser(cfg['wallet']['file'])
        self.caller = cfg['wallet']['caller']
        self.deployer = cfg['wallet'].get('deployer', self.caller)
        # hook_bot's arb sections: optional here (the copy desk reads [gas] [contracts] [quotes] [wallet] [refuel] [lp])
        self.strategies = cfg.get('strategies', {}).get('enabled', [])
        loop = cfg.get('loop', {})
        self.poll_s = float(loop.get('poll_s', 3.0))
        # how long the ws loop keeps collecting socket events before it prices (core/runner.run_ws). 0.5 s was the
        # single biggest stage of event -> gate on robinhood (p50 544 ms of 916, 2026-09-25): a 0.1 s chain moved
        # five blocks while we waited for more news. Price what has arrived; the next pass takes the rest.
        self.drain_s = float(loop.get('drain_s', 0.5))
        self.twin_min_depth_usd = float(loop.get('twin_min_depth_usd', 0.0))
        self.feed_source = loop.get('source', 'poll')      # 'ws' | 'poll' - swapping is config, not code
        self.risk = cfg.get('risk', {})
        self.fees = cfg.get('fees', {})
        self.refuel = cfg.get('refuel', {'mode': 'none'})
        self.waivers = cfg.get('waivers', {})
        self.targets_file = os.path.join(self.dir, cfg.get('targets', {}).get('file', 'pools.json'))
        self.tg_config = os.path.expanduser(cfg.get('tg_config', '~/.config/lp-copy-desk/telegram.json'))
        self.var = os.path.join(self.dir, 'var')
        os.makedirs(self.var, exist_ok=True)
        # transport
        r = cfg['rpc']
        self.rpc_url = self._key_url() or r.get('primary') or r['public']
        self.public = r['public']
        self.rotate = r.get('rotate', [self.rpc_url])
        self.send_urls = r.get('send_urls', [self.rpc_url])
        self.broadcast_urls = r.get('broadcast', [])           # extra endpoints a signed tx is pushed to in parallel
        self.archive_urls = r.get('archive_urls', [self.public])
        self.log_pool = r.get('log_pool', [])
        self.hypersync = r.get('hypersync', '')
        # Where a human reads a tx. Only fmt uses it, to turn a hash on a card into a link.
        self.explorer = (r.get('explorer') or '').rstrip('/')
        self.ws_urls = r.get('ws', [])
        # PRE-CONFIRMED STATE (base Flashblocks, 2026-09-25): sockets that serve `pendingLogs` - a swap reaches us when the
        # sequencer pre-confirms it (~200 ms), up to ~1.8 s before its block seals - and the HTTP nodes whose 'pending'
        # tag IS that state (polkachu answers pending == latest: not one of them). [loop] gate_block picks the gate's tag.
        self.ws_pending_urls = r.get('ws_pending', [])
        self.gate_urls = r.get('gate_urls', [])
        self.gate_prefer_first = bool(r.get('gate_prefer_first', False))   # the chain's own node first (ops/node.py bridge)
        self.gate_block = str((self.cfg.get('loop') or {}).get('gate_block', 'latest'))
        self.head = None      # {number, basefee, t} - see observe_head
        self.l1 = None
        from . import rpc as _rpc
        self.node = _rpc.Replay(fixtures or {}) if backend == 'replay' else _rpc.Live(self)

    # -- keys / wallets ----------------------------------------------------------------------------
    def _key_url(self):
        """A keyed endpoint from config_dir, unless marked down. crypto/rh.py honours `alchemy.down`; this
        did not, so on 2026-09-19 every robinhood read went first to an Alchemy key past its monthly cap
        (429) and only then rotated - a wasted round trip on every call, gates included."""
        f = os.path.join(self.conf_dir, 'rpc.url')
        if not os.path.exists(f) or os.path.exists(os.path.join(self.conf_dir, 'alchemy.down')):
            return None
        return open(f).read().strip() or None

    # -- gas: the ONE real per-chain behaviour -----------------------------------------------------
    # -- the header cache ----------------------------------------------------------------------------
    # newHeads (socket) and the per-tick header read both land here, so basefee is not an RPC per priced
    # route: cycles.price_legs calls gas_usd() once per route, and every call used to be an
    # eth_getBlockByNumber. HEAD_TTL bounds how stale a cached basefee may be before we read again.
    HEAD_TTL = 10.0

    def observe_head(self, number=None, basefee=None, l1=None, ts=None):
        """Record a header. Returns True when the L1 block moved (the calendar's redraw)."""
        import time as _t
        # THE HEAD ONLY MOVES FORWARD. The tick reads 'latest' from a rotating HTTP pool and some members lag (robinhood
        # 2026-09-25: pocket up to 8 blocks, blxrbdn up to 72, the chain's own rpc 7 always); that header was recorded
        # as the head, which went BACKWARDS 945 times in 20k trace records (worst 2,712 blocks) - trace blocks, the
        # block_age clock and the pinned state reads all followed it. An older header is ignored, l1 included.
        cur = (self.head or {}).get('number')
        if number is not None and cur is not None and number < cur:
            return False
        moved = l1 is not None and l1 != self.l1
        self.head = dict(number=number, basefee=basefee if basefee is not None else (self.head or {}).get('basefee'),
                         t=_t.time(), ts=ts)      # t = when WE saw it, ts = the block's own timestamp
        if l1 is not None:
            self.l1 = l1
        return moved

    def read_head(self):
        """One eth_getBlockByNumber: block, basefee and l1. The live desks make exactly this read once per
        poll; on 4663 it is also the ONLY source of l1 (newHeads does not carry l1BlockNumber there)."""
        hdr = self.node.rpc('eth_getBlockByNumber', ['latest', False])
        bf = hdr.get('baseFeePerGas')
        return self.observe_head(int(hdr['number'], 16), int(bf, 16) if bf else None, self.l1_block(hdr),
                                 int(hdr['timestamp'], 16) if hdr.get('timestamp') else None)

    def basefee(self):
        if self.gas_mode == 'flat':
            return self.gwei * 10 ** 9
        import time as _t
        h = self.head
        if h and h.get('basefee') and _t.time() - h['t'] < self.HEAD_TTL:
            return h['basefee']
        return int(self.node.rpc('eth_getBlockByNumber', ['latest', False])['baseFeePerGas'], 16)

    def block_s(self):
        """Seconds per block: [gas] block_s, else measured once from two headers 5,000 blocks apart. The LP screen and
        record had 0.1014 (robinhood) hard-coded with 1.0 for every other chain: on arc (0.508 s) its "1 day ago"
        read landed ~12 h back, on base (2.0 s) ~48 h back."""
        if getattr(self, '_block_s', None):
            return self._block_s
        cfg = (self.cfg.get('gas') or {}).get('block_s')
        if cfg:
            self._block_s = float(cfg)
            return self._block_s
        h1 = self.node.rpc('eth_getBlockByNumber', ['latest', False])
        h0 = self.node.rpc('eth_getBlockByNumber', [hex(int(h1['number'], 16) - 5000), False])
        self._block_s = (int(h1['timestamp'], 16) - int(h0['timestamp'], 16)) / 5000
        return self._block_s

    def refresh_eth_usd(self):
        """Re-read ETH's dollar price off [gas] eth_usd_pool (a native/stable v4 pool) into eth_usd and the
        ETH/WETH quotes, or None. The typed-in 2512 on robinhood sat ~9% under the market (2,751 on
        2026-09-22), so every refuel showed as a balance DROP and every route under-priced its gas.
        Native is always c0 in a v4 pool (address 0 sorts first), so the price is c1 per ETH."""
        if not self.eth_usd_pool:
            return None
        from .tickfetch import S
        s = self.node.call(self.stateview, S['v4slot0'] + self.eth_usd_pool[2:])
        sp = int(s[2:66], 16)
        stable = next(v for k, v in self.quotes.items() if v[1] == 1.0 and k != NAT)
        px = (sp / 2 ** 96) ** 2 * 10 ** (self.native_dec - stable[2])
        if not (0.5 < px / self.eth_usd < 2.0):
            return None      # a bad read or a drained pool must not reprice the desk's gas and book
        self.eth_usd = px
        for k, v in list(self.quotes.items()):
            if v[0].upper() in ('ETH', 'WETH'):
                self.quotes[k] = (v[0], px, v[2])
        try:
            self.refresh_quote_prices()                   # a failed quote read keeps the last prices, never costs ETH's
        except Exception:
            pass
        return px

    def refresh_quote_prices(self):
        """Price every live-priced quote (quote_pools) off its reference pool's slot0, in the `vs` quote's dollars.
        The same band as ETH: a move over 2x (or a first read at zero) is a bad read or a drained pool, not a price."""
        if not getattr(self, 'quote_pools', None):
            return {}
        from .tickfetch import S
        calls = [(self.stateview, S['v4slot0'] + pid[2:]) for pid, _vs in self.quote_pools.values()]
        out = {}
        for (tok, (pid, vs)), (ok, h) in zip(self.quote_pools.items(), self.node.multicall(calls)):
            if not ok or len(h) < 66 or vs not in self.quotes:
                continue
            sp = int(h[2:66], 16)
            if not sp:
                continue
            p1_per_0 = (sp / 2 ** 96) ** 2                     # raw token1 per raw token0
            sym, old, dec = self.quotes[tok]
            vdec, vusd = self.quotes[vs][2], self.quotes[vs][1]
            if tok < vs:                                        # tok is currency0: vs per tok
                px = p1_per_0 * 10 ** (dec - vdec) * vusd
            else:
                px = (1 / p1_per_0) * 10 ** (dec - vdec) * vusd
            if old > 0 and not (0.5 < px / old < 2.0):
                continue
            self.quotes[tok] = (sym, px, dec)
            out[sym] = px
        return out

    def gas_usd(self, basefee=None):
        """USD cost of a default 2-hop send. On Arc gas IS USDC (eth_usd=1.0) so this is already dollars;
        on Robinhood it is ETH * price."""
        bf = self.basefee() if basefee is None else basefee
        return self.gas_units * bf / 1e18 * self.eth_usd

    def l1_block(self, hdr):
        """The block number the calendar waiver hashes on. Arbitrum (4663) exposes l1BlockNumber; a chain
        without an L1 falls back to the L2 number and its calendar simply never fires."""
        v = hdr.get('l1BlockNumber')
        if isinstance(v, int):          # anvil forks of 4663 hand it back as a JSON number, not hex
            return v
        return int(v, 16) if v else int(hdr['number'], 16)

    # -- quote-token money maths -------------------------------------------------------------------
    def units(self, usd, tok):
        _sym, px, dec = self.quotes[tok.lower()]
        return int(usd / px * 10 ** dec)

    def to_usd(self, amt, tok):
        _sym, px, dec = self.quotes[tok.lower()]
        return amt / 10.0 ** dec * px

    def is_quote(self, tok):
        return tok.lower() in self.quotes

    def quote_tokens(self):
        return list(self.quotes)

    def sym(self, tok):
        q = self.quotes.get((tok or '').lower())
        return q[0] if q else (tok or '')[:10]


def load(name, backend='live', fixtures=None):
    """Load a chain by name. `name` may be a bare name (chains/<name>/chain.toml) or a toml path. An
    rpc.toml beside it becomes the [rpc] table."""
    path = name if os.path.isfile(name) else os.path.join(ROOT, 'chains', name, 'chain.toml')
    if not os.path.isfile(path):
        raise SystemExit('no chain config for %r (looked at %s)' % (name, path))
    cfg = tomllib.load(open(path, 'rb'))
    cfg['_dir'] = os.path.dirname(os.path.abspath(path))
    rp = os.path.join(cfg['_dir'], 'rpc.toml')
    if os.path.isfile(rp):
        cfg['rpc'] = tomllib.load(open(rp, 'rb'))
    # KEYED ENDPOINTS live in rpc.local.toml beside it - gitignored, never committed. Its lists go IN FRONT of rpc.toml's
    # (the keyed node first, the public ones as fallback), its scalars replace; `primary` becomes the first read endpoint.
    lp = os.path.join(cfg['_dir'], 'rpc.local.toml')
    if backend == 'live' and os.path.isfile(lp) and 'rpc' in cfg:
        for k, v in tomllib.load(open(lp, 'rb')).items():
            cfg['rpc'][k] = list(dict.fromkeys(list(v) + list(cfg['rpc'].get(k) or []))) if isinstance(v, list) else v
    if backend == 'replay' and fixtures is None:
        fx = os.path.join(ROOT, 'tests', 'fixtures', cfg['name'], 'poll.json')
        import json
        fixtures = json.load(open(fx)) if os.path.exists(fx) else {}
    return Chain(cfg, backend=backend, fixtures=fixtures)


def names():
    d = os.path.join(ROOT, 'chains')
    return sorted(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)) and not n.startswith('_')
                  and os.path.exists(os.path.join(d, n, 'chain.toml')))


def _selftest():
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    EX = os.path.join(ROOT, 'chains', 'robinhood', 'chain.example.toml')   # the shipped example: no install needed
    rh = load(EX, backend='replay', fixtures={})
    A(rh.name == 'robinhood' and rh.chain_id == 4663)
    A(rh.gas_token == 'eth' and rh.gas_mode == 'dynamic')
    # Robinhood: gas_usd = units * basefee * eth_usd. At a fixed basefee it scales with eth_usd.
    A(abs(rh.gas_usd(5 * 10 ** 7) - (420_000 * 5e7 / 1e18 * 2512.0)) < 1e-6, 'rh gas scales by eth_usd')
    A(rh.units(1.0, '0x5fc5360d0400a0fd4f2af552add042d716f1d168') == 10 ** 6, 'usdg 6-dec')
    A(len(rh.ws_urls) >= 1, 'ws endpoints are config')
    # l1_block: Arbitrum field vs L2 fallback
    A(rh.l1_block({'number': '0x64', 'l1BlockNumber': '0x10'}) == 16)
    # a fresh header is served from cache - no RPC per priced route
    class _NoRpc:
        def rpc(self, *a, **k): raise AssertionError('basefee must come from the header cache')
    rh.node, keep = _NoRpc(), rh.node
    A(rh.observe_head(100, 5 * 10 ** 7, 16) is True and rh.basefee() == 5 * 10 ** 7, 'cached basefee')
    A(rh.observe_head(101, 6 * 10 ** 7, 16) is False, 'same l1: no redraw')
    A(rh.observe_head(102, None, 17) is True and rh.basefee() == 6 * 10 ** 7, 'l1 moved; basefee kept')
    A(rh.observe_head(90, 9 * 10 ** 9, 99) is False and rh.head['number'] == 102 and rh.l1 == 17, 'an older header never rolls the head back')
    rh.node = keep
    # eth_usd from the pool: sqrtP for 2750 USDC(6)/ETH(18), then a read 10x off is refused
    rh = load(EX, backend='replay', fixtures={})
    class N:
        sp = int((2750 * 10 ** (6 - 18)) ** 0.5 * 2 ** 96)
        def call(self, to, data, **k): return '0x' + hex(self.sp)[2:].rjust(64, '0') + '0' * 192
    rh.node, rh.eth_usd = N(), 2512.0
    A(abs(rh.refresh_eth_usd() - 2750) < 1 and abs(rh.quotes[NAT][1] - 2750) < 1, 'live ETH price read')
    rh.node.sp *= 4
    A(rh.refresh_eth_usd() is None and abs(rh.eth_usd - 2750) < 1, 'a 16x price read is refused')
    # a live-priced quote: token is currency0 (address below the stable) or currency1, 18 vs 6 decimals, band kept
    import math
    class _N:
        def __init__(self, px): self.px = px
        def multicall(self, calls): return [(True, '0x%064x' % int(math.sqrt(self.px) * 2 ** 96))]
    q = type('Q', (), {})()
    q.stateview, q.quote_pools = '0xsv', {'0x0a': ('0xpool', '0xff')}
    q.quotes = {'0xff': ('USDG', 1.0, 6), '0x0a': ('META', 750.0, 18)}
    q.node = _N(750.0 * 10 ** 6 / 10 ** 18)                 # raw USDG per raw META, META is currency0
    Chain.refresh_quote_prices(q)
    A(abs(q.quotes['0x0a'][1] - 750.0) < 1e-6, 'quote as currency0 priced: %s' % (q.quotes['0x0a'],))
    q.quote_pools, q.quotes = {'0xfff': ('0xpool', '0x0b')}, {'0x0b': ('USDG', 1.0, 6), '0xfff': ('NVDA', 225.0, 18)}
    q.node = _N(10 ** 18 / (225.0 * 10 ** 6))               # raw NVDA per raw USDG, NVDA is currency1
    Chain.refresh_quote_prices(q)
    A(abs(q.quotes['0xfff'][1] - 225.0) < 1e-6, 'quote as currency1 priced: %s' % (q.quotes['0xfff'],))
    q.node = _N(10 ** 18 / (900.0 * 10 ** 6))               # a 4x jump is a bad read: the old price stays
    Chain.refresh_quote_prices(q)
    A(abs(q.quotes['0xfff'][1] - 225.0) < 1e-6, 'a >2x move is refused')
    print('chain selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(HERE))
    sys.exit(_selftest())
