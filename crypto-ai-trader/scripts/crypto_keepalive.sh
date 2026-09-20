#!/bin/bash
# Self-locating: script lives in <repo>/scripts/, repo root is parent.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# Phase 1: one-shot bootstrap (idempotent via marker). The bootstrap
# script lives in this repo; its credentials (git URL, .env) are read
# from /root credential files at runtime — never embedded in the repo.
if [ ! -f /root/.crypto_bootstrapped ] && [ -f "$REPO/scripts/setup_crypto.sh" ]; then
  if bash "$REPO/scripts/setup_crypto.sh" >> "$REPO/logs/setup_crypto.log" 2>&1; then
    touch /root/.crypto_bootstrapped
  fi
fi
# Phase 2: sing-box keepalive (every minute)
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 -x http://127.0.0.1:17890 https://api.github.com 2>/dev/null)
if [ "$code" == "000" ] || [ -z "$code" ]; then
  pkill -x sing-box 2>/dev/null; sleep 1
  nohup /usr/local/bin/sing-box run -c /etc/sing-box/config.json >>"$REPO/logs/singbox.log" 2>&1 &
  echo "$(date) sing-box restarted" >> "$REPO/logs/singbox_keepalive.log"
fi
