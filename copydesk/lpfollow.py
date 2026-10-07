"""THE COPY LP DESK: watch one leader wallet, mirror its Uniswap v4 positions at our own size (docs/LPWALLETS.md).

    hookbot <chain> lpfollow backtest <leader> [--days 7] [--size 100] [--capital 1000]   what copying would have made
    hookbot <chain> lpfollow run --go [--seconds N]      the daemon, live only (--go AND LIVE_OK=yes)
    hookbot <chain> lpfollow status                      open copies, closed record, P&L

WATCH (no log scans, no websocket, none of the hypersync or blockmachine budget): every poll_s, PositionManager
nextTokenId() says which NFTs were minted since the last poll; one batched ownerOf over them finds the leader's new
positions. The leader's tracked positions get one batched getPositionLiquidity: below exit_frac of its peak = it exited.

MIRROR: same pool, same ticks, OUR size (size_usd, never the leader's). A range that holds only USDG at the current
price (a bid, the leader's usual shape) is minted quote-only, no swap; a range around the price goes through
lp.enter (swap the range's token share, then mint). The exit is lp.exit: decrease all, burn, take, sell the token side.

SKIP (recorded with the reason): no USDG side, native-ETH pools, hooked pools (a hook can tax or block us), leader
positions under min_leader_usd, capital (max_open, max_deploy_usd) exhausted, the program stopped.

GUARDS: var/lp/follow.STOP stops new copies (written by hand or by the drawdown stop: realized + open value falling
stop_drawdown x max_deploy_usd under its high exits every copy); a leader quiet for max_idle_h pauses entries.

TELEGRAM: the LP desk's own card (core/fmt.lp_card) on every copy opened or closed and a daily report: the desk's
value and P&L from its starting capital, the move, then each open copy (value, fees, deposit and P&L, min/now/max).
Live only: paper mode was removed (owner, 2026-09-28); what copying would have made is `lpfollow backtest`.
"""
import collections, json, os, time

from core.exec import lp as X

POSM = '0x58daec3116aae6d93017baaea7749052e8a04fa7'
SV = '0xF3334192D15450CdD385c8B70e03f9A6bD9E673b'
NATIVE = '0x' + '0' * 40
GAS_CYCLE_USD = 0.031 + 0.014 + 0.013          # mint + exit + sell, live trace medians (var/lp/trace.jsonl, 2026-09-28)
GAS_SWAP_USD = 0.013                           # the entry swap of a range around the price
COPY_DELAY = 'live'                            # the replay's delay: per copy kind, measured nightly (lpcopy.lag_table)
DEFAULTS = dict(leader='', size_usd=100.0, max_open=8, max_deploy_usd=800.0, min_leader_usd=20.0, poll_s=3.0,
                exit_frac=0.5, stop_drawdown=0.30, max_idle_h=24.0, haircut=0.81)


def uncopyable(c0, c1, hooks, pid, usdg, deny=(), native=True):
    """Why the desk cannot copy a position in this pool, or None - THE ONE RULE, shared with the replay (lpcopy.run), so a
    wallet is never scored on positions the desk would skip (2026-09-30: bench wallets busy in ETH pairs scored well
    and then copied nothing). It funds copies in USDG, or in native ETH through the treasury (owner 2026-09-30: 1,950 of
    the followed wallets' 1,951 uncopyable mints in a day were token/ETH pools); hooked and denied pools are skipped, and
    so is the ETH/USDG pool itself (the treasury's own conversion pool). native=False: USDG pools only."""
    c0, c1 = c0.lower(), c1.lower()
    if not int(c1, 16):
        return 'unknown pool'                                       # an all-zero key (burned position, unregistered pool)
    if int(hooks or '0x0', 16):
        return 'hooked pool'
    if NATIVE in (c0, c1) and usdg in (c0, c1):
        return 'the ETH/USDG pool'
    if NATIVE in (c0, c1):
        if not native:
            return 'native-ETH pool'
    elif usdg not in (c0, c1):
        return 'no USDG or ETH side'
    if pid.lower() in {x.lower() for x in deny}:
        return 'denied pool'                                        # [lp.follow] deny_pools: user markout beats its fees
    return None


ETH_POOL = '0x54f7883914619af9105355bf83ed678bcf9f63560218ac61c9963b9503d0ba32'   # [gas] eth_usd_pool: native/USDG
_px = dict(v=None, t=0.0)


def _read_px():
    sp = _w(_batch([_call(SV, '0xc815641c' + ETH_POOL[2:])])[0], 0) / 2 ** 96
    _px.update(v=sp * sp * 1e18 / 1e6, t=time.time())


def eth_px():
    """USDG per ETH off the ETH/USDG pool's slot0 (StateView): what an ETH-quoted copy is worth in dollars. The desk keeps it
    fresh from a background thread every 5 s (Follow), so an entry never waits for it; elsewhere it is read at most once
    every 5 s."""
    if not _px['v'] or (not _px.get('bg') and time.time() - _px['t'] > 5):
        _read_px()
    return _px['v']


def own_wallets():
    """Every address of ours (wallets/wallets.json, addresses only): never watched, copied, scored or picked. 2026-09-30
    the wide bench picked lp-1 - the desk's own wallet - and copied its own copy once (VRAX, closed +$2.18)."""
    import json
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wallets', 'wallets.json')
    try:
        return {v['address'].lower() for v in json.load(open(p)).values() if isinstance(v, dict) and v.get('address')}
    except Exception:
        return set()


def leaders(chain):
    """[lp.follow] leader: one address or a list (several leaders fill each other's quiet hours; copies share one capital)."""
    x = cfg(chain).get('leader') or []
    return [a.lower() for a in ([x] if isinstance(x, str) else x) if a]


def bench(chain):
    """[lp.follow] bench: backup leaders (owner 2026-09-29: two quiet hours left the book in cash). A bench copy uses spare
    capital at any time but always leaves room for one main copy, and a main copy that does not fit closes bench copies
    first: a main leader is never skipped for capital while any bench copy is open (Follow.room)."""
    x = cfg(chain).get('bench') or []
    return [a.lower() for a in ([x] if isinstance(x, str) else x) if a]


# [lp.follow] settings the running desk restarts itself on (read once at start): the room rule, sizing, token rules
RELOAD = ('reserve', 'size_free_div', 'native', 'max_open', 'size_frac', 'max_per_token', 'cool_n', 'cool_h', 'deny_tokens', 'deny_pools',
          'bench_per_wallet', 'filler_per_wallet', 'thin_max_share', 'room_waste_min', 'room_waste_rate', 'last_slot_frac', 'min_leader_usd',
          'cold_volz', 'flat_rv24', 'launch_cap_h', 'smart_exit', 'relay_exit', 'relay_margin_usd', 'filler_consensus_h', 'max_entry_cost', 'stop_pause_h', 'slot_target_usd', 'slot_min_book', 'launch_creator_frac', 'min_pool_age_h')


# the v4 PoolManager and its Initialize topic (the launch size guard reads a pool's creation from the node: the desk's box has
# no history store, so nothing from copydesk/lphist is imported here)
POOL_MANAGER = '0x8366a39cc670b4001a1121b8f6a443a643e40951'
INITIALIZE = '0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438'
# MANAGED LADDERS (owner 2026-10-01): Delta Liquidity's managers hold the NFTs, the LP is named only in ManagedOpenV4(address owner,
# bytes32, uint256[] tokenIds) - copydesk/lphist.MANAGERS / OPEN_V4, inlined (no lphist on the desk's box)
MANAGERS = ['0x83551a233f6d7b3cb4defbf55abfd887c6811ce4', '0xbcb96b15dc2246d242c879316c86e25e846ad5fb', '0x56bf0f5b26d888b989a9a20ec2164f0ca560836c',
            '0xf321ed71750c3e627a0f435c8950429f231e3497', '0x077af3f17c0ef7bb04a9882e1955e9c23d2f9e91', '0xac6a35d097bb230eb9966ddd8e5970055a622d98',
            '0x5ca6214227d1195c4b7b4b96847b8966c688295d', '0xc5941433114bb47a9733cb31a0a3a3dbff45b418', '0x64680254bf644bbdde394b95129895c13317fed4']
OPEN_V4 = '0x7d9003b0fdfd3f07da59f826e63af5ba028daf72e1a95af0a6a11be0c5a6a2be'


def managed_open(l):
    """A ManagedOpenV4 log -> (owner, [tokenIds]) (lphist.managed)."""
    d = l['data'][2:]
    w = [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]
    return '0x' + l['topics'][1][-40:].lower(), w[2:2 + w[1]]


def fee_rate(o, now, w):
    """CAPITAL AT WORK: the copy's fees over the trailing w seconds, in % of its size per hour, from its minute samples
    (fee_hist); None while it is younger than w or has no sample that old."""
    h = o.get('fee_hist') or []
    old = [f for t, f in h if t <= now - w]
    if not h or not old:
        return None
    return (h[-1][1] - old[-1]) / o['size_usd'] * 100 * 3600.0 / w


def thin_share(leader_L, leader_usd, size_usd, pool_L):
    """Our copy's share of the pool's active liquidity: its liquidity is the leader's scaled to our size (same range and
    price: liquidity is proportional to value), against the pool's active liquidity plus ours."""
    ours = leader_L * size_usd / leader_usd if leader_usd > 0 else 0.0
    return ours / (pool_L + ours) if pool_L + ours > 0 else 1.0


# THE EXIT SALE, SURFACED (owner 2026-10-03, after FIRM: "eval tracing etc should surface this better, the fact we sold so
# low ... through which aggregator"). Every close carries its sale: the venue, what that venue quoted, the fair value
# (lp.sell_best's; for exits before it, the best quote seen), every other quote and every failure. A sale under
# SALE_ALARM of fair or a held token is BAD (a failed aggregator only shows its ✗): it is on the close card, an error record (lp watch's
# feed, the checks) and `lp status`'s exit table.
SALE_ALARM = 0.9


def sale_of(r_):
    """An exit route (lp.sell_best's, or a close record's 'exit') -> dict(venue, got, fair, pct, quotes, fails, bad) or None."""
    if not r_:
        return None
    v = r_.get('venue')
    quotes = {k: q for k, q in (r_.get('aggs') or {}).items() if q is not None}
    if r_.get('relay_usd') is not None:
        quotes.setdefault('relay', r_['relay_usd'])
    pu = r_.get('usd', r_.get('pool_usd'))
    if pu is not None:
        quotes['pool'] = pu
    fails = dict(r_.get('errors') or {})
    if r_.get('relay_error'):
        fails.setdefault('relay', r_['relay_error'])
    got = quotes.get(v) if v not in (None, 'held') else None
    fair = r_.get('fair_usd') or (max(quotes.values()) if quotes else None)
    pct = got / fair if got is not None and fair else None
    return dict(venue=v, got=got, fair=fair, pct=pct, quotes=quotes, fails=fails,
                bad=v == 'held' or (pct is not None and pct < SALE_ALARM))     # a failed aggregator alone is not (CRADLE: kyber paid 100 %)


def sale_line(s):
    if not s:
        return ''
    head = ('HELD (nothing paid the floor)' if s['venue'] == 'held' else
            'sold via %s $%.2f' % (s['venue'], s['got'] or 0) + (' = %.0f%% of fair $%.2f' % (100 * s['pct'], s['fair']) if s['pct'] is not None else ''))
    oth = ['%s $%.2f%s' % (k, q, ' ✗' if k in s['fails'] else '') for k, q in sorted(s['quotes'].items(), key=lambda kv: -kv[1]) if k != s['venue']]
    oth += ['%s ✗ %s' % (k, e[:40]) for k, e in s['fails'].items() if k not in s['quotes']]
    return ('⚠ ' if s['bad'] else '') + head + (' · ' + ', '.join(oth) if oth else '')


def pair(o):
    """A copy's pair as traders write it, base/quote: VRAX/USDG, OFY/ETH (owner 2026-09-30) - the card, the captions and
    lp watch all name a copy this way, so an ETH-quoted one is never mistaken for a USDG one."""
    return '%s/%s' % (o.get('symbol') or o.get('sym') or (o.get('pool') or '?')[2:8], 'ETH' if o.get('quote') == 'eth' else 'USDG')


def filler(chain):
    """[lp.follow] filler: the third tier (owner 2026-09-30, idle capital): every other wallet whose copy score clears
    filler_min_score, written by `lp pick`. Copied only into slots nobody above needs, and closed first when they do."""
    x = cfg(chain).get('filler') or []
    return [a.lower() for a in ([x] if isinstance(x, str) else x) if a]


def cfg(chain):
    return dict(DEFAULTS, **((chain.cfg.get('lp') or {}).get('follow') or {}))


def pnl_points(eq, st, now_total=None, days=1.0, now=None):
    """The desk's P&L over time from its balance points eq [(ts, v)] (owner 2026-09-30): balance minus the capital in
    the book AT THAT TIME, so a deposit is not a step; the live total as the last point. The Telegram card and lp watch
    draw the same line."""
    now = now or time.time()
    dep = [(d['ts'], d['usd']) for d in st.get('deposits') or [] if d.get('ts')]
    cap_at = lambda t: (st.get('capital') or 0.0) - sum(u for ts, u in dep if ts > t)
    pts = [(t, v - cap_at(t)) for t, v in eq if t >= now - days * 86400] + ([(now, now_total - cap_at(now))] if now_total is not None else [])
    # ponytail: median of 3 drops one-point misreads (a copy valued mid-mint read $374 on a $290 book); a real move
    # lasts longer than one 10-minute point and stays
    if len(pts) > 2:
        pts = pts[:1] + [(pts[i][0], sorted(v for _, v in pts[i - 1:i + 2])[1]) for i in range(1, len(pts) - 1)] + pts[-1:]
    return pts


def live_size(chain):
    """(copy size $, book $) as the desk sizes a copy NOW: size_frac of its book, the book being the desk's last equity
    point (var/lp/equity.jsonl: spare USDG + open copies, written every 10 min; mirrored home). Every scorer and
    simulator uses it (owner 2026-09-30: size is dynamic) - replays are stored at $25 and scaled, so gas, a flat cost a
    copy, weighs what it weighs at the real size. Without size_frac or a book: the fixed size_usd / max_deploy_usd."""
    c = cfg(chain)
    fixed = float(c['size_usd']), float(c['max_deploy_usd'])
    if not c.get('size_frac'):
        return fixed
    try:
        with open(os.path.join(_dir(chain), 'equity.jsonl'), 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400))
            book = float(json.loads(f.read().decode().strip().splitlines()[-1])['v'])
    except Exception:
        return fixed
    return round(book * float(c['size_frac']), 2), book


def _dir(chain):
    d = os.path.join(chain.var, 'lp')
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(chain):
    return os.path.join(_dir(chain), 'follow.json')


# SKIP CODES (owner 2026-10-02: "everything nicely organized, exactly with reasons"): every skip keeps its sentence in
# `why` and gets a stable `code` to count and filter by (`lp log skips`). First match wins; unknown text keeps its own
# prefix, so a new skip reason shows up as itself instead of vanishing into "other".
SKIP_CODES = (('capital', 'capital'), ('bench: room kept', 'room kept for a main'), ('wallet limit', 'wallet limit'),
              ('token cap', 'token cap'), ('token cooling off', 'token cooling off'), ('thin pool', 'thin pool'),
              ('cold pool', 'cold pool'), ('price-flat', 'price-flat pool'), ('leader position', 'leader below floor'),
              ('outside its hours', 'outside its hours'), ('stop', 'stop'), ('treasury stopped', 'treasury stopped'),
              ('no usdg', 'not copyable'), ('hooked pool', 'not copyable'), ('unknown pool', 'not copyable'),
              ('denied', 'denied'), ('owner close', 'owner close'), ('unreadable', 'unreadable'), ('ladder', 'unreadable'),
              ('no squad agreement', 'no squad agreement'), ('entry cost', 'entry cost'))


def skip_code(why):
    w = (why or '').strip().lower()
    for pre, code in SKIP_CODES:
        if w.startswith(pre):
            return code
    return w.split(':')[0][:40] or 'other'


def _tx_brief(r):
    """One send as the desk's records keep it: what it did, its hash, its gas, and what went wrong if it did."""
    keep = ('action', 'tx', 'block', 'gas_usd', 'venue', 'pool', 'slip_bps', 'at', 'revert', 'why')
    return {k: r[k] for k in keep if r.get(k) is not None}


def _log_path(chain):
    return os.path.join(_dir(chain), 'follow.jsonl')


def close_path(chain):
    """`lp close <leader tokenId ...>`: the owner's request, one tokenId a line; the running desk closes those copies on
    its next loop through its own exit (booked like any close) and deletes the file."""
    return os.path.join(_dir(chain), 'close.req')


def stop_path(chain):
    return os.path.join(_dir(chain), 'follow.STOP')


# --------------------------------------------------------------------------- reads: OUR node first (owner, 2026-09-28)
# NO NODE (2026-10-01): rh-node's spot box was reclaimed at 09:14 with its chain DB and is not rebuilt (owner). None = every read
# and every socket goes to the public endpoints at once, nothing dials a dead address. A rebuilt node sets these again (home:
# 'http://127.0.0.1:18547' / 'ws://127.0.0.1:18548' through supervise's tunnels; the desk's box: the bridge block's 172.* urls)
NODE = None
NODE_WS = None
NODE_MAX_LAG = 20                        # blocks behind the public tip before the desk stops trusting the node
# WHEN THE NODE IS GONE (2026-10-01 09:14: rh-node's spot box reclaimed, its chain DB with it): the same reads from public
# endpoints that serve full log windows - blockmachine 10,000 blocks of PoolManager logs in 1.4-3 s at any age, the official
# rpc 2,000 (publicnode 403, drpc 400 on those reads). The node is tried first again 60 s after a failure
NODE_FALLBACK = ['https://rpc-robinhood.blockmachine.io', 'https://rpc.mainnet.chain.robinhood.com',
                 'https://robinhood-mainnet-rpc.blockreq.com/v1/rpc/public']   # blockreq: batches + 1,000-block logs in 108 ms (benchmarked)
_node = dict(ok=True, checked=0.0, lag=None, down_until=0.0)


def _check_node():
    _node['checked'] = time.time()
    if not NODE:
        return _node.update(ok=False, lag=None)
    try:
        from copydesk import lpwallet as W
        mine = int(_node_batch([('eth_blockNumber', [])], raw=True)[0], 16)
        pub = int(W._public_batch([('eth_blockNumber', [])])[0], 16)
        _node.update(lag=pub - mine, ok=pub - mine <= NODE_MAX_LAG)
    except Exception:
        _node.update(ok=False, lag=None)


# BACKUP SOCKETS (2026-10-01 15:35: publicnode's handshakes timed out and it was the only one): drpc and blockmachine trail it by
# ~35 / ~97 ms p50 (1,480 blocks raced from the desk's box), used only when the sockets before them fail
WS_FALLBACK = ['wss://robinhood.drpc.org', 'wss://rpc-robinhood.blockmachine.io']


def ws_urls(chain):
    """The sockets, in order: our node's (when there is one), the chain's public ones (rpc.toml ws), then WS_FALLBACK."""
    u_ = ([NODE_WS] if NODE_WS else []) + [u for u in chain.ws_urls if u != NODE_WS and not u.startswith('ws://172.')]
    return u_ + [u for u in WS_FALLBACK if u not in u_]


