"""Signing and sending, dry-safe by construction. `send()` is DRY (no network at all) unless go AND
LIVE_OK=yes AND the wallet clears gas-cost + gas_floor. One nonce lock across the process (the arc engine
proved shots_tick/feepolicy/deploy can all reach send in one process and race the nonce). Send endpoints
are tried in the chain's order because a metered proxy can refuse a gas limit the CHAIN itself accepts
(crypto_arc §: a +$114 route rejected at gas 16.8M by the proxy while the block gasLimit was 30M).

Revert decoding is CycleArb's custom errors; `explain` replays a landed tx at its block on an archive
endpoint (neither chain has tracing) to name why it reverted."""
import os, sys, threading, time
from Crypto.Hash import keccak as _kc
from eth_abi import decode as abi_decode

_SEND_LOCK = threading.Lock()

_ERR_SIGS = ['NotCaller(address)', 'NotOwner(address)', 'NotPoolManager(address)', 'BadV3Callback(address)',
             'EmptyRoute()', 'FloorBelowPrincipal(uint256,uint256)', 'Overspend(uint256,uint256,uint256)',
             'LegLeftOpen(uint256)', 'WrongEndToken(address,address)', 'ZeroOutput(uint256)',
             'LegShort(uint256,uint256,uint256)', 'FloorNotMet(uint256,uint256)',
             'SettleShort(address,uint256,uint256)', 'TransferFailed(address)', 'RescueFailed()',
             'TooManyHops(uint256)', 'WrongTokenOut(uint256,address,address)', 'CallFailed(uint256,address)',
             'TargetNotAllowed(address)', 'Pinned(uint8)', 'BadPins(uint256)',
             'WrappedError(address,bytes4,bytes,bytes)']   # v4 PoolManager wrapping a HOOK's revert


def _sel(sig):
    k = _kc.new(digest_bits=256); k.update(sig.encode()); return '0x' + k.hexdigest()[:8]


ERRS = {_sel(s): s for s in _ERR_SIGS}


def gas_price(chain):
    if chain.gas_mode == 'flat':
        return chain.gwei * 10 ** 9
    return int(chain.node.rpc('eth_gasPrice', []), 16)


def max_fee(chain, gp):
    # Arc basefee is flat (no auction) -> a small headroom; Robinhood races -> 3x + tip.
    return int(gp * 1.25) + 10 ** 5 if chain.gas_mode == 'flat' else gp * 3 + 10 ** 6


MIN_TIP = 10 ** 5
_NONCE = {}


def _next_nonce(chain, addr):
    """The SEND node's pending nonce, never BELOW one this process already used.

    Two ways to get this wrong and both have happened. A node whose pending pool LAGS hands the same nonce
    twice (robinhood, 02:44 2026-09-20, a shot send right after a deploy from the same key) - the memory
    covers that. A node that is AHEAD refuses a nonce that looked free, and the read used to come from the
    rotate pool while the tx goes to send_urls, so the two views were allowed to differ: four robinhood
    routes died on `tx: 1906 state: 1907` on 2026-09-22, after three bridge txs moved the same key from
    outside this process. Reading it from the node that will TAKE the tx is what closes that; the rotate
    pool is only the fallback when the send endpoint cannot answer."""
    n = None
    prep = _PREP.get((chain.chain_id, addr.lower())) or {}
    if prep.get('nonce') is not None and time.time() - prep.get('nonce_t', 0) < 5.0:
        n = prep['nonce']                                 # the send node's view, at most 5 s old (warm())
    for url in (list(getattr(chain, 'send_urls', None) or []) + [None]) if n is None else []:
        try:
            n = int(chain.node.rpc('eth_getTransactionCount', [addr, 'pending'], url=url), 16)
            break
        except Exception:
            continue
    if n is None:
        raise RuntimeError('no endpoint answered eth_getTransactionCount for %s' % addr)
    k = (chain.chain_id, addr.lower())
    n = max(n, _NONCE.get(k, -1) + 1)
    _NONCE[k] = n
    return n


