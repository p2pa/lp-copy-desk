"""AN LP WALLET'S POSITIONS, AS LP AGENT SHOWS THEM - from the chain (Uniswap v4 on robinhood).

  python -m copydesk.lpportfolio <address> [--days 3] [--json]
  python -m copydesk.lpportfolio check                   vs copydesk/fixtures/lpagent_0x78a1003e.json (LP Agent's own view)

Per position NFT of the v4 PositionManager owned by the address:
- the pool and every liquidity change: PoolManager ModifyLiquidity rows whose salt is the tokenId (the helper contracts some
  wallets open through - 0x56044b4f... - never hide it: the pool still logs the salt);
- principal of each change from the liquidity math at the pool's price just before it (StateView.getSlot0 at block - 1);
- fees = what the close / decrease actually paid out (ERC-20 Transfers PoolManager -> owner) minus that principal;
- USD: USDG = 1, the other token at the pool's own price; ETH = USD / ETH price from the deepest v3 WETH/USDG pool at the block.
Invested = deposits in ETH at open; fee % and PnL % in USD terms (LP Agent's fee % reproduces exactly that way).
"""
import collections, json, math, os, sys, time, urllib.request

from eth_utils import keccak
from discovery import forensics as F

POSM = '0x58daec3116aae6d93017baaea7749052e8a04fa7'
SV = '0xF3334192D15450CdD385c8B70e03f9A6bD9E673b'
MOD = '0x' + keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
INIT = '0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'
XFR = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
WETH = '0x0bd7d308f8e1639fab988df18a8011f41eacad73'
ETHPOOL = '0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca'      # deepest v3 WETH/USDG (fee 100), 2026-09-28
AR = ['https://rpc-robinhood.blockmachine.io', 'https://robinhood.rpc.blxrbdn.com']
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'lpagent_0x78a1003e.json')


_CACHE_F = os.path.join(os.path.dirname(F.INDEX), 'lpportfolio_rpc.json')
_CACHE = None


