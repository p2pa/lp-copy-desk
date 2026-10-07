"""lpdesk - the copy LP desk, all on the command line. `./lpdesk [--chain robinhood] <command> [args] [--json] [--go]`

Every command returns structured data and prints it two ways: aligned text, or with --json only the JSON document (stdout
stays parseable, so a script or an agent can chain calls: `./lpdesk status --json | jq .`). A live send needs --go AND the
environment variable LIVE_OK=yes; without both the desk prices, decides and logs, but signs nothing.

SET UP
  init                         chains/<chain>/chain.toml from the example (once) and the var/ folders
  doctor                       config, wallet file, endpoints, history store, Telegram: what is missing, before anything runs
  smoke                        every module's offline selftest (no network, no keys)

THE DESK
  run --go [--seconds N]       the daemon: copies the squad's new positions, exits with them (supervise it; see README)
  status                       open copies checked against their leaders and the chain, the record, the exit sales
  watch                        full screen, live: book, open copies, wallets, event feed (q / ctrl-c)
  stop | resume                no new copies (open ones still exit with their leader) | copying again
  close <leader tokenId ...>   the desk closes those copies on its next loop, through its own exit
  treasury [resume]            USDG <-> ETH conversions, the gas band, dust; resume after a stop
  card [--always]              the Telegram card now (cron hourly; silent with nothing open)
  log [N|tokenId|SYM|0x..|skips H|errors H]   the desk's own records, one copy at a time
  trace [key]                  the send trace (var/lp/trace.jsonl)

CHOOSING WHOM TO FOLLOW
  hist [ingest|backfill N|prune]   the local LP history store every replay reads (cron: ingest every minute)
  nightly [--dry]              discover -> replay the store -> measure gas + take -> pick --apply -> knobs (cron 03:00 UTC)
  repick [--dry]               replay what closed since, then pick --apply (cron hourly)
  pick [--apply]               main leaders + bench + filler from replayed copies (--apply rewrites [lp.follow])
  find board|bench|replay-all|calibrate|replay <addr ...>   discovery and the copy score
  qc [addr ...] [--hours 24]   quality control before a wallet joins the squad
  wallet <addr> [--days 14]    a wallet's own LP P&L, checked against its balances
  copy <addr> [--days 7]       replay copying it: validation, +2 s / +7 s, every size and capital
  backtest [addr] [--days 7] [--size S] [--capital C]   copying one leader at our size
  portfolio [addr ...]         several leaders at once: hours, correlation, best mix
  verify [addr ...]            the LP db's checks per wallet
  optimize [--n 12] [--k 3]    who to follow and when: 1-k leaders x hours, walk-forward, at our size
  tune [--only k1,k2] [--folds 5] | tune recency|squad|downside|trail   the blind fold tests behind every desk setting

Flags: --json, --go, --limit N, --chain NAME (default robinhood: chains/<NAME>/chain.toml)."""
import json, os, shutil, sys, time

try:
    sys.stdout.reconfigure(line_buffering=True)           # a supervised daemon's log flushes per line, not per 4 KB
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


class Flags(dict):
    __getattr__ = dict.get


def parse_flags(args):
    f, rest, i = Flags(json=False, go=False, limit=None, chain='robinhood'), [], 0
    while i < len(args):
        a = args[i]
        if a in ('--json', '--go'):
            f[a[2:]] = True
        elif a in ('--limit', '--chain') and i + 1 < len(args):
            f[a[2:]] = int(args[i + 1]) if a == '--limit' else args[i + 1]
            i += 1
        else:
            rest.append(a)
        i += 1
    return f, rest


def _load(name):
    from core import chain as cm
    return cm.load(name)


def cmd_init(name, f):
    d = os.path.join(ROOT, 'chains', name)
    out, cfg = [], os.path.join(d, 'chain.toml')
    if not os.path.exists(cfg):
        shutil.copy(os.path.join(d, 'chain.example.toml'), cfg)
        out.append('wrote %s from the example: fill in [wallet] and run `./lpdesk pick --apply` (or set [lp.follow] leader)' % cfg)
    else:
        out.append('%s exists, left alone' % cfg)
    for sub in ('var', 'var/lp'):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    out.append('config dir: %s (wallets.json, telegram.json, hypersync.token, rpc.url - none of them in the repo)' %
               os.path.expanduser('~/.config/lp-copy-desk'))
    return dict(ok=True), out


