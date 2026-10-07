"""The COPY DESK end to end against an anvil fork of robinhood, before a real wei moves: `python -m tests.copyfork [--keep]`.

The real desk code (copydesk/lpfollow.Follow, poll -> mint -> room -> entry -> exit) runs against the fork's real
PositionManager / UniversalRouter / PoolManager bytecode. A throwaway LEADER wallet opens positions; the desk must copy
them through its own poll, the same path it runs live:
  1. a USDG-only bid in a busy USDG pool -> a copy sized max(1/5 of the book, free/size_free_div)
  2. a two-sided position in a busy native-ETH pool -> the treasury buys the ETH, the mint sends it as value and sweeps
     the rest back, the unwind returns the buffer: the copy's cost is the USDG that left, the ETH band is where it was
  3. an ETH-only bid in the same pool (quote_only, native)
  4. value_of prices every copy in dollars within reason of its cost; each copy says what it holds (holds_label)
  5. THE TIERS: with the book full, a MAIN mint closes the newest FILLER copy; a filler mint never closes anything
  6. a stray token in the wallet is NOT sold by an exit (only what the exit received), and treasury.settle sells it as dust
  7. the leader exits everything -> the desk's poll sees each exit and closes the copy; ETH comes back as USDG
  8. THE BOOKS: USDG now - USDG at start == the copies' booked P&L + the treasury's settle/dust proceeds, to a cent

Isolation (tests/lpfork.start): private chain id (real + 1,000,000), every url 127.0.0.1, throwaway keys for every
wallet the config names, funds moved by impersonation on the fork only. The LP db is never written (desk.remote)."""
import collections, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.forkutil import _rpc, start, fund_usdg   # noqa: E402

SWAP = '0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f'


def busy_pools(src, pm, blocks=1000):             # the official rpc caps an answer at 10,000 logs
    """(usdg pool key, native pool key): the busiest unhooked pool of each kind over the last `blocks` blocks."""
    from copydesk import lpcopy as C, lpfollow as L
    head = int(_rpc(src, 'eth_blockNumber', []), 16)
    logs = _rpc(src, 'eth_getLogs', [dict(address=pm, fromBlock=hex(head - blocks), toBlock=hex(head), topics=[SWAP])])
    n = collections.Counter(l['topics'][1] for l in logs)
    keys = C.keys([p for p, _ in n.most_common(200)])
    u = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
    # the desk refuses pools under [lp.follow] min_pool_age_h (2 h, since 2026-10-04): test on a pool that swapped ~4 h ago
    # (Robinhood ~10 blocks a second: 144,000 blocks), or the copy is refused by design and the test reads it as a failure
    old = lambda p: bool(_rpc(src, 'eth_getLogs', [dict(address=pm, fromBlock=hex(head - 147000), toBlock=hex(head - 144000), topics=[SWAP, p])]))
    best = {}
    for p, _ in n.most_common(200):
        k = keys.get(p)
        if not k or int(k[2], 16) or not int(k[1], 16):
            continue                                                  # hooked, or an all-zero key (not a PositionManager pool)
        kind = 'eth' if int(k[0], 16) == 0 and k[1] != u else 'usdg' if u in k[:2] and int(k[0], 16) else None
        if kind and kind not in best and not L.uncopyable(k[0], k[1], k[2], p, u) and old(p):
            best[kind] = p
    return best.get('usdg'), best.get('eth')


