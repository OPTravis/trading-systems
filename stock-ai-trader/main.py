#!/usr/bin/env python3
"""
stock-ai-trader CLI 入口

全球股票自動交易系統 — 基於因子投資 + 截面排名
交易限制: SPOT ONLY（不做期貨、期權、杠杆）

Commands:
    scan       - 掃描全市場，生成交易信號
    status     - 顯示持倉、P&L、風控狀態
    analyze    - 深度分析指定股票（基本面 + 技術面 + 情緒）
    trade      - 執行交易（需確認）
    backtest   - 回測策略（Walk-forward 驗證）

Usage:
    python main.py scan [--universe global] [--market US]
    python main.py status [--detailed]
    python main.py analyze AAPL MSFT
    python main.py trade [--dry-run] [--confirm]
    python main.py backtest [--strategy momentum] [--from 2024-01-01] [--to 2026-01-01]
"""

import argparse
import asyncio
import nest_asyncio  # noqa: E402
nest_asyncio.apply()
import json
import sys
import os
import logging
from pathlib import Path
from datetime import datetime

# Add project root and parent (for shared module) to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("stock-ai-trader")


def load_config(config_name: str = "config") -> dict:
    """Load YAML config file."""
    import yaml
    config_path = PROJECT_ROOT / "config" / f"{config_name}.yaml"
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_env():
    """Load environment variables from .env file."""
    from dotenv import load_dotenv
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        print("Warning: .env file not found. Copy .env.example to .env and fill in your API keys.")


# ============================================================
# Broker Factory
# ============================================================
def create_broker(config: dict, sync: bool = False):
    """Create broker client based on config.
    
    Args:
        sync: If True, return SyncIBKRWrapper (synchronous) instead of IBKRClient (async).
    """
    ibkr_cfg = config.get("ibkr", {})
    host = ibkr_cfg.get("host", "127.0.0.1")
    port = ibkr_cfg.get("gateway", {}).get("paper_port", 4001)
    client_id = ibkr_cfg.get("client_id", 1)

    if sync:
        from src.brokers.sync_ibkr_wrapper import SyncIBKRWrapper
        broker = SyncIBKRWrapper(host=host, port=port, client_id=client_id)
        broker.connect()
        return broker
    else:
        from src.brokers import IBKRClient
        return IBKRClient(
            host=host,
            port=port,
            client_id=client_id,
            max_reconnect_attempts=ibkr_cfg.get("max_reconnect_attempts", 3),
        )


def build_orchestrator(config: dict, broker=None):
    """Build ScanOrchestrator with all dependencies wired up."""
    from src.portfolio import PortfolioManager
    from src.data.stock_data_feed import StockDataFeed
    from src.scoring.stock_scorer import StockScorer
    from src.scoring.composite_ranker import CompositeRanker
    from src.risk.stock_risk_manager import StockRiskManager
    from src.market.regime_detector import RegimeDetector
    from src.research.stock_researcher import StockResearcher
    from src.trade_executor import TradeExecutor, HybridPositionSizer
    from src.scan_orchestrator import ScanOrchestrator
    from src.notifier import FeishuNotifier
    from src.data.feature_store import FeatureStore

    if broker is None:
        broker = create_broker(config, sync=True)

    portfolio = PortfolioManager()
    data_feed = StockDataFeed(ibkr_client=broker)
    scorer = StockScorer(feature_store=FeatureStore())
    ranker = CompositeRanker()
    risk_mgr = StockRiskManager()
    regime_detector = RegimeDetector()
    researcher = StockResearcher(data_feed=data_feed)
    position_sizer = HybridPositionSizer()
    trade_executor = TradeExecutor(broker=broker, position_sizer=position_sizer)
    notifier = FeishuNotifier()

    return ScanOrchestrator(
        broker=broker,
        portfolio=portfolio,
        stock_data_feed=data_feed,
        stock_scorer=scorer,
        composite_ranker=ranker,
        risk_manager=risk_mgr,
        regime_detector=regime_detector,
        stock_researcher=researcher,
        position_sizer=position_sizer,
        trade_executor=trade_executor,
        config=config,
    )


