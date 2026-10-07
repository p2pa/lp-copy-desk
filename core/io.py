"""Side-effect plumbing: the per-chain var store, the tape, the daily gas spend, and the ONE Telegram
channel. Every line the bot sends is tagged [chain/strategy] so a single channel carries both chains
legibly - the one-channel requirement. TG failures never raise (a desk must not die because a message
would not send)."""
import json, os, time, urllib.request, urllib.parse


def _atomic(path, obj):
    tmp = path + '.tmp'
    json.dump(obj, open(tmp, 'w'), indent=1)
    os.replace(tmp, path)


class Store:
    """Files under var/<chain>/. Everything mutable lives here and is gitignored."""
    def __init__(self, chain):
        self.dir = chain.var
        self.name = chain.name

    def path(self, base):
        return os.path.join(self.dir, base)

    def load(self, base, default=None):
        p = self.path(base)
        try:
            return json.load(open(p))
        except Exception:
            return {} if default is None else default

    def save(self, base, obj):
        _atomic(self.path(base), obj)

    def exists(self, base):
        return os.path.exists(self.path(base))


class Tape:
    def __init__(self, store, base='tape.jsonl'):
        self.path = store.path(base)

    def write(self, row):
        row = dict(row, ts=time.time())
        with open(self.path, 'a') as f:
            f.write(json.dumps(row) + '\n')


class Spend:
    """Today's gas spend (UTC), the daily backstop counter shared by every fire path on the chain."""
    def __init__(self, store, base='spend.json'):
        self.store = store
        self.base = base

    def today(self, add=0.0):
        day = time.strftime('%Y-%m-%d', time.gmtime())
        d = self.store.load(self.base, {})
        if add:
            d[day] = round(d.get(day, 0.0) + add, 6)
            self.store.save(self.base, d)
        return d.get(day, 0.0)


class Telegram:
    """ONE channel for the whole bot, HTML-formatted. Nothing calls this with a hand-built string: every
    message is an event dict rendered by core.fmt, which is why the channel stays readable.

    chat_id is LEARNED from getUpdates on first use (send the bot one message) and persisted next to the
    token. A missing token or chat makes send() a quiet no-op, so dry/smoke never depend on Telegram."""
    API = 'https://api.telegram.org/bot%s/%s'

    def __init__(self, chain):
        self.cf = chain.tg_config
        self.name = chain.name

    def _cfg(self):
        try:
            return json.load(open(self.cf))
        except Exception:
            return {}

    def _api(self, method, **params):
        c = self._cfg()
        r = urllib.request.Request(self.API % (c['token'], method),
                                   urllib.parse.urlencode(params).encode())
        return json.load(urllib.request.urlopen(r, timeout=20))

    def chat_id(self):
        """The configured chat, or the first chat that ever messaged the bot (then persisted)."""
        c = self._cfg()
        if not c.get('token'):
            return None
        if c.get('chat_id'):
            return c['chat_id']
        try:
            for u in self._api('getUpdates').get('result', []):
                m = u.get('message') or u.get('channel_post') or {}
                if m.get('chat'):
                    c['chat_id'] = m['chat']['id']
                    _atomic(self.cf, c)
                    os.chmod(self.cf, 0o600)
                    return c['chat_id']
        except Exception:
            pass
        return None

    def send_event(self, ev):
        """Render an event through core.fmt and send it as HTML, falling back to plain text so a markup
        slip never silently drops an alert."""
        from . import fmt
        cid = self.chat_id()
        if not cid:
            return False
        body = fmt.tg(ev)[:4000]
        for mode in ('HTML', None):
            try:
                kw = {'chat_id': cid, 'text': body if mode else _strip_tags(body),
                      'disable_web_page_preview': 'true'}
                if mode:
                    kw['parse_mode'] = mode
                if self._api('sendMessage', **kw).get('ok'):
                    return True
            except Exception:
                continue
        return False

    def photo(self, png, caption=''):
        """One image with an HTML caption (sendPhoto, multipart). False when Telegram is not configured or refuses."""
        import uuid
        c, cid = self._cfg(), self.chat_id()
        if not cid:
            return False
        b = uuid.uuid4().hex
        part = lambda k, v: ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode()
        body = (part('chat_id', cid) + part('caption', caption[:1000]) + part('parse_mode', 'HTML')
                + ('--%s\r\nContent-Disposition: form-data; name="photo"; filename="card.png"\r\nContent-Type: image/png\r\n\r\n' % b).encode()
                + png + ('\r\n--%s--\r\n' % b).encode())
        try:
            r = urllib.request.Request(self.API % (c['token'], 'sendPhoto'), body,
                                       {'Content-Type': 'multipart/form-data; boundary=%s' % b})
            return bool(json.load(urllib.request.urlopen(r, timeout=30)).get('ok'))
        except Exception:
            return False

    # kept so old call sites (and ad-hoc notes) still work: a bare string becomes a `note` event
    def send(self, text, tag=None):
        chain, _, strategy = (tag or self.name).partition('/')
        return self.send_event(dict(kind='note', chain=chain or self.name,
                                    strategy=strategy or None, detail=text))


