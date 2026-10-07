"""THE ROUTE'S OWN STORY, stage by stage, from the gate's pick to the fill. var/trace.jsonl.

The tape says what happened to the desk (a fill, a revert, a heartbeat); this says what happened to ONE
route and why: which pass picked it and which rule skipped it, which gate asked about it at what gas and
floor, which leg came back short by how much, what the learner took from it, why a passed gate still did
not send, the tx, and the receipt. Every record carries the same `rid` - a hash of the route's shape, size
and block - so `hookbot <chain> trace <rid|pool|tx>` prints one route's whole path.

Why it exists: robinhood 2026-09-23/24 made 0 fills for ten hours while a +$61 synth phantom held a gate slot
every pass, and the console said only 'gate refused (0x8674c1c5 synth): LegShort' - not which leg (the
bridge, a pool a different learner owned), not by how much (-2,504 bps). One record would have named it.

EVERY RECORD CARRIES ITS CLOCK, so latency is read, not guessed:
  head_blk / head_ts      the block the pass priced on, and that block's own timestamp
  block_age_ms            now - head_ts: how old the chain state was when this stage ran
  head_seen_age_ms        now - when WE received that head
  trigger                 what woke the pass (swap/modify/tick/l1...), ev_blk/ev_tx the newest pool event in it
  since_ev_ms             now - when that event reached us: the reaction time, event -> this stage
  since_pass_ms           now - the start of the pass (drain + pricing + gates so far)
  queue_ms                the newest event's wait between socket arrival and the loop dequeuing it (a long pass before)
  drain_ms / tick_ms / sync_ms   the pass before pricing: socket drain, the poll_s tick read, refetch/resync
  pre_ms                  poll() before pricing: eth_usd refresh, refuel, shot stock
  price_ms                enumeration + pricing of every route this pass (on every record of the pass)
  beat_ms / cand_ms       after pricing: heartbeat + gate alarm, then picking the gate candidates
  shot_ms                 shot routes' gates and sends, which run inline BEFORE the shared candidates
  prefetch_ms / prefetch_n   the parallel gate batch's wall time (pins reads + eth_calls) and its size
plus per-stage: gate call_ms; send sign_ms / submit_ms / receipt_ms / send_url; settle landed_blk /
landed_idx / landed_ts / blocks_after_gate / s_submit_to_land and `race`: every OTHER swap on the route's
pools from the gate's block to ours, ordered, marked before/after us.

Stages (`stage` field): gate | decide | send | settle, plus boot (start-up phases: init_ms, fetch_ms,
subscribe_ms, total_ms, then first_pass_done_ms) and rpc (any single RPC attempt over rpc.SLOW_S or failing:
method, url, ms, body_bytes, outcome). Never raises: a trace that cannot be
written costs the record, never the pass. Rolled at MAX_BYTES to trace.jsonl.1 (one generation kept).
"""
import hashlib, json, os, time

MAX_BYTES = 64 * 1024 * 1024


def rid(r, blk=None):
    """Stable id for one route in one pass: strategy, legs (pool, direction), size, block."""
    legs = ','.join('%s%d' % (lg.pool.id[:18], 1 if lg.zfo else 0) for lg in r.legs)
    s = '%s|%s|%d|%s' % (r.strategy, legs, int(r.amount_in), blk)
    return hashlib.blake2s(s.encode(), digest_size=5).hexdigest()


def route_fields(chain, r):
    """What a route IS, once per record: enough to read a line without joining anything."""
    try:
        size = round(chain.to_usd(r.amount_in, r.start), 2)
    except Exception:
        size = None
    st = getattr(r, 'state', None) or [()] * len(r.legs)
    legs = []
    for lg, s in zip(r.legs, st):
        d = dict(pool=lg.pool.id[:18], hook=(lg.pool.hook or '')[:10], zfo=bool(lg.zfo), v=lg.pool.v,
                 skim=None if lg.skim is None else round(lg.skim, 2))
        if s:
            d.update(at=s[0], tick=s[1], lf=s[3])       # the block this leg's state was at when the model priced it
        legs.append(d)
    out = dict(strategy=r.strategy, sym=r.sym, pool=r.pool, twin=r.twin, size_usd=size,
               net_usd=round(r.net_usd, 4), gross_usd=round(r.gross_usd, 4),
               need_gas=r.need.gas, fresh=r.need.fresh_sender, legs=legs)
    ats = [s[0] for s in st if s and s[0]]
    head = (getattr(chain, 'head', None) or {}).get('number')
    if ats:
        out['state_blk'] = min(ats)                     # the OLDEST leg state the price stood on
        if head:
            out['state_lag_blk'] = head - min(ats)      # how many blocks behind the head that was
    return out