def node_fresh():
    """Is our node at the tip? Checked against a public node once a minute - by a BACKGROUND thread (2026-09-30): the check
    sat on the entry path and a slow public node made 1 in 10 copies spend 1.5-3 s before their first send. The first
    call checks once in line (start-up), after that this only reads the last answer."""
    if not _node.get('thread'):
        _node['thread'] = True
        _check_node()

        def loop():
            while True:
                time.sleep(60)
                _check_node()
        __import__('threading').Thread(target=loop, daemon=True).start()
    elif _node['checked'] == 0.0:                                     # re-check asked for (the desk switched node address)
        _check_node()
    return _node['ok']


_conn = __import__('threading').local()


def _post(body, timeout, url=None):
    """One JSON-RPC POST over a KEPT-ALIVE connection (one per thread and url): a fresh connection per call through the ssh
    tunnel cost ~0.5 s a read, and a wallet report makes ~60 of them. url: our node by default."""
    import http.client
    from urllib.parse import urlsplit
    url = url or NODE
    if not url:
        raise ConnectionRefusedError('no node configured (lpfollow.NODE)')
    cs = _conn.__dict__.setdefault('cs', {})
    for attempt in range(2):
        c = cs.get(url)
        if c is None or c.timeout != timeout:
            u = urlsplit(url)
            k = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection
            c = cs[url] = k(u.hostname, u.port or (443 if u.scheme == 'https' else 80), timeout=timeout)
            c.path_ = u.path or '/'
        try:
            c.request('POST', c.path_, body, {'Content-Type': 'application/json', 'User-Agent': 'lp-copy-desk'})
            return json.loads(c.getresponse().read())
        except Exception as e:
            cs.pop(url, None)                                           # a dropped connection: reconnect once
            if attempt or isinstance(e, TimeoutError):                  # ... but a timeout is the endpoint, not the connection
                raise


def _node_batch(calls, timeout=20, raw=False):
    """Batches to our node; raw=False: when the node is unreachable, the same batches from NODE_FALLBACK (in order)."""
    if raw or (NODE and time.time() >= _node['down_until']):
        try:
            return _node_batch_at(calls, timeout, None)
        except (OSError, ConnectionError) as e:
            if raw:
                raise
            _node['down_until'] = time.time() + 60
            last = e
    else:
        last = None
    # every endpoint rate-limited at once (2026-10-01: 8 of 3,155 replays; the official rpc allows 60 a minute): wait for
    # the window to reset and go round again, three rounds, before a read fails
    down = _node.setdefault('fb_down', {})
    for rnd in range(4):
        k = _node.setdefault('rr', 0)
        _node['rr'] = k + 1                                             # the fallbacks take turns: two rate limits, not one
        fb = NODE_FALLBACK[k % len(NODE_FALLBACK):] + NODE_FALLBACK[:k % len(NODE_FALLBACK)] if NODE_FALLBACK else []
        # one that timed out or refused the connection is skipped for 60 s, and a try waits 8 s at most (2026-10-01 15:38:
        # blockmachine stopped answering; every other ladder read waited 20 s on it, poll passes of 21-44 s)
        for u in [u for u in fb if down.get(u, 0) < time.time()] or fb:
            try:
                return _node_batch_at(calls, min(timeout, 8), u)
            except (OSError, TimeoutError) as e:
                down[u] = time.time() + 60
                last = e
            except Exception as e:
                last = e
        if not any(x in str(last) for x in ('429', 'Too Many', '-32029', 'rate')):
            break
        time.sleep((5, 15, 30, 0)[rnd])
    raise RuntimeError('node and every fallback failed: %s' % str(last)[:160])


def _node_batch_at(calls, timeout, url):
    out = []
    n = 100 if url is None else 50                                     # blockmachine answers a 100-item batch with 429
    for i in range(0, len(calls), n):
        chunk = calls[i:i + n]
        body = [dict(jsonrpc='2.0', id=k, method=m, params=p) for k, (m, p) in enumerate(chunk)]
        for attempt in range(3):
            r = _post(json.dumps(body), timeout, url)
            if url is None or '429' not in str(r)[:300] and 'Too Many' not in str(r)[:300]:
                break
            time.sleep(attempt + 1)                                     # rate limited: twice more, then the next endpoint
        if not isinstance(r, list) or any(x.get('error') and 'revert' not in str(x['error']).lower() for x in r):
            bad = next((x['error'] for x in r if x.get('error')), None) if isinstance(r, list) else None
            raise RuntimeError('node: %s' % str(bad or r)[:160])                # the failing item's error, not the first item
        m = {x['id']: x.get('result') for x in r}
        out += [m.get(k) for k in range(len(chunk))]
    return out


def _batch(calls):
    """Every desk read: our node while it is fresh, public nodes (lpwallet's rotation) otherwise."""
    if node_fresh():
        try:
            return _node_batch(calls)
        except Exception:
            _node['ok'] = False                                         # re-checked within a minute
    from copydesk import lpwallet as W
    return W._public_batch(calls)


def _call(to, data):
    return ('eth_call', [dict(to=to, data=data), 'latest'])


def _w(r, i):
    return int(r[2 + 64 * i:66 + 64 * i], 16) if r and len(r) >= 66 + 64 * i else 0


def _sg24(v):
    return v - (1 << 24) if v >> 23 else v


def _enc(v):
    return '%064x' % (v % (1 << 256))


def position(tid):
    """(key dict, tickLower, tickUpper, liquidity, pid) of a PositionManager NFT, or None."""
    from eth_utils import keccak
    info, liq = _batch([_call(POSM, '0x7ba03aad' + '%064x' % tid), _call(POSM, '0x1efeed33' + '%064x' % tid)])
    if not info or len(info) < 2 + 64 * 6:
        return None
    pi = _w(info, 5)
    key = dict(currency0='0x%040x' % _w(info, 0), currency1='0x%040x' % _w(info, 1), fee=_w(info, 2),
               tickSpacing=_sg24(_w(info, 3) & 0xffffff), hooks='0x%040x' % _w(info, 4))
    pid = '0x' + keccak(bytes.fromhex(info[2:2 + 64 * 5])).hex()
    return key, _sg24((pi >> 8) & 0xffffff), _sg24((pi >> 32) & 0xffffff), int(liq, 16) if liq else 0, pid


def pool_state(pid, tl, tu):
    """(sqrtP as a float of the Q96 ratio, fee growth inside 0, fee growth inside 1, lpFee pips) at the latest block."""
    s0, fg = _batch([_call(SV, '0xc815641c' + pid[2:]), _call(SV, '0x53e9c1fb' + pid[2:] + _enc(tl) + _enc(tu))])
    return _w(s0, 0) / 2 ** 96, _w(fg, 0), _w(fg, 1), _w(s0, 3)


# --------------------------------------------------------------------------- position maths
def amounts(L, sp, tl, tu):
    sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
    spc = min(max(sp, sa), sb)
    return L * (sb - spc) / (spc * sb), L * (spc - sa)


def usd_value(x0, x1, sp, usdg_c0, d0, d1, sell_cut=0.0):
    """USD of raw amounts in a USDG pair at sqrtP `sp`; the token side is cut by `sell_cut` (the fee to sell it)."""
    p = sp * sp * 10 ** d0 / 10 ** d1                              # token1 per token0, whole units
    if usdg_c0:
        return x0 / 10 ** d0 + x1 / 10 ** d1 / p * (1 - sell_cut)
    return x1 / 10 ** d1 + x0 / 10 ** d0 * p * (1 - sell_cut)


# --------------------------------------------------------------------------- backtest (paper-copy the history)
def backtest(leader, days=7.0, size_usd=100.0, capital_usd=1000.0, delay=70, log=print):
    """Copy the leader's closed positions over `days` at our fixed size, `delay` blocks late, inside our capital:
    a copy is skipped while size_usd more would exceed capital_usd. P&L per copy = the replayed P&L (copydesk/lpcopy:
    same pool, ticks, fees swap by swap) scaled to our size, times the wallet's own validation ratio (real / model at
    delay 0, measured in the same run), minus our measured gas per cycle."""
    from copydesk import lpcopy as C
    R = C.run(leader.lower(), days, [0, delay])
    v0, vd = R['delays'][0], R['delays'][delay]
    ratio = v0['actual'] / v0['pnl_known'] if v0['pnl_known'] else 1.0
    open_, deployed, rows = [], 0.0, []
    for t in sorted(vd['trades'], key=lambda x: x['t0']):
        open_ = [o for o in open_ if o[0] > t['t0']]
        deployed = sum(o[1] for o in open_)
        if deployed + size_usd > capital_usd:
            rows.append(dict(t, taken=False))
            continue
        k = size_usd / (t['invested'] or t['model_in']) if (t['invested'] or t['model_in']) else 0
        pnl = t['model'] * k * min(1.0, ratio) - GAS_CYCLE_USD
        open_.append((t['t1'], size_usd))
        rows.append(dict(t, taken=True, ours=pnl))
    taken = [r for r in rows if r['taken']]
    days_ = collections.Counter()
    for r in taken:
        days_[time.strftime('%m-%d', time.gmtime(r['t1']))] += r['ours']
    peak = max(1, max((len([o for o in taken if o['t0'] <= r['t0'] < o['t1']]) for r in taken), default=1))
    return dict(leader=leader, days=days, size_usd=size_usd, capital_usd=capital_usd, delay_s=delay / 10,
                validation_ratio=round(ratio, 3), copies=len(taken), skipped_for_capital=len(rows) - len(taken),
                pnl=sum(r['ours'] for r in taken), gas=len(taken) * GAS_CYCLE_USD,
                win=100 * sum(1 for r in taken if r['ours'] > 0) / len(taken) if taken else 0,
                peak_open=peak, peak_deployed=peak * size_usd, daily=dict(sorted(days_.items())), secs=R['secs'])