def run(keep=False, log=print):
    from eth_account import Account
    from core import chain as cm
    from core.exec import lp as X
    from copydesk import lpfollow as L, treasury as TR
    real = cm.load('robinhood')
    os.environ.setdefault('LPFORK_URL', 'https://rpc.mainnet.chain.robinhood.com')   # rh-node is gone (2026-10-01); blockmachine 429s anvil's start
    a, u, fork, cpath = start(real, 8, 'copyfork')
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
        log('  ok  %s' % m)
    try:
        os.environ['LIVE_OK'] = 'yes'
        # the desk's reads go to the FORK (lpfollow reads through its own node client, not chain.node)
        L.NODE, L.NODE_WS = u, u.replace('http', 'ws')
        L._node.update(ok=True, checked=1e18, lag=0, thread=True)      # pinned fresh: no background check against the real chain
        L._conn.c = None
        L.NODE_FALLBACK = []                                           # a fork never falls back to mainnet reads
        L._px.update(v=None, t=0.0)
        pm = fork.pm
        pu, pe = busy_pools(os.environ['LPFORK_URL'], pm)
        A(pu and pe, 'a busy USDG pool %s and a busy native-ETH pool %s to test on' % ((pu or '-')[:10], (pe or '-')[:10]))
        ku, ke = X.pool_key(fork, pu), X.pool_key(fork, pe)
        A(X.is_native(ke['currency0']) and X.quote_of(fork, ke) == X.NATIVE and X.quote_of(fork, ku) == X.usdg(fork), 'quote_of')

        # --- wallets: the desk's (throwaway lp-1 of the fork config) and a leader + a filler wallet
        from core import lpbook
        me = lpbook.account(fork)
        lead, fill = Account.create(), Account.create()
        usdg = X.usdg(fork)
        for w, eth, usd in ((me, 10 ** 16, 300), (lead, 2 * 10 ** 18, 1000), (fill, 10 ** 18, 500)):
            _rpc(u, 'anvil_setBalance', [w.address, hex(eth)])
            A(fund_usdg(u, fork, w.address, usd * 10 ** 6), 'funded %s with $%d USDG' % (w.address[:8], usd))
        fork.refresh_eth_usd()

        # --- the desk, pointed at our test wallets
        cfg = fork.cfg['lp']['follow']
        cfg.update(leader=[lead.address.lower()], bench=[], filler=[fill.address.lower()], native=True, reserve=0,
                   size_free_div=2.0, max_open=3, min_leader_usd=5.0, gas_keep_usd=15.0, hours={})
        cfg.pop('hours', None)
        notes = []
        d = L.Follow(fork, go=True, log=lambda m: None, note=lambda *x, **k: notes.append(x))
        d.remote = True                                                # never write the real LP db from a fork
        A(d.leaders == [lead.address.lower()] and d.filler == [fill.address.lower()], 'the desk follows the test wallets')
        d.poll()                                                       # first poll: next_id = now
        u0, e0 = X.balance(fork, usdg, me.address), X.balance_net(fork, X.NATIVE, me.address)
        tr_usd = [0.0]

        def mint_as(w, key, kind, usd):
            sp = X.slot0(fork, key)[0]
            ts = key['tickSpacing']
            q = X.quote_of(fork, key)
            px = d.tr.px() if X.is_native(q) else 1.0
            raw = int(usd / px * (1e18 if X.is_native(q) else 1e6))
            tok = key['currency1'] if key['currency0'].lower() == q else key['currency0']
            if kind == 'bid':
                tl, tu = X.quote_only_ticks(sp, ts, key['currency0'].lower() == q, rng=1.3)
                tid, _ = X.mint_quote_only(fork, w, key, tl, tu, raw, True, lambda m: None)
            else:
                tl, tu = X.tick_range(sp, ts, 1.5)
                r = X.enter(fork, w, dict(key=key, token=tok, pool=X.pool_id(key), tick_lower=tl, tick_upper=tu), raw, True, lambda m: None)
                tid = r['token_id']
            return tid, dict(pool=X.pool_id(key), key=key, token=tok, token_id=tid, tick_lower=tl, tick_upper=tu)

        # 1. USDG bid from the MAIN leader
        t1, p1 = mint_as(lead, ku, 'bid', 40)
        d.poll()
        o1 = d.st['ours'].get(str(t1))
        A(o1 and o1.get('token_id'), 'the desk copied the USDG bid #%s -> ours #%s' % (t1, (o1 or {}).get('token_id')))
        book = u0 / 1e6
        A(o1['quote'] == 'usdg' and abs(o1['size_usd'] - book / 2) < book * 0.02, 'idle book: the copy is half the free capital ($%.2f of $%.2f)' % (o1['size_usd'], book))
        owner = _rpc(u, 'eth_call', [{'to': L.POSM, 'data': '0x6352211e%064x' % o1['token_id']}, 'latest'])
        A(int(owner, 16) == int(me.address, 16), 'the desk owns its copy')

        # 1b. a two-sided USDG copy whose mint reverts after its swap (2026-10-02 V4: the pool moved 13% between our swap
        # and the mint, MaximumAmountExceeded): the token it bought is sold back, the cost is on the error record, nothing booked
        tokb = X.balance(fork, p1['token'], me.address)
        mc = X.mint_calldata
        tf, _ = mint_as(lead, ku, 'range', 40)
        X.mint_calldata = lambda key, tl, tu, liq, a0, a1, *a: mc(key, tl, tu, liq * 2, a0, a1, *a)
        try:
            d.poll()
        finally:
            X.mint_calldata = mc
        er = json.loads(open(L._log_path(fork)).read().strip().splitlines()[-1])
        A(str(tf) not in d.st['ours'] and er.get('ev') == 'error' and er.get('undo') == 'sold back',
          'a failed mint books nothing and sells back what its swap bought: %s' % er.get('why'))
        A(X.balance(fork, p1['token'], me.address) <= tokb, 'no stray token after the failed entry')
        failed = er.get('lost_usd') or 0.0
        A(0 <= failed < 0.1 * 40, 'the failed entry cost $%.4f (fees both ways), on the record' % failed)

        # 2. a two-sided ETH-pool position from the leader: the treasury funds it
        eb = X.balance_net(fork, X.NATIVE, me.address)
        t2, p2 = mint_as(lead, ke, 'range', 40)
        d.poll()
        o2 = d.st['ours'].get(str(t2))
        A(o2 and o2.get('token_id') and o2['quote'] == 'eth', 'the desk copied the ETH-pool position #%s as an ETH copy' % t2)
        A(abs(X.balance_net(fork, X.NATIVE, me.address) - eb) < 0.02 * o2['size_usd'] / d.tr.px() * 1e18,
          'the ETH band is where it was: what the mint did not take was unwound (%.6f ETH moved)' % ((X.balance_net(fork, X.NATIVE, me.address) - eb) / 1e18))
        tl = [json.loads(l) for l in open(TR.log_path(fork))]
        A(any(r['what'] == 'buy_eth' for r in tl) and all(r['cost_bps'] < 100 for r in tl), 'treasury conversions filled within 100 bps of mid: %s' % [r['cost_bps'] for r in tl])

        # 3. an ETH-only bid (native quote-only)
        t3, p3 = mint_as(lead, ke, 'bid', 25)
        d.poll()
        o3 = d.st['ours'].get(str(t3))
        A(o3 and o3.get('token_id') and o3['quote'] == 'eth' and o3['quote_only'], 'the desk copied the ETH-only bid #%s' % t3)

        # 4. value and side
        v = L.value_of(d, [o1, o2, o3])
        for o in (o1, o2, o3):
            x = v[o['token_id']]
            w = x['principal'] + x['fees']
            A(0.9 * o['size_usd'] < w < 1.1 * o['size_usd'], '%s valued at $%.2f against $%.2f in, holds %s' % (L.pair(o), w, o['size_usd'], L.holds_label(x, o)))
        A(L.holds_label(v[o1['token_id']], o1) == 'USDG' and '/' in L.holds_label(v[o2['token_id']], o2) and L.holds_label(v[o3['token_id']], o3) == 'ETH',
          'holds: %s / %s / %s' % tuple(L.holds_label(v[o['token_id']], o) for o in (o1, o2, o3)))

        # 5. THE TIERS: the book is full (3 slots); a filler mint is refused, then a main mint closes the newest filler copy
        d.c['max_open'] = 4
        A(fund_usdg(u, fork, me.address, 200 * 10 ** 6), 'a $200 deposit: the free/2 sizing had put the book to work (%.0f%% deployed)' % (
            100 * sum(o['size_usd'] for o in (o1, o2, o3)) / book))
        u0 += 200 * 10 ** 6
        last = lambda: json.loads(open(L._log_path(fork)).read().strip().splitlines()[-1])
        t4, p4 = mint_as(fill, ku, 'bid', 30)
        d.poll()
        o4 = d.st['ours'].get(str(t4))
        A(o4 and o4.get('token_id'), 'a FILLER copy fills the 4th slot (%s)' % ('' if o4 else last()))
        t5, p5 = mint_as(fill, ku, 'bid', 30)
        d.poll()
        A(str(t5) not in d.st['ours'] and all(not d.st['ours'][str(t)].get('closed') for t in (t1, t2, t3, t4)),
          'a second filler copy finds no room and closes nothing: %s' % last().get('why'))
        t6, p6 = mint_as(lead, ku, 'bid', 30)
        d.poll()
        A(d.st['ours'][str(t4)].get('closed') and d.st['ours'].get(str(t6), {}).get('token_id'),
          'a MAIN mint closed the filler copy to make room and was copied')

        # 6. a stray token is not an exit's return
        tok = p2['token']
        b0 = X.balance(fork, tok, lead.address)
        X.swap(fork, lead, ke, X.NATIVE, int(10 / d.tr.px() * 1e18), True, lambda m: None)   # the leader buys $10 of it
        amt = X.balance(fork, tok, lead.address) - b0
        from eth_abi import encode
        h = X._send(fork, lead, tok, '0xa9059cbb' + encode(['address', 'uint256'], [me.address, amt]).hex(), True, lambda m: None, 'stray')
        stray = X.balance(fork, tok, me.address)
        A(stray >= amt, 'a stray %s sits in the desk wallet' % o2['symbol'])

        # 7. the leader exits everything; the desk follows
        for p in (p1, p2, p3, p6):
            X.exit(fork, lead, p, True, lambda m: None)
        X.exit(fork, fill, p5, True, lambda m: None)
        # THE WEBSOCKET EXIT PATH: the listener heard the leader's ModifyLiquidity; the main loop's drain() closes the copies
        d.drain(time.time() + 8)
        closes = [json.loads(l) for l in open(L._log_path(fork)) if '"ev": "close"' in l]
        by_exit = [e for e in closes if str(e.get('leader_tid')) in {str(t) for t in (t1, t2, t3, t6)}]
        A(len(by_exit) == 4 and all(e.get('path') == 'ws' for e in by_exit),
          'all 4 leader exits closed by the websocket path: %s' % [e.get('path') for e in by_exit])
        d.poll()                                                       # the backstop for anything the socket missed
        opn = [o for o in d.st['ours'].values() if not o.get('closed')]
        A(not opn, 'every copy closed after its leader: %s' % [L.pair(o) for o in opn])
        A(X.balance(fork, tok, me.address) >= stray, 'the exit sold only what it received: the stray is still there')
        for o in d.st['ours'].values():
            A(abs(o['pnl_usd']) < 0.1 * o['size_usd'] + 1, '%s booked %+.2f on $%.2f (held seconds on a fork)' % (L.pair(o), o['pnl_usd'], o['size_usd']))

        # 8. the treasury sells the dust and the ETH above the band; THE BOOKS
        ub = X.balance(fork, usdg, me.address)
        d.tr.log = log
        d.tr.settle([o for o in d.st['ours'].values() if o.get('closed')], topup=False)
        tr_usd[0] = (X.balance(fork, usdg, me.address) - ub) / 1e6
        A(X.balance(fork, tok, me.address) == 0, 'treasury.settle sold the dust')
        booked = sum(o['pnl_usd'] for o in d.st['ours'].values())
        moved = (X.balance(fork, usdg, me.address) - u0) / 1e6
        A(abs(moved - booked - tr_usd[0] + failed) < 0.02, 'THE BOOKS: USDG moved %+.4f = booked %+.4f + treasury %+.4f - failed entry %.4f' % (moved, booked, tr_usd[0], failed))
        A(not d.tr.stopped(), 'the treasury is running')
        opens = [json.loads(l) for l in open(L._log_path(fork)) if '"ev": "open"' in l]
        A(all('heard_ms' in e and 'prep_ms' in e and 'entry_ms' in e for e in opens),
          'every open carries its timings: %s' % [(e.get('path'), e.get('heard_ms'), e.get('prep_ms'), e.get('entry_ms')) for e in opens][:3])
        log('copyfork: %d asserts, all passed' % n[0])
        return 0
    finally:
        os.environ.pop('LIVE_OK', None)
        if not keep:
            a.terminate()


if __name__ == '__main__':
    sys.exit(run(keep='--keep' in sys.argv))