class Trace:
    def __init__(self, chain, store):
        self.chain = chain
        self.path = store.path('trace.jsonl')
        self.ctx = {}          # the pass's clock (runner.poll sets it): t_pass, trigger, ev_*, price_ms
        import threading
        self._tl = threading.local()   # pin(): a background settle writes with ITS pass's clock and block

    def pin(self, ctx, blk):
        """Pin this thread's records to a pass's context and block (runner's background settle), None to unpin."""
        self._tl.ctx, self._tl.blk = ctx, blk

    def clock(self):
        """The timing fields every record carries, computed at the moment of the record."""
        now = time.time()
        h = getattr(self.chain, 'head', None) or {}
        c = getattr(self._tl, 'ctx', None) or self.ctx
        out = dict(head_blk=h.get('number'), head_ts=h.get('ts'))
        if h.get('ts'):
            out['block_age_ms'] = round((now - h['ts']) * 1e3)
        if h.get('t'):
            out['head_seen_age_ms'] = round((now - h['t']) * 1e3, 1)
        if c.get('t_pass'):
            out['since_pass_ms'] = round((now - c['t_pass']) * 1e3, 1)
        if c.get('ev_recv'):
            out['since_ev_ms'] = round((now - c['ev_recv']) * 1e3, 1)
        for k in ('trigger', 'ev_kind', 'ev_blk', 'ev_tx', 'ev_pool', 'ev_n', 'queue_ms', 'drain_ms', 'tick_ms', 'sync_ms', 'pre_ms',
                  'price_ms', 'price_cpu_ms', 'beat_ms', 'shot_ms', 'cand_ms', 'prefetch_ms', 'prefetch_n', 'source', 'first_pass'):
            if c.get(k) is not None:
                out[k] = c[k]
        return out

    def blk(self):
        pinned = getattr(self._tl, 'blk', None)
        return pinned if pinned is not None else (getattr(self.chain, 'head', None) or {}).get('number')

    def __call__(self, stage, r=None, full=False, **kw):
        """One record. `full` adds the route's own fields (the first record of a route in a pass)."""
        try:
            b = self.blk()
            row = dict(ts=round(time.time(), 3), blk=b, stage=stage)
            row.update(self.clock())
            if r is not None:
                row['rid'] = rid(r, b)
                row.update(route_fields(self.chain, r) if full else
                           dict(strategy=r.strategy, sym=r.sym, pool=r.pool, net_usd=round(r.net_usd, 4)))
            row.update(kw)
            if getattr(self, 'async_write', False):
                self._put(row)
            else:
                self._write([row])
            return row
        except Exception:
            return None

    def _write(self, rows):
        try:
            if os.path.getsize(self.path) > MAX_BYTES:
                os.replace(self.path, self.path + '.1')
        except OSError:
            pass
        with open(self.path, 'a') as f:
            f.write(''.join(json.dumps(x, default=str) + '\n' for x in rows if x is not None))

    def _put(self, row):
        """OFF THE HOT PATH (a live desk sets `async_write`; tools and tests write synchronously): the row is built here, serialised and appended by one writer thread. A send used to wait
        for its gate and decide records to be dumped and written (~18 ms between a fast gate's eth_call and its record,
        2026-09-26). Order is kept (one queue, one writer); `flush()` drains it."""
        import queue, threading
        if getattr(self, '_q', None) is None:
            self._q = queue.Queue()

            def writer():
                while True:
                    rows = [self._q.get()]
                    while not self._q.empty() and len(rows) < 500:
                        rows.append(self._q.get_nowait())
                    try:
                        self._write(rows)
                    except Exception:
                        pass
                    for _ in rows:
                        self._q.task_done()
            threading.Thread(target=writer, name='trace-writer', daemon=True).start()
            import atexit
            atexit.register(self.flush)                   # a short-lived command (lp, --once) never exits with rows queued
        self._q.put(row)

    def flush(self):
        """Wait until every queued row is on disk (tests, and a desk that is about to exit)."""
        if getattr(self, '_q', None) is not None:
            self._q.join()


def read(path, key=None, limit=200):
    """The records whose rid/pool/sym/tx starts with `key` (every record if None), whole routes kept
    together: a key that matches one record of a route pulls in that route's other stages too."""
    rows = []
    for p in (path + '.1', path):
        try:
            for line in open(p):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
    if key:
        k = key.lower()
        hit = {r.get('rid') for r in rows
               if any(str(r.get(f) or '').lower().startswith(k) for f in ('rid', 'pool', 'sym', 'tx', 'twin'))}
        rows = [r for r in rows if r.get('rid') in hit or (r.get('rid') is None and
                any(str(r.get(f) or '').lower().startswith(k) for f in ('pool', 'sym', 'tx')))]
    return rows[-limit:]


def _selftest():
    import tempfile
    n = [0]

    def A(c, m=''):
        assert c, m
        n[0] += 1
    from core.model import Pool, Leg, Route, Need, NAT

    class C:
        head = {'number': 7}
        def to_usd(self, a, t): return a / 1e6

    class S:
        def __init__(self, d): self.d = d
        def path(self, b): return os.path.join(self.d, b)
    d = tempfile.mkdtemp()
    h = Pool('0x' + 'aa' * 32, '0x' + '11' * 20, '0x' + '22' * 20, 0, 1, '0x' + 'cd' * 20, 4)
    t = Pool('0x' + 'bb' * 32, '0x' + '11' * 20, '0x' + '22' * 20, 100, 1, NAT, 4)
    r = Route('twin', [Leg(h, True, -12.0), Leg(t, False, 0.0)], '0x' + '11' * 20, 50_000_000, [1, 2], 0.2, 0.1,
              Need(), sym='X', pool=h.id, twin=t.id)
    tr = Trace(C(), S(d))
    a = tr('pick', r, full=True, rank=0)
    b = tr('gate', r, result='refused', reason='LegShort', leg=1, short_bps=2504.0)
    A(a['rid'] == b['rid'] and a['size_usd'] == 50.0 and a['legs'][0]['skim'] == -12.0, 'one rid through the stages')
    C.head = {'number': 8}
    A(tr('pick', r)['rid'] != a['rid'], 'the next block is a new route')
    tr('settle', None, tx='0xfeed', result='fill')
    tr.flush()                                           # the writer thread: rows land on disk asynchronously
    A([x['stage'] for x in read(tr.path, a['rid'])] == ['pick', 'gate'], 'read by rid returns that route only')
    A(len(read(tr.path, h.id[:10])) == 3 and read(tr.path, '0xfeed')[0]['result'] == 'fill', 'by pool, by tx')
    A(tr('x', object()) is None, 'a bad route never raises')
    print('trace selftest OK: %d asserts' % n[0])
    return 0


if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(_selftest())
