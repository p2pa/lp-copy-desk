#!/bin/sh
# Put this desk on a server - an AWS box near the chain's sequencer, or any Linux host you can ssh into - in one command.
#
#   ops/deploy.sh ubuntu@1.2.3.4                  code + config there, ./setup.sh --yes --cron there (a NEW wallet is made
#                                                 there unless one exists: the remote desk signs with its own key)
#   ops/deploy.sh ubuntu@1.2.3.4 --history        also the history store (chains/<chain>/var/lphist.db, ~100 MB a day) so the
#                                                 remote does not rebuild it for hours
#   ops/deploy.sh ubuntu@1.2.3.4 --move           MOVE the running desk: this box's wallet, book and open copies, history and
#                                                 Telegram go there; refuses while the desk still runs here (one signer per key)
#   ops/deploy.sh ubuntu@1.2.3.4 --live           and go live there (touch ops/LIVE: the remote supervisor starts the desk)
#   ops/deploy.sh local:/srv/lpdesk ...           the same into a local folder (a second install, or a dry run of the above)
#
# Code is pushed only when you run this (never automatically: a half-edited file must not reach a desk its supervisor
# restarts). Re-running it updates the code; the remote's own state (var/) is never overwritten unless --move.
# Env: LPDESK_DIR (remote folder, default lp-copy-desk in the remote home), LPDESK_CHAIN (default robinhood), SSH_OPTS.
set -e
HOST=$1; [ -n "$HOST" ] || { sed -n '2,19p' "$0"; exit 1; }
shift
HIST=0; MOVE=0; LIVE=0
for a in "$@"; do
  case $a in --history) HIST=1 ;; --move) MOVE=1; HIST=1 ;; --live) LIVE=1 ;; *) echo "unknown option $a"; exit 1 ;; esac
done
D=$(cd "$(dirname "$0")/.." && pwd)
CH=${LPDESK_CHAIN:-robinhood}
CONF=${LPDESK_CONF:-$HOME/.config/lp-copy-desk}
case $HOST in
  local:*) DIR=${HOST#local:}; DEST=$DIR; run() { (cd "$DIR" && sh -c "$1"); }; RCONF=$CONF ;;
  *)       DIR=${LPDESK_DIR:-lp-copy-desk}; DEST="$HOST:$DIR"; run() { ssh $SSH_OPTS "$HOST" "cd $DIR && $1"; }; RCONF='~/.config/lp-copy-desk'
           ssh $SSH_OPTS "$HOST" "mkdir -p $DIR" ;;
esac
if [ $MOVE = 1 ]; then
  if pgrep -f "$D/cli.py run --go" >/dev/null || [ -f "$D/ops/LIVE" ]; then
    echo "refusing to move: the desk still runs here. rm $D/ops/LIVE, kill its PID (SIGTERM is graceful), then re-run."; exit 1
  fi
fi
echo "== code -> $DEST"
rsync -az --exclude .git --exclude .venv --exclude '__pycache__' --exclude 'chains/*/var' --exclude ops/LIVE --exclude ops/PAUSE \
  "$D/" "$DEST/"
run "mkdir -p chains/$CH/var/lp"
if [ $HIST = 1 ]; then
  echo "== history store"
  rsync -az "$D/chains/$CH/var/lphist.db" "$DEST/chains/$CH/var/"
  [ -f "$D/chains/$CH/var/lp_heat.json" ] && rsync -az "$D/chains/$CH/var/lp_heat.json" "$DEST/chains/$CH/var/"
fi
if [ $MOVE = 1 ]; then
  echo "== the desk's book and open copies (var/lp), its wallet and Telegram"
  rsync -az "$D/chains/$CH/var/lp/" "$DEST/chains/$CH/var/lp/"
  case $HOST in
    local:*) ;;
    *) ssh $SSH_OPTS "$HOST" "mkdir -p ~/.config/lp-copy-desk && chmod 700 ~/.config/lp-copy-desk"
       for f in wallets.json telegram.json hypersync.token rpc.url; do
         [ -f "$CONF/$f" ] && scp -q $SSH_OPTS -p "$CONF/$f" "$HOST:.config/lp-copy-desk/$f" && ssh $SSH_OPTS "$HOST" "chmod 600 ~/.config/lp-copy-desk/$f"
       done ;;
  esac
fi
echo "== setup there"
run "./setup.sh --yes --cron"
[ $LIVE = 1 ] && { run "touch ops/LIVE"; echo "== LIVE: the remote supervisor starts the desk within a minute"; }
echo
echo "watch it:   ssh -t $HOST 'cd $DIR && ./lpdesk watch'      (or: ./lpdesk status --json over ssh)"
echo "stop it:    ssh $HOST 'cd $DIR && ./lpdesk stop'           (no new copies; open ones still exit)"
