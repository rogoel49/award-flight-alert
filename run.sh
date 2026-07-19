#!/usr/bin/env bash
# One-shot poll: search + filter + dedupe (check.py), then email new hits (notify.py).
# Sources the gitignored .env first, because launchd does NOT inherit your shell
# environment. Fully deterministic — safe on any timer (launchd, cron).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load SEATS_AERO_API_KEY (+ AWARD_SMTP_PASSWORD) from the local .env if present.
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi

SUMMARY="$(python3 "$DIR/check.py")"
printf '%s\n' "$SUMMARY" | grep -E '^NEW_HITS:' || true

N="$(printf '%s\n' "$SUMMARY" | sed -n 's/^NEW_HITS: //p')"
if [ "${N:-0}" -gt 0 ]; then
  python3 "$DIR/notify.py"
fi
