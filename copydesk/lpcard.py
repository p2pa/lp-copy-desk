"""The copy LP desk's Telegram card as ONE image (owner 2026-09-29: an overview, not a wall of text).

The image is ALWAYS the overview (owner 2026-09-29); what just happened (a copy opened or closed) is the caption's job.
render(d) -> PNG bytes. d = dict(total, pnl, pct, ts, open=[...], leaders=[...], spare, gas, fees):
  open:    dict(sym, inn, now, fees, age, in_range, band)   band = where the price sits in the range, 0-100 (None = unknown)
  leaders: dict(tag, role ('lead' | 'bench'), open, n, today, total) - only the wallets followed now
Font: Geist Sans for words, Geist Mono for numbers (aligned columns), in copydesk/fonts/ (SIL OFL 1.1, OFL.txt).
"""
import io, os

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')

W = 720
BG, FG, DIM, LINE = (17, 20, 26), (230, 233, 238), (132, 140, 153), (44, 50, 60)
GREEN, RED, AMBER = (60, 200, 120), (235, 87, 87), (240, 180, 60)


def _font(n, face='Geist-Regular'):
    from PIL import ImageFont
    return ImageFont.truetype(os.path.join(FONTS, face + '.ttf'), n)


def _money(x):
    return '$0.00' if abs(x) < 0.005 else ('%+.2f' % x).replace('+', '+$').replace('-', '-$')


def _spark(g, pts, box):
    """The desk's P&L over the last 24 h (top right, lpfollow.pnl_points - the same line as lp watch): a dashed zero line,
    the P&L line green above zero and red below, a dot at now."""
    if len(pts) < 2:
        return
    x0, y0, x1, y1 = box
    t0, t1 = pts[0][0], pts[-1][0]
    lo, hi = min([v for _, v in pts] + [0.0]), max([v for _, v in pts] + [0.0])
    hi = hi if hi > lo else lo + 1
    Y = lambda v: y1 - (y1 - y0) * (v - lo) / (hi - lo)
    xy = [(x0 + (x1 - x0) * (t - t0) / ((t1 - t0) or 1), Y(v)) for t, v in pts]
    c = GREEN if pts[-1][1] >= 0 else RED
    for x in range(int(x0), int(x1), 8):                                        # zero: break-even on the capital put in
        g.line([(x, Y(0)), (min(x + 4, x1), Y(0))], fill=DIM, width=1)
    g.line(xy, fill=c, width=2, joint='curve')
    g.ellipse((xy[-1][0] - 4, xy[-1][1] - 4, xy[-1][0] + 4, xy[-1][1] + 4), fill=c)
    f = _font(12, 'Geist-Medium')
    g.text((x0, y1 + 4), 'P&L 24h  hi %s  lo %s' % (_money(hi), _money(lo)), font=f, fill=DIM)
    g.text((x1, y1 + 4), 'now %s' % _money(pts[-1][1]), font=f, fill=c, anchor='ra')