_PREP = {}
_PUSHERS = {}      # url -> Pusher


class Pusher:
    """ONE LONG-LIVED THREAD PER SEND ENDPOINT, holding its connection open. rpc._post keeps connections alive PER
    THREAD, and send() started a new thread per endpoint for every send - so every send paid a fresh TCP+TLS handshake:
    robinhood submit 387 ms against a 121-141 ms round trip (2026-09-25). A cheap eth_chainId every KEEP_S stops a
    server closing the idle socket. A send is one round trip."""
    KEEP_S = 15.0

    def __init__(self, chain, url):
        import queue
        self.chain, self.url, self.q = chain, url, queue.Queue()
        threading.Thread(target=self._loop, name='push-' + url.split('//')[-1][:24], daemon=True).start()

    def _loop(self):
        import queue
        last = 0.0
        while True:
            try:
                job = self.q.get(timeout=max(0.1, self.KEEP_S - (time.time() - last)))
            except queue.Empty:
                try:
                    self.chain.node.rpc('eth_chainId', [], url=self.url)
                except Exception:
                    pass
                last = time.time()
                continue
            raw, cb = job
            try:
                cb(self.url, self.chain.node.rpc('eth_sendRawTransaction', [raw], url=self.url), None)
            except Exception as e:
                cb(self.url, None, e)
            last = time.time()

    def push(self, raw, cb):
        self.q.put((raw, cb))


def pushers(chain):
    """The chain's send + broadcast endpoints, each behind its own warm Pusher (started on first use)."""
    urls = list(dict.fromkeys(list(chain.send_urls) + list(getattr(chain, 'broadcast_urls', None) or [])))
    for u in urls:
        if u not in _PUSHERS:
            _PUSHERS[u] = Pusher(chain, u)
    return [_PUSHERS[u] for u in urls]


def warm(chain, addr, every=2.0):
    """Keep what a send must know about `addr` fresh in the background: the SEND node's pending nonce and the wallet's
    balance, every `every` s. A send then signs without a round trip - robinhood's 'sign' stage was 265 ms of network
    (nonce + gas price, 2026-09-25), the signing itself microseconds. The nonce rule is unchanged: _next_nonce still
    takes max(send node, what this process used + 1), and a failed send still forgets it."""
    from . import wallet
    k = (chain.chain_id, addr.lower())
    if k in _PREP:
        return
    box = _PREP[k] = {}
    if getattr(chain.node, 'fx', None) is None:
        pushers(chain)          # open and keep warm every send connection now, not on the first send

    def loop():
        while True:
            for url in list(getattr(chain, 'send_urls', None) or []) + [None]:
                try:
                    box['nonce'] = int(chain.node.rpc('eth_getTransactionCount', [addr, 'pending'], url=url), 16)
                    box['nonce_t'] = time.time()
                    break
                except Exception:
                    continue
            try:
                box['bal'], box['bal_t'] = wallet.usd_balance(chain, addr), time.time()
            except Exception:
                pass
            time.sleep(every)
    threading.Thread(target=loop, name='send-prep', daemon=True).start()


def forget_nonce(chain, addr):
    """After a send FAILS at the node, the remembered nonce is a lie - drop it and re-read next time."""
    _NONCE.pop((chain.chain_id, (addr or '').lower()), None)


