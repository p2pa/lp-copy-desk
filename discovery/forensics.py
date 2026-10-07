"""LAUNCH FORENSICS, point-in-time: who held a token's supply, who bought first, who sold early and who funded whom, AS OF
A GIVEN BLOCK. The readings 0xPerceptor (perceptor.info) computes per token, rebuilt from hypersync so that a backtest can
take them at its own entry block (docs/PERCEPTOR.md).

  python -m discovery.forensics <token> [--window S] [--at BLOCK] [--json]
  python -m discovery.forensics compare <token> <perceptor dir>     our readings next to Perceptor's

Definitions follow Perceptor's published rules:
- launch = the first Transfer of the token (mint or first move) - their "first liquidity / first executable trade";
- launch window = launch + 1,800 s;
- custody (never a holder, never a buyer): the token's pools (v3 PoolCreated, v4 Initialize via the PoolManager), the v4
  PoolManager, the v3 NPM, routers, burn addresses;
- a BUY = the token leaving custody to a non-custody address; the trader is that recipient, or the tx sender when the
  recipient is a router. first buyers = first N distinct traders by (block, logIndex);
- float = sum of positive balances outside custody and burns; concentration is taken at the window end (or --at).
"""
import collections, json, os, sys, time

from discovery import scout

HS = 'https://4663.hypersync.xyz/query'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
PONS_FACTORY = '0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e'
PONS_LAUNCHED = '0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607'
PONS_HOOK = '0xe5e702641ea86f4ae6cc3cdaed2b886f976be044'
PONS_ESCROW = '0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e'
V3_FACTORY = '0x1f7d7550b1b028f7571e69a784071f0205fd2efa'
POOL_CREATED = '0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118'
PM = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
INITIALIZE = '0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'
BURNS = {'0x0000000000000000000000000000000000000000', '0x000000000000000000000000000000000000dead',
         '0x0000000000000000000000000000000000000001'}
ROUTERS = {'0x8876789976decbfcbbbe364623c63652db8c0904',   # Universal Router
           '0xcaf681a66d020601342297493863e78c959e5cb2',   # SwapRouter02
           '0x204faca1764b154221e35c0d20abb3c525710498',   # UR-style clone
           '0x000000000022d473030f116ddee9f6b43ac78ba3',   # Permit2
           '0x73991a25c818bf1f1128deaab1492d45638de0d3',   # v3 NPM
           '0x58daec3116aae6d93017baaea7749052e8a04fa7'}   # v4 PositionManager


def _key():
    return open(scout.HS_TOKEN).read().strip()


def _pages(body, key, stop=None):
    frm = body['from_block']
    while True:
        r = scout._hs(HS, key, dict(body, from_block=frm), None)
        yield r
        nb = r.get('next_block')
        if not nb or nb == frm or nb >= (stop or r.get('archive_height') or 0):
            return
        frm = nb


INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chains', 'robinhood', 'var', 'forensics_index.json')


def build_index(key=None):
    """ONE scan of the chain: token -> custody (its v3 pools, the hooks of its v4 pools, its Pons curve) and Pons launches
    (token -> curve, deployer, launch block). Every per-token read then skips three full-history scans."""
    key = key or _key()
    cust, pons = collections.defaultdict(set), {}
    for r in _pages(dict(from_block=0, logs=[dict(address=[V3_FACTORY], topics=[[POOL_CREATED]])],
                         field_selection=dict(log=['topic1', 'topic2', 'data'])), key):
        for d in r['data']:
            for lg in d.get('logs') or []:
                pool = '0x' + lg['data'][-40:].lower()
                cust['0x' + lg['topic1'][-40:]].add(pool)
                cust['0x' + lg['topic2'][-40:]].add(pool)
    for r in _pages(dict(from_block=0, logs=[dict(address=[PM], topics=[[INITIALIZE]])],
                         field_selection=dict(log=['topic2', 'topic3', 'data'])), key):
        for d in r['data']:
            for lg in d.get('logs') or []:
                hook = '0x' + lg['data'][2 + 64 * 2 + 24:2 + 64 * 3].lower()
                if int(hook, 16):
                    cust['0x' + lg['topic2'][-40:]].add(hook)
                    cust['0x' + lg['topic3'][-40:]].add(hook)
    for r in _pages(dict(from_block=0, logs=[dict(address=[PONS_FACTORY], topics=[[PONS_LAUNCHED]])],
                         field_selection=dict(log=['block_number', 'topic1', 'topic2', 'topic3', 'data'])), key):
        for d in r['data']:
            for lg in d.get('logs') or []:
                tok, curve = '0x' + lg['topic1'][-40:], '0x' + lg['topic2'][-40:]
                cust[tok] |= {curve, PONS_HOOK, PONS_ESCROW}
                pons[tok] = dict(curve=curve, deployer='0x' + lg['topic3'][-40:], block=lg['block_number'],
                                 thr=int(lg['data'][130:194], 16) if len(lg['data']) >= 194 else None)
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    json.dump(dict(custody={k: sorted(v) for k, v in cust.items()}, pons=pons, built=time.time()), open(INDEX + '.tmp', 'w'))
    os.replace(INDEX + '.tmp', INDEX)
    return len(cust), len(pons)