# --------------------------------------------------------------------------- the daemon
class Follow:
    """The live copy desk. Live only: every copy is a real position of the [lp] wallet (paper mode was removed, owner
    2026-09-28 - the backtest is the paper)."""

    def __init__(self, chain, go=False, log=print, note=None):
        from core import lpbook
        if not go:
            raise SystemExit('lpfollow run is live only: --go (and LIVE_OK=yes). The paper is `lpfollow backtest`.')
        self.chain, self.log, self.note = chain, log, note or (lambda *a, **k: None)
        self.c = cfg(chain)
        self.leaders = leaders(chain)
        if not self.leaders:
            raise SystemExit('[lp.follow] leader is not set in chain.toml')
        self.leader = self.leaders[0]                                 # the first leader names the desk in its cards
        own = own_wallets()
        self.leaders = [a for a in self.leaders if a not in own]
        self.bench = [a for a in bench(chain) if a not in self.leaders and a not in own]
        self.filler = [a for a in filler(chain) if a not in self.leaders and a not in self.bench and a not in own]
        self.watch = self.leaders + self.bench + self.filler          # every wallet whose mints we hear
        self.u = X.usdg(chain)
        if self.c.get('relay_exit'):
            X.relay_warm()                                            # the exit's Relay quote never pays a TLS handshake
        # ENTRIES READ FROM rh-node too. From home chain.node means publicnode/drpc (~150 ms a read, sometimes blocks
        # behind); the node through the tunnel is the same reads a few ms from the sequencer. The public list stays behind it.
        global NODE, NODE_WS
        # hook_bot ran this desk on a box beside its own chain node (ops/fleet); standalone there is no fleet: remote when
        # LPDESK_REMOTE=1 (the desk then prefers the node's private 172.* endpoints from rpc.local.toml)
        try:
            from ops import fleet
            self.remote = fleet.here() != fleet.conf()['home']
        except ImportError:
            self.remote = os.environ.get('LPDESK_REMOTE') == '1'
        if self.remote:
            # beside rh-node ([place_lp]): its private address from the node bridge block in rpc.local.toml, ~2 ms away,
            # where home reaches it through the ssh tunnels (259 ms a read, 2026-09-29)
            http_ = [u for u in self.chain.gate_urls if u.startswith('http://172.')]
            ws_ = [u for u in self.chain.ws_urls if u.startswith('ws://172.')]
            if http_ and ws_:
                NODE, NODE_WS = http_[0], ws_[0]
                _node.update(checked=0.0)      # the freshness check ran against the tunnel address: re-check the real one
        n = self.chain.node
        if NODE and NODE not in n.rotate:
            n.rpc_url, n.rotate = NODE, [NODE] + [u for u in n.rotate if u != NODE]
        self.ev = lpbook.tracer(chain, 'fw' + time.strftime('%m%d%H%M%S', time.gmtime()))
        # THE SEND TRACE (owner 2026-10-02): every tx the exec layer signs (approve, swap, mint, exit, relay, permit2) and
        # every decoded refusal, numbered; an open or a close carries the ones it made (`txs`), so a copy's whole life is
        # one record with hashes, gas and reasons
        self._tx, self._txn = [], 0
        prev = X.TRACE                                         # lpbook.tracer: var/lp/trace.jsonl, kept

        def _trace(stage, **kw):
            if stage in ('tx', 'error') and not kw.get('dry'):
                self._txn += 1
                self._tx.append((self._txn, dict(_tx_brief(kw), stage=stage)))
                del self._tx[:-400]
            if prev:
                prev(stage, **kw)
        X.TRACE = _trace
        self.ac = lpbook.account(chain)
        from copydesk.treasury import Treasury
        self.tr = Treasury(chain, self.ac, self.c, self.log, self.note)       # USDG <-> ETH, the gas band, dust
        p = _state_path(chain)
        self.st = json.load(open(p)) if os.path.exists(p) else dict(next_id=0, leader={}, ours={}, hwm=0.0, realized=0.0,
                                                                     last_leader_ts=time.time())
        self.book_gas()
        self.dec, self.sym = {}, {}
        try:                                                          # token facts kept across restarts (Follow._token)
            for k, (d_, s_) in json.load(open(os.path.join(_dir(chain), 'tokens.json'))).items():
                self.dec[k], self.sym[k] = d_, s_
        except Exception:
            pass
        # THE ETH PRICE FROM THE BACKGROUND: an ETH copy's entry reads it, never waits for it
        _px['bg'] = True

        def px_loop():
            while True:
                try:
                    _read_px()
                except Exception:
                    pass
                time.sleep(5)
        __import__('threading').Thread(target=px_loop, daemon=True).start()
        if not self.st.get('capital'):
            # the desk's starting capital: free USDG + what its open copies cost (the card's P&L is measured from it)
            idle = X.balance(chain, self.u, self.ac.address) / 1e6
            self.st['capital'] = round(idle + sum(o['size_usd'] for o in self.st['ours'].values() if not o.get('closed')), 2)
            self.st['started'] = time.time()
        self.st['hwm'] = max(self.st.get('hwm') or 0.0, self.st['capital'])
        for o in self.st['ours'].values():
            if not o.get('symbol'):
                o['symbol'] = self.symbol(o['token'])                  # copies opened before cards named the token
            self.db(o, 'closed' if o.get('closed') else 'open')         # the store holds every copy, old ones included
        # LEADER MINTS BY WEBSOCKET: the PositionManager's Transfer from 0x0 to a leader arrives as its block lands, where the
        # poll saw it up to poll_s later (2026-09-29 17:43: a copy missed by ~1 s). The poll stays as the backstop.
        import queue, threading
        self.q, self.seen = queue.Queue(), set()
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        """Background thread: rh-node's websocket, two subscriptions, both queued for the main loop:
        - leader MINTS: the PositionManager's Transfer from 0x0 to a followed wallet -> ('mint', tokenId, wallet, heard);
        - leader EXITS (2026-09-30): every PoolManager ModifyLiquidity sent by the PositionManager whose salt (= tokenId)
          is a leader position we track and whose liquidity delta is negative -> ('exit', tokenId, heard). The 1 s poll found
          exits before: our burn landed a median 8 blocks after the leader's, the slowest tenth 88. The poll stays as
          the backstop for both. Reconnects forever."""
        import asyncio, websockets
        from eth_utils import keccak
        T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        MOD = '0x' + keccak(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
        mints = dict(address=POSM, topics=[T, '0x' + '0' * 64, ['0x' + '0' * 24 + a[2:] for a in self.watch]])
        mods = dict(address=self.chain.pm, topics=[MOD, None, '0x' + '0' * 24 + POSM[2:]])
        ladders = dict(address=MANAGERS, topics=[OPEN_V4, ['0x' + '0' * 24 + a[2:] for a in self.watch]])

        # THE NODE FIRST, THE CHAIN'S PUBLIC SOCKETS BEHIND IT (2026-10-01 09:14: rh-node's spot box was reclaimed and the desk
        # retried its dead address every 7 s, hearing mints and exits only through the 1 s poll): a failed connect moves on
        urls = ws_urls(self.chain)
        cur = [0]

        async def once():
            async with websockets.connect(urls[cur[0] % len(urls)], ping_interval=20, max_size=None) as ws:
                ids = {}
                for n, (tag, sub) in enumerate((('mint', mints), ('exit', mods), ('ladder', ladders)), 1):
                    await ws.send(json.dumps(dict(jsonrpc='2.0', id=n, method='eth_subscribe', params=['logs', sub])))
                    ids[json.loads(await ws.recv())['result']] = tag
                self.log('%s ws: subscribed to leader mints, managed ladders and exits on %s' % (time.strftime('%H:%M:%S'), urls[cur[0] % len(urls)]))
                async for m in ws:
                    p = json.loads(m).get('params') or {}
                    lg, tag = p.get('result') or {}, ids.get(p.get('subscription'))
                    if lg.get('removed'):
                        continue
                    if tag == 'mint' and len(lg.get('topics') or []) >= 4:
                        self.q.put(('mint', int(lg['topics'][3], 16), '0x' + lg['topics'][2][-40:].lower(), time.time()))
                    elif tag == 'ladder':
                        who, tids = managed_open(lg)
                        if tids:
                            self.q.put(('ladder', tids, who, time.time()))
                    elif tag == 'exit':
                        d = lg['data'][2:]
                        delta, tid = int(d[128:192], 16), int(d[192:256], 16)
                        tid = int((self.st.get('bins') or {}).get(str(tid), tid))  # a ladder bin: its ladder's copy
                        if delta >> 255 and str(tid) in self.st['leader']:      # liquidity out of a leader position we copy
                            self.q.put(('exit', tid, None, time.time()))
        while True:
            try:
                asyncio.run(once())
            except Exception as e:
                cur[0] += 1                                            # the next socket in the list
                self.log('%s ws: %s, reconnecting on %s' % (time.strftime('%H:%M:%S'), str(e)[:120], urls[cur[0] % len(urls)]))
            time.sleep(5)

    def owner_closes(self):
        """Close the copies the owner asked for (`lp close`: var/lp/close.req, leader tokenIds), through the desk's own exit."""
        p = close_path(self.chain)
        if not os.path.exists(p):
            return
        try:
            tids = [l.split()[0] for l in open(p) if l.strip()]
            os.remove(p)
        except Exception as e:
            return self.log('close request unreadable: %s' % str(e)[:120])
        for t in tids:
            o = self.st['ours'].get(str(t))
            if not o or o.get('closed'):
                self.rec(ev='skip', leader_tid=t, why='owner close: no open copy of leader #%s' % t)
                continue
            self.on_leader_exit(str(t), why='closed by the owner (lp close)')
        self.save()

    def exit_routes(self, o):
        """SMART EXIT ROUTING (owner 2026-10-01): the PoolKeys of the token's other hookless USDG / ETH pools that traded in 72 h
        (the heat table's 'routes', from home's history store), keys cached per pool. [] when off, unknown or alone."""
        if not self.c.get('smart_exit'):
            return []
        self.cold_pool(o['pool'])                                     # (re)loads the heat table when it changed
        h = getattr(self, '_heat', None) or {}
        if time.time() - h.get('ts', 0) > 1200:
            return []
        pids = [p for p in (h.get('routes') or {}).get(o['token'].lower(), []) if p.lower() != o['pool'].lower()]
        keys, cache = [], self.__dict__.setdefault('_keys', {})
        for p in pids:
            if p not in cache:
                try:
                    cache[p] = X.pool_key(self.chain, p)
                except Exception:
                    cache[p] = None                                    # never minted through the PositionManager: no key
            if cache[p] and not int(cache[p]['hooks'], 16):
                keys.append(cache[p])
        return keys

    def prefetch_routes(self, o):
        """At open, in the background: the route keys, so the exit itself only quotes."""
        import threading
        threading.Thread(target=lambda: self.exit_routes(o), daemon=True).start()

    def relay_shadow(self, o, route):
        """Relay's quote for the same sale, LOGGED BESIDE OURS, never executed (owner 2026-10-01: Relay quoted $66.92 where the
        best pool paid $72.43; if it ever wins consistently, the log says so)."""
        import threading, urllib.request
        def go():
            try:
                amt = int(route.get('amount') or 0)
                if not amt:
                    return
                body = json.dumps(dict(user=self.ac.address, originChainId=4663, destinationChainId=4663, originCurrency=o['token'],
                                       destinationCurrency=self.u, amount=str(amt), tradeType='EXACT_INPUT')).encode()
                r = json.load(urllib.request.urlopen(urllib.request.Request('https://api.relay.link/quote', body, {'Content-Type': 'application/json'}), timeout=15))
                out = float(((r.get('details') or {}).get('currencyOut') or {}).get('amountFormatted') or 0)
                self.rec(ev='route_shadow', leader_tid=o['leader_tid'], sym=o.get('symbol'), ours_usd=route['usd'], own_pool_usd=route.get('own_usd'),
                         relay_usd=round(out, 4), relay_better=out > route['usd'])
            except Exception as e:
                self.log('   relay shadow quote failed: %s' % str(e)[:80])
        threading.Thread(target=go, daemon=True).start()

    def young_pool(self, pid, hours, unread=False):
        """Was the pool created (its Initialize) in the last `hours`? One log query on the node; a pool found old stays old,
        a young one is re-asked once its hour could be over. Unreadable = `unread` (False: no cap; the min-age ban passes True).
        Cached per (pool, hours): an answer for one window says nothing about a longer one."""
        c = self.__dict__.setdefault('_young', {})
        k = (pid, hours)
        if k in c and (c[k] is False or c[k] > time.time()):
            return c[k] is not False
        try:
            # through the log-capable path (the node, else NODE_FALLBACK): the general reads go to public nodes that refuse
            # log queries - after rh-node was gone this retried ~36 s on EVERY entry and then read 'not young' (2026-10-01)
            head = int(_node_batch([('eth_blockNumber', [])], timeout=10)[0], 16)
            lo = head - int(hours * 36000)
            logs = _node_batch([('eth_getLogs', [dict(fromBlock=hex(lo), toBlock=hex(head), address=POOL_MANAGER, topics=[INITIALIZE, pid])])], timeout=10)[0] or []
        except Exception:
            return unread
        if logs:
            c[k] = time.time() + (int(logs[0]['blockNumber'], 16) - lo) / 10.0     # young until its hour is over
            return True
        c[k] = False
        return False

    def creator_in(self, pid, hours):
        """Is the pool's CREATOR - its first PositionManager LP - still in? The first ModifyLiquidity add the PositionManager sent
        to this pool in the last `hours` (salt = its tokenId), then that NFT's liquidity now. Unreadable = in (the cautious side)."""
        try:
            from eth_utils import keccak as _k
            head = int(_node_batch([('eth_blockNumber', [])], timeout=10)[0], 16)
            mod = '0x' + _k(text='ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)').hex()
            logs = _node_batch([('eth_getLogs', [dict(fromBlock=hex(head - int(hours * 36000)), toBlock=hex(head), address=POOL_MANAGER,
                                                      topics=[mod, pid, '0x' + '0' * 24 + POSM[2:]])])], timeout=10)[0] or []
            for l in logs:
                d = l['data'][2:]
                if int(d[128:192], 16) >> 255 == 0 and int(d[128:192], 16):              # liquidityDelta > 0: an add
                    tid = int(d[192:256], 16)
                    return _w(_batch([_call(POSM, '0x1efeed33' + '%064x' % tid)])[0], 0) > 0
            return True
        except Exception:
            return True

    def cold_pool(self, pid):
        """THE COLD-POOL RULE (owner 2026-09-30; lp tune downside: +$17-19/day on 9 days, twice, worst day -$4 vs -$31): skip
        a copy whose pool's last hour of volume is a z-score under cold_volz (-0.22 = the squad's bottom 30%) against its
        own 72 hours - var/lp_heat.json, written home every 5 min, pushed here every 30 s. Off without the knob; a table
        over 20 min old, or a pool not in it, never skips."""
        k, fk = self.c.get('cold_volz'), self.c.get('flat_rv24')
        if k is None and fk is None:
            return None
        p = os.path.join(self.chain.var, 'lp_heat.json')
        try:
            m = os.stat(p).st_mtime
            if getattr(self, '_heat_m', None) != m:
                self._heat, self._heat_m = json.load(open(p)), m
        except (OSError, ValueError):
            return None
        h = self._heat
        if time.time() - h.get('ts', 0) > 1200:
            return None
        z = h.get('pools', {}).get(pid.lower())
        if k is not None and z is not None and z < float(k):
            return 'cold pool: last hour volume z %.2f under %.2f' % (z, float(k))
        # PRICE-FLAT (owner 2026-09-30, lp tune downside: cold OR flat +$110.36/day, worst -$21.99 vs cold alone +$104.27 / -$51.21)
        rv = h.get('rv24', {}).get(pid.lower())
        if fk is not None and rv is not None and rv < float(fk):
            return 'price-flat pool: 24h volatility %.4f under %.4f' % (rv, float(fk))
        return None

    def squad_changed(self):
        """THE SQUAD WATCH (owner 2026-09-30: act the moment the lineup changes): chain.toml is stat'ed every pass (the mirror
        pushes it within 30 s of `lp pick --apply`); when it changed, the squad and every RELOAD knob are compared."""
        p = os.path.join(self.chain.dir, 'chain.toml')
        try:
            m = os.stat(p).st_mtime
        except OSError:
            return False
        if getattr(self, '_cfg_m', None) is None or m == self._cfg_m:
            self._cfg_m = m
            return False
        self._cfg_m = m
        try:
            from core import chain as cm_
            c2 = cm_.load(self.chain.name)
            if leaders(c2) != self.leaders or [x for x in bench(c2) if x not in leaders(c2)] != self.bench or \
                    [x for x in filler(c2) if x not in leaders(c2) and x not in bench(c2)] != self.filler or \
                    {k: c2.cfg['lp']['follow'].get(k) for k in RELOAD} != {k: self.c.get(k) for k in RELOAD}:
                self.rec(ev='restart', why='squad or knobs changed: leaders %s, bench %s' % (
                    ','.join(x[:6] for x in leaders(c2)), ','.join(x[:6] for x in bench(c2))))
                return True
        except Exception as e:
            self.log('config check failed: %s' % str(e)[:120])
        return False

    def reexec(self):
        """Restart in place on the new squad: the book saved, the cards queued sent, then the same command line in the
        same process (same PID: supervise never sees a gap, so it can never start a second signer)."""
        import sys
        self.save()
        t_ = time.time()
        while getattr(self, 'cards', None) and self.cards.unfinished_tasks and time.time() - t_ < 20:
            time.sleep(0.2)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, '-m', 'cli'] + sys.argv[1:])

    def sample_fees(self):
        """Every open copy's unclaimed fees, once a minute (fee_hist, the last room_waste_min + 5 minutes): what fee_rate reads."""
        opn = [o for o in self.st['ours'].values() if not o.get('closed')]
        if not opn:
            return
        vals, now = value_of(self, opn), time.time()
        keep = (float(self.c.get('room_waste_min', 0) or 0) + 5) * 60
        for o in opn:
            v = vals.get(o.get('token_id'))
            if v:
                o['fee_hist'] = [x for x in o.get('fee_hist') or [] if now - x[0] <= keep] + [[round(now, 1), round(v['fees'], 5)]]

    def drain(self, end=0.0):
        """Until `end`: every websocket mint and exit the listener queued, handled the moment it arrives. end=0: only what
        is queued now, without waiting - the loop runs this BEFORE each poll, so the websocket handles everything it heard
        and the poll only catches what it missed (2026-09-30: three exits heard at once, the poll closed two of them)."""
        while not getattr(self, 'stop', False):
            wait = end - time.time()
            try:
                kind, tid, who, heard = self.q.get(timeout=wait) if wait > 0 else self.q.get_nowait()
            except Exception:
                break
            try:
                if kind == 'mint':
                    self.mint(tid, who, heard, 'ws')
                elif kind == 'ladder':
                    self.mint(min(tid), who, heard, 'ws', bins=sorted(tid))
                elif str(tid) in self.st['leader']:
                    # the same rule as the poll: out when its liquidity is under exit_frac of its peak (a partial
                    # decrease that leaves more than that is not an exit)
                    L_ = self.leader_liq([str(tid)])[str(tid)]
                    lp_ = self.st['leader'][str(tid)]
                    lp_['peak'] = max(lp_['peak'], L_)
                    if L_ < self.c['exit_frac'] * lp_['peak']:
                        self.on_leader_exit(str(tid), heard=heard, path='ws')
            except Exception as e:
                self.log('ws %s failed: %s' % (kind, str(e)[:160]))
            self.save()

    def mint(self, tid, who, heard=None, path='poll', bins=None):
        """One place a leader mint is handled, from the websocket or the poll, never twice - not even across a restart: a
        websocket copy does not move next_id, so the poll after a restart meets the same mint again (and copied it twice)."""
        if tid in self.seen or str(tid) in self.st['ours'] or str(tid) in self.st['leader'] or (who or '').lower() in own_wallets():
            return
        self.seen.add(tid)
        self.on_leader_mint(tid, who, heard or time.time(), path, bins=bins)

    def leader_liq(self, keys):
        """Each tracked leader position's liquidity now, one batch: a ladder's is the sum of its bins."""
        ids = {t: [int(b) for b in (self.st['leader'].get(t) or {}).get('bins') or [t]] for t in keys}
        flat = [b for v in ids.values() for b in v]
        got = dict(zip(flat, _batch([_call(POSM, '0x1efeed33' + '%064x' % b) for b in flat]))) if flat else {}
        return {t: sum(int(got[b], 16) if got.get(b) else 0 for b in v) for t, v in ids.items()}

    def managed_poll(self):
        """The backstop for managed ladders, as the poll is for mints: ManagedOpenV4 logs naming a watched wallet since the
        last read, every 10 s (the websocket handles them first; mint() never copies one twice)."""
        if time.time() - getattr(self, '_mgr_t', 0) < 10:
            return
        self._mgr_t = time.time()
        head = int(_node_batch([('eth_blockNumber', [])])[0], 16)
        frm = self.st.get('mgr_blk') or head
        # at most the last 10 minutes (a ladder older than that is not worth copying, and public nodes refuse windows over
        # 100k blocks: from 09:14 a stale mgr_blk failed this every 10 s and aborted the rest of each poll, 2026-10-01)
        frm = max(frm, head - 6000)
        self.st['mgr_blk'] = head
        if head > frm:
            logs = _node_batch([('eth_getLogs', [dict(fromBlock=hex(frm + 1), toBlock=hex(head), address=MANAGERS,
                                                 topics=[OPEN_V4, ['0x' + '0' * 24 + a[2:] for a in self.watch]])])])[0] or []
            for lg in logs:
                who, tids = managed_open(lg)
                if tids:
                    self.mint(min(tids), who, time.time(), 'poll', bins=sorted(tids))

    def reconcile(self):
        """Unfinished entries (st['pending']) against the chain: an lp-1 NFT minted after the intent, in its pool and ticks,
        and not in the book is ADOPTED at the USDG that actually left the wallet; an intent with no such NFT is dropped
        (nothing was minted). Runs at start and after a failed entry."""
        pend = self.st.get('pending') or {}
        if not pend:
            return
        T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        me = '0x' + '0' * 24 + self.ac.address[2:].lower()
        head = int(_node_batch([('eth_blockNumber', [])])[0], 16)
        frm = head - int((time.time() - min(p['ts'] for p in pend.values())) / 0.1) - 600
        logs = _node_batch([('eth_getLogs', [dict(fromBlock=hex(max(0, frm)), toBlock='latest', address=POSM, topics=[T, '0x' + '0' * 64, me])]),
                       ('eth_getLogs', [dict(fromBlock=hex(max(0, frm)), toBlock='latest', address=self.u, topics=[T, me])])])
        booked = {o.get('token_id') for o in self.st['ours'].values()}
        mints = [(int(l['blockNumber'], 16), int(l['topics'][3], 16)) for l in logs[0] or [] if int(l['topics'][3], 16) not in booked]
        outs = [(int(l['blockNumber'], 16), int(l['data'], 16)) for l in logs[1] or []]
        # an ETH copy also got USDG back (the unwind of what the mint did not take): its cost is out minus in
        ins = [(int(l['blockNumber'], 16), int(l['data'], 16)) for l in (_node_batch([('eth_getLogs', [dict(
            fromBlock=hex(max(0, frm)), toBlock='latest', address=self.u, topics=[T, None, me])])])[0] or [])]
        for t, o in list(pend.items()):
            hit = None
            for b, nft in mints:
                p = position(nft)
                if p and p[4].lower() == o['pool'].lower() and (p[1], p[2]) == (o['tick_lower'], o['tick_upper']) and p[3] > 0:
                    hit = (b, nft, p[3])
                    break
            if hit:
                b, nft, L = hit
                start = b - int((time.time() - o['ts']) / 0.1) - 600
                cost = sum(v for bb, v in outs if start <= bb <= b) / 1e6
                if o.get('quote') == 'eth':
                    cost -= sum(v for bb, v in ins if start <= bb <= b + 600) / 1e6
                o = {k: v for k, v in o.items() if k != 'ts'}
                o.update(token_id=nft, liquidity=L, planned_usd=o['size_usd'], size_usd=round(cost, 4) if cost > 0 else o['size_usd'])
                self.st['ours'][t] = o
                mints = [m for m in mints if m[1] != nft]
                self.rec(ev='adopt', leader_tid=int(t), pool=o['pool'], token_id=nft, size_usd=o['size_usd'],
                         why='minted before a restart or a failed step, now in the book')
            pend.pop(t)
        self.save()

    def _burned_back(self, o):
        """Our copy's NFT is already burned (a death between the exit's burn and the book): what came back = the USDG the
        burn tx paid lp-1 plus whatever selling the leftover token brings now. None if the burn is not found."""
        T = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        me = '0x' + '0' * 24 + self.ac.address[2:].lower()
        head = int(_node_batch([('eth_blockNumber', [])])[0], 16)
        frm = head - int((time.time() - o['entry_ts']) / 0.1) - 600
        lg = _node_batch([('eth_getLogs', [dict(fromBlock=hex(max(0, frm)), toBlock='latest', address=POSM,
                                           topics=[T, me, '0x' + '0' * 64, '0x%064x' % o['token_id']])])])[0]
        if not lg:
            return None
        if o.get('quote') == 'eth':
            # native ETH leaves no Transfer log: sell the leftover token, then sell ETH above the gas band; what that
            # brings in USDG is the return (approximate: the band's own excess, if any, is counted with it)
            u0 = X.balance(self.chain, self.u, self.ac.address)
            try:
                e0 = self.tr.eth()
                held = sum(int(x['unsold']['amount']) for x in self.st['ours'].values() if x.get('unsold') and x['token'].lower() == o['token'].lower())
                amt = X.balance(self.chain, o['token'], self.ac.address) - held     # never another copy's held token
                if amt > 0:                                            # smart-routed like every exit (lp.sell_best: aggregators, pools, floor)
                    X.sell_best(self.chain, self.ac, o['token'], amt, [o['key']] + self.exit_routes(o), eth_px(), self.relay_cfg(), True, self.log,
                                dict(leg='burned', sym=o.get('symbol', '')), floor=float(self.c.get('exit_floor', X.EXIT_FLOOR)))
                self.tr.unwind(e0, 'leftover of a burned copy')
                self.tr.settle(topup=False)
            except Exception as e:
                self.log('   burned ETH copy unwind failed: %s' % str(e)[:120])
            return (X.balance(self.chain, self.u, self.ac.address) - u0) / 1e6
        dec = o['d0'] if o['usdg_is_c0'] else o['d1']
        b = int(lg[-1]['blockNumber'], 16)
        # the burn tx pays the USDG side; the token side comes back in the SELL txs that follow (OFY 19:11: $2.41 + $29.24):
        # count USDG into lp-1 from the burn tx and from any tx in the next minute that sent this token out of lp-1
        ins, outs = _node_batch([('eth_getLogs', [dict(fromBlock=hex(b), toBlock=hex(b + 600), address=self.u, topics=[T, None, me])]),
                            ('eth_getLogs', [dict(fromBlock=hex(b), toBlock=hex(b + 600), address=o['token'], topics=[T, me])])])
        sold = {l['transactionHash'] for l in outs or []} | {lg[-1]['transactionHash']}
        got = sum(int(l['data'], 16) for l in ins or [] if l['transactionHash'] in sold) / 10 ** dec
        u0 = X.balance(self.chain, self.u, self.ac.address)
        try:
            held = sum(int(x['unsold']['amount']) for x in self.st['ours'].values() if x.get('unsold') and x['token'].lower() == o['token'].lower())
            amt = X.balance(self.chain, o['token'], self.ac.address) - held     # never another copy's held token
            if amt > 0:                                                # smart-routed like every exit (lp.sell_best)
                X.sell_best(self.chain, self.ac, o['token'], amt, [o['key']] + self.exit_routes(o), eth_px(), self.relay_cfg(), True, self.log,
                            dict(leg='burned', sym=o.get('symbol', '')), floor=float(self.c.get('exit_floor', X.EXIT_FLOOR)))
        except Exception as e:
            self.log('   leftover sell failed: %s' % str(e)[:120])
        return got + (X.balance(self.chain, self.u, self.ac.address) - u0) / 10 ** dec

    def save(self):
        json.dump(self.st, open(_state_path(self.chain) + '.tmp', 'w'), indent=1)
        os.replace(_state_path(self.chain) + '.tmp', _state_path(self.chain))

    def txs_since(self, n0, claim=True):
        """The sends since mark n0 that no earlier record took: a copy closed to make room books its own exit, and the
        open that made the room gets only its own txs."""
        seen = self.__dict__.setdefault('_claimed', set())
        out = [t for n, t in getattr(self, '_tx', []) if n > n0 and n not in seen]
        if claim:
            seen.update(n for n, t in getattr(self, '_tx', []) if n > n0)
            if len(seen) > 2000:
                seen.difference_update(sorted(seen)[:1000])
        return out

    def rec(self, **r):
        r = dict(ts=time.time(), **r)
        if r.get('ev') == 'skip':
            r.setdefault('code', skip_code(r.get('why')))
        with open(_log_path(self.chain), 'a') as f:
            f.write(json.dumps(r, default=str) + '\n')
        self.log('%s %s' % (time.strftime('%H:%M:%S'), ' '.join('%s=%s' % (k, v) for k, v in r.items() if k not in ('ts',))))
        if r.get('ev') in ('stop', 'error'):
            try:
                self.note('lp copy %s: %s' % (r['ev'], r.get('why')))
            except Exception:
                pass

    def _token(self, t):
        """A token's decimals and symbol: read once, in ONE call, and kept in var/lp/tokens.json across restarts (a new
        token's two reads were part of the slow first copies)."""
        if t not in self.dec or t not in self.sym:
            d, s = _batch([_call(t, '0x313ce567'), _call(t, '0x95d89b41')])
            self.dec[t] = int(d, 16) if d and d != '0x' and len(d) <= 66 else 18
            try:
                self.sym[t] = bytes.fromhex(s[130:130 + 2 * int(s[66:130], 16)]).decode()
            except Exception:
                self.sym[t] = t[:8]
            try:
                p = os.path.join(_dir(self.chain), 'tokens.json')
                json.dump({k: [self.dec[k], self.sym.get(k)] for k in self.dec}, open(p + '.tmp', 'w'))
                os.replace(p + '.tmp', p)
            except Exception:
                pass
        return self.dec[t], self.sym[t]

    def decimals(self, t):
        return self._token(t)[0] if t != NATIVE else 18

    def symbol(self, t):
        return self._token(t)[1] if t != NATIVE else 'ETH'

    # ---- watch
    def poll(self):
        try:
            self.managed_poll()
        except Exception as e:                                         # a failed ladder read never costs the mint / exit backstop
            self.log('managed poll failed: %s' % str(e)[:160])
        nid = _w(_batch([_call(POSM, '0x75794a3c')])[0], 0)
        if not self.st['next_id']:
            self.st['next_id'] = nid                                  # first run: only positions minted from now on
        ids = list(range(self.st['next_id'], nid))
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            for tid, own in zip(chunk, _batch([_call(POSM, '0x6352211e' + '%064x' % t) for t in chunk])):
                if own and len(own) >= 66 and '0x' + own[-40:].lower() in self.watch:
                    self.mint(tid, '0x' + own[-40:].lower(), time.time(), 'poll')
        self.st['next_id'] = max(self.st['next_id'], nid)
        # ONLY THE POSITIONS WE COPY ARE WATCHED FOR AN EXIT (2026-10-01: every leader mint was tracked, skipped ones too - 185
        # positions re-read each pass for 5 copies, 4-28 s a pass on public endpoints, every mint behind it waiting). A mint
        # we did not copy is dropped once next_id has passed it (no restart can meet it again)
        held = {str(o['leader_tid']) for o in self.st['ours'].values() if not o.get('closed')}
        for t in [t for t in self.st['leader'] if t not in held and int(t) < self.st['next_id']]:
            for b_ in self.st['leader'].pop(t).get('bins') or []:
                (self.st.get('bins') or {}).pop(b_, None)
        tr = [t for t in self.st['leader'] if t in held]
        if tr:
            liq = self.leader_liq(tr)
            for t in tr:
                L = liq[t]
                lp_ = self.st['leader'][t]
                lp_['peak'] = max(lp_['peak'], L)
                if L < self.c['exit_frac'] * lp_['peak']:
                    self.on_leader_exit(t, heard=time.time(), path='poll')
        self.save()

    # ---- entry
    def on_leader_mint(self, tid, who=None, heard=None, path='poll', bins=None):
        # TIMINGS (2026-09-30): heard -> handling starts (the queue), handling -> first send (reads, room), the whole entry
        t0 = time.time()
        n0 = getattr(self, '_txn', 0)
        tm = dict(path=path, heard_ms=round((t0 - (heard or t0)) * 1e3))
        self.st['last_leader_ts'] = time.time()
        # [lp.follow.hours] "0x.." = [6, 12, ...]: that leader is copied only in those 3-hour UTC blocks (block start hours,
        # as `lp optimize` prints them); a leader not listed is copied at every hour
        hrs = {k.lower(): v for k, v in (self.c.get('hours') or {}).items()}.get(who or self.leader)
        if hrs and time.gmtime().tm_hour // 3 * 3 not in hrs:
            return self.rec(ev='skip', leader_tid=tid, why='outside its hours (%s UTC)' % ','.join('%02d' % h for h in hrs))
        p = position(tid)
        if not p:
            return self.rec(ev='skip', leader_tid=tid, why='unreadable position')
        key, tl, tu, L, pid = p
        ladder = None
        if bins and len(bins) > 1:
            # A MANAGED LADDER IS COPIED AS ONE RANGE over all its bins (lpcopy.union, research/laddershape: union beat every
            # bin on 82% of 2,273 Delta ladders). Its leader liquidity for the exit rule is the sum of the bins
            ladder = [position(b) for b in bins]
            if any(x is None for x in ladder) or len({x[4] for x in ladder}) != 1:
                return self.rec(ev='skip', leader_tid=tid, leader=who, why='ladder of %d bins unreadable or across pools' % len(bins))
            tl, tu, L = min(x[1] for x in ladder), max(x[2] for x in ladder), sum(x[3] for x in ladder)
            for b in bins:
                self.st.setdefault('bins', {})[str(b)] = str(tid)
        self.st['leader'][str(tid)] = dict(peak=L, pid=pid, **(dict(bins=[str(b) for b in bins]) if ladder else {}))
        c0, c1 = key['currency0'].lower(), key['currency1'].lower()
        why = None
        why = uncopyable(c0, c1, key['hooks'], pid, self.u, self.c.get('deny_pools') or (), native=bool(self.c.get('native', True)))
        if why:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, why=why)
        eth = c0 == NATIVE                                             # an ETH-quoted pool: the treasury funds it
        if eth and self.tr.stopped():
            return self.rec(ev='skip', leader_tid=tid, pool=pid, why='treasury stopped: %s' % self.tr.stopped())
        q = NATIVE if eth else self.u
        uc0 = c0 == q
        tok = c1 if uc0 else c0
        agree = self.agreement(tok, who or self.leader)               # other wallets, before this mint is counted
        self.heard_mint(tok, who or self.leader)
        why = self.token_block(tok)
        me_ = self.tier(who or self.leader)
        low_tok = lambda: [o for o in self.st['ours'].values() if not o.get('closed') and o.get('token', '').lower() == tok.lower()
                           and self.tier(o.get('leader')) > me_]
        # a token cap that a lower tier's copy can give way to is decided at the end, with the room (cap_room below); any
        # other block, or a cap nobody beneath us can give way to, is a skip now
        cap_room = bool(why) and why.startswith('token cap') and bool(low_tok())
        if why and not cap_room:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why=why)
        if me_ == 2 and float(self.c.get('filler_consensus_h') or 0) and not agree:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why='no squad agreement: no other squad wallet in %s in %g h' % (
                self.sym.get(tok, tok[:10]), float(self.c['filler_consensus_h'])))
        d0, d1 = self.decimals(c0), self.decimals(c1)
        sp = pool_state(pid, tl, tu)[0]
        a0, a1 = amounts(L, sp, tl, tu)
        qpx = eth_px() if eth else 1.0                                 # dollars per unit of the quote
        lead_usd = usd_value(a0, a1, sp, uc0, d0, d1) * qpx
        if ladder:
            # the ladder's dollars are its bins'; L becomes the one range's liquidity holding them (thin-pool share, sizes)
            lead_usd = sum(usd_value(*amounts(x[3], sp, x[1], x[2]), sp, uc0, d0, d1) for x in ladder) * qpx
            u1 = usd_value(*amounts(1e18, sp, tl, tu), sp, uc0, d0, d1) * qpx
            L = lead_usd / u1 * 1e18 if u1 else L
        if lead_usd < self.c['min_leader_usd']:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, why='leader position $%.2f' % lead_usd)
        if os.path.exists(stop_path(self.chain)):
            return self.rec(ev='skip', leader_tid=tid, pool=pid, why='STOP')
        why = self.wallet_block(who or self.leader)
        if why:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why=why)
        # SKIPS THAT NEED NO ROOM COME FIRST: room() may close open copies to make space, so a copy it made room for must
        # not be skipped afterwards (2026-09-30: the thin-pool check ran after room). Cold pool, then thin at the base size
        # NO LAUNCHES (owner 2026-10-04, "permanently fix this", after the night of 10-03: HOODAGENTS -$86.20, ORBTOPIA -$39.71
        # and -$32.72, SIGH x4 -$72.59, every one in a pool 6-15 min old, and the drawdown stop tripped again). 10 days of LIVE
        # closes (902 copies, copydesk/launchtag), dropping copies in pools younger than N hours: all +-$12.46, worst day -$167,
        # drawdown $430, worst copy -$92; >= 0.5 h +$273 / -$141 / $172 / -$79; >= 2 h +$188 / -$51 / $70 / -$38; >= 3 h
        # +$192 / -$51 / $66 / -$38 (6 of 7 days positive from 2 h up). The pools under 30 min alone: -$285 on 172 copies, 20
        # of them losing 20%+ for -$721. Unreadable age = young (skipped): a missed copy costs nothing, a launch costs the day
        ma = float(self.c.get('min_pool_age_h', 0) or 0)
        if ma and self.young_pool(pid, ma, unread=True):
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why='launch: pool under %gh old' % ma)
        why = self.cold_pool(pid)
        if why:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why=why)
        mx = float(self.c.get('thin_max_share', 0) or 0)
        if mx:
            base = self.sizing(sum(o['size_usd'] for o in self.st['ours'].values() if not o.get('closed')))[0]
            pool_L = _w(_batch([_call(SV, '0xfa6793d5' + pid[2:])])[0], 0)
            if thin_share(L, lead_usd, base, pool_L) > mx:
                return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who,
                                why='thin pool: even a base copy would be %.0f%% of its active liquidity' % (100 * thin_share(L, lead_usd, base, pool_L)))
        # ENTRY COST (owner 2026-10-02, lp tune downside 9 days, one continuous run with every live rule): a range copy starts
        # by swapping its token share, and in a thin or one-sided pool that swap alone costs percents it rarely earns back.
        # Skipped above max_entry_cost of the copy: 3% +$140.03/day vs +$130.74, worst day +$22.52 vs -$1.02, max drawdown
        # 57 vs 72, ahead 6 of 9 days, winners kept $1,925 vs $1,922; 2% agrees (+$139.17, 6/9), 1% / 0.5% +$141.91 / +$138.46
        # (5/9). The swap _fund would make, quoted exactly, at the BASE size (as the replay measured it), before any room is made
        cf = None
        ec = float(self.c.get('max_entry_cost', 0) or 0)
        if ec:
            base_ = self.sizing(sum(o['size_usd'] for o in self.st['ours'].values() if not o.get('closed')))[0]
            try:
                cf = X.entry_cost_frac(self.chain, key, tl, tu, int(base_ / qpx * 10 ** (d0 if uc0 else d1)))
            except Exception as e:
                self.log('   entry cost unreadable (%s): not skipped for it' % str(e)[:80])
            if cf is not None and cf > ec:
                return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who,
                                why='entry cost: our entry swap would cost %.1f%% of the copy (over %.1f%%)' % (100 * cf, 100 * ec))
        # THE CAP RESPECTS THE TIERS (2026-09-30: bench 0x63fb held 4 VRAX copies and main 0x87ef's 5 VRAX mints were all
        # refused by the cap): a copy that the cap refuses closes the newest open copy of that token from a LOWER tier, as
        # the room rule does for slots, and goes in. LAST, after every skip that needs no room (2026-10-01 13:45: it ran
        # first, so a bench mint the wallet limit then refused had already closed a filler copy - 7 copies opened and closed
        # within seconds in one PRIORS burst, each an exit and a re-entry for nothing)
        while cap_room and why is None:
            why = self.token_block(tok)
            if not why or not why.startswith('token cap'):
                break
            low = low_tok()
            if not low:
                break
            v = max(low, key=lambda o: (self.tier(o.get('leader')), o.get('entry_ts') or 0))
            self.on_leader_exit(str(v['leader_tid']), why='made room for a %s copy of %s (token cap)' % (self.TIERS[me_], v.get('symbol')))
            if not self.st['ours'][str(v['leader_tid'])].get('closed'):
                break
            why = None
        if why:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why=why)
        opn, deployed, size, why = self.room(self.tier(who or self.leader))
        if why:
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why=why)
        # THE STOP, AGAIN: a copy closed to make room (above, or for the token cap) books its loss, and that can trip the drawdown
        # stop - which this entry checked BEFORE making room (2026-10-02 06:06: room() closed CME at -$37.50, the stop tripped,
        # and VRAX still went in 4 s later). A stop tripped while making room stops this entry too
        if os.path.exists(stop_path(self.chain)):
            return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why='STOP (tripped while making room)')
        # THE LAUNCH SIZE GUARD (owner 2026-10-01, after ZIP: our $80 in a 30-min-old pool beside the leader's $60 lost 98.7%):
        # in a pool younger than launch_cap_h hours, never more than the leader's own position. 9 days: max drawdown
        # $124 -> $66 for 1.2% of the total
        lc = self.c.get('launch_cap_h')
        if lc and size > lead_usd and self.young_pool(pid, float(lc)):
            self.log('   launch size guard: pool under %gh old, $%.2f -> the leader\'s $%.2f' % (float(lc), size, lead_usd))
            size = round(lead_usd, 2)
        # THE LAUNCH CREATOR GUARD (owner 2026-10-03, "fucking fix this already", after ASTRAL -$92.36 in a 3-min-old pool and
        # HOODAGENTS -$61.12 in a 16-min-old one, both with the pool's creator still in): in a pool under launch_cap_h hours old
        # whose creator has not pulled liquidity yet, the copy goes in at launch_creator_frac of its size (0 = skipped). Live
        # closes, 7 days: such copies -$28.70 on 121 (+$63.66 without ASTRAL - a small win until one launch dump); research/launch:
        # the robot's bid there -4.6 / -6.3 %. On the owner's word; lp tune downside to confirm the fraction
        fr = self.c.get('launch_creator_frac')
        if fr is not None and lc and self.young_pool(pid, float(lc)) and self.creator_in(pid, float(lc)):
            if float(fr) <= 0:
                return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why='launch: pool under %gh old, its creator still in' % float(lc))
            self.log('   launch creator guard: pool under %gh old, creator still in: $%.2f -> $%.2f' % (float(lc), size, size * float(fr)))
            size = round(size * float(fr), 2)
        # THIN POOLS ARE SKIPPED (owner 2026-09-30, lp tune downside): our copy over thin_max_share of the pool's active
        # liquidity - where the replay's fee share is least reliable and our own exit sale moves the price most. Seven days
        # at desk level, twice: 50% -> +$70.44 / +$63.93 a day vs +$47.69 / +$57.24, same worst day, 5 of 7 days better
        mx = float(self.c.get('thin_max_share', 0) or 0)
        if mx:
            pool_L = _w(_batch([_call(SV, '0xfa6793d5' + pid[2:])])[0], 0)      # StateView getLiquidity(poolId)
            sh = thin_share(L, lead_usd, size, pool_L)
            if sh > mx:
                return self.rec(ev='skip', leader_tid=tid, pool=pid, leader=who, why='thin pool: our copy would be %.0f%% of its active liquidity' % (100 * sh))
        sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
        quote_only = (sp <= sa) if uc0 else (sp >= sb)                 # the range holds only USDG at this price
        o = dict(leader_tid=tid, leader=who or self.leader, pool=pid, key=key, token=tok, symbol=self.symbol(tok), usdg_is_c0=uc0, tick_lower=tl, tick_upper=tu,
                 **(dict(bins=len(bins)) if ladder else {}),
                 d0=d0, d1=d1, size_usd=size, entry_ts=time.time(), quote_only=quote_only, leader_usd=round(lead_usd, 2),
                 quote='eth' if eth else 'usdg')
        # WRITE-AHEAD: the intent is on disk before anything is sent, so a death mid-entry leaves a record reconcile() can
        # match against the chain (an NFT minted but never booked would never be exited)
        self.st.setdefault('pending', {})[str(tid)] = dict(o, ts=time.time())
        self.save()
        tm['prep_ms'] = round((time.time() - t0) * 1e3)
        u0 = X.balance(self.chain, self.u, self.ac.address)
        tk0 = X.balance(self.chain, tok, self.ac.address)
        rc0 = len(X.RECEIPTS)                                          # this entry's own txs from here (usdg_flow)
        mark = None
        try:
            raw = int(size / qpx * 10 ** (d0 if uc0 else d1))
            if eth:
                mark = self.tr.buy(raw, 'copy of %s #%s' % (o['symbol'], tid))       # USDG -> the ETH this copy needs
            if quote_only:
                try:
                    o['token_id'], o['liquidity'] = X.mint_quote_only(self.chain, self.ac, key, tl, tu, raw, True, self.log,
                                                                     dict(pool=pid, leader_tid=tid, sp=int(sp * 2 ** 96)))
                except RuntimeError as e:
                    # the price crossed into the range between our read and the mint (2026-09-29 17:43, leader #3418634):
                    # it raises before any send, so the copy goes in as a normal two-sided entry instead of being missed
                    if 'not one-sided' not in str(e):
                        raise
                    quote_only = o['quote_only'] = False
            if not quote_only:
                r = X.enter(self.chain, self.ac, dict(key=key, token=tok, pool=pid, tick_lower=tl, tick_upper=tu, symbol=o['symbol']),
                            raw, True, self.log)
                o['token_id'], o['liquidity'] = r['token_id'], r['liquidity']
        except Exception as e:
            # the entry swap landed but the mint did not: sell back what it bought, or the token sits in the wallet with no
            # record that would ever sell it (the treasury's dust sweep reads only closed copies; 2026-10-02 V4, $2.60).
            # Not when the mint itself may have landed: reconcile() adopts that NFT and the token is inside it.
            undo = {}
            if 'failed: 0x' in str(e) or 'would revert' in str(e):        # reverted on chain / refused by estimateGas
                try:
                    got = X.balance(self.chain, tok, self.ac.address) - tk0
                    if got > 0:
                        # the best venue, no floor: what was bought seconds ago is sold back, never left without a record
                        ok_, _ = X.sell_best(self.chain, self.ac, tok, got, [key] + self.exit_routes(dict(token=tok, pool=pid)), eth_px(),
                                             self.relay_cfg(), True, self.log, dict(pool=pid, leg='entry undo'), floor=0.0, tries=2)
                        if not ok_:
                            raise RuntimeError('no venue quoted the undo')
                        undo['undo'] = 'sold back'
                except Exception as e2:
                    undo['undo'] = 'failed: %s (the token stays in the wallet)' % str(e2)[:120]
            if mark is not None:
                try:
                    self.tr.unwind(mark, 'entry of #%s failed: its ETH back to USDG' % tid)
                except Exception as e2:
                    self.log('   unwind failed: %s (the beat sells ETH above the band)' % str(e2)[:120])
            if undo and not eth:                                       # what the failed entry cost (swap fees both ways)
                undo['lost_usd'] = round((u0 - X.balance(self.chain, self.u, self.ac.address)) / 10 ** (d0 if uc0 else d1), 4)
            self.rec(ev='error', leader_tid=tid, pool=pid, at='entry', why=str(e)[:200] + ''.join(
                ' · %s %s' % (k, v) for k, v in undo.items()), **undo)
            return self.reconcile()                                    # a later step failed after the mint: adopt it
        if mark is not None:
            try:
                self.tr.unwind(mark, 'what the mint of #%s did not take' % tid)
            except Exception as e:
                self.log('   unwind failed: %s (the beat sells ETH above the band)' % str(e)[:120])
        # THE COST IS WHAT LEFT THE WALLET, not the planned size: a two-sided entry whose mint takes less than planned leaves
        # the rest as free USDG (OFY 2026-09-29 19:04: $56.26 planned, $31.36 spent, booked as a -43.8% loss that was +$0.29).
        # An ETH copy is measured the same way, in USDG: bought ETH minus what the unwind sold back, conversions included
        # THE COST FROM THIS ENTRY'S OWN TXS (2026-10-01: balance reads at different public heads booked a $95.04 copy at
        # $26.54): the USDG its receipts moved out of the wallet; no receipt (nothing landed) = the balance delta as before
        rcs = X.RECEIPTS[rc0:]
        spent = -X.usdg_flow(self.chain, self.ac.address, rcs) / 1e6 if rcs else (u0 - X.balance(self.chain, self.u, self.ac.address)) / 1e6
        if spent > 0:
            o['planned_usd'], o['size_usd'] = size, round(spent, 4)
            size = o['size_usd']
        self.st['ours'][str(tid)] = o
        self.st['pending'].pop(str(tid), None)
        self.db(o, 'open')
        tm['entry_ms'] = round((time.time() - t0) * 1e3)
        self.prefetch_routes(o)
        txs = self.txs_since(n0)
        o['gas_usd'] = round(sum(t.get('gas_usd') or 0 for t in txs), 4)               # the copy's cost, booked at its close
        self.rec(ev='open', leader_tid=tid, pool=pid, sym=o['symbol'], size_usd=size, leader_usd=o['leader_usd'],
                 quote_only=quote_only, ticks=[tl, tu], token_id=o.get('token_id'), quote=o['quote'], **tm,
                 leader=o.get('leader'), tier=self.TIERS[self.tier(o.get('leader'))], planned_usd=o.get('planned_usd'),
                 entry_cost_pct=round(100 * cf, 2) if cf is not None else None,
                 gas_usd=round(sum(t.get('gas_usd') or 0 for t in txs), 4), txs=txs)
        w = o['leader']
        self.card('▲ %s $%.2f · %s…%s ($%.0f)' % (pair(o), size, w[:6], w[-4:], o['leader_usd']))

    def heard_mint(self, tok, who, now=None):
        """Every squad mint the desk hears in a copyable pool, copied or not: (time, wallet) per token, 24 h, kept in the
        desk's state so a restart (every squad change re-execs) does not forget the last two hours."""
        now = now or time.time()
        m = self.st.setdefault('mints', {})
        t = tok.lower()
        m[t] = [x for x in m.get(t, []) if now - x[0] <= 86400] + [[now, (who or '').lower()]]
        if len(m) > 2000:
            for k in [k for k, v in m.items() if not v or now - v[-1][0] > 86400]:
                m.pop(k, None)

    def agreement(self, tok, who, now=None):
        """SQUAD AGREEMENT (owner 2026-10-02, lp tune downside 9 days, one continuous run with every live rule): a filler
        copy only when ANOTHER squad wallet minted the same token in the last filler_consensus_h hours - +$135.63/day vs
        +$114.64, worst day +$16.93 vs -$15.91, max drawdown 72 vs 86, ahead 7 of 9 days, more kept on winners ($1,950 vs
        $1,914); 24 h agrees (+$135.14, 7/9). Residual test (research/lpfactor): rank IC +0.121 (t 2.6) on the copy P&L
        that activity, volatility and depth leave, twice the cold-pool rule's. -> the other wallets in that window."""
        h = float(self.c.get('filler_consensus_h') or 0)
        now = now or time.time()
        me = (who or '').lower()
        return {w for t, w in self.st.get('mints', {}).get(tok.lower(), []) if now - t <= h * 3600 and w != me}

    # ---- exit
    def token_block(self, tok):
        """Why a copy of `tok` is refused, or None (owner 2026-09-29):
        - deny_tokens ([lp.follow], addresses): out for good, by hand;
        - COOL-OFF: our last >= cool_n closed copies of it inside cool_h hours net below zero -> out until they age out
          (SI on 09-29: 11 copies -$16.26, its leader -0.5% on the same positions; DOTS +$11.43 over 7 stays in). It
          decides from our own results and lifts by itself, where a blacklist is a guess that never expires;
        - CAP: at most max_per_token copies of one token open (0x12c2 ladders ranges: 4 DEADBEEF copies were open at once)."""
        t = tok.lower()
        if t in {x.lower() for x in (self.c.get('deny_tokens') or [])}:
            return 'denied token'
        mine = [o for o in self.st['ours'].values() if o.get('token', '').lower() == t]
        cap = int(self.c.get('max_per_token', 0))                      # 0 = off (the backtest's pick, 2026-09-29)
        if cap and sum(1 for o in mine if not o.get('closed')) >= cap:
            return 'token cap: %d copies of it open' % cap
        h, n = float(self.c.get('cool_h', 24.0)), int(self.c.get('cool_n', 8))
        recent = [o for o in mine if o.get('closed') and time.time() - (o.get('exit_ts') or 0) < h * 3600]
        gross = sum(o.get('pnl_gross_usd', o.get('pnl_usd')) or 0 for o in recent)   # the rule was backtested before gas
        if len(recent) >= n and gross < 0:
            return 'token cooling off: our last %d copies of it %+.2f in %gh' % (len(recent), gross, h)
        return None

    TIERS = ('main', 'bench', 'filler')

    def wallet_block(self, who):
        """Why a copy is refused for its WALLET, or None: a bench (or filler) wallet may have at most bench_per_wallet
        (filler_per_wallet) copies open; 0 = no limit, mains never. Six blind days (2026-09-30): bench 2 +$196.23/day,
        worst day -$11.39 vs no limit +$184.46 / -$26.80, 5 of 6 days better - one wallet's burst no longer fills the book."""
        t = self.tier(who)
        n = int(self.c.get(('', 'bench_per_wallet', 'filler_per_wallet')[t]) or 0) if t else 0
        if n and sum(1 for o in self.st['ours'].values() if not o.get('closed') and (o.get('leader') or '').lower() == (who or '').lower()) >= n:
            return 'wallet limit: %d copies of %s open (%s)' % (n, (who or '')[:6], self.TIERS[t])
        return None

    def tier(self, who):
        """0 main, 1 bench (specialists included), 2 filler; a wallet no longer followed ranks with the filler (its open copy
        is still ours and still exits with it, but it gives way first)."""
        w = (who or '').lower()
        return 0 if w in self.leaders else 1 if w in self.bench else 2

    def room(self, tier):
        """(open copies, deployed $, copy size, why-not or None). THE PRIORITY RULE (owner 2026-09-30, idle capital), three
        tiers - main > bench > filler:
        - a tier below main opens only with `reserve` more slots (and their base size) still free for a main copy;
        - a copy that does not fit closes the open copies of LOWER tiers, the lowest tier and the newest first, until it
          does - so nobody is ever skipped for capital while something beneath it is open.
        SIZE (size_free_div): the larger of size_frac of the book and 1/div of the free capital, within what is free after
        the reserve: an idle book puts more to work per copy, a full one copies at the base size. Five blind one-day folds
        (2026-09-30, fee dilution measured in): filler >= $10/day + free/3 + reserve 0 beat the live rule on all five days,
        +$127.39/day vs +$87.72, worst day -$23.32 vs -$29.39, capital deployed 71% vs 31%."""
        def now():
            opn = [o for o in self.st['ours'].values() if not o.get('closed')]
            dep = sum(o['size_usd'] for o in opn)
            base, cap = self.sizing(dep)
            return opn, dep, base, cap
        r = int(self.c.get('reserve', 1)) if tier else 0
        div = float(self.c.get('size_free_div', 0) or 0)
        n_slots = [int(self.c['max_open'])]

        def plan(opn, dep, base, cap):
            free = cap - dep
            n_slots[0] = self.slots(cap)
            div_ = div if n_slots[0] == int(self.c['max_open']) else 0     # dynamic slots: every copy book / slots, no free/div
            # 0.5% headroom on what is free: an ETH copy's treasury buy adds 0.3% (2026-09-30: the last slot took all $42.65
            # free and the buy needed $42.78)
            size = min(max(base, free / div_) if div_ else base, (free - r * base) / 1.005)
            # last_slot_frac < 1: the last of the capital opens a copy when it is at least that share of a base copy
            ok = len(opn) + 1 + r <= n_slots[0] and size >= float(self.c.get('last_slot_frac', 1) or 1) * base / 1.005 - 0.01
            return round(size, 2), ok
        opn, dep, base, cap = now()
        size, ok = plan(opn, dep, base, cap)
        # CAPITAL AT WASTE GIVES WAY FIRST (owner 2026-09-30, lp tune downside, 9 days: +$89.64/day vs +$73.15, 7 of 9 days
        # better): a copy that does not fit closes the open copy, of any tier, earning the least over the last room_waste_min
        # minutes, if that is <= room_waste_rate % of its size per hour
        w = float(self.c.get('room_waste_min', 0) or 0) * 60
        while not ok and w:
            t_ = time.time()
            dead = [(fee_rate(o, t_, w), o) for o in opn]
            dead = [z for z in dead if z[0] is not None and z[0] <= float(self.c.get('room_waste_rate', 0.5))]
            if not dead:
                break
            rt, v = min(dead, key=lambda z: z[0])
            self.on_leader_exit(str(v['leader_tid']), why='made room: %.2f%%/h over %.0fm' % (rt, w / 60))
            if not self.st['ours'][str(v['leader_tid'])].get('closed'):
                return opn, dep, size, 'capital: a copy at %.2f%%/h could not be closed to make room' % rt
            opn, dep, base, cap = now()
            size, ok = plan(opn, dep, base, cap)
        while not ok:
            low = [o for o in opn if self.tier(o.get('leader')) > tier]
            if not low:
                free = cap - dep
                no_res = len(opn) + 1 <= n_slots[0] and min(max(base, free / div) if div else base, free) >= \
                    float(self.c.get('last_slot_frac', 1) or 1) * base - 0.01
                why = ('%s: room kept for a main leader (%d open, $%.0f deployed)' if r and no_res else
                       'capital: %d open, $%.0f deployed') % ((self.TIERS[tier], len(opn), dep) if r and no_res else (len(opn), dep))
                return opn, dep, size, why
            v = max(low, key=lambda o: (self.tier(o.get('leader')), o.get('entry_ts') or 0))
            self.on_leader_exit(str(v['leader_tid']), why='made room for a %s copy' % self.TIERS[tier])
            if not self.st['ours'][str(v['leader_tid'])].get('closed'):
                return opn, dep, size, 'capital: a %s copy could not be closed to make room' % self.TIERS[self.tier(v.get('leader'))]
            opn, dep, base, cap = now()
            size, ok = plan(opn, dep, base, cap)
        return opn, dep, size, None

    def on_leader_exit(self, tid, why=None, heard=None, path=None):
        t0 = time.time()
        n0 = getattr(self, '_txn', 0)
        tm = dict(path=path, heard_ms=round((t0 - heard) * 1e3)) if heard else {}
        for b in (self.st['leader'].pop(tid, None) or {}).get('bins') or []:
            (self.st.get('bins') or {}).pop(b, None)
        o = self.st['ours'].get(tid)
        if not o or o.get('closed'):
            return
        own = _batch([_call(POSM, '0x6352211e' + '%064x' % o['token_id'])])[0]
        if not own or len(own) < 66:
            # ownerOf reverts: our NFT is burned already - book what actually came back instead of retrying a NOT_MINTED revert
            back = self._burned_back(o)
            if back is None:
                return self.rec(ev='error', leader_tid=tid, at='exit', why='our copy is burned but its burn tx was not found')
            o.update(closed=True, exit_ts=time.time(), back_usd=back, fees_usd=0.0, pnl_gross_usd=back - o['size_usd'],
                     pnl_usd=back - o['size_usd'] - (o.get('gas_usd') or 0.0))
            self.st['realized'] += o['pnl_usd']
            self.db(o, 'closed')
            self.save()
            return self.rec(ev='close', leader_tid=tid, pool=o['pool'], sym=o.get('symbol'), back_usd=round(back, 4),
                            pnl_usd=round(o['pnl_usd'], 4), why='already burned before a restart, booked from the chain',
                            leader=o.get('leader'), size_usd=o.get('size_usd'))
        v_ = value_of(self, [o]).get(o['token_id']) or {}
        fees, mark = v_.get('fees', 0.0), v_.get('principal', 0.0) + v_.get('fees', 0.0)   # at spot, read before the exit
        u0 = X.balance(self.chain, self.u, self.ac.address)
        rc0 = len(X.RECEIPTS)                                          # this exit's own txs from here (usdg_flow)
        routes = self.exit_routes(o)
        # any copy can come back in ETH now (its token sold in an ETH pool): the ETH reading is taken for every exit
        e0 = self.tr.eth() if (o.get('quote') == 'eth' or routes) else None
        try:
            pos = dict(pool=o['pool'], key=o['key'], token=o['token'], token_id=o['token_id'], tick_lower=o['tick_lower'],
                       tick_upper=o['tick_upper'], symbol=o.get('symbol', ''), routes=routes, eth_usd=eth_px() if (routes or self.c.get('relay_exit')) else None,
                       relay=self.relay_cfg(), floor=float(self.c.get('exit_floor', X.EXIT_FLOOR)))
            try:
                X.exit(self.chain, self.ac, pos, True, self.log)
            except Exception as e:
                # A BURN REFUSED AT ITS OWN FLOOR (2026-09-30 VRAX: MinimumAmountInsufficient, the pool moved 2% while three
                # copies and the leader exited in 4 s) is retried at once on a fresh quote, not 3 minutes later by the check
                own = _batch([_call(POSM, '0x6352211e' + '%064x' % o['token_id'])])[0]
                if not own or len(own) < 66:                           # burned already: the check books it from the chain
                    raise
                self.rec(ev='error', leader_tid=tid, at='exit', why='%s - retrying on a fresh quote' % str(e)[:160])
                X.exit(self.chain, self.ac, pos, True, self.log)
        except Exception as e:
            return self.rec(ev='error', leader_tid=tid, at='exit', why=str(e)[:200])
        if e0 is not None:
            try:
                self.tr.unwind(e0, 'exit of %s #%s' % (o.get('symbol'), tid))          # the exit's ETH back to USDG
            except Exception as e:
                self.log('   unwind failed: %s (the beat sells ETH above the band)' % str(e)[:120])
        # WHAT CAME BACK, IN USDG, for both kinds: an ETH copy's return is what its ETH sold for (a failed unwind books
        # less now, and the beat's settle returns the rest to the book)
        # WHAT CAME BACK FROM THIS EXIT'S OWN TXS (the burn, the sale, the ETH unwind): their USDG into the wallet; balance reads
        # at different public heads booked a $127.52 return as $59.01 (2026-10-01)
        rcs = X.RECEIPTS[rc0:]
        back = X.usdg_flow(self.chain, self.ac.address, rcs) / 1e6 if rcs else (X.balance(self.chain, self.u, self.ac.address) - u0) / 1e6
        # FEES AT WHAT THE EXIT GOT, not at spot: selling the token side moves a thin pool, so the spot value overstates
        # (VRAX 2026-09-30: $37.31 of fees at spot, $30.59 back in all) - scaled by back / (principal + fees at spot)
        if mark > 0:
            fees *= min(1.0, back / mark)
        txs = self.txs_since(n0)                                       # the exit's own sends: burn, sale, unwind
        gas = (o.get('gas_usd') or 0.0) + sum(t.get('gas_usd') or 0 for t in txs)
        pnl = back - o['size_usd'] - gas                               # NET of the copy's gas, open and close
        o.update(closed=True, exit_ts=time.time(), back_usd=back, fees_usd=fees, pnl_usd=pnl, pnl_gross_usd=pnl + gas, gas_usd=round(gas, 4))
        if pos.get('held'):                                            # under the sale floor: kept, sold by settle_unsold when it pays
            o['unsold'] = dict(pos['held'], since=time.time(), tries=0)
        if pos.get('route'):
            o['exit_route'] = r_ = pos['route']
            if 'relay_usd' not in r_:                                  # Relay off: its quote is only logged beside ours
                self.relay_shadow(o, pos['route'])
        self.st['realized'] += pnl
        self.save()                                                    # the close is on disk the moment the chain has it
        self.db(o, 'closed')
        held = (o['exit_ts'] - o['entry_ts']) / 3600
        tm['exit_ms'] = round((time.time() - t0) * 1e3)
        r_ = o.get('exit_route') or {}
        self.rec(ev='close', leader_tid=tid, pool=o['pool'], sym=o.get('symbol'), quote=o.get('quote', 'usdg'), **tm, held_min=round(held * 60, 1), back_usd=round(back, 4),
                 fees_usd=round(fees, 4), pnl_usd=round(pnl, 4), realized=round(self.st['realized'], 2),
                 why=why or 'leader exited', leader=o.get('leader'), tier=self.TIERS[self.tier(o.get('leader'))], size_usd=o.get('size_usd'),
                 pnl_pct=round(100 * pnl / o['size_usd'], 2) if o.get('size_usd') else None,
                 exit=dict(venue=r_.get('venue'), pool_usd=r_.get('usd'), own_pool_usd=r_.get('own_usd'), pools_quoted=r_.get('n'),
                           relay_usd=r_.get('relay_usd'), relay_ms=r_.get('relay_ms'), relay_error=r_.get('relay_error'),
                           aggs=r_.get('aggs'), fair_usd=r_.get('fair_usd'), errors=r_.get('errors') or None, rounds=r_.get('tries')) if r_ else None,
                 gas_usd=round(gas, 4), exit_gas_usd=round(sum(t.get('gas_usd') or 0 for t in txs), 4), pnl_gross_usd=round(pnl + gas, 4), txs=txs)
        sale = sale_of(o.get('exit_route'))
        if sale and sale['bad']:                                       # in lp watch's feed and the checks, not only on the card
            self.rec(ev='error', leader_tid=tid, at='exit_sale', sym=o.get('symbol'), why='%s %s' % (o.get('symbol'), sale_line(sale)), sale=sale)
        w = (o.get('leader') or self.leader)
        self.card('▼ %s $%.2f → $%.2f (%+.1f%%, %s after $%.3f gas) · %s · %s…%s%s' % (
            pair(o), o['size_usd'], back, 100 * pnl / o['size_usd'], ('+$%.2f' if pnl >= 0 else '-$%.2f') % abs(pnl), gas,
            ('%.0fm' % (held * 60)) if held < 1 else ('%.1fh' % held), w[:6], w[-4:], ' · ' + why if why else '')
            + ('\n' + sale_line(sale) if sale else '')
            + ('\n⏸ %s held, fair ~$%.2f: no venue paid %.0f%% of it - sold when one does' % (
                o.get('symbol'), o['unsold'].get('fair_usd') or 0, 100 * float(self.c.get('exit_floor', X.EXIT_FLOOR))) if o.get('unsold') else ''))
        self.guard()

    def book_gas(self):
        """GAS IS A COST OF THE COPY (owner 2026-10-03: "mint costs are still costs"): pnl_usd is USDG back - USDG in - the gas of
        the copy's own txs (mint, approvals, entry swap, exit, sale), pnl_gross_usd the same before gas. Copies closed before this
        are re-booked ONCE from follow.jsonl's open/close records (their gas_usd), realized with them."""
        todo = [o for o in self.st['ours'].values() if o.get('closed') and 'pnl_gross_usd' not in o]
        if not todo:
            return
        gas = collections.defaultdict(float)
        try:
            for l in open(_log_path(self.chain)):
                if '"gas_usd"' not in l:
                    continue
                r = json.loads(l)
                if r.get('ev') in ('open', 'close') and r.get('gas_usd'):
                    gas[str(r.get('leader_tid'))] += r['gas_usd']
        except (OSError, ValueError):
            return
        tot = 0.0
        for o in todo:
            g = gas.get(str(o['leader_tid']), 0.0)
            o.update(gas_usd=round(g, 4), pnl_gross_usd=o['pnl_usd'], pnl_usd=o['pnl_usd'] - g)
            tot += g
        self.st['realized'] = self.st.get('realized', 0.0) - tot
        self.save()
        self.log('gas booked into %d closed copies: $%.2f off realized' % (len(todo), tot))

    def relay_cfg(self):
        # every aggregator (lp.AGGS: relay, kyber, lifi, paraswap) quotes each sale; relay_exit = false leaves our pools only
        return dict(margin=float(self.c.get('relay_margin_usd', 0.05)), slip_bps=round(1e4 * float(X.cfg(self.chain)['slippage'])), timeout=3.0) \
            if self.c.get('relay_exit') else None

    def settle_unsold(self):
        """A HELD token side (lp.sell_best refused to sell it under the floor of its fair value: FIRM 2026-10-03) is re-quoted
        every minute - Relay and every pool of the token - and sold the moment a venue pays the floor; after unsold_max_h it
        is sold at whatever the best venue pays. What it brings is added to ITS copy's return and P&L."""
        for o in [o for o in self.st['ours'].values() if o.get('unsold')]:
            u_ = o['unsold']
            amt = min(int(u_['amount']), X.balance(self.chain, o['token'], self.ac.address))
            if amt <= 0:
                o.pop('unsold')
                self.save()
                continue
            age_h = (time.time() - u_['since']) / 3600
            floor = float(self.c.get('exit_floor', X.EXIT_FLOOR)) if age_h < float(self.c.get('unsold_max_h', 24)) else 0.0
            e0, rc0 = self.tr.eth(), len(X.RECEIPTS)
            ok, route = X.sell_best(self.chain, self.ac, o['token'], amt, [o['key']] + self.exit_routes(o), eth_px(), self.relay_cfg(), True,
                                    self.log, dict(pool=o['pool'], sym=o.get('symbol', ''), leg='unsold', token_id=o.get('token_id')), floor=floor, tries=1)
            u_['tries'] += 1
            u_['last'] = route
            if not ok:
                self.save()
                continue
            try:
                self.tr.unwind(e0, 'held %s of #%s' % (o.get('symbol'), o['leader_tid']))
            except Exception as e:
                self.log('   unwind failed: %s (the beat sells ETH above the band)' % str(e)[:120])
            got = X.usdg_flow(self.chain, self.ac.address, X.RECEIPTS[rc0:]) / 1e6
            o.pop('unsold')
            o['back_usd'] += got
            o['pnl_usd'] += got
            o['pnl_gross_usd'] = o.get('pnl_gross_usd', o['pnl_usd'] - got) + got
            self.st['realized'] += got
            o['unsold_sold'] = dict(usd=round(got, 4), after_h=round(age_h, 2), route=route)
            self.save()
            self.db(o, 'closed')
            self.rec(ev='unsold_sold', leader_tid=o['leader_tid'], sym=o.get('symbol'), usd=round(got, 4), fair_usd=route.get('fair_usd'),
                     venue=route.get('venue'), after_h=round(age_h, 2), pnl_usd=round(o['pnl_usd'], 4), realized=round(self.st['realized'], 2))
            self.card('▲ %s held token sold: +$%.2f after %.0f min · the copy now $%.2f → $%.2f (%+.1f%%)\n%s' % (
                pair(o), got, age_h * 60, o['size_usd'], o['back_usd'], 100 * o['pnl_usd'] / o['size_usd'], sale_line(sale_of(route))))

    def db(self, o, status):
        if getattr(self, 'remote', False):
            return                     # the LP store (var/lpwallets.db) lives on home; a desk on another box keeps follow.json only
        try:
            from copydesk import lpdb
            lpdb.save_copy(o.get('leader') or self.leader, o, status)                     # the dashboard's copy-vs-leader table
        except Exception as e:
            self.log('store failed: %s' % str(e)[:160])

    # ---- the card (core/fmt.lp_card, the LP desk's own format)
    def card(self, title, enter=(), exit=(), with_check=False):
        """Telegram: queued for the card worker with a snapshot of the book, so drawing and uploading it never holds up the
        next mint or exit (2026-09-30: it ran in line after every open and close). One worker: cards go out in order."""
        import copy, queue, threading
        if not hasattr(self, 'cards'):
            self.cards = queue.Queue()

            def work():
                while True:
                    t_, st_, wc_, bal_ = self.cards.get()
                    try:
                        self._card(t_, st_, wc_, bal_)
                    finally:
                        self.cards.task_done()
            threading.Thread(target=work, daemon=True).start()
        # THE CASH WITH THE SNAPSHOT (owner 2026-10-01: a 'made room' card showed $318 of ~$395): read later by the worker, the
        # USDG balance already missed the next copy's mint while the snapshot did not hold that copy yet. One read, here
        try:
            bal = X.balance(self.chain, self.u, self.ac.address) / 1e6
        except Exception:
            bal = None
        self.cards.put((title, copy.deepcopy(self.st), with_check, bal))

    def _card(self, title, st, with_check=False, bal=None):
        """ONE image, always the desk's overview (copydesk/lpcard: book, open copies, P&L by leader), with what just happened
        as its caption (owner 2026-09-29), drawn from the snapshot `st`. If the image cannot be drawn or sent, the same
        caption goes out as plain text so an event is never lost."""
        from core import fmt, io as cio
        try:
            opn = [o for o in st['ours'].values() if not o.get('closed')]
            vals = value_of(self, opn)
            idle = bal if bal is not None else X.balance(self.chain, self.u, self.ac.address) / 1e6
            gas = int(self.chain.node.rpc('eth_getBalance', [self.ac.address, 'latest']), 16) / 1e18 * self.chain.eth_usd
            rows, worth_open, fees_open = [], 0.0, 0.0
            for o in opn:
                v = vals.get(o.get('token_id')) or {}
                h = (time.time() - o['entry_ts']) / 3600
                w = (v.get('principal') or 0) + (v.get('fees') or 0)
                worth_open, fees_open = worth_open + w, fees_open + (v.get('fees') or 0)
                rows.append(dict(sym=o.get('symbol') or o['pool'][2:8], quote='ETH' if o.get('quote') == 'eth' else 'USDG',
                                 side='holds ' + holds_label(v, o) if v else '', inn=o['size_usd'], now=w, fees=v.get('fees') or 0,
                                 age=('%dm' % (h * 60)) if h < 1 else ('%dh' % h) if h < 48 else ('%dd' % (h / 24)),
                                 in_range=bool(v.get('in_range')), band=v.get('band')))
            total = idle + worth_open
            self.st['hwm'] = max(self.st['hwm'], total)
            pnl = total - st['capital']
            closed_fees = sum(o.get('fees_usd') or 0 for o in st['ours'].values() if o.get('closed'))
            cap = fmt.esc(title)
            if with_check:
                cap += ''.join('\n⚠️ ' + fmt.esc(i[:80]) for i in check(self.chain)[1])
            try:
                from copydesk import lpcard
                png = lpcard.render(dict(total=total, pnl=pnl, pct=100 * pnl / st['capital'],
                                         equity=pnl_points(self.equity(total), st), open=rows, leaders=self.by_leader(st),
                                         spare=idle, gas=gas, fees=fees_open + closed_fees, net=st['realized']))
                if cio.Telegram(self.chain).photo(png, cap):
                    return
            except Exception as e:
                self.log('card image failed: %s' % str(e)[:160])
            self.note.card(cap) if hasattr(self.note, 'card') else self.note(title)
        except Exception as e:
            self.log('card failed: %s' % str(e)[:200])

    def equity(self, total=None, days=7.0):
        """The desk's balance over time for the card's graph: var/lp/equity.jsonl, one {ts, v} per card and per 10-minute
        beat. Missing, it is rebuilt from the book: starting capital plus realized P&L at each close (open copies unseen)."""
        p = os.path.join(_dir(self.chain), 'equity.jsonl')
        if not os.path.exists(p):
            v, rows = self.st['capital'], [dict(ts=self.st.get('started') or time.time(), v=self.st['capital'])]
            for o in sorted((o for o in self.st['ours'].values() if o.get('closed') and o.get('exit_ts')), key=lambda o: o['exit_ts']):
                v += o.get('pnl_usd') or 0.0
                rows.append(dict(ts=o['exit_ts'], v=round(v, 2)))
            open(p, 'w').write(''.join(json.dumps(r) + '\n' for r in rows))
        # a point the book cannot be (not finite, or over 100x what went in) is a broken read, never charted
        sane = lambda v: v == v and abs(v) < 100 * max(1.0, self.st.get("capital") or 0)
        if total is not None and sane(total):
            with open(p, 'a') as f:
                f.write(json.dumps(dict(ts=time.time(), v=round(total, 2))) + '\n')
        pts = []
        for l in open(p):
            try:
                r = json.loads(l)
                if r['ts'] >= time.time() - days * 86400 and sane(r['v']):
                    pts.append((r['ts'], r['v']))
            except Exception:
                pass
        return pts[::max(1, len(pts) // 300)] + (pts[-1:] if len(pts) > 300 else [])

    def by_leader(self, st=None):
        """The card's leader table (owner 2026-09-29): ONLY the wallets followed now - main leaders, then the bench - each with
        its copies open / closed and realized P&L (back + fees - deposit, before gas) today and since the start."""
        today = time.strftime('%Y-%m-%d', time.gmtime())
        g = {a: dict(open=0, n=0, today=0.0, total=0.0) for a in self.watch}
        for o in (st or self.st)['ours'].values():
            r = g.get((o.get('leader') or '').lower())
            if r is None:
                continue                                              # a leader we no longer follow: in the book, not the table
            if not o.get('closed'):
                r['open'] += 1
                continue
            r['n'] += 1
            r['total'] += o.get('pnl_usd') or 0.0
            if time.strftime('%Y-%m-%d', time.gmtime(o.get('exit_ts') or 0)) == today:
                r['today'] += o.get('pnl_usd') or 0.0
        # the card stays short however wide the bench is (owner 2026-09-30: 43 rows was far too tall): each main leader and
        # ONE row for the whole bench (its open copies already show in the card's OPEN table)
        rows = [dict(g[a], tag=a[2:6], role='lead') for a in self.leaders]
        b = [g[a] for a in self.bench]
        rows.append(dict(tag='%d' % len(b), role='bench-all', open=sum(r['open'] for r in b), n=sum(r['n'] for r in b),
                         today=sum(r['today'] for r in b), total=sum(r['total'] for r in b)))
        if self.filler:
            f = [g[a] for a in self.filler]
            rows.append(dict(tag='%d' % len(f), role='fill-all', open=sum(r['open'] for r in f), n=sum(r['n'] for r in f),
                             today=sum(r['today'] for r in f), total=sum(r['total'] for r in f)))
        return rows

    def sizing(self, deployed):
        """(copy size, deploy cap) in USD. [lp.follow] size_frac (owner 2026-09-29: 0.2) sizes each copy as that share of the
        desk's book - free USDG plus what the open copies cost - and lets the whole book be deployed, so size follows
        the book up and down. Without it, the fixed size_usd / max_deploy_usd."""
        f = self.c.get('size_frac')
        if not f:
            return self.c['size_usd'], self.c['max_deploy_usd']
        book = X.balance(self.chain, self.u, self.ac.address) / 1e6 + deployed
        n = self.slots(book)
        return round(book * (1.0 / n if n != int(self.c['max_open']) else float(f)), 2), book

    def slots(self, book):
        """DYNAMIC SLOTS (owner 2026-10-01, research/sizing.py slots + targets: 14,354 squad copies over 9 days with price impact,
        smart exit and the thin-pool skip): from a book of slot_min_book, as many slots as keep each copy near slot_target_usd
        - slots = book / target, never under max_open, each copy book / slots. Target $65 passed the backtest bar against 5
        slots at every book from $1,000 to $5,000 (9 of 9; $50 and $80 agree from $1,250); under $1,000 nothing passed, so
        the desk stays at max_open there. Without the knobs: max_open, as before."""
        t, mb = self.c.get('slot_target_usd'), float(self.c.get('slot_min_book', 0) or 0)
        n0 = int(self.c['max_open'])
        if not t or book < mb:
            return n0
        return max(n0, int(round(book / float(t))))

    # ---- guards
    def guard(self):
        eq = self.st['realized']
        cap = self.sizing(sum(o['size_usd'] for o in self.st['ours'].values() if not o.get('closed')))[1]
        peak = self.st.get('realized_hwm', 0.0)
        self.st['realized_hwm'] = peak = max(peak, eq)
        if peak - eq > self.c['stop_drawdown'] * cap and not os.path.exists(stop_path(self.chain)):
            open(stop_path(self.chain), 'w').write('drawdown %.2f from high-water %.2f at %s\n' % (eq - peak, peak, time.ctime()))
            self.st['stop_tripped'] = True
            # stop_pause_h (owner 2026-10-01): PAUSE - no new copy for that long, the open copies are kept and exit with their
            # leaders, then the desk resumes itself (the resume below resets the high). lp tune downside, 9 days: pause 3 h
            # +$83.48/day vs exit all 2 h +$83.25, 6 h +$79.47 (one stop in 9 days: a structural choice, not a measured edge)
            ph = self.c.get('stop_pause_h')
            if ph:
                self.st['pause_until'] = time.time() + float(ph) * 3600
                self.rec(ev='stop', why='drawdown $%.2f from the realized high $%.2f: no new copies for %g h, copies kept' % (eq - peak, peak, ph))
                return
            self.rec(ev='stop', why='drawdown $%.2f from the realized high $%.2f: exiting every copy' % (eq - peak, peak))
            for t in [t for t, o in self.st['ours'].items() if not o.get('closed')]:
                self.on_leader_exit(t)
        pu = self.st.get('pause_until')
        if pu and time.time() >= pu:
            self.st.pop('pause_until', None)
            if self.st.get('stop_tripped') and os.path.exists(stop_path(self.chain)):
                os.remove(stop_path(self.chain))                      # the loop's resume resets the high on its next pass

    def run(self, seconds=0.0):
        self.rec(ev='start', leader=','.join(self.leaders), bench=','.join(self.bench), filler=','.join(self.filler), size_usd=self.c['size_usd'], max_open=self.c['max_open'],
                 max_deploy_usd=self.c['max_deploy_usd'], capital=self.st['capital'])
        t_end = time.time() + seconds if seconds else 0
        beat = 0.0
        # A GRACEFUL STOP: `kill` (SIGTERM) only raises a flag; the action in progress (an entry or an exit, several txs)
        # finishes and is saved before the loop ends, so a restart never lands mid-transaction
        import signal
        self.stop = False
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, 'stop', True))
        try:
            self.reconcile()
        except Exception as e:
            self.log('reconcile failed: %s' % str(e)[:160])
        day = time.strftime('%Y-%m-%d', time.gmtime())
        while (not t_end or time.time() < t_end) and not self.stop:
            t0 = time.time()
            del X.RECEIPTS[:]                                           # between steps: nothing is mid-entry or mid-exit here
            self.drain()                                                # the websocket first: everything already heard
            # A HUMAN RESUME (`lp resume` deletes the STOP file) restarts the drawdown stop's reference at today's realized:
            # left at the old high, the next small loss would liquidate and stop again (2026-10-01: high $165.59, realized
            # $71.45, the stop at ~$100 - a $6 loss away)
            stopped = os.path.exists(stop_path(self.chain))
            # the stop the desk tripped itself is in its book (stop_tripped): STOP gone afterwards = a human resumed it, also
            # across a restart (supervise starts the desk only once STOP is gone). follow.RESUME asks for the same reset by hand
            rq = os.path.join(_dir(self.chain), 'follow.RESUME')
            if (self.st.get('stop_tripped') or os.path.exists(rq)) and not stopped:
                self._stopped = True
                self.st.pop('stop_tripped', None)
                if os.path.exists(rq):
                    os.remove(rq)
            if getattr(self, '_stopped', None) and not stopped:
                self.rec(ev='resume', why='STOP removed: the drawdown reference reset from $%.2f to $%.2f' % (
                    self.st.get('realized_hwm', 0.0), self.st['realized']))
                self.st['realized_hwm'] = self.st['realized']
                self.save()
            self._stopped = stopped
            if self.squad_changed():
                return self.reexec()
            self.owner_closes()
            ph, tp = {}, time.time()                                   # PHASE TIMES: a pass over 3 s logs where it went
            if time.time() - getattr(self, '_fee_t', 0) > 60:
                self._fee_t = time.time()
                try:
                    self.sample_fees()
                except Exception as e:
                    self.log('fee sample failed: %s' % str(e)[:160])
            ph['fees'], tp = time.time() - tp, time.time()
            try:
                self.poll()                                             # the backstop: what the socket missed
            except Exception as e:
                self.log('poll failed: %s' % str(e)[:200])
            ph['poll'], tp = time.time() - tp, time.time()
            if time.strftime('%Y-%m-%d', time.gmtime()) != day:       # the daily report card, once past midnight UTC
                day = time.strftime('%Y-%m-%d', time.gmtime())
                self.card('Daily report')
            if time.time() - getattr(self, '_unsold_t', 0) > 60 and any(o.get('unsold') for o in self.st['ours'].values()):
                self._unsold_t = time.time()
                try:
                    self.settle_unsold()
                except Exception as e:
                    self.log('unsold settle failed: %s' % str(e)[:160])
            if time.time() - beat > 600:
                beat = time.time()
                try:
                    _, issues = check(self.chain)                         # every open copy vs the leader and the chain
                    for i in issues:
                        self.rec(ev='error', at='check', why=i)
                        t = [t for t, o in self.st['ours'].items() if not o.get('closed') and '#%s ' % o['leader_tid'] in i + ' ']
                        if 'the leader has exited' in i and t:
                            self.on_leader_exit(t[0])                    # the poll missed it: close now
                except Exception as e:
                    self.log('check failed: %s' % str(e)[:160])
                try:
                    # THE DESK FUNDS ITSELF (copydesk/treasury): ETH above the gas band back to USDG, gas under the band
                    # bought (refuel.keep: slippage floor, stops after two bad fills), closed copies' token dust sold
                    self.tr.settle([o for o in self.st['ours'].values() if o.get('closed') and time.time() - (o.get('exit_ts') or 0) < 86400],
                                   hold={o['token'].lower() for o in self.st['ours'].values() if o.get('unsold')}, relay=self.relay_cfg())
                except Exception as e:
                    self.log('treasury settle failed: %s' % str(e)[:120])
                try:
                    opn = [o for o in self.st['ours'].values() if not o.get('closed')]
                    self.equity(X.balance(self.chain, self.u, self.ac.address) / 1e6
                                + sum(v['principal'] + v['fees'] for v in value_of(self, opn).values()))
                except Exception as e:
                    self.log('equity failed: %s' % str(e)[:120])
                s = status(self.chain)
                self.log('%s beat: next_id %d, leader positions tracked %d, ours open %d, closed %d, realized $%.2f, leader quiet %.1f h' % (
                    time.strftime('%H:%M'), self.st['next_id'], len(self.st['leader']), len(s['open']), len(s['closed']),
                    self.st['realized'], (time.time() - self.st['last_leader_ts']) / 3600))
            ph['beat'] = time.time() - tp
            if time.time() - t0 > 3:
                self.log('%s slow pass %.1f s: %s' % (time.strftime('%H:%M:%S'), time.time() - t0, ', '.join('%s %.1f' % kv for kv in ph.items() if kv[1] > 0.2)))
            self.drain(t0 + self.c['poll_s'])
        self.save()
        t_ = time.time()
        while getattr(self, 'cards', None) and self.cards.unfinished_tasks and time.time() - t_ < 20:
            time.sleep(0.2)                                            # the cards already queued still go out
        if self.stop:
            self.rec(ev='stop', why='SIGTERM: the action in progress finished and the book is saved')
        return 0


