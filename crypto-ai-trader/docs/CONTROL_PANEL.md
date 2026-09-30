# 控制面运维说明（WO-1001-1）

## 启用步骤（生产）

1. **生成 token（不入 git，不入 repo）**：
   ```bash
   openssl rand -hex 32   # → CTRL_RO_TOKEN（只读）
   openssl rand -hex 32   # → CTRL_RW_TOKEN（可写）
   ```
   两份 master 副本交 **Leo 密码管理器保管**；机器上只放
   `/root/trading-systems/.ctrl.env`（chmod 600，root only）。

2. **写入环境文件**：
   ```bash
   cat > /root/trading-systems/.ctrl.env <<'E'   # 然后 chmod 600
   CTRL_RO_TOKEN=<hex>
   CTRL_RW_TOKEN=<hex>
   E
   ```

3. **启动（默认仅监听 127.0.0.1:8787）**：
   `bash scripts/run_control_panel.sh`
   常驻建议 crontab 加 `@reboot bash /root/trading-systems/crypto-ai-trader/scripts/run_control_panel.sh >> /root/trading-systems/crypto-ai-trader/logs/control_panel.log 2>&1`（是否启用由 Travis/Leo 决定）。

## API

- `GET /status`（RO token）：持仓/保护锁/exit:mode/halt 状态
- `POST /cmd` `{"cmd": "/pause"}`（RW token）
  - `/pause` 拒新开仓（出场/trailing 不受影响）
  - `/resume` 恢复
  - `/forceexit SYMUSDT|ALL` —— 先回显清单，60s 内再发 `{"cmd":..., "confirm":"CONFIRM"}`；显式人工指令可 override exit-mode=off（审计记 override_exit_mode）
  - `/exit-mode auto|notify|off`
- Bearer token；每 token 10 cmd/min；公网暴露需 `CTRL_BIND_PUBLIC=1` + `CTRL_ALLOWED_IPS`（且强烈建议反代 HTTPS——本进程自身是明文 HTTP）

## Telegram（可选）

`CTRL_TG_BOT_TOKEN` + `CTRL_TG_ALLOWED_CHAT_IDS` 设置后自动启用轮询。
chat 白名单内 = 只读；`/auth <RW_TOKEN>` 本次会话升级可写。

## 故障语义

控制面是旁路进程：挂掉不影响交易主链。所有 RW 命令审计到
ledger_events（source=control_panel）+ token 短指纹（8 位哈希，全 token 永不落盘）。
