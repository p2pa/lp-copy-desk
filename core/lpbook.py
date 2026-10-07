"""The LP program's book: its positions, their value read off the chain, the high-water mark and the program
stop. var/<chain>/lp/book.json. Nothing trades until `lp init` writes a book; `STOP` in var/lp/ (written by the
-30% stop, or by hand) halts every send - the arb desk's own STOP file is never touched.

VALUE, from the chain every time: per position, PositionManager.getPositionLiquidity(tokenId), principal from
that L, the ticks and the pool's sqrtP; uncollected fees = L x (StateView.getFeeGrowthInside - the position's
feeGrowthInsideLast from StateView.getPositionInfo(pool, owner=PositionManager, ticks, salt=tokenId)). Plus the
wallet's idle USDG, any token it still holds of a pool it has held (at that pool's price), and its gas ETH, so
gas spent shows up as the cost it is. A position that cannot be read makes the total None, never short.

REBALANCE (the doc's five steps). Takes the screen's picks as an argument (cli wires discovery to it; core
never imports discovery): keep positions whose pool is still picked, fill to N with random qualifiers - one per
token, never ranked (the top 3 by trailing fee beat the rest in 2 of 4 weeks) - exit the rest, size new
entries at capital / N capped by the idle USDG, record tokenId/pool/ticks/entry."""
import json, os, random, time

from eth_abi import encode


def lpdir(chain):
    d = os.path.join(chain.var, 'lp')
    os.makedirs(d, exist_ok=True)
    return d


def _path(chain, base):
    return os.path.join(lpdir(chain), base)


class _Dir:
    def __init__(self, d):
        self.d = d

    def path(self, b):
        return os.path.join(self.d, b)


def tracer(chain, run=None):
    """One run's trace: every record lands in var/lp/trace.jsonl (core/trace.py's writer and reader) with
    rid = this run's id, so `hookbot <chain> lp trace <run|pool|sym|tx>` pulls a whole run or one position's
    history. Also routes core/exec/lp's per-send records here. Returns ev(stage, **fields)."""
    from core import trace
    from core.exec import lp
    t = trace.Trace(chain, _Dir(lpdir(chain)))
    run = run or time.strftime('%m%d%H%M%S', time.gmtime())

    def ev(stage, **kw):
        return t(stage, rid=run, **kw)
    ev.run = run
    ev.path = t.path
    lp.TRACE = ev
    return ev


def _ev(stage, **kw):
    from core.exec import lp
    if lp.TRACE:
        lp.TRACE(stage, **kw)


def load(chain):
    try:
        return json.load(open(_path(chain, 'book.json')))
    except Exception:
        return None


def save(chain, book):
    p = _path(chain, 'book.json')
    json.dump(book, open(p + '.tmp', 'w'), indent=1)
    os.replace(p + '.tmp', p)


def stopped(chain):
    return os.path.exists(_path(chain, 'STOP'))


def write_stop(chain, why):
    open(_path(chain, 'STOP'), 'w').write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()), why))


def account(chain):
    from core.exec import wallet
    return wallet.account(chain, chain.cfg['lp']['wallet'])


def init(chain, address, value_usd):
    book = dict(address=address, capital=value_usd, hwm=value_usd, started=time.time(), positions=[], closed=[],
                tokens={}, last_rebalance=None, history=[])
    save(chain, book)
    return book


# --------------------------------------------------------------------------- value
def _sel(sig):
    from core.exec.lp import _sel as s
    return s(sig)


