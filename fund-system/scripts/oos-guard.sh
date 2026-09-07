#!/bin/bash
# Daily OOS window guard. Snapshot the workflow literals, compare to each open
# window, mark INVALIDATED + alert on divergence.
cd /root/fund-system
if [ "${OOS_GUARD_FORCE_FAILURE:-0}" = "1" ]; then
  echo '{"ok":false,"state":"INTENTIONAL_FAILURE_FIXTURE"}'
  exit 2
fi
PM2_NAME="${OOS_GUARD_PM2_NAME:-fund-system}"
PID=$(pm2 pid "$PM2_NAME" 2>/dev/null | tail -n 1 | tr -d '[:space:]')
if ! [[ "$PID" =~ ^[1-9][0-9]*$ ]] || [ ! -r "/proc/$PID/environ" ]; then
  printf '{"ok":false,"state":"TARGET_PROCESS_ABSENT","process":"%s"}\n' "$PM2_NAME"
  exit 2
fi
while IFS= read -r line; do
  case "$line" in DATABASE_URL=*|TELEGRAM_BOT_TOKEN=*|TELEGRAM_CHAT_ID=*) export "$line";; esac
done < <(tr "\0" "\n" < /proc/$PID/environ)
if [ -z "${DATABASE_URL:-}" ]; then
  echo '{"ok":false,"state":"DATABASE_URL_MISSING_FROM_TARGET_PROCESS"}'
  exit 2
fi
python3 scripts/oos-snapshot.py > /dev/null || exit 1
node scripts/oos-guard.mjs --apply