def tip_wei(chain, expected_usd, units=None):
    """The priority tip per gas for a fill expected to make `expected_usd`, in native wei.

    On a PRIORITY-ordered chain the tip is the whole race. Arc sorts a block by price: of 41,218 pairs of
    our-pool txs sharing a block over 48h, the earlier one paid more in 98.6% (2026-09-19). The rivals on
    our arc pools paid a median 35-120 gwei; the old desk and this engine paid 20 (basefee, 1e5 wei tip),
    i.e. LAST in every contested block. So bid a SHARE of the edge: at most `tip_share` of what the fill is
    expected to make, spread over the gas it burns, capped at `tip_max_gwei`. A FCFS chain gets the floor
    tip - robinhood charges every tx in a block one price, so a tip there is money for nothing."""
    if getattr(chain, 'ordering', 'fcfs') != 'priority' or expected_usd <= 0 or chain.tip_share <= 0:
        return MIN_TIP
    per_gas_usd = chain.tip_share * expected_usd / float(units or chain.gas_units)
    # A4 (docs/TODO-coverage.md): THE TIP'S WORST CASE IN DOLLARS. A priority tip is paid on a revert too, and a big
    # modelled edge bid big: arc's +$85.97 route lost its race and its revert cost $2.67 (2026-09-26). `tip_max_usd`
    # caps tip x the route's gas, so a lost race costs at most that much in tip.
    cap_usd = float(getattr(chain, 'tip_max_usd', 0) or 0)
    if cap_usd > 0:
        per_gas_usd = min(per_gas_usd, cap_usd / float(units or chain.gas_units))
    wei = int(per_gas_usd / chain.eth_usd * 10 ** chain.native_dec)
    return max(MIN_TIP, min(wei, int(chain.tip_max_gwei * 10 ** 9)))


def tip_usd(chain, tip, units=None):
    return (units or chain.gas_units) * tip / 10 ** chain.native_dec * chain.eth_usd


def receipt(chain, h, timing=None, tries=80):
    """Poll for a receipt, 0.5 s apart; None after `tries`."""
    for _ in range(tries):
        r = chain.node.rpc('eth_getTransactionReceipt', [h])
        if r:
            if timing is not None and timing.get('t_submitted'):
                timing['receipt_ms'] = round((time.time() - timing['t_submitted']) * 1e3, 1)
            return r
        time.sleep(0.5)
    print('no receipt after %.0fs' % (tries * 0.5))
    return None