def render(d):
    from PIL import Image, ImageDraw
    rows_o, rows_l = d.get('open') or [], d.get('leaders') or []
    H = 110 + (44 + 44 * len(rows_o) if rows_o else 0) + 44 + 34 * len(rows_l) + 36
    im = Image.new('RGB', (W, H), BG)
    g = ImageDraw.Draw(im)
    f13, f16, f20 = _font(14, 'Geist-Medium'), _font(17, 'GeistMono-Regular'), _font(20, 'Geist-Medium')
    m20, f36 = _font(20, 'GeistMono-Medium'), _font(40, 'Geist-SemiBold')
    up = d['pnl'] >= 0
    col = GREEN if up else RED

    # the book
    g.text((28, 22), '$%.2f' % d['total'], font=f36, fill=FG)
    g.text((28 + g.textlength('$%.2f' % d['total'], font=f36) + 16, 38), '%s  (%+.1f%%)' % (_money(d['pnl']), d['pct']),
           font=m20, fill=col)
    _spark(g, d.get('equity') or [], (430, 22, W - 28, 70))
    y = 88
    g.line((28, y, W - 28, y), fill=LINE, width=1)

    # open copies: symbol, in -> now, fees, age, and where the price sits in the range
    if rows_o:
        y += 14
        for x, t in ((28, 'OPEN'), (150, 'IN → NOW'), (340, 'FEES'), (420, 'AGE'), (500, 'RANGE')):
            g.text((x, y), t, font=f13, fill=DIM)
        y += 28
        for p in rows_o:
            pc = 100 * (p['now'] / p['inn'] - 1) if p['inn'] else 0
            # the pair, base/quote (VRAX/USDG, OFY/ETH): the base in full weight, the quote small and dim beside it
            q = '/' + p.get('quote', 'USDG')
            fq = _font(14, 'Geist-Medium')
            room = 116 - g.textlength(q, font=fq)
            sym = p['sym']
            while sym and g.textlength(sym, font=f20) > room:
                sym = sym[:-1]
            g.text((28, y), sym, font=f20, fill=FG)
            g.text((28 + g.textlength(sym, font=f20) + 1, y + 5), q, font=fq, fill=AMBER if q == '/ETH' else DIM)
            if p.get('side'):                                  # what it holds now: bid / ask / in range / fell or rose through
                g.text((28, y + 23), p['side'], font=_font(11, 'Geist-Medium'), fill=DIM)
            g.text((150, y), '%.0f → %.2f' % (p['inn'], p['now']), font=m20, fill=GREEN if pc >= 0 else RED)
            g.text((340, y + 3), '$%.2f' % p['fees'], font=f16, fill=FG)
            g.text((420, y + 3), p['age'], font=f16, fill=DIM)
            x0, x1, yy = 500, W - 40, y + 12
            g.line((x0, yy, x1, yy), fill=LINE, width=6)
            b = p.get('band')
            if b is not None:
                bx = x0 + (x1 - x0) * min(1.0, max(0.0, b / 100.0))
                c = GREEN if p['in_range'] else AMBER
                g.ellipse((bx - 7, yy - 7, bx + 7, yy + 7), fill=c)
            y += 44
        g.line((28, y + 2, W - 28, y + 2), fill=LINE, width=1)

    # P&L by leader: a bar per leader, scaled to the largest total
    y += 16
    for x, t in ((28, 'LEADER'), (150, 'OPEN'), (215, 'COPIES'), (310, 'TODAY'), (420, 'TOTAL')):
        g.text((x, y), t, font=f13, fill=DIM)
    y += 28
    top = max([abs(r['total']) for r in rows_l] + [1e-9])
    for r in rows_l:
        bench = r.get('role') in ('bench', 'bench-all', 'fill-all')
        if r.get('role') in ('bench-all', 'fill-all'):
            if r['role'] == 'bench-all':
                g.line((28, y - 4, W - 28, y - 4), fill=LINE, width=1)
            g.text((28, y + 2), '%s ×%s' % ('bench' if r['role'] == 'bench-all' else 'filler', r['tag']), font=_font(16, 'Geist-Medium'), fill=DIM)
        else:
            g.text((28, y), r['tag'], font=m20, fill=DIM if bench else FG)
        if bench and r.get('role') not in ('bench-all', 'fill-all'):
            g.rounded_rectangle((84, y + 2, 140, y + 22), radius=5, outline=AMBER, width=1)
            g.text((112, y + 12), 'BENCH', font=_font(11, 'Geist-Medium'), fill=AMBER, anchor='mm')
        g.text((150, y), str(r['open']), font=m20, fill=FG)
        g.text((215, y), str(r['n']), font=m20, fill=FG)
        g.text((310, y), _money(r['today']), font=m20, fill=GREEN if r['today'] > 0 else RED if r['today'] < 0 else DIM)
        g.text((420, y), _money(r['total']), font=m20, fill=GREEN if r['total'] > 0 else RED if r['total'] < 0 else DIM)
        bw = (W - 40 - 540) * abs(r['total']) / top
        if bw >= 1:
            g.rectangle((540, y + 6, 540 + bw, y + 20), fill=GREEN if r['total'] > 0 else RED)
        y += 34

    g.text((28, H - 36), 'spare $%.0f  ·  gas left $%.2f  ·  fees $%.0f gross, net of price moves %s' % (d['spare'], d['gas'], d['fees'],
                                                                                   ('+$%.0f' if d.get('net', 0) >= 0 else '-$%.0f') % abs(d.get('net', 0))),
           font=f13, fill=DIM)
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def _selftest():
    d = dict(total=227.37, pnl=32.01, pct=16.4, equity=[(0, -4.6), (40000, -10.0), (90000, 10.0), (150000, 27.4)],
             open=[dict(sym='DARK', inn=25, now=23.44, fees=0.92, age='5h', in_range=True, band=62),
                   dict(sym='SUPERLONGNAME', quote='ETH', side='in range', inn=73, now=80.1, fees=2.2, age='12m', in_range=True, band=40),
                   dict(sym='DOTS', inn=48, now=36.8, fees=0.1, age='4m', in_range=False, band=104)],
             leaders=[dict(tag='12c2', role='lead', open=0, n=18, today=12.93, total=12.93),
                      dict(tag='0708', role='lead', open=0, n=0, today=0, total=0),
                      dict(tag='de88', role='bench', open=1, n=4, today=1.2, total=1.2),
                      dict(tag='40', role='bench-all', open=1, n=31, today=4.1, total=9.8),
                      dict(tag='27', role='fill-all', open=2, n=6, today=3.0, total=3.0)],
             spare=203, gas=7.46, fees=69.84)
    png = render(d)
    assert png[:8] == b'\x89PNG\r\n\x1a\n' and len(png) > 2000
    render(dict(d, open=[], leaders=[]))                                  # nothing open, no leaders: still an image
    print('lpcard selftest OK: %d bytes' % len(png))
    return png


if __name__ == '__main__':
    import sys
    png = _selftest()
    if len(sys.argv) > 1:
        open(sys.argv[1], 'wb').write(png)
