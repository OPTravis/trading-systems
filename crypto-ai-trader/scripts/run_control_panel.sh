#!/usr/bin/env bash
# WO-1001-1: control panel sidecar launcher (ops).
# Tokens are generated OUTSIDE the repo and exported here or via a
# root-only env file (chmod 600 /root/trading-systems/.ctrl.env):
#   openssl rand -hex 32   # CTRL_RO_TOKEN
#   openssl rand -hex 32   # CTRL_RW_TOKEN
# Keep both OFF git. Leo holds the master copies (password manager).
set -u
cd "$(dirname "$0")/.."
[ -f /root/trading-systems/.ctrl.env ] && . /root/trading-systems/.ctrl.env
exec python3 -m src.control_panel
