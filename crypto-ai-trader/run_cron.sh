#!/bin/bash
# Crypto AI Trader - Cron Wrapper
# Usage: run_cron.sh <subcommand> [args...]
set -euo pipefail

BASEDIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$BASEDIR/logs"
mkdir -p "$LOGDIR"

CMD="${1:?Usage: run_cron.sh <subcommand>}"
shift

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOGFILE="$LOGDIR/${CMD}.log"
# WO-1005 addendum-4: DRYRUN rounds log to a twin so live cron-scan.log
# stays pure-live for tail consumers (health_check freshness, bridge).
# Same convention as the reside_scan gate/live-dir/db twins (WO-1003-5).
if [ "${DRYRUN:-0}" = "1" ]; then
    LOGFILE="$LOGDIR/${CMD}_dryrun.log"
fi

# === Proxy Auto-Failover ===
PROXY_PORT=17890
CONFIG_FILE="/etc/sing-box/config.json"

# 节点列表：server:port (旧 shadowsocks 节点)
# 旧 SS 节点 2026-09-29 全部下线（cn02/cn01/164 端口不通），清空待机场恢复
NODES=()

NODE_PASSWORD="${SINGBOX_PASSWORD:-passwd}"
NODE_METHOD="chacha20-ietf"
NODE_OBFS_OPTS="obfs=http;obfs-host=28760-8mLb0x2l.download.microsoft.com"

# AnyTLS 备用节点（2026-09-29 从 9/26 Leo 后备订阅启用；f2nas.com 机场域名已 NXDOMAIN 报废）
# 顺序: 香港0.5倍率 → 香港11 → 新加坡06 → 台湾01（cnx2 日本/美国端口 refused 未入列）
ANYTLS_PASSWORD="8mLb0x2l"
ANYTLS_SNI="download.mihoyo.yuanshen.com"
ANYTLS_NODES=(
    "cnx1.somethingstranges.com:12001"
    "cn03.somethingstranges.com:12111"
    "cn07.somethingstranges.com:12206"
    "cn10.somethingstranges.com:12301"
)

