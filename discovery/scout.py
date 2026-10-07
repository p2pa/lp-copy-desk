"""The one piece of hook_bot's chain scout the copy desk uses: a HyperSync request with its rate-limit handling
(discovery/forensics reads launch history through it). The token is a file, never in the repo."""
import json, os, time, urllib.request

HS_TOKEN = os.path.expanduser(os.environ.get('HYPERSYNC_TOKEN_FILE', '~/.config/lp-copy-desk/hypersync.token'))


class OverBudget(RuntimeError):
    pass


def _hs(url, key, body, deadline, tries=4):
    """One hypersync request: 60 s timeout, `tries` tries, never past the chain's deadline (seed._query allows
    180 s x 14 tries - right for a one-off genesis walk, a hang for a scout: BSC sat 20 min in it). A 429 (the
    token is shared with the desks' watchers: 30 requests/min) waits for the window the server names."""
    for a in range(tries):
        if deadline and time.time() > deadline:
            raise OverBudget('hypersync over the chain budget')
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(
                url, json.dumps(body).encode(), {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}),
                timeout=60))
        except OverBudget:
            raise
        except Exception as e:
            if a == tries - 1:
                raise
            if getattr(e, 'code', None) == 429:
                # the token's budget is 30 requests a minute, shared by every job on the box: wait out the window the
                # server names instead of a blind back-off that retried inside the same exhausted window
                try:
                    time.sleep(min(61, int(e.headers.get('x-ratelimit-reset')) + 1))
                except Exception:
                    time.sleep(5 * (a + 1))
            else:
                time.sleep(3 * (a + 1))