def value_of(desk, opn):
    """token_id -> principal $, unclaimed fees $, in range, band %, (min, now, max) price in USDG per token, for our open
    copies: liquidity + pool price + fee growth inside vs the position's last snapshot, one batched read per pool."""
    out = {}
    for o in opn:
        t = o.get('token_id')
        if not t:
            continue
        tl, tu, pid = o['tick_lower'], o['tick_upper'], o['pool']
        liq, s0, fg, pinf = _batch([_call(POSM, '0x1efeed33' + '%064x' % t), _call(SV, '0xc815641c' + pid[2:]),
                                    _call(SV, '0x53e9c1fb' + pid[2:] + _enc(tl) + _enc(tu)),
                                    _call(SV, '0xdacf1d2f' + pid[2:] + POSM[2:].rjust(64, '0') + _enc(tl) + _enc(tu) + '%064x' % t)])
        L = int(liq, 16) if liq else 0
        sp = _w(s0, 0) / 2 ** 96
        # fee growth now - the position's snapshot, mod 2^256 as the contract counts; a difference past 2^255 is a NEGATIVE one
        # = the snapshot read from a later block than the growth (public endpoints answer a batch from different heads):
        # a stale read, 0 fees - not 2^256 (2026-10-01 17:19: the book read $2.3e53 and the P&L chart went with it)
        grow = lambda a, b: (lambda d: 0 if d >= 1 << 255 else d)((a - b) % (1 << 256))
        f0 = grow(_w(fg, 0), _w(pinf, 1)) * L / 2 ** 128
        f1 = grow(_w(fg, 1), _w(pinf, 2)) * L / 2 ** 128
        uc0, d0, d1 = o['usdg_is_c0'], o['d0'], o['d1']
        x0, x1 = amounts(L, sp, tl, tu)
        tick_px = lambda tk: 1.0001 ** tk * 10 ** d0 / 10 ** d1        # token1 per token0, whole units
        token_px = (lambda tk: 1 / tick_px(tk)) if uc0 else tick_px     # USDG per token
        now = token_px(__import__('math').log(sp * sp) / __import__('math').log(1.0001)) if sp else 0
        lo, hi = sorted((token_px(tl), token_px(tu)))
        sa, sb = 1.0001 ** (tl / 2), 1.0001 ** (tu / 2)
        k = eth_px() if o.get('quote') == 'eth' else 1.0          # an ETH-quoted copy: its quote side in dollars
        qv = (x0 / 10 ** d0 if uc0 else x1 / 10 ** d1) * k          # the principal's quote side, in dollars
        pv = usd_value(x0, x1, sp, uc0, d0, d1) * k
        out[t] = dict(principal=pv, fees=usd_value(f0, f1, sp, uc0, d0, d1) * k, in_range=sa <= sp < sb,
                      band=100 * (now - lo) / (hi - lo) if hi > lo else None, px=(lo * k, now * k, hi * k), holds=holds(qv, pv, o))
    return out


