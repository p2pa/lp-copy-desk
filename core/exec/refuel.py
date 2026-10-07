"""Keep the wallet's gas floor. This is chain-agnostic in the CORE (the runner calls refuel.keep() every
so often regardless of chain); the MODE is chain.refuel.mode:

  none  - Arc. Gas IS the quote token (USDC == native face), so profit and gas are one asset and there is
          nothing to convert. keep() is a no-op.
  swap  - Robinhood. Gas is ETH, profit is USDG, so the desk sells a clip of USDG for WETH and unwraps to
          ETH when the balance dips under the floor. This is the ONE thing in the bot that takes price
          risk (a router minOut, not a Solidity require over a flash-accounted cycle), so it is capped in
          size and stops for good after two realisations under bad_bps. Ported from hook_sniper.refuel.

Only the mode differs; the runner does not know which chain it is on."""
from Crypto.Hash import keccak as _kc
from eth_utils import to_checksum_address
from . import tx, wallet

BALANCEOF = '0x70a08231'
ALLOWANCE = '0xdd62ed3e'
APPROVE = '0x095ea7b3'


def _sel(sig):
    k = _kc.new(digest_bits=256); k.update(sig.encode()); return '0x' + k.hexdigest()[:8]


EXACT_IN = _sel('exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))')
WITHDRAW = _sel('withdraw(uint256)')


def _p(x):
    if isinstance(x, str):
        return '0' * 24 + x[2:].lower()
    return '%064x' % x


def keep(chain, ac, store, tg, go, cfg=None, state='refuel.json'):
    """Top the wallet's gas back up if it is under the floor. No-op unless mode == 'swap'. cfg / state: another wallet's
    own settings and state file on the same chain (the copy LP desk's lp-1: lpfollow)."""
    cfg = dict(chain.refuel, **(cfg or {}))
    if cfg.get('mode') != 'swap':
        return
    st = store.load(state, {'refuels': [], 'stopped': None})
    if st.get('stopped'):
        return
    node = chain.node
    bal = node.getbalance(ac.address, 'latest')
    # refuel_below tops up BEFORE the send floor bites: a 30M-gas gate needs gas x price in the wallet, far
    # above what one send costs. Defaults to gas_floor, the old behaviour.
    if bal / 1e18 >= float(cfg.get('refuel_below', cfg['gas_floor'])):
        return
    quote = cfg['quote_token']; router = cfg['router']; weth = chain.weth
    have = int(node.call(quote, BALANCEOF + _p(ac.address))[2:], 16)
    if have < 5 * 10 ** 6:
        print('   refuel skipped: %.6f ETH, only $%.2f quote' % (bal / 1e18, have / 1e6))
        if not st.get('dry_paged'):
            # nothing left to sell: only a human (or `exec bridge`) can fix this, so say it once
            st['dry_paged'] = True
            store.save(state, st)
            tg.send('REFUEL DRY: %.6f ETH left, $%.2f quote to sell - bridge gas in (exec bridge)'
                    % (bal / 1e18, have / 1e6), tag=chain.name)
        return
    amt = min(int(float(cfg.get('clip_usd', 20)) * 10 ** 6), have)
    allow = int(node.call(quote, ALLOWANCE + _p(ac.address) + _p(router))[2:], 16)
    if allow < amt:
        if not go:
            print('   refuel needs a one-time approval first; re-run with --go')
            return
        print('   refuel: approving quote->router (one time)')
        ra = tx.send(chain, ac, {'to': to_checksum_address(quote),
                                 'data': APPROVE + _p(router) + _p(2 ** 256 - 1), 'value': 0, 'gas': 80000}, go)
        if not (ra and int(ra['status'], 16) == 1):
            print('   refuel: approval FAILED')
            return
    quote_data = EXACT_IN + _p(quote) + _p(weth) + _p(100) + _p(ac.address) + _p(amt) + _p(0) + _p(0)
    try:
        exp = int(node.call(router, quote_data, frm=ac.address)[2:], 16)
    except Exception as e:
        print('   refuel quote failed: %s' % str(e)[:90])
        return
    min_out = exp * int(cfg.get('min_bps', 9900)) // 10000
    print('   REFUEL: %.6f ETH left, selling $%.2f, quote %.6f WETH, minOut %.6f'
          % (bal / 1e18, amt / 1e6, exp / 1e18, min_out / 1e18))
    if not go:
        return
    swap_data = EXACT_IN + _p(quote) + _p(weth) + _p(100) + _p(ac.address) + _p(amt) + _p(min_out) + _p(0)
    rc = tx.send(chain, ac, {'to': to_checksum_address(router), 'data': swap_data, 'value': 0, 'gas': 400000}, go)
    ok = bool(rc) and int(rc['status'], 16) == 1
    got = 0
    if ok:
        got = int(node.call(weth, BALANCEOF + _p(ac.address))[2:], 16)
        tx.send(chain, ac, {'to': to_checksum_address(weth), 'data': WITHDRAW + _p(got), 'value': 0, 'gas': 80000}, go)
    realised = round((got / exp) * 10000, 1) if (ok and exp) else 0.0
    st['refuels'].append(dict(when=None, usd=amt / 1e6, quote_weth=exp / 1e18, got_weth=got / 1e18,
                              realised_bps=realised, tx=(rc or {}).get('transactionHash'), ok=ok))
    bad = [x for x in st['refuels'][-2:] if x['realised_bps'] < int(cfg.get('bad_bps', 9850))]
    if len(bad) == 2 and len(st['refuels']) >= 2:
        st['stopped'] = 'two consecutive refuels under bad_bps'
        tg.send('REFUEL STOPPED: ' + st['stopped'], tag=chain.name)
    st['dry_paged'] = False if ok else st.get('dry_paged')
    store.save(state, st)
    print('   refuel %s: %.2f%%' % ('OK' if ok else 'FAILED', realised / 100.0))
