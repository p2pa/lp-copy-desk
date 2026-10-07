"""The vocabulary every part of hook_bot speaks. Five frozen things, no chain logic here.

A hook_bot Route is one arbitrage cycle that starts and ends in a QUOTE token and passes through at
least one hooked pool whose fee a WAIVER makes free. `minOut` in CycleArb.sol is the only profit
protection - a miss costs gas, never principal - so nothing in Python needs to be trusted about price.

WHY `Need`. The old two engines (crypto desk4-arb, crypto_arc arc-hook-shots) grew per-mechanism firing
paths that each re-derived the same three questions - how much gas must the tx carry, must the sender be
fresh, which L1 block must it land in - and fixed the same bugs (route_gas undercount, phantom hooked
twin) two different ways. Here a waiver DECLARES its requirement as a Need, a route's Need is the merge
(`|`) of its legs', and the gate eth_call and the send use that one Need by construction. "A gate that
does not match the send is not a gate" (DESK4.md §19) becomes true because they read the same object.
"""
NAT = '0x' + '0' * 40


def _lc(a):
    return (a or NAT).lower()


class Pool:
    """A v4 or v3 pool. `v` 4 -> priced by PoolKey (c0,c1,fee,ts,hook); v 3 -> priced off its own address.

    THE INVARIANT (crypto r/2026-09-17 §11 and crypto_arc §43.5, the phantom-twin bug fixed two ways):
    a v4 PoolKey hashes the hook into the poolId, so a key built with the wrong hook addresses a pool that
    does not exist and every route gated against it can never fill. `pid()` recomputes the id from the key;
    `matches_id()` is the load-time guard a twin must pass. A twin with hook != NAT is a HOOKED twin and
    keeps its real hook - it is never silently coerced to NAT."""
    __slots__ = ('id', 'c0', 'c1', 'fee', 'ts', 'hook', 'v', 'alg', 'sol')

    def __init__(self, id, c0, c1, fee, ts, hook=NAT, v=4, alg=False, sol=False):
        self.id = (id or '').lower()
        self.c0, self.c1 = _lc(c0), _lc(c1)
        self.fee = int(fee or 0)
        self.ts = None if ts is None else int(ts)   # a v3 twin may carry ts=None until resolved live
        self.v = int(v)
        self.hook = _lc(hook)
        self.sol = bool(sol)    # v=2 only: a Solidly/Aerodrome volatile pair (fee floored off the input)
        self.alg = bool(alg)    # an Algebra (Integral) pool: v3 swap ABI, globalState/tickTable reads, ts=1 (raw-tick table)

    def pid(self):
        """keccak(abi.encode(PoolKey)) - the v4 pool id, hook hashed in. v3 has no such id (returns self.id)."""
        if self.v != 4:
            return self.id
        from eth_hash.auto import keccak
        a = lambda x: bytes.fromhex(x[2:].lower().rjust(40, '0')).rjust(32, b'\0')
        n = lambda x: (int(x) & (2 ** 256 - 1)).to_bytes(32, 'big')
        return '0x' + keccak(a(self.c0) + a(self.c1) + n(self.fee) + n(self.ts) + a(self.hook)).hex()

    def matches_id(self):
        """True if the stored id is the keccak of the stored key. A v4 pool that fails this would build
        calldata for a phantom pool; load_targets drops such a twin loudly instead of arming it."""
        return self.v != 4 or self.pid() == self.id

    def other(self, tok):
        tok = _lc(tok)
        return self.c1 if self.c0 == tok else self.c0

    def has(self, tok):
        return _lc(tok) in (self.c0, self.c1)

    def key(self):
        """The dict shape amm/route/tickfetch already speak."""
        return dict(pool=self.id, c0=self.c0, c1=self.c1, fee=self.fee, ts=self.ts, hook=self.hook, v=self.v, alg=self.alg, sol=self.sol)

    def __repr__(self):
        return 'Pool(%s v%d %s)' % (self.id[:10], self.v, self.hook[:8])


class Leg:
    """One hop: a pool, a direction, and the fee the chain charges us on it in bps (negative = we pay).
    `skim` overlays a CHAIN-MEASURED hook fee on top of the AMM curve; it is 0.0 for an unhooked leg and
    is NEVER a guess (a hooked leg's fee is probed or learned live, never modelled from slot0)."""
    __slots__ = ('pool', 'zfo', 'skim')

    def __init__(self, pool, zfo, skim=0.0):
        # None = UNKNOWN, which is not 0.0. A hooked leg no waiver owns and the learner has never measured
        # must not be priced as free; core.cycles.price_legs resolves it or refuses the route.
        self.pool, self.zfo = pool, bool(zfo)
        self.skim = None if skim is None else float(skim)

    def triple(self):
        """The (key, zfo, skim) tuple the copied amm/price functions take."""
        if self.skim is None:
            raise ValueError('leg %s has an UNKNOWN hooked fee and must not be priced' % self.pool.id[:12])
        return (self.pool.key(), self.zfo, self.skim)


