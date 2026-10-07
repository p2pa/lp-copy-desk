"""MANAGED LADDERS on the copy desk, against an anvil fork of robinhood: `python -m tests.ladderfork [--keep]`.

Delta Liquidity's manager holds a ladder's NFTs and names its LP only in ManagedOpenV4 (owner 2026-10-01: 0x620a joined the squad
and the desk never heard its mints). On a fork at head, the desk follows 0x620a and:
  1. 0x620a's own last ladder-open tx is sent again from 0x620a (impersonated, its deadline word moved forward): the manager mints
     the real bins and emits the real ManagedOpenV4 -> the desk's WEBSOCKET copies the ladder as ONE range over all its bins
     (lpcopy.union) at the ladder's dollars
  2. its previous ladder the same way, heard by the BACKSTOP (managed_poll) before the socket: one copy, never two
  3. the manager burns half of the first ladder's bins (impersonated): the sum of the bins is the leader's liquidity, so the copy
     stays while more than exit_frac of the ladder is left; then the rest -> the copy closes, by the websocket
  4. the books: what the desk's USDG moved == the closed copy's booked P&L + what the open copy cost
Isolation as tests/lpfork: private chain id, every url local, throwaway desk keys, the LP db never written (desk.remote)."""
import json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.forkutil import _rpc, start, fund_usdg, wait   # noqa: E402

LP = '0x620a47629b144b2d1e4d05be5bd6cc2d4ba1c5ed'
FORK = []


def ladder_txs(n=2):
    """0x620a's latest ladder-open txs, newest first (the history store)."""
    import sqlite3
    con = sqlite3.connect('file:%s/chains/robinhood/var/lphist.db?mode=ro' % ROOT, uri=True)
    return [r[0] for r in con.execute('SELECT p.mint_tx, max(p.open_blk) FROM positions p JOIN managed m ON m.tid = p.tid WHERE m.owner = ? '
                                      'GROUP BY p.mint_tx ORDER BY 2 DESC LIMIT ?', (LP, n))]


