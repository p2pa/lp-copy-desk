"""hookbot <chain> lp watch: the copy desk full screen and live (owner 2026-09-30). The Telegram card, wider and realtime:
the book in big numbers, its P&L chart, every open copy against its range, every wallet we follow, one event feed.

REALTIME, not polled: one websocket (lpfollow.ws_urls: our node's when there is one, else the chain's public sockets) carries
  newHeads                         the block and the block rate
  PoolManager Swap on OUR pools    the copy in that pool is re-valued at once (its row flashes up or down)
  PositionManager Transfer         a followed wallet mints or burns: in the feed before the desk has even copied it
  USDG Transfer to / from lp-1     spare USDG and gas re-read
Re-valuing runs on its own thread, so a burst of swaps coalesces into one read and the screen never waits on a node. With
the socket down it falls back to a 10 s poll (header shows POLL) and reconnects. The desk's own files (var/lp/follow.json,
follow.jsonl, equity.jsonl; the mirror brings them from AWS within 5 s) are re-read when they change.

Times show in Europe/Amsterdam (LPWATCH_TZ overrides), specialist hours too; wallets are always tag(): 0x + 4 hex.
Read-only. q or ctrl-c closes it; s hides/shows skips; r switches the P&L chart between 24 h and 7 d."""
import asyncio, collections, curses, datetime, json, os, queue, re, threading, time, zoneinfo

from copydesk import lpfollow as L
from core.events import SWAP

USDG = '0x5fc5360d0400a0fd4f2af552add042d716f1d168'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
SPARK = '▁▂▃▄▅▆▇█'
TZ = zoneinfo.ZoneInfo(os.environ.get('LPWATCH_TZ', 'Europe/Amsterdam'))   # the owner's clock; the box runs UTC
BIT = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))       # braille dot (row, col) -> bit
FONT = {'0': ('█▀█', '█ █', '█▄█'), '1': ('▀█ ', ' █ ', '▄█▄'), '2': ('▀▀█', '█▀▀', '█▄▄'), '3': ('▀▀█', ' ▀█', '▄▄█'),
        '4': ('█ █', '▀▀█', '  █'), '5': ('█▀▀', '▀▀█', '▄▄█'), '6': ('█▀▀', '█▀█', '█▄█'), '7': ('▀▀█', '  █', '  █'),
        '8': ('█▀█', '█▀█', '█▄█'), '9': ('█▀█', '▀▀█', '▄▄█'), '.': (' ', ' ', '▄'), '$': ('▄▄', '█▄', '▄█'), ',': (' ', ' ', '▄')}


# --------------------------------------------------------------------------- pure helpers (selftested)
def _age(s):
    s = max(0, s)
    return '%ds' % s if s < 60 else '%dm' % (s / 60) if s < 3600 else '%.1fh' % (s / 3600) if s < 172800 else '%dd' % (s / 86400)


def clock(ts, f='%H:%M:%S'):
    return datetime.datetime.fromtimestamp(ts, TZ).strftime(f)


def tag(a):
    """THE wallet label, the same in every panel: 0x + 4 hex."""
    return (a or '')[:6].lower()


def big(text):
    """Three rows of block glyphs for a number like '$289.08'."""
    g = [FONT.get(ch, ('   ', '   ', '   ')) for ch in text]
    return [' '.join(x[r] for x in g) for r in range(3)]


def spark(vals, n):
    v = list(vals)[-n:]
    if len(v) < 2:
        return ''
    lo, hi = min(v), max(v)
    return ''.join(SPARK[int((x - lo) / (hi - lo) * 7)] if hi > lo else SPARK[3] for x in v)


