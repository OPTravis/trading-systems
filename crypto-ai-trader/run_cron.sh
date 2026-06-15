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
# P2-10: Proxy is mandatory — abort if unavailable
if ! ensure_proxy; then
    echo "[$(date)] No proxy available, aborting." >> "$LOGFILE"
    exit 1
fi

# Load .env
set -a
source "$BASEDIR/.env"
set +a

cd "$BASEDIR"

echo "========== $(date) - $CMD ==========" >> "$LOGFILE"
python3 main.py "$CMD" "$@" >> "$LOGFILE" 2>&1
EXIT_CODE=$?
echo "========== Exit: $EXIT_CODE ==========" >> "$LOGFILE"

# Rotate log if > 5MB
if [ -f "$LOGFILE" ] && [ $(stat -c%s "$LOGFILE" 2>/dev/null || echo 0) -gt 5242880 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

exit $EXIT_CODE
