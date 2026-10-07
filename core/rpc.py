"""The chain transport, with TWO backends so everything above it runs offline.

  Live(chain)   - real JSON-RPC over the chain's endpoint pool, with the rotation ladder both old engines
                  earned (crypto rh.rpc: 400/403/520-524 and JSON-RPC -32603/'unavailable' are transient
                  and rotate; a socket read timeout / RemoteDisconnected rotates too; a solo-endpoint chain
                  backs off instead of raising). multicall3 aggregate3, eth_call with state overrides.
  Replay(fixtures) - answers eth_call/eth_getBalance/eth_blockNumber from a recorded dict. `hookbot <chain>
                  smoke` runs the whole pipeline on this, zero network, so a poll is testable from the get-go.

Both expose the same surface: rpc, head, call, multicall, getbalance. Nothing above this module knows
which one it is holding.
"""
import json, socket, ssl, threading, time, urllib.parse, urllib.request, urllib.error, http.client

MULTICALL3 = '0xca11bde05977b3631167028862be2a173976ca11'
_UA = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/128'}
_TRANSIENT = (400, 403, 408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530)   # 525/526/530: Cloudflare origin errors (rpc.arc-scan.org 530, 2026-10-04) - rotate, never crash; 408: a public node timing out a big multicall (botlab find, 2026-10-04)


class RpcError(RuntimeError):
    pass


def _qty_hex(n):
    """JSON-RPC quantity (minimal hex digits, no leading zero digits except 0x0)."""
    n = int(n)
    return '0x0' if n == 0 else hex(n)


# IPv4 FIRST, for every socket this process opens (JSON-RPC and the websockets both resolve through here).
# This box's IPv6 path to Cloudflare drops large packets: a 80 KB eth_call body answered 0/10 over IPv6 and
# 10/10 over IPv4 on publicnode (1/10 vs 10/10 on the official RPC), measured 2026-09-24. Small calls fit
# one packet and worked, so it showed up only as 30 s RemoteDisconnected stalls on the first state read
# after every pools rotation (81-320 s blind desks) and publicnode's socket never acking our log
# subscription. Preferring, not forcing: a host with no A record still gets its AAAA.
_gai = socket.getaddrinfo


def _v4_first(*a, **k):
    return sorted(_gai(*a, **k), key=lambda r: r[0] != socket.AF_INET)


socket.getaddrinfo = _v4_first


_CONNS = threading.local()
# ONE TLS context for every connection. http.client built a fresh default context per connection, and that re-reads the
# whole CA bundle under the interpreter lock: 18.7% of the robinhood desk's CPU (py-spy on AWS, 2026-09-26), the lock
# the fast path waits on. An SSLContext is safe to share across threads and connections.
_SSL = ssl.create_default_context()
_MC_POOLS = {}          # workers -> a persistent executor: its threads keep their kept-alive connections warm


REQS = __import__('collections').Counter()     # host -> JSON-RPC POSTs this process made (the heartbeat reports the rate)


def _post(url, body, timeout=60):
    """POST a JSON-RPC body on a KEPT-ALIVE connection (one per host per thread). urlopen opened a fresh
    TCP+TLS connection for every call - 76 ms of a 138 ms robinhood round trip, measured 2026-09-19 - and
    every gate pays it on a one-block edge. A connection the server has since closed is retried once on
    a fresh one; a timeout is never retried here (the caller's rotation ladder owns that)."""
    p = urllib.parse.urlsplit(url)
    REQS[p.netloc] += 1
    pool = getattr(_CONNS, 'd', None)
    if pool is None:
        pool = _CONNS.d = {}
    key = (p.scheme, p.netloc)
    path = (p.path or '/') + ('?' + p.query if p.query else '')
    for fresh in (False, True):
        c = pool.get(key)
        if c is None:
            C = http.client.HTTPSConnection if p.scheme == 'https' else http.client.HTTPConnection
            c = pool[key] = (C(p.netloc, timeout=timeout, context=_SSL) if p.scheme == 'https' else C(p.netloc, timeout=timeout))
        try:
            c.request('POST', path, body=body, headers=_UA)
            resp = c.getresponse()
            data = resp.read()
        except (TimeoutError, socket.timeout):
            c.close(); pool.pop(key, None)
            raise
        except (http.client.HTTPException, ConnectionError, OSError):
            c.close(); pool.pop(key, None)
            if fresh:
                raise
            continue
        if resp.status >= 400:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
        return json.loads(data)