def holds(quote_usd, total_usd, o):
    """What a copy holds right now (owner 2026-09-30: the pair names the pool, the side is what we own): 'quote' - only
    USDG/ETH, a bid waiting for the price to fall into it (or an ask the price rose through); 'token' - only the token, an
    ask above the price (or a bid the price fell through); 'both' - in range, earning. Read off the amounts, not the
    ticks, so it follows the price as it crosses the range."""
    if total_usd <= 0:
        return 'empty'
    s = quote_usd / total_usd
    return 'quote' if s > 0.99 else 'token' if s < 0.01 else 'both'


def holds_label(v, o):
    """What a copy holds now, as tokens only (owner 2026-09-30): 'USDG', 'VRAX', or 'VRAX/USDG' when it holds both."""
    h, tok, q = (v or {}).get('holds'), o.get('symbol') or '?', 'ETH' if o.get('quote') == 'eth' else 'USDG'
    return {'quote': q, 'token': tok, 'both': '%s/%s' % (tok, q)}.get(h, '')


def status(chain):
    p = _state_path(chain)
    st = json.load(open(p)) if os.path.exists(p) else dict(ours={}, realized=0.0, hwm=0.0)
    ours = list(st['ours'].values())
    closed = [o for o in ours if o.get('closed')]
    rows = [json.loads(l) for l in open(_log_path(chain))] if os.path.exists(_log_path(chain)) else []
    sales = [(o, sale_of(o.get('exit_route'))) for o in closed if o.get('exit_route')]
    sales = [(o, x) for o, x in sales if x and x['venue']]                 # routes from before 2026-10-01 carry no venue
    return dict(open=[o for o in ours if not o.get('closed')], closed=closed, realized=st.get('realized', 0.0), sales=sales,
                win=100 * sum(1 for o in closed if o['pnl_usd'] > 0) / len(closed) if closed else None,
                skips=collections.Counter(r.get('why') for r in rows if r.get('ev') == 'skip'),
                stopped=os.path.exists(stop_path(chain)))


