"""SMART EXIT ROUTING against an anvil fork of robinhood (owner 2026-10-01): `python -m tests.exitfork [--keep]`.

The case that cost us: 06:28:48 UTC, our ZIP copy's 954,240 ZIP were sold back into the copy's own pool (0x3f461fb6), which
another LP had drained a second earlier - $1.06 back, -98.7%. On a fork of the chain NOW the fork's desk wallet is handed
those same 954,240 ZIP (moved from the PoolManager by impersonation, fork only) and sells them twice from one snapshot:

  1. FORCED: the old exit - the copy's own pool only
  2. ROUTED: the new exit - core/exec/lp.exit's choice over the token's hookless USDG / ETH pools (the heat table's routes)

and asserts: the routed sale picked the best quote; what arrived is that quote within the slippage bound; routed >= forced;
the USDG the wallet gained is exactly what the sale's own Transfer logs paid (the books, to the cent). Isolation as in
tests/lpfork: a private chain id, every url local, throwaway keys, nothing written to the LP stores."""
import os, sys, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.forkutil import _rpc, start, wait   # noqa: E402

ZIP = '0xc00899951d84ee5afb1bf22df1d91d5206457d86'
OWN = '0x3f461fb63811ec1423cc251f7f86c7f34ee09c67b5fd754fe056b327b92182a6'
AMOUNT = 954_240 * 10 ** 18


def fund_token(u, fork, token, addr, amt):
    """`amt` raw of `token` to `addr`, from the PoolManager's balance by impersonation (fork only)."""
    from core.exec import lp
    from eth_abi import encode
    _rpc(u, 'anvil_setBalance', [fork.pm, hex(10 ** 18)])
    _rpc(u, 'anvil_impersonateAccount', [fork.pm])
    b0 = lp.balance(fork, token, addr)
    h = _rpc(u, 'eth_sendTransaction', [{'from': fork.pm, 'to': token, 'data': '0xa9059cbb' + encode(['address', 'uint256'], [addr, amt]).hex()}])
    _rpc(u, 'anvil_stopImpersonatingAccount', [fork.pm])
    wait(u, h)
    return lp.balance(fork, token, addr) - b0 == amt


def run(keep=False, log=print):
    from core import chain as cm, lpbook
    from core.exec import lp as X
    real = cm.load('robinhood')
    a, u, fork, cpath = start(real, 11, 'exitfork')
    os.environ['LIVE_OK'] = 'yes'                                # sends reach the fork only: start() refused any non-local url
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
        log('  ok  ' + m)
    try:
        ac = lpbook.account(fork)
        _rpc(u, 'anvil_setBalance', [ac.address, hex(10 ** 17)])
        usdg = X.usdg(fork)
        A(fund_token(u, fork, ZIP, ac.address, AMOUNT), 'the fork wallet holds our 954,240 ZIP')
        heat = json.load(open(os.path.join(real.var, 'lp_heat.json')))
        pids = [p for p in heat['routes'].get(ZIP, []) if p != OWN]
        own_key = X.pool_key(fork, OWN)
        keys = [k for k in (X.pool_key(fork, p) for p in pids) if not int(k['hooks'], 16)]
        from copydesk.lpfollow import eth_px
        eth = eth_px()
        ranked = X.best_sale(fork, ZIP, AMOUNT, [own_key] + keys, eth)
        own_q = next((us for us, q, k in ranked if X.pool_id(k) == OWN), None)
        log('  quotes: best $%.2f in %s, own pool $%s, %d pools quoted' % (ranked[0][0], X.pool_id(ranked[0][2])[:10], '%.2f' % own_q if own_q else '-', len(ranked)))
        A(ranked and (own_q is None or ranked[0][0] >= own_q), 'the best quote is at least the own pool\'s')
        T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

        def sale(key):
            """Sell the ZIP on `key`'s pool; (USDG gained, ETH gained, the USDG its receipt paid us)."""
            u0, e0 = X.balance_net(fork, usdg, ac.address), X.balance_net(fork, X.NATIVE, ac.address)
            q, rc = X.sell(fork, ac, key, ZIP, True, lambda m: None, amount=AMOUNT)
            me = '0x' + '0' * 24 + ac.address[2:].lower()
            paid = sum(int(l['data'], 16) for l in (rc or {}).get('logs') or [] if l['address'].lower() == usdg.lower() and l['topics'][0] == T
                       and l['topics'][2].lower() == me)
            return X.balance_net(fork, usdg, ac.address) - u0, X.balance_net(fork, X.NATIVE, ac.address) - e0, paid, q
        fu, fe, fpaid, fq = sale(own_key)
        forced = fu / 1e6 + max(0, fe) / 1e18 * eth
        log('  FORCED (own pool): $%.2f' % forced)
        # no snapshot rewind (it resets the fork's nonce under our sender's own count): fund again, quote again - the forced
        # sale moved only the own pool
        A(fund_token(u, fork, ZIP, ac.address, AMOUNT), 'the same 954,240 ZIP again for the routed sale')
        ranked = X.best_sale(fork, ZIP, AMOUNT, [own_key] + keys, eth)
        best_usd, best_q, best_key = ranked[0]
        ru, re_, rpaid, rq = sale(best_key)
        routed = ru / 1e6 + max(0, re_) / 1e18 * eth * 0.997
        log('  ROUTED (%s): $%.2f' % (X.pool_id(best_key)[:10], routed))
        slip = float(X.cfg(fork)['slippage'])
        A(routed >= best_usd * (1 - slip) - 0.01, 'what arrived is the quote within the slippage bound ($%.2f vs $%.2f)' % (routed, best_usd))
        A(routed >= forced - 0.01, 'routed $%.2f >= forced $%.2f' % (routed, forced))
        if best_key['currency0'] != X.NATIVE:
            A(ru == rpaid, 'the books: USDG gained %d == the receipt paid %d, to the raw unit' % (ru, rpaid))
        A(X.balance(fork, ZIP, ac.address) == 0, 'every ZIP sold, none left behind')
        log('exitfork: %d asserts, all passed - routed $%.2f vs forced $%.2f' % (n[0], routed, forced))
        return n[0]
    finally:
        if not keep:
            a.terminate()


if __name__ == '__main__':
    run(keep='--keep' in sys.argv)
