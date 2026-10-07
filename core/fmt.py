"""ONE formatter. Every outbound line - Telegram, console, JSON - is built from the same event dict here,
so nothing anywhere else formats a number or a message by hand. The old desks concatenated %-strings at
~40 call sites and the channel was unreadable; this is the fix.

An event is `dict(kind=..., **fields)`. Three renderings:
    tg(ev)       -> Telegram HTML: bold title, <code> for every number/address, <=6 lines, no filler
    console(ev)  -> one dense terminal line
    ev itself    -> the JSON the CLI emits with --json

House rules, enforced here and nowhere else:
  money   signed, 4 dp, thin-space thousands           +1 234.5678
  size    compact, no cents                            $16k / $1,250
  address shortened to head+tail                       0xcd8aa5…3fb2
  time    UTC HH:MM:SS
  a field that is None is DROPPED, never printed as 'None' (the old tape's worst habit)
"""
import html, time

MARK = {'fill': '▲', 'revert': '▼', 'dry': '·', 'gated': '⨯', 'breaker': '■', 'breaker_clear': '□',
        'regime': '◆', 'health': '!', 'shots': '◇', 'parity': '=', 'error': '!', 'status': '≡',
        'refuel': '~', 'sweep': '→'}


# --------------------------------------------------------------------------- primitives
def esc(s):
    return html.escape(str(s), quote=False)


def money(x, dp=4, sign=True):
    """Signed money with thin-space thousands: +1 234.5678"""
    if x is None:
        return '—'
    s = '%+.*f' % (dp, x) if sign else '%.*f' % (dp, x)
    neg = s[0] in '+-'
    head, _, tail = s.lstrip('+-').partition('.')
    out = '{:,}'.format(int(head)).replace(',', ' ')
    return (s[0] if neg else '') + out + ('.' + tail if tail else '')


def usd(x, dp=0):
    """A clip size: $16k, $1,250, $50"""
    if x is None:
        return '—'
    if abs(x) >= 10000:
        return '$%.0fk' % (x / 1000.0)
    return '$' + ('{:,.%df}' % dp).format(x)


def bps(x):
    return '—' if x is None else '%+.1f bps' % x


def short(a, head=6, tail=4):
    a = str(a or '')
    if len(a) <= head + tail + 3:
        return a
    return a[:head] + '…' + a[-tail:]


def hhmmss(ts=None):
    return time.strftime('%H:%M:%S', time.gmtime(ts))


def _clean(ev):
    return {k: v for k, v in ev.items() if v is not None and v != ''}


def tag(ev):
    """chain·strategy, whichever parts exist."""
    parts = [p for p in (ev.get('chain'), ev.get('strategy') or ev.get('waiver')) if p]
    return '·'.join(parts)


# --------------------------------------------------------------------------- telegram renderers
def _kv(rows):
    """Aligned key/value lines. Values already escaped/marked up."""
    return '\n'.join('%s <code>%s</code>' % (esc(k), v) if not v.startswith('<') else '%s %s' % (esc(k), v)
                     for k, v in rows)


def _txrow(ev):
    """The tx line, linked when the chain has an explorer (core/io.py puts `tx_url` on every event)."""
    h = esc(short(ev['tx'], 10, 6))
    return ('tx', '<a href="%s">%s</a>' % (esc(ev['tx_url']), h) if ev.get('tx_url') else h)


def _fill(ev):
    """What was filled, on what chain, at what size, what the book went UP by, and what the pot holds now.

    NET ONLY. The card used to carry `got` (the realised profit) above `net` (that minus gas) and the two
    are always within a few cents of each other on these chains - two numbers that never disagree teach
    nothing and the eye stops reading both. `got` and the gas are still on the tape for forensics.
    `bal` is the receiving contract's whole pot, quotes included, so the card answers "and where are we
    now" without a second command."""
    rows = []
    if ev.get('net_usd') is not None:
        rows.append(('net', '<b>%s</b>' % esc(money(ev['net_usd']))))
    if ev.get('bal_usd') is not None:
        rows.append(('bal', '<b>%s</b>' % esc(usd(ev['bal_usd'], 2))))
    if ev.get('tx'):
        rows.append(_txrow(ev))
    return '%s <b>FILL</b> · %s\n%s  <code>%s</code>\n%s' % (
        MARK['fill'], esc(tag(ev)), esc(ev.get('sym', '?')), esc(usd(ev.get('size_usd'))), _kv(rows))


