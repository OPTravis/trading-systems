#!/bin/bash
# Start sing-box proxy for trading system
if pgrep -x sing-box > /dev/null; then
    echo "[ok] sing-box already running"
else
    nohup sing-box run -c /etc/sing-box/config.json > /tmp/sing-box.log 2>&1 &
    sleep 2
    if pgrep -x sing-box > /dev/null; then
        echo "[ok] sing-box started on :17890"
    else
        echo "[FAIL] sing-box failed to start"
        cat /tmp/sing-box.log
    fi
fi