_IDX = []


def _index():
    if not _IDX and os.path.exists(INDEX):
        _IDX.append(json.load(open(INDEX)))
    return _IDX[0] if _IDX else None


def pools_of(token, key):
    ix = _index()
    if ix is not None:
        return set(ix['custody'].get(token.lower(), ())), set()
    """Every v3 pool and v4 pool id holding `token` (custody)."""
    t = '0x' + token[2:].lower().rjust(64, '0')
    out = set()
    for tp in ([[POOL_CREATED], [t]], [[POOL_CREATED], [], [t]]):
        for r in _pages(dict(from_block=0, logs=[dict(address=[V3_FACTORY], topics=tp)], field_selection=dict(log=['data'])), key):
            for d in r['data']:
                for lg in d.get('logs') or []:
                    out.add('0x' + lg['data'][-40:].lower())       # PoolCreated data: tickSpacing, pool
    v4 = set()
    for tp in ([[INITIALIZE], [], [t]], [[INITIALIZE], [], [], [t]]):
        for r in _pages(dict(from_block=0, logs=[dict(address=[PM], topics=tp)], field_selection=dict(log=['topic1', 'data'])), key):
            for d in r['data']:
                for lg in d.get('logs') or []:
                    v4.add(lg['topic1'])
                    out.add('0x' + lg['data'][2 + 64 * 2 + 24:2 + 64 * 3].lower())   # the pool's hook: custody too
    # a Pons V2 launch: its bonding curve holds the unsold supply (custody), and the MemeHook / escrow are protocol custody
    for r in _pages(dict(from_block=0, logs=[dict(address=[PONS_FACTORY], topics=[[PONS_LAUNCHED], [t]])],
                         field_selection=dict(log=['topic2'])), key):
        for d in r['data']:
            for lg in d.get('logs') or []:
                out |= {'0x' + lg['topic2'][-40:].lower(), PONS_HOOK, PONS_ESCROW}
    out.discard('0x' + '0' * 40)
    return out, v4


def transfers(token, key, frm, to=None):
    """The token's Transfer logs in [frm, to), with the tx sender and the block time."""
    body = dict(from_block=frm, logs=[dict(address=[token], topics=[[TRANSFER]])],
                field_selection=dict(log=['block_number', 'log_index', 'transaction_hash', 'topic1', 'topic2', 'data'],
                                     transaction=['hash', 'from'], block=['number', 'timestamp']))
    if to:
        body['to_block'] = to
    rows, txf, bt = [], {}, {}
    for r in _pages(body, key, stop=to):
        for d in r['data']:
            for tx in d.get('transactions') or []:
                txf[tx['hash']] = tx['from'].lower()
            for b in d.get('blocks') or []:
                bt[b['number']] = int(b['timestamp'], 16) if isinstance(b['timestamp'], str) else b['timestamp']
            for lg in d.get('logs') or []:
                rows.append((lg['block_number'], lg['log_index'], '0x' + lg['topic1'][-40:], '0x' + lg['topic2'][-40:],
                             int(lg['data'] or '0x0', 16), lg['transaction_hash']))
    rows.sort()
    return rows, txf, bt


def _rpc(m, p):
    import urllib.request
    q = urllib.request.Request('https://robinhood-rpc.publicnode.com', json.dumps(dict(jsonrpc='2.0', id=1, method=m, params=p)).encode(),
                               {'Content-Type': 'application/json', 'User-Agent': 'x'})
    return json.loads(urllib.request.urlopen(q, timeout=20).read())['result']


def _ts(b):
    return int(_rpc('eth_getBlockByNumber', [hex(b), False])['timestamp'], 16)


def block_at(ts):
    """The last block with timestamp <= ts (bisection over the RPC)."""
    lo, hi = 0, int(_rpc('eth_blockNumber', []), 16)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _ts(mid) <= ts:
            lo = mid
        else:
            hi = mid
    return lo


