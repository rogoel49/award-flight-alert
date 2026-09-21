#!/usr/bin/env bash
# Bootstrap award-alert-agent on THIS host as the single poller:
#   - create local config/alerts/.env from the committed examples (never overwrites)
#   - record this host as the designated poller (so a stray second host can't double-alert)
#   - install + load the launchd timer (every 10 min)
#   - list alerts and send a test email
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.awardalertagent.poll"
PLIST_SRC="$DIR/award-alert-agent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "== award-alert-agent setup =="

# 1. Python 3.12+
command -v python3 >/dev/null || { echo "ERROR: python3 not found (need 3.12+)"; exit 1; }

# 2. local config from examples (never clobber an existing file)
for pair in "config.example.json:config.json" "alerts.example.json:alerts.json"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  if [ ! -f "$DIR/$dst" ]; then cp "$DIR/$src" "$DIR/$dst"; echo "created $dst"; fi
done
if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  echo "created .env — edit it to add SEATS_AERO_API_KEY (+ AWARD_SMTP_PASSWORD for email)"
fi
chmod 600 "$DIR/.env" || true

# 3. designate this host as the poller (write via Python so it matches the guard's
#    socket.gethostname(), not the shell `hostname` which can differ short-vs-FQDN)
python3 -c 'import socket; print(socket.gethostname())' > "$DIR/.poll-host"
echo "recorded poll host: $(cat "$DIR/.poll-host")"

# 4. install the launchd agent (substitute repo dir + log path; plist carries no secrets).
#    Logs go OUTSIDE the repo so personal route/hit data never lands in the checkout.
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
LOG_PATH="$HOME/Library/Logs/award-alert-agent.log"
sed -e "s|__REPO_DIR__|$DIR|g" -e "s|__LOG_PATH__|$LOG_PATH|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "installed + loaded $LABEL (polls every 10 min)"

# 5. show alerts, then a test notification through each configured notify.channels
#    (the email channel needs AWARD_SMTP_PASSWORD in .env; macos needs nothing)
python3 "$DIR/manage.py" list || true
echo "sending a test notification through your configured channels..."
set -a; [ -f "$DIR/.env" ] && . "$DIR/.env"; set +a
python3 "$DIR/notify.py" --test || true

echo
echo "done. add an alert with:"
echo "  python3 $DIR/manage.py add --name \"SFO-Tokyo Nov\" --from SFO --to NRT,HND,KIX \\"
echo "      --cabin business --max-miles 90000 --min-seats 2 --start 2026-11-01 --end 2026-11-30"