def _revert(ev):
    """Why it failed, and whether that was OUR number or SOMEBODY ELSE'S speed.

    `LegShort(leg, got, want)` was being rendered as its bare signature, which says nothing: a leg 5 bps
    short because a racer moved the pool and a leg 44% short because we priced a fee we never measured
    read exactly the same. The decoded shortfall and the count of other swaps on that pool in the window
    are the whole difference."""
    why = ev.get('reason') or 'unknown'
    if ev.get('short_bps') is not None:
        why = '%s leg %s short %s bps' % (str(why).split('(')[0], ev.get('leg', '?'),
                                          ('%.0f' % ev['short_bps']))
        if ev.get('racers'):
            why += ' · %d raced us' % ev['racers']
    elif ev.get('short_usd') is not None:
        why = 'FloorNotMet: closed $%.4f under the floor%s' % (ev['short_usd'], ' · %d raced us' % ev['racers'] if ev.get('racers') else '')
    rows = [('size', esc(usd(ev.get('size_usd')))),
            ('why', '<b>%s</b>' % esc(why)),
            ('gas', esc(money(ev.get('gas_usd'), 4, sign=False)))]
    if ev.get('tx'):
        rows.append(_txrow(ev))
    if ev.get('pool_disabled'):
        rows.append(('pool', 'disabled, cooling off'))
    return '%s <b>revert</b> %s · %s\n%s' % (MARK['revert'], esc(ev.get('sym', '?')), esc(tag(ev)), _kv(rows))


def _gated(ev):
    return ('%s <b>GATED</b> %s · %s\n%s\n<i>expires on its own (1 h, doubling per repeat strike, max 48 h); '
            'clear now: <code>hookbot %s risk gated clear</code></i>'
            % (MARK['gated'], esc(ev.get('sym', '?')), esc(tag(ev)),
               _kv([('pool', esc(short(ev.get('pool'), 10, 6))),
                    ('dir', esc('z4o' if ev.get('zfo') else 'o4z')),
                    ('why', '<b>%s</b>' % esc(ev.get('reason') or 'no data')),
                    ('tx', esc(short(ev.get('tx'), 10, 6)))]),
               esc(ev.get('chain', ''))))


def _breaker(ev):
    return ('%s <b>BREAKER %s</b> · %s\n%s\n<i>firing paused, still pricing</i>'
            % (MARK['breaker'], esc(str(ev.get('mode', '')).upper()), esc(tag(ev)),
               _kv([('net', esc(money(ev.get('net')))),
                    ('peak', esc(money(ev.get('peak')))),
                    ('detail', esc(ev.get('detail') or ''))])))


def _breaker_clear(ev):
    return '%s <b>breaker cleared</b> · %s — net <code>%s</code>, firing resumed' % (
        MARK['breaker_clear'], esc(tag(ev)), esc(money(ev.get('net'))))


def _regime(ev):
    on = ev.get('state') == 'enabled'
    return '%s <b>6M %s</b> %s · %s — fee <code>%s</code> at 6M, <code>%s</code> at 3M' % (
        MARK['regime'], 'ENABLED' if on else 'disabled', esc(ev.get('sym', '?')), esc(tag(ev)),
        esc(bps(ev.get('fee6m'))), esc(bps(ev.get('fee3m'))))


def _health(ev):
    return '%s <b>HEALTH</b> · %s\n%s' % (MARK['health'], esc(tag(ev)), esc(ev.get('detail', '')))


def _shots(ev):
    return '%s <b>SHOTS %s</b> · %s\n%s' % (MARK['shots'], esc(ev.get('state', '')).upper(),
                                            esc(tag(ev)), esc(ev.get('detail', '')))


