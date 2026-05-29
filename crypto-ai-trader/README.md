# AI Crypto Trading System

Binance SPOT AI 智能交易系统

## 功能

- **市场扫描** - 全市场扫描，筛选有机会的币种
- **情绪分析** - 新闻情绪 + 恐慌贪婪指数
- **6种交易策略** - 网格/DCA/趋势/RSI/布林/VWAP
- **持仓管理** - 动态仓位 + 风险管理
- **回测引擎** - 策略对比 + 参数优化
- **自动执行** - Binance SPOT API

## 安装

```bash
cd ~/crypto_ai_trader
pip install -r requirements.txt
```

## 配置

复制环境变量：
```bash
export BINANCE_API_KEY=your_key
export BINANCE_API_SECRET=your_secret
```

## 使用

```bash
# 扫描市场机会
python main.py scan

# 情绪分析
python main.py sentiment

# 查看持仓状态
python main.py status

# 策略回测
python main.py backtest

# 交易循环
python main.py trade
```

## 策略说明

| 策略 | 描述 | 适合场景 |
|------|------|---------|
| Grid | 网格交易，低买高卖 | 震荡市 |
| DCA | 定投策略，逢跌加仓 | 长期持有 |
| Trend | 趋势跟踪，MA金叉 | 趋势明确 |
| RSI | RSI均值回归 | 超卖超买 |
| Bollinger | 布林带突破 | 波动突破 |
| VWAP | VWAP分布 | 机构参考 |

## 文件结构

```
crypto_ai_trader/
├── config/          # 配置文件
│   ├── strategies.yaml
│   └── risk_limits.yaml
├── src/
│   ├── binance_client.py   # Binance API
│   ├── indicators.py       # 技术指标
│   ├── market_scanner.py  # 市场扫描
│   ├── sentiment.py       # 情绪分析
│   ├── portfolio.py       # 持仓管理
│   ├── backtester.py      # 回测引擎
│   └── strategies/         # 交易策略
│       ├── grid.py
│       ├── dca.py
│       ├── trend.py
│       ├── rsi_reversion.py
│       ├── bollinger.py
│       └── vwap.py
├── data/             # 数据缓存
├── main.py          # 主入口
└── requirements.txt
```

## 风险提示

⚠️ SPOT ONLY - 不使用合约/杠杆/期权
⚠️ 策略仅供参考，实盘前请充分回测
⚠️ 控制仓位，遵守风险管理规则