def _selftest():
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
    # USDG = token0 (6 decimals), token1 18 decimals; price 100 token per USDG -> sqrtP = sqrt(100 * 1e18 / 1e6)
    sp = (100 * 1e18 / 1e6) ** 0.5
    import math
    t = math.log(sp * sp) / math.log(1.0001)
    tl, tu = int(t) + 100, int(t) + 2000                           # above the price: all token0 = all USDG
    u0, _ = amounts(1e18, sp, tl, tu)
    L = 100.0 * 1e6 / u0 * 1e18                                    # the liquidity $100 of USDG buys on the range
    a0, a1 = amounts(L, sp, tl, tu)
    A(abs(a0 / 1e6 - 100.0) < 1e-6 and a1 == 0, 'a USDG-only range holds exactly the size in USDG')
    A(abs(usd_value(a0, a1, sp, True, 6, 18) - 100.0) < 1e-6, 'valued at its size')
    # the price walks through the whole range: all of it becomes token1, sold at the range's top price minus a 1% fee
    sp2 = 1.0001 ** (tu / 2) * 1.01
    b0, b1 = amounts(L, sp2, tl, tu)
    A(b0 == 0 and b1 > 0, 'past the range it is all token')
    v = usd_value(b0, b1, sp2, True, 6, 18, sell_cut=0.01)
    # the 22%-wide range converts at its geometric mean (~10% under entry), then pays the 1% fee to sell
    import math as m
    avg = 100.0 / m.sqrt(1.0001 ** (tu - tl)) * 0.99
    A(abs(v - avg) / avg < 0.02, 'filled bid sold back at the range average minus the fee (%.2f vs %.2f)' % (v, avg))
    # THE BENCH PRIORITY RULE (Follow.room): 5 slots of $20 on a $100 book
    f = Follow.__new__(Follow)
    f.c, f.leaders, f.bench, f.filler = dict(max_open=5, reserve=1), ['0xm'], ['0xb'], ['0xf']
    f.sizing = lambda dep: (20.0, 100.0)
    closed = []

    def ex(tid, why=None):
        f.st['ours'][tid]['closed'] = True
        closed.append(tid)
    f.on_leader_exit = ex
    cp = lambda i, who: (str(i), dict(leader_tid=i, leader=who, size_usd=20.0))
    f.st = dict(ours=dict([cp(1, '0xm'), cp(2, '0xm'), cp(3, '0xb')]))
    A(f.room(1)[3] is None, 'bench may open while a main copy still fits beside it (3 open of 5)')
    f.st['ours'].update([cp(4, '0xb')])
    A(f.room(1)[3] and not closed, 'bench refused at 4 of 5: the last slot is a main copy\'s')
    A(f.room(0)[3] is None and not closed, 'a main copy takes the free slot without closing anything')
    f.st['ours'].update([cp(5, '0xm')])
    A(f.room(0)[3] is None and closed == ['3'], 'a full book: the main copy closes one bench copy')
    f.st['ours'] = dict([cp(i, '0xm') for i in range(1, 6)])
    A(f.room(0)[3] and closed == ['3'], 'all main: skipped, nothing closed')
    # THE THIRD TIER: a filler copy gives way to a bench copy, the newest filler first; nothing gives way to a filler
    closed.clear()
    f.c = dict(max_open=5, reserve=0)
    f.st['ours'] = dict([cp(1, '0xm'), cp(2, '0xb'), (lambda k, v: (k, dict(v, entry_ts=1)))(*cp(3, '0xf')),
                         (lambda k, v: (k, dict(v, entry_ts=2)))(*cp(4, '0xf')), cp(5, '0xb')])
    A(f.room(2)[3] and not closed, 'a filler copy never closes another filler copy')
    A(f.room(1)[3] is None and closed == ['4'], 'a bench copy closes the NEWEST filler copy (%s)' % closed)
    # SIZE free/div: an empty $100 book at div 3 copies $33.33, never less than the base $20 and never past the reserve
    f.c = dict(max_open=5, reserve=1, size_free_div=3)
    f.st['ours'] = {}
    A(abs(f.room(0)[2] - 100 / 3) < 0.02, 'idle book: a third of the free capital (%s)' % f.room(0)[2])
    f.st['ours'] = dict([cp(1, '0xm'), cp(2, '0xm'), cp(3, '0xm')])
    A(abs(f.room(0)[2] - 40 / 3 if 40 / 3 >= 20 else f.room(0)[2] - 20) < 0.02 and f.room(1)[3] is None,
      '$60 deployed: $40 free, the base $20 (%s)' % (f.room(0)[2],))
    f.st['ours'] = dict([cp(1, '0xm'), cp(2, '0xm'), cp(3, '0xm'), cp(4, '0xm')])
    A(f.room(1)[3] and f.room(0)[3] is None, 'reserve 1: the last slot is a main copy\'s')
    # A RESTART NEVER COPIES A MINT TWICE: a websocket copy leaves next_id behind, so the poll meets the tid again
    seen = []
    f.seen, f.on_leader_mint = set(), lambda tid, who=None, heard=None, path=None, bins=None: seen.append(tid)
    f.st = dict(ours={'7': dict(leader_tid=7)}, leader={'8': dict(peak=1)})
    for t in (7, 8, 9, 9):
        f.mint(t, '0xm')
    A(seen == [9], 'a mint already booked or tracked is not handled again, a new one once (%s)' % seen)
    # THE TOKEN RULES: cap on open copies, cool-off from our own closed results, manual deny
    f.c = dict(max_per_token=2, cool_h=24.0, cool_n=3, deny_tokens=['0xDEAD'])
    now = time.time()
    f.st = dict(ours={'1': dict(token='0xa'), '2': dict(token='0xa'),
                      '3': dict(token='0xb', closed=True, exit_ts=now, pnl_usd=-1.0), '4': dict(token='0xb', closed=True, exit_ts=now, pnl_usd=-1.0),
                      '5': dict(token='0xb', closed=True, exit_ts=now, pnl_usd=0.5),
                      '6': dict(token='0xc', closed=True, exit_ts=now, pnl_usd=-1.0), '7': dict(token='0xc', closed=True, exit_ts=now, pnl_usd=3.0),
                      '8': dict(token='0xc', closed=True, exit_ts=now - 90000, pnl_usd=-9.0)})
    A((f.token_block('0xA') or '').startswith('token cap'), 'two open copies of a token: a third is refused')
    A((f.token_block('0xb') or '').startswith('token cooling'), 'three recent copies net -1.50: cooling off')
    A(f.token_block('0xc') is None, 'net +1.00 recent (the -9 is older than 24 h): allowed')
    A(f.token_block('0xdead') == 'denied token' and f.token_block('0xd') is None, 'deny list by address; a fresh token is free')
    # the cap is per token, the displacement is by tier (on_leader_mint): the lower tier's newest copy of it gives way
    f.leaders, f.bench, f.filler = ['0xm'], ['0xb'], []
    f.st = dict(ours={'1': dict(token='0xv', leader='0xb', entry_ts=1), '2': dict(token='0xv', leader='0xb', entry_ts=2)})
    low = [o for o in f.st['ours'].values() if not o.get('closed') and o['token'] == '0xv' and f.tier(o['leader']) > f.tier('0xm')]
    A(max(low, key=lambda o: (f.tier(o['leader']), o['entry_ts']))['entry_ts'] == 2, 'the newest bench copy of the token gives way to a main')
    # THE THIN-POOL SHARE: a $60 copy of a $600 leader position whose pool holds 3x the leader's liquidity: ~3%; a $60 copy
    # of a $30 leader alone in its pool: 2/3 -> over 50%, skipped
    A(abs(thin_share(1000.0, 600.0, 60.0, 3000.0) - 100 / 3100) < 1e-9, 'deep pool: a small share')
    A(abs(thin_share(1000.0, 30.0, 60.0, 1000.0) - 2000 / 3000) < 1e-9, 'our copy bigger than the leader alone in its pool')
    # THE WALLET LIMIT: 2 open copies of one bench wallet refuse a third; a main is never limited
    f.c = dict(bench_per_wallet=2)
    f.st = dict(ours={'1': dict(leader='0xb'), '2': dict(leader='0xb'), '3': dict(leader='0xm'), '4': dict(leader='0xm')})
    A((f.wallet_block('0xb') or '').startswith('wallet limit') and f.wallet_block('0xm') is None, 'bench wallet at its limit, main free')
    f.st['ours']['2']['closed'] = True
    A(f.wallet_block('0xb') is None, 'a closed copy frees the wallet')
    # THE LAST SLOT: $100 book, $70 deployed, base $40: $30 free opens at last_slot_frac 0.5, never at 1
    f.sizing = lambda dep: (40.0, 100.0)
    f.c = dict(max_open=5, reserve=0)
    f.st = dict(ours={'1': dict(leader_tid=1, leader='0xm', size_usd=70.0)})
    A(f.room(0)[3], '$30 free under a $40 base: no copy')
    f.c['last_slot_frac'] = 0.5
    A(f.room(0)[3] is None and abs(f.room(0)[2] - 30 / 1.005) < 0.01, 'last slot at half the base: $30 goes to work, less the headroom')
    # THE FEE RATE: $0.25 of fees over 30 min on $50 = 1%/h; too young = None
    now_ = time.time()
    o = dict(size_usd=50.0, fee_hist=[[now_ - 1900, 1.0], [now_ - 1000, 1.1], [now_, 1.25]])
    A(abs(fee_rate(o, now_, 1800) - 1.0) < 1e-9, 'trailing 30 min: $0.25 on $50 = 1%%/h (%s)' % fee_rate(o, now_, 1800))
    A(fee_rate(dict(o, fee_hist=o['fee_hist'][1:]), now_, 1800) is None, 'younger than the window: no rate')
    # MAKE ROOM: 2 slots full; a main copy closes the open copy earning the least (<= 0.5%/h), whatever its tier; with both
    # earning, the tier rule decides (the bench copy gives way), and a main copy never closes a main copy that earns
    f.sizing = lambda dep: (40.0, 80.0)
    f.c = dict(max_open=2, reserve=0, room_waste_min=30, room_waste_rate=0.5)
    dead_ = [[now_ - 1900, 0.0], [now_, 0.02]]                         # $0.02 in 30 min on $40: 0.1%/h
    busy_ = [[now_ - 1900, 0.0], [now_, 2.0]]                          # 10%/h
    mk = lambda i, who, h: (str(i), dict(leader_tid=i, leader=who, size_usd=40.0, entry_ts=now_ - 2000, fee_hist=h))
    closed.clear()
    f.st = dict(ours=dict([mk(1, '0xm', dead_), mk(2, '0xb', busy_)]))
    A(f.room(0)[3] is None and closed == ['1'], 'the dead MAIN copy makes room, not the earning bench copy (%s)' % closed)
    closed.clear()
    f.st = dict(ours=dict([mk(1, '0xm', busy_), mk(2, '0xb', busy_)]))
    A(f.room(0)[3] is None and closed == ['2'], 'both earning: the tier rule, the bench copy gives way (%s)' % closed)
    closed.clear()
    f.st = dict(ours=dict([mk(1, '0xm', busy_), mk(2, '0xm', busy_)]))
    A(f.room(0)[3] and not closed, 'two earning mains: nothing closes')
    # THE COLD-POOL RULE: under the cut skips; over it, missing pool, stale table or no knob never skips
    import tempfile, types
    d_ = tempfile.mkdtemp()
    f.chain = types.SimpleNamespace(var=d_)
    json.dump(dict(ts=time.time(), pools={'0xcold': -0.9, '0xwarm': 0.5}), open(os.path.join(d_, 'lp_heat.json'), 'w'))
    f.c = dict(cold_volz=-0.223)
    A((f.cold_pool('0xCOLD') or '').startswith('cold pool') and f.cold_pool('0xwarm') is None and f.cold_pool('0xnew') is None, 'cold skips, warm and unknown pass')
    f.c = {}
    A(f.cold_pool('0xcold') is None, 'no knob: never skips')
    f.c = dict(cold_volz=-0.223)
    json.dump(dict(ts=time.time() - 3600, pools={'0xcold': -0.9}), open(os.path.join(d_, 'lp_heat.json'), 'w'))
    os.utime(os.path.join(d_, 'lp_heat.json'), (time.time() + 5, time.time() + 5))
    A(f.cold_pool('0xcold') is None, 'a table an hour old never skips')
    # THE LAUNCH SIZE GUARD's age cache: a young pool is re-asked after its hour, an old one never again
    f._young = {('0xold', 1): False, ('0xnew', 1): time.time() + 600, ('0xexpired', 1): time.time() - 1}
    A(f.young_pool('0xold', 1) is False and f.young_pool('0xnew', 1) is True, 'cached old stays old, cached young stays young')
    # NO LAUNCHES: an answer for 1 h says nothing about 2 h, and an unreadable age is young for the ban, not for the size guard
    nb = globals()['_node_batch']
    globals()['_node_batch'] = lambda *a, **k: (_ for _ in ()).throw(IOError('down'))
    try:
        A(f.young_pool('0xold', 2, unread=True) is True, 'old for 1 h is not old for 2 h; unreadable = young for the ban')
        A(f.young_pool('0xold2', 1) is False, 'unreadable = not young for the size guard')
    finally:
        globals()['_node_batch'] = nb
    f.c = dict(cold_volz=-0.223, flat_rv24=0.031)
    json.dump(dict(ts=time.time(), pools={'0xwarm': 0.5, '0xflat': 0.5}, rv24={'0xwarm': 0.09, '0xflat': 0.01}), open(os.path.join(d_, 'lp_heat.json'), 'w'))
    os.utime(os.path.join(d_, 'lp_heat.json'), (time.time() + 9, time.time() + 9))
    A((f.cold_pool('0xflat') or '').startswith('price-flat') and f.cold_pool('0xwarm') is None, 'flat skips, a moving warm pool passes')
    f.c = dict(flat_rv24=0.031)
    A((f.cold_pool('0xflat') or '').startswith('price-flat'), 'the flat knob works without the cold one')
    # THE TOKEN CAP GIVES WAY LAST (2026-10-01 13:45): a bench mint into a capped token that the wallet limit refuses closes
    # nothing; a filler mint never closes a filler copy; an allowed bench mint closes the newest filler copy of the token
    import types
    K = dict(currency0='0x' + '11' * 20, currency1='0x' + '22' * 20, fee=10000, tickSpacing=200, hooks='0x' + '0' * 40)
    g = globals()
    saved = {k: g[k] for k in ('position', 'pool_state', 'uncopyable', 'stop_path')}
    g.update(position=lambda tid: (K, -200, 200, 10 ** 18, '0xpool'), pool_state=lambda pid, tl, tu: (1.0, 0, 0, 0),
             uncopyable=lambda *a, **k: None, stop_path=lambda c: '/nonexistent/follow.STOP')
    try:
        f = Follow.__new__(Follow)
        f.c = dict(min_leader_usd=0.0, thin_max_share=0, max_per_token=4, hours={})
        f.leaders, f.bench, f.filler, f.leader, f.u, f.chain = ['0xm'], ['0xb'], ['0xf'], '0xm', K['currency0'], None
        f.st = dict(leader={}, ours={str(i): dict(leader_tid=i, leader='0xf', token=K['currency1'], size_usd=70, entry_ts=1e9 - i) for i in range(4)})
        recs, closed = [], []
        f.rec = lambda **r: recs.append(r)
        f.on_leader_exit = lambda t, why=None, **k: (closed.append(t), f.st['ours'][t].update(closed=True))
        n_tok = lambda tok: sum(1 for o in f.st['ours'].values() if not o.get('closed') and o['token'] == tok)
        f.token_block = lambda tok: 'token cap: %d copies of it open' % n_tok(tok) if n_tok(tok) >= 4 else None
        f.wallet_block = lambda who: 'wallet limit: 1 copies of 0xb open (bench)' if who == '0xb' else None
        f.decimals, f.symbol, f.cold_pool = (lambda t: 18), (lambda t: 'TOK'), (lambda pid: None)
        f.tr = types.SimpleNamespace(stopped=lambda: None)
        f.on_leader_mint(100, '0xb')
        A(not closed and recs[-1]['why'].startswith('wallet limit'), 'a refused bench mint at the token cap closes nothing')
        f.on_leader_mint(102, '0xf')
        A(not closed and recs[-1]['why'].startswith('token cap'), 'a filler mint at the cap never closes a filler copy')
        f.wallet_block, f.room = (lambda who: None), (lambda tier: ([], 0, 70, 'capital: stop here'))
        f.on_leader_mint(101, '0xb')
        A(closed == ['0'], 'an allowed bench mint closes the newest filler copy of the token (%s)' % closed)
    finally:
        g.update(saved)
    # DYNAMIC SLOTS: max_open under slot_min_book; above it book / target, never fewer
    f = Follow.__new__(Follow)
    f.c = dict(max_open=5, size_frac=0.2, slot_target_usd=65, slot_min_book=1000)
    A(f.slots(400) == 5 and f.slots(999) == 5, 'under the floor: max_open')
    A(f.slots(1000) == 15 and f.slots(2500) == 38 and f.slots(5000) == 77, 'from the floor: book / $65 (%s)' % [f.slots(b) for b in (1000, 2500, 5000)])
    f.c = dict(max_open=5, size_frac=0.2)
    A(f.slots(5000) == 5, 'no knobs: max_open as before')
    # skip codes: every reason the desk has logged maps to a stable code; a new reason keeps its own prefix
    for why, code in (('capital: 5 open, $538 deployed', 'capital'), ('wallet limit: 1 copies of 0x4b2d open (bench)', 'wallet limit'),
                      ('bench: room kept for a main leader (4 open, $400 deployed)', 'room kept for a main'),
                      ('leader position $13.33', 'leader below floor'), ('STOP (tripped while making room)', 'stop'),
                      ('cold pool: last hour volume z -0.40 under -0.22', 'cold pool'), ('no USDG or ETH side', 'not copyable'),
                      ('token cooling off: our last 9 copies of it -17.08 in 24h', 'token cooling off'),
                      ('something new: detail', 'something new'), (None, 'other')):
        A(skip_code(why) == code, 'skip code %r -> %r (got %r)' % (why, code, skip_code(why)))
    # squad agreement: only OTHER wallets, only inside the window, remembered per token
    g = Follow.__new__(Follow)
    g.st, g.c = {}, dict(filler_consensus_h=2.0)
    t0 = 1_000_000.0
    A(not g.agreement('0xTok', '0xF', now=t0), 'nobody minted it: no agreement')
    g.heard_mint('0xTok', '0xF', now=t0)
    A(not g.agreement('0xtok', '0xf', now=t0 + 60), 'its own earlier mint is not agreement')
    g.heard_mint('0xTok', '0xMain', now=t0 + 120)
    A(g.agreement('0xtok', '0xf', now=t0 + 600) == {'0xmain'}, 'another squad wallet 8 min earlier is agreement')
    A(not g.agreement('0xtok', '0xf', now=t0 + 120 + 2 * 3600 + 1), 'older than the window: none')
    A(not g.agreement('0xother', '0xf', now=t0 + 600), 'another token: none')
    g.heard_mint('0xTok', '0xB', now=t0 + 90000)
    A([w for _, w in g.st['mints']['0xtok']] == ['0xb'], 'the memory keeps 24 h per token')
    A(skip_code('no squad agreement: no other squad wallet in SGT in 2 h') == 'no squad agreement', 'its skip code')
    A(skip_code('entry cost: our entry swap would cost 4.2% of the copy (over 3.0%)') == 'entry cost', 'the entry cost skip code')
    b = _tx_brief(dict(action='mint', tx='0xab', gas_usd=0.03, ok=True, liquidity=5, slip_bps=None, pool='0xp'))
    A(b == dict(action='mint', tx='0xab', gas_usd=0.03, pool='0xp'), 'a tx brief keeps what, hash, gas, pool; drops the rest (%s)' % b)
    return n[0]