def braille(vals, w, h, base=None):
    """A w x h cell braille line chart of vals (2 x 4 dots a cell). -> rows, lo, hi, the cell row of `base` (or None)."""
    if len(vals) < 2:
        return [' ' * w] * h, None, None, None
    X, Y = 2 * w, 4 * h
    pts = [vals[min(len(vals) - 1, int((i + 1) * len(vals) / X) - 1)] for i in range(X)]
    lo, hi = min(pts + ([base] if base is not None else [])), max(pts + ([base] if base is not None else []))
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    ydot = lambda v: min(Y - 1, max(0, int(round((hi - v) / (hi - lo) * (Y - 1)))))
    grid = [[0] * w for _ in range(h)]
    prev = None
    for x, v in enumerate(pts):
        y = ydot(v)
        a, b = (y, y) if prev is None else sorted((prev, y))
        for r in range(a, b + 1):
            grid[r // 4][x // 2] |= BIT[r % 4][x % 2]
        prev = y
    rows = [''.join(chr(0x2800 + c) if c else ' ' for c in row) for row in grid]
    return rows, lo + pad, hi - pad, (ydot(base) // 4 if base is not None else None)


def _tail(path, n=400):
    """The last n rows of a jsonl file (reads only its end)."""
    if not os.path.exists(path):
        return []
    with open(path, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 300 * n))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    out = []
    for l in lines[1 if size > 300 * n else 0:][-n:]:
        try:
            out.append(json.loads(l, strict=False))
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------- the chain, kept live by the node's socket
class Live:
    def __init__(self, c, addr, watch):
        self.c, self.addr, self.watch = c, addr.lower(), watch
        self.lock = threading.Lock()
        self.vals, self.hist, self.flash = {}, collections.defaultdict(lambda: collections.deque(maxlen=60)), {}
        self.spare = self.gas = self.block = None
        self.heads = collections.deque(maxlen=40)
        self.ws, self.err, self.at = False, None, 0.0
        self.feed = collections.deque(maxlen=100)                     # chain events: followed wallets' mints and burns
        self.bts = {}                                                 # block -> its timestamp (newHeads)
        self.opn, self.pools, self.resub = [], {}, False
        self.want = queue.Queue()

    def set_open(self, opn):
        with self.lock:
            self.opn = list(opn)
            pools = {}
            for o in opn:
                pools.setdefault(o['pool'].lower(), []).append(o)
            self.resub = set(pools) != set(self.pools)
            self.pools = pools
        self.want.put('all')

    def start(self):
        threading.Thread(target=self._valuer, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(self._socket()), daemon=True).start()

    # ---- re-valuing (its own thread: a burst of swaps is one read)
    def _valuer(self):
        try:
            self.c.refresh_eth_usd()
        except Exception:
            pass
        while True:
            try:
                got = {self.want.get(timeout=2.0 if self.ws else 10.0)}
            except queue.Empty:
                got = {'all'}                                         # the heartbeat: a missed log is repaired here
            time.sleep(0.1)
            while not self.want.empty():
                got.add(self.want.get())
            now_ = time.time()
            try:
                with self.lock:
                    opn, pools = list(self.opn), dict(self.pools)
                todo = opn if 'all' in got else [o for p in got if p in pools for o in pools[p]]
                vals = L.value_of(None, todo) if todo else {}
                # THE BALANCE MOVES WITH THE BOOK (owner 2026-10-01: the total dipped by a copy's size before each open): it was
                # re-read the moment our USDG moved, while the book that holds the new copy arrives through the mirror up to
                # 5 s later. Now: when the book file changes ('bal', from load), and every 60 s for the treasury's own moves
                if 'bal' in got or now_ - getattr(self, '_bal_t', 0) > 60:
                    self._bal_t = now_
                    r = L._batch([L._call(USDG, '0x70a08231' + self.addr[2:].rjust(64, '0')), ('eth_getBalance', [self.addr, 'latest'])])
                    spare, gas = int(r[0], 16) / 1e6, int(r[1], 16) / 1e18 * self.c.eth_usd
                else:
                    spare, gas = self.spare, self.gas
                now = time.time()
                with self.lock:
                    for t, v in vals.items():
                        w = v['principal'] + v['fees']
                        old = self.vals.get(t)
                        if old and abs(old['principal'] + old['fees'] - w) > 1e-4:
                            self.flash[t] = (now, 1 if w > old['principal'] + old['fees'] else -1)
                        self.vals[t] = v
                        self.hist[t].append(w)
                    self.spare, self.gas, self.at, self.err = spare, gas, now, None
            except Exception as e:
                self.err = str(e)[:80]

    # ---- the socket
    async def _socket(self):
        import websockets
        pm = self.c.pm.lower()
        wait, urls, cur = 2, L.ws_urls(self.c), 0
        while True:
            self.url = urls[cur % len(urls)]
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=None, open_timeout=10) as ws:
                    ids, tags, n = {}, {}, [0]

                    async def sub(tag, params):
                        n[0] += 1
                        tags[n[0]] = tag
                        await ws.send(json.dumps(dict(jsonrpc='2.0', id=n[0], method='eth_subscribe', params=params)))

                    a32 = '0x' + self.addr[2:].rjust(64, '0')
                    await sub('head', ['newHeads'])
                    await sub('nft', ['logs', dict(address=L.POSM, topics=[TRANSFER])])
                    await sub('usdg', ['logs', dict(address=USDG, topics=[TRANSFER, a32])])
                    await sub('usdg', ['logs', dict(address=USDG, topics=[TRANSFER, None, a32])])
                    swap_id = [None, True]                                    # (subscription id, needs (re)subscribing)
                    self.ws, wait = True, 2
                    while True:
                        if self.resub or swap_id[1]:
                            self.resub, swap_id[1] = False, False
                            if swap_id[0]:
                                await ws.send(json.dumps(dict(jsonrpc='2.0', id=0, method='eth_unsubscribe', params=[swap_id[0]])))
                                ids.pop(swap_id[0], None)
                                swap_id[0] = None
                            with self.lock:
                                pids = list(self.pools)
                            if pids:
                                await sub('swap', ['logs', dict(address=pm, topics=[SWAP, pids])])
                        try:
                            m = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
                        except asyncio.TimeoutError:
                            continue
                        if 'id' in m and m.get('id') in tags and isinstance(m.get('result'), str):
                            ids[m['result']] = tags[m['id']]
                            if tags[m['id']] == 'swap':
                                swap_id[0] = m['result']
                            continue
                        p = m.get('params') or {}
                        tag, r = ids.get(p.get('subscription')), p.get('result') or {}
                        if tag == 'head':
                            with self.lock:
                                self.block = int(r['number'], 16)
                                self.heads.append(time.time())
                                self.bts[self.block] = int(r['timestamp'], 16)
                                if len(self.bts) > 600:
                                    for b in sorted(self.bts)[:300]:
                                        self.bts.pop(b)
                        elif tag == 'swap':
                            self.want.put(r['topics'][1].lower())
                        elif tag == 'usdg':
                            pass                    # the balance is re-read with the book (load), never ahead of it
                        elif tag == 'nft' and len(r.get('topics', [])) == 4:
                            frm, to, tid = '0x' + r['topics'][1][-40:], '0x' + r['topics'][2][-40:], int(r['topics'][3], 16)
                            # AT THE BLOCK'S TIME, not when we heard it: the desk decides on the same mint ~0.3 s before home
                            # hears it, so a mint stamped on arrival sorted after the skip it caused (owner 2026-09-30)
                            bt = self.bts.get(int(r['blockNumber'], 16), int(time.time()))
                            if self.addr in (frm, to):
                                self.want.put('all')
                            elif int(frm, 16) == 0 and to in self.watch:
                                self.feed.append(dict(ts=bt, ev='chain', what='mint', who=to, tid=tid))
                            elif int(to, 16) == 0 and frm in self.watch:
                                self.feed.append(dict(ts=bt, ev='chain', what='burn', who=frm, tid=tid))
            except Exception as e:
                self.ws, self.err = False, 'socket: %s' % str(e)[:60]
                cur += 1                                               # the next socket in the list
                await asyncio.sleep(wait)
                wait = min(30, wait * 2)

    def rate(self):
        with self.lock:
            h = list(self.heads)
        return (len(h) - 1) / (h[-1] - h[0]) if len(h) > 2 and h[-1] > h[0] else 0.0