async def connect_broker(broker) -> bool:
    """Connect to broker with timeout."""
    try:
        await asyncio.wait_for(broker.connect(), timeout=15)
        return True
    except Exception as e:
        logger.error("Broker connection failed: %s", e)
        return False


# ============================================================
# Command: scan
# ============================================================
def cmd_scan(args):
    """掃描全市場，生成交易信號"""
    print(f"[*] Scanning universe: {args.universe}, market: {args.market}")
    print(f"[*] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    config = load_config("config")
    strategies = load_config("strategies")

    # Build orchestrator and connect broker
    broker = create_broker(config, sync=True)
    if not broker.is_connected():
        print("[!] Failed to connect to broker. Running with limited data.")

    orchestrator = build_orchestrator(config, broker)

    # Run scan pipeline
    auto_execute = os.environ.get("AUTO_EXECUTE", "").lower() == "true"
    result = orchestrator.run(
        universe_name=args.universe,
        auto_execute=auto_execute,
        top_n_research=5,
        min_score=config.get("scanner", {}).get("signal_threshold", 0.6) * 100,
    )

    # Display results
    print(f"\n{'='*60}")
    print(f"  SCAN RESULTS — {result.timestamp}")
    print(f"{'='*60}")
    print(f"  Regime:            {result.regime}")
    print(f"  Universe size:     {result.universe_size}")
    print(f"  Candidates scored: {result.candidates_scored}")
    print(f"  Research done:     {result.research_completed}")
    print(f"  Signals:           {len(result.signals)}")
    print(f"  Blocked:           {len(result.blocked)}")
    print(f"  Duration:          {result.duration_sec:.1f}s")

    if result.signals:
        print(f"\n{'='*60}")
        print(f"  TRADE SIGNALS")
        print(f"{'='*60}")
        for sig in result.signals:
            print(f"  {sig.side} {sig.symbol}")
            print(f"    Score: {sig.score:.1f} | Strategy: {sig.strategy}")
            print(f"    Price: {sig.price:.2f} {sig.currency}")
            if sig.stop_loss:
                print(f"    Stop Loss: {sig.stop_loss:.2f}")
            if sig.take_profit:
                print(f"    Take Profit: {sig.take_profit:.2f}")
            print(f"    Size: ${sig.position_size_usd:,.0f}")
            if sig.research_summary:
                print(f"    Research: {sig.research_summary[:100]}...")
            print()

    if result.blocked:
        print(f"\n  BLOCKED ({len(result.blocked)}):")
        for b in result.blocked:
            print(f"    {b.get('symbol', '?')}: {b.get('reason', 'unknown')}")

    # Disconnect broker
    if hasattr(broker, '_ib') and broker._ib.isConnected():
        broker.disconnect()


# ============================================================
# Command: status
# ============================================================
def cmd_status(args):
    """顯示持倉、P&L、風控狀態"""
    from ib_insync import IB
    print(f"[*] Portfolio Status")
    print(f"[*] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    config = load_config("config")
    mode = config.get("system", {}).get("mode", "unknown")
    print(f"[*] Mode: {mode}")

    # Connect directly via ib_insync for reliability
    ibkr_cfg = config.get("ibkr", {})
    host = ibkr_cfg.get("host", "127.0.0.1")
    port = ibkr_cfg.get("gateway", {}).get("paper_port", 4001)
    client_id = ibkr_cfg.get("client_id", 1)

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
    except Exception as e:
        print(f"[!] Cannot connect to IBKR: {e}")
        return

    print(f"[*] Connected to IBKR: {ib.managedAccounts()}")

    # Account summary
    print(f"\n{'='*60}")
    print(f"  ACCOUNT")
    print(f"{'='*60}")
    acct = ib.accountSummary()
    for item in acct:
        if item.tag in ['TotalCashValue', 'NetLiquidation', 'BuyingPower',
                        'UnrealizedPnL', 'AvailableFunds']:
            print(f"  {item.tag}: {float(item.value):,.2f} {item.currency}")

    # Positions
    positions = ib.positions()
    if positions:
        print(f"\n{'='*60}")
        print(f"  POSITIONS ({len(positions)})")
        print(f"{'='*60}")
        for p in positions:
            symbol = p.contract.symbol
            qty = p.position
            avg_cost = p.avgCost
            # Get current price
            try:
                ib.qualifyContracts(p.contract)
                [ticker] = ib.reqTickers(p.contract)
                current = ticker.marketPrice()
                if current != current:  # NaN check
                    current = avg_cost
            except Exception:
                current = avg_cost

            market_val = current * qty
            pnl = (current - avg_cost) * qty
            pnl_pct = ((current / avg_cost) - 1) * 100 if avg_cost else 0
            marker = "+" if pnl >= 0 else ""
            print(f"  {symbol}: {qty:.0f} shares")
            print(f"    Entry: {avg_cost:.2f} → Current: {current:.2f}")
            print(f"    P&L: {marker}{pnl:,.2f} ({marker}{pnl_pct:.1f}%)")
            print(f"    Value: {market_val:,.2f} {p.contract.currency}")
    else:
        print("\n  No open positions.")

    # Open orders
    orders = ib.openOrders()
    if orders:
        print(f"\n{'='*60}")
        print(f"  OPEN ORDERS ({len(orders)})")
        print(f"{'='*60}")
        for o in orders:
            print(f"  {o.action} {o.totalQuantity} {o.orderType} LMT={getattr(o, 'lmtPrice', 'N/A')}")
    else:
        print("\n  No open orders.")

    # Regime & Risk (if --detailed)
    if args.detailed:
        print(f"\n{'='*60}")
        print(f"  REGIME & RISK")
        print(f"{'='*60}")
        try:
            ib.reqMarketDataType(3)  # delayed
            from src.market.regime_detector import RegimeDetector
            from src.data.stock_data_feed import StockDataFeed
            # Dummy broker for data feed (just needs ib_insync IB)
            class _DummyBroker:
                def __init__(self, ib_ref):
                    self._ib = ib_ref
            detector = RegimeDetector(data_feed=StockDataFeed(broker=_DummyBroker(ib)))
            vix_contract = __import__('ib_insync').Stock('^VIX', 'CBOE', 'USD')
            ib.qualifyContracts(vix_contract)
            [vix_ticker] = ib.reqTickers(vix_contract)
            vix = vix_ticker.marketPrice()
            regime = detector.detect_regime(vix=vix if vix == vix else None)
            print(f"  Regime: {regime}")
            if vix == vix:  # not NaN
                print(f"  VIX: {vix:.2f}")
        except Exception as e:
            print(f"  Regime detection failed: {e}")

    ib.disconnect()

    # Live account via CPG (if --live)
    if getattr(args, 'live', False):
        print(f"\n{'='*60}")
        print(f"  LIVE ACCOUNT (CPG)")
        print(f"{'='*60}")
        from src.brokers.cpg_client import CPGClient
        cpg = CPGClient()
        live = cpg.get_live_status()
        if live is None:
            print("  [!] CPG session expired or not running.")
            print("  [*] Login at https://localhost:5000 in browser first.")
        else:
            s = live["summary"]
            print(f"  Account:     {s.get('account_id', '?')}")
            print(f"  Total Cash:  {s.get('total_cash', 0):,.2f} {s.get('currency', '')}")
            print(f"  Net Liq:     {s.get('net_liquidation', 0):,.2f} {s.get('currency', '')}")
            print(f"  Available:   {s.get('available_funds', 0):,.2f} {s.get('currency', '')}")
            print(f"  Buying Power:{s.get('buying_power', 0):,.2f} {s.get('currency', '')}")
            positions = live["positions"]
            if positions:
                print(f"\n  Positions ({len(positions)}):")
                for p in positions:
                    pnl = p.get("unrealized_pnl", 0)
                    marker = "+" if pnl >= 0 else ""
                    print(f"    {p['symbol']}: {p['quantity']:.0f} @ {p['avg_cost']:.2f} | "
                          f"P&L: {marker}{pnl:,.2f} {p.get('currency', '')}")
            else:
                print("  No open positions.")

    print(f"\n[*] Done.")


# ============================================================
# Command: analyze
# ============================================================
def cmd_analyze(args):
    """深度分析指定股票"""
    symbols = args.symbols
    print(f"[*] Analyzing: {', '.join(symbols)}")
    print(f"[*] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    config = load_config("config")
    broker = create_broker(config, sync=True)

    if not broker.is_connected():
        print("[!] Failed to connect to broker. Analysis will be limited.")

    orchestrator = build_orchestrator(config, broker)

    for symbol in symbols:
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")

        result = orchestrator.analyze_symbol(symbol)

        # Quote
        quote = result.get("quote", {})
        if quote:
            price = quote.get("price", 0)
            change = quote.get("change_pct", 0)
            vol = quote.get("volume", 0)
            marker = "+" if change >= 0 else ""
            print(f"  Price:    {price:.2f} ({marker}{change:.2f}%)")
            print(f"  Volume:   {vol:,.0f}")

        # Factor scores
        factors = result.get("factor_scores", {})
        if factors:
            print(f"\n  Factor Scores:")
            print(f"    Composite:    {factors.get('composite', 0):.1f}")
            for dim in ['technical', 'fundamental', 'momentum', 'sentiment', 'quality', 'value']:
                score = factors.get(dim, 0)
                bar = "█" * int(score / 5) + "░" * (20 - int(score / 5))
                print(f"    {dim:14s} {score:5.1f} {bar}")

        # Research
        research = result.get("research", {})
        if research:
            print(f"\n  Research:")
            # ResearchReport is a dataclass, access via attributes
            if hasattr(research, 'recommendation'):
                recommendation = research.recommendation
                # Handle enum or string
                if hasattr(recommendation, 'value'):
                    recommendation = recommendation.value
                confidence = research.confidence
                summary = research.summary
                print(f"    Recommendation: {recommendation} (confidence: {confidence:.0%})")
                if summary:
                    print(f"    Summary: {summary[:200]}...")
                if hasattr(research, 'risk_rating'):
                    print(f"    Risk: {research.risk_rating}")
                if hasattr(research, 'bear_case') and research.bear_case:
                    print(f"    Bear case: {research.bear_case[:150]}...")
            elif isinstance(research, dict):
                recommendation = research.get("recommendation", "N/A")
                confidence = research.get("confidence", 0)
                summary = research.get("summary", "")
                print(f"    Recommendation: {recommendation} (confidence: {confidence:.0%})")
                if summary:
                    print(f"    Summary: {summary[:200]}...")

    # Disconnect
    if broker.is_connected():
        broker.disconnect()


# ============================================================
# Command: trade
# ============================================================
def cmd_trade(args):
    """執行交易"""
    config = load_config("config")
    risk_limits = load_config("risk_limits")
    mode = config.get("system", {}).get("mode", "paper")

    print(f"[*] Trade execution")
    print(f"[*] Mode: {mode}")
    print(f"[*] Dry run: {args.dry_run}")
    print(f"[*] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if mode == "live" and not args.confirm:
        print("[!] Live mode requires --confirm flag")
        sys.exit(1)

    broker = create_broker(config, sync=True)
    if not broker.is_connected():
        print("[!] Failed to connect to broker. Cannot execute trades.")
        sys.exit(1)

    orchestrator = build_orchestrator(config, broker)

    # Run scan to get signals
    print("[*] Running scan to generate signals...")
    result = orchestrator.run(
        universe_name=getattr(args, 'universe', 'global'),
        auto_execute=False,
        top_n_research=3,
        min_score=config.get("scanner", {}).get("signal_threshold", 0.6) * 100,
    )

    if not result.signals:
        print("[*] No signals generated. Nothing to trade.")
        if hasattr(broker, '_ib') and broker._ib.isConnected():
            broker.disconnect()
        return

    print(f"\n[*] {len(result.signals)} signals ready:")
    for sig in result.signals:
        print(f"    {sig.side} {sig.symbol} — Score: {sig.score:.1f}, Size: ${sig.position_size_usd:,.0f}")

    if args.dry_run:
        print("\n[DRY RUN] No trades executed.")
    else:
        print(f"\n[*] Executing {len(result.signals)} trades...")
        orchestrator._phase5_execute(result.signals)
        print("[*] Execution complete.")

    # Disconnect
    if hasattr(broker, '_ib') and broker._ib.isConnected():
        broker.disconnect()


# ============================================================
# Command: backtest
# ============================================================
def cmd_backtest(args):
    """回測策略"""
    print(f"[*] Backtest: strategy={args.strategy}")
    print(f"[*] Period: {args.from_date} to {args.to_date}")
    print(f"[*] Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    strategies = load_config("strategies")
    risk_limits = load_config("risk_limits")

    from src.walk_forward import WalkForwardEngine

    engine = WalkForwardEngine(
        strategies_config=strategies,
        risk_config=risk_limits,
    )

    result = engine.run(
        strategy_name=args.strategy if args.strategy != "all" else None,
        start_date=args.from_date,
        end_date=args.to_date,
        universe=args.universe,
    )

    # Display results
    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS")
    print(f"{'='*60}")
    metrics = result.get("metrics", {})
    print(f"  Total Return:    {metrics.get('total_return', 0):.2%}")
    print(f"  Sharpe Ratio:    {metrics.get('sharpe', 0):.2f}")
    print(f"  Sortino Ratio:   {metrics.get('sortino', 0):.2f}")
    print(f"  Max Drawdown:    {metrics.get('max_drawdown', 0):.2%}")
    print(f"  Win Rate:        {metrics.get('win_rate', 0):.1%}")
    print(f"  Profit Factor:   {metrics.get('profit_factor', 0):.2f}")
    print(f"  Total Trades:    {metrics.get('total_trades', 0)}")


# ============================================================
# Main CLI parser
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="stock-ai-trader: 全球股票自動交易系統",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py scan --universe sp500 --market US
  python main.py status --detailed
  python main.py analyze AAPL MSFT NVDA
  python main.py trade --dry-run
  python main.py backtest --strategy momentum --from 2024-01-01 --to 2026-01-01
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scan
    p_scan = subparsers.add_parser("scan", help="掃描全市場，生成交易信號")
    p_scan.add_argument("--universe", default="global", help="Stock universe (default: global)")
    p_scan.add_argument("--market", default="US", help="Market: US/HK/CN (default: US)")
    p_scan.add_argument("--strategy", default="all", help="Strategy filter (default: all)")

    # status
    p_status = subparsers.add_parser("status", help="顯示持倉、P&L、風控狀態")
    p_status.add_argument("--detailed", action="store_true", help="Show detailed info")
    p_status.add_argument("--live", action="store_true", help="Show live account via CPG (localhost:5000)")

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="深度分析指定股票")
    p_analyze.add_argument("symbols", nargs="+", help="Stock symbols to analyze")

    # trade
    p_trade = subparsers.add_parser("trade", help="執行交易")
    p_trade.add_argument("--universe", default="global", help="Stock universe (default: global)")
    p_trade.add_argument("--dry-run", action="store_true", help="Simulate without executing")
    p_trade.add_argument("--confirm", action="store_true", help="Confirm live execution")

    # backtest
    p_backtest = subparsers.add_parser("backtest", help="回測策略")
    p_backtest.add_argument("--strategy", default="all", help="Strategy to backtest")
    p_backtest.add_argument("--from", dest="from_date", default="2024-01-01", help="Start date")
    p_backtest.add_argument("--to", dest="to_date", default="2026-01-01", help="End date")
    p_backtest.add_argument("--universe", default="global", help="Stock universe")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Load environment
    load_env()

    # Dispatch command
    commands = {
        "scan": cmd_scan,
        "status": cmd_status,
        "analyze": cmd_analyze,
        "trade": cmd_trade,
        "backtest": cmd_backtest,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