if __name__ == '__main__':
    print('lpfollow selftest: %d asserts' % _selftest())


def check(chain):
    """Every open copy against the chain: the leader's position (owner, liquidity vs its peak), ours (owner, liquidity),
    our value now. A copy whose leader has already exited, or that we no longer hold, is an ISSUE - the daemon should
    have closed it, so seeing one means it did not."""
    from core import lpbook
    st = json.load(open(_state_path(chain))) if os.path.exists(_state_path(chain)) else dict(ours={}, leader={})
    lead = leaders(chain)
    me = lpbook.account(chain).address.lower()
    opn = [o for o in st['ours'].values() if not o.get('closed')]
    rows, issues = [], []
    if not opn:
        return rows, issues
    calls = []
    for o in opn:
        calls += [_call(POSM, '0x6352211e' + '%064x' % int(o['leader_tid'])), _call(POSM, '0x1efeed33' + '%064x' % int(o['leader_tid'])),
                  _call(POSM, '0x6352211e' + '%064x' % int(o['token_id'])), _call(POSM, '0x1efeed33' + '%064x' % int(o['token_id']))]
    r = _batch(calls)

    class _D:
        pass
    vals = value_of(_D(), opn)
    for i, o in enumerate(opn):
        lo, ll, oo, ol = r[4 * i:4 * i + 4]
        l_owner = '0x' + lo[-40:].lower() if lo and len(lo) >= 66 else None
        l_liq = int(ll, 16) if ll else 0
        peak = (st['leader'].get(str(o['leader_tid'])) or {}).get('peak') or l_liq
        o_owner = '0x' + oo[-40:].lower() if oo and len(oo) >= 66 else None
        o_liq = int(ol, 16) if ol else 0
        v = vals.get(o['token_id']) or {}
        leader = (o.get('leader') or lead[0]).lower()
        row = dict(sym=o.get('symbol') or o['pool'][:10], leader=leader, leader_tid=o['leader_tid'], our_tid=o['token_id'], size=o['size_usd'],
                   held_h=(time.time() - o['entry_ts']) / 3600, leader_owner=l_owner, leader_liq_pct=100 * l_liq / peak if peak else 0,
                   leader_holds=bool(l_liq) and l_liq >= cfg(chain)['exit_frac'] * peak, we_hold=o_owner == me and o_liq > 0,
                   value=(v.get('principal') or 0) + (v.get('fees') or 0), fees=v.get('fees') or 0, in_range=v.get('in_range'),
                   leader_moved=l_owner not in (None, leader))
        rows.append(row)
        if not row['leader_holds']:
            issues.append('%s: the leader has exited #%s (liquidity %.0f%% of peak) but our copy #%s is still open' % (
                row['sym'], o['leader_tid'], row['leader_liq_pct'], o['token_id']))
        if not row['we_hold']:
            issues.append('%s: our copy #%s is not ours any more or is empty (owner %s)' % (row['sym'], o['token_id'], o_owner))
    return rows, issues