def send(chain, ac, tx, go, gas_floor=None, tip=MIN_TIP, timing=None, wait=True):
    """DRY unless go AND LIVE_OK=yes AND balance clears (gas cost + gas_floor). Returns the receipt dict,
    or None. Never signs or hits the network in a dry run."""
    from . import wallet
    h0 = getattr(chain, 'head', None) or {}
    fresh_head = chain.gas_mode != 'flat' and h0.get('basefee') and time.time() - h0.get('t', 0) < getattr(chain, 'HEAD_TTL', 2.0)
    gp = h0['basefee'] if fresh_head else gas_price(chain)   # the newHeads basefee we already hold, else one RPC
    gas_usd = tx.get('gas', 400_000) * (gp + tip) / 1e18 * chain.eth_usd
    if not go:
        print('DRY RUN (~$%.4f gas). add --go to send.' % gas_usd)
        return None
    if os.environ.get('LIVE_OK') != 'yes':
        print('REFUSED: --go given but LIVE_OK != yes')
        return None
    floor = chain.refuel.get('gas_floor', 0.0) if gas_floor is None else gas_floor
    prep = _PREP.get((chain.chain_id, ac.address.lower())) or {}
    bal = prep['bal'] if prep.get('bal') is not None and time.time() - prep.get('bal_t', 0) < 10.0 else wallet.usd_balance(chain, ac.address)
    need = gas_usd + floor
    if bal < need:
        print('REFUSED: %s has %.4f, need %.4f (gas %.4f + floor %.4f)' % (ac.address, bal, need, gas_usd, floor))
        return None
    if tx.get('to'):
        # the signer refuses a lower-case `to`, and receipts hand contract addresses back lower-case: the
        # first shot rehearsal on a fork (2026-09-19) had its gate PASS and then could not be signed.
        # Normalised HERE because every send path - shared, shot, sweep, refuel - comes through this line.
        from eth_utils import to_checksum_address
        tx = dict(tx, to=to_checksum_address(tx['to']))
    with _SEND_LOCK:
        # NO RETRY ON `nonce too low`, deliberately. The tempting fix is to re-read and re-broadcast, and
        # it is wrong: the refusal usually means a tx of OURS already took that nonce, so re-signing the
        # same calldata one nonce up sends the SAME TRADE TWICE. 2026-09-22 is the receipt - two SHOT FIRE
        # lines on 0x9eebbe87 from the same shot, the first landing (+0.6050) and the second refused. A
        # retry would have duplicated it. The nonce is read from the SEND node instead (see _next_nonce),
        # which is what actually removes the race, and a refusal loses one route rather than risking two.
        t_sign = time.time()
        # HOLD THE INTERPRETER FOR SIGN + SUBMIT: signing is ~1 ms on the AWS box, but the desk read 2.1 ms p50 / 6.8 p90 /
        # 31.6 max - the prober, the pass and the writer threads took the lock mid-sign at the 1 ms switch interval
        # (2026-09-26). For these few ms nobody else runs; the interval is restored before the network wait
        _sw = sys.getswitchinterval()
        sys.setswitchinterval(0.005)   # 5 ms: coincurve and keccak release the lock mid-sign, and a thread that takes it then may keep it for the whole interval (a 50 ms setting read one 15.3 ms sign)
        try:
            full = dict(tx, chainId=chain.chain_id,
                        nonce=_next_nonce(chain, ac.address),
                        maxFeePerGas=max_fee(chain, gp) + tip, maxPriorityFeePerGas=int(tip))
            raw = ac.sign_transaction(full).raw_transaction.hex()
        except BaseException:
            sys.setswitchinterval(_sw)
            raise
        raw = raw if raw.startswith('0x') else '0x' + raw
        t_sub = time.time()
        # IN PARALLEL: the send node plus the chain's `broadcast` endpoints (rpc.toml), first acceptance wins. One
        # signed tx, one nonce: a copy arriving second is refused as 'already known', never sent twice. In order,
        # a slow or refusing first endpoint cost the whole round trip before the next one was tried.
        ps = pushers(chain)
        urls = [p.url for p in ps]
        res, done, lk = {}, threading.Event(), threading.Lock()

        def cb(url, h, e):
            with lk:
                if h is not None:
                    res.setdefault('h', (h, url, time.time()))
                    done.set()
                else:
                    res.setdefault('err', {})[url] = e
                    if len(res['err']) == len(urls):
                        done.set()
        for p in ps:
            p.push(raw, cb)
        sys.setswitchinterval(_sw)
        done.wait(timeout=30)
        if 'h' not in res and any(k in str(e).lower() for e in (res.get('err') or {}).values() for k in ('deadline', 'timed out', 'timeout', 'already known')):
            # A SEND THAT TIMED OUT CAN STILL HAVE LANDED (2026-10-02 16:38, the copy desk's mint: blockmachine answered
            # 'context deadline exceeded' while the tx went in, and the four other endpoints then refused it as 'nonce too
            # low' - the nonce THIS tx had just used). Before calling it failed, ask the chain for THIS signed tx by its own
            # hash. Nothing is re-signed or re-sent (the rule above stands): either our tx is there, or the send failed.
            from eth_hash.auto import keccak
            mine = '0x' + keccak(bytes.fromhex(raw[2:])).hex()
            for _ in range(10):
                try:
                    if chain.node.rpc('eth_getTransactionByHash', [mine]):
                        res['h'] = (mine, 'found by its hash after a timeout', time.time())
                        print('   send timed out but the tx is on chain: %s' % mine)
                        break
                except Exception:
                    pass
                time.sleep(0.5)
        if 'h' not in res:
            for u, e in (res.get('err') or {}).items():
                print('   send refused by %s: %s' % (u.split('//')[-1].split('/')[0], str(e)[:90]))
            forget_nonce(chain, ac.address)
            raise next(iter((res.get('err') or {'none': RuntimeError('no endpoint answered the send')}).values()))
        h, url, t_ok = res['h']
        if timing is not None:
            # sign = nonce (warm) + signing; submit = the first acceptance; t_submitted = epoch
            timing.update(sign_ms=round((t_sub - t_sign) * 1e3, 1), submit_ms=round((t_ok - t_sub) * 1e3, 1),
                          t_submitted=round(t_ok, 3), send_url=url.split('//')[-1].split('/')[0],
                          nonce=full['nonce'], gas_limit=int(full.get('gas') or 0),
                          max_fee_gwei=round(full['maxFeePerGas'] / 1e9, 4), tip_gwei=round(full['maxPriorityFeePerGas'] / 1e9, 4),
                          send_head_blk=(getattr(chain, 'head', None) or {}).get('number'))
    print('tx', h)
    if not wait:
        return dict(transactionHash=h, pending=True)   # the caller settles later (runner._settle_bg)
    return receipt(chain, h, timing)


