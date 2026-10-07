#!/bin/sh
# Keep the live desk running (cron, every minute). `touch ops/PAUSE` stops restarts; the desk's own STOP file
# (`./lpdesk stop`) means NO NEW COPIES and is honoured by the running desk - it is not a reason to stop the process,
# because open copies must still exit with their leaders.
D=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$D/ops/PAUSE" ] && exit 0
pgrep -f "$D/cli.py run --go" >/dev/null && exit 0
mkdir -p "$D/chains/robinhood/var/lp"
LIVE_OK=yes nohup "$D/lpdesk" run --go >>"$D/chains/robinhood/var/lp/follow.log" 2>&1 &
