#!/bin/sh
# One command for a fresh box: the venv, the packages, then `./lpdesk setup` (config, wallet, Telegram, cron, doctor).
#   ./setup.sh                   asks what it cannot know
#   ./setup.sh --yes --cron      no questions (a new wallet if there is none, no Telegram), the schedule installed
set -e
D=$(cd "$(dirname "$0")" && pwd)
cd "$D"
command -v python3 >/dev/null || { echo "python3 (3.11+) is needed"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "python 3.11+ is needed (tomllib)"; exit 1; }
[ -x .venv/bin/python ] || python3 -m venv .venv || { echo "python3-venv is missing: sudo apt install python3-venv"; exit 1; }
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
exec ./lpdesk setup "$@"