def rpc(m, p):
    """Archive read with retries across both endpoints; eth_call / receipts at fixed blocks are cached on disk."""
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.load(open(_CACHE_F))
        except Exception:
            _CACHE = {}
    k = m + json.dumps(p, sort_keys=True)
    cacheable = m in ('eth_getTransactionReceipt', 'eth_getTransactionByHash') or (m == 'eth_call' and p[1] != 'latest')
    if cacheable and k in _CACHE:
        return _CACHE[k]
    for i in range(12):
        try:
            q = urllib.request.Request(AR[i % 2], json.dumps(dict(jsonrpc='2.0', id=1, method=m, params=p)).encode(),
                                       {'Content-Type': 'application/json', 'User-Agent': 'x'})
            r = json.loads(urllib.request.urlopen(q, timeout=20).read())
            if 'result' in r and r['result'] is not None:
                if cacheable:
                    _CACHE[k] = r['result']
                    if len(_CACHE) % 50 == 0:
                        json.dump(_CACHE, open(_CACHE_F + '.tmp', 'w')); os.replace(_CACHE_F + '.tmp', _CACHE_F)
                return r['result']
            if 'error' in r and 'revert' in str(r['error']).lower():
                return None
        except Exception:
            pass
        time.sleep(min(8, 0.5 * 2 ** (i // 2)))
    raise RuntimeError('archive read failed: %s %s' % (m, str(p)[:120]))


def save_cache():
    if _CACHE:
        json.dump(_CACHE, open(_CACHE_F + '.tmp', 'w')); os.replace(_CACHE_F + '.tmp', _CACHE_F)


def _sel(s):
    return keccak(text=s)[:4].hex()


def _s(h, bits=256):
    v = int(h, 16)
    return v - (1 << bits) if v >> (bits - 1) else v


_eth = {}


def eth_usd(blk):
    if blk not in _eth:
        r = rpc('eth_call', [dict(to=ETHPOOL, data='0x' + _sel('slot0()')), hex(blk)])
        sp = int(r[2:66], 16) / 2 ** 96
        # token0 = WETH (0x0bd7) < USDG (0x5fc5): price = USDG per WETH = sp^2 * 1e18 / 1e6
        _eth[blk] = sp * sp * 1e18 / 1e6
    return _eth[blk]


def slot0(pid, blk):
    r = rpc('eth_call', [dict(to=SV, data='0x' + _sel('getSlot0(bytes32)') + pid[2:]), hex(blk)])
    return int(r[2:66], 16) / 2 ** 96 if r else None


def amounts(L, sp, tl, tu):
    sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
    if sp <= sa:
        return L * (sb - sa) / (sa * sb), 0.0
    if sp >= sb:
        return 0.0, L * (sb - sa)
    return L * (sb - sp) / (sp * sb), L * (sp - sa)


_dec = {}


def decimals(t):
    if t not in _dec:
        r = rpc('eth_call', [dict(to=t, data='0x' + _sel('decimals()')), 'latest'])
        _dec[t] = int(r, 16) if r and r != '0x' else 18
    return _dec[t]


def portfolio(owner, days=3, only=None):
    owner = owner.lower()
    key = F._key()
    head = int(F._rpc('eth_blockNumber', []), 16)
    frm = head - int(days * 864000)
    ot = '0x' + owner[2:].rjust(64, '0')
    # 1. the position NFTs the owner received or burned in the window
    nft, bt = {}, {}
    body = dict(from_block=frm, logs=[dict(address=[POSM], topics=[[XFR], [], [ot]]), dict(address=[POSM], topics=[[XFR], [ot]])],
                field_selection=dict(log=['block_number', 'topic1', 'topic2', 'topic3', 'transaction_hash'], block=['number', 'timestamp']))
    for r in F._pages(body, key):
        for d in r['data']:
            for b in d.get('blocks') or []:
                bt[b['number']] = int(b['timestamp'], 16) if isinstance(b['timestamp'], str) else b['timestamp']
            for lg in d.get('logs') or []:
                tid = int(lg['topic3'], 16)
                x = nft.setdefault(tid, dict(tid=tid))
                if lg['topic2'][-40:] == owner[2:] and int(lg['topic1'], 16) == 0:
                    x['mint_blk'], x['mint_tx'] = lg['block_number'], lg['transaction_hash']
                if lg['topic1'][-40:] == owner[2:] and int(lg['topic2'], 16) == 0:
                    x['burn_blk'], x['burn_tx'] = lg['block_number'], lg['transaction_hash']
    out = []
    for tid, x in sorted(nft.items()):
        if 'mint_tx' not in x or (only and tid not in only):
            continue
        rc = rpc('eth_getTransactionReceipt', [x['mint_tx']])
        mrow = next((l for l in rc['logs'] if l['topics'][0] == MOD and l['data'][-64:] == '%064x' % tid), None)
        if not mrow:
            continue
        pid = mrow['topics'][1]
        # 2. every liquidity change of this position: the pool's ModifyLiquidity rows with salt = tokenId
        rows = []
        stop = (x.get('burn_blk') or head) + 1
        for r in F._pages(dict(from_block=x['mint_blk'], to_block=stop, logs=[dict(address=[F.PM], topics=[[MOD], [pid]])],
                               field_selection=dict(log=['block_number', 'log_index', 'transaction_hash', 'data'], block=['number', 'timestamp'])),
                          key, stop=stop):
            for d in r['data']:
                for b in d.get('blocks') or []:
                    bt[b['number']] = int(b['timestamp'], 16) if isinstance(b['timestamp'], str) else b['timestamp']
                rows += [l for l in d.get('logs') or [] if l['data'][-64:] == '%064x' % tid]
        rows.sort(key=lambda l: (l['block_number'], l['log_index']))
        # the pool's currencies: PositionManager.getPoolAndPositionInfo(tokenId) at the block before the burn (a burned NFT reverts)
        r = rpc('eth_call', [dict(to=POSM, data='0x' + _sel('getPoolAndPositionInfo(uint256)') + '%064x' % tid),
                             hex((x.get('burn_blk') or head + 1) - 1)])
        if not r or len(r) < 2 + 64 * 5:
            continue
        c0, c1 = '0x' + r[2 + 24:66].lower(), '0x' + r[66 + 24:130].lower()
        if USDG not in (c0, c1):
            continue                                        # USDG pairs first (the reference wallet); ETH pairs next
        d0, d1 = decimals(c0), decimals(c1)
        inv_usd = inv_eth = fee_usd = out_usd = 0.0
        L = 0
        back = collections.Counter()
        tl = tu = None
        last_px = None
        for l in rows:
            dd = l['data'][2:]
            tl, tu, dl = _s(dd[0:64]), _s(dd[64:128]), _s(dd[128:192])
            blk = l['block_number']
            sp = slot0(pid, blk - 1)
            a0, a1 = amounts(abs(dl), sp, tl, tu)
            p1_in_0 = sp * sp * 10 ** d0 / 10 ** d1               # token1 per token0, human units
            usd = lambda x0, x1: (x0 / 10 ** d0 + x1 / 10 ** d1 / p1_in_0) if c0 == USDG else (x1 / 10 ** d1 + x0 / 10 ** d0 * p1_in_0)
            if dl > 0:
                inv_usd += usd(a0, a1)
                inv_eth += usd(a0, a1) / eth_usd(blk - 1)
                L += dl
            else:
                # a decrease (dl < 0) pays principal + fees; a zero-delta row is a fee COLLECTION (principal 0)
                if dl == 0:
                    a0 = a1 = 0.0
                rc2 = rpc('eth_getTransactionReceipt', [l['transaction_hash']])
                got = collections.Counter()
                for t in rc2['logs']:
                    if t['topics'][0] == XFR and len(t['topics']) == 3 and t['topics'][1][-40:] == F.PM[2:] and t['topics'][2][-40:] == owner[2:]:
                        got[t['address'].lower()] += int(t['data'], 16)
                g0, g1 = got.get(c0, 0), got.get(c1, 0)
                fee_usd += usd(max(0.0, g0 - a0), max(0.0, g1 - a1))
                out_usd += usd(g0, g1)
                back[c0] += g0
                back[c1] += g1
                L += dl
        t_open, t_close = bt.get(x['mint_blk']), bt.get(x.get('burn_blk'))
        age_h = ((t_close or time.time()) - t_open) / 3600 if t_open else None
        row = dict(tokenId=tid, pool=pid, pair='%s/%s' % (c0[:8], c1[:8]), closed=bool(x.get('burn_blk')), age_h=age_h,
                   invested_eth=inv_eth, invested_usd=inv_usd, fee_usd=fee_usd, fee_pct=100 * fee_usd / inv_usd if inv_usd else None,
                   pnl_usd=out_usd - inv_usd if x.get('burn_blk') else None)
        if row['pnl_usd'] is not None and inv_usd:
            row['pnl_pct'] = 100 * row['pnl_usd'] / inv_usd
            row['dpr_pct'] = row['pnl_pct'] * 24 / age_h if age_h else None
            row['fee_eth'] = fee_usd / eth_usd(x['burn_blk'] - 1)
            row['pnl_eth'] = row['pnl_usd'] / eth_usd(x['burn_blk'] - 1)
        tok = c1 if c0 == USDG else c0
        if x.get('burn_blk') and back.get(tok):
            sold, usdg_in = sale(owner, tok, x['burn_blk'], back[tok], key)
            if sold:
                row['sold_frac'] = sold / back[tok]
                row['realized_usd'] = back[USDG] / 1e6 + usdg_in * (back[tok] / sold if sold else 0) - inv_usd
                row['realized_pct'] = 100 * row['realized_usd'] / inv_usd if inv_usd else None
        elif x.get('burn_blk'):
            row['realized_usd'] = back[USDG] / 1e6 - inv_usd
            row['realized_pct'] = 100 * row['realized_usd'] / inv_usd if inv_usd else None
        out.append(row)
        print('  #%d %s done' % (tid, row['pair']), file=sys.stderr, flush=True)
    return out


SALE_BLOCKS = 6000      # ~10 min: the reference wallet sold its returned token 37 blocks after the close


def sale(owner, tok, blk, amount, key):
    """(tokens sold, USDG received) in the owner's txs within SALE_BLOCKS after the close that send `tok` out and take USDG in."""
    txs = []
    for r in F._pages(dict(from_block=blk + 1, to_block=blk + SALE_BLOCKS, transactions=[{'from': [owner]}],
                           field_selection=dict(transaction=['hash', 'block_number'])), key, stop=blk + SALE_BLOCKS):
        for d in r['data']:
            txs += d.get('transactions') or []
    sold = usdg = 0
    for t in sorted(txs, key=lambda t: t['block_number']):
        rc = rpc('eth_getTransactionReceipt', [t['hash']])
        out_t = sum(int(l['data'], 16) for l in rc['logs'] if l['topics'][0] == XFR and len(l['topics']) == 3
                    and l['address'].lower() == tok and l['topics'][1][-40:] == owner[2:])
        in_u = sum(int(l['data'], 16) for l in rc['logs'] if l['topics'][0] == XFR and len(l['topics']) == 3
                   and l['address'].lower() == USDG and l['topics'][2][-40:] == owner[2:])
        if out_t and in_u:
            sold += out_t
            usdg += in_u / 1e6
        if sold >= amount * 0.99:
            break
    return sold, usdg


def check():
    ref = json.load(open(FIX))
    mine = {r['tokenId']: r for r in portfolio(ref['address'], days=3, only={h['tokenId'] for h in ref['historical']})}
    save_cache()
    print('%-9s %-6s | %-17s | %-17s | %-24s | %s' % ('tokenId', 'age_h', 'invested ETH', 'fee %', 'pnl % (LPA vs pool price)', 'REALIZED % incl. token sale'))
    for h in ref['historical']:
        m = mine.get(h['tokenId'])
        if not m:
            print('%-9d missing' % h['tokenId']); continue
        f = lambda v, n=4: ('%.' + str(n) + 'f') % v if isinstance(v, (int, float)) else str(v)
        print('%-9d %-6s | %s vs %s | %s vs %s | %8s vs %8s        | %s (sold %s)' % (h['tokenId'], f(m['age_h'], 2), f(h['invested_eth']), f(m['invested_eth']),
              f(h['fee_pct']), f(m['fee_pct']), f(h['pnl_pct'], 2), f(m.get('pnl_pct'), 2), f(m.get('realized_pct'), 2), f(m.get('sold_frac'), 2)))


def main(a):
    if a and a[0] == 'check':
        return check()
    days = float(a[a.index('--days') + 1]) if '--days' in a else 3
    rows = portfolio(a[0], days)
    print(json.dumps(rows, indent=1, default=str) if '--json' in a else '\n'.join(str(r) for r in rows))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