def cmd_doctor(name, f):
    """What the desk needs, checked one by one: each line OK or what to do."""
    rows = []
    add = lambda ok, what, fix='': rows.append((ok, what, fix))
    try:
        c = _load(name)
        add(True, 'config chains/%s/chain.toml' % name)
    except SystemExit as e:
        add(False, 'config', '%s - run `./lpdesk init`' % e)
        return dict(ok=False, rows=rows), ['%-4s %s %s' % ('OK' if ok else 'FIX', w, x) for ok, w, x in rows]
    fol = (c.cfg.get('lp') or {}).get('follow') or {}
    add(bool(fol.get('leader')), '[lp.follow] leader: %d main, %d bench, %d filler' % (len(fol.get('leader') or []), len(fol.get('bench') or []),
                                                                                    len(fol.get('filler') or [])), '`./lpdesk pick --apply` writes them')
    wf = c.wallet_file
    if os.path.exists(wf):
        mode = oct(os.stat(wf).st_mode & 0o777)
        w = json.load(open(wf))
        name_ = c.cfg['lp']['wallet']
        add(name_ in w and mode == '0o600', 'wallet %s in %s (mode %s)' % (name_, wf, mode), 'needs the wallet entry and chmod 600')
    else:
        add(False, 'wallet file %s' % wf, 'create it: {"lp-1": {"address": "0x..", "key": "0x.."}}, chmod 600')
    try:
        b = c.node.rpc('eth_blockNumber', [])
        add(True, 'endpoint %s at block %d' % (c.rpc_url, int(b, 16)))
    except Exception as e:
        add(False, 'endpoint %s' % c.rpc_url, str(e)[:120])
    hist = os.path.join(c.var, 'lphist.db')
    add(os.path.exists(hist), 'history store %s' % hist, '`./lpdesk hist backfill 10` (then cron `hist ingest` every minute)')
    add(os.path.exists(c.tg_config), 'telegram %s' % c.tg_config, 'optional: {"token": "...", "chat_id": "..."}; without it nothing is posted')
    from discovery import scout
    add(os.path.exists(scout.HS_TOKEN), 'hypersync token %s' % scout.HS_TOKEN, 'optional: wallet forensics (envio.dev token)')
    add(os.environ.get('LIVE_OK') == 'yes', 'LIVE_OK=yes in this environment', 'only the live daemon needs it')
    return dict(ok=all(ok for ok, _, _ in rows[:4]), rows=rows), ['%-4s %s%s' % ('OK' if ok else 'FIX', w, '' if ok else '  -> ' + x) for ok, w, x in rows]


def cmd_smoke(name, f):
    import importlib
    out, bad = [], 0
    mods = ['core.amm', 'core.chain', 'core.events', 'core.fmt', 'core.exec.lp', 'core.exec.tx', 'core.lpbook', 'core.trace',
            'copydesk.lpfollow', 'copydesk.lpcopy', 'copydesk.lpdb', 'copydesk.lphist', 'copydesk.lpopt', 'copydesk.lpfind',
            'copydesk.treasury', 'copydesk.lpcard', 'copydesk.lplive', 'copydesk.lpqc']
    for m in mods:
        try:
            mod = importlib.import_module(m)
            st = getattr(mod, '_selftest', None)
            if st:
                st()
            out.append('ok    %s%s' % (m, '' if st else ' (imports)'))
        except (Exception, SystemExit) as e:                 # a selftest that exits is a failure here, not the end of smoke
            bad += 1
            out.append('FAIL  %s: %s' % (m, str(e)[:200]))
    return dict(ok=not bad, failed=bad), out


def _lp_note(chain, go=True):
    """The LP program's one exit: tape (var/lp/tape.jsonl) + console + Telegram, tagged <chain>·lp. A DRY run
    (go False) prints and tapes but never posts: a dry rebalance paged the channel as if it had traded (2026-09-25).
    note(text) is a plain line; note.card(html) is a formatted card (core/fmt.py lp_card / lp_alarms)."""
    from core import io
    nf = io.Notifier(chain, io.Tape(io.Store(chain), 'lp/tape.jsonl'), io.Telegram(chain))
    note = lambda text, **kw: nf('note', strategy='lp', detail=text, **dict(dict(tg=go), **kw))
    note.card = lambda markup: nf('lp', strategy='lp', html=markup, tg=go)
    return note


