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
  # WO-1009-⑤ (10/2): the host injects a resident proxy on 17890 — a
  # local `sing-box run` here can almost never bind that port (a FATAL
  # "address already in use" loop that bloated logs/singbox.log to 23MB)
  # and the every-minute pkill also raced the run_cron.sh ensure_proxy
  # takeover during real host-proxy outages (10/2 03:20-04:20). Alert
  # only; failover/takeover belongs to ensure_proxy at scan time.
  echo "$(date) WARN: proxy 17890 unreachable (github probe code=$code) — alert-only, takeover belongs to run_cron.sh ensure_proxy" >> "$REPO/logs/singbox_keepalive.log"
fi
