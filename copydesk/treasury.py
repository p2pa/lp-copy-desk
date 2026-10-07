"""THE COPY DESK'S TREASURY (owner 2026-09-30: "a system that can swap in and out and do whatever to self fund itself").

The desk's book is USDG. It also needs native ETH: for gas, and since 2026-09-30 for copies in token/ETH pools (97% of
what its leaders open). Every move between the two goes through here, on the ETH/USDG v4 pool ([gas] eth_usd_pool:
native/USDG, 0.046%, ~$6.9M), exact-tick quoted by core/exec/lp.swap with minOut = quote - slippage:

  buy(usd)            USDG -> ETH before an ETH-quoted copy (its size plus a small buffer)
  unwind(since)       ETH -> USDG: every ETH that arrived since the reading `since` (an exit's ETH, a mint's unused
                      buffer), so a copy's cost and its return are both measured in USDG, like a USDG copy's
  settle(topup)       the beat: ETH above the gas band's top (gas_keep_usd) goes back to USDG - the book never sits in
                      idle ETH; under refuel_below_usd a clip of USDG buys gas (core/exec/refuel.keep, topup only); token
                      dust that closed copies left behind is sold into its pool

Guards: conversions stop (and page once) after two in a row filled more than max_conv_bps under the pool's mid; a
stopped treasury refuses ETH copies (skipped, with the reason) and leaves the rest of the desk running. Every
conversion is one line in var/lp/treasury.jsonl. Native balances are read net of our own gas (lp.balance_net), so a
difference of two readings is what trades moved."""
import json, os, time

from core.exec import lp as X

Q96 = 2 ** 96
DUST_USD = 0.5


def _dir(chain):
    d = os.path.join(chain.var, 'lp')
    os.makedirs(d, exist_ok=True)
    return d


def log_path(chain):
    return os.path.join(_dir(chain), 'treasury.jsonl')


def state_path(chain):
    return os.path.join(_dir(chain), 'treasury.json')