# --------------------------------------------------------------------------- the screen
class Screen:
    def __init__(self, c):
        self.c, self.skips, self.days = c, True, 1
        self.d = L._dir(c)
        self.mt, self.cache = {}, {}
        w = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'wallets', 'wallets.json')))
        self.addr = w[c.cfg['lp']['wallet']]['address']
        self.live = Live(c, self.addr, set())
        self.squad()

    def squad(self):
        """The squad from chain.toml, re-read when the file changes: repick rewrites it hourly, and a watch started before a
        change kept showing the old specialists (2026-10-01: 0x12c2 / 0xde88 hours after they were replaced)."""
        p = os.path.join(self.c.dir, 'chain.toml')
        m = os.path.getmtime(p)
        if getattr(self, '_squad_m', None) == m:
            return
        import tomllib
        self.c.cfg.setdefault('lp', {})['follow'] = tomllib.load(open(p, 'rb')).get('lp', {}).get('follow', {})
        self._squad_m = m
        self.leads, self.bench = L.leaders(self.c), L.bench(self.c)
        self.filler = [a for a in L.filler(self.c) if a not in self.leads and a not in self.bench]
        self.live.watch = set(self.leads + self.bench)

    # ---- files, re-read only when they change
    def _file(self, key, path, read):
        m = os.path.getmtime(path) if os.path.exists(path) else 0
        if self.mt.get(key) != m:
            self.mt[key], self.cache[key] = m, read(path) if m else None
        return self.cache[key]

    def load(self):
        self.squad()
        m0 = self.mt.get('st')
        st = self._file('st', L._state_path(self.c), lambda p: json.load(open(p))) or dict(ours={}, capital=0.0, realized=0.0)
        if self.mt.get('st') != m0:
            self.live.want.put('bal')                                  # the book changed: its cash with it
        evs = self._file('ev', L._log_path(self.c), lambda p: _tail(p, 400)) or []
        eq = self._file('eq', os.path.join(self.d, 'equity.jsonl'), lambda p: [(r['ts'], r['v']) for r in _tail(p, 12000) if 'v' in r]) or []
        opn = sorted((o for o in st['ours'].values() if not o.get('closed')), key=lambda o: o['entry_ts'])
        if [o.get('token_id') for o in opn] != [o.get('token_id') for o in self.live.opn]:
            self.live.set_open(opn)
        return st, opn, eq, evs

    # ---- drawing primitives
    def palette(self):
        curses.start_color()
        curses.use_default_colors()
        rich = curses.COLORS >= 256
        spec = dict(text=(252, 7), dim=(246, 7), faint=(240, 8 if curses.COLORS > 8 else 7), green=(114, 2), red=(203, 1),
                    yellow=(221, 3), cyan=(80, 6), blue=(111, 4), magenta=(176, 5), white=(255, 7))
        self.C = {}
        for i, (k, (a, b)) in enumerate(spec.items(), 1):
            curses.init_pair(i, a if rich else b, -1)
            self.C[k] = curses.color_pair(i) | (curses.A_DIM if k == 'faint' and not rich else 0)
        n = len(spec) + 1
        curses.init_pair(n, 252 if rich else 7, 236 if rich else 4)
        curses.init_pair(n + 1, 114 if rich else 2, 236 if rich else 4)
        curses.init_pair(n + 2, 203 if rich else 1, 236 if rich else 4)
        curses.init_pair(n + 3, 221 if rich else 3, 236 if rich else 4)
        self.C.update(bar=curses.color_pair(n), bar_green=curses.color_pair(n + 1) | curses.A_BOLD,
                      bar_red=curses.color_pair(n + 2) | curses.A_BOLD, bar_yellow=curses.color_pair(n + 3) | curses.A_BOLD)

    def put(self, y, x, s, a=0, lim=None):
        H, W = self.s.getmaxyx()
        n = min(W - x - (1 if y == H - 1 else 0), lim if lim is not None else 10 ** 6)
        if 0 <= y < H and 0 <= x < W and n > 0:
            try:
                self.s.addnstr(y, x, s, n, a)
            except curses.error:
                pass
        return x + min(len(s), max(n, 0))

    def segs(self, y, x, parts, lim=None):
        """One line from (text, attr) parts; lim = the cells it may use."""
        end = x + lim if lim else 10 ** 6
        for t, a in parts:
            if x >= end:
                break
            x = self.put(y, x, t, a, end - x)
        return x

    def box(self, y, x, h, w, title, right=''):
        f = self.C['faint']
        self.put(y, x, '╭' + '─' * (w - 2) + '╮', f)
        for r in range(1, h - 1):
            self.put(y + r, x, '│', f)
            self.put(y + r, x + w - 1, '│', f)
        self.put(y + h - 1, x, '╰' + '─' * (w - 2) + '╯', f)
        self.put(y, x + 2, ' %s ' % title, self.C['dim'] | curses.A_BOLD)
        if right:
            self.put(y, x + w - 3 - len(right), ' %s ' % right, self.C['dim'])

    def money(self, v, sign=False):
        return ('%+.2f' if sign else '%.2f') % v

    def pn(self, v):
        return self.C['green'] if v > 0.005 else self.C['red'] if v < -0.005 else self.C['dim']

    # ---- panels
    def header(self, W, run, now):
        lv = self.live
        B = self.C['bar']
        self.put(0, 0, ' ' * W, B)
        fresh = lv.ws and lv.heads and now - lv.heads[-1] < 3
        self.segs(0, 1, [(' ● LIVE ' if fresh else ' ◐ POLL ', self.C['bar_green'] if fresh else self.C['bar_yellow']),
                         ('  lp copy desk', B | curses.A_BOLD), ('  ·  %s  ·  ' % self.c.name, B),
                         (run, self.C['bar_green'] if run == 'copying' else self.C['bar_red'])])
        rt = 'block %s  ·  %.1f blk/s  ·  %s %s ' % ('{:,}'.format(lv.block) if lv.block else '-', lv.rate(), clock(now), clock(now, '%Z'))
        self.put(0, W - len(rt) - 1, rt, B)

    def book(self, y, x, w, st, opn, total, now, parts=None):
        self.box(y, x, 11, w, 'BOOK')
        C, lv, cap = self.C, self.live, st.get('capital') or 0.0
        if total is None:
            self.put(y + 3, x + 3, 'reading the chain...', C['dim'])
            return
        for r, line in enumerate(big('$%.2f' % total)):
            self.put(y + 1 + r, x + 3, line, C['white'] | curses.A_BOLD)
        d = total - cap
        self.segs(y + 1, x + 32, [('▲ ' if d >= 0 else '▼ ', self.pn(d)), (self.money(d, True), self.pn(d) | curses.A_BOLD)], w - 33)
        self.segs(y + 2, x + 32, [('%+.1f%%' % (100 * d / cap if cap else 0), self.pn(d))], w - 33)
        self.segs(y + 3, x + 32, [('since $%.0f' % cap, C['faint'])], w - 33)
        closed = [o for o in st['ours'].values() if o.get('closed')]
        today = time.strftime('%Y-%m-%d', time.gmtime(now))
        tod = [o for o in closed if time.strftime('%Y-%m-%d', time.gmtime(o.get('exit_ts') or 0)) == today]
        pt = sum(o.get('pnl_usd') or 0 for o in tod)
        won = sum(1 for o in tod if (o.get('pnl_usd') or 0) > 0)
        # WHAT THE BIG NUMBER IS MADE OF (owner 2026-09-30): one bar, each part in proportion, the legend adds up to it.
        # Gas is the ETH the desk keeps to sign with: shown beside, outside the book (the P&L is measured without it)
        if parts:
            bw = w - 6
            tot_ = sum(v for _, v, _ in parts) or 1.0
            n_, x_ = [max(1 if v > 0.005 else 0, int(round(bw * v / tot_))) for _, v, _ in parts], x + 3
            n_[max(range(len(n_)), key=lambda i: n_[i])] += bw - sum(n_)
            for (lab, v, col), k_ in zip(parts, n_):
                x_ = self.put(y + 5, x_, '█' * k_, C[col])
            leg = []
            for lab, v, col in parts:
                leg += [('■', C[col]), ('%s $%.0f ' % (lab, v), C['text'] if v > 0.005 else C['faint'])]
            self.segs(y + 6, x + 3, leg, w - 5)
        self.segs(y + 7, x + 3, [('today   ', C['faint']), ('%d closed  ' % len(tod), C['text']), ('%d won  ' % won, C['dim']),
                                 (self.money(pt, True), self.pn(pt) | curses.A_BOLD)], w - 5)
        k = L.cfg(self.c).get('max_open', 5)
        dep = sum(o['size_usd'] for o in opn)
        self.segs(y + 8, x + 3, [('slots   ', C['faint']), ('■' * min(len(opn), k), C['cyan']), ('□' * max(0, k - len(opn)), C['faint']),
                                 ('  %d/%d  ' % (len(opn), k), C['dim']), ('in $%.0f' % dep, C['text']), ('  spare $%.0f' % (lv.spare or 0), C['dim'])], w - 5)
        gas = lv.gas or 0
        low = gas < L.cfg(self.c).get('refuel_below_usd', 5.0)
        allw = sum(1 for o in closed if (o.get('pnl_usd') or 0) > 0)
        self.segs(y + 9, x + 3, [('gas ', C['faint']), ('$%.2f' % min(gas, float(L.cfg(self.c).get('gas_keep_usd', 15.0))), C['red'] if low else C['text']),
                                 (' refuel due' if low else ' outside book', C['red'] if low else C['faint']),
                                 ('  realized ', C['faint']), (self.money(st.get('realized', 0.0), True), self.pn(st.get('realized', 0.0))),
                                 ('  win %s' % ('%.0f%%' % (100 * allw / len(closed)) if closed else '-'), C['dim']),
                                 ('  TREASURY STOPPED' if self.treasury_stopped() else '', C['red'] | curses.A_BOLD)], w - 5)

    def treasury_stopped(self):
        from copydesk import treasury
        return self._file('tr', treasury.state_path(self.c), lambda p: json.load(open(p)).get('stopped'))

    def chart(self, y, x, w, st, eq, total, now):
        pts = [v for _, v in L.pnl_points(eq, st, total, self.days, now)]            # the same line as the Telegram card
        cw = w - 12
        rows, lo, hi, brow = braille(pts, cw, 8, 0.0)
        right = '' if lo is None else 'lo %+.0f  hi %+.0f  now %+.2f' % (lo, hi, pts[-1])
        self.box(y, x, 11, w, 'P&L · %s' % ('24H' if self.days == 1 else '7D'), right)
        if lo is None:
            self.put(y + 4, x + 3, 'no balance points yet', self.C['dim'])
            return
        up = pts[-1] >= 0
        for i, r in enumerate(rows):
            if i == brow:
                for j, ch in enumerate(r):
                    self.put(y + 1 + i, x + 2 + j, ch if ch != ' ' else '┈', self.C['faint'] if ch == ' ' else self.C['green' if up else 'red'])
            else:
                self.put(y + 1 + i, x + 2, r, self.C['green' if up else 'red'])
        self.put(y + 1, x + w - 9, '%+.0f' % hi, self.C['dim'])
        if brow not in (None, 0, 7):
            self.put(y + 1 + brow, x + w - 9, ' 0', self.C['faint'])
        self.put(y + 8, x + w - 9, '%+.0f' % lo, self.C['dim'])
        lbl, mid = ('-24h', '-12h') if self.days == 1 else ('-7d', '-3.5d')
        self.put(y + 9, x + 2, lbl, self.C['faint'])
        self.put(y + 9, x + 2 + cw // 2 - len(mid) // 2, mid, self.C['faint'])
        self.put(y + 9, x + 2 + cw - 3, 'now', self.C['faint'])

    def gauge(self, v, n=18):
        """The range as a track; ◆ is the price. Out of range: an arrow on the side it left by, and how far."""
        C = self.C
        if not v or v.get('band') is None:
            return [('·' * (n + 2), C['faint'])], None
        b, (lo, now, hi) = v['band'], v['px']
        if b < 0:
            return [('◀', C['yellow']), ('┄' * n, C['faint']), ('┤', C['faint'])], ('%+.1f%% under' % (100 * (now / lo - 1)), C['yellow'])
        if b > 100:
            return [('├', C['faint']), ('┄' * n, C['faint']), ('▶', C['yellow'])], ('%+.1f%% over' % (100 * (now / hi - 1)), C['yellow'])
        i = min(n - 1, int(b / 100 * n))
        return [('├', C['green']), ('─' * i, C['green']), ('◆', C['white'] | curses.A_BOLD), ('─' * (n - 1 - i), C['green']), ('┤', C['green'])], ('%.0f%% up' % b, C['dim'])

    def copies(self, y, x, w, opn, now):
        k = L.cfg(self.c).get('max_open', 5)
        h = max(1, len(opn)) + 3
        self.box(y, x, h, w, 'OPEN COPIES', '%d/%d' % (len(opn), k))
        C, lv = self.C, self.live
        cols = '  %-15s %5s  %-16s %-13s %8s   %8s   %9s %7s %7s %6s   %-20s %-13s %s' % (
            'PAIR', 'FEE', 'HOLDS', 'WALLET', 'IN', 'NOW', 'P&L', '', 'FEES', 'AGE', 'RANGE', '', 'TREND')
        self.put(y + 1, x + 2, cols, C['faint'], w - 4)
        if not opn:
            self.put(y + 2, x + 4, 'nothing open · waiting on the leaders', C['dim'])
            return h
        for i, o in enumerate(opn):
            with lv.lock:
                v = lv.vals.get(o.get('token_id'))
                hist = list(lv.hist.get(o.get('token_id')) or [])
                fl = lv.flash.get(o.get('token_id'))
            who = (o.get('leader') or '').lower()
            role = 'main' if who in self.leads else ('spec' if who in {k.lower() for k in L.cfg(self.c).get('hours') or {}} else 'bench') if who in self.bench else 'fill' if who in self.filler else 'old'
            wv = v['principal'] + v['fees'] if v else None
            sym, q = (o.get('symbol') or o['pool'][2:8])[:10], '/ETH' if o.get('quote') == 'eth' else '/USDG'
            hl = L.holds_label(v, o) if v else ''
            sc = C['green'] if '/' in hl else C['cyan'] if hl in ('USDG', 'ETH') else C['yellow']
            fp = int((o.get('key') or {}).get('fee') or 0)
            fee = 'dyn' if fp >= 0x800000 else ('%.2g%%' % (fp / 1e4))
            d = (wv - o['size_usd']) if v else 0.0
            hot = fl and now - fl[0] < 1.5
            track, dist = self.gauge(v)
            parts = [('●' if v and v.get('in_range') else '○', C['green'] if v and v.get('in_range') else C['yellow']),
                     (' ' + sym, C['white'] | curses.A_BOLD), (q, C['yellow'] if q == '/ETH' else C['dim']),
                     (' ' * max(1, 16 - len(sym) - len(q)), 0), ('%5s  ' % fee, C['dim']), ('%-16s ' % hl[:16], sc),
                     ('%-6s ' % tag(who), C['text']), ('%-6s ' % role, C['cyan'] if role == 'main' else C['dim']),
                     ('%8s' % self.money(o['size_usd']), C['dim']), (' → ', C['faint']),
                     ('%8s' % ('-' if wv is None else self.money(wv)), C['white'] | (curses.A_BOLD if hot else 0)),
                     ((' ▲' if fl[1] > 0 else ' ▼') if hot else '  ', C['green'] if hot and fl[1] > 0 else C['red']),
                     (' %9s' % ('-' if wv is None else self.money(d, True)), self.pn(d) | curses.A_BOLD),
                     (' %6s%%' % ('-' if wv is None else '%+.1f' % (100 * d / o['size_usd'])), self.pn(d)),
                     (' %7s' % ('-' if not v else self.money(v['fees'])), C['text']),
                     (' %6s   ' % _age(now - o['entry_ts']), C['dim'])] + track + \
                    [(' %-13s' % (dist[0] if dist else ''), dist[1] if dist else 0), (' ' + spark(hist, 12), self.pn(d))]
            self.segs(y + 2 + i, x + 2, parts, w - 4)
        return h

    def wallets(self, y, x, w, h, st, now):
        self.box(y, x, h, w, 'FOLLOWING', '%d main · %d bench · %d filler' % (len(self.leads), len(self.bench), len(self.filler)))
        C = self.C
        hours = {k.lower(): v for k, v in (L.cfg(self.c).get('hours') or {}).items()}
        today = time.strftime('%Y-%m-%d', time.gmtime(now))
        g = {a: dict(open=0, n=0, today=0.0, total=0.0, last=0.0) for a in self.leads + self.bench + self.filler}
        for o in st['ours'].values():
            r = g.get((o.get('leader') or '').lower())
            if r is None:
                continue
            r['last'] = max(r['last'], o.get('entry_ts') or 0)
            if not o.get('closed'):
                r['open'] += 1
                continue
            r['n'] += 1
            r['total'] += o.get('pnl_usd') or 0
            if time.strftime('%Y-%m-%d', time.gmtime(o.get('exit_ts') or 0)) == today:
                r['today'] += o.get('pnl_usd') or 0
        self.put(y + 1, x + 2, '  %-6s %-7s %-7s %4s %6s %9s %9s  %s' % ('WALLET', 'ROLE', 'HOURS', 'OPEN', 'COPIES', 'TODAY', 'TOTAL', 'LAST'), C['faint'], w - 4)
        hr = time.gmtime(now).tm_hour
        off = int(datetime.datetime.fromtimestamp(now, TZ).utcoffset().total_seconds() // 3600)
        names = self.leads + self.bench
        room = h - 3
        for i, a in enumerate(names[:room if len(names) <= room else room - 1]):
            r = g[a]
            hs = hours.get(a)
            role = 'main' if a in self.leads else 'spec' if hs else 'bench'     # a specialist is a bench wallet with hours
            active = hs is None or any(b <= hr < b + 3 for b in hs)
            self.segs(y + 2 + i, x + 2, [
                ('●' if r['open'] else '·', C['cyan'] if r['open'] else C['faint']),
                (' %-6s ' % tag(a), C['text'] if active else C['dim']),
                ('%-7s ' % role, C['cyan'] | curses.A_BOLD if role == 'main' else C['dim']),
                ('%-7s ' % (','.join('%02d-%02d' % ((b + off) % 24, (b + 3 + off) % 24) for b in hs) if hs else 'all'), (C['yellow'] | curses.A_BOLD) if hs and active else C['faint'] if hs else C['dim']),
                ('%4s ' % (r['open'] or '·'), C['text']), ('%6d ' % r['n'], C['dim']),
                ('%9s ' % self.money(r['today'], True), self.pn(r['today'])), ('%9s  ' % self.money(r['total'], True), self.pn(r['total']) | curses.A_BOLD),
                (_age(now - r['last']) if r['last'] else '-', C['faint'])], w - 4)
        if len(names) > room:
            self.put(y + 1 + room, x + 4, '+%d more' % (len(names) - room + 1), C['faint'])
        elif self.filler and len(names) < room:
            # the filler as ONE row: up to 40 wallets, copied only into slots nobody above needs
            f = [g[a] for a in self.filler]
            fr = dict(open=sum(r['open'] for r in f), n=sum(r['n'] for r in f), today=sum(r['today'] for r in f),
                      total=sum(r['total'] for r in f), last=max([r['last'] for r in f] + [0]))
            self.segs(y + 2 + len(names), x + 2, [
                ('●' if fr['open'] else '·', C['cyan'] if fr['open'] else C['faint']), (' %-6s ' % ('x%d' % len(f)), C['dim']),
                ('%-7s ' % 'filler', C['dim']), ('%-7s ' % 'all', C['dim']),
                ('%4s ' % (fr['open'] or '·'), C['text']), ('%6d ' % fr['n'], C['dim']),
                ('%9s ' % self.money(fr['today'], True), self.pn(fr['today'])), ('%9s  ' % self.money(fr['total'], True), self.pn(fr['total']) | curses.A_BOLD),
                (_age(now - fr['last']) if fr['last'] else '-', C['faint'])], w - 4)

    def closes(self, y, x, w, h, st, now):
        """The last closed copies, newest first, each P&L as a bar scaled to the biggest one shown."""
        C = self.C
        rows = sorted((o for o in st['ours'].values() if o.get('closed') and o.get('exit_ts')), key=lambda o: -o['exit_ts'])[:h - 2]
        tot = sum(o.get('pnl_usd') or 0 for o in rows)
        self.box(y, x, h, w, 'LAST CLOSES', 'last %d  %s' % (len(rows), self.money(tot, True)))
        top = max([abs(o.get('pnl_usd') or 0) for o in rows] + [0.01])
        bw = max(4, w - 52)
        for i, o in enumerate(rows):
            p = o.get('pnl_usd') or 0
            n = max(1, int(round(abs(p) / top * bw))) if abs(p) >= 0.005 else 0
            self.segs(y + 1 + i, x + 2, [(clock(o['exit_ts'], '%H:%M  '), C['faint']),
                                         ('%-15s ' % L.pair(o)[:15], C['text']), ('%-6s ' % tag(o.get('leader')), C['dim']),
                                         ('%5s  ' % _age(o['exit_ts'] - o['entry_ts']), C['faint']), ('%8s ' % self.money(p, True), self.pn(p) | curses.A_BOLD),
                                         (' ' + '█' * n, self.pn(p))], w - 4)

    def feed(self, y, x, w, h, st, evs, now):
        self.box(y, x, h, w, 'FEED', 'skips %s' % ('shown' if self.skips else 'hidden'))
        C = self.C
        ids = {str(o.get('leader_tid')): o for o in st['ours'].values()}
        off = int(datetime.datetime.fromtimestamp(now, TZ).utcoffset().total_seconds() // 3600)
        with self.live.lock:
            chain = list(self.live.feed)
        from copydesk import treasury
        chain += [dict(r, ev='treasury') for r in (self._file('trj', treasury.log_path(self.c), lambda p: _tail(p, 100)) or [])]
        rows = []
        for e in sorted(evs + chain, key=lambda e: e.get('ts', 0), reverse=True):
            if e.get('ev') == 'skip':
                if not self.skips:
                    continue
                if rows and rows[-1][0].get('ev') == 'skip' and rows[-1][0].get('why') == e.get('why'):
                    rows[-1][1] += 1
                    continue
            rows.append([e, 1])
            if len(rows) >= h - 2:
                break
        for i, (e, cnt) in enumerate(rows):
            k, t = e.get('ev'), e.get('ts', 0)
            fresh = now - t < 10
            o = ids.get(str(e.get('leader_tid'))) or {}
            who = tag(o.get('leader') or e.get('who'))
            if k == 'open':
                parts = [('▲ OPEN  ', C['cyan'] | curses.A_BOLD), ('%-15s ' % L.pair(e)[:15], C['white']),
                         ('$%.2f' % e.get('size_usd', 0), C['text']), ('  %s' % who, C['dim']), ('  bid' if e.get('quote_only') else '  range', C['faint'])]
            elif k == 'close':
                p = e.get('pnl_usd') or 0
                parts = [('✓ CLOSE ' if p > 0 else '✗ CLOSE ', self.pn(p) | curses.A_BOLD), ('%-15s ' % L.pair(e)[:15], C['white']),
                         (self.money(p, True), self.pn(p) | curses.A_BOLD), ('  %s held, fees $%.2f, gas $%.3f' % (_age(60 * (e.get('held_min') or 0)), e.get('fees_usd') or 0, e.get('gas_usd') or 0), C['dim'])]
                sl = L.sale_of(e.get('exit'))                          # where the token side went, at what share of fair
                if sl and sl['venue']:
                    parts.append(('  %s%s' % ('HELD' if sl['venue'] == 'held' else 'via ' + sl['venue'], ' %.0f%%' % (100 * sl['pct']) if sl['pct'] is not None else ''),
                                  C['yellow'] | curses.A_BOLD if sl['bad'] else C['faint']))
            elif k == 'treasury':
                w_ = e.get('what')
                lab = {'buy_eth': 'USDG→ETH', 'sell_eth': 'ETH→USDG', 'refuel': 'refuel'}.get(w_, w_)
                parts = [('⇄ %-6s' % ('GAS' if w_ == 'refuel' else 'SWAP'), C['magenta']), ('%-9s ' % lab, C['text']),
                         ('$%.2f' % e.get('usd', 0), C['text']), ('  %.1f bps  ' % e.get('cost_bps', 0), C['faint']), (e.get('why', ''), C['faint'])]
            elif k == 'chain':
                parts = [('◇ %-6s' % e['what'], C['blue']), ('%s ' % who, C['text']), ('#%d' % e['tid'], C['faint']), ('  on chain', C['faint'])]
            elif k == 'skip':
                why = re.sub(r'\((\d\d) UTC\)', lambda m: '(%02d %s)' % ((int(m.group(1)) + off) % 24, clock(now, '%Z')), e.get('why', ''))
                parts = [('· skip  ', C['faint']), ('x%d ' % cnt if cnt > 1 else '', C['dim']), (why, C['faint'])]
            elif k == 'error':
                parts = [('! ERROR ', C['magenta'] | curses.A_BOLD), ('%s %s' % (e.get('at', ''), e.get('why', '')), C['magenta'])]
            else:
                parts = [('↻ %-6s' % k, C['yellow']), (e.get('why') or ('%d main, %d bench, %d filler' % tuple(len([x for x in (e.get(f) or '').split(',') if x]) for f in ('leader', 'bench', 'filler')) if k == 'start' else ''), C['dim'])]
            self.segs(y + 1 + i, x + 2, [(clock(t) + '  ', C['text'] if fresh else C['faint'])] +
                      [(s, a | (curses.A_BOLD if fresh else 0)) for s, a in parts], w - 4)

    def draw(self):
        now = time.time()
        st, opn, eq, evs = self.load()
        H, W = self.s.getmaxyx()
        self.s.erase()
        if W < 110 or H < 30:
            self.put(H // 2, max(0, W // 2 - 25), 'make the terminal at least 110 x 30 (now %d x %d)' % (W, H), self.C['yellow'])
            self.s.refresh()
            return
        lv = self.live
        with lv.lock:
            vals, spare = dict(lv.vals), lv.spare
        # a copy not valued yet counts at what it cost, an entry in flight (the desk's write-ahead intent) at its size: neither
        # is $0 while the USDG that bought it has already left (owner 2026-10-01: the total dipped before each open)
        wv = lambda o: vals[o['token_id']]['principal'] + vals[o['token_id']]['fees'] if o.get('token_id') in vals else o.get('size_usd') or 0.0
        in_u = sum(wv(o) for o in opn if o.get('quote') != 'eth')
        in_e = sum(wv(o) for o in opn if o.get('quote') == 'eth')
        in_u += sum(p.get('size_usd') or 0.0 for p in (st.get('pending') or {}).values())
        loose = max(0.0, (lv.gas or 0.0) - float(L.cfg(self.c).get('gas_keep_usd', 15.0)))   # ETH above the gas band (in flight)
        total = None if spare is None else spare + in_u + in_e + loose
        parts = None if spare is None else [('free', spare, 'cyan'), ('USDG pairs', in_u, 'green'), ('ETH pairs', in_e, 'yellow'),
                                            ('ETH', loose, 'magenta')]
        stopped = os.path.exists(L.stop_path(self.c))
        last = next((e for e in reversed(evs) if e.get('ev') in ('start', 'stop')), {})
        run = 'STOPPED · no new copies' if stopped else 'DESK DOWN' if last.get('ev') == 'stop' else 'copying'
        self.header(W, run, now)
        bw = 58
        self.book(2, 1, bw, st, opn, total, now, parts)
        self.chart(2, 1 + bw + 1, W - bw - 3, st, eq, total, now)
        h = self.copies(13, 1, W - 2, opn, now)
        y = 13 + h
        rest = H - 1 - y
        ww = min(84, W // 2)
        wh = len(self.leads + self.bench) + 3 + (1 if self.filler else 0)
        if rest - wh >= 6:
            self.wallets(y, 1, ww, wh, st, now)
            self.closes(y + wh, 1, ww, rest - wh, st, now)
        else:
            self.wallets(y, 1, ww, rest, st, now)
        self.feed(y, 1 + ww + 1, W - ww - 3, rest, st, evs, now)
        foot = [(' q ', self.C['bar'] | curses.A_BOLD), (' quit   ', self.C['dim']), (' s ', self.C['bar'] | curses.A_BOLD), (' skips   ', self.C['dim']),
                (' r ', self.C['bar'] | curses.A_BOLD), (' 24h / 7d   ', self.C['dim'])]
        self.segs(H - 1, 1, foot)
        src = ('websocket %s' % __import__('urllib.parse').parse.urlsplit(getattr(lv, 'url', '') or '').hostname if lv.ws else 'poll every 10 s (socket down)') + ('  ·  ' + lv.err if lv.err else '') + \
            '  ·  desk event %s ago ' % (_age(now - evs[-1]['ts']) if evs else '-')
        self.put(H - 1, W - len(src) - 1, src, self.C['red'] if lv.err else self.C['faint'])
        self.s.refresh()

    def main(self, s):
        self.s = s
        curses.curs_set(0)
        self.palette()
        s.timeout(200)
        self.load()
        self.live.start()
        while True:
            self.draw()
            k = s.getch()
            if k in (ord('q'), 27):
                return
            if k == ord('s'):
                self.skips = not self.skips
            if k == ord('r'):
                self.days = 7 if self.days == 1 else 1
            if k == curses.KEY_RESIZE:
                curses.update_lines_cols()


def run(c):
    os.environ.setdefault('ESCDELAY', '25')
    try:
        curses.wrapper(Screen(c).main)
    except KeyboardInterrupt:
        pass


def _selftest():
    n = 0
    rows, lo, hi, b = braille([1, 2, 3, 4], 4, 2, base=2.5)
    assert (lo, hi) == (1, 4) and len(rows) == 2 and all(len(r) == 4 for r in rows) and b in (0, 1), (rows, lo, hi, b)
    n += 1
    assert rows[1][0] != ' ' and rows[0][-1] != ' ', rows                 # starts low-left, ends high-right
    n += 1
    assert braille([5], 4, 2)[1] is None and spark([1, 2], 5) == '▁█' and spark([3], 5) == ''
    n += 1
    g = big('$1.5')
    assert len(g) == 3 and len({len(r) for r in g}) == 1
    n += 1
    assert _age(90) == '1m' and _age(7200) == '2.0h'
    return n + 1


if __name__ == '__main__':
    print('lpwatch selftest: %d asserts' % _selftest())