def read(token, window=1800, at=None, key=None, custody_extra=()):
    token = token.lower()
    key = key or _key()
    v3, v4 = pools_of(token, key)
    custody = v3 | {PM} | ROUTERS | set(custody_extra)
    # launch = the first Transfer; one small query finds it, then the window is fetched
    first = None
    for r in _pages(dict(from_block=0, logs=[dict(address=[token], topics=[[TRANSFER]])],
                         field_selection=dict(log=['block_number'], block=['number', 'timestamp'])), key):
        for d in r['data']:
            if d.get('logs'):
                first = min(lg['block_number'] for lg in d['logs'])
                ts = {b['number']: b['timestamp'] for b in d.get('blocks') or []}
                break
        if first is not None:
            break
    t_launch = _ts(first)
    end = at or block_at(t_launch + window)
    rows, txf, bt = transfers(token, key, first, end + 1)
    end_block = end
    # PASS-THROUGH custody: an address whose token balance nets to zero inside every tx it touches, over >= 2 txs, is a
    # router / aggregator (Perceptor excludes these as 'router (protocol custody or pass-through)')
    per = collections.defaultdict(collections.Counter)
    for blk, li, f, t, v, h in rows:
        per[f.lower()][h] -= v
        per[t.lower()][h] += v
    custody |= {a for a, d in per.items() if len(d) >= 2 and all(x == 0 for x in d.values())}
    bal = collections.Counter()
    buyers, early_buyers, recv = [], set(), collections.Counter()
    for blk, li, f, t, v, h in rows:
        f, t = f.lower(), t.lower()
        bal[f] -= v
        bal[t] += v
        if t not in custody and t not in BURNS:
            recv[t] += v
        if f in custody and t not in custody and t not in BURNS:
            trader = txf.get(h, t) if t in ROUTERS else t
            if trader not in buyers:
                buyers.append(trader)
                if bt.get(blk, t_launch) - t_launch <= window:
                    early_buyers.add(trader)
    holders = {a: b for a, b in bal.items() if b > 0 and a not in custody and a not in BURNS}
    fl = sum(holders.values())
    top = sorted(holders.items(), key=lambda x: -x[1])
    minted = sum(v for blk, li, f, t, v, h in rows if f.lower() == '0x0000000000000000000000000000000000000000')
    supply = minted or fl

    def tier(n):
        s = sum(b for _, b in top[:n])
        return dict(pct_of_supply=round(100 * s / supply, 4), pct_of_float=round(100 * s / fl, 4) if fl else None)
    return dict(token=token, launch_block=first, launch_ts=t_launch, end_block=end_block, window_s=window,
                pools_v3=len(v3), pools_v4=len(v4), transfers=len(rows), holder_count=len(holders), float=fl, supply=supply,
                top1=tier(1), top5=tier(5), top10=tier(10), top20=tier(20),
                top10_addresses=[a for a, _ in top[:10]], first_buyers_10=buyers[:10], first_buyers_50=buyers[:50],
                early_buyers=len(early_buyers), largest_recipients_20=[a for a, _ in recv.most_common(20)])


def compare(token, pdir, at=None):
    ours = read(token, at=at)
    c = json.load(open(os.path.join(pdir, 'concentration.json')))['launch']
    co = {x['key']: x for x in json.load(open(os.path.join(pdir, 'cohorts.json')))}
    th = c['tiers']
    p_fb10 = [m['address'] for m in co['first_buyers_10']['members']]
    p_rec = [m['address'] for m in co['largest_launch_recipients']['members']]
    rows = [
        ('launch window end block', c['label'], ours['end_block']),
        ('holders', c['holder_count'], ours['holder_count']),
        ('float (tokens)', int(c['float_raw']) / 1e18, ours['float'] / 1e18),
        ('top1 % supply', th['top1']['pct_of_supply'], ours['top1']['pct_of_supply']),
        ('top10 % supply', th['top10']['pct_of_supply'], ours['top10']['pct_of_supply']),
        ('top10 % float', th['top10']['pct_of_float'], ours['top10']['pct_of_float']),
        ('top20 % float', th['top20']['pct_of_float'], ours['top20']['pct_of_float']),
        ('top10 holders in common', 10, len(set(th['top10']['addresses']) & set(ours['top10_addresses']))),
        ('early buyers (<= window)', co['early_buyers']['member_count'], ours['early_buyers']),
        ('first 10 buyers in common', 10, len(set(p_fb10) & set(ours['first_buyers_10']))),
        ('first 10 buyers same order', 10, sum(1 for a, b in zip(p_fb10, ours['first_buyers_10']) if a == b)),
        ('largest 20 recipients in common', 20, len(set(p_rec) & set(ours['largest_recipients_20']))),
    ]
    return rows, ours


def main(a):
    if a and a[0] == 'index':
        print('custody for %d tokens, %d Pons launches -> %s' % (*build_index(), INDEX))
        return 0
    if a and a[0] == 'compare':
        rows, ours = compare(a[1], a[2], int(a[a.index('--at') + 1]) if '--at' in a else None)
        print('%-34s %28s %20s' % ('reading', 'Perceptor', 'ours'))
        for k, p, o in rows:
            print('%-34s %28s %20s' % (k, p if not isinstance(p, float) else round(p, 4), o if not isinstance(o, float) else round(o, 4)))
        return 0
    tok = a[0]
    win = int(a[a.index('--window') + 1]) if '--window' in a else 1800
    at = int(a[a.index('--at') + 1]) if '--at' in a else None
    r = read(tok, win, at)
    print(json.dumps(r, indent=1) if '--json' in a else json.dumps({k: v for k, v in r.items() if not isinstance(v, list)}, indent=1))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]) or 0)