def decode_revert(data):
    """(signature, [args as str]) for a CycleArb custom-error payload."""
    data = data or ''
    sig = ERRS.get(data[:10], data[:10] or '(no data)')
    args = []
    if data[:10] in ERRS and '(' in sig and sig[sig.index('(') + 1:-1]:
        types = sig[sig.index('(') + 1:-1].split(',')
        try:
            args = ['0x' + v.hex() if isinstance(v, bytes) else str(v) for v in abi_decode(types, bytes.fromhex(data[10:]))]
        except Exception:
            pass
    return sig, args


def revert_raw(exc):
    """The raw revert payload hex out of an eth_call exception, or None."""
    import re
    m = re.search(r"'data': '(0x[0-9a-fA-F]+)", str(exc))
    return m.group(1) if m else None


def revert_from_exc(exc):
    """(sig, args) from an eth_call revert exception, or (None, [])."""
    raw = revert_raw(exc)
    return decode_revert(raw) if raw else (None, [])


def explain(chain, txh):
    """Block/idx/status/gasUsed and, for a revert, the decoded reason of a LANDED tx.

    The TRACE first (debug_traceTransaction callTracer; robinhood's drpc serves it): the revert payload of the tx as it
    ran, at its index in its block. The eth_call replay is only a fallback, at the PARENT block: replaying at the
    landing block reads that block's FINAL state, where a route that lost to an earlier tx passes - every robinhood
    revert card read "why unknown" that way (FloorNotMet 0x10da0f2b, 2026-09-25), and an unknown reason on a
    non-fresh route is what the permanent A6 gate keys on."""
    tx = chain.node.rpc('eth_getTransactionByHash', [txh])
    rc = chain.node.rpc('eth_getTransactionReceipt', [txh])
    o = dict(block=int(tx['blockNumber'], 16), idx=int(rc['transactionIndex'], 16),
             status=int(rc['status'], 16), gasUsed=int(rc['gasUsed'], 16), reason=None, args=[])
    if o['status'] == 1:
        return o
    for u in list(dict.fromkeys(list(chain.archive_urls) + list(getattr(chain, 'rpc_urls', None) or chain.cfg['rpc'].get('rotate', [])))):
        try:
            tr = chain.node.rpc('debug_traceTransaction', [txh, {'tracer': 'callTracer'}], url=u)
        except Exception:
            continue
        out = (tr or {}).get('output') or ''
        if len(out) >= 10:
            o['reason'], o['args'] = decode_revert(out)
            o['via'] = 'trace'
            return o
    last = None
    for u in chain.archive_urls:
        try:
            chain.node.rpc('eth_call', [{'from': tx['from'], 'to': tx['to'], 'data': tx['input'],
                                         'value': tx['value']}, hex(int(tx['blockNumber'], 16) - 1)], url=u)
            return o
        except Exception as e:
            last = e; break
    err = last.args[0] if last and last.args and isinstance(last.args[0], dict) else {}
    o['reason'], o['args'] = decode_revert(err.get('data', '') or '')
    return o