def position_value(chain, pos, with_range=False):
    """(principal USDG, fees USDG, liquidity) of one position, from the chain; + in_range when with_range."""
    from core import amm
    from core.exec import lp
    pm = chain.cfg['lp']['positionmanager']
    tid = pos['token_id']
    pid = lp.pool_id(pos['key'])
    tl, tu = pos['tick_lower'], pos['tick_upper']
    sv = chain.stateview
    calls = [(pm, _sel('getPositionLiquidity(uint256)') + encode(['uint256'], [tid]).hex()),
             (sv, _sel('getFeeGrowthInside(bytes32,int24,int24)') + encode(['bytes32', 'int24', 'int24'], [bytes.fromhex(pid[2:]), tl, tu]).hex()),
             (sv, _sel('getPositionInfo(bytes32,address,int24,int24,bytes32)') + encode(
                 ['bytes32', 'address', 'int24', 'int24', 'bytes32'], [bytes.fromhex(pid[2:]), pm, tl, tu, tid.to_bytes(32, 'big')]).hex())]
    (o1, h1), (o2, h2), (o3, h3) = chain.node.multicall(calls)
    if not (o1 and o2 and o3):
        raise RuntimeError('position %s unreadable' % tid)
    L = int(h1[2:66], 16)
    fi0, fi1 = int(h2[2:66], 16), int(h2[66:130], 16)
    last0, last1 = int(h3[66:130], 16), int(h3[130:194], 16)
    M = 2 ** 256
    f0, f1 = L * ((fi0 - last0) % M) // 2 ** 128, L * ((fi1 - last1) % M) // 2 ** 128
    sp = lp.slot0(chain, pos['key'])[0]
    sa, sb = amm.sqrt_at_tick(tl), amm.sqrt_at_tick(tu)
    a0, a1 = lp.amounts_for(sp, sa, sb, L)
    # WHAT the position holds comes from its own pool; what that is WORTH from the market. For an undercut or own-hook
    # pool that is the incumbent (swap_key): our pool has no liquidity outside our range, so its price is meaningless
    # there - #3227037 sat at the minimum tick and valued 7,008 fee tokens at 2.4e54 USDG (2026-09-24)
    mk = pos.get('swap_key')
    px = lp.slot0(chain, mk)[0] if mk and mk != pos['key'] else sp
    to_u = lambda x0, x1: to_usdg(pos['usdg_is_c0'], px, x0, x1)
    dec = chain.quotes[lp.usdg(chain)][2]
    out = (to_u(a0, a1) / 10 ** dec, to_u(f0, f1) / 10 ** dec, L)
    return out + (sa <= sp < sb,) if with_range else out


def band_pct(chain, pos):
    """Where the TOKEN's price sits in the position's range: 0 = the range's low edge, 100 = its high edge
    (outside the range: below 0 or above 100). Log scale, the way ticks are."""
    import math
    from core.exec import lp
    sp = lp.slot0(chain, pos['key'])[0]
    f = (math.log(sp) - math.log(1.0001 ** (pos['tick_lower'] / 2) * 2 ** 96)) / ((pos['tick_upper'] - pos['tick_lower']) / 2 * math.log(1.0001))
    return round(100 * (1 - f if pos.get('usdg_is_c0') else f), 1)   # USDG as currency0: the token's price is 1/p


_DEC = {}


def prices(chain, pos):
    """(low, now, high) USDG per token for the position's range and its pool's price, the way an LP app shows it."""
    from core import amm
    from core.exec import lp
    t = pos.get('token') or pos['key']['currency1' if pos.get('usdg_is_c0') else 'currency0']
    if t not in _DEC:
        _DEC[t] = int(chain.node.multicall([(t, '0x313ce567')])[0][1], 16)
    k = 10 ** (_DEC[t] - chain.quotes[lp.usdg(chain)][2])
    px = lambda sq: ((2 ** 96 / sq) ** 2 if pos.get('usdg_is_c0') else (sq / 2 ** 96) ** 2) * k
    a, b = px(amm.sqrt_at_tick(pos['tick_lower'])), px(amm.sqrt_at_tick(pos['tick_upper']))
    return min(a, b), px(lp.slot0(chain, pos['key'])[0]), max(a, b)


def to_usdg(u, sp, a0, a1):
    """USDG raw value of (a0, a1) at sqrtPriceX96 sp. u = USDG is currency0."""
    p = (sp / 2 ** 96) ** 2
    return a0 + a1 / p if u else a0 * p + a1