def replay_store(leader, days=7.0, size=25.0):
    """THE NIGHTLY REPLAY, incremental (owner 2026-09-29: keep the data, only append): a wallet already in the store is
    replayed only for the positions that closed since its last stored copy (an hour of overlap); a new wallet gets its
    full window once. Copies are stored at our size with the no-delay model and the leader's own result beside them,
    and the wallet's summary row is computed from the stored copies, so full and incremental agree. Returns seconds."""
    from copydesk import lpcopy as C, lpdb
    t = time.time()
    a = leader.lower()
    last = lpdb.last_copy(a)
    since = last - 3600 if last and last > time.time() - days * 86400 else None
    R = C.run(a, days, [0, COPY_DELAY], size=size, since=since)
    z = {round(x['t0']): x for x in R['delays'][0]['trades']}
    # the no-delay run replays the LEADER's own liquidity (lpcopy: own_l at d=0), so it is scaled to our size per dollar -
    # per dollar of the MODEL's own entry value, not the wallet book's invested: the book cannot see native ETH sent with
    # a mint (0x8a22: $0.12 invested vs $205 in the pool), and dividing by it blew one copy up to a $765,954 model
    km = lambda y: size / y['model_in'] if y.get('model_in') else 0.0
    k = lambda x: size / x['invested'] if x['invested'] else 0.0
    # THE TRUST TARGET (2026-09-30): what each position's exit really paid (its burn's net token flows, lpcopy._paid),
    # minus the model's own entry value - the model is judged on the pool's payout, not on the wallet's later trading.
    # None where the payout cannot be read cleanly (no burn, a re-range, native ETH): those copies do not vote on trust
    paid = lambda y: (y['paid'] - y['model_in']) * km(y) if y.get('paid') is not None and y.get('model_in') else None
    tr = [dict(x, model0=z[round(x['t0'])]['model'] * km(z[round(x['t0'])]), real_ours=paid(z[round(x['t0'])]))
          for x in R['delays'][COPY_DELAY]['trades'] if round(x['t0']) in z]
    lpdb.save_trades(a, tr, size, since=since)
    lpdb.mark_replayed(a)
    real, m0, ours = lpdb.copy_summary(a, days, size)
    lpdb.save_replay(a, days, real, m0, None, ours, -1, size)                  # delay_blocks -1: the live lag per copy kind
    # DISK: the fetch caches the wallet's raw logs and txs (~4 MB a wallet; 617 wallets made a 2.5 GB store on a disk with
    # 3 GB free). Kept only for wallets we follow (their nightly replay stays incremental); any other is refetched
    try:
        from core import chain as cm_
        c_ = cm_.load('robinhood')
        if a not in set(leaders(c_)) | set(bench(c_)):
            lpdb.prune_raw(a)
    except Exception:
        pass
    return round(time.time() - t, 1), since is not None, len(tr)


def grid(leader, days=7.0, log=print, cfg_size=25.0):
    """The replayed copy (lpcopy, 7 s late, validation ratio applied, our gas) at every size and capital."""
    import collections as C_
    from copydesk import lpcopy as C
    # OUR measured copy delay: the first live copy minted 33 blocks (~3.0 s) after its leader; the daemon polls every
    # 3 s. The replay prices copies at that delay (COPY_DELAY) and at 2 s; the old 7 s was a guess, not a measurement
    R = C.run(leader.lower(), days, [0, 20, COPY_DELAY])
    v0, vd, v2 = R['delays'][0], R['delays'][COPY_DELAY], R['delays'][20]
    ratio = min(1.0, v0['actual'] / v0['pnl_known']) if v0['pnl_known'] else 1.0
    try:
        # THE KEEP RATE AT OUR SIZE: a copy the leader's size shares his fees 50/50 in the thin pools a first LP opens,
        # which halved every such leader (0x1017f961: 20% kept at its size, 71% at $25). Per dollar, ours vs his.
        size = float(cfg_size or 25.0)
        Rs = C.run(leader.lower(), days, [0, COPY_DELAY], size=size)
        t0_ = {round(t['t0']): t for t in Rs['delays'][0]['trades']}
        his = sum(t0_[round(t['t0'])]['model'] * size / t0_[round(t['t0'])]['invested'] for t in Rs['delays'][COPY_DELAY]['trades']
                  if round(t['t0']) in t0_ and t0_[round(t['t0'])]['invested'])
        ours = sum(t['model'] for t in Rs['delays'][COPY_DELAY]['trades'] if round(t['t0']) in t0_)
        from copydesk import lpdb
        lpdb.save_replay(leader.lower(), days, v0['actual'], his, v2['pnl'], ours, -1, size)
        lpdb.save_trades(leader.lower(), Rs['delays'][COPY_DELAY]['trades'], size)
    except Exception:
        pass
    out = ['%s %g d: %d positions replayed; its real P&L $%.2f, the model at no delay $%.2f (ratio %.2f); copy +2 s $%.2f, at our live lag $%.2f (%.0f s)' % (
        leader[:10], days, len(vd['trades']), v0['actual'], v0['pnl'], ratio, v2['pnl'], vd['pnl'], R['secs'])]
    tr = sorted(vd['trades'], key=lambda t: t['t0'])
    for cap in (100, 300, 800):
        for size in (25, 50, 100, 200):
            if size > cap:
                continue
            open_, pnl, n, skip, d = [], 0.0, 0, 0, C_.Counter()
            for t in tr:
                open_ = [o for o in open_ if o > t['t0']]
                if (len(open_) + 1) * size > cap:
                    skip += 1
                    continue
                p = t['model'] * size / t['invested'] * ratio - GAS_CYCLE_USD
                pnl += p
                n += 1
                open_.append(t['t1'])
                d[time.strftime('%m-%d', time.gmtime(t['t1']))] += p
            out.append('  capital $%4d  size $%3d  copies %4d  skipped %4d  P&L $%+8.2f  (%+.0f%% of capital a week)  green days %d/%d  worst day $%+.2f' % (
                cap, size, n, skip, pnl, 100 * pnl / cap * 7 / days, sum(v > 0 for v in d.values()), len(d), min(d.values()) if d else 0))
    return dict(leader=leader, days=days, lines=out), out


def portfolio(leaders_, days=14.0, size=25.0, cap=100.0, lag=0.9):
    """Our desk copying several leaders at once over the same days, from the store: first come first served inside our
    capital; each copy earns the leader's own realized % x `lag`, minus our gas. Plus when each is active and how their
    daily P&L correlates - two leaders that are busy at different hours and rarely lose together fill each other's gaps."""
    import itertools, statistics as S
    from copydesk import lpdb
    con = lpdb.connect()
    t0 = time.time() - days * 86400
    P = {a: con.execute('SELECT open_ts, close_ts, invested_usd, realized_pct FROM positions WHERE owner=? AND closed=1 AND skip IS NULL '
                        'AND realized_pct IS NOT NULL AND open_ts >= ? AND invested_usd >= 20', (a, t0)).fetchall() for a in leaders_}
    ds = [time.strftime('%Y-%m-%d', time.gmtime(t0 + 86400 * (i + 1))) for i in range(int(days))]
    D = {a: dict(con.execute('SELECT day, net_usd FROM daily WHERE owner=? AND day >= ?', (a, ds[0])).fetchall()) for a in leaders_}
    nm = lambda a: a[:6]
    out = ['positions opened per 3-hour block (UTC), last %g days' % days, '  %-7s' % '' + ''.join('%6s' % ('%02d-%02d' % (h, h + 3)) for h in range(0, 24, 3))]
    for a in leaders_:
        c = collections.Counter(int(time.gmtime(p[0]).tm_hour // 3) for p in P[a])
        out.append('  %-7s' % nm(a) + ''.join('%6d' % c[i] for i in range(8)))

    def corr(x, y):
        mx, my = S.mean(x), S.mean(y)
        den = (sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)) ** .5
        return sum((a - mx) * (b - my) for a, b in zip(x, y)) / den if den else 0.0
    out += ['daily LP P&L correlation', '  %-7s' % '' + ''.join('%8s' % nm(b) for b in leaders_)]
    out += ['  %-7s' % nm(a) + ''.join('%8.2f' % corr([D[a].get(d, 0) for d in ds], [D[b].get(d, 0) for d in ds]) for b in leaders_) for a in leaders_]

    def sim(ls):
        ev = sorted((p[0], p[1], p[3], a) for a in ls for p in P[a])
        open_, pnl, n, skip, day = [], 0.0, 0, 0, collections.Counter()
        for o, c, pct, a in ev:
            open_ = [x for x in open_ if x > o]
            if (len(open_) + 1) * size > cap:
                skip += 1
                continue
            r = size * pct / 100 * lag - GAS_CYCLE_USD
            pnl += r
            n += 1
            open_.append(c)
            day[time.strftime('%Y-%m-%d', time.gmtime(c))] += r
        net = [day.get(d, 0) for d in ds]
        return pnl, n, skip, sum(x > 0 for x in net), min(net)
    res = sorted(((sim(c), c) for k in range(1, min(3, len(leaders_)) + 1) for c in itertools.combinations(leaders_, k)), key=lambda x: -x[0][0])
    out.append('our desk ($%.0f copies, $%.0f deployed, lag -%.0f%%, our gas) copying each combination:' % (size, cap, 100 * (1 - lag)))
    out += ['  %-26s P&L $%+8.2f  copies %4d  skipped %4d  green %d/%d  worst day $%+.2f' % ('+'.join(nm(a) for a in c), r[0], r[1], r[2], r[3], len(ds), r[4])
            for r, c in res[:12]]
    return dict(best=[list(c) for _, c in res[:3]]), out