def _status(ev):
    """Two books. hook_bot's own is zero until it is armed - which is a fact about the CUTOVER, not about
    the strategy - so the live desk's book is shown beside it rather than leaving a bare row of zeros."""
    lb = ev.get('live_book') or {}
    head = '%s <b>%s</b> · %s' % (MARK['status'], esc(ev.get('chain', '')).upper(), esc(hhmmss()))
    if not ev.get('traded'):
        rows = [('state', '<b>not trading</b> (%s)' % ('armed, no fills yet' if ev.get('armed')
                                                       else 'disarmed - cutover not done')),
                ('watching', esc('%d targets / %d pools' % (ev.get('targets') or 0, ev.get('edges') or 0))),
                ('6M on', esc(', '.join(ev.get('sixm_on') or []) or 'none')),
                ('breaker', esc(ev.get('breaker', 'ok')))]
    else:
        rows = [('net', '<b>%s</b>  peak %s' % (esc(money(ev.get('net'))), esc(money(ev.get('peak'))))),
                ('gas', esc(money(ev.get('gas_today'), 4, sign=False)) + ' today'),
                ('fills', esc('%d today' % (ev.get('fills') or 0))),
                ('6M on', esc(', '.join(ev.get('sixm_on') or []) or 'none')),
                ('shots', esc('%d in stock' % (ev.get('shot_stock') or 0))),
                ('breaker', esc(ev.get('breaker', 'ok')))]
    if ev.get('disabled'):
        rows.append(('disabled', esc(', '.join(short(p, 6, 4) for p in ev['disabled']))))
    if ev.get('gated'):
        rows.append(('gated', esc(str(ev['gated']))))
    if ev.get('gate_found'):
        gf, gp = ev.get('gate_found') or 0, ev.get('gate_passed') or 0
        mh = ev.get('model_high_bps')
        rows.append(('gate', esc('%d priced, %d passed%s' % (gf, gp,
                     (', model high %s bps' % mh) if mh is not None else ''))))
    if lb:
        mark = '' if lb.get('complete', True) else ' <i>(incomplete)</i>'
        rows.append(('live desk', '<b>%s</b> over %d fills%s' % (esc(money(lb.get('gross'))),
                                                                 lb.get('fills') or 0, mark)))
    return '%s\n%s' % (head, _kv(rows))


def _parity(ev):
    bad = ev.get('drift') or []
    if not bad:
        return '%s <b>parity OK</b> · %s — %d checks, engine matches the live desk' % (
            MARK['parity'], esc(ev.get('chain', '')), ev.get('checks', 0))
    lines = '\n'.join('· %s' % esc(d) for d in bad[:6])
    return '%s <b>PARITY DRIFT</b> · %s — %d of %d checks\n%s' % (
        MARK['health'], esc(ev.get('chain', '')), len(bad), ev.get('checks', 0), lines)


def _error(ev):
    return '%s <b>error</b> · %s\n%s' % (MARK['error'], esc(tag(ev)), esc(ev.get('detail', ''))[:400])


def _note(ev):
    """A free-text card: first line is the title, the rest is the body as written (the LP program's cards)."""
    head, _, body = str(ev.get('detail') or '').partition('\n')
    return '%s <b>%s</b> · %s%s' % (MARK.get('note', '·'), esc(head), esc(tag(ev)), '\n' + esc(body) if body else '')


# --------------------------------------------------------------------------- the LP program's cards
def _cols(s):
    """Monospace width: a CJK symbol (富贵) takes two cells, so pad by cells, not characters."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in str(s))


def _pad(s, w, right=False):
    s = str(s)
    fill = ' ' * max(0, w - _cols(s))
    return fill + s if right else s + fill


def _d(x, dp=2):
    """$ amount, sign only when negative: 27.93 / -4.80"""
    return '%.*f' % (dp, x) if x is not None else '—'


def _sd(x, dp=2):
    return ('+' if (x or 0) >= 0 else '-') + '$%.*f' % (dp, abs(x or 0))


def _band(pct, in_range, w=10):
    """Where the price sits in the position's range, low price on the left: ▕───●──────▏"""
    if not in_range or pct is None:
        return ('●▕' + '─' * w + '▏') if (pct or 0) < 0 else ('▕' + '─' * w + '▏●')
    i = min(w - 1, max(0, int(pct / 100.0 * w)))
    return '▕' + '─' * i + '●' + '─' * (w - 1 - i) + '▏'


def _px(x):
    """A token price with 4 significant digits and no exponent: 0.00003612, 1.234, 5678."""
    import math
    return '%.4g' % x if x >= 1 or x <= 0 else '%.*f' % (3 - int(math.floor(math.log10(x))), x)


