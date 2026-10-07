"""Fork helpers for the desk's tests (from hook_bot's tests/fork.py and tests/lpfork.py): anvil forks the chain at head on a
PRIVATE chain id (real + 1,000,000), every url in the fork's config is 127.0.0.1, and every wallet it names gets a throwaway key,
so nothing a test signs can ever reach the real chain. Needs foundry's anvil (ANVIL, default ~/.foundry/bin/anvil)."""
import json, os, re, shutil, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ANVIL = os.path.expanduser(os.environ.get('ANVIL', '~/.foundry/bin/anvil'))


def _rpc(url, method, params):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    r = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    d = json.load(urllib.request.urlopen(r, timeout=60))
    if 'error' in d:
        raise RuntimeError(d['error'])
    return d['result']


def fork_source(real):
    """First endpoint that answers eth_chainId with the real id."""
    for u in [real.rpc_url] + list(real.rotate) + list(real.archive_urls):
        try:
            if int(_rpc(u, 'eth_chainId', []), 16) == real.chain_id:
                return u
        except Exception:
            continue
    raise SystemExit('no endpoint to fork %s from' % real.name)


def fork_dir(real, port, name='lpfork'):
    d = os.path.join(real.var, name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(os.path.join(d, 'var'))
    t = open(os.path.join(real.dir, 'chain.toml')).read()
    t = t.replace('name = "%s"' % real.name, 'name = "%s-%s"' % (real.name, name), 1)
    t = t.replace('chain_id = %d' % real.chain_id, 'chain_id = %d' % (real.chain_id + 1_000_000), 1)
    t = re.sub(r'^tg_config = .*$', 'tg_config = "%s/no-telegram.json"' % d, t, flags=re.M)
    t = re.sub(r'^config_dir = .*$', 'config_dir = "%s"' % d, t, flags=re.M)
    # THROWAWAY KEYS: the fork copies mainnet, and the real lp-1 / caller hold real balances and positions there,
    # which a test must neither inherit nor sign for. Every wallet the fork's config names gets a fresh key.
    from eth_account import Account
    names = {real.caller, real.deployer, real.cfg.get('lp', {}).get('wallet', 'lp-1')}
    wf = os.path.join(d, 'wallets.json')
    keys = {n: (lambda a: dict(address=a.address, key=a.key.hex()))(Account.create()) for n in names}
    json.dump(keys, open(wf, 'w'))
    os.chmod(wf, 0o600)
    t = re.sub(r'^file = .*wallets.*$', 'file = "%s"' % wf, t, count=1, flags=re.M)
    open(os.path.join(d, 'chain.toml'), 'w').write(t)
    u = 'http://127.0.0.1:%d' % port
    open(os.path.join(d, 'rpc.toml'), 'w').write(
        'public = "%s"\nrotate = ["%s"]\nsend_urls = ["%s"]\narchive_urls = ["%s"]\nws = []\n' % (u, u, u, u))
    return os.path.join(d, 'chain.toml')


def start(real, slot, name='lpfork'):
    """(anvil, url, fork chain, toml path): a fork of `real` at head on a private chain id, every url local.
    The sequencer's own node is the source: the rotate pool's load-balanced backends answer a lazy fork's storage
    reads with 'historical state ... not available' when one of them is pruned (2026-09-24)."""
    from core import chain as cm
    port = 18545 + (real.chain_id % 1000) + slot
    cpath = fork_dir(real, port, name)
    src = os.environ.get('LPFORK_URL') or next((x for x in real.send_urls), None) or fork_source(real)
    bf = int(_rpc(src, 'eth_getBlockByNumber', ['latest', False]).get('baseFeePerGas') or '0x0', 16)
    a = subprocess.Popen([ANVIL, '--fork-url', src, '--chain-id', str(real.chain_id + 1_000_000),
                          '--block-base-fee-per-gas', str(bf), '--gas-price', str(bf), '--port', str(port),
                          '--accounts', '1', '--retries', '20', '--fork-retry-backoff', '500', '--silent'],
                         stdout=open(os.path.join(os.path.dirname(cpath), 'anvil.log'), 'w'), stderr=subprocess.STDOUT)
    u = 'http://127.0.0.1:%d' % port
    for _ in range(60):
        try:
            _rpc(u, 'eth_blockNumber', []); break
        except Exception:
            time.sleep(1)
    _rpc(u, 'evm_mine', [])            # an Orbit header has no blob fields; one local block carries them
    fork = cm.load(cpath)
    assert all('127.0.0.1' in x for x in [fork.rpc_url] + list(fork.send_urls) + list(fork.archive_urls)), \
        'refusing: the fork chain has a non-local url'
    return a, u, fork, cpath


def wait(u, h):
    for _ in range(60):
        rc = _rpc(u, 'eth_getTransactionReceipt', [h])
        if rc:
            return rc
        time.sleep(0.5)
    return None


def fund_usdg(u, fork, addr, amt):
    """Move `amt` raw USDG to `addr` by impersonating the PoolManager (fork only). True when it landed."""
    from core.exec import lp
    from eth_abi import encode
    usdg = lp.usdg(fork)
    _rpc(u, 'anvil_setBalance', [fork.pm, hex(10 ** 18)])
    _rpc(u, 'anvil_impersonateAccount', [fork.pm])
    b0 = lp.balance(fork, usdg, addr)
    h = _rpc(u, 'eth_sendTransaction', [{'from': fork.pm, 'to': usdg, 'data': '0xa9059cbb' + encode(['address', 'uint256'], [addr, amt]).hex()}])
    _rpc(u, 'anvil_stopImpersonatingAccount', [fork.pm])
    rc = wait(u, h)
    return bool(rc and int(rc['status'], 16) == 1 and lp.balance(fork, usdg, addr) - b0 == amt)