class Treasury:
    def __init__(self, chain, ac, c, log=print, note=None):
        """c: [lp.follow] (gas_keep_usd, refuel_below_usd, refuel_clip_usd, conv_slippage_bps, max_conv_bps)."""
        self.chain, self.ac, self.c, self.log, self.note = chain, ac, c, log, note or (lambda *a, **k: None)
        self.u = X.usdg(chain)
        self._key = None
        p = state_path(chain)
        self.st = json.load(open(p)) if os.path.exists(p) else dict(stopped=None, bad=0)

    # ---- reads
    def key(self):
        if self._key is None:
            self._key = X.pool_key(self.chain, self.chain.eth_usd_pool)
            assert X.is_native(self._key['currency0']) and self._key['currency1'].lower() == self.u, 'eth_usd_pool is not native/USDG'
        return self._key

    def px(self):
        """USDG per ETH at the pool's mid (slot0)."""
        sp = X.slot0(self.chain, self.key())[0] / Q96
        return sp * sp * 1e18 / 1e6

    def eth(self):
        return X.balance_net(self.chain, X.NATIVE, self.ac.address)

    def usdg(self):
        return X.balance(self.chain, self.u, self.ac.address)

    def stopped(self):
        return self.st.get('stopped')

    # ---- conversions
    def _save(self):
        json.dump(self.st, open(state_path(self.chain) + '.tmp', 'w'), indent=1)
        os.replace(state_path(self.chain) + '.tmp', state_path(self.chain))

    def _rec(self, **r):
        with open(log_path(self.chain), 'a') as f:
            f.write(json.dumps(dict(ts=time.time(), **r)) + '\n')

    def _swap(self, to_eth, amount_raw, why):
        """One conversion. Returns the raw amount received (ETH wei or USDG raw). Records it; two bad fills in a row stop it."""
        if self.stopped():
            raise RuntimeError('treasury stopped: %s' % self.stopped())
        px = self.px()
        tin, tout = (self.u, X.NATIVE) if to_eth else (X.NATIVE, self.u)
        X.approve(self.chain, self.ac, tin, True, self.log, dict(leg='treasury'))
        b0 = X.balance_net(self.chain, tout, self.ac.address)
        slip = float(self.c.get('conv_slippage_bps', 50)) / 1e4
        k = self.key()
        zfo = not to_eth                                               # native (currency0) in = zero for one
        q = X.quote(self.chain, k, zfo, amount_raw)
        if not q:
            raise RuntimeError('treasury: no quote on the ETH/USDG pool')
        # lp.swap's minOut uses [lp] slippage (0.5%); a conversion on a $6.9M pool gets its own tighter bound
        data = X.swap_calldata(k, zfo, amount_raw, int(q * (1 - slip)), X.deadline(self.chain))
        X._send(self.chain, self.ac, X.cfg(self.chain)['universalrouter'], data, True, self.log, 'convert',
                dict(leg='treasury', to='ETH' if to_eth else 'USDG', amount_in=amount_raw, quote_out=q),
                value=amount_raw if not to_eth else 0)
        got = X.balance_net(self.chain, tout, self.ac.address) - b0
        mid = amount_raw / 1e6 / px * 1e18 if to_eth else amount_raw / 1e18 * px * 1e6
        bps = round(1e4 * (1 - got / mid), 1) if mid else 0.0
        usd = amount_raw / 1e6 if to_eth else amount_raw / 1e18 * px
        rec = dict(ts=time.time(), what='buy_eth' if to_eth else 'sell_eth', why=why, usd=round(usd, 4), eth=round((got if to_eth else amount_raw) / 1e18, 8),
                   px=round(px, 2), cost_bps=bps)
        self._rec(**{k: v for k, v in rec.items() if k != 'ts'})
        self.log('   treasury %s $%.2f at %.2f (%.1f bps under mid) - %s' % (rec['what'], usd, px, bps, why))
        self.st['bad'] = self.st.get('bad', 0) + 1 if bps > float(self.c.get('max_conv_bps', 100)) else 0
        if self.st['bad'] >= 2:
            self.st['stopped'] = 'two conversions in a row filled over %s bps under mid (last %.1f)' % (self.c.get('max_conv_bps', 100), bps)
            self.note('lp treasury STOPPED: %s - ETH copies skip until `lp treasury resume`' % self.st['stopped'])
        self._save()
        return got

    def buy(self, eth_raw, why):
        """Make sure `eth_raw` wei of ETH is there to spend ON TOP of what the wallet holds now: buy it with USDG (+0.3%
        buffer; unwind() returns what the mint does not take). Returns the reading to unwind against."""
        mark = self.eth()
        usdg_raw = int(eth_raw / 1e18 * self.px() * 1e6 * 1.003) + 1
        if usdg_raw > self.usdg():
            raise RuntimeError('treasury: $%.2f of USDG needed, $%.2f held' % (usdg_raw / 1e6, self.usdg() / 1e6))
        self._swap(True, usdg_raw, why)
        return mark

    def unwind(self, since, why):
        """Sell every wei of ETH that arrived since the reading `since` (net of gas) back into USDG. Returns USDG raw got."""
        extra = self.eth() - since
        if extra <= 0 or extra / 1e18 * self.px() < 0.05:            # under 5 cents: not worth a swap's gas
            return 0
        return self._swap(False, extra, why)

    def settle(self, closed=(), topup=True, hold=(), relay=None):
        """The beat: ETH above the band's top back to USDG; gas under the band's floor bought (topup); dust sold."""
        px = self.px()
        keep = float(self.c.get('gas_keep_usd', 15.0))
        eth_usd = X.balance(self.chain, X.NATIVE, self.ac.address) / 1e18 * px
        if eth_usd > keep + 2.0 and not self.stopped():
            self._swap(False, int((eth_usd - keep) / px * 1e18), 'ETH above the gas band ($%.2f > $%.0f)' % (eth_usd, keep))
        if topup:
            from core import io as cio
            from core.exec import refuel
            below = float(self.c.get('refuel_below_usd', 5.0))
            u0, e0 = self.usdg(), X.balance(self.chain, X.NATIVE, self.ac.address)
            refuel.keep(self.chain, self.ac, cio.Store(self.chain), cio.Telegram(self.chain), True,
                        cfg=dict(refuel_below=below / px, gas_floor=below / px / 4, clip_usd=float(self.c.get('refuel_clip_usd', 10.0))),
                        state='lp/refuel.json')
            spent = (u0 - self.usdg()) / 1e6
            if spent > 0.01:                                       # a refuel happened: in the record like any conversion
                got = (X.balance(self.chain, X.NATIVE, self.ac.address) - e0) / 1e18
                self._rec(what='refuel', why='gas under $%.0f' % below, usd=round(spent, 4), eth=round(got, 8), px=round(px, 2),
                          cost_bps=round(1e4 * (1 - got * px / spent), 1) if spent else 0.0)
        # DUST: a closed copy's token left in the wallet (a sell that failed after its burn, lp.exit keeps the token then)
        seen = set()
        for o in closed:
            t = (o.get('token') or '').lower()
            if not t or t in seen or t in hold or not o.get('key'):     # a held token belongs to its copy (settle_unsold)
                continue
            seen.add(t)
            amt = X.balance(self.chain, t, self.ac.address)
            if amt <= 0:
                continue
            q = X.quote(self.chain, o['key'], t == o['key']['currency0'].lower(), amt)
            usd = (q or 0) / 1e18 * px if X.is_native(o['key']['currency0']) else (q or 0) / 1e6
            self.log('   treasury: %s dust worth $%.2f%s' % (o.get('symbol') or t[:8], usd, '' if usd >= DUST_USD else ' (kept: under $%.2f)' % DUST_USD))
            if usd >= DUST_USD:
                e0, u0 = self.eth(), self.usdg()
                ok, r_ = X.sell_best(self.chain, self.ac, t, amt, [o['key']], px, relay, True, self.log, dict(leg='dust'), tries=1)  # the floor too
                if not ok:
                    self.log('   treasury: %s dust held (best $%s, fair $%s)' % (o.get('symbol') or t[:8], r_.get('usd'), r_.get('fair_usd')))
                    continue
                self.unwind(e0, 'dust of %s' % (o.get('symbol') or t[:8]))
                self.log('   treasury sold dust %s: +$%.2f' % (o.get('symbol') or t[:8], (self.usdg() - u0) / 1e6))

    def resume(self):
        self.st.update(stopped=None, bad=0)
        self._save()