def _lp_trace(c, a, f):
    """lp trace [run|pool|sym|tx|error|last]: the LP program's records, one line each."""
    from core import trace as tr
    path = os.path.join(c.var, 'lp', 'trace.jsonl')
    key = a[0] if a else 'last'
    rows = tr.read(path, None, limit=10 ** 6)
    if key == 'error':
        rows = [r for r in rows if r.get('stage') == 'error' or r.get('ok') is False]
    elif key == 'last':
        last = next((r['rid'] for r in reversed(rows) if r.get('stage') == 'run'), None)
        rows = [r for r in rows if r.get('rid') == last]
    else:
        k = key.lower()
        rows = [r for r in rows if any(str(r.get(x) or '').lower().startswith(k) for x in ('rid', 'pool', 'sym', 'tx'))
                or str(r.get('token_id')) == key]
    rows = rows[-(f.limit or 200):]
    skip = ('ts', 'blk', 'stage', 'rid', 'pool', 'sym', 'head_blk', 'head_ts')
    lines = ['%s %s %-8s %-10s %-8s %s' % (time.strftime('%m-%d %H:%M:%S', time.gmtime(r['ts'])), r.get('rid'), r['stage'],
                                           (r.get('sym') or '')[:10], (r.get('pool') or '')[:8],
                                           ' '.join('%s=%s' % (k, json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                                                    for k, v in r.items() if k not in skip))[:400] for r in rows]
    return {'rows': rows}, lines


def _lp_desk(c, sub, a, f):
    """lpdesk <command> ...  - the copy LP desk and everything behind it:
      [status]                         the desk: every open copy checked against its leader and the chain
      watch                            the desk full screen, live: book, open copies, wallets, event feed (q / ctrl-c)
      treasury [resume]                USDG <-> ETH conversions, the gas band, dust; resume after a stop
      close <leader tokenId ...>       the desk closes those copies on its next loop, through its own exit
      hist [ingest | backfill N | prune]   the local LP history store every replay reads (cron: ingest every 5 min)
      qc [addr ...] [--hours 24]       quality control of wallets before they join the squad (none: new ones over the filler bar)
      tune [--only k1,k2] [--folds 5]  the blind fold tests of the desk settings; `lp tune recency`: which score window predicts
      run --go [--seconds N]           the daemon (supervised; live only)
      stop | resume                    no new copies (open ones still exit with their leader) | copying again
      leaders                          who the desk follows ([lp.follow] leader in chain.toml)
      wallet <addr> [--days 14]        a wallet's LP P&L, checked against its own balances (stored in the LP db)
      copy <addr> [--days 7]           replay copying it: validation, +2 s / +7 s, every size and capital
      portfolio [addr ...] [--days 14] [--size 25] [--capital 100]   several leaders at once: hours, correlation, best mix
      find candidates|rank|board|calibrate   the wallet screen, its 0-100 score, the score's walk-forward check
      verify [addr ...]                the LP db's five checks per wallet (all stored wallets without an address)
      optimize [--n 12] [--k 3] [--notify]   who to follow and when: 1-k leaders x hours, walk-forward, at our size
      card [--always]                  the desk's Telegram card now (cron hourly; silent with nothing open)
      trace [key]                      the send trace (var/lp/trace.jsonl)"""
    from copydesk import lpfollow as L
    opt = lambda k, d: type(d)(next((a[i + 1] for i, x in enumerate(a) if x == k and i + 1 < len(a)), d))
    addrs = [x.lower() for x in a[1:] if x.startswith('0x')]
    log = (lambda *x: None) if f.json else (lambda m: print(m, flush=True))
    if sub == 'status':
        return _lp_follow_status(c, f)
    if sub == 'backtest':
        # replay copying one leader at our size (lpfollow.backtest): validation, delay, daily P&L
        leader = addrs[0] if addrs else L.leaders(c)[0]
        r = L.backtest(leader, opt('--days', 7.0), opt('--size', float(L.cfg(c)['size_usd'])),
                       opt('--capital', float(L.cfg(c)['max_deploy_usd'])), log=log)
        return r, ['%s  %g days  size $%.0f  capital $%.0f  copy %.0f s late' % (r['leader'], r['days'], r['size_usd'], r['capital_usd'], r['delay_s']),
                   '  copies %d (skipped for capital %d)  P&L $%+.2f after gas $%.2f  win %.0f%%' % (
                       r['copies'], r['skipped_for_capital'], r['pnl'], r['gas'], r['win'])] + ['  %s  $%+.2f' % kv for kv in r['daily'].items()]
    if sub == 'log':
        return _lp_log(c, a[1:], f)
    if sub == 'watch':
        from copydesk import lpwatch
        lpwatch.run(c)
        return dict(), []
    if sub == 'run':
        # THIS process only: our node first for every read the send path makes (balances, quotes, estimates, receipts);
        # public endpoints behind it; signed txs still go to the sequencer (send_urls). The AWS desks are untouched.
        if L.node_fresh():
            c.rotate = [L.NODE] + [u for u in c.rotate if u != L.NODE]
            c.rpc_url = L.NODE
            c.node.rotate, c.node.rpc_url = c.rotate, c.rpc_url
        log('lp run: reads from %s' % ('our node (%s, %s blocks behind)' % (L.NODE, L._node['lag']) if L._node['ok'] else 'public nodes' + (' (our node not fresh)' if L.NODE else '')))
        try:
            c.refresh_eth_usd()
        except Exception:
            pass
        return dict(exit=L.Follow(c, go=bool(f.go), log=log, note=_lp_note(c)).run(opt('--seconds', 0.0))), []
    if sub in ('stop', 'resume'):
        p = L.stop_path(c)
        if sub == 'stop':
            open(p, 'w').write('stopped by hand %s\n' % time.ctime())
        elif os.path.exists(p):
            os.remove(p)
        return dict(stopped=os.path.exists(p)), ['copy desk %s' % ('STOPPED: no new copies (open ones still exit with their leader)' if os.path.exists(p) else 'copying')]
    if sub == 'treasury':
        # the desk's USDG <-> ETH, gas band and dust (copydesk/treasury): its record from the mirror; `resume` where the desk runs
        from copydesk import treasury as T
        if a[1:2] == ['resume']:
            from core import lpbook
            T.Treasury(c, lpbook.account(c), L.cfg(c)).resume()
        r = T.status(c)
        return r, ['treasury %s: %d conversions in 24 h, $%.2f, %s bps under mid (weighted)' % (
            'STOPPED (%s)' % r['stopped'] if r['stopped'] else 'running', r['n24'], r['usd24'],
            '-' if r['cost_bps24'] is None else '%.1f' % r['cost_bps24'])] + [
            '  %s  %-8s $%8.2f  %.8f ETH at %.2f  %5.1f bps  %s' % (time.strftime('%m-%d %H:%M', time.gmtime(x['ts'])), x['what'], x['usd'],
                                                             x['eth'], x['px'], x['cost_bps'], x.get('why', '')) for x in r['last']]
    if sub == 'hist':
        # the local LP history store (copydesk/lphist): status | ingest | backfill <days> | prune
        from copydesk import lphist
        w = a[1] if len(a) > 1 else 'status'
        if w == 'ingest':
            lphist.ingest(c, log=log)
            lphist.heat(c, log=log)                                  # the pool heat table the desk's cold-pool rule reads
        elif w == 'backfill':
            lphist.backfill(c, float(a[2]) if len(a) > 2 else lphist.KEEP_DAYS, log=log)
        elif w == 'prune':
            lphist.prune(c)
        elif w == 'delta':                                          # credit Delta ladders already stored to their LPs (dry without --go)
            return lphist.delta_backfill(c, go=bool(getattr(f, 'go', False)) or '--go' in a, log=log), []
        st = lphist.status(c)
        f, l = lphist.Reader(c).span() if st['last'] else (None, None)
        return st, ['history store: %d swaps, %d positions, %d NFTs, %d pools (%d copyable), %.0f MB; unbroken %s' % (
            st['swaps'], st['positions'], st['nfts'], st['pools'][0] or 0, st['pools'][1] or 0, st['mb'],
            '%.2f days to block %d' % ((l - f) / 864000.0, l) if l else '-')]
    if sub == 'close':
        # lp close <leader tokenId ...>: the desk closes those copies on its next loop, through its own exit (booked as usual)
        tids = [x for x in a[1:] if x.isdigit()]
        if not tids:
            return dict(error='give leader tokenIds'), ['lp close <leader tokenId ...> (lp status lists them)']
        with open(L.close_path(c), 'a') as f:
            f.write(''.join('%s\n' % t for t in tids))
        return dict(requested=tids), ['close requested for %s: the desk closes them on its next loop (~1 s)' % ', '.join('#' + t for t in tids)]
    if sub == 'leaders':
        return dict(leaders=L.leaders(c)), ['following: ' + ', '.join(L.leaders(c)), '  change it in chains/<chain>/chain.toml [lp.follow] leader (one address or a list); restart the desk']
    if sub == 'wallet':
        from copydesk import lpwallet as W
        R = W.report(addrs[0], opt('--days', 14.0))
        return {k: v for k, v in R.items() if k != 'rows'}, [W.line(R)] + ['  %s  %3d closed  won %3d  realized $%+8.2f  LP gas $%5.2f  net $%+8.2f' % (
            d, x['n'], x['won'], x['usd'], x['gas'], x['net']) for d, x in R['daily'].items()]
    if sub == 'copy':
        return L.grid(addrs[0] if addrs else L.leaders(c)[0], opt('--days', 7.0))
    if sub == 'portfolio':
        return L.portfolio(addrs or L.leaders(c), opt('--days', 14.0), opt('--size', 25.0), opt('--capital', 100.0))
    if sub == 'find':
        from copydesk import lpfind
        import contextlib, io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            lpfind.main(a[1:] or ['board'])
        return dict(ok=True), buf.getvalue().rstrip().split('\n')
    if sub == 'verify':
        from copydesk import lpdb
        con = lpdb.connect()
        owners = addrs or [r[0] for r in con.execute('SELECT owner FROM cover')]
        lines, bad = [], 0
        for o in owners:
            good, ls = lpdb.verify(o)
            bad += not good
            lines += ['%s  %s' % (o, 'SOLID' if good else 'NOT SOLID')] + ['   ' + x for x in ls]
        return dict(checked=len(owners), not_solid=bad), lines
    if sub == 'card':
        # the hourly Telegram card (cron): every open copy, and whether each leader still holds; silent with none open
        st = L.status(c)
        if not st['open'] and '--always' not in a:
            return dict(sent=False, open=0), ['no open copies: no card']
        try:
            c.refresh_eth_usd()
        except Exception:
            pass
        L.Follow(c, go=True, log=log, note=_lp_note(c)).card('Hourly update', with_check=True)
        return dict(sent=True, open=len(st['open'])), ['card sent: %d open cop%s' % (len(st['open']), 'y' if len(st['open']) == 1 else 'ies')]
    if sub == 'tune':
        # the blind fold tests behind the desk's settings, re-run on today's data; proposes, never applies (cron Mondays)
        from copydesk import lpopt
        out = []
        if a[1:2] == ['squad']:                                     # re-pick the squad every H hours, walk forward: which H?
            return dict(), lpopt.squad_walk(c, log=lambda m: None, days=opt('--days', 3.0))
        if a[1:2] == ['recency']:                                   # does a shorter / fading score window predict tomorrow better?
            return dict(), lpopt.recency(c, log=lambda m: None, folds=opt('--folds', 5))
        if a[1:2] == ['downside']:                                  # downside rules that should leave the upside alone
            return dict(), lpopt.downside(c, days=opt('--days', 3.0), log=lambda m: None) or ['the history store does not cover that yet']
        if a[1:2] == ['trail']:                                     # a trailing stop of our own: which level, if any?
            return dict(), lpopt.trail(c, days=opt('--days', 3.0), log=lambda m: None) or ['the history store does not cover that yet']
        only = set(opt('--only', '').split(',')) - {''} or None      # lp tune --only per_token,slots
        better, _ = lpopt.tune(c, log=out.append, folds=opt('--folds', 5), only=only)
        if better and '--notify' in a:
            _lp_note(c)('lp tune: ' + '; '.join(better))
        return dict(better=better), out
    if sub == 'qc':
        # quality control before a wallet joins the squad (copydesk/lpqc): addresses, or every new wallet over the filler bar
        from copydesk import lpqc
        rows, size = lpqc.quality(c, addrs, hours=opt('--hours', 24.0))
        return dict(rows=[dict(q, address=x) for x, q in rows]), lpqc.lines(rows, size)
    if sub == 'repick':
        # cron every 6 h: replay what closed since, then pick --apply (QC inside, at most pick_swaps bench swaps)
        from copydesk import lpfind
        out = []
        msg, changed, d = lpfind.repick(c, log=out.append, dry='--dry' in a)
        if d:
            from core import fmt
            _lp_note(c).card(fmt.squad_card(d))
        return dict(changed=changed), out
    if sub == 'nightly':
        # cron 03:00 UTC: discover -> replay the whole store -> pick --apply -> knobs; one Telegram note
        from copydesk import lpfind
        out = []
        msg, changed = lpfind.nightly(c, log=out.append, dry='--dry' in a)
        if '--dry' not in a:
            _lp_note(c)(msg)
            if getattr(c, 'squad', None):
                from core import fmt
                _lp_note(c).card(fmt.squad_card(c.squad))
        return dict(changed=changed), out
    if sub == 'pick':
        # main leaders (optimizer, out of sample) + bench (replayed copy $/day); --apply rewrites [lp.follow]
        from copydesk import lpopt
        out = []
        lead, bench, changed = lpopt.pick(c, apply='--apply' in a, log=out.append)
        if changed and '--notify' in a:
            _lp_note(c)('lp pick%s: %s' % (' (applied)' if '--apply' in a else '', ' · '.join(out[:3])))
        return dict(leaders=lead, bench=bench, changed=changed), out
    if sub == 'optimize':
        from copydesk import lpopt
        cfg = L.cfg(c)
        res = lpopt.optimize(c, n=opt('--n', 12), k=opt('--k', 3), days=opt('--days', 14.0), size=L.live_size(c)[0],
                             cap=L.live_size(c)[1], refresh='--no-refresh' not in a, current=L.leaders(c), log=log,
                             replay=opt('--replay', 0))
        out = lpopt.lines(res)
        if '--notify' in a and res['best'] and sorted(res['best']['leaders']) != sorted(L.leaders(c)):
            _lp_note(c)('lp optimize: follow %s instead of %s? out of sample %+.2f vs %+.2f over %g days (lpdesk optimize)' % (
                ' + '.join(x[:8] for x in res['best']['leaders']), ' + '.join(x[:8] for x in L.leaders(c)), res['best']['test']['pnl'],
                (res['current'] or {}).get('test', {}).get('pnl', 0.0), res['days'] / 2))
        return {k: v for k, v in res.items() if k != 'top'}, out
    if sub == 'trace':
        return _lp_trace(c, a[1:], f)
    return dict(error='unknown command %r' % sub, ok=False), ['unknown command %r - `./lpdesk help` lists them' % sub]


def _lp_log(c, a, f):
    """lp log [N | <leader tokenId> | <SYM> | <0x tx> | <0x wallet> | skips [hours] | errors [hours]] (owner 2026-10-02):
    the copy desk's own records (var/lp/follow.jsonl, mirrored from the desk's box), organised by copy. Offline.
      lp log              the last 15 copies, one line each: opened, closed, why, P&L, exit venue
      lp log 3529239      one copy's whole life: every record of that leader position, every tx with its link
      lp log MOONLET      every copy of a token          lp log 0x… (42)  every copy of that leader
      lp log 0x… (66)     the copy that sent that tx     lp log skips 24  why copies were refused, by code, tier and leader"""
    from copydesk import lpfollow as L
    import collections
    rows = []
    try:
        with open(L._log_path(c)) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        return dict(error='no follow.jsonl'), ['no records at %s' % L._log_path(c)]
    out, ex = [], getattr(c, 'explorer', '')
    hm = lambda t: time.strftime('%m-%d %H:%M', time.gmtime(t))
    link = lambda h: '%s/tx/%s' % (ex, h) if ex else h
    key = a[0] if a else ''
    hours = float(a[1]) if len(a) > 1 and a[1].replace('.', '').isdigit() else 24.0
    if key in ('skips', 'errors'):
        since = time.time() - hours * 3600
        sel = [r for r in rows if r.get('ev') == key[:-1] and r.get('ts', 0) >= since]
        if key == 'errors':
            for r in sel[-40:]:
                out.append('%s #%s %-8s %s' % (hm(r['ts']), r.get('leader_tid', '-'), r.get('at', ''), r.get('why')))
            return dict(errors=sel), out or ['no errors in %g h' % hours]
        by = collections.Counter(r.get('code') or L.skip_code(r.get('why')) for r in sel)
        who = collections.Counter((r.get('leader') or '?')[:10] for r in sel)
        out.append('%d skips in %g h' % (len(sel), hours))
        out += ['  %5d  %s' % (n, k) for k, n in by.most_common()]
        out.append('by leader: ' + ', '.join('%s %d' % (k, n) for k, n in who.most_common(8)))
        return dict(codes=dict(by), leaders=dict(who)), out
    tid = lambda r: str(r.get('leader_tid'))
    if not key or key.isdigit() and len(key) < 4:            # the last N copies
        n = int(key or 15)
        opens = {tid(r): r for r in rows if r.get('ev') == 'open'}
        closes = {tid(r): r for r in rows if r.get('ev') == 'close'}
        last = sorted(opens.values(), key=lambda r: r['ts'])[-n:]
        for o in last:
            cl = closes.get(tid(o))
            e = (cl or {}).get('exit') or {}
            out.append('%s #%s %-9s %-6s $%7.2f  %s' % (hm(o['ts']), tid(o), (o.get('sym') or '')[:9], o.get('tier') or '', o.get('size_usd') or 0,
                       ('-> %s %+7.2f (%+.1f%%) %s · %s%s' % (hm(cl['ts']), cl.get('pnl_usd') or 0,
                                                              cl['pnl_pct'] if cl.get('pnl_pct') is not None else 100 * (cl.get('pnl_usd') or 0) / (o.get('size_usd') or 1),
                                                              '%.0fm' % (cl.get('held_min') or 0), cl.get('why') or '-',
                                                              ' · sold via %s' % e['venue'] if e.get('venue') else '')) if cl else 'open'))
        return dict(copies=last), out
    k = key.lower()
    if k.isdigit():
        sel = [r for r in rows if tid(r) == k]
    elif k.startswith('0x') and len(k) == 66:
        hit = {tid(r) for r in rows if any(t.get('tx', '').lower() == k for t in r.get('txs') or [])}
        sel = [r for r in rows if tid(r) in hit]
    elif k.startswith('0x'):
        hit = {tid(r) for r in rows if (r.get('leader') or '').lower().startswith(k) and r.get('ev') in ('open', 'skip')}
        sel = [r for r in rows if tid(r) in hit and r.get('ev') in ('open', 'close', 'skip', 'error')]
    else:
        hit = {tid(r) for r in rows if (r.get('sym') or '').lower() == k and r.get('ev') == 'open'}
        sel = [r for r in rows if tid(r) in hit and r.get('ev') in ('open', 'close', 'error')]
    for r in sel[-200:]:
        ev = r.get('ev')
        if ev == 'open':
            out.append('%s #%s OPEN  %s %s $%.2f (planned $%s) beside leader %s $%.2f [%s] ticks %s' % (
                hm(r['ts']), tid(r), r.get('sym'), r.get('tier') or '', r.get('size_usd') or 0, r.get('planned_usd') or '-',
                (r.get('leader') or '?')[:10], r.get('leader_usd') or 0, 'bid' if r.get('quote_only') else 'range', r.get('ticks')))
        elif ev == 'close':
            e = r.get('exit') or {}
            out.append('%s #%s CLOSE %s back $%.2f pnl %+.2f (%s%%) after %s min · %s' % (
                hm(r['ts']), tid(r), r.get('sym'), r.get('back_usd') or 0, r.get('pnl_usd') or 0, r.get('pnl_pct'), r.get('held_min'), r.get('why') or '-'))
            if e:
                out.append('      exit via %s: best pool $%s (own pool $%s, %s quoted), Relay $%s in %s ms%s' % (
                    e.get('venue'), e.get('pool_usd'), e.get('own_pool_usd'), e.get('pools_quoted'), e.get('relay_usd'), e.get('relay_ms'),
                    ' · relay error: %s' % e['relay_error'] if e.get('relay_error') else ''))
        elif ev == 'route_shadow':                            # before 2026-10-02 the exit route was its own record
            out.append('      exit via %s: best pool $%s (own pool $%s), Relay $%s' % (r.get('venue') or 'pool', r.get('ours_usd'), r.get('own_pool_usd'), r.get('relay_usd')))
        else:
            out.append('%s #%s %-5s %s' % (hm(r['ts']), tid(r), ev.upper(), r.get('code') + ': ' if r.get('code') else '') + str(r.get('why') or ''))
        for t in r.get('txs') or []:
            out.append('      %-8s %s  gas $%s%s' % (t.get('action') or t.get('stage'), link(t.get('tx', '')) if t.get('tx') else '(refused: %s)' % t.get('revert'),
                                                  t.get('gas_usd', '-'), ' slip %s bps' % t['slip_bps'] if t.get('slip_bps') is not None else ''))
    return dict(records=sel), out or ['nothing for %s' % key]


def _lp_follow_status(c, f):
    """The copy desk at a glance: the record, each open copy checked against the chain (the leader still holds it? we
    still hold ours? its value now), and any mismatch as an ISSUE line."""
    import collections
    from copydesk import lpfollow as L
    r = L.status(c)
    rows, issues = L.check(c)
    st = json.load(open(L._state_path(c))) if os.path.exists(L._state_path(c)) else {}
    lead = ', '.join(L.leaders(c))
    lines = ['copy desk following %s: %d open, %d closed, realized $%+.2f, win %s, %s%s' % (
        lead, len(r['open']), len(r['closed']), r['realized'], '-' if r['win'] is None else '%.0f%%' % r['win'],
        'STOPPED' if r['stopped'] else 'running', '' if not st.get('last_leader_ts') else ', leader last opened %.1f h ago' % (
            (time.time() - st['last_leader_ts']) / 3600))]
    for x in rows:
        if not x['we_hold']:                                         # burned on chain, the mirror has not caught up (a few s)
            lines.append('  open  %-10s ours #%s $%.0f -> closing (burned on chain; the mirrored book catches up in seconds)' % (x['sym'], x['our_tid'], x['size']))
            continue
        lines.append('  open  %-10s ours #%s $%.0f -> $%.2f (%+.2f%%, fees $%.2f) %s %4.1fh | leader %s %s (%.0f%% of peak)%s' % (
            x['sym'], x['our_tid'], x['size'], x['value'], 100 * (x['value'] / x['size'] - 1), x['fees'],
            'in range' if x['in_range'] else 'OUT of range', x['held_h'], '%s #%s' % (x['leader'][:6], x['leader_tid']), 'still holds' if x['leader_holds'] else 'EXITED',
            x['leader_liq_pct'], ' (NFT moved to %s)' % x['leader_owner'] if x['leader_moved'] else ''))
    lines += ['  ISSUE ' + i for i in issues] or ['  check: every open copy matches the leader and the chain']
    lines += ['  closed leader #%s  %5.1f min  pnl $%+.3f (fees $%.3f)' % (o['leader_tid'], (o['exit_ts'] - o['entry_ts']) / 60, o['pnl_usd'], o['fees_usd'])
              for o in r['closed'][-(f.limit or 15):]]
    lines += ['  skips: ' + ', '.join('%s %d' % kv for kv in r['skips'].most_common())] if r['skips'] else []
    # THE EXIT SALES (owner 2026-10-03): where the token sides were sold, at what share of fair, what failed, the worst
    for lab, sel in (('24h', [x for x in r['sales'] if time.time() - (x[0].get('exit_ts') or 0) < 86400]), ('all', r['sales'])):
        if not sel:
            continue
        by = collections.Counter(s_['venue'] for _, s_ in sel)
        pc = sorted(s_['pct'] for _, s_ in sel if s_['pct'] is not None)
        fl = collections.Counter(k for _, s_ in sel for k in s_['fails'])
        lines.append('  exit sales %-3s %d: %s | median %s of fair | %d under %.0f%% | failed: %s' % (
            lab, len(sel), ', '.join('%s %d' % kv for kv in by.most_common()), '%.0f%%' % (100 * pc[len(pc) // 2]) if pc else '-',
            sum(1 for p_ in pc if p_ < L.SALE_ALARM), 100 * L.SALE_ALARM, ', '.join('%s %d' % kv for kv in fl.most_common()) or 'none'))
    for o, s_ in sorted([x for x in r['sales'] if x[1]['bad']], key=lambda x: -(x[0].get('exit_ts') or 0))[:f.limit or 8]:
        lines.append('    %s %-8s %s' % (time.strftime('%m-%d %H:%M', time.gmtime(o.get('exit_ts') or 0)), (o.get('symbol') or '')[:8], L.sale_line(s_)))
    r = dict(r, sales=[dict(leader_tid=o['leader_tid'], sym=o.get('symbol'), exit_ts=o.get('exit_ts'), **s_) for o, s_ in r['sales']])
    return dict(r, skips=dict(r['skips']), check=rows, issues=issues), lines


def main(argv=None):
    f, argv = parse_flags(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    sub = argv[0]
    import contextlib
    try:
        # THE --json CONTRACT: stdout is the JSON document and nothing else; a send path's prints go to stderr
        with contextlib.redirect_stdout(sys.stderr) if f.json else contextlib.nullcontext():
            if sub in ('init', 'doctor', 'smoke'):
                data, lines = {'init': cmd_init, 'doctor': cmd_doctor, 'smoke': cmd_smoke}[sub](f.chain, f)
            else:
                data, lines = _lp_desk(_load(f.chain), sub, argv, f)
    except SystemExit:
        raise
    except FileNotFoundError as e:                          # a missing wallet / config / store: say which, not a traceback
        msg = 'missing %s - `./lpdesk doctor` lists what the desk still needs' % (e.filename or e)
        print(json.dumps(dict(ok=False, error=msg)) if f.json else msg)
        return 1
    except Exception as e:
        if f.json:
            print(json.dumps(dict(ok=False, error=str(e)[:400]), indent=1))
            return 1
        raise
    if f.json:
        print(json.dumps(data, indent=1, default=str))
    else:
        for l in lines:
            print(l)
    return 1 if isinstance(data, dict) and data.get('ok') is False else 0


if __name__ == '__main__':
    sys.exit(main())
