# Crypto AI Trader — 全面代码审查报告

**审查日期**: 2026-06-14  
**代码库**: `/app/data/所有对话/主对话/trading-systems/crypto-ai-trader/`  
**代码总量**: ~35,000 行 Python (200 个 `.py` 文件)  
**审查范围**: 交易执行层、风险管理、策略层、市场扫描、状态管理、基础设施、运维脚本、配置管理  

---

## 目录

- [P0 — 紧急问题（需立即修复）](#p0--紧急问题需立即修复)
- [P1 — 重要问题（应在下次发布前修复）](#p1--重要问题应在下次发布前修复)
- [P2 — 改进建议（长期优化）](#p2--改进建议长期优化)
- [总结与优先级矩阵](#总结与优先级矩阵)

---

## P0 — 紧急问题（需立即修复）

### P0-1. 安全检查 fail-open：允许异常时通过交易

| 项目 | 详情 |
|------|------|
| **文件** | `src/trade_executor.py` |
| **行号** | L46-L50, L65-L73 |
| **严重性** | 🔴 可能导致在异常价格或重复下单时执行交易 |

**问题描述**:  
`_check_price_deviation()` 和 `_check_duplicate_order()` 在检查发生异常时返回 `True`（通过），即"fail-open"策略。这意味着：

```python
# L46-50: 价格偏差检查 — 异常时允许交易
except Exception as e:
    logger.warning(f"Price deviation check failed for {symbol}: {e} — allowing trade (fail-open)")
    return True  # fail-open: allow trade on transient check failure

# L65-73: 重复订单检查 — 异常时允许交易
except Exception as e:
    logger.warning(f"Duplicate order check failed for {symbol}: {e} — allowing trade (fail-open)")
    return True  # fail-open: allow trade on transient check failure
```

当 API 临时不可用（网络抖动、代理切换、Binance 限流）时，这些检查会被跳过，可能在闪崩价格下买入，或重复下单同一币种。对于一个自动交易系统，安全检查应该是 fail-closed（异常时阻止交易）。

**建议修复方案**:  
```python
# 改为 fail-closed：检查失败时阻止交易
except Exception as e:
    logger.error(f"Price deviation check failed for {symbol}: {e} — BLOCKING trade (fail-closed)")
    return False
```

对于 `_check_duplicate_order`，同样改为返回 `False`。虽然这可能导致代理抖动时偶尔错过交易机会，但远比在价格异常时买入安全得多。

---

### P0-2. WebSocket 用户数据流不使用代理

| 项目 | 详情 |
|------|------|
| **文件** | `src/ws_user_stream.py` |
| **行号** | L36, L131-137, L141 |
| **严重性** | 🔴 WebSocket 功能完全不可用（Binance 在国内被墙） |

**问题描述**:  
WebSocket 模块硬编码了 Binance 直连地址，且不通过 sing-box 代理：

```python
# L36: 硬编码 WebSocket 地址
SPOT_WS_BASE = "wss://stream.binance.com:9443/ws"

# L131-137: REST API 也直连，不使用代理
url = "https://api3.binance.com/api/v3/userDataStream"
resp = requests.post(url, headers={"X-MBX-API-KEY": self.api_key}, timeout=10)
```

在当前部署环境（国内云电脑通过 sing-box 代理访问 Binance），这些直连请求全部会失败。WebSocket 功能（实时余额更新、订单状态推送）完全不可用。

**建议修复方案**:  
1. 为 `requests` 调用添加代理配置：
   ```python
   proxies = {"https": "http://127.0.0.1:17890"}
   resp = requests.post(url, headers=..., timeout=10, proxies=proxies)
   ```
2. 为 WebSocket 使用 `websocket` 库的代理支持：
   ```python
   self._ws.run_forever(ping_interval=30, ping_timeout=10, 
                         http_proxy_host="127.0.0.1", http_proxy_port=17890)
   ```
3. 将代理配置提取为环境变量（如 `SING_BOX_PROXY`），避免硬编码端口。

---

### P0-3. `fapi.binance.com` 期货 API 直连（无代理 + 期货端点）

| 项目 | 详情 |
|------|------|
| **文件** | `src/scan_orchestrator.py` |
| **行号** | L167-172 |
| **严重性** | 🔴 直连被墙域名 + 访问期货 API（系统设计为 SPOT only） |

**问题描述**:  
```python
# L167-172
fr_resp = _req.get(
    "https://fapi.binance.com/fapi/v1/fundingRate",
    params={"symbol": "BTCUSDT", "limit": "1"},
    timeout=5,
)
```

两个问题：
1. `fapi.binance.com` 在国内被墙，此请求会失败或超时。
2. 系统设计明确标注 "SPOT ONLY - no futures"，但这里访问了期货 API。
3. 请求使用裸 `requests.get()` 不走代理，虽然有 `timeout=5` 但每次扫描都会超时浪费 5 秒。

**建议修复方案**:  
将此请求改为走代理，或使用替代数据源获取 funding rate。如果 funding rate 非关键，可降级为可选（失败时跳过）：
```python
try:
    fr_resp = _req.get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        params={"symbol": "BTCUSDT", "limit": "1"},
        timeout=5,
        proxies={"https": "http://127.0.0.1:17890"},  # 走代理
    )
except Exception:
    btc_funding_rate = 0.0  # 降级
```

---

### P0-4. Circuit Breaker 单例无线程安全保护

| 项目 | 详情 |
|------|------|
| **文件** | `src/circuit_breaker.py`, `src/daily_loss_breaker.py` |
| **行号** | `circuit_breaker.py` L227-235, `daily_loss_breaker.py` L265-273 |
| **严重性** | 🔴 并发 cron 任务可能导致风控失效 |

**问题描述**:  
`CircuitBreaker` 和 `DailyLossBreaker` 使用模块级单例模式，但没有线程锁保护：

```python
# circuit_breaker.py L227-235
_cb_instance: Optional[CircuitBreaker] = None

def get_circuit_breaker() -> CircuitBreaker:
    global _cb_instance
    if _cb_instance is None:
        _cb_instance = CircuitBreaker()
    return _cb_instance
```

当 `trailing-check`（每 5 分钟）和 `scan`（每 2 小时）cron 任务同时运行时：
- 单例初始化存在 race condition（两个进程可能创建不同实例）
- 实例内部的 `_failure_count`、`_tripped_until` 等状态变量被多个线程并发读写
- StateDB 使用 `check_same_thread=False` 但 Python 进程间不共享内存

实际上，由于 cron 任务以独立 Python 进程运行（`python3 main.py scan`），单例模式在每个进程中是独立的。这意味着 **跨进程的状态同步完全依赖 StateDB**，但 StateDB 的 SQLite 在 WAL 模式下并发写入仍可能产生 `database is locked` 错误。

**建议修复方案**:  
1. 在单例初始化加锁（解决线程内 race condition）：
   ```python
   _cb_lock = threading.Lock()
   def get_circuit_breaker() -> CircuitBreaker:
       global _cb_instance
       if _cb_instance is None:
           with _cb_lock:
               if _cb_instance is None:
                   _cb_instance = CircuitBreaker()
       return _cb_instance
   ```
2. 对于跨进程安全，确保每次 `is_tripped()` 调用都从 StateDB 重新加载状态，而不是依赖内存缓存。
3. 考虑在 StateDB 的 `kv_get`/`kv_set` 操作上加 `BEGIN IMMEDIATE` 事务锁。

---

### P0-5. `execute_auto_trade` 函数过度复杂（500+ 行单函数）

| 项目 | 详情 |
|------|------|
| **文件** | `src/trade_executor.py` |
| **行号** | L100-L500 (整个 `execute_auto_trade` 函数) |
| **严重性** | 🔴 难以测试、难以维护、增加引入 bug 的风险 |

**问题描述**:  
`execute_auto_trade` 函数包含以下所有逻辑：
- API key 验证
- DCA 排除检查
- Circuit breaker 检查
- Daily loss breaker 检查
- Stepwise drawdown 检查
- Position count 检查
- Kelly position sizing
- Fee optimization
- Contextual bandit sizing
- Cash reserve / exposure caps
- Single trade loss limit
- Exchange filter fetching
- Price deviation check
- Duplicate order check
- TWAP/MARKET order routing
- Fill parsing
- Fee-adjusted qty calculation
- Tiered TP exit placement
- OCO fallback
- Separate SL+TP fallback
- Uncovered quantity protection
- Emergency market sell
- Portfolio state update
- Event bus publishing
- Notification sending

这是一个 God Function。其中有 39 个 `except Exception` 块，7 个 `pass`（静默吞错误），多个嵌套层级超过 4 层。

**建议修复方案**:  
将函数拆分为独立的责任单元：
```python
def execute_auto_trade(symbol, price, strategy, ...):
    pre_trade_checks = _run_pre_trade_checks(client, symbol, price)
    if not pre_trade_checks.ok:
        return pre_trade_checks.error
    
    position = _calculate_position_size(client, symbol, score, ...)
    if position.too_small:
        return {"success": False, "error": "Position too small"}
    
    buy_result = _execute_buy(client, symbol, position, price)
    if not buy_result.success:
        return buy_result.error
    
    exit_orders = _place_exit_orders(client, symbol, buy_result, stop_loss_pct, tp_levels)
    _update_portfolio_state(portfolio, symbol, buy_result, exit_orders)
    _send_notification(notifier, symbol, buy_result, exit_orders)
    
    return _build_result(buy_result, exit_orders)
```

---

### P0-6. Trailing Stop SL 替换期间的裸露窗口

| 项目 | 详情 |
|------|------|
| **文件** | `src/cmd_trailing_check.py` |
| **号** | L245-L260 |
| **严重性** | 🔴 旧 SL 已取消、新 SL 未挂上期间持仓完全无保护 |

**问题描述**:  
当追踪止损需要上移 SL 时，执行流程是：
1. 取消旧 SL 订单
2. 挂新 SL 订单

如果步骤 2 失败，持仓在这段时间内完全没有止损保护：

```python
# L250: 先取消旧 SL
cancel_result = client.cancel_order(symbol, _order_id(sl_order))
if cancel_result:
    # L253-258: 再挂新 SL — 如果这里失败，持仓裸露
    new_sl_order = client.place_order(
        symbol, "SELL", "STOP_LOSS_LIMIT",
        sl_qty, price=new_sl_rounded, stop_price=new_sl_rounded
    )
    if new_sl_order:
        sl_moved = True
    else:
        # 仅发通知，没有重试或回滚
        logger.critical("TrailingStop: failed to place new SL for %s after cancel!", asset)
        notifier.send_text(f"🔴 SL更新失敗 {asset}！舊SL已取消但新SL未掛上！手動處理！")
```

通知虽然发送了，但在人工介入之前持仓完全暴露在市场风险中。对于每 5 分钟运行一次的 cron 任务，这个窗口可能持续数分钟。

**建议修复方案**:  
采用 "先挂后撤"（place-then-cancel）策略：
1. 先挂新 SL 订单（此时可能有 qty 超限，需要先计算差额）
2. 确认新 SL 成功后再取消旧 SL
3. 如果新 SL 挂不上，保持旧 SL 不动

或者采用 OCO 订单替换方式，原子性地替换整个止损止盈组合。

---

### P0-7. `run_cron.sh` 代理密码硬编码

| 项目 | 详情 |
|------|------|
| **文件** | `run_cron.sh` |
| **行号** | L27 |
| **严重性** | 🔴 密码泄露风险 |

**问题描述**:  
```bash
NODE_PASSWORD="passwd"  # 实际密码明文写入 shell 脚本
```

这个文件在 git 仓库中，代理节点的密码以明文形式存储。如果仓库泄露（即使是私有仓库），攻击者可以利用这个密码连接到代理节点。

**建议修复方案**:  
1. 将密码移到 `.env` 文件中（`.env` 不应提交到 git）
2. 在 `run_cron.sh` 中从环境变量读取：
   ```bash
   NODE_PASSWORD="${SINGBOX_PASSWORD:?SINGBOX_PASSWORD not set}"
   ```
3. 确保 `.gitignore` 包含 `.env`

---

## P1 — 重要问题（应在下次发布前修复）

### P1-1. 单次交易执行中重复调用 `get_account()` 5+ 次

| 项目 | 详情 |
|------|------|
| **文件** | `src/trade_executor.py` |
| **行号** | L287-295 (daily loss), L340-355 (exposure cap), L410-425 (loss limit), L662-680 (post-trade) |
| **严重性** | 🟡 性能问题 + API 限流风险 |

**问题描述**:  
`execute_auto_trade` 在一次执行中多次调用 `client.get_account()`：
1. Daily loss breaker 计算总组合价值 (L287)
2. Total exposure cap 检查 (L340)
3. Single trade loss limit 检查 (L410)
4. Post-trade USDT 余额获取 (L662)

每次 `get_account()` 都是一次 Binance API 调用。在 Binance 的权重限制下，account endpoint 权重为 10（较高），连续调用可能触发限流。

**建议修复方案**:  
在函数开始时一次性获取 account 数据，后续计算复用：
```python
account_data = client.get_account()
# 后续所有检查复用 account_data
```

---

### P1-2. 广泛的异常捕获掩盖真实错误

| 项目 | 详情 |
|------|------|
| **文件** | `src/trade_executor.py`, `src/risk_manager.py`, `src/cmd_trailing_check.py` |
| **行号** | 见下方统计 |
| **严重性** | 🟡 错误被静默吞没，难以排查问题 |

**问题描述**:  
核心文件中有 34 个 `except Exception` 块，其中 7 个直接 `pass`（完全吞灭错误）：

| 文件 | `except Exception` 数 | `pass` 数 |
|------|----------------------|-----------|
| `trade_executor.py` | 39 | 5 |
| `risk_manager.py` | - | 2 |
| `cmd_trailing_check.py` | - | 0 |

典型模式：
```python
try:
    from src.contextual_bandit import get_contextual_bandit
    bandit = get_contextual_bandit()
    # ... bandit logic
except Exception as e:
    logger.warning(f"ContextualBandit unavailable (using 1.0x): {e}")
    _bandit_multiplier = 1.0  # 静默降级
```

这种模式的问题在于：bandit 的一个 typo 或配置错误不会被发现，系统会静默地使用默认值。

**建议修复方案**:  
1. 缩小 except 范围到具体异常类型（如 `ImportError`, `ConnectionError`）
2. 对于关键路径（SL/TP 订单、资金检查），禁止静默降级
3. 添加结构化错误追踪（error code + context）

---

### P1-3. Daily Loss Breaker 只升不降

| 项目 | 详情 |
|------|------|
| **文件** | `src/daily_loss_breaker.py` |
| **行号** | L196 (注释), L210-218 |
| **严重性** | 🟡 可能导致本应恢复的交易继续被阻止 |

**问题描述**:  
注释明确说明：
```python
# Tier escalates (never de-escalates within same day)
if new_tier > self._current_tier:
```

即使组合价值完全恢复（从 -3% 反弹到 +5%），tier 仍然保持在 3（halt all），直到第二天 UTC 重置。这在快速反弹的行情中过于保守，会错过明显的交易机会。

**建议修复方案**:  
考虑允许在组合恢复到当日正收益时降级 tier：
```python
# 允许在 PnL 转正时降级（但不低于 tier 0 以下）
if daily_pnl_pct > 0 and self._current_tier > 0:
    self._current_tier = max(0, self._current_tier - 1)
```
或者添加一个手动/半自动的降级机制。

---

### P1-4. `StrategyAdaptor` 缓存使用类级变量

| 项目 | 详情 |
|------|------|
| **文件** | `src/strategy_adaptor.py` |
| **行号** | L20-21 |
| **严重性** | 🟡 多实例间缓存污染 |

**问题描述**:  
```python
class StrategyAdaptor:
    _cache: Optional[Dict] = None      # 类级变量
    _cache_ts: float = 0.0             # 类级变量
    _cache_ttl: float = 300            # 类级变量
    _btc_klines_cache = None           # 类级变量
    _btc_klines_ts: float = 0.0        # 类级变量
```

这些是类级（class-level）变量，所有实例共享。在测试中创建多个实例时，一个实例的 `adapt()` 调用会污染另一个实例的缓存。`_btc_klines_cache` 尤其危险，因为它存储的是可变的 kline 数据。

**建议修复方案**:  
将缓存改为实例级变量：
```python
def __init__(self):
    self._cache: Optional[Dict] = None
    self._cache_ts: float = 0.0
    self._cache_ttl: float = 300
    self._btc_klines_cache = None
    self._btc_klines_ts: float = 0.0
```

---

### P1-5. `sync_from_binance` 清空后重建（非原子操作）

| 项目 | 详情 |
|------|------|
| **文件** | `src/portfolio_state.py` |
| **行号** | L130-L175 |
| **严重性** | 🟡 中断时本地状态丢失 |

**问题描述**:  
```python
def sync_from_binance(self, binance_client):
    # Save original state for rollback on failure
    old_positions = dict(self.positions)
    old_cash = self.cash_balance
    
    # Clear local state
    self.positions = {}  # ← 本地状态被清空
    
    # ... 逐个从 Binance 重建 ...
    # 如果在这个过程中发生异常：
    #   except 块会恢复 old_positions
    #   但如果在重建过程中（positions 部分填充时）进程被 kill
    #   则内存中的状态是不完整的，且尚未持久化到 DB
```

虽然有 try/except 回滚机制，但如果进程在清空后、重建中被强制终止（OOM kill、超时 kill），内存中的状态会丢失，而 DB 中可能还是旧状态。

**建议修复方案**:  
采用 "build-then-swap" 模式：先构建新状态到临时变量，全部成功后再原子替换：
```python
def sync_from_binance(self, binance_client):
    new_positions = {}
    new_cash = 0.0
    
    # 构建新状态（不影响现有状态）
    account = binance_client.get_account()
    for balance in account.get("balances", []):
        # ... build new_positions ...
    
    # 原子替换
    with self._lock:
        self.positions = new_positions
        self.cash_balance = new_cash
    self._save_state(force=True)
```

---

### P1-6. 无订单幂等性保护

| 项目 | 详情 |
|------|------|
| **文件** | `src/scan_orchestrator.py`, `src/trade_executor.py` |
| **行号** | 整体流程 |
| **严重性** | 🟡 cron 任务重叠可能导致重复交易 |

**问题描述**:  
系统使用 cron 任务每 2 小时扫描并执行交易。如果某次扫描因网络延迟超过 2 小时（或 cron 调度重叠），两次扫描可能同时执行，导致：
1. 同一信号被评估两次
2. `_check_duplicate_order` 可能因为时序问题无法检测到并发的 BUY 订单
3. 两个进程可能同时通过所有风控检查，各自下单

目前唯一的防护是 `_check_duplicate_order`（检查 pending BUY orders），但这是在 `execute_auto_trade` 函数内部调用的，如果两个进程几乎同时到达这个检查点，两者都可能看到没有 pending order。

**建议修复方案**:  
1. 使用文件锁（`fcntl.flock`）确保同一时间只有一个 scan 进程运行：
   ```python
   import fcntl
   lock_file = open("/tmp/crypto-trader-scan.lock", "w")
   try:
       fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
   except BlockingIOError:
       logger.warning("Another scan is already running, skipping")
       return
   ```
2. 或在 StateDB 中添加 `scan_in_progress` 标志，带 TTL 自动过期。

---

### P1-7. `SSL_CERT_VERIFICATION` 可被关闭

| 项目 | 详情 |
|------|------|
| **文件** | `src/_binance_sdk_client.py` |
| **行号** | L32 |
| **严重性** | 🟡 MITM 攻击风险 |

**问题描述**:  
```python
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() in ("true", "1", "yes")
```

虽然默认值为 true，但可以通过环境变量关闭 SSL 验证。在通过 sing-box 代理的环境中，如果有人错误地设置了 `VERIFY_SSL=false`，所有 API 请求将不验证服务器证书，容易受到中间人攻击。

代码注释也提到了这一点：
> For production, consider implementing certificate pinning to prevent MITM attacks.

**建议修复方案**:  
1. 在生产模式（非 testnet）下禁止关闭 SSL 验证
2. 添加启动检查日志，如果 SSL 验证被关闭则发出警告
3. 考虑对 Binance API 实现证书钉扎（certificate pinning）

---

### P1-8. `count_active_positions` 的 fail-open 返回值

| 项目 | 详情 |
|------|------|
| **文件** | `src/trade_executor.py` |
| **行号** | L128-131 |
| **严重性** | 🟡 API 失败时可能超出最大持仓限制 |

**问题描述**:  
```python
def count_active_positions(client):
    try:
        # ... count logic ...
        return count
    except Exception:
        logger.warning("count_active_positions: account fetch failed")
        return 0  # ← 失败时返回 0
```

当 API 调用失败时返回 0，调用方会认为没有持仓，可能继续开仓，突破 5 个最大持仓限制。

**建议修复方案**:  
失败时返回一个哨兵值或抛出异常，阻止后续交易：
```python
except Exception:
    logger.error("count_active_positions: account fetch failed — blocking trade")
    return -1  # or raise

# 调用方：
active_positions = count_active_positions(client)
if active_positions < 0:
    return {"success": False, "error": "Cannot determine active positions"}
```

---

### P1-9. `HealthCheck` 脚本 Shebang 路径硬编码

| 项目 | 详情 |
|------|------|
| **文件** | `scripts/health_check.py` |
| **行号** | L1 |
| **严重性** | 🟡 重启后路径失效 |

**问题描述**:  
```python
#!/home/travis/crypto-ai-trader/.venv/bin/python3
```

这个 shebang 硬编码了用户家目录路径。根据系统文档，云电脑重启后根文件系统会重置，`/home/travis/` 可能不存在（系统持久化在 `/app/data/` 和 `/tmp/user/`）。

**建议修复方案**:  
改为通用 shebang 或在 cron 中显式指定解释器：
```python
#!/usr/bin/env python3
```

---

### P1-10. Trailing Check PnL 计算使用 `pos['total']` 而非实际成交数据

| 项目 | 详情 |
|------|------|
| **文件** | `src/cmd_trailing_check.py` |
| **行号** | L305-310 |
| **严重性** | 🟡 PnL 记录不准确 |

**问题描述**:  
当检测到持仓已消失（SL/TP 已成交）时：
```python
qty = sym_pos['total'] if sym_pos else sym_info.get('qty', 0)
# ...
pnl = (exit_price - entry_price) * qty
```

`sym_pos` 是 None（持仓已消失），所以 `qty = sym_info.get('qty', 0)`。但 trailing_stop 表中存储的 `qty` 字段可能不存在或为 0（TrailingStop 的 state 结构不包含 qty 字段），导致 PnL 计算为 0。

**建议修复方案**:  
从 Binance 交易历史获取实际成交数量：
```python
trades = client.get_my_trades(symbol=symbol, limit=5)
actual_qty = sum(float(t.get('qty', 0)) for t in trades)
```

---

## P2 — 改进建议（长期优化）

### P2-1. 缺少结构化日志

| 项目 | 详情 |
|------|------|
| **文件** | 所有模块 |
| **严重性** | 🟢 运维效率 |

**问题描述**:  
所有模块使用标准 `logging.getLogger(__name__)`，没有结构化字段（trade_id, symbol, strategy 等）。在排查问题时需要在大量文本日志中搜索。

**建议**:  
引入结构化日志（如 `structlog` 或 JSON 格式），为每笔交易添加唯一 ID 贯穿全流程。

---

### P2-2. 测试覆盖以验证脚本为主，缺少真正的单元测试

| 项目 | 详情 |
|------|------|
| **文件** | `tests/` (33 files), `scripts/verify_*.py` (20+ files) |
| **严重性** | 🟢 质量保障 |

**问题描述**:  
`scripts/` 目录下有大量 `verify_*.py` 文件（verify_phase0.py 到 verify_phase5.py 等），这些更像是集成验证脚本而非单元测试。`tests/` 目录下虽然存在 33 个测试文件，但核心模块如 `execute_auto_trade` 缺少充分的 mock 测试。

**关键测试盲区**:
- `execute_auto_trade` 的各种降级路径（Kelly fallback、OCO fallback、tiered exit fallback）
- SL 订单失败后的恢复逻辑
- 并发场景下的状态一致性
- 极端市场条件下的风控触发

**建议**:  
1. 为 `execute_auto_trade` 拆分后的子函数编写 mock-based 单元测试
2. 添加边界条件测试（$0 余额、最小交易额边界、step size 边界）
3. 设置 CI 中的测试覆盖率门槛

---

### P2-3. 配置分散在多处

| 项目 | 详情 |
|------|------|
| **文件** | `config/risk_limits.yaml`, `config/strategies.yaml`, `.env`, Python 常量 |
| **严重性** | 🟢 可维护性 |

**问题描述**:  
风控参数分散在：
- `config/risk_limits.yaml`: max_position_pct, stop_loss_pct 等
- `.env`: API keys, AUTO_EXECUTE, USE_CCXT
- Python 代码中的硬编码常量：
  - `trade_executor.py`: `MIN_STOP_LOSS_PCT = 3.0`, `MAX_SINGLE_LOSS_PCT = 5.0`
  - `circuit_breaker.py`: `CONSECUTIVE_FAILURES_MAX = 5`, `DRAWDOWN_TRIP_PCT = 20.0`
  - `daily_loss_breaker.py`: `TIER_1_LOSS_PCT = 1.0` 等
  - `drawdown_breaker.py`: `HARD_STOP_PCT = 0.10`
  - `stepwise_drawdown.py`: `LEVELS` 字典

某些参数在多个地方有重叠但不一致：
- `drawdown_breaker.py`: `HARD_STOP_PCT = 0.10` (10%)
- `circuit_breaker.py`: `DRAWDOWN_TRIP_PCT = 20.0` (20%)
- `risk_limits.yaml`: `max_drawdown_pct: 15`

三个不同的回撤阈值分散在三个文件中。

**建议**:  
将所有风控参数集中到一个 `risk_config.yaml` 中，Python 代码从配置文件读取。

---

### P2-4. `StateDB` WAL 模式的检查点策略

| 项目 | 详情 |
|------|------|
| **文件** | `src/state_db.py` |
| **行号** | L78-85 |
| **严重性** | 🟢 数据库膨胀 |

**问题描述**:  
StateDB 使用 WAL 模式，但 WAL checkpoint 只在连接回收（5 分钟周期）时执行：
```python
if now - conn_age > 300:  # 5 minutes
    self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```

如果连接持续活跃（每 5 分钟内都有操作），连接永远不会被回收，WAL 文件会持续增长。

**建议**:  
添加定期 checkpoint cron 任务，或在每次 scan 完成后显式执行 checkpoint。

---

### P2-5. `FeishuNotifier` 通知发送无重试和队列

| 项目 | 详情 |
|------|------|
| **文件** | `src/trade_executor.py` (多处), `src/cmd_trailing_check.py` |
| **行号** | 分散 |
| **严重性** | 🟢 运维通知可靠性 |

**问题描述**:  
通知发送代码通常长这样：
```python
try:
    notifier.send_text(f"🚨 URGENT: SL failed for {symbol}!")
except Exception:
    logger.error("Failed to send SL failure alert notification", exc_info=True)
```

如果飞书 API 临时不可用，关键通知（SL 失败、回撤触发）会丢失。没有重试、没有本地队列、没有备用通知渠道。

**建议**:  
1. 为关键通知添加重试机制（3 次重试 + 指数退避）
2. 实现本地通知队列（持久化到 StateDB），定期重发失败通知
3. 考虑多渠道通知（飞书 + 邮件 + Telegram）

---

### P2-6. 缺少优雅关闭机制

| 项目 | 详情 |
|------|------|
| **文件** | `src/ws_user_stream.py`, 整体系统 |
| **严重性** | 🟢 数据一致性 |

**问题描述**:  
`UserDataStream.stop()` 直接关闭 WebSocket，没有等待 pending 操作完成。整个系统没有 `SIGTERM` 信号处理器，当 cron 任务被 kill 时可能丢失正在进行的交易操作。

**建议**:  
1. 添加 `signal.SIGTERM` 处理器，在收到终止信号时完成当前操作
2. 在 `execute_auto_trade` 中添加原子性检查点（交易完成 → 持久化 → 通知）
3. 启动时检查上次运行是否正常退出，如果有中断标记则进入恢复模式

---

### P2-7. SmartOrder 与 TradeExecutor 功能重叠

| 项目 | 详情 |
|------|------|
| **文件** | `src/smart_order.py`, `src/trade_executor.py` |
| **严重性** | 🟢 代码维护 |

**问题描述**:  
两个模块都实现了：
- 仓位大小计算（SmartOrder 用 score+volatility，TradeExecutor 用 Kelly）
- SL/TP 设置（SmartOrder 用 ATR，TradeExecutor 用百分比）
- 订单放置（两者都有完整的 SL-first-then-TP 逻辑）

`execute_auto_trade` 实际上直接调用了 `SmartOrder.get_symbol_filters()` 用于获取过滤器信息，但没有使用 SmartOrder 的核心交易逻辑。这导致两套并行实现，修改一处可能遗漏另一处。

**建议**:  
明确分工：
- SmartOrder 负责 ATR-based SL/TP 计算和过滤器处理（纯计算，无副作用）
- TradeExecutor 负责编排（调用 SmartOrder 计算 → 执行交易 → 管理状态）

---

### P2-8. 缺少 API 调用预算/权重追踪

| 项目 | 详情 |
|------|------|
| **文件** | `src/_binance_sdk_client.py` |
| **严重性** | 🟢 运维稳定性 |

**问题描述**:  
Binance API 有请求权重限制（每分钟 1200 权重，account endpoint 权重为 10）。系统没有追踪已使用的权重，可能在密集扫描时触发限流（429/418 错误）。

`get_klines` 方法有 `max_retries=5` 和 SSL 重试，但对 429 错误只是 `logger.warning` 后返回空列表，没有真正的退避等待（只有重试，没有指数退避）。

**建议**:  
1. 添加 API 权重追踪器（从 response headers 的 `X-MBX-USED-WEIGHT` 读取）
2. 当权重接近限制时主动降速（delay requests）
3. 对 429/418 错误实现 `Retry-After` 头解析后的真正等待

---

### P2-9. `_parse_retry_after` 存在但未被使用

| 项目 | 详情 |
|------|------|
| **文件** | `src/_binance_sdk_client.py` |
| **行号** | L57-65 |
| **严重性** | 🟢 限流处理 |

**问题描述**:  
代码中定义了 `_parse_retry_after()` 函数来解析 `Retry-After` 头，但在实际的 `get_klines` 和其他方法中，对 429/418 错误只是记录警告并返回空列表，没有调用此函数进行等待。

```python
# L310-313: 遇到 429/418 只是 warning，没有 Retry-After 处理
if e.status_code in (429, 418, 400):
    logger.warning(f"Binance API warning (klines {symbol}): [{e.status_code}] {msg}")
```

**建议**:  
在遇到 429/418 时，解析 `Retry-After` 头并 sleep 对应时间后重试。

---

### P2-10. `run_cron.sh` 代理故障时仅 `|| true`

| 项目 | 详情 |
|------|------|
| **文件** | `run_cron.sh` |
| **行号** | L111 |
| **严重性** | 🟢 运维可靠性 |

**问题描述**:  
```bash
ensure_proxy || true  # ← 代理启动失败也不阻止后续命令
```

如果所有代理节点都不可用，`ensure_proxy` 返回 1，但 `|| true` 使得脚本继续执行。随后的 Python 交易命令会因为无法连接 Binance API 而失败，可能产生误导性的错误。

**建议**:  
代理失败时应该退出而不是继续：
```bash
if ! ensure_proxy; then
    echo "[$(date)] [FATAL] No proxy available, aborting $CMD" >> "$LOGFILE"
    # Send notification
    exit 1
fi
```

---

## 总结与优先级矩阵

### 问题统计

| 优先级 | 数量 | 类别 |
|--------|------|------|
| P0 | 7 | 安全风险 + 数据一致性 + 核心功能不可用 |
| P1 | 10 | 可靠性 + 性能 + 正确性 |
| P2 | 10 | 可维护性 + 运维效率 + 测试覆盖 |

### 优先级矩阵

| | 紧急 | 重要 | 可延迟 |
|---|---|---|---|
| **高影响** | P0-1 (fail-open), P0-2 (WS无代理), P0-6 (SL裸露窗口) | P1-1 (API重复调用), P1-6 (无幂等性) | P2-1 (结构化日志), P2-7 (功能重叠) |
| **中影响** | P0-3 (fapi直连), P0-4 (单例线程安全), P0-7 (密码硬编码) | P1-2 (异常吞灭), P1-3 (tier不降), P1-5 (sync非原子), P1-8 (count fail-open) | P2-2 (测试覆盖), P2-3 (配置分散), P2-8 (API权重) |
| **低影响** | P0-5 (函数过长) | P1-4 (类级缓存), P1-7 (SSL可关), P1-9 (shebang), P1-10 (PnL计算) | P2-4 (WAL checkpoint), P2-5 (通知重试), P2-6 (优雅关闭), P2-9 (retry_after未用), P2-10 (代理||true) |

### 建议修复顺序

1. **立即修复** (本周内): P0-1, P0-2, P0-3, P0-6, P0-7
2. **短期修复** (2周内): P0-4, P0-5, P1-1, P1-2, P1-6, P1-8
3. **中期改进** (1个月内): P1-3, P1-4, P1-5, P1-7, P1-9, P1-10
4. **长期优化** (持续): 所有 P2 项目

### 架构层面建议

1. **模块解耦**: `execute_auto_trade` 需要拆分为编排器 + 执行器 + 风控检查器
2. **统一错误处理**: 建立 centralized error handler，统一 fail-open/fail-closed 策略
3. **配置中心**: 所有硬编码参数移到 YAML 配置
4. **可观测性**: 添加 Prometheus metrics 或类似指标导出，监控 API 权重、SL 覆盖率、风控触发频率
5. **交易审计**: 每笔交易从信号生成到订单执行的全链路追踪

---

*报告生成时间: 2026-06-14*  
*审查工具: 人工代码审查 + grep 分析*
