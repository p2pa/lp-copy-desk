"""Keys and balances. Keys live in chain.wallet_file (mode 600, gitignored, the same wallets.json both
old repos already use - hook_bot points at it by path and never copies a key). Balances know the chain's
gas token: on Arc the native balance (18-dec) and the ERC-20 face (6-dec) are ONE balance 1e12 apart, and
`usd_balance` reads both and raises if they disagree beyond rounding - the exact 1e12 trap CLAUDE.md
warns about, which reads as "the wallet is broke" instead of "the code is wrong"."""
import json
from eth_account import Account

BALANCEOF = '0x70a08231'


def account(chain, name=None):
    name = name or chain.caller
    w = json.load(open(chain.wallet_file))
    if name not in w:
        raise SystemExit('wallet %s missing in %s' % (name, chain.wallet_file))
    if not w[name].get('key'):
        # WATCH-ONLY: an address without its key (a dry desk on a box that must never sign, e.g. the AWS rehearsal
        # before the robinhood move, docs/MIGRATION-AWS.md). Gates eth_call from it; any signature attempt fails.
        from eth_utils import to_checksum_address
        return type('WatchOnly', (), {'address': to_checksum_address(w[name]['address']), 'key': None})()
    return Account.from_key(w[name]['key'])


def pot_usd(chain, address, node=None):
    """Everything an address holds that this chain calls money: the gas token plus every configured quote.

    usd_balance() reads the NATIVE balance, which is the whole story on arc and none of it on robinhood -
    the CycleArb there held 73.90 USDG and 0.0 ETH, so a fill card saying "balance 0.00" would have been
    true and useless. Unreadable tokens are skipped, never counted as zero."""
    node = node or chain.node
    total = 0.0
    try:
        total += usd_balance(chain, address, node)
    except Exception:
        pass
    for tok in chain.quote_tokens():
        if int(tok, 16) == 0 or (chain.gas_token == 'usdc' and tok.lower() == (chain.weth or '').lower()):
            continue                      # native already counted; arc's USDC face IS the native balance
        try:
            raw = node.call(tok, BALANCEOF + address[2:].lower().rjust(64, '0'))
            if raw and raw != '0x':
                total += chain.to_usd(int(raw, 16), tok)
        except Exception:
            continue
    return round(total, 6)


def usd_balance(chain, address, node=None):
    """The wallet's spendable value in USD-equivalent of the gas token.
    Arc: native 18-dec must equal the ERC-20 face 6-dec (1e12 apart) or raise.
    Robinhood: native ETH * eth_usd."""
    node = node or chain.node
    native = node.getbalance(address, 'latest') / 10 ** chain.native_dec
    if chain.gas_token == 'usdc':
        sel = BALANCEOF + address[2:].lower().rjust(64, '0')
        face = int(node.call(chain.weth, sel)[2:], 16) / 10 ** 6
        if abs(native - face) > max(1e-6, native * 1e-6):
            raise RuntimeError('balance split: eth_getBalance %.6f vs balanceOf %.6f for %s (should be the '
                               'same balance, 1e12 apart)' % (native, face, address))
        return native   # $ == USDC
    return native * chain.eth_usd


def native_balance(chain, address, node=None):
    node = node or chain.node
    return node.getbalance(address, 'latest') / 10 ** chain.native_dec