def _selftest():
    n = [0]
    def A(c, m=''):
        assert c, m
        n[0] += 1

    # error selectors are stable and decode their args
    A(ERRS[_sel('LegShort(uint256,uint256,uint256)')] == 'LegShort(uint256,uint256,uint256)')
    from eth_abi import encode as abi_encode
    payload = _sel('FloorNotMet(uint256,uint256)') + abi_encode(['uint256', 'uint256'], [5, 9]).hex()
    sig, args = decode_revert(payload)
    A(sig.startswith('FloorNotMet') and args == ['5', '9'])
    A(decode_revert('0xdeadbeef') == ('0xdeadbeef', []), 'unknown selector passes through')
    s, a = revert_from_exc("RuntimeError({'code': 3, 'data': '%s'})" % payload)
    A(s.startswith('FloorNotMet') and a == ['5', '9'], 'decode from an exception string')
    w = _sel('WrappedError(address,bytes4,bytes,bytes)') + abi_encode(['address', 'bytes4', 'bytes', 'bytes'],
        ['0x' + '11' * 20, b'\x57\x5e\x24\xb4', bytes.fromhex('deadbeef'), b'']).hex()
    s, a = decode_revert(w)
    A(s.startswith('WrappedError') and a[2] == '0xdeadbeef', 'hook revert unwrapped: %r' % a)

    # send is DRY-safe: no go -> returns None, touches no network
    class C:
        gas_mode = 'flat'; gwei = 20; eth_usd = 1.0; chain_id = 5042
        refuel = {'gas_floor': 2.0}
        class node:
            @staticmethod
            def rpc(*a, **k): raise AssertionError('dry send must not hit the network')
    A(send(C(), None, {'to': '0x0', 'data': '0x', 'gas': 100000}, go=False) is None, 'dry send is a no-op')

    # the nonce never goes backwards inside one process, even when the node's pending pool lags
    class N:
        chain_id = 4663
        class node:
            answers = ['0x5', '0x5', '0x9']
            @classmethod
            def rpc(cls, *a, **k): return cls.answers.pop(0)
    A([_next_nonce(N, '0xabc'), _next_nonce(N, '0xabc'), _next_nonce(N, '0xabc')] == [5, 6, 9],
      'a lagging pending nonce is bumped, a jump forward is taken')
    forget_nonce(N, '0xabc')
    A((N.chain_id, '0xabc') not in _NONCE, 'a failed send forgets it')

    # THE NONCE COMES FROM THE SEND NODE, and a `nonce too low` refusal is NOT retried: re-signing the
    # same calldata one nonce up is how one trade becomes two. 2026-09-22 receipts, both in this block.
    import os as _os
    class _Acct:
        address = '0x' + 'ab' * 20
        seen = []
        @staticmethod
        def sign_transaction(full):
            _Acct.seen.append(full['nonce'])
            return type('S', (), {'raw_transaction': b'\x01'})
    class R:
        chain_id = 4663; eth_usd = 1.0; native_dec = 18; gas_token = 'eth'
        ordering = 'fcfs'; gas_units = 420_000; send_urls = ['http://send']
        refuel = {'gas_floor': 0.0}
        gas_mode, gwei = 'flat', 1
        class node:
            sends = 0
            @classmethod
            def rpc(cls, method, params, url=None):
                if method == 'eth_getTransactionCount':
                    return '0x3' if url == 'http://send' else '0x2'   # the rotate pool lags by one
                if method == 'eth_sendRawTransaction':
                    cls.sends += 1
                    raise RuntimeError("{'code': -32000, 'message': 'nonce too low: tx: 3 state: 4'}")
                raise AssertionError(method)
            @staticmethod
            def getbalance(a, b='latest'): return 10 ** 18
    _NONCE.pop((R.chain_id, _Acct.address.lower()), None)
    _live = _os.environ.get('LIVE_OK'); _os.environ['LIVE_OK'] = 'yes'
    try:
        send(R, _Acct, {'to': '0x' + 'cd' * 20, 'data': '0x', 'gas': 100000}, go=True)
        A(False, 'a refused send must raise, not return quietly')
    except RuntimeError:
        A(True)
    finally:
        _os.environ.pop('LIVE_OK', None)
        if _live is not None:
            _os.environ['LIVE_OK'] = _live
    A(_Acct.seen == [3], 'the nonce is the SEND node\'s 3, not the rotate pool\'s 2')
    A(R.node.sends == 1, 'ONE broadcast: a nonce refusal is never retried, or one trade becomes two')
    A((R.chain_id, _Acct.address.lower()) not in _NONCE, 'and the remembered nonce is dropped')

    # a send that TIMED OUT but landed: found by its own hash, not re-sent, not reported as a failure
    class R2(R):
        chain_id = 4664; send_urls = ['http://send2']                # its own pusher (they are cached per chain and url)
        class node:
            sends = 0
            @classmethod
            def rpc(cls, method, params, url=None):
                if method == 'eth_getTransactionCount':
                    return '0x7'
                if method == 'eth_sendRawTransaction':
                    cls.sends += 1
                    raise RuntimeError("{'code': -32000, 'message': 'context deadline exceeded'}")
                if method == 'eth_getTransactionByHash':
                    return {'hash': params[0]}
                raise AssertionError(method)
            @staticmethod
            def getbalance(a, b='latest'): return 10 ** 18
    from eth_hash.auto import keccak as _k
    _NONCE.pop((R2.chain_id, _Acct.address.lower()), None)
    _os.environ['LIVE_OK'] = 'yes'
    try:
        out = send(R2, _Acct, {'to': '0x' + 'cd' * 20, 'data': '0x', 'gas': 100000}, go=True, wait=False)
    finally:
        _os.environ.pop('LIVE_OK', None)
        if _live is not None:
            _os.environ['LIVE_OK'] = _live
    A(out.get('transactionHash') == '0x' + _k(b'\x01').hex(), 'a timed-out send that landed is found by its own hash (%s)' % out)
    A(R2.node.sends == 1, 'and it was broadcast ONCE: nothing re-signed or re-sent')

    # the tip: a share of the edge on a PRIORITY chain, the floor on FCFS, never above the cap
    class P:
        ordering = 'priority'; tip_share = 0.3; tip_max_gwei = 250; gas_units = 420_000
        eth_usd = 1.0; native_dec = 18
    A(abs(tip_usd(P, tip_wei(P, 0.1)) - 0.03) < 1e-9, 'a $0.10 edge bids exactly 30%% of it')
    A(tip_wei(P, 50.0) == 250 * 10 ** 9, 'a big edge is capped at tip_max_gwei')
    A(tip_wei(P, -1.0) == MIN_TIP, 'no edge, no bid')
    # A4: tip_max_usd bounds the tip's worst case (tip x the route's gas) whatever the edge
    P.tip_max_gwei, P.tip_max_usd = 10 ** 6, 0.30
    A(abs(tip_usd(P, tip_wei(P, 86.0, units=1_000_000), units=1_000_000) - 0.30) < 1e-6, 'an $86 edge still risks only $0.30 of tip')
    A(abs(tip_usd(P, tip_wei(P, 0.5)) - 0.15) < 1e-9, 'under the cap the share rules')
    P.tip_max_usd = 0
    P.ordering = 'fcfs'
    A(tip_wei(P, 50.0) == MIN_TIP, 'FCFS: a tip buys nothing, so none is paid')
    print('tx selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())

