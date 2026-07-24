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

# === Proxy Auto-Failover ===
PROXY_PORT=17890
CONFIG_FILE="/etc/sing-box/config.json"

# 节点列表：server:port
NODES=(
    "cn02.somethingstranges.com:8101"
    "cn01.somethingstranges.com:8101"
    "23.186.200.164:8101"
)

NODE_PASSWORD="${SINGBOX_PASSWORD:-passwd}"
NODE_METHOD="chacha20-ietf"
NODE_OBFS_OPTS="obfs=http;obfs-host=28760-8mLb0x2l.download.microsoft.com"

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
    # 如果代理正常，直接返回
    if pgrep -x sing-box > /dev/null && test_proxy; then
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

    echo "[$(date)] [PROXY] ⚠️ All nodes failed! Proxy not available." >> "$LOGFILE"
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
        exit 0
    fi
fi

echo "========== $(date) - $CMD ==========" >> "$LOGFILE"
set +e
python3 main.py "$CMD" "$@" >> "$LOGFILE" 2>&1
EXIT_CODE=$?
set -e
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Record failure for monitoring
if [ $EXIT_CODE -ne 0 ]; then
    echo "{\"timestamp\":\"$(date -Iseconds)\",\"job\":\"$CMD\",\"exit_code\":$EXIT_CODE}" >> "$LOGDIR/cron_failures.jsonl"
fi

# Auto-push notifications after scan to prevent backlog
if [ "$CMD" = "cron-scan" ] && [ $EXIT_CODE -eq 0 ]; then
    python3 scripts/push_notifications.py >> "$LOGFILE" 2>&1 || true
fi

# Rotate log if > 1MB (prevents stale breaker/notification messages from persisting)
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 1048576 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