SLOW_S = 3.0      # a single RPC attempt slower than this (or failing) is reported to on_slow


class Live:
    # on_slow(**fields): the runner points this at its trace, so a node that eats a big batch for 60 s
    # is named in var/trace.jsonl (stage 'rpc') instead of showing up only as a desk that went silent -
    # robinhood 2026-09-24 lost 81-320 s after each pools rotation to exactly that.
    on_slow = None

    def __init__(self, chain):
        self.chain = chain
        self.rpc_url = chain.rpc_url
        self.rotate = chain.rotate or [self.rpc_url]

    # -- the one lever every read goes through -----------------------------------------------------
    @staticmethod
    def _is_pinned(method, params):
        """True when this call asks for state at a PAST block. Those need an archive node: robinhood's
        publicnode answers 'latest' happily and 403s on history, which surfaced as 7 phantom 'state
        mismatches' in feedcheck before the read was routed properly."""
        try:
            if method in ('eth_call', 'eth_getBalance', 'eth_getStorageAt', 'eth_getCode'):
                tag = params[-1] if params else 'latest'
                if isinstance(tag, dict):
                    tag = params[1] if len(params) > 1 else 'latest'
                return isinstance(tag, str) and tag.startswith('0x') and len(tag) <= 20
        except Exception:
            pass
        return False

    RECENT_BLOCKS = 64

    def _needs_archive(self, method, params):
        """A pinned read of a RECENT block is not history: every full node keeps ~128 blocks of state. Sending those to
        archive_urls routed the desk's own head-pinned tick / refetch / resync to the slowest, most rate-limited nodes
        (robinhood 2026-09-25: blxrbdn 'header not found', 429s, 1,545-pool refetches failing, the loop 40-90 s behind)."""
        if not self._is_pinned(method, params):
            return False
        try:
            tag = params[1] if isinstance(params[-1], dict) and len(params) > 1 else params[-1]
            head = (getattr(self.chain, 'head', None) or {}).get('number')
            return not (head and head - int(tag, 16) <= self.RECENT_BLOCKS)
        except Exception:
            return True

    def rpc(self, method, params, url=None):
        want = url or self.rpc_url
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
        if url is None and self._needs_archive(method, params) and getattr(self.chain, 'archive_urls', None):
            arch = [u for u in self.chain.archive_urls]
            urls = arch + [u for u in ([want] + self.rotate) if u not in arch]
        else:
            urls = [want] + [u for u in self.rotate if u != want]
        solo = len(urls) == 1
        tries = 9 if solo else 6
        out = None
        for attempt in range(tries):
            u = urls[attempt % len(urls)]
            t_try = time.time()
            try:
                out = _post(u, body)
                if self.on_slow and time.time() - t_try > SLOW_S:
                    self._slow(method, u, t_try, body, attempt, 'ok')
                err = out.get('error') if isinstance(out, dict) else None
                if isinstance(err, dict) and attempt < tries - 1:
                    msg = str(err.get('message', '')).lower()
                    # 'historical state ... is not available': blockmachine load-balances over nodes of which
                    # some are pruned; the next attempt usually lands on one that holds the state (2026-09-24)
                    if err.get('code') in (-32603, 429, -32005) or 'unavailable' in msg or 'not available' in msg \
                            or 'try again' in msg:
                        time.sleep(min(20, 0.75 * 2 ** attempt) if solo else 1.0); continue
                break
            # ssl.SSLError is transport too: 'EOF occurred in violation of protocol' from a node dropping the
            # TLS session killed a robinhood desk at startup (2026-09-23) because nothing here caught it
            except (TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException, ssl.SSLError) as e:
                if self.on_slow:
                    self._slow(method, u, t_try, body, attempt, type(e).__name__)
                if attempt < tries - 1:
                    if solo: time.sleep(min(30, 0.75 * 2 ** attempt))
                    continue
                raise
            except urllib.error.URLError as e:
                code = getattr(e, 'code', None)
                if (code is None or code in _TRANSIENT) and attempt < tries - 1:
                    if solo: time.sleep(min(30, 0.75 * 2 ** attempt))
                    elif attempt >= len(urls) - 1: time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        else:
            raise RpcError('rpc: every source refused ' + method)
        if isinstance(out, dict) and 'error' in out:
            raise RpcError(out['error'])
        return out['result']

    def _slow(self, method, url, t0, body, attempt, outcome):
        try:
            self.on_slow(method=method, url=url.split('//')[-1].split('/')[0], ms=round((time.time() - t0) * 1e3),
                         body_bytes=len(body), attempt=attempt, outcome=outcome)
        except Exception:
            pass

    def head(self):
        return int(self.rpc('eth_blockNumber', []), 16)

    def getbalance(self, addr, block='latest', url=None):
        return int(self.rpc('eth_getBalance', [addr, block], url=url), 16)

    def call(self, to, data, frm=None, gas=None, gas_price=None, block='latest', override=None, url=None, value=None):
        p = {'to': to, 'data': data}
        if frm: p['from'] = frm
        if gas is not None: p['gas'] = _qty_hex(gas)
        if gas_price is not None: p['gasPrice'] = _qty_hex(gas_price)
        if value: p['value'] = _qty_hex(int(value))
        args = [p, block] + ([override] if override else [])
        return self.rpc('eth_call', args, url=url) if url else self.rpc('eth_call', args)

    MC_WORKERS = 4

    def multicall(self, calls, block='latest', chunk=200, override=None, gas=None, workers=None, url=None):
        """calls: [(to, calldata_hex)] -> [(ok, returndata_hex)]. One RPC per `chunk` (Multicall3 aggregate3).
        `url` pins the read to one node (a gate's pins must come from the node and block tag the gate reads).
        `override`/`gas` pass through to the eth_call (discovery/scout.py batches state-override probes).
        A read of several chunks runs them IN PARALLEL (MC_WORKERS), results in order: base's boot fetch read 29,404
        ticks as ~300 chunks one after another, 85 s, and the desk re-execs on every rotation (2026-09-25)."""
        workers = self.MC_WORKERS if workers is None else workers
        if len(calls) > 2 * chunk and workers > 1:
            import concurrent.futures as cf
            parts = [calls[i:i + chunk] for i in range(0, len(calls), chunk)]
            # a PERSISTENT pool: a fresh executor per call meant fresh threads, so every chunk paid a new TCP+TLS
            # connection (connections are per thread) - 33% of the robinhood desk's samples sat in these workers
            ex = _MC_POOLS.get(workers) or _MC_POOLS.setdefault(workers, cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix='mc'))
            res = list(ex.map(lambda p: self._multicall1(p, block, len(p), override, gas, url), parts))
            return [x for r in res for x in r]
        return self._multicall1(calls, block, chunk, override, gas, url)

    def _multicall1(self, calls, block='latest', chunk=200, override=None, gas=None, url=None):
        out = []
        for i in range(0, len(calls), chunk):
            part = calls[i:i + chunk]; n = len(part)
            head = '82ad56cb' + (32).to_bytes(32, 'big').hex() + n.to_bytes(32, 'big').hex()
            offs, bodies, cur = [], [], 32 * n
            for to, data in part:
                d = bytes.fromhex(data[2:] if data.startswith('0x') else data)
                body = (bytes.fromhex(to[2:].lower().rjust(64, '0')) + (1).to_bytes(32, 'big')
                        + (96).to_bytes(32, 'big') + len(d).to_bytes(32, 'big') + d
                        + b'\0' * ((32 - len(d) % 32) % 32))
                offs.append(cur); bodies.append(body); cur += len(body)
            enc = head + ''.join(o.to_bytes(32, 'big').hex() for o in offs) + ''.join(b.hex() for b in bodies)
            try:
                r = bytes.fromhex(self.call(MULTICALL3, '0x' + enc, block=block, override=override, gas=gas, url=url)[2:])
            except RpcError as e:
                # A NODE'S BODY CAP (base 2026-09-25: "request 36747 bytes exceeds the 26001 byte limit"): halve the
                # batch and retry, instead of losing the whole read - the tick and discovery both went through here.
                if n > 1 and any(w in str(e).lower() for w in ('size limit', 'too large', 'too complex', 'exceeds')):
                    out += self._multicall1(part, block=block, chunk=max(1, n // 2), override=override, gas=gas, url=url)
                    continue
                raise
            base = int.from_bytes(r[0:32], 'big'); cnt = int.from_bytes(r[base:base + 32], 'big')
            for k in range(cnt):
                o = base + 32 + int.from_bytes(r[base + 32 + 32 * k:base + 64 + 32 * k], 'big')
                ok = int.from_bytes(r[o:o + 32], 'big') == 1
                doff = int.from_bytes(r[o + 32:o + 64], 'big'); dl = int.from_bytes(r[o + doff:o + doff + 32], 'big')
                out.append((ok, '0x' + r[o + doff + 32:o + doff + 32 + dl].hex()))
        return out


class Replay:
    """A backend that answers from a recorded fixture. Keys are method-specific:
        head              -> {'head': <int>}
        eth_getBalance    -> {'balance': {'<addr_lower>@<block>': <int>}} (falls back to 'balance_default')
        eth_call          -> {'call': {'<to_lower>|<data_prefix>': '<hex>'}} matched by longest data prefix
        multicall         -> {'multicall': {'<to>|<sel8>': '<hex>'}} per sub-call, else (True,'0x')
    A missing key raises, so a fixture gap is a loud test failure, never a silent zero."""
    def __init__(self, fx):
        self.fx = fx

    def head(self):
        return int(self.fx.get('head', 0))

    def getbalance(self, addr, block='latest', url=None):
        b = self.fx.get('balance', {})
        k = '%s@%s' % (addr.lower(), block)
        if k in b: return int(b[k])
        if addr.lower() in b: return int(b[addr.lower()])
        return int(self.fx.get('balance_default', 0))

    def _lookup_call(self, to, data):
        table = self.fx.get('call', {})
        to = (to or '').lower(); data = data or ''
        best, bestlen = None, -1
        for key, val in table.items():
            kt, _, kd = key.partition('|')
            if kt and kt != to: continue
            if data.startswith(kd) and len(kd) > bestlen:
                best, bestlen = val, len(kd)
        return best

    def call(self, to, data, frm=None, gas=None, gas_price=None, block='latest', override=None, url=None, value=None):
        v = self._lookup_call(to, data)
        if v is None:
            raise RpcError('Replay: no fixture for eth_call to %s data %s' % ((to or '')[:12], (data or '')[:12]))
        if isinstance(v, dict) and v.get('revert'):
            raise RpcError({'code': 3, 'message': 'execution reverted', 'data': v['revert']})
        return v

    def rpc(self, method, params, url=None):
        if method == 'eth_blockNumber':
            return hex(self.head())
        if method == 'eth_getBalance':
            return hex(self.getbalance(params[0], params[1] if len(params) > 1 else 'latest'))
        if method == 'eth_call':
            p = params[0]
            return self.call(p.get('to'), p.get('data'), block=params[1] if len(params) > 1 else 'latest')
        if method == 'eth_gasPrice':
            return hex(int(self.fx.get('gas_price', 20 * 10 ** 9)))
        if method == 'eth_getBlockByNumber':
            return {'number': hex(self.head()), 'baseFeePerGas': hex(int(self.fx.get('basefee', 20 * 10 ** 9))),
                    'timestamp': hex(0)}
        if method == 'eth_getStorageAt':
            return self.fx.get('storage', {}).get(params[0].lower(), '0x' + '00' * 31 + '01')
        raise RpcError('Replay: unsupported method %s' % method)

    def multicall(self, calls, block='latest', chunk=200, override=None, gas=None, workers=None, url=None):
        """Match each sub-call by '<to>|<data prefix>' longest-first (so a fixture can distinguish two pools
        that share a selector by their poolId in the data), then '<to>|<sel10>', then '*|<sel10>', else
        (True,'0x')."""
        table = self.fx.get('multicall', {})
        out = []
        for to, data in calls:
            to = (to or '').lower(); data = data or ''; sel = data[:10]
            best, bestlen = None, -1
            for key, val in table.items():
                kt, _, kd = key.partition('|')
                if kt not in (to, '*'):
                    continue
                if data.startswith(kd) and len(kd) > bestlen:
                    best, bestlen = val, len(kd)
            out.append((True, best if best is not None else '0x'))
        return out


def _selftest():
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    fx = {'head': 100, 'balance': {'0xabc@latest': 5, '0xabc': 7},
          'call': {'0xdead|0x1234': '0x' + '00' * 31 + '2a',      # 42
                   '0xdead|0x': '0x' + '00' * 31 + '01'},          # fallback 1
          'multicall': {'*|0x70a08231': '0x' + '00' * 31 + '09'}}
    r = Replay(fx)
    A(r.head() == 100)
    A(r.getbalance('0xABC', 'latest') == 5, 'block-specific balance')
    A(r.getbalance('0xabc', 'pending') == 7, 'addr fallback balance')
    A(int(r.call('0xDEAD', '0x1234ffff')[2:], 16) == 42, 'longest-prefix call match')
    # an at-block read is recognised as archive work; a 'latest' one is not
    A(Live._is_pinned('eth_call', [{'to': '0x1'}, '0x1491d33']) is True)
    A(Live._is_pinned('eth_call', [{'to': '0x1'}, 'latest']) is False)
    A(Live._is_pinned('eth_getBalance', ['0xabc', '0x64']) is True)
    A(Live._is_pinned('eth_blockNumber', []) is False)
    A(int(r.call('0xDEAD', '0x9999')[2:], 16) == 1, 'call prefix fallback')
    A(r.multicall([('0xX', '0x70a08231' + '0' * 64)])[0][1].endswith('09'), 'multicall by selector')
    try:
        r.call('0xNOPE', '0xbeef'); A(False, 'missing fixture must raise')
    except RpcError:
        A(True)
    # a fixture can declare a revert payload (used to test the gate/A6 path offline)
    r2 = Replay({'call': {'0xh|0x': {'revert': '0xdeadbeef'}}})
    try:
        r2.call('0xh', '0x01'); A(False)
    except RpcError as e:
        A(isinstance(e.args[0], dict) and e.args[0]['data'] == '0xdeadbeef', 'revert payload surfaced')
    # an SSL EOF from one node is transport, not an answer: rotate to the next url instead of raising
    global _post
    real, calls = _post, []
    def fake(u, body, timeout=60):
        calls.append(u)
        if u == 'https://a':
            raise ssl.SSLEOFError(8, 'EOF occurred in violation of protocol')
        return {'jsonrpc': '2.0', 'id': 1, 'result': '0x2a'}
    class Ch:
        rpc_url, rotate, archive_urls = 'https://a', ['https://a', 'https://b'], []
    _post = fake
    try:
        A(Live(Ch()).rpc('eth_blockNumber', []) == '0x2a' and calls == ['https://a', 'https://b'], calls)
    finally:
        _post = real
    print('rpc selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
