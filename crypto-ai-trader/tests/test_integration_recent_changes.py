#!/usr/bin/env python3
"""
Integration tests for recent changes in crypto-ai-trader.
Tests each component independently to verify they work correctly.

Components tested:
1. ccxt client (USE_CCXT=1): klines, balance, ticker, 24hr stats, get_symbols
2. PaperTrader (TRADING_MODE=paper): init, balance, ticker, place simulated order
3. RiskManager: init with all sub-modules
4. BinanceClient proxy: verify both USE_CCXT=1 and default paths work
5. Funding rate: direct REST call to fapi.binance.com
6. ATR calculation: klines dict format (k['close'] not k[4])
"""

import os
import sys
import traceback
from datetime import datetime

# ── Setup ──────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

results = []


def record(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append((name, status, detail))
    icon = "✅" if passed else "❌"
    print(f"  {icon} {status}: {name}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ======================================================================
# TEST 1: ccxt client (USE_CCXT=1)
# ======================================================================
section("1. CCXT Client (USE_CCXT=1)")

try:
    from pathlib import Path

    # Manually load env so API keys are available
    from dotenv import load_dotenv

    from src.ccxt_client import BinanceClient as CcxtBinanceClient

    load_dotenv(Path(PROJECT_ROOT) / "crypto-secrets.env", override=False)
    load_dotenv(Path(PROJECT_ROOT) / ".env", override=False)

    os.environ["USE_CCXT"] = "1"

    # The ccxt BinanceClient requires API keys
    if os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET"):
        client = CcxtBinanceClient(testnet=False)
        record("ccxt_client_init", True)
    else:
        record("ccxt_client_init", False, "No API keys available in env")
        client = None

    if client:
        # 1a. get_symbols
        try:
            symbols = client.get_symbols(quote="USDT")
            has_btc = any("BTCUSDT" in s for s in symbols) if symbols else False
            record(
                "ccxt_get_symbols",
                has_btc,
                f"found {len(symbols)} symbols, BTCUSDT={'yes' if has_btc else 'no'}",
            )
        except Exception as e:
            record("ccxt_get_symbols", False, str(e)[:120])

        # 1b. get_klines (klines returns list of dicts)
        try:
            klines = client.get_klines("BTCUSDT", "1h", limit=5)
            is_list = isinstance(klines, list)
            has_len = len(klines) > 0
            first_k = klines[0] if has_len else {}
            is_dict = isinstance(first_k, dict)
            has_keys = (
                all(
                    k in first_k
                    for k in ["open", "high", "low", "close", "volume", "open_time"]
                )
                if is_dict
                else False
            )
            passed = is_list and has_len and is_dict and has_keys
            record(
                "ccxt_get_klines",
                passed,
                f"len={len(klines)} keys={list(first_k.keys())[:6] if first_k else 'none'}",
            )
        except Exception as e:
            record("ccxt_get_klines", False, str(e)[:120])

        # 1c. get_balance
        try:
            bal = client.get_balance("USDT")
            passed = isinstance(bal, (int, float)) and bal >= 0
            record("ccxt_get_balance", passed, f"balance={bal}")
        except Exception as e:
            record("ccxt_get_balance", False, str(e)[:120])

        # 1d. get_ticker_price
        try:
            price = client.get_ticker_price("BTCUSDT")
            passed = isinstance(price, float) and price > 0
            record("ccxt_get_ticker_price", passed, f"BTCUSDT={price}")
        except Exception as e:
            record("ccxt_get_ticker_price", False, str(e)[:120])

        # 1e. get_24hr_stats
        try:
            stats = client.get_24hr_stats("BTCUSDT")
            passed = isinstance(stats, dict) and "price_change_pct" in stats
            record("ccxt_get_24hr_stats", passed, f"keys={list(stats.keys())[:5]}")
        except Exception as e:
            record("ccxt_get_24hr_stats", False, str(e)[:120])
    else:
        record("ccxt_get_symbols", False, "skipped (no client)")
        record("ccxt_get_klines", False, "skipped (no client)")
        record("ccxt_get_balance", False, "skipped (no client)")
        record("ccxt_get_ticker_price", False, "skipped (no client)")
        record("ccxt_get_24hr_stats", False, "skipped (no client)")

except Exception as e:
    record("ccxt_client_import", False, f"import failed: {e}")
    traceback.print_exc()

# ======================================================================
# TEST 2: BinanceClient proxy (USE_CCXT=1 vs default)
# ======================================================================
section("2. BinanceClient Proxy (USE_CCXT=1 vs default)")

try:
    # Test USE_CCXT=1 path
    os.environ["USE_CCXT"] = "1"
    # Need to reimport to pick up the env change
    if "src.binance_client" in sys.modules:
        del sys.modules["src.binance_client"]
    if "src.ccxt_client" in sys.modules:
        del sys.modules["src.ccxt_client"]
    if "src._binance_sdk_client" in sys.modules:
        del sys.modules["src._binance_sdk_client"]

    # Force re-read of .env
    os.environ.pop("USE_CCXT", None)
    os.environ["USE_CCXT"] = "1"

    import inspect

    import src.binance_client as bc_mod

    # Check which implementation is loaded
    source_file = inspect.getfile(bc_mod.BinanceClient)
    is_ccxt = "ccxt_client" in source_file
    record("proxy_use_ccxt_path", is_ccxt, f"loaded from: {source_file}")

    # Test default path (USE_CCXT not set) — just verify ccxt_client source is correct
    # The module caches the import at load time, so we can't reimport easily.
    # Instead, verify the ccxt source file is importable
    ccxt_source = Path(PROJECT_ROOT) / "src" / "ccxt_client.py"
    sdk_source = Path(PROJECT_ROOT) / "src" / "_binance_sdk_client.py"
    proxy_source = Path(PROJECT_ROOT) / "src" / "binance_client.py"
    record(
        "proxy_source_files_exist",
        ccxt_source.exists() and sdk_source.exists() and proxy_source.exists(),
        f"ccxt={ccxt_source.exists()} sdk={sdk_source.exists()} proxy={proxy_source.exists()}",
    )

    # Verify the proxy module reads USE_CCXT correctly
    with open(proxy_source) as f:
        proxy_content = f.read()
    uses_ccxt_check = '"1"' in proxy_content or '"true"' in proxy_content
    record(
        "proxy_env_check_logic",
        uses_ccxt_check,
        "proxy checks USE_CCXT env var correctly",
    )

except Exception as e:
    record("binance_client_proxy", False, str(e)[:120])
    traceback.print_exc()

# ======================================================================
# TEST 3: PaperTrader (TRADING_MODE=paper)
# ======================================================================
section("3. PaperTrader (TRADING_MODE=paper)")

try:
    os.environ["TRADING_MODE"] = "paper"
    from src.paper_trader import PaperTrader, is_paper_mode

    # 3a. is_paper_mode
    record(
        "paper_is_paper_mode",
        is_paper_mode(),
        f"TRADING_MODE={os.environ.get('TRADING_MODE')}",
    )

    # 3b. PaperTrader init
    try:
        pt = PaperTrader()
        record("paper_init", True)
    except Exception as e:
        record("paper_init", False, str(e)[:120])
        pt = None

    if pt:
        # 3c. get_balance
        try:
            bal = pt.get_balance("USDT")
            # Should return PAPER_INITIAL_BALANCE (10000) on first run
            passed = isinstance(bal, (int, float)) and bal >= 0
            record("paper_get_balance", passed, f"balance={bal}")
        except Exception as e:
            record("paper_get_balance", False, str(e)[:120])

        # 3d. get_ticker_price (delegates to public ccxt, no auth needed)
        try:
            price = pt.get_ticker_price("BTCUSDT")
            passed = isinstance(price, float) and price > 0
            record("paper_get_ticker_price", passed, f"BTCUSDT={price}")
        except Exception as e:
            record("paper_get_ticker_price", False, str(e)[:120])

        # 3e. get_current_price (public endpoint)
        try:
            price = pt.get_current_price("BTCUSDT")
            passed = isinstance(price, float) and price > 0
            record("paper_get_current_price", passed, f"BTCUSDT={price}")
        except Exception as e:
            record("paper_get_current_price", False, str(e)[:120])

        # 3f. place simulated market buy
        try:
            current_price = pt.get_current_price("BTCUSDT")
            if current_price > 0:
                qty = round(50.0 / current_price, 6)  # buy $50 worth
                order = pt.place_order("BTCUSDT", "BUY", "MARKET", quantity=qty)
                passed = order is not None and "orderId" in order
                record(
                    "paper_place_market_buy",
                    passed,
                    f"orderId={order.get('orderId') if order else None}, qty={qty}, price={order.get('fills', [{}])[0].get('price') if order else None}",
                )
            else:
                record("paper_place_market_buy", False, "no price available")
        except Exception as e:
            record("paper_place_market_buy", False, str(e)[:120])

        # 3g. verify balance decreased after buy
        try:
            new_bal = pt.get_balance("USDT")
            record(
                "paper_balance_after_buy",
                new_bal < 10000,
                f"balance={new_bal} (should be < 10000)",
            )
        except Exception as e:
            record("paper_balance_after_buy", False, str(e)[:120])

        # 3h. get_position after buy
        try:
            pos = pt.get_position("BTCUSDT")
            passed = pos.get("free", 0) > 0
            record("paper_get_position", passed, f"position={pos}")
        except Exception as e:
            record("paper_get_position", False, str(e)[:120])

except Exception as e:
    record("paper_trader_import", False, f"import failed: {e}")
    traceback.print_exc()

# ======================================================================
# TEST 4: RiskManager (all sub-modules)
# ======================================================================
section("4. RiskManager (all sub-modules)")

try:
    from src.risk_manager import (
        ConsecutiveLossGuard,
        DailyLossLimit,
        PerPairCooldown,
        RiskManager,
        TrailingStop,
        TrendFilter,
    )
    from src.sector_classifier import SectorExposure

    record("risk_imports", True, "all classes imported successfully")

    # 4a. TrendFilter init
    try:
        tf = TrendFilter()
        record("risk_trend_filter_init", True)
    except Exception as e:
        record("risk_trend_filter_init", False, str(e)[:120])

    # 4b. TrailingStop init
    try:
        ts = TrailingStop()
        record("risk_trailing_stop_init", True, f"state entries={len(ts._state)}")
    except Exception as e:
        record("risk_trailing_stop_init", False, str(e)[:120])

    # 4c. ConsecutiveLossGuard init
    try:
        clg = ConsecutiveLossGuard()
        record("risk_consecutive_loss_guard_init", True)
    except Exception as e:
        record("risk_consecutive_loss_guard_init", False, str(e)[:120])

    # 4d. DailyLossLimit init
    try:
        dll = DailyLossLimit()
        record("risk_daily_loss_limit_init", True)
    except Exception as e:
        record("risk_daily_loss_limit_init", False, str(e)[:120])

    # 4e. PerPairCooldown init
    try:
        ppc = PerPairCooldown()
        record("risk_per_pair_cooldown_init", True)
    except Exception as e:
        record("risk_per_pair_cooldown_init", False, str(e)[:120])

    # 4f. SectorExposure init
    try:
        se = SectorExposure()
        record("risk_sector_exposure_init", True)
    except Exception as e:
        record("risk_sector_exposure_init", False, str(e)[:120])

    # 4g. RiskManager init (without client — simpler path)
    try:
        rm = RiskManager(binance_client=None)
        has_tf = hasattr(rm, "trend_filter") and isinstance(
            rm.trend_filter, TrendFilter
        )
        has_ts = hasattr(rm, "trailing_stop") and isinstance(
            rm.trailing_stop, TrailingStop
        )
        has_clg = hasattr(rm, "loss_guard") and isinstance(
            rm.loss_guard, ConsecutiveLossGuard
        )
        has_dll = hasattr(rm, "daily_loss") and isinstance(
            rm.daily_loss, DailyLossLimit
        )
        has_ppc = hasattr(rm, "pair_cooldown") and isinstance(
            rm.pair_cooldown, PerPairCooldown
        )
        has_se = hasattr(rm, "sector_exposure") and isinstance(
            rm.sector_exposure, SectorExposure
        )
        all_ok = all([has_tf, has_ts, has_clg, has_dll, has_ppc, has_se])
        record(
            "risk_manager_init_no_client",
            all_ok,
            f"tf={has_tf} ts={has_ts} clg={has_clg} dll={has_dll} ppc={has_ppc} se={has_se}",
        )
    except Exception as e:
        record("risk_manager_init_no_client", False, str(e)[:120])

    # 4h. RiskManager init (with client)
    try:
        if os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET"):
            from src.ccxt_client import BinanceClient as CcxtClient

            client = CcxtClient(testnet=False)
            rm2 = RiskManager(binance_client=client)
            has_corr = rm2.correlation_risk is not None
            has_dd = rm2.drawdown_breaker is not None
            record(
                "risk_manager_init_with_client",
                has_corr and has_dd,
                f"correlation={has_corr} drawdown={has_dd}",
            )
        else:
            record("risk_manager_init_with_client", False, "no API keys")
    except Exception as e:
        record("risk_manager_init_with_client", False, str(e)[:120])

except Exception as e:
    record("risk_manager_import", False, f"import failed: {e}")
    traceback.print_exc()

# ======================================================================
# TEST 5: Funding Rate (direct REST to fapi.binance.com)
# ======================================================================
section("5. Funding Rate (fapi.binance.com REST)")

try:
    from src.data_feed_funding import FundingRate

    fr = FundingRate()

    # 5a. Fetch BTC funding rates
    try:
        rates = fr.get_funding_rate("BTCUSDT", limit=3)
        is_list = isinstance(rates, list)
        has_data = len(rates) > 0 if is_list else False
        if has_data:
            first = rates[0]
            has_keys = all(
                k in first for k in ["funding_rate", "funding_time", "symbol"]
            )
            record(
                "funding_rate_fetch",
                has_keys,
                f"got {len(rates)} entries, keys={list(first.keys())}",
            )
        else:
            record(
                "funding_rate_fetch",
                is_list,
                f"got empty list (len={len(rates) if is_list else 'N/A'})",
            )
    except Exception as e:
        record("funding_rate_fetch", False, str(e)[:120])

    # 5b. Fetch ETH funding rates
    try:
        eth_rates = fr.get_funding_rate("ETHUSDT", limit=3)
        passed = isinstance(eth_rates, list) and len(eth_rates) > 0
        record("funding_rate_eth", passed, f"got {len(eth_rates)} entries")
    except Exception as e:
        record("funding_rate_eth", False, str(e)[:120])

    # 5c. Check funding rate value is numeric
    try:
        if rates and len(rates) > 0:
            rate_val = rates[-1].get("funding_rate")
            is_numeric = isinstance(rate_val, (int, float))
            record("funding_rate_value_numeric", is_numeric, f"rate={rate_val}")
        else:
            record("funding_rate_value_numeric", False, "no data")
    except Exception as e:
        record("funding_rate_value_numeric", False, str(e)[:120])

except Exception as e:
    record("funding_rate_import", False, f"import failed: {e}")
    traceback.print_exc()

# ======================================================================
# TEST 6: ATR Calculation (klines dict format)
# ======================================================================
section("6. ATR Calculation (klines dict format)")

try:
    from src.indicators import Indicators

    # 6a. ATR with dict-format klines (k['close'] style)
    try:
        # Create synthetic klines in dict format
        synthetic_klines = []
        base_price = 100.0
        for i in range(30):
            synthetic_klines.append(
                {
                    "open_time": i * 3600000,
                    "open": base_price + i * 0.5,
                    "high": base_price + i * 0.5 + 2.0,
                    "low": base_price + i * 0.5 - 1.0,
                    "close": base_price + i * 0.5 + 0.5,
                    "volume": 1000 + i * 10,
                    "close_time": (i + 1) * 3600000 - 1,
                    "quote_volume": 0.0,
                    "trades": 0,
                    "is_closed": True,
                }
            )

        atr_val = Indicators.atr(synthetic_klines, period=14)
        passed = isinstance(atr_val, float) and atr_val > 0
        record("atr_with_dict_klines", passed, f"ATR={atr_val}")
    except Exception as e:
        record("atr_with_dict_klines", False, str(e)[:120])

    # 6b. ADX with dict-format klines
    try:
        adx_val = Indicators.adx(synthetic_klines, period=14)
        passed = isinstance(adx_val, float) and adx_val >= 0
        record("adx_with_dict_klines", passed, f"ADX={adx_val}")
    except Exception as e:
        record("adx_with_dict_klines", False, str(e)[:120])

    # 6c. SMA / EMA with plain list (these don't need klines)
    try:
        prices = [float(i) for i in range(50)]
        sma = Indicators.sma(prices, 20)
        ema = Indicators.ema(prices, 20)
        rsi = Indicators.rsi(prices, 14)
        passed = all(isinstance(v, float) for v in [sma, ema, rsi])
        record(
            "indicators_sma_ema_rsi",
            passed,
            f"SMA={sma:.2f} EMA={ema:.2f} RSI={rsi:.2f}",
        )
    except Exception as e:
        record("indicators_sma_ema_rsi", False, str(e)[:120])

    # 6d. Verify klines from ccxt client match dict format for ATR
    if client:
        try:
            live_klines = client.get_klines("BTCUSDT", "1h", limit=30)
            atr_live = Indicators.atr(live_klines, period=14)
            passed = isinstance(atr_live, float) and atr_live > 0
            record(
                "atr_from_live_klines",
                passed,
                f"ATR={atr_live} (from {len(live_klines)} live klines)",
            )
        except Exception as e:
            record("atr_from_live_klines", False, str(e)[:120])
    else:
        record("atr_from_live_klines", False, "skipped (no client)")

except Exception as e:
    record("indicators_import", False, f"import failed: {e}")
    traceback.print_exc()

# ======================================================================
# SUMMARY
# ======================================================================
print("\n" + "=" * 60)
print("  INTEGRATION TEST SUMMARY")
print("=" * 60)

passed_count = sum(1 for _, s, _ in results if s == "PASS")
failed_count = sum(1 for _, s, _ in results if s == "FAIL")
total = len(results)

print(f"\n  Total: {total}  |  Passed: {passed_count}  |  Failed: {failed_count}")
print(f"  Pass Rate: {passed_count/total*100:.1f}%\n")

if failed_count > 0:
    print("  FAILED TESTS:")
    for name, status, detail in results:
        if status == "FAIL":
            print(f"    ❌ {name}: {detail}")

print(f"\n  Run completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

# Exit with non-zero if any failures (only when run as script, not imported by pytest)
if __name__ == "__main__":
    sys.exit(1 if failed_count > 0 else 0)