def _strip_tags(s):
    import re
    return re.sub(r'<[^>]+>', '', s)


class Notifier:
    """The one exit for anything the bot wants to say: tape it, print it, send it. Call sites pass an
    event dict, never a formatted string."""
    def __init__(self, chain, tape, tg):
        self.chain = chain
        self.tape = tape
        self.tg = tg

    def __call__(self, kind, tg=True, tape=True, echo=True, **fields):
        from . import fmt
        ev = dict(kind=kind, chain=self.chain.name, **fields)
        # ONE PLACE TURNS A HASH INTO A LINK. Every card that carries a tx gets the chain's explorer, so
        # fmt never has to know which chain it is rendering (rpc.toml `explorer`).
        if ev.get('tx') and getattr(self.chain, 'explorer', ''):
            ev.setdefault('tx_url', '%s/tx/%s' % (self.chain.explorer, ev['tx']))
        if tape:
            try:
                self.tape.write(ev)
            except Exception:
                pass
        if echo:
            print('   ' + fmt.console(ev))
        if tg and tg_worthy(ev):
            try:
                self.tg.send_event(ev)
            except Exception:
                pass
        return ev


# WHAT REACHES TELEGRAM: money and outages, nothing else. The first live evening (2026-09-19) sent ~230
# messages in 6h - 123 drift states, 34 6M regime flips, gate/watchdog churn - and a channel that pages
# for everything is read for nothing. Every event still lands on the tape and the console; `hookbot
# <chain> health` / `stats` are where the rest is read.
TG_KINDS = ('fill', 'revert', 'error', 'gated', 'breaker', 'note', 'lp')
TG_HEALTH = ('DEAD', 'DARK', 'BLOCKED: breaker')


# A LOST RACE IS NOT NEWS. A revert whose leg came back within this much of its floor, on a pool somebody
# else swapped in the same window, is the ordinary cost of racing - it lands on the tape and the console
# and is counted by risk.race_lost(), but it does not page. 30 of arc's 30 reverts were this shape, and a
# channel that pages for every lost race is read for nothing. Anything WIDER than this is a real miss (a
# fee we did not know about, a stale twin) and still pages.
LOST_RACE_BPS = 100.0


def tg_worthy(ev):
    k = ev.get('kind')
    if k == 'revert' and ev.get('racers') and (ev.get('short_bps') or 1e9) <= LOST_RACE_BPS:
        return False
    if k in TG_KINDS:
        return True
    if k == 'health' and ev.get('waiver') == 'watchdog':
        return any(p in str(ev.get('detail') or '') for p in TG_HEALTH)
    return False


def _selftest():
    import tempfile
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    # a lost race is taped but never paged; a WIDE miss still pages
    A(tg_worthy(dict(kind='revert', short_bps=33.0, racers=2)) is False, 'a 33 bps miss with a racer is noise')
    A(tg_worthy(dict(kind='revert', short_bps=4447.0, racers=1)) is True, 'a 44% miss is a mispricing')
    A(tg_worthy(dict(kind='revert', short_bps=12.0, racers=0)) is True, 'short with NOBODY else there pages')
    A(tg_worthy(dict(kind='revert')) is True, 'an undecodable revert still pages')
    A(tg_worthy(dict(kind='fill')) is True and tg_worthy(dict(kind='poll')) is False)

    class FakeChain:
        pass
    d = tempfile.mkdtemp()
    ch = FakeChain(); ch.var = d; ch.name = 'test'; ch.tg_config = os.path.join(d, 'nope.json')
    st = Store(ch)
    st.save('x.json', {'a': 1})
    A(st.load('x.json') == {'a': 1} and st.exists('x.json'))
    A(st.load('missing.json', []) == [], 'missing default')
    sp = Spend(st)
    A(sp.today() == 0.0)
    A(abs(sp.today(0.5) - 0.5) < 1e-9 and abs(sp.today() - 0.5) < 1e-9, 'spend accumulates')
    tp = Tape(st)
    tp.write({'ev': 'dry', 'net': 1.5})
    A(sum(1 for _ in open(tp.path)) == 1 and json.loads(open(tp.path).readline())['ev'] == 'dry')
    A(Telegram(ch).send('hi') is False, 'missing tg config -> quiet no-op')
    A(Telegram(ch).chat_id() is None, 'no token -> no chat')
    A(_strip_tags('<b>x</b> <code>1</code>') == 'x 1', 'html fallback strips tags')

    # Notifier: tapes + echoes + never raises when tg is unconfigured
    class FakeChain2:
        name = 'test'
    nf = Notifier(FakeChain2(), tp, Telegram(ch))
    ev = nf('fill', sym='X', size_usd=100.0, profit_usd=1.25, gas_usd=0.01, strategy='gasleft', echo=False)
    A(ev['kind'] == 'fill' and ev['chain'] == 'test')
    rows = [json.loads(l) for l in open(tp.path)]
    A(rows[-1]['kind'] == 'fill' and rows[-1]['profit_usd'] == 1.25, 'notifier taped the event')
    print('io selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