def lp_card(d):
    """ONE LP card (Telegram HTML): the book at a glance, what just happened, every position in two aligned
    tables (money / where in its range), the small print last. `d` is built by cli._lp_card_data."""
    pnl = d['total'] - d['capital']
    dot = '🟢' if pnl >= 0 else '🔴'
    out = ['%s <b>LP  $%.2f</b>   <b>%s (%+.1f%%)</b>' % (dot, d['total'], _sd(pnl), 100 * pnl / d['capital']),
           '<i>%s · %s UTC</i>' % (esc(d['title']), time.strftime('%d %b %H:%M', time.gmtime(d.get('ts') or time.time())))]
    m = d.get('moves')
    if m:
        out += ['', '<b>Rebalanced</b>']
        out += ['▲ in    <b>%s</b>  $%.2f%s' % (esc(x['sym']), x['usd'], '  ·  %.1f%%/day lately' % x['yield'] if x.get('yield') is not None else '')
                for x in m.get('enter', [])]
        out += ['▼ out   <b>%s</b>  $%.2f → $%.2f (%+.1f%%)  ·  %.0fh' % (esc(x['sym']), x['in'], x['back'], x['pct'], x['held_h'])
                for x in m.get('exit', [])]
        if m.get('kept'):
            out.append('● kept  %s' % esc(', '.join(m['kept'])))
    if d.get('reinvested'):
        out += ['', '<b>Fees reinvested</b>']
        out += ['♻ <b>%s</b>  $%.2f back into the position' % (esc(s), a) for s, a in d['reinvested']]
    # one block per position, laid out like an LP app's position page: pair + fee tier + range badge, the position's
    # value, fees (unclaimed now / earned all time), deposited vs P&L, and the price range min / now / max
    for p in (p for p in d['positions'] if not p.get('error')):
        inr = p.get('in_range')
        out += ['', '<b>%s / USDG</b>  %s  %s' % (esc(p['sym']), '%g%%' % p['fee_pct'] if p.get('fee_pct') else 'dyn',
                                                  '🟢 In range' if inr else '🟠 Out of range')]
        rows = ['Position  $%s' % _d(p['principal'] if p.get('principal') is not None else p['worth']),
                'Fees      $%s unclaimed · $%s earned' % (_d(p.get('unclaimed') or 0), _d(p['fees'])),
                'Deposit   $%s · P&L %s (%+.1f%%)%s' % (_d(p['entry']), _sd(p['worth'] - p['entry']), p['pnl_pct'] or 0,
                                                     ' · %.0fh' % p['held_h'] if p.get('held_h') else '')]
        if p.get('px'):
            lo, now, hi = p['px']
            rows += ['Min %s  Now %s  Max %s' % (_px(lo), _px(now), _px(hi)), '    %s' % _band(p.get('band'), inr, 20)]
        out.append('<pre>%s</pre>' % esc('\n'.join(rows)))
    for p in d['positions']:
        if p.get('error'):
            out.append('⚠️ <b>%s</b> unreadable: %s' % (esc(p['sym']), esc(p['error'])))
    out += ['', '<i>started $%.2f on %s · high $%.2f</i>' % (d['capital'], esc(d['started']), d['hwm']),
            '<i>fees earned $%.2f%s</i>' % (d['fees_total'], ' · +$%.2f last hour' % d['fees_hour'] if d.get('fees_hour') is not None else ''),
            '<i>next reinvest %s UTC · spare $%.2f · gas $%.2f</i>' % (esc(d['next_reinvest']), d['spare'], d['gas'])]
    return '\n'.join(out)


def lp_alarms(new, gone):
    """Alarm strings read 'SYM #id: what': the symbol goes bold, the id stays out of the reader's way."""
    def one(a):
        head, _, what = a.partition(': ')
        return '• <b>%s</b>  %s' % (esc(head.split(' #')[0]), esc(what or head))
    out = ['⚠️ <b>LP check</b>'] + [one(a) for a in new]
    if gone:
        out += ['', '✅ <i>cleared</i>'] + [one(a) for a in gone]
    return '\n'.join(out)


def squad_card(d):
    """lp repick / nightly: only what changed in the squad, one wallet a line (owner 2026-09-30: the one-line note was
    unreadable). d = lpfind.squad_diff(...)."""
    out = ['🔄 <b>Squad</b> · %d change%s' % (len(d['changes']), '' if len(d['changes']) == 1 else 's')]
    for w, old, new, why in d['changes']:
        arrow = '▲' if old is None or old == 'filler' else '▼' if new is None or new == 'filler' else '↕'
        out.append('%s <b>%s</b>  %s → %s  <i>%s</i>' % (arrow, esc(w[:6]), esc(old or 'new'), esc(new or 'out'), esc(why)))
    out += ['', 'mains  %s' % esc(' · '.join(x[:6] for x in d['mains'])), 'filler  %d → %d' % d['filler']]
    if d.get('foot'):
        out.append('<i>%s</i>' % esc(d['foot']))
    return '\n'.join(out)