class Need:
    """What execution must look like for a route's hooked legs to run free. Merged over legs with `|`.

    gas          - the tx gas LIMIT (gasleft waivers switch off above a per-hook threshold T; MAX over legs,
                   because a tier is a floor - 42/42 laddered hooks read got(16.8M) >= got(6M)).
    fresh_sender - some hooks waive the fee once per SENDER, so the route must fire from a fresh contract
                   (a shot), never the shared CycleArb. True if ANY leg needs it.
    block        - the calendar waivers price per L1 block; carries the block a route must land in, if any.
    fresh_origin - the hook waives once per signing wallet, and only when the swap comes through a router
                   it trusts. A fresh contract under our usual caller still pays. True if ANY leg needs it.
    """
    __slots__ = ('gas', 'fresh_sender', 'block', 'fresh_origin')

    def __init__(self, gas=0, fresh_sender=False, block=None, fresh_origin=False):
        self.gas, self.fresh_sender, self.block = int(gas), bool(fresh_sender), block
        self.fresh_origin = bool(fresh_origin)

    def __or__(a, b):
        return Need(max(a.gas, b.gas), a.fresh_sender or b.fresh_sender,
                    a.block if a.block is not None else b.block,
                    a.fresh_origin or b.fresh_origin)

    def __repr__(self):
        return 'Need(gas=%d fresh=%s origin=%s block=%s)' % (
            self.gas, self.fresh_sender, self.fresh_origin, self.block)


class Route:
    """A priced, sized cycle. `legs` are Legs, `start` the quote token it opens and closes in."""
    __slots__ = ('strategy', 'legs', 'start', 'amount_in', 'expected', 'gross_usd', 'net_usd', 'need',
                 'sym', 'pool', 'twin', 'state')

    def __init__(self, strategy, legs, start, amount_in, expected, gross_usd, net_usd, need,
                 sym='', pool=None, twin=None):
        self.strategy, self.legs, self.start = strategy, legs, start
        self.amount_in, self.expected = int(amount_in), expected
        self.gross_usd, self.net_usd, self.need = gross_usd, net_usd, need
        self.sym, self.pool, self.twin = sym, pool, twin
        self.state = None       # [(state block, tick, L, lf)] per leg: what the model priced on (core.cycles.price_legs)

    def hooked_leg(self):
        """The index of the first hooked leg - the direction skims/gates/A6 are keyed under."""
        for i, lg in enumerate(self.legs):
            if lg.pool.hook != NAT:
                return i
        return 0


def _selftest():
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    # Need merges by MAX gas / OR fresh / first block
    A((Need(6_000_000) | Need(16_777_216)).gas == 16_777_216)
    A((Need(0, True) | Need(0, False)).fresh_sender is True)
    A((Need(0) | Need(0, fresh_origin=True)).fresh_origin is True)
    A((Need(0, fresh_origin=True) | Need(0)).fresh_sender is False, 'an origin waiver is not a clone shot')
    A((Need(0) | Need(0, block=5)).block == 5)
    A((Need(0, block=3) | Need(0, block=9)).block == 3, 'first block wins on merge')

    # a v4 pool's id must be the keccak of its key (matches_id), and the hook is hashed in
    real = '0x' + '0' * 40
    p = Pool('x', NAT, '0x' + '11' * 20, 100, 1, NAT, 4)
    p.id = p.pid()
    A(p.matches_id(), 'a pool built from its own key must validate')
    # flip the hook -> the id no longer matches: this is the phantom-twin guard
    bad = Pool(p.id, NAT, '0x' + '11' * 20, 100, 1, '0x' + '22' * 20, 4)
    A(not bad.matches_id(), 'a hooked twin keyed under NAT must be caught')
    # v3 pool: no key hashing, always matches
    A(Pool('0x' + '33' * 20, NAT, '0x' + '11' * 20, 500, 10, NAT, 3).matches_id())

    # other()/has()
    A(p.other(NAT) == ('0x' + '11' * 20) and p.has(NAT) and not p.has('0x' + '99' * 20))

    # Route.hooked_leg finds the hooked index
    h = Pool('a', NAT, '0x' + '11' * 20, 0, 1, '0x' + 'ab' * 20, 4)
    t = Pool('b', NAT, '0x' + '11' * 20, 100, 1, NAT, 4)
    A(Route('twin', [Leg(t, True), Leg(h, False)], NAT, 1, [1, 1], 0, 0, Need()).hooked_leg() == 1)
    print('model selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