def value(chain, book):
    """{total, positions:[...], idle_usdg, idle_tokens, gas_usd}; total None if any position is unreadable."""
    from core.exec import lp, wallet
    addr = book['address']
    rows, total = [], 0.0
    for pos in book['positions']:
        try:
            pr, fe, L, inr = position_value(chain, pos, with_range=True)
            rows.append(dict(pool=pos['pool'], symbol=pos.get('symbol', ''), token_id=pos['token_id'], principal=round(pr, 4),
                             fees=round(fe, 4), value=round(pr + fe, 4), entry=round(pos['entry_usd'], 4),
                             pnl_pct=round(100 * ((pr + fe + (pos.get('fees_collected') or 0)) / pos['entry_usd'] - 1), 2) if pos.get('entry_usd') else None,
                             fees_collected=round(pos.get('fees_collected') or 0, 4),
                             liquidity=L, in_range=inr, band_pct=band_pct(chain, pos),
                             fee_pct=pos['key']['fee'] / 1e4 if pos['key']['fee'] < 0x800000 else None, px=prices(chain, pos),
                             price_move=round(pr - pos['entry_usd'], 4) if pos.get('entry_usd') else None,
                             held_h=round((time.time() - pos['entry_ts']) / 3600, 1) if pos.get('entry_ts') else None))
            total += pr + fe
        except Exception as e:
            rows.append(dict(pool=pos['pool'], token_id=pos.get('token_id'), error=str(e)[:120]))
            total = None
            break
    u = lp.usdg(chain)
    dec = chain.quotes[u][2]
    idle_u = lp.balance(chain, u, addr) / 10 ** dec
    idle_t = 0.0
    for tok, key in (book.get('tokens') or {}).items():
        b = lp.balance(chain, tok, addr)
        if b:
            sp = lp.slot0(chain, key)[0]
            uc0 = key['currency0'].lower() == u
            idle_t += to_usdg(uc0, sp, 0 if uc0 else b, b if uc0 else 0) / 10 ** dec
    if chain.gas_token == 'usdc':
        # ONE BALANCE ON ARC: the native gas coin IS the USDC the ERC-20 face shows (1e12 apart). Count it once, and label
        # the [lp] gas_reserve_usd slice as gas so the deployable figure (total - gas) and the entries leave it alone
        gas = min(idle_u, gas_reserve_usd(chain))
        idle_u -= gas
    else:
        gas = wallet.usd_balance(chain, addr)
    if total is not None:
        total += idle_u + idle_t + gas
    return dict(total=None if total is None else round(total, 4), positions=rows, idle_usdg=round(idle_u, 4),
                idle_tokens=round(idle_t, 4), gas_usd=round(gas, 4))


def mark(book, total, stop_dd, now=None):
    """Record today's value, move the high-water mark; True when the drawdown from it reaches stop_dd."""
    if total is None:
        return False
    book['hwm'] = max(book.get('hwm') or total, total)
    book.setdefault('history', []).append(dict(ts=now or time.time(), value=total))
    book['history'] = book['history'][-400:]
    return total <= book['hwm'] * (1 - stop_dd)


# --------------------------------------------------------------------------- rebalance
def target_n(chain, total):
    c = chain.cfg['lp']
    return int(c['positions_big'] if total and total / max(1, int(c['positions_big'])) > float(c['big_at_usd'])
               else c['positions'])


def plan(book, picks, n, rng=random):
    """(keep, exit, enter): positions still picked stay; new picks fill to n, ONE PER TOKEN, at random."""
    picked = {p['pool'] for p in picks}
    # our own hooked pool is never in the screen (it only admits hookless pools): it stays until the stop, an
    # exit-all or its kill line, and its token counts as held so the basket never competes with it
    keep = [p for p in book['positions'] if p['pool'] in picked or p.get('hooked')]
    out = [p for p in book['positions'] if p['pool'] not in picked and not p.get('hooked')]
    held_tok = {p['token'] for p in keep}
    pool, seen = [], set(held_tok)
    for p in picks:
        if p['token'] not in seen:
            seen.add(p['token'])
            pool.append(p)
    new = rng.sample(pool, min(max(0, n - len(keep)), len(pool)))
    return keep, out, new


def gas_reserve_usd(chain):
    """Quote kept back for gas where the gas coin IS the quote (arc: USDC). 0 where gas is ETH."""
    return float((chain.cfg.get('lp') or {}).get('gas_reserve_usd', 3.0)) if chain.gas_token == 'usdc' else 0.0


TOP_UP_MIN_USD = 1.0        # below this per kept position a top-up costs more in gas and swap fee than it earns
TOP_UP_RESERVE_USD = 0.10   # USDG left idle after a top-up round (rounding, the next approve)


