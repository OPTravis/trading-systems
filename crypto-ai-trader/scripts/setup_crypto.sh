#!/bin/bash
set -u
LOG=/root/setup_crypto.log
exec > >(tee -a "$LOG") 2>&1
echo "=== $(date) ==="

GIT_URL_FILE=/root/.crypto_git_url   # credential file (mode 600, single line)
if [ ! -s "$GIT_URL_FILE" ]; then
  echo "[0] FATAL: $GIT_URL_FILE missing/empty — rebuild it first:"
  echo "      echo 'https://<user>:<token>@github.com/OPTravis/trading-systems.git' > $GIT_URL_FILE"
  echo "      chmod 600 $GIT_URL_FILE"
  exit 1
fi
GIT_URL="$(head -n1 "$GIT_URL_FILE" | tr -d '[:space:]')"
SB_VER=1.11.4
SB_BIN=/usr/local/bin/sing-box

if [ ! -x "$SB_BIN" ]; then
  echo "[1] downloading sing-box v$SB_VER"
  URL="https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/sing-box-${SB_VER}-linux-amd64.tar.gz"
  curl -sL --max-time 120 -o /tmp/sb.tar.gz "$URL" || true
  if [ ! -s /tmp/sb.tar.gz ]; then
    echo "[1] direct failed, trying ghproxy mirror"
    curl -sL --max-time 120 -o /tmp/sb.tar.gz "https://ghproxy.net/$URL" || true
  fi
  rm -rf /tmp/sbx && mkdir -p /tmp/sbx && tar -xzf /tmp/sb.tar.gz -C /tmp/sbx 2>/dev/null
  SB_EXTRACT=$(find /tmp/sbx -name sing-box -type f | head -1)
  [ -n "$SB_EXTRACT" ] && install -m755 "$SB_EXTRACT" $SB_BIN && echo "[1] sing-box installed" || { echo "[1] INSTALL FAILED"; exit 1; }
else
  echo "[1] sing-box exists"
fi

mkdir -p /etc/sing-box
cat > /etc/sing-box/config.json <<'EOF'
{
  "log": {"level": "info"},
  "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": 17890}],
  "outbounds": [{
    "type": "shadowsocks",
    "server": "cn02.somethingstranges.com",
    "server_port": 8101,
    "method": "chacha20-ietf",
    "password": "passwd",
    "plugin": "obfs-local",
    "plugin_opts": "obfs=http;obfs-host=28760-8mLb0x2l.download.microsoft.com"
  }]
}
EOF
pkill -x sing-box 2>/dev/null; sleep 1
nohup $SB_BIN run -c /etc/sing-box/config.json >/dev/null 2>&1 &
sleep 3
CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 -x http://127.0.0.1:17890 https://api.github.com)
echo "[2] proxy test via 17890 -> github api: $CODE"
[ "$CODE" != "000" ] || { echo "[2] PROXY DEAD"; exit 1; }

if [ ! -f "$REPO/crypto-ai-trader/main.py" ]; then
  echo "[3] cloning repo"
  export https_proxy=http://127.0.0.1:17890 http_proxy=http://127.0.0.1:17890
  rm -rf $REPO
  git clone --depth 1 "$GIT_URL" $REPO 2>&1 | tail -1
  unset https_proxy http_proxy
  [ -f "$REPO/crypto-ai-trader/main.py" ] || { echo "[3] CLONE FAILED"; exit 1; }
  echo "[3] clone OK: $(git -C $REPO log --oneline -1)"
else
  echo "[3] repo exists: $(git -C $REPO log --oneline -1)"
fi

mkdir -p /root/trading-state
if [ -s /root/.crypto_env ]; then
  cp /root/.crypto_env $REPO/crypto-ai-trader/.env
  chmod 600 $REPO/crypto-ai-trader/.env
  echo "[4] .env restored from /root/.crypto_env"
else
  cat > $REPO/crypto-ai-trader/.env <<'EOF2'
# SKELETON — no credentials embedded. Populate and restart the chain:
#   cp .env /root/.crypto_env && chmod 600 /root/.crypto_env   # persist for rebuilds
BINANCE_API_KEY=__FILL_ME__
BINANCE_API_SECRET=__FILL_ME__
DEEPSEEK_API_KEY=__FILL_ME__
AUTO_EXECUTE=true
USE_CCXT=0
STATE_DB_PATH=/root/trading-state/state.db
PROXY_HTTP=http://127.0.0.1:17890
PROXY_HTTPS=http://127.0.0.1:17890
HTTP_PROXY=http://127.0.0.1:17890
HTTPS_PROXY=http://127.0.0.1:17890
http_proxy=http://127.0.0.1:17890
https_proxy=http://127.0.0.1:17890
EOF2
  chmod 600 $REPO/crypto-ai-trader/.env
  echo "[4] WARN: /root/.crypto_env missing — wrote SKELETON .env with __FILL_ME__ keys."
  echo "    Trading chain will FAIL until real credentials are provided."
fi
echo "[4] .env written"

LATEST=$(ls -t /Coze/Drive/Crypto_Trading_Monitor/state_backup/state.db.* 2>/dev/null | head -1)
if [ -n "$LATEST" ] && [ ! -s /root/trading-state/state.db ]; then
  cp "$LATEST" /root/trading-state/state.db && echo "[5] DB restored from $LATEST"
elif [ -s /root/trading-state/state.db ]; then
  echo "[5] DB exists"
else
  echo "[5] WARN no backup found"
fi

cd $REPO/crypto-ai-trader
pip install --quiet -r requirements.txt 2>&1 | tail -2
echo "[6] deps done"
echo "=== BOOTSTRAP DONE ==="