test_proxy() {
    # 测试代理是否可用：先检查端口，再试 Binance API
    if ! nc -zv -w 3 127.0.0.1 $PROXY_PORT > /dev/null 2>&1; then
        return 1
    fi
    local result=$(curl -s --max-time 8 --proxy http://127.0.0.1:$PROXY_PORT https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT 2>/dev/null)
    if echo "$result" | grep -q '"symbol"'; then
        return 0
    fi
    return 1
}

test_node_direct() {
    local server="$1"
    local port="$2"
    nc -zv -w 5 "$server" "$port" > /dev/null 2>&1
}

ensure_proxy() {
    # WO-1009-④ (10/2): the host now injects a resident proxy on 17890;
    # the local sing-box client is no longer a persistent process, so the
    # old `pgrep -x sing-box` fast-path never matched and EVERY run fell
    # into the failover branch (config rewrites + nohup churn). Judge
    # proxy health by test_proxy alone. The failover loop below stays as
    # the host-proxy-outage takeover (10/2 03:20 it really bound 17890
    # and kept the scans alive for an hour).
    if test_proxy; then
        return 0
    fi

    echo "[$(date)] [PROXY] Proxy down or unhealthy, attempting failover..." >> "$LOGFILE"
    
    # 停掉旧进程
    pkill sing-box 2>/dev/null || true
    sleep 1

    for node in "${NODES[@]}"; do
        local server="${node%%:*}"
        local port="${node##*:}"
        
        echo "[$(date)] [PROXY] Testing node: $node" >> "$LOGFILE"
        
        if test_node_direct "$server" "$port"; then
            echo "[$(date)] [PROXY] Node $node reachable, switching..." >> "$LOGFILE"
            
            # 写入新配置
            cat > "$CONFIG_FILE" << EOF
{
  "log": { "level": "info" },
  "inbounds": [
    {
      "type": "mixed",
      "listen": "127.0.0.1",
      "listen_port": $PROXY_PORT
    }
  ],
  "outbounds": [
    {
      "type": "shadowsocks",
      "server": "$server",
      "server_port": $port,
      "method": "$NODE_METHOD",
      "password": "$NODE_PASSWORD",
      "plugin": "obfs-local",
      "plugin_opts": "$NODE_OBFS_OPTS"
    }
  ]
}
EOF
            
            nohup sing-box run -c "$CONFIG_FILE" > /dev/null 2>&1 &
            sleep 3
            
            if test_proxy; then
                echo "[$(date)] [PROXY] ✅ Switched to $node, proxy working." >> "$LOGFILE"
                return 0
            else
                echo "[$(date)] [PROXY] ❌ Node $node connected but proxy test failed." >> "$LOGFILE"
                pkill sing-box 2>/dev/null || true
                sleep 1
            fi
        else
            echo "[$(date)] [PROXY] ❌ Node $node unreachable." >> "$LOGFILE"
        fi
    done

    # All SS nodes failed, try Hysteria2 fallback nodes
    echo "[$(date)] [PROXY] All SS nodes failed, trying AnyTLS nodes..." >> "$LOGFILE"

    for node in "${ANYTLS_NODES[@]}"; do
        local server="${node%%:*}"
        local port="${node##*:}"
        
        echo "[$(date)] [PROXY] Testing AnyTLS node: $node" >> "$LOGFILE"
        
        # Write Hysteria2 config
        cat > "$CONFIG_FILE" << EOF
{
  "log": { "level": "info" },
  "inbounds": [
    {
      "type": "mixed",
      "listen": "127.0.0.1",
      "listen_port": $PROXY_PORT
    }
  ],
  "outbounds": [
    {
      "type": "anytls",
      "server": "$server",
      "server_port": $port,
      "password": "$ANYTLS_PASSWORD",
      "tls": {
        "enabled": true,
        "server_name": "$ANYTLS_SNI",
        "insecure": true
      }
    }
  ]
}
EOF
        
        nohup sing-box run -c "$CONFIG_FILE" > /dev/null 2>&1 &
        sleep 3
        
        if test_proxy; then
            echo "[$(date)] [PROXY] ✅ Switched to AnyTLS $node, proxy working." >> "$LOGFILE"
            return 0
        else
            echo "[$(date)] [PROXY] ❌ AnyTLS node $node proxy test failed." >> "$LOGFILE"
            pkill sing-box 2>/dev/null || true
            sleep 1
        fi
    done

    echo "[$(date)] [PROXY] ⚠️ All nodes (SS + AnyTLS) failed! Proxy not available." >> "$LOGFILE"
    return 1
}

# === Main ===
# Try proxy first; if all nodes fail, test direct connection as fallback
if ! ensure_proxy; then
    echo "[$(date)] [PROXY] All proxy nodes failed, testing direct connection..." >> "$LOGFILE"
    # Some domestic IPs can reach api.binance.com directly
    DIRECT_TEST=$(curl -s --max-time 10 https://api.binance.com/api/v3/ping 2>/dev/null)
    if echo "$DIRECT_TEST" | grep -q '{}'; then
        echo "[$(date)] [PROXY] ⚡ Direct connection works, proceeding without proxy." >> "$LOGFILE"
        export SKIP_PROXY=1
    else
        echo "[$(date)] No proxy and no direct connection, aborting." >> "$LOGFILE"
        exit 1
    fi
fi

# Load .env
set -a
source "$BASEDIR/.env"
set +a

# If direct connection mode, clear proxy env vars so Python uses direct
if [ "${SKIP_PROXY:-0}" = "1" ]; then
    unset HTTP_PROXY HTTPS_PROXY ALL_PROXY
fi

cd "$BASEDIR"

# Dynamic scan gate: skip scan if market conditions don't warrant it
if [ "$CMD" = "cron-scan" ]; then
    set +e
    python3 scripts/scan_gate.py >> "$LOGFILE" 2>&1
    GATE_EXIT=$?
    set -e
    if [ $GATE_EXIT -ne 0 ]; then
        echo "========== $(date) - $CMD SKIPPED by dynamic gate ==========" >> "$LOGFILE"
        # WO-0924 P2 followup (9/24): gate-skip rounds still owe a shadow
        # diff. The dynamic gate parks the heavy scan ~43x/day at the 1h
        # F&G cadence, which silently starved the P2 observation clock
        # (stats stuck at round 3 while 9/24 kept skipping). The diff is
        # pure-DB with zero API calls, so it cannot undermine the gate's
        # waste-prevention role. Always || true: never blocks.
        python3 -m src.ledger shadow-round >> "$LOGFILE" 2>&1 || true
        exit 0
    fi
fi

echo "========== $(date) - $CMD ==========" >> "$LOGFILE"
# bug#41 (2026-09-17 review): weekly ops pipelines are standalone scripts,
# not main.py subcommands — route them here so crontab can reuse the
# proxy/env preamble. cron-scan and all other commands behave as before.
case "$CMD" in
    weekly-learning)  RUN_CMD=(python3 scripts/learning_pipeline.py "$@") ;;
    weekly-backtest)  RUN_CMD=(python3 scripts/weekly_backtest.py "$@") ;;
    daily-learning)   RUN_CMD=(python3 scripts/daily_learning.py "$@") ;;
    *)                RUN_CMD=(python3 main.py "$CMD" "$@") ;;