def rebalance(chain, book, picks, go, total=None, log=print, rng=random):
    """The doc's rebalance on `picks` (the screen's output). Mutates and saves the book when go."""
    from core.exec import lp
    if stopped(chain):
        raise RuntimeError('lp STOP file present: %s' % open(_path(chain, 'STOP')).read().strip())
    ac = account(chain)
    n = target_n(chain, total)
    keep, out, new = plan(book, picks, n, rng)
    by = {p['pool']: p for p in picks}
    _ev('plan', go=go, n=n, total=total, picks=len(picks),
        keep=[dict(pool=p['pool'], sym=p.get('symbol'), token_id=p['token_id'], hooked=bool(p.get('hooked')),
                   rate=by[p['pool']]['trailing_fee_pct'] if p['pool'] in by else None) for p in keep],
        exit=[dict(pool=p['pool'], sym=p.get('symbol'), token_id=p['token_id'], why='no longer qualifies') for p in out],
        enter=[dict(pool=p['pool'], sym=p.get('symbol'), rate=p['trailing_fee_pct'], depth=p['depth_usd'], fee=p['lp_fee_pct'])
               for p in new],
        passed_over=[dict(pool=p['pool'], sym=p.get('symbol'), why='token held or random pick')
                     for p in picks if p not in new and p['pool'] not in {k['pool'] for k in keep}])
    log('lp rebalance: keep %d, exit %d, enter %d (target %d)' % (len(keep), len(out), len(new), n))
    for p in out:
        log('  exit %s #%s' % (p.get('symbol') or p['pool'][:10], p['token_id']))
        close(chain, ac, book, p, go, log)
    u = lp.usdg(chain)
    dec = chain.quotes[u][2]
    # ALL FEES REINVESTED (the doc): a KEPT position's fees sit owed in the position and earn nothing - collect
    # them now, and whatever the new entries do not take is added back to the kept positions below
    fees_in = 0
    for p in keep:
        log('  collect %s #%s' % (p.get('symbol') or p['pool'][:10], p['token_id']))
        got = lp.collect(chain, ac, p, go, log)
        fees_in += got
        if go:                                   # fees taken OUT of this position: its P&L credits them (value())
            p['fees_collected'] = (p.get('fees_collected') or 0) + got / 10 ** dec
    idle = lp.balance(chain, u, ac.address) - int(gas_reserve_usd(chain) * 10 ** dec)
    if not go:
        idle += sum(int(p.get('entry_usd', 0) * 10 ** dec) for p in out)      # dry: what the exits would return
    if new:
        cap = int((total or 0) / n * 10 ** dec) if total else idle
        size = min(idle // len(new), cap) if cap else idle // len(new)
        log('  size %.2f USDG x %d' % (size / 10 ** dec, len(new)))
        for p in new:
            if size <= 0:
                break
            log('  enter %s fee %.2f%% ticks %d..%d' % (p.get('symbol') or p['pool'][:10], p['lp_fee_pct'], p['tick_lower'], p['tick_upper']))
            rec = lp.enter(chain, ac, p, size, go, log)
            if go:
                rec['entry_usd'] = size / 10 ** dec
                rec['entry_sp'] = lp.slot0(chain, rec['key'])[0]
                rec['entry_rate'] = p['trailing_fee_pct']
                book['positions'].append(rec)
                book.setdefault('tokens', {})[rec['token']] = rec.get('swap_key') or rec['key']
                save(chain, book)
                try:
                    pr, fe, L = position_value(chain, rec)
                    rec['entry_principal'] = pr
                    save(chain, book)
                except Exception:
                    pr = None
                # ENTRY COST vs IDLE, kept apart: what the mint did not take (the lp_slippage haircut, tick rounding)
                # stays in the wallet and in the book - it is capital left OUT of the range, not money lost
                m = rec.get('mint') or {}
                left = to_usdg(rec['usdg_is_c0'], m['sp'], m['max0'] - m['spent0'], m['max1'] - m['spent1']) / 10 ** dec if m else 0.0
                _ev('opened', pool=rec['pool'], sym=rec.get('symbol'), token_id=rec['token_id'], tx=rec.get('entry_tx'),
                    entry_usd=rec['entry_usd'], principal=pr, idle_usd=round(left, 6), ticks=[rec['tick_lower'], rec['tick_upper']],
                    entry_cost_bps=round(1e4 * (1 - (pr + left) / rec['entry_usd']), 1) if pr else None,
                    idle_bps=round(1e4 * left / rec['entry_usd'], 1),
                    model_cost_bps=round(1e4 * (p['lp_fee_pct'] / 100 * 0.5), 1))
    # ... and the rest back into the kept positions, equally (the collected fees, plus anything the entries left)
    rest = lp.balance(chain, u, ac.address) - int((TOP_UP_RESERVE_USD + gas_reserve_usd(chain)) * 10 ** dec)
    added = 0
    if keep and rest >= int(TOP_UP_MIN_USD * 10 ** dec) * len(keep):
        per = rest // len(keep)
        for p in keep:
            log('  top up %s #%s +%.2f USDG' % (p.get('symbol') or p['pool'][:10], p['token_id'], per / 10 ** dec))
            if lp.top_up(chain, ac, p, per, go, log) and go:
                p['entry_usd'] = (p.get('entry_usd') or 0) + per / 10 ** dec      # the P&L basis follows the capital
                p.setdefault('top_ups', []).append(dict(ts=time.time(), usd=per / 10 ** dec))
                added += per
                save(chain, book)
    _ev('compound', go=go, fees_collected_usd=round(fees_in / 10 ** dec, 6), reinvested_usd=round(added / 10 ** dec, 6),
        kept=len(keep))
    if go:
        book['last_rebalance'] = time.time()
        save(chain, book)
    return dict(keep=[p['pool'] for p in keep], exit=[p['pool'] for p in out], enter=[p['pool'] for p in new], n=n,
                fees_collected=round(fees_in / 10 ** dec, 4), reinvested=round(added / 10 ** dec, 4))


COMPOUND_MIN_ADD_USD = 0.05   # a collect that returns less is left idle (the next round or rebalance takes it)


def compound(chain, book, go, log=print, now=None):
    """Between rebalances: every compound_h hours, each open position whose uncollected fees reach
    compound_min_usd collects them and adds them back to ITSELF (collect -> sell the token side -> top_up).
    Returns [(symbol, fees usd, added usd)]. Backtest: see chain.toml [lp] compound_h."""
    from core.exec import lp
    cfg = chain.cfg['lp']
    now = now or time.time()
    if not compound_due(book, float(cfg.get('compound_h', 0)), now) or stopped(chain):
        return []
    ac = account(chain)
    u = lp.usdg(chain)
    dec = chain.quotes[u][2]
    out = []
    for p in list(book['positions']):
        try:
            _, fe, _ = position_value(chain, p)
        except Exception:
            continue
        if fe < float(cfg.get('compound_min_usd', 0.2)):
            continue
        got = lp.collect(chain, ac, p, go, log)
        if go:
            p['fees_collected'] = (p.get('fees_collected') or 0) + got / 10 ** dec
        add = 0
        if go and got > int(COMPOUND_MIN_ADD_USD * 10 ** dec):
            if lp.top_up(chain, ac, p, got, go, log):
                add = got
                p['entry_usd'] = (p.get('entry_usd') or 0) + add / 10 ** dec
                p.setdefault('top_ups', []).append(dict(ts=now, usd=add / 10 ** dec, from_fees=True))
        out.append((p.get('symbol') or p['pool'][:10], round(fe, 4), round(add / 10 ** dec, 4)))
        _ev('compound', go=go, pool=p['pool'], sym=p.get('symbol'), token_id=p['token_id'], fees_usd=round(fe, 6),
            collected_usd=round(got / 10 ** dec, 6), reinvested_usd=round(add / 10 ** dec, 6))
    if go:
        book['last_compound'] = now
        save(chain, book)
    return out


def compound_due(book, every_h, now=None):
    return every_h > 0 and (now or time.time()) - max(book.get('last_compound') or 0, book.get('last_rebalance') or 0) >= every_h * 3600 - 600


def close(chain, ac, book, p, go, log=print):
    """Exit one position and book what it really returned (USDG back) next to what it was worth on the chain
    the moment before (principal + fees), so eval can split fees from price and execution."""
    from core.exec import lp
    try:
        pr, fe, _ = position_value(chain, p)
    except Exception:
        pr = fe = None
    back = lp.exit(chain, ac, p, go, log)
    if not go:
        return
    dec = chain.quotes[lp.usdg(chain)][2]
    row = dict(p, exit_ts=time.time(), exit_usdg=back / 10 ** dec, exit_principal=pr, exit_fees=fe)
    book['positions'].remove(p)
    book['closed'].append(row)
    save(chain, book)
    held = (row['exit_ts'] - p.get('entry_ts', row['exit_ts'])) / 3600
    _ev('closed', pool=p['pool'], sym=p.get('symbol'), token_id=p['token_id'], entry_usd=p.get('entry_usd'),
        exit_usdg=row['exit_usdg'], value_before=None if pr is None else pr + fe, fees=fe, held_h=round(held, 1),
        pnl_pct=round(100 * (row['exit_usdg'] / p['entry_usd'] - 1), 2) if p.get('entry_usd') else None,
        exit_cost_bps=round(1e4 * (1 - row['exit_usdg'] / (pr + fe)), 1) if pr else None)


def open_hooked(chain, book, pick, go, log=print, usd=None, hook=None):
    """Our own pool next to `pick` (an incumbent from the screen): deploy the hook if needed, initialise a
    dynamic-fee pool at the incumbent's price, and mint the seed with the incumbent's tick range - bought on the
    incumbent, since the new pool is empty. Refused when the basket already holds the token: the hook pool
    exists to pull flow off the incumbent, and a position there would compete with it."""
    from core.exec import lp
    ac = account(chain)
    if any(p['token'] == pick['token'] for p in book['positions']):
        raise RuntimeError('the basket holds %s already: one position per token' % (pick.get('symbol') or pick['token']))
    desk = account_of(chain, chain.caller)
    hook = hook or lp.deploy_hook(chain, ac, desk, go, log)
    key = lp.hook_key(chain, hook, pick['token'], pick['key']['tickSpacing'])
    sp_inc = lp.slot0(chain, pick['key'])[0]
    pid = lp.pool_id(key)
    try:
        lp.slot0(chain, key)
        log('   pool %s already initialised' % pid[:12])
    except RuntimeError:
        lp.initialize(chain, ac, key, sp_inc, go, log)
    tl, tu = lp.tick_range(sp_inc, key['tickSpacing'])
    dec = chain.quotes[lp.usdg(chain)][2]
    size = int((usd or float(lp.hook_cfg(chain)['seed_usd'])) * 10 ** dec)
    mine = dict(pick, pool=pid, key=key, swap_key=pick['key'], tick_lower=tl, tick_upper=tu, incumbent=pick['pool'])
    _ev('hook_open', pool=pid, sym=pick.get('symbol'), hook=hook, incumbent=pick['pool'], desk=desk, go=go, size=size / 10 ** dec)
    if not go:
        lp.enter(chain, ac, mine, size, go, log)
        return dict(pool=pid, hook=hook, key=key)
    rec = lp.enter(chain, ac, mine, size, go, log)
    rec.update(entry_usd=size / 10 ** dec, entry_sp=lp.slot0(chain, key)[0], incumbent=pick['pool'], hook=hook)
    book['positions'].append(rec)
    book.setdefault('tokens', {})[rec['token']] = pick['key']
    save(chain, book)
    pr = position_value(chain, rec)[0]
    rec['entry_principal'] = pr
    save(chain, book)
    _ev('opened', pool=pid, sym=rec.get('symbol'), token_id=rec['token_id'], tx=rec.get('entry_tx'), hooked=True,
        entry_usd=rec['entry_usd'], principal=pr, ticks=[tl, tu])
    return rec


def account_of(chain, name):
    from core.exec import wallet
    return wallet.account(chain, name).address


def exit_all(chain, book, go, log=print):
    from core.exec import lp
    ac = account(chain)
    for p in list(book['positions']):
        close(chain, ac, book, p, go, log)
    if go:   # leftover tokens of any pool held before
        for tok, key in (book.get('tokens') or {}).items():
            lp.sell(chain, ac, key, tok, go, log)


def verify(chain, book):
    """The book against the chain - a book you have not checked is a claim. Issues, each one actionable:
    an NFT count that differs from the open positions, a position not owned by the wallet or with liquidity
    other than booked, a STOP beside open positions, a token of a pool we hold nothing in left in the wallet."""
    from core.exec import lp
    pm = chain.cfg['lp']['positionmanager']
    addr = book['address']
    issues = []
    n_nft = int(chain.node.call(pm, _sel('balanceOf(address)') + encode(['address'], [addr]).hex()), 16)
    if n_nft != len(book['positions']):
        issues.append('wallet holds %d position NFTs, book has %d open' % (n_nft, len(book['positions'])))
    for p in book['positions']:
        tid = p.get('token_id')
        if not tid:
            issues.append('%s: open position without a tokenId' % p['pool'][:10])
            continue
        try:
            owner = chain.node.call(pm, _sel('ownerOf(uint256)') + encode(['uint256'], [tid]).hex())
            if int(owner, 16) != int(addr, 16):
                issues.append('#%s owned by 0x%s, not the lp wallet' % (tid, owner[-40:]))
        except Exception as e:
            issues.append('#%s: ownerOf reverts (burned?) %s' % (tid, str(e)[:60]))
            continue
        liq = int(chain.node.call(pm, _sel('getPositionLiquidity(uint256)') + encode(['uint256'], [tid]).hex()), 16)
        if liq != p.get('liquidity'):
            issues.append('#%s liquidity %d on chain, %s booked' % (tid, liq, p.get('liquidity')))
    if stopped(chain) and book['positions']:
        issues.append('STOP is set but %d positions are open: `lp exit-all --go`' % len(book['positions']))
    held = {p['token'] for p in book['positions']}
    u = lp.usdg(chain)
    for tok, key in (book.get('tokens') or {}).items():
        if tok in held:
            continue
        b = lp.balance(chain, tok, addr)
        if b:
            sp = lp.slot0(chain, key)[0]
            uc0 = key['currency0'].lower() == u
            usd = to_usdg(uc0, sp, 0 if uc0 else b, b if uc0 else 0) / 10 ** chain.quotes[u][2]
            if usd > 0.5:
                issues.append('%.2f USDG of %s idle in the wallet, no open position: sell it' % (usd, tok[:10]))
    return dict(ok=not issues, nft=n_nft, open=len(book['positions']), issues=issues)


def due(book, hold_days, now=None):
    # CALENDAR DAYS (UTC), not hours: the daily cron runs at 00:15, and a manual rebalance at 13:36 is 10.6 h before it -
    # any hour-based slack either skips that 00:15 or lets two rebalances land in one day
    if not book.get('last_rebalance'):
        return True
    return int((now or time.time()) // 86400) - int(book['last_rebalance'] // 86400) >= hold_days


def _selftest():
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
    mk = lambda pool, tok: dict(pool=pool, token=tok, lp_fee_pct=1.0, tick_lower=0, tick_upper=1)
    book = dict(positions=[dict(pool='a', token='A', token_id=1), dict(pool='b', token='B', token_id=2)])
    picks = [mk('a', 'A'), mk('c', 'C'), mk('c2', 'C'), mk('d', 'D'), mk('e', 'E')]
    keep, out, new = plan(book, picks, 4, random.Random(1))
    A([p['pool'] for p in keep] == ['a'] and [p['pool'] for p in out] == ['b'], 'keep the still-picked, exit the rest')
    A(len(new) == 3 and len({p['token'] for p in new}) == 3 and 'A' not in {p['token'] for p in new}, 'one per token, fill to 4')
    A(not any(p['pool'] == 'c2' for p in new), 'the second pool of a token is never taken')
    _, _, few = plan(dict(positions=[]), [mk('x', 'X')], 4)
    A(len(few) == 1, 'fewer qualifiers than slots: take what there is')
    # the stop: -30% from the high-water mark, not from the start
    b = dict(hwm=100.0)
    A(not mark(b, 130.0, 0.30) and b['hwm'] == 130.0, 'new high')
    A(not mark(b, 92.0, 0.30), '-29% from the high: hold')
    A(mark(b, 91.0, 0.30), '-30% from the high: stop')
    A(not mark(b, None, 0.30) and b['hwm'] == 130.0, 'an unreadable book never trips or moves the stop')
    A(compound_due(dict(last_rebalance=0.0), 24, now=86400) and not compound_due(dict(last_compound=100.0), 24, now=3600)
      and not compound_due(dict(last_rebalance=0.0), 0, now=10 ** 9), 'compound every compound_h, off at 0')
    A(due(dict(), 7) and not due(dict(last_rebalance=1000.0), 7, now=1000.0 + 86400) and due(dict(last_rebalance=0.0), 7, now=7 * 86400),
      'hold interval')
    # USDG value of a two-sided holding, both orientations
    sp = 2 ** 96 * 2                               # price 4 (token1 per token0)
    A(abs(to_usdg(True, sp, 10, 40) - 20) < 1e-9 and abs(to_usdg(False, sp, 10, 40) - 80) < 1e-9)
    print('lpbook selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