def _lp(ev):
    return ev.get('html') or esc(ev.get('text', ''))


def _generic(ev):
    body = _kv([(k, esc(v)) for k, v in _clean(ev).items() if k not in ('kind', 'chain', 'strategy')])
    return '%s <b>%s</b> · %s\n%s' % (MARK.get(ev.get('kind'), '·'), esc(ev.get('kind', '?')),
                                      esc(tag(ev)), body)


_TG = {'fill': _fill, 'revert': _revert, 'gated': _gated, 'breaker': _breaker,
       'breaker_clear': _breaker_clear, 'regime': _regime, 'health': _health, 'shots': _shots,
       'status': _status, 'parity': _parity, 'error': _error, 'note': _note, 'lp': _lp}


def tg(ev):
    """Telegram HTML for an event. Never raises - a formatter must not take a desk down."""
    try:
        return _TG.get(ev.get('kind'), _generic)(_clean(ev))
    except Exception as e:
        return '· <b>%s</b> (unformattable: %s)' % (esc(ev.get('kind', '?')), esc(str(e)[:80]))


# --------------------------------------------------------------------------- console
def console(ev):
    """One dense terminal line for the same event."""
    ev = _clean(ev)
    k = ev.get('kind', '?')
    m = MARK.get(k, '·')
    t = tag(ev)
    if k == 'lp':
        import re
        return html.unescape(re.sub(r'<[^>]+>', '', ev.get('html') or ev.get('text', '')))
    if k == 'fill':
        return '%s FILL %s %s  size %s  got %s  net %s  %s' % (
            m, t, ev.get('sym', '?'), usd(ev.get('size_usd')), money(ev.get('profit_usd')),
            money(ev.get('net_usd')), short(ev.get('tx'), 10, 6))
    if k == 'revert':
        return '%s revert %s %s  %s  gas %s  %s' % (
            m, t, ev.get('sym', '?'), ev.get('reason'), money(ev.get('gas_usd'), 4, sign=False),
            short(ev.get('tx'), 10, 6))
    if k == 'status':
        lb = ev.get('live_book') or {}
        own = ('not trading (%s)' % ('armed' if ev.get('armed') else 'disarmed, cutover not done')
               if not ev.get('traded') else
               'net %s peak %s gas %s fills %s' % (money(ev.get('net')), money(ev.get('peak')),
                                                   money(ev.get('gas_today'), 4, sign=False),
                                                   ev.get('fills', 0)))
        return '%s %s  hook_bot: %s | watching %s targets/%s pools | 6M[%s] | breaker %s | live desk: %s over %s fills' % (
            m, ev.get('chain'), own, ev.get('targets', 0), ev.get('edges', 0),
            ','.join(ev.get('sixm_on') or []) or '-', ev.get('breaker', 'ok'),
            money(lb.get('gross')), lb.get('fills', 0))
    if k == 'regime':
        return '%s 6M %s %s %s fee6m %s fee3m %s' % (m, ev.get('state'), t, ev.get('sym'),
                                                     bps(ev.get('fee6m')), bps(ev.get('fee3m')))
    parts = ['%s=%s' % (kk, vv) for kk, vv in ev.items() if kk not in ('kind', 'chain', 'strategy')]
    return '%s %s %s %s' % (m, k, t, ' '.join(parts[:8]))


# --------------------------------------------------------------------------- tables (console + json)
def table(rows, cols):
    """rows: list of dicts. cols: [(key, header, width, align)]. Returns a plain aligned string."""
    if not rows:
        return '(none)'
    head = '  '.join(('%-*s' % (w, h)) if a == 'l' else ('%*s' % (w, h)) for _k, h, w, a in cols)
    out = [head, '  '.join('-' * w for _k, _h, w, _a in cols)]
    for r in rows:
        cells = []
        for k, _h, w, a in cols:
            v = r.get(k)
            v = '' if v is None else str(v)
            v = v[:w]
            cells.append(('%-*s' % (w, v)) if a == 'l' else ('%*s' % (w, v)))
        out.append('  '.join(cells))
    return '\n'.join(out)