def status(chain):
    """The treasury from its files (home reads the mirror): the last conversions, their cost, stopped or not."""
    rows = []
    if os.path.exists(log_path(chain)):
        for l in open(log_path(chain)):
            try:
                rows.append(json.loads(l))
            except Exception:
                pass
    st = json.load(open(state_path(chain))) if os.path.exists(state_path(chain)) else {}
    day = [r for r in rows if r['ts'] > time.time() - 86400]
    return dict(stopped=st.get('stopped'), n24=len(day), usd24=sum(r['usd'] for r in day),
                cost_bps24=(sum(r['cost_bps'] * r['usd'] for r in day) / sum(r['usd'] for r in day)) if day else None, last=rows[-10:])


def _selftest():
    """The accounting rule on a fake wallet: a copy funded in ETH costs the USDG that left for the ETH it kept."""
    class W:
        eth, usd = 10 ** 16, 100 * 10 ** 6
    t = Treasury.__new__(Treasury)
    t.c, t.st, t.log, t.note = {}, dict(stopped=None, bad=0), (lambda *a: None), (lambda *a: None)
    t.px = lambda: 2500.0
    t.eth = lambda: W.eth
    t.usdg = lambda: W.usd
    t._save = lambda: None

    def swap(to_eth, amt, why):
        if to_eth:
            W.usd -= amt
            got = int(amt / 1e6 / 2500.0 * 1e18 * 0.999)
            W.eth += got
        else:
            W.eth -= amt
            got = int(amt / 1e18 * 2500.0 * 1e6 * 0.999)
            W.usd += got
        return got
    t._swap = swap
    u0 = W.usd
    mark = t.buy(int(20 / 2500 * 1e18), 'test')                     # a $20 ETH copy
    W.eth -= int(19.9 / 2500 * 1e18)                                 # the mint took $19.90 of it
    t.unwind(mark, 'test')
    spent = (u0 - W.usd) / 1e6
    assert 19.9 < spent < 19.99, spent                               # what stayed in the position + both conversion fees
    t.st['stopped'] = 'x'
    t._swap = Treasury._swap.__get__(t)
    try:
        t.buy(10 ** 15, 'test')
        assert False, 'a stopped treasury must refuse'
    except RuntimeError as e:
        assert 'stopped' in str(e)
    return 2


if __name__ == '__main__':
    print('treasury selftest: %d asserts' % _selftest())