esac

set +e
"${RUN_CMD[@]}" >> "$LOGFILE" 2>&1
EXIT_CODE=$?
set -e
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Record failure for monitoring
# WO-1005 addendum-4: dry-run failures are visible via latest_dryrun.json
# (exit_code) — keep them OUT of live cron_failures.jsonl so the bug#35
# self-heal pipeline never reacts to simulator noise.
if [ $EXIT_CODE -ne 0 ] && [ "${DRYRUN:-0}" != "1" ]; then
    echo "{\"timestamp\":\"$(date -Iseconds)\",\"job\":\"$CMD\",\"exit_code\":$EXIT_CODE}" >> "$LOGDIR/cron_failures.jsonl"
fi

# Auto-push notifications after scan to prevent backlog
# WO-1005 addendum-4: never drain live pending_notifications.json on a
# DRYRUN round — push_notifications.py has no DRYRUN guard and would mark
# live queue items pushed (WO-1003-⑤ muted notifier.send only). New-signal
# muting lives in src/notifier.py; queue draining is muted here.
if [ "$CMD" = "cron-scan" ] && [ $EXIT_CODE -eq 0 ] && [ "${DRYRUN:-0}" != "1" ]; then
    python3 scripts/push_notifications.py >> "$LOGFILE" 2>&1 || true
fi

# bug#15: backup local state.db to fuse-side rolling copy (survives sandbox restarts)
# WO-1005 addendum-4: skip on DRYRUN — .env still points STATE_DB_PATH at
# the live db, so a dry-run round would refresh the live autobak (and a
# future env flip would overwrite it with dryrun bytes). The dryrun twin
# is already backed up by reside_scan.backup_db (state_backup_dryrun/).
if [ "${DRYRUN:-0}" != "1" ] && [ -n "$STATE_DB_PATH" ] && [ -f "$STATE_DB_PATH" ]; then
    cp -f "$STATE_DB_PATH" "$BASEDIR/data/state.db.autobak" 2>/dev/null || true
fi

# Rotate log if > 1MB (prevents stale breaker/notification messages from persisting)
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 1048576 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
    # WO-0926 order ②: keep the path alive. Between this mv and the next
    # invocation's first append (up to one cron interval) the log was
    # ABSENT — the 9/26 11:01→11:20 rotation void broke tail -f / file
    # monitors. Recreate immediately so the path always exists.
    touch "$LOGFILE"
fi

exit $EXIT_CODE