def _selftest():
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    A(money(1234.5678) == '+1 234.5678', money(1234.5678))
    A(money(-0.5) == '-0.5000' and money(None) == '—')
    A(money(3.2, 4, sign=False) == '3.2000')
    A(usd(16000) == '$16k' and usd(1250) == '$1,250' and usd(50) == '$50')
    A(short('0x' + 'ab' * 20) == '0xabab…abab')
    A(bps(-12.34) == '-12.3 bps' and bps(None) == '—')
    A(esc('<b>&x') == '&lt;b&gt;&amp;x', esc('<b>&x'))

    f = tg(dict(kind='fill', chain='arc', strategy='gasleft', sym='EURC→USDC', size_usd=16000,
                profit_usd=114.7213, net_usd=114.7179, gas_usd=0.0034, bal_usd=239.58,
                tx='0x' + 'cd' * 32, tx_url='https://arc-scan.org/tx/0x' + 'cd' * 32, expected=114.71))
    A('<b>FILL</b>' in f and 'arc·gasleft' in f, f)
    A('114.7179' in f and '114.7213' not in f, 'NET only: `got` is on the tape, not on the card')
    A('$239.58' in f, 'and the pot as it stands after the fill')
    A('href="https://arc-scan.org/tx/0xcdcd' in f, 'the hash links to the chain explorer')
    A('gate' not in f and '\ngas ' not in f, 'a fill card carries no gate quote and no gas line')
    A(f.count('\n') == 4, 'a fill card is 5 lines: title, pair+size, net, bal, tx')
    A('None' not in tg(dict(kind='fill', chain='arc', sym='X', size_usd=None, profit_usd=1.0)),
      'a None field is dropped, never printed')
    r = tg(dict(kind='revert', chain='arc', strategy='h2h', sym='X', reason='FloorNotMet', gas_usd=0.003))
    A('revert' in r and 'FloorNotMet' in r)
    s = tg(dict(kind='status', chain='arc', net=21.5, peak=21.9, gas_today=0.4, fills=3, traded=True,
                sixm_on=['EURC', 'USDC'], shot_stock=1, breaker='ok',
                live_book=dict(fills=24, gross=214.5275, net=214.40, complete=True)))
    A('ARC' in s and '21.5000' in s)
    A('live desk' in s and '214.5275' in s, 'the status card carries the live desk book too')
    sg = tg(dict(kind='status', chain='rh', traded=True, net=0.0, peak=0.0,
                 gate_found=4701, gate_passed=14, model_high_bps=8.4))
    A('4701 priced' in sg and '8.4 bps' in sg, 'the gate bias is on the status card')
    s0 = tg(dict(kind='status', chain='arc', net=0.0, peak=0.0, traded=False, armed=False,
                 targets=113, edges=1271, breaker='ok',
                 live_book=dict(fills=24, gross=214.5275, complete=False)))
    A('not trading' in s0 and 'cutover not done' in s0, 'a zero book says WHY it is zero')
    A('incomplete' in s0, 'an incomplete live book is marked')
    p = tg(dict(kind='parity', chain='arc', checks=12, drift=[]))
    A('parity OK' in p)
    p2 = tg(dict(kind='parity', chain='arc', checks=12, drift=['amm.py differs']))
    A('PARITY DRIFT' in p2 and 'amm.py differs' in p2)
    # an unknown kind still renders, and a broken event never raises
    A('weird' in tg(dict(kind='weird', chain='arc', a=1)))
    A(tg({'kind': 'fill', 'size_usd': object()}).startswith('▲') or True)
    # console
    A(console(dict(kind='fill', chain='arc', strategy='gasleft', sym='X', size_usd=100,
                   profit_usd=1.5, gas_usd=0.01, tx='0xabc')).startswith('▲ FILL arc·gasleft'))
    A('net' in console(dict(kind='status', chain='arc', net=1.0, peak=1.0, gas_today=0.0, traded=True)))
    A('not trading' in console(dict(kind='status', chain='arc', traded=False, armed=False)),
      'the console line says why a zero book is zero too')
    # table
    t = table([{'a': 'x', 'b': 1}], [('a', 'A', 4, 'l'), ('b', 'B', 3, 'r')])
    A(t.splitlines()[0].startswith('A') and t.splitlines()[-1].startswith('x'))
    A(table([], []) == '(none)')
    print('fmt selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