def resend(u, src, tx):
    """Send tx's calldata again from its sender on the fork, the deadline word (any word that reads as a unix time within a
    day of now) moved a day forward. Returns the receipt."""
    from copydesk import lphist
    t = lphist._node([('eth_getTransactionByHash', [tx])])[0]
    d = t['input'][10:]
    now = int(time.time())
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    w = ['%064x' % (int(x, 16) + 86400) if abs(int(x, 16) - now) < 86400 else x for x in w]
    _rpc(u, 'anvil_impersonateAccount', [t['from']])
    _rpc(u, 'anvil_setBalance', [t['from'], hex(10 ** 18)])
    from tests.exitfork import fund_token
    for tok in ('0x' + w[0][-40:], '0x' + w[1][-40:]):              # the pair: approve the manager (the LP approves per ladder)
        if int(tok, 16):
            bal = int(_rpc(u, 'eth_call', [{'to': tok, 'data': '0x70a08231' + '0' * 24 + FORK[0].pm[2:]}, 'latest']), 16)
            fund_token(u, FORK[0], tok, t['from'], bal // 50)        # a share of the PoolManager's: whatever side the price needs now
            _rpc(u, 'anvil_impersonateAccount', [t['from']])
            wait(u, _rpc(u, 'eth_sendTransaction', [{'from': t['from'], 'to': tok, 'data': '0x095ea7b3' + '0' * 24 + t['to'][2:] + 'f' * 64}]))
    try:                                                              # a ladder the price has since moved into cannot mint as-is
        _rpc(u, 'eth_call', [{'from': t['from'], 'to': t['to'], 'data': t['input'][:10] + ''.join(w), 'gas': hex(30_000_000)}, 'latest'])
    except Exception:
        _rpc(u, 'anvil_stopImpersonatingAccount', [t['from']])
        return None
    h = _rpc(u, 'eth_sendTransaction', [{'from': t['from'], 'to': t['to'], 'data': t['input'][:10] + ''.join(w), 'gas': hex(30_000_000)}])
    _rpc(u, 'anvil_stopImpersonatingAccount', [t['from']])
    rc = wait(u, h)
    if rc and rc['status'] != '0x1':
        try:
            _rpc(u, 'eth_call', [{'from': t['from'], 'to': t['to'], 'data': t['input'][:10] + ''.join(w), 'gas': hex(30_000_000)}, 'latest'])
        except Exception as e:
            print('  revert:', str(e)[:200])
    return rc


def burn_as_manager(u, fork, mgr, tids, key):
    """The manager decreases and burns these bins (impersonated): the leader's exit, as the PoolManager sees it."""
    from core.exec import lp as X
    from copydesk import lpfollow as L
    _rpc(u, 'anvil_impersonateAccount', [mgr])
    _rpc(u, 'anvil_setBalance', [mgr, hex(10 ** 18)])
    for t in tids:
        liq = int(_rpc(u, 'eth_call', [{'to': L.POSM, 'data': '0x1efeed33%064x' % t}, 'latest']), 16)
        h = _rpc(u, 'eth_sendTransaction', [{'from': mgr, 'to': L.POSM, 'data': X.exit_calldata(t, liq, 0, 0, key, mgr, X.deadline(fork)),
                                             'gas': hex(2_000_000)}])
        wait(u, h)
    _rpc(u, 'anvil_stopImpersonatingAccount', [mgr])


def run(keep=False, log=print):
    from core import chain as cm, lpbook
    from core.exec import lp as X
    from copydesk import lpfollow as L
    real = cm.load('robinhood')
    os.environ.setdefault('LPFORK_URL', 'https://rpc.mainnet.chain.robinhood.com')
    a, u, fork, cpath = start(real, 12, 'ladderfork')
    FORK.append(fork)
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
        log('  ok  %s' % m)
    try:
        os.environ['LIVE_OK'] = 'yes'
        L.NODE, L.NODE_WS = u, u.replace('http', 'ws')
        L._node.update(ok=True, checked=1e18, lag=0, thread=True)
        L._conn.c = None
        L.NODE_FALLBACK = []                                           # a fork never falls back to mainnet reads
        L._px.update(v=None, t=0.0)
        me = lpbook.account(fork)
        _rpc(u, 'anvil_setBalance', [me.address, hex(10 ** 16)])
        A(fund_usdg(u, fork, me.address, 300 * 10 ** 6), 'the desk holds $300 USDG')
        usdg = X.usdg(fork)
        cfg = fork.cfg['lp']['follow']
        cfg.update(leader=[LP], bench=[], filler=[], native=True, reserve=0, size_free_div=2.0, max_open=3, min_leader_usd=5.0,
                   gas_keep_usd=15.0, cold_volz=None, flat_rv24=None, launch_cap_h=None, smart_exit=False, thin_max_share=0)
        cfg.pop('hours', None)
        d = L.Follow(fork, go=True, log=lambda m: None, note=lambda *x, **k: None)
        d.remote = True
        d.poll()                                                       # next_id and the managers' block = now
        time.sleep(3)                                                  # the listener subscribes
        A(fund_usdg(u, fork, LP, 3000 * 10 ** 6), '0x620a holds $3,000 USDG on the fork for its two ladders')
        u0 = X.balance(fork, usdg, me.address)
        cands = iter(ladder_txs(40))

        def next_ladder():
            for tx in cands:
                rc_ = resend(u, None, tx)
                if rc_:
                    return rc_
        # 1. a recent ladder still below the price, heard by the websocket
        rc = next_ladder()
        lg = [l for l in rc['logs'] if l['topics'][0] == L.OPEN_V4]
        A(rc['status'] == '0x1' and lg, 'the manager minted the ladder again on the fork and emitted ManagedOpenV4')
        who, bins = L.managed_open(lg[0])
        A(who == LP and len(bins) > 1, 'the log names %s and %d bins' % (who[:8], len(bins)))
        d.drain(time.time() + 8)
        o = d.st['ours'].get(str(min(bins)))
        A(o and o.get('token_id'), 'the desk copied the ladder by the websocket: ours #%s' % (o or {}).get('token_id'))
        ps = [L.position(b) for b in bins]
        A((o['tick_lower'], o['tick_upper']) == (min(p[1] for p in ps), max(p[2] for p in ps)) and o.get('bins') == len(bins),
          'ONE range over all %d bins: ticks %s..%s' % (len(bins), o['tick_lower'], o['tick_upper']))
        mine = L.position(o['token_id'])
        A(mine and (mine[1], mine[2]) == (o['tick_lower'], o['tick_upper']) and mine[3] > 0, 'our NFT holds that range')
        lead = d.st['leader'][str(min(bins))]
        A(sorted(int(b) for b in lead['bins']) == sorted(bins) and all(d.st['bins'][str(b)] == str(min(bins)) for b in bins),
          'the ladder is tracked as the sum of its bins')
        A(lead['peak'] == sum(p[3] for p in ps), 'its peak liquidity is the bins\' sum')
        A(abs(o['size_usd'] - u0 / 1e6 / 2) < u0 / 1e6 * 0.03, 'sized free/2: $%.2f of $%.2f' % (o['size_usd'], u0 / 1e6))
        A(o['leader_usd'] > 5, 'the leader\'s ladder is worth $%.2f (the sum of its bins)' % o['leader_usd'])

        # 2. the previous ladder, heard by the backstop first; the socket hears it after and must not copy it again
        rc2 = next_ladder()
        _, bins2 = L.managed_open([l for l in rc2['logs'] if l['topics'][0] == L.OPEN_V4][0])
        d._mgr_t = 0
        d.managed_poll()
        o2 = d.st['ours'].get(str(min(bins2)))
        A(o2 and o2.get('token_id'), 'the BACKSTOP copied the second ladder (%d bins)' % len(bins2))
        nopen = sum(1 for x in d.st['ours'].values() if not x.get('closed'))
        d.drain(time.time() + 5)
        A(sum(1 for x in d.st['ours'].values() if not x.get('closed')) == nopen == 2, 'the socket heard it too: still one copy')

        # 3. the manager burns half of the first ladder's bins: no exit; then the rest: the copy closes by the websocket
        key = ps[0][0]
        mgr = lg[0]['address'].lower()
        by_L = sorted(zip(bins, (p[3] for p in ps)), key=lambda z: z[1])
        half, tot, cut = [], sum(p[3] for p in ps), 0
        for b, l_ in by_L:
            if cut + l_ >= (1 - d.c['exit_frac']) * tot:
                break
            half.append(b)
            cut += l_
        burn_as_manager(u, fork, mgr, half, key)
        d.drain(time.time() + 5)
        A(half and not d.st['ours'][str(min(bins))].get('closed'), '%d of %d bins out (%.0f%% of the ladder): the copy stays' % (
            len(half), len(bins), 100 * cut / tot))
        burn_as_manager(u, fork, mgr, [b for b in bins if b not in half], key)
        d.drain(time.time() + 8)
        o = d.st['ours'][str(min(bins))]
        closes = [json.loads(l) for l in open(L._log_path(fork)) if '"ev": "close"' in l]
        A(o.get('closed') and any(str(e.get('leader_tid')) == str(min(bins)) and e.get('path') == 'ws' for e in closes),
          'the rest out: the copy closed by the websocket, %+.2f on $%.2f' % (o.get('pnl_usd', 0), o['size_usd']))
        A(str(min(bins)) not in d.st['leader'] and not any(v == str(min(bins)) for v in (d.st.get('bins') or {}).values()),
          'the ladder and its bins are no longer tracked')

        # 4. the books
        moved = (X.balance(fork, usdg, me.address) - u0) / 1e6
        exp = o['pnl_usd'] - d.st['ours'][str(min(bins2))]['size_usd']
        A(abs(moved - exp) < 0.05, 'THE BOOKS: USDG moved %+.4f = closed %+.4f - the open copy\'s $%.4f' % (
            moved, o['pnl_usd'], d.st['ours'][str(min(bins2))]['size_usd']))
        log('ladderfork: %d asserts, all passed' % n[0])
        return 0
    finally:
        os.environ.pop('LIVE_OK', None)
        if not keep:
            a.terminate()


if __name__ == '__main__':
    sys.exit(run(keep='--keep' in sys.argv))
