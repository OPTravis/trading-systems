"""
Backtest Engine — 完整的回測框架
模擬交易循環：評分 → 入場 → SL/TP → 統計
支持 BTC 趨勢過濾、追蹤止損、多幣種同時回測
"""

import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.indicators import Indicators

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """模擬倉位"""

    symbol: str
    entry_price: float
    entry_bar: int  # 入場的 K 線 index
    entry_time: int  # open_time (ms)
    quantity: float  # 買入數量
    usdt_cost: float  # 買入成本 (USDT)
    atr: float
    sl_price: float
    tp1_price: float
    tp1_size: float  # TP1 平倉數量 (40%)
    tp2_price: float
    tp2_size: float  # TP2 平倉數量 (40%)
    tp3_price: float
    tp3_size: float  # TP3 平倉數量 (20%)
    # 追蹤止損狀態
    trailing_activated: bool = False
    trailing_sl: float = 0.0
    highest_price: float = 0.0
    # TP 已觸發紀錄
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False


@dataclass
class ClosedTrade:
    """已平倉交易紀錄"""

    symbol: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    pnl_usdt: float
    reason: str  # 'sl', 'tp1', 'tp2', 'tp3', 'trailing', 'end_of_data'
    holding_bars: int
    entry_time: int
    exit_time: int
    score: float = 0.0


# ---------------------------------------------------------------------------
# Scoring — 複製 MarketScanner._calculate_opportunity_score 的邏輯
# ---------------------------------------------------------------------------


def calculate_score(
    a_1h: Dict,
    a_4h: Optional[Dict],
    a_1d: Optional[Dict],
    volume_surge: bool = False,
) -> float:
    """
    Aligned with MarketScanner._factor_technical scoring logic.
    Base centered at 40 to match live scanner calibration.
    Threshold: 50+ to enter.
    """
    score = 40  # base — aligned with live scanner

    rsi = a_1h.get("rsi", 50)
    macd_1h = a_1h.get("macd_histogram", 0)

    # === RSI SCORING (aligned with _factor_technical) ===
    if rsi < 20:
        score += 20
    elif rsi < 30:
        score += 18
    elif rsi < 40:
        score += 10
    elif rsi < 50:
        score += 5
    elif 50 <= rsi <= 60:
        score += 3
    elif 60 <= rsi < 70:
        score += 5
    elif rsi > 80:
        score -= 15
    elif rsi > 70:
        score -= 10

    # === MACD (aligned with _factor_technical) ===
    if macd_1h > 0:
        score += 25
    elif macd_1h < 0:
        score -= 10
    if a_4h and a_4h.get("macd_histogram", 0) > 0:
        score += 5
    if a_1d and a_1d.get("macd_histogram", 0) > 0:
        score += 5

    # === VOLUME SURGE ===
    if volume_surge:
        score += 20

    # === BOLLINGER BAND ===
    current_price = a_1h.get("current_price", 0)
    bb_lower = a_1h.get("bb_lower", 0)
    a_1h.get("bb_upper", 0)
    a_1h.get("bb_position", 0.5)
    if current_price and bb_lower:
        if current_price < bb_lower:
            score += 20
        elif current_price < bb_lower * 1.01:
            score += 10

    # === VWAP ===
    vwap = a_1h.get("vwap", 0)
    if current_price and vwap and current_price > vwap:
        score += 15

    # === MA alignment ===
    ma7 = a_1h.get("ma7", 0)
    ma25 = a_1h.get("ma25", 0)
    ma99 = a_1h.get("ma99", 0)
    if ma7 > ma25 > ma99:
        score += 10

    # === Volatility ===
    vol = a_1h.get("volatility_pct", 0)
    if 2 <= vol <= 8:
        score += 3
    elif vol > 15:
        score -= 5

    return max(0, min(100, score))


def _detect_volume_surge(klines: List[Dict]) -> bool:
    """偵測最近一根 K 線成交量是否 > 1.5 倍 20 根平均"""
    if len(klines) < 21:
        return False
    recent_volumes = [k["volume"] for k in klines[-21:-1]]
    current_volume = klines[-1]["volume"]
    avg_volume = sum(recent_volumes) / len(recent_volumes)
    return avg_volume > 0 and current_volume > avg_volume * 1.5


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------


class BacktestEngine:
    """回測引擎：模擬完整交易循環"""

    # 倉位限制 (與 risk_config / TradeExecutor 一致)
    MAX_POSITIONS = 5
    MAX_SINGLE_POSITION_PCT = 15  # 單筆最多 15%
    MAX_TOTAL_EXPOSURE_PCT = 70  # 總敞口上限 70%

    # ATR SL/TP 乘數 (與 SmartOrder ATR constants 一致)
    SL_ATR_MULT = 2.0
    TP1_ATR_MULT = 2.0
    TP2_ATR_MULT = 4.0
    TP3_ATR_MULT = 6.0
    TP1_SIZE_PCT = 40
    TP2_SIZE_PCT = 40
    TP3_SIZE_PCT = 20

    # 硬限制
    MIN_SL_PCT = 3.0
    MAX_SL_PCT = 12.0
    MIN_TP_PCT = 3.0
    MAX_TP_PCT = 25.0

    # 手續費
    TAKER_FEE = 0.00075  # Binance Spot 0.075% (BNB discount, matching live FeeOptimizer)

    # 追蹤止損參數 (與 TrailingStop 一致)
    TRAILING_ACTIVATION_ATR = 1.5
    TRAILING_DISTANCE_ATR = 0.5

    # 評分閾值
    SCORE_THRESHOLD = 50

    # 最少需要的 K 線數量（用於指標計算 warm-up）
    WARMUP_BARS = 100

    def __init__(self, binance_client, initial_capital: float = 10000):
        self.client = binance_client
        self.initial_capital = initial_capital

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        symbol: str,
        interval: str = "1h",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 90,
        enable_trend_filter: bool = False,
        enable_trailing_stop: bool = False,
    ) -> Dict:
        """
        執行單一幣種回測。

        Args:
            symbol: 幣種名稱 (e.g. 'BTC' 或 'BTCUSDT')
            interval: K 線週期
            start_date: 開始日期 'YYYY-MM-DD'
            end_date: 結束日期 'YYYY-MM-DD'
            days: 回測天數（當 start_date 未指定時使用）
            enable_trend_filter: 啟用 BTC 200MA 趨勢過濾
            enable_trailing_stop: 啟用追蹤止損

        Returns:
            回測結果 dict
        """
        # 標準化 symbol
        symbol = self._normalize_symbol(symbol)

        # 計算日期範圍
        if not start_date:
            end_dt = datetime.now(timezone.utc)
            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            start_dt = end_dt - timedelta(days=days)
        else:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            end_dt = (
                datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if end_date
                else datetime.now(timezone.utc)
            )

        logger.info(
            "Backtest %s %s  %s → %s  trend=%s trailing=%s",
            symbol,
            interval,
            start_dt.strftime("%Y-%m-%d"),
            end_dt.strftime("%Y-%m-%d"),
            enable_trend_filter,
            enable_trailing_stop,
        )

        # 1. 拉取 K 線資料
        # Extend fetch start to include warmup bars so simulation can begin at start_dt
        warmup_td = timedelta(
            seconds=self.WARMUP_BARS * self._interval_to_ms(interval) / 1000
        )
        fetch_start_dt = start_dt - warmup_td
        klines = self._fetch_klines(symbol, interval, fetch_start_dt, end_dt)
        if len(klines) < self.WARMUP_BARS + 10:
            logger.error("Insufficient kline data for %s: %d bars", symbol, len(klines))
            return self._empty_result(symbol)

        # 2. (可選) 拉取 BTC 日線用於趨勢過濾
        btc_daily: Optional[List[Dict]] = None
        btc_sma_200_cache: Dict[int, float] = {}  # open_time -> sma_200
        if enable_trend_filter:
            # Need at least 200 daily candles BEFORE the backtest start date
            # to compute SMA200 from day 1 of the backtest
            btc_start = start_dt - timedelta(days=300) if start_dt else None
            btc_klines = self._fetch_klines("BTCUSDT", "1d", btc_start, end_dt)
            if len(btc_klines) >= 200:
                btc_daily = btc_klines
                # 預計算每根 K 線時間點的 BTC SMA200
                closes = [k["close"] for k in btc_daily]
                for i in range(199, len(closes)):
                    btc_sma_200_cache[btc_daily[i]["open_time"]] = Indicators.sma(
                        closes[: i + 1], 200
                    )
                logger.info(
                    "BTC daily data loaded: %d bars (SMA200 calculated)", len(btc_daily)
                )
            else:
                logger.warning(
                    "BTC daily data insufficient (%d), trend filter disabled",
                    len(btc_klines),
                )

        # 3. (可選) 拉取 4h 和 1d 用於多時間框架評分
        klines_4h: Optional[List[Dict]] = None
        klines_1d: Optional[List[Dict]] = None
        if interval == "1h":
            klines_4h = self._fetch_klines(symbol, "4h", fetch_start_dt, end_dt)
            klines_1d = self._fetch_klines(symbol, "1d", fetch_start_dt, end_dt)
            logger.info(
                "Multi-TF data: 4h=%d, 1d=%d",
                len(klines_4h or []),
                len(klines_1d or []),
            )

        # 4. 執行回測模擬
        return self._simulate(
            symbol=symbol,
            interval=interval,
            start_date=start_dt.strftime("%Y-%m-%d"),
            end_date=end_dt.strftime("%Y-%m-%d"),
            klines=klines,
            klines_4h=klines_4h,
            klines_1d=klines_1d,
            btc_daily=btc_daily,
            btc_sma_200_cache=btc_sma_200_cache,
            enable_trend_filter=enable_trend_filter and btc_daily is not None,
            enable_trailing_stop=enable_trailing_stop,
        )

    def run_multi(
        self,
        symbols: List[str],
        interval: str = "1h",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 90,
        enable_trend_filter: bool = False,
        enable_trailing_stop: bool = False,
    ) -> Dict:
        """
        多幣種回測（獨立資金，各自跑自己的回測）。
        Returns:
            {
                "individual": {symbol: result, ...},
                "summary": {aggregated stats across all symbols}
            }
        """
        results = {}
        for sym in symbols:
            try:
                result = self.run(
                    symbol=sym,
                    interval=interval,
                    start_date=start_date,
                    end_date=end_date,
                    days=days,
                    enable_trend_filter=enable_trend_filter,
                    enable_trailing_stop=enable_trailing_stop,
                )
                results[sym] = result
                time.sleep(0.5)  # rate limit courtesy
            except Exception as e:
                logger.error("Backtest failed for %s: %s", sym, e)
                results[sym] = self._empty_result(sym)

        # 聚合統計
        summary = self._aggregate_results(results)
        return {"individual": results, "summary": summary}

    def walk_forward(
        self,
        symbol: str,
        interval: str = "1h",
        total_days: int = 180,
        train_pct: float = 0.7,
        n_splits: int = 3,
        enable_trend_filter: bool = False,
        enable_trailing_stop: bool = True,
    ) -> Dict:
        """Walk-forward optimization: train on historical, test on out-of-sample.

        Splits the total period into n_splits segments.
        For each split: train on early data, test on later data.
        Returns per-split OOS (out-of-sample) results + aggregate metrics.

        This is the gold standard for avoiding overfitting in backtesting.
        """
        symbol = self._normalize_symbol(symbol)

        end_dt = datetime.now(timezone.utc)
        total_td = timedelta(days=total_days)
        split_td = total_td / n_splits
        train_td = split_td * train_pct
        split_td * (1 - train_pct)

        splits: List[Dict[str, Any]] = []
        for i in range(n_splits):
            split_start = end_dt - total_td + split_td * i
            train_start = split_start
            train_end = split_start + train_td
            test_start = train_end
            test_end = split_start + split_td

            logger.info(
                "Walk-forward split %d/%d: train %s→%s  test %s→%s",
                i + 1,
                n_splits,
                train_start.strftime("%Y-%m-%d"),
                train_end.strftime("%Y-%m-%d"),
                test_start.strftime("%Y-%m-%d"),
                test_end.strftime("%Y-%m-%d"),
            )

            # Train phase (for reference — not used for optimization here,
            # but included so future grid-search can optimize per split)
            train_result = self.run(
                symbol=symbol,
                interval=interval,
                start_date=train_start.strftime("%Y-%m-%d"),
                end_date=train_end.strftime("%Y-%m-%d"),
                enable_trend_filter=enable_trend_filter,
                enable_trailing_stop=enable_trailing_stop,
            )

            # Test phase (OOS — this is what we evaluate)
            test_result = self.run(
                symbol=symbol,
                interval=interval,
                start_date=test_start.strftime("%Y-%m-%d"),
                end_date=test_end.strftime("%Y-%m-%d"),
                enable_trend_filter=enable_trend_filter,
                enable_trailing_stop=enable_trailing_stop,
            )

            splits.append(
                {
                    "split": i + 1,
                    "train": {
                        "start": train_start.strftime("%Y-%m-%d"),
                        "end": train_end.strftime("%Y-%m-%d"),
                        "return_pct": train_result.get("total_return_pct", 0),
                        "sharpe": train_result.get("sharpe_ratio", 0),
                        "win_rate": train_result.get("win_rate", 0),
                        "trades": train_result.get("total_trades", 0),
                        "max_dd": train_result.get("max_drawdown_pct", 0),
                    },
                    "test": {
                        "start": test_start.strftime("%Y-%m-%d"),
                        "end": test_end.strftime("%Y-%m-%d"),
                        "return_pct": test_result.get("total_return_pct", 0),
                        "sharpe": test_result.get("sharpe_ratio", 0),
                        "sortino": test_result.get("sortino_ratio", 0),
                        "calmar": test_result.get("calmar_ratio", 0),
                        "win_rate": test_result.get("win_rate", 0),
                        "trades": test_result.get("total_trades", 0),
                        "max_dd": test_result.get("max_drawdown_pct", 0),
                        "profit_factor": test_result.get("profit_factor", 0),
                    },
                }
            )

        # Aggregate OOS metrics
        oos_returns = [s["test"]["return_pct"] for s in splits]
        oos_sharpes = [s["test"]["sharpe"] for s in splits]
        oos_dds = [s["test"]["max_dd"] for s in splits]
        oos_trades = [s["test"]["trades"] for s in splits]
        oos_win_rates = [s["test"]["win_rate"] for s in splits]

        import numpy as np

        avg_oos_return = np.mean(oos_returns) if oos_returns else 0
        avg_oos_sharpe = np.mean(oos_sharpes) if oos_sharpes else 0
        avg_oos_dd = np.mean(oos_dds) if oos_dds else 0
        total_oos_trades = sum(oos_trades)
        avg_win_rate = np.mean(oos_win_rates) if oos_win_rates else 0

        # Robustness check: are OOS returns consistently positive?
        positive_splits = sum(1 for r in oos_returns if r > 0)
        robustness = positive_splits / n_splits * 100 if n_splits > 0 else 0

        return {
            "symbol": symbol,
            "interval": interval,
            "total_days": total_days,
            "n_splits": n_splits,
            "train_pct": train_pct,
            "splits": splits,
            "oos_summary": {
                "avg_return_pct": round(avg_oos_return, 2),
                "avg_sharpe": round(avg_oos_sharpe, 2),
                "avg_max_drawdown_pct": round(avg_oos_dd, 2),
                "avg_win_rate": round(avg_win_rate, 1),
                "total_oos_trades": total_oos_trades,
                "robustness_pct": round(robustness, 0),
            },
        }

    # ------------------------------------------------------------------
    # K 線資料拉取
    # ------------------------------------------------------------------

    def _fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_dt: Optional[datetime],
        end_dt: Optional[datetime],
    ) -> List[Dict]:
        """
        拉取指定時間範圍的 K 線。
        Binance 限制每次最多 1500 根，超過時分批拉取並拼接。
        """
        all_klines: List[Dict] = []
        # 預估需要的 K 線數量
        interval_ms = self._interval_to_ms(interval)

        if start_dt is None or end_dt is None:
            return all_klines

        total_ms = int((end_dt - start_dt).total_seconds() * 1000)
        total_ms // interval_ms + 1

        batch_size = 1500
        # 每批重疊 10 根以防時間 gap
        overlap = 10

        current_start = int(start_dt.timestamp() * 1000)

        while True:
            try:
                end_ts = int(end_dt.timestamp() * 1000)
                batch = self.client.get_klines(
                    symbol,
                    interval,
                    limit=batch_size,
                    start_time=current_start,
                    end_time=end_ts,
                )
                if not batch:
                    break

                # 只保留 >= current_start 的資料
                batch = [k for k in batch if k["open_time"] >= current_start]
                if not batch:
                    break

                # 去重：跳過已經在最後一筆的
                if all_klines:
                    last_time = all_klines[-1]["open_time"]
                    batch = [k for k in batch if k["open_time"] > last_time]

                all_klines.extend(batch)

                # 最後一批，或已到 end_dt
                last_time = batch[-1]["open_time"]
                if last_time >= end_ts or len(batch) < batch_size - overlap:
                    break

                # 下一批從最後一根 K 線之前 overlap 根開始
                current_start = batch[-1]["open_time"] + 1

            except Exception as e:
                logger.error("Error fetching klines for %s %s: %s", symbol, interval, e)
                break

        # 截斷到 end_dt
        assert end_dt is not None
        end_ts = int(end_dt.timestamp() * 1000)
        all_klines = [k for k in all_klines if k["open_time"] <= end_ts]

        logger.info("Fetched %d klines for %s %s", len(all_klines), symbol, interval)
        return all_klines

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        """將 interval 字串轉為毫秒數"""
        mapping = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "2h": 7_200_000,
            "4h": 14_400_000,
            "6h": 21_600_000,
            "8h": 28_800_000,
            "12h": 43_200_000,
            "1d": 86_400_000,
        }
        return mapping.get(interval, 3_600_000)

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """確保 symbol 以 USDT 結尾"""
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"
        return symbol

    # ------------------------------------------------------------------
    # 核心模擬邏輯
    # ------------------------------------------------------------------

    def _simulate(
        self,
        symbol: str,
        interval: str,
        start_date: str,
        end_date: str,
        klines: List[Dict],
        klines_4h: Optional[List[Dict]],
        klines_1d: Optional[List[Dict]],
        btc_daily: Optional[List[Dict]],
        btc_sma_200_cache: Dict[int, float],
        enable_trend_filter: bool,
        enable_trailing_stop: bool,
    ) -> Dict:
        """
        逐 K 線模擬交易。

        流程：
        1. 計算指標和評分
        2. 評分 >= 50 → 用下一根 K 線 open 入場
        3. 入場後設置 ATR 動態 SL/TP
        4. 每根 K 線檢查 SL/TP 觸發
        5. (可選) BTC 趨勢過濾
        6. (可選) 追蹤止損
        """
        capital = self.initial_capital
        positions: List[Position] = []
        closed_trades: List[ClosedTrade] = []
        equity_curve: List[float] = [capital]  # 每根 K 線的總權益

        pending_entry: Optional[Dict] = None  # 評分達標，等待下一根 K 線入場

        # 準備多時間框架資料的時間索引
        tf_4h_by_time = {k["open_time"]: k for k in (klines_4h or [])}
        tf_1d_by_time = {k["open_time"]: k for k in (klines_1d or [])}

        # 找到最近的 4h / 1d 分析（用於 1h 模式）
        def _get_latest_tf_analysis(
            current_time: int,
        ) -> Tuple[Optional[Dict], Optional[Dict]]:
            a_4h = None
            a_1d = None
            if tf_4h_by_time and klines_4h:
                # 找 <= current_time 的最新 4h K 線
                times_4h = [t for t in tf_4h_by_time if t <= current_time]
                if times_4h:
                    latest_4h_idx = max(
                        i
                        for i, k in enumerate(klines_4h)
                        if k["open_time"] <= current_time
                    )
                    a_4h = Indicators.analyze_symbol(klines_4h[: latest_4h_idx + 1])
            if tf_1d_by_time and klines_1d:
                latest_1d_idx = max(
                    i for i, k in enumerate(klines_1d) if k["open_time"] <= current_time
                )
                a_1d = Indicators.analyze_symbol(klines_1d[: latest_1d_idx + 1])
            return a_4h, a_1d

        for bar_idx in range(self.WARMUP_BARS, len(klines)):
            kline = klines[bar_idx]
            current_time = kline["open_time"]
            current_high = kline["high"]
            current_low = kline["low"]

            # ---- 處理待入場 ----
            if pending_entry is not None:
                # 用當前 K 線的 open 作為入場價
                entry_price = kline["open"]
                pe = pending_entry
                pending_entry = None

                # 檢查持倉限制（取消入場，但不跳過後續 SL/TP 檢查）
                can_enter = True
                if len(positions) >= self.MAX_POSITIONS:
                    can_enter = False

                # 計算總敞口
                if can_enter:
                    total_exposure = sum(p.usdt_cost for p in positions)
                    max_exposure = capital * (self.MAX_TOTAL_EXPOSURE_PCT / 100)
                    available_exposure = max_exposure - total_exposure
                    if available_exposure <= 0:
                        can_enter = False

                if can_enter:
                    # 計算入場金額
                    single_limit = capital * (self.MAX_SINGLE_POSITION_PCT / 100)
                    usdt_amount = min(single_limit, available_exposure)
                    # 根據評分調整
                    score_factor = min(pe["score"] / 70, 1.0)
                    if pe["score"] < 55:
                        score_factor *= 0.5
                    elif pe["score"] < 65:
                        score_factor *= 0.75
                    usdt_amount *= score_factor
                    if usdt_amount >= 10:
                        # 扣除手續費的實際可用金額
                        fee = usdt_amount * self.TAKER_FEE
                        net_usdt = usdt_amount - fee
                        quantity = net_usdt / entry_price

                        # 計算 SL/TP
                        atr = pe["atr"]
                        sl_tp = self._calculate_sl_tp(entry_price, atr)

                        position = Position(
                            symbol=symbol,
                            entry_price=entry_price,
                            entry_bar=bar_idx,
                            entry_time=current_time,
                            quantity=quantity,
                            usdt_cost=usdt_amount,
                            atr=atr,
                            sl_price=sl_tp["sl_price"],
                            tp1_price=sl_tp["tp1_price"],
                            tp1_size=quantity * (self.TP1_SIZE_PCT / 100),
                            tp2_price=sl_tp["tp2_price"],
                            tp2_size=quantity * (self.TP2_SIZE_PCT / 100),
                            tp3_price=sl_tp["tp3_price"],
                            tp3_size=quantity * (self.TP3_SIZE_PCT / 100),
                            highest_price=entry_price,
                            trailing_sl=0.0,
                        )
                        positions.append(position)

            # ---- 處理持倉：檢查 SL/TP 觸發 ----
            trades_to_close: List[Tuple[int, Position, str, float]] = (
                []
            )  # (pos_idx, pos, reason, exit_price)

            for pos_idx, pos in enumerate(positions):
                exit_reason = None

                # When both SL and TP could trigger on the same candle,
                # randomize the check order to eliminate systematic pessimistic bias
                _sl_triggered = current_low <= pos.sl_price
                _tp1_triggered = not pos.tp1_hit and current_high >= pos.tp1_price

                if _sl_triggered and _tp1_triggered:
                    # Both possible — randomize to eliminate SL-first bias
                    check_sl_first = random.random() < 0.5
                else:
                    check_sl_first = (
                        True  # only one or neither triggers; order irrelevant
                    )

                if check_sl_first:
                    # 1. Stop Loss (original order)
                    if _sl_triggered:
                        exit_reason = "sl"

                    # 2. TP1 (skip if SL already triggered on this candle)
                else:
                    # Check TP first, then SL
                    if _tp1_triggered:
                        pos.tp1_hit = True
                        tp1_pnl_pct = (
                            (pos.tp1_price - pos.entry_price) / pos.entry_price
                        ) * 100
                        tp1_pnl_usdt = (
                            pos.tp1_size * pos.tp1_price
                            - pos.tp1_size * pos.entry_price
                        )
                        tp1_fee = pos.tp1_size * pos.tp1_price * self.TAKER_FEE
                        tp1_pnl_usdt -= tp1_fee
                        closed_trades.append(
                            ClosedTrade(
                                symbol=pos.symbol,
                                entry_price=pos.entry_price,
                                exit_price=pos.tp1_price,
                                pnl_pct=tp1_pnl_pct,
                                pnl_usdt=tp1_pnl_usdt,
                                reason="tp1",
                                holding_bars=bar_idx - pos.entry_bar,
                                entry_time=pos.entry_time,
                                exit_time=current_time,
                                score=0,
                            )
                        )
                        pos.quantity -= pos.tp1_size
                        pos.usdt_cost *= 1 - self.TP1_SIZE_PCT / 100

                    if _sl_triggered and exit_reason is None:
                        exit_reason = "sl"

                # 2. TP1 (skip if SL already triggered, and not already handled in TP-first branch)
                if (
                    check_sl_first
                    and exit_reason is None
                    and not pos.tp1_hit
                    and current_high >= pos.tp1_price
                ):
                    pos.tp1_hit = True
                    # 模擬 TP1 平倉 40%
                    tp1_pnl_pct = (
                        (pos.tp1_price - pos.entry_price) / pos.entry_price
                    ) * 100
                    tp1_pnl_usdt = (
                        pos.tp1_size * pos.tp1_price - pos.tp1_size * pos.entry_price
                    )
                    tp1_fee = pos.tp1_size * pos.tp1_price * self.TAKER_FEE
                    tp1_pnl_usdt -= tp1_fee
                    closed_trades.append(
                        ClosedTrade(
                            symbol=pos.symbol,
                            entry_price=pos.entry_price,
                            exit_price=pos.tp1_price,
                            pnl_pct=tp1_pnl_pct,
                            pnl_usdt=tp1_pnl_usdt,
                            reason="tp1",
                            holding_bars=bar_idx - pos.entry_bar,
                            entry_time=pos.entry_time,
                            exit_time=current_time,
                            score=0,
                        )
                    )
                    # 更新剩余持倉
                    pos.quantity -= pos.tp1_size
                    pos.usdt_cost *= 1 - self.TP1_SIZE_PCT / 100

                # 3. TP2 (skip if SL already triggered on this candle)
                if (
                    exit_reason is None
                    and pos.tp1_hit
                    and not pos.tp2_hit
                    and current_high >= pos.tp2_price
                ):
                    pos.tp2_hit = True
                    tp2_pnl_pct = (
                        (pos.tp2_price - pos.entry_price) / pos.entry_price
                    ) * 100
                    tp2_pnl_usdt = (
                        pos.tp2_size * pos.tp2_price - pos.tp2_size * pos.entry_price
                    )
                    tp2_fee = pos.tp2_size * pos.tp2_price * self.TAKER_FEE
                    tp2_pnl_usdt -= tp2_fee
                    closed_trades.append(
                        ClosedTrade(
                            symbol=pos.symbol,
                            entry_price=pos.entry_price,
                            exit_price=pos.tp2_price,
                            pnl_pct=tp2_pnl_pct,
                            pnl_usdt=tp2_pnl_usdt,
                            reason="tp2",
                            holding_bars=bar_idx - pos.entry_bar,
                            entry_time=pos.entry_time,
                            exit_time=current_time,
                            score=0,
                        )
                    )
                    pos.quantity -= pos.tp2_size
                    pos.usdt_cost *= 1 - self.TP2_SIZE_PCT / 100

                # 4. TP3 (skip if SL already triggered on this candle)
                if (
                    exit_reason is None
                    and pos.tp2_hit
                    and not pos.tp3_hit
                    and current_high >= pos.tp3_price
                ):
                    pos.tp3_hit = True
                    tp3_pnl_pct = (
                        (pos.tp3_price - pos.entry_price) / pos.entry_price
                    ) * 100
                    tp3_pnl_usdt = (
                        pos.tp3_size * pos.tp3_price - pos.tp3_size * pos.entry_price
                    )
                    tp3_fee = pos.tp3_size * pos.tp3_price * self.TAKER_FEE
                    tp3_pnl_usdt -= tp3_fee
                    closed_trades.append(
                        ClosedTrade(
                            symbol=pos.symbol,
                            entry_price=pos.entry_price,
                            exit_price=pos.tp3_price,
                            pnl_pct=tp3_pnl_pct,
                            pnl_usdt=tp3_pnl_usdt,
                            reason="tp3",
                            holding_bars=bar_idx - pos.entry_bar,
                            entry_time=pos.entry_time,
                            exit_time=current_time,
                            score=0,
                        )
                    )
                    pos.quantity -= pos.tp3_size
                    pos.usdt_cost = 0
                    exit_reason = "tp3_full"

                # 5. SL on remaining position (after partial TP)
                if exit_reason == "sl" and pos.quantity > 0:
                    remaining_qty = pos.quantity
                    sl_pnl_pct = (
                        (pos.sl_price - pos.entry_price) / pos.entry_price
                    ) * 100
                    sl_pnl_usdt = (
                        remaining_qty * pos.sl_price - remaining_qty * pos.entry_price
                    )
                    sl_fee = remaining_qty * pos.sl_price * self.TAKER_FEE
                    sl_pnl_usdt -= sl_fee
                    closed_trades.append(
                        ClosedTrade(
                            symbol=pos.symbol,
                            entry_price=pos.entry_price,
                            exit_price=pos.sl_price,
                            pnl_pct=sl_pnl_pct,
                            pnl_usdt=sl_pnl_usdt,
                            reason="sl",
                            holding_bars=bar_idx - pos.entry_bar,
                            entry_time=pos.entry_time,
                            exit_time=current_time,
                            score=0,
                        )
                    )
                    pos.quantity = 0

                # 標記需要移除的倉位
                if pos.quantity <= 1e-10:
                    trades_to_close.append((pos_idx, pos, "closed", 0))

                # ---- 追蹤止損 (skip if SL already triggered on this candle) ----
                if (
                    exit_reason is None
                    and enable_trailing_stop
                    and pos.quantity > 0
                    and not pos.tp1_hit
                ):
                    if current_high > pos.highest_price:
                        pos.highest_price = current_high

                    profit = pos.highest_price - pos.entry_price
                    if profit >= self.TRAILING_ACTIVATION_ATR * pos.atr:
                        if not pos.trailing_activated:
                            pos.trailing_activated = True
                            pos.trailing_sl = (
                                pos.highest_price - self.TRAILING_DISTANCE_ATR * pos.atr
                            )
                        else:
                            new_sl = (
                                pos.highest_price - self.TRAILING_DISTANCE_ATR * pos.atr
                            )
                            if new_sl > pos.trailing_sl:
                                pos.trailing_sl = new_sl

                        # 只在追蹤 SL 優於固定 SL 時使用
                        if pos.trailing_sl > pos.sl_price:
                            # 檢查觸發
                            if current_low <= pos.trailing_sl:
                                trail_pnl_pct = (
                                    (pos.trailing_sl - pos.entry_price)
                                    / pos.entry_price
                                ) * 100
                                trail_pnl_usdt = (
                                    pos.quantity * pos.trailing_sl
                                    - pos.quantity * pos.entry_price
                                )
                                trail_fee = (
                                    pos.quantity * pos.trailing_sl * self.TAKER_FEE
                                )
                                trail_pnl_usdt -= trail_fee
                                closed_trades.append(
                                    ClosedTrade(
                                        symbol=pos.symbol,
                                        entry_price=pos.entry_price,
                                        exit_price=pos.trailing_sl,
                                        pnl_pct=trail_pnl_pct,
                                        pnl_usdt=trail_pnl_usdt,
                                        reason="trailing",
                                        holding_bars=bar_idx - pos.entry_bar,
                                        entry_time=pos.entry_time,
                                        exit_time=current_time,
                                        score=0,
                                    )
                                )
                                pos.quantity = 0
                                trades_to_close.append((pos_idx, pos, "trailing", 0))

            # 移除已平倉的倉位（逆序）
            for pos_idx, pos, reason, price in reversed(trades_to_close):
                if pos_idx < len(positions) and positions[pos_idx].quantity <= 1e-10:
                    positions.pop(pos_idx)

            # ---- 計算指標和評分 ----
            if bar_idx >= self.WARMUP_BARS and not pending_entry:
                slice_klines = klines[: bar_idx + 1]
                analysis_1h = Indicators.analyze_symbol(slice_klines)
                if analysis_1h:
                    a_4h, a_1d = (
                        _get_latest_tf_analysis(current_time)
                        if interval == "1h"
                        else (None, None)
                    )
                    vol_surge = _detect_volume_surge(slice_klines)
                    score = calculate_score(analysis_1h, a_4h, a_1d, vol_surge)

                    # ---- BTC 趨勢過濾 ----
                    if enable_trend_filter and btc_sma_200_cache:
                        # 找 <= current_time 的最新 BTC SMA200
                        trend_times = [
                            t for t in btc_sma_200_cache if t <= current_time
                        ]
                        if trend_times:
                            latest_btc_time = max(trend_times)
                            sma_200 = btc_sma_200_cache[latest_btc_time]
                            # 找對應的 BTC close
                            btc_close = 0
                            for bk in btc_daily or []:
                                if bk["open_time"] == latest_btc_time:
                                    btc_close = bk["close"]
                                    break
                            # 如果找不到精確匹配，用最近的
                            if btc_close == 0:
                                for bk in reversed(btc_daily or []):
                                    if bk["open_time"] <= current_time:
                                        btc_close = bk["close"]
                                        break
                            if btc_close > 0 and sma_200 > 0 and btc_close < sma_200:
                                # BTC 在 200SMA 下方，禁止開倉
                                score = 0

                    if score >= self.SCORE_THRESHOLD:
                        atr = analysis_1h.get("atr", 0)
                        if atr > 0:
                            pending_entry = {
                                "score": score,
                                "atr": atr,
                            }

            # ---- 計算權益曲線 ----
            open_positions_value = sum(
                pos.quantity * kline["close"] for pos in positions if pos.quantity > 0
            )
            closed_pnl = sum(t.pnl_usdt for t in closed_trades)
            equity = (
                capital
                - sum(p.usdt_cost for p in positions)
                + open_positions_value
                + closed_pnl
            )
            equity_curve.append(equity)

        # ---- 強制平倉所有剩余持倉 ----
        if klines:
            last_close = klines[-1]["close"]
            for pos in positions:
                if pos.quantity > 0:
                    pnl_pct = ((last_close - pos.entry_price) / pos.entry_price) * 100
                    pnl_usdt = (
                        pos.quantity * last_close - pos.quantity * pos.entry_price
                    )
                    fee = pos.quantity * last_close * self.TAKER_FEE
                    pnl_usdt -= fee
                    closed_trades.append(
                        ClosedTrade(
                            symbol=pos.symbol,
                            entry_price=pos.entry_price,
                            exit_price=last_close,
                            pnl_pct=pnl_pct,
                            pnl_usdt=pnl_usdt,
                            reason="end_of_data",
                            holding_bars=len(klines) - 1 - pos.entry_bar,
                            entry_time=pos.entry_time,
                            exit_time=klines[-1]["open_time"],
                            score=0,
                        )
                    )

        # ---- 計算統計 ----
        return self._compute_stats(
            symbol=symbol,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
            closed_trades=closed_trades,
            equity_curve=equity_curve,
            initial_capital=self.initial_capital,
            enable_trend_filter=enable_trend_filter,
            enable_trailing_stop=enable_trailing_stop,
        )

    # ------------------------------------------------------------------
    # SL/TP 計算 (與 SmartOrder.calculate_sl_tp 一致)
    # ------------------------------------------------------------------

    def _calculate_sl_tp(self, price: float, atr: float) -> Dict[str, float]:
        atr_pct = (atr / price) * 100

        sl_pct = self.SL_ATR_MULT * atr_pct
        sl_pct = max(self.MIN_SL_PCT, min(self.MAX_SL_PCT, sl_pct))
        sl_price = price * (1 - sl_pct / 100)

        tp1_pct = max(
            self.MIN_TP_PCT, min(self.MAX_TP_PCT, self.TP1_ATR_MULT * atr_pct)
        )
        tp2_pct = max(
            self.MIN_TP_PCT, min(self.MAX_TP_PCT, self.TP2_ATR_MULT * atr_pct)
        )
        tp3_pct = max(
            self.MIN_TP_PCT, min(self.MAX_TP_PCT, self.TP3_ATR_MULT * atr_pct)
        )

        return {
            "sl_price": round(sl_price, 6),
            "sl_pct": round(sl_pct, 2),
            "tp1_price": round(price * (1 + tp1_pct / 100), 6),
            "tp1_pct": round(tp1_pct, 2),
            "tp2_price": round(price * (1 + tp2_pct / 100), 6),
            "tp2_pct": round(tp2_pct, 2),
            "tp3_price": round(price * (1 + tp3_pct / 100), 6),
            "tp3_pct": round(tp3_pct, 2),
        }

    # ------------------------------------------------------------------
    # 統計計算
    # ------------------------------------------------------------------

    def _compute_stats(
        self,
        symbol: str,
        interval: str,
        start_date: str,
        end_date: str,
        closed_trades: List[ClosedTrade],
        equity_curve: List[float],
        initial_capital: float,
        enable_trend_filter: bool,
        enable_trailing_stop: bool,
    ) -> Dict:
        total_pnl_usdt = sum(t.pnl_usdt for t in closed_trades)
        final_equity = initial_capital + total_pnl_usdt
        total_return_pct = ((final_equity - initial_capital) / initial_capital) * 100

        # 年化收益率
        trading_days = max(len(equity_curve) / 24, 1)  # 假設 1h K 線
        annualized_return_pct = (
            ((final_equity / initial_capital) ** (365 / trading_days) - 1) * 100
            if trading_days > 0
            else 0
        )

        # Max drawdown
        max_drawdown_pct = self._calc_max_drawdown(equity_curve)

        # Sharpe / Sortino / Calmar
        returns = self._calc_bar_returns(equity_curve)
        sharpe_ratio = self._calc_sharpe(returns)
        sortino_ratio = self._calc_sortino(returns)
        calmar_ratio = (
            annualized_return_pct / max_drawdown_pct if max_drawdown_pct > 0 else 0
        )

        # Win/Loss stats
        wins = [t for t in closed_trades if t.pnl_usdt > 0]
        losses = [t for t in closed_trades if t.pnl_usdt <= 0]
        total_trades = len(closed_trades)
        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0

        avg_win_pct = (sum(t.pnl_pct for t in wins) / len(wins)) if wins else 0
        avg_loss_pct = (sum(t.pnl_pct for t in losses) / len(losses)) if losses else 0

        gross_profit = sum(t.pnl_usdt for t in wins)
        gross_loss = abs(sum(t.pnl_usdt for t in losses))
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else float("inf") if gross_profit > 0 else 0
        )

        # Max consecutive losses
        max_consecutive_losses = self._calc_max_consecutive(closed_trades)

        # Avg holding bars
        avg_holding_bars = (
            (sum(t.holding_bars for t in closed_trades) / total_trades)
            if total_trades > 0
            else 0
        )

        # Trade list
        trade_list = [
            {
                "symbol": t.symbol,
                "entry_price": round(t.entry_price, 6),
                "exit_price": round(t.exit_price, 6),
                "pnl_pct": round(t.pnl_pct, 2),
                "pnl_usdt": round(t.pnl_usdt, 4),
                "reason": t.reason,
                "holding_bars": t.holding_bars,
                "entry_time": (
                    datetime.fromtimestamp(
                        t.entry_time / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M")
                    if t.entry_time
                    else ""
                ),
                "exit_time": (
                    datetime.fromtimestamp(
                        t.exit_time / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d %H:%M")
                    if t.exit_time
                    else ""
                ),
            }
            for t in closed_trades
        ]

        return {
            "symbol": symbol,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": initial_capital,
            "final_equity": round(final_equity, 2),
            "total_pnl_usdt": round(total_pnl_usdt, 2),
            "total_return_pct": round(total_return_pct, 2),
            "annualized_return_pct": round(annualized_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "sortino_ratio": round(sortino_ratio, 2),
            "calmar_ratio": round(calmar_ratio, 2),
            "win_rate": round(win_rate, 1),
            "avg_win_pct": round(avg_win_pct, 2),
            "avg_loss_pct": round(avg_loss_pct, 2),
            "profit_factor": (
                round(profit_factor, 2) if profit_factor != float("inf") else "∞"
            ),
            "max_consecutive_losses": max_consecutive_losses,
            "total_trades": total_trades,
            "avg_holding_bars": round(avg_holding_bars, 1),
            "wins": len(wins),
            "losses": len(losses),
            "enable_trend_filter": enable_trend_filter,
            "enable_trailing_stop": enable_trailing_stop,
            "trades": trade_list,
        }

    # ------------------------------------------------------------------
    # 統計輔助函數
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_max_drawdown(equity_curve: List[float]) -> float:
        """最大回撤百分比"""
        if len(equity_curve) < 2:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _calc_bar_returns(equity_curve: List[float]) -> List[float]:
        """每根 K 線的收益率百分比"""
        if len(equity_curve) < 2:
            return []
        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            if prev > 0:
                returns.append((equity_curve[i] - prev) / prev * 100)
        return returns

    @staticmethod
    def _calc_sharpe(returns: List[float], risk_free_rate: float = 5.0) -> float:
        """夏普比率（年化）假設無風險利率 5%，每小時收益率"""
        if len(returns) < 2:
            return 0.0
        import numpy as np

        avg_ret = float(np.mean(returns))
        std_ret = float(np.std(returns))
        if std_ret == 0:
            return 0.0
        # 年化：每小時 → 每年 (8760 hours)
        hourly_rf = risk_free_rate / 100 / 8760 * 100  # 每小時無風險收益 %
        return ((avg_ret - hourly_rf) / std_ret) * math.sqrt(8760)

    @staticmethod
    def _calc_sortino(returns: List[float], risk_free_rate: float = 5.0) -> float:
        """索提諾比率（年化）只用下行波動"""
        if len(returns) < 2:
            return 0.0
        import numpy as np

        avg_ret = float(np.mean(returns))
        # 下行偏差
        downside = [r for r in returns if r < 0]
        if not downside:
            return float("inf")
        downside_std = float(np.std(downside))
        if downside_std == 0:
            return 0.0
        hourly_rf = risk_free_rate / 100 / 8760 * 100
        return ((avg_ret - hourly_rf) / downside_std) * math.sqrt(8760)

    @staticmethod
    def _calc_max_consecutive(trades: List[ClosedTrade]) -> int:
        """最大連續虧損次數"""
        max_streak = 0
        current_streak = 0
        for t in trades:
            if t.pnl_usdt <= 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        return max_streak

    @staticmethod
    def _empty_result(symbol: str) -> Dict:
        return {
            "symbol": symbol,
            "error": "insufficient_data",
            "total_trades": 0,
            "total_return_pct": 0,
            "annualized_return_pct": 0,
            "max_drawdown_pct": 0,
            "sharpe_ratio": 0,
            "sortino_ratio": 0,
            "calmar_ratio": 0,
            "win_rate": 0,
            "avg_win_pct": 0,
            "avg_loss_pct": 0,
            "profit_factor": 0,
            "max_consecutive_losses": 0,
            "avg_holding_bars": 0,
            "trades": [],
        }

    # ------------------------------------------------------------------
    # 多幣種聚合
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_results(results: Dict[str, Dict]) -> Dict:
        """聚合多幣種回測結果"""
        all_trades: List[Dict[str, Any]] = []
        total_initial = 0.0
        total_final = 0.0

        for sym, r in results.items():
            if "error" in r:
                continue
            all_trades.extend(r.get("trades", []))
            total_initial += r.get("initial_capital", 10000)
            total_final += r.get("final_equity", r.get("initial_capital", 10000))

        total_return_pct = (
            ((total_final - total_initial) / total_initial * 100)
            if total_initial > 0
            else 0
        )
        total_trades = len(all_trades)

        wins = sum(1 for t in all_trades if t.get("pnl_pct", 0) > 0)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        profit_factor = 0.0
        if all_trades:
            gp = sum(t["pnl_usdt"] for t in all_trades if t["pnl_usdt"] > 0)
            gl = abs(sum(t["pnl_usdt"] for t in all_trades if t["pnl_usdt"] <= 0))
            profit_factor = gp / gl if gl > 0 else float("inf")

        return {
            "symbols_tested": len(results),
            "symbols_with_trades": sum(
                1 for r in results.values() if r.get("total_trades", 0) > 0
            ),
            "total_trades": total_trades,
            "total_initial_capital": total_initial,
            "total_final_equity": round(total_final, 2),
            "total_pnl_usdt": round(total_final - total_initial, 2),
            "total_return_pct": round(total_return_pct, 2),
            "win_rate": round(win_rate, 1),
            "profit_factor": (
                round(profit_factor, 2) if profit_factor != float("inf") else "∞"
            ),
            "per_symbol": {
                sym: {
                    "total_return_pct": r.get("total_return_pct", 0),
                    "total_trades": r.get("total_trades", 0),
                    "win_rate": r.get("win_rate", 0),
                    "max_drawdown_pct": r.get("max_drawdown_pct", 0),
                }
                for sym, r in results.items()
            },
        }

    # ------------------------------------------------------------------
    # 報告生成
    # ------------------------------------------------------------------

    @staticmethod
    def generate_report(result: Dict) -> str:
        """生成格式化的文字報告"""
        lines = []
        w = 60  # line width

        def hr(char="═"):
            return char * w

        lines.append(hr())
        lines.append("  BACKTEST REPORT".center(w))
        lines.append(hr())
        lines.append("")

        if "error" in result:
            lines.append(f"  ⚠️  Error: {result['error']}")
            lines.append(f"  Symbol: {result.get('symbol', 'N/A')}")
            lines.append("")
            return "\n".join(lines)

        lines.append(f"  Symbol:          {result.get('symbol', 'N/A')}")
        lines.append(f"  Interval:        {result.get('interval', 'N/A')}")
        lines.append(
            f"  Period:          {result.get('start_date', '')} → {result.get('end_date', '')}"
        )
        lines.append(f"  Initial Capital: ${result.get('initial_capital', 0):,.2f}")
        lines.append(
            f"  Trend Filter:    {'ON' if result.get('enable_trend_filter') else 'OFF'}"
        )
        lines.append(
            f"  Trailing Stop:   {'ON' if result.get('enable_trailing_stop') else 'OFF'}"
        )
        lines.append("")
        lines.append(hr("─"))
        lines.append("  PERFORMANCE SUMMARY".center(w))
        lines.append(hr("─"))
        lines.append("")
        lines.append(
            f"  Final Equity:         ${result.get('final_equity', 0):>12,.2f}"
        )
        lines.append(
            f"  Total PnL:            ${result.get('total_pnl_usdt', 0):>12,.2f}"
        )
        lines.append(
            f"  Total Return:         {result.get('total_return_pct', 0):>11.2f}%"
        )
        lines.append(
            f"  Annualized Return:    {result.get('annualized_return_pct', 0):>11.2f}%"
        )
        lines.append(
            f"  Max Drawdown:         {result.get('max_drawdown_pct', 0):>11.2f}%"
        )
        lines.append(f"  Sharpe Ratio:         {result.get('sharpe_ratio', 0):>12.2f}")
        lines.append(f"  Sortino Ratio:        {result.get('sortino_ratio', 0):>12.2f}")
        calmar = result.get("calmar_ratio", 0)
        lines.append(
            f"  Calmar Ratio:         {calmar:>12.2f}"
            if isinstance(calmar, (int, float))
            else f"  Calmar Ratio:         {calmar:>12}"
        )
        lines.append("")
        lines.append(hr("─"))
        lines.append("  TRADE STATISTICS".center(w))
        lines.append(hr("─"))
        lines.append("")
        lines.append(f"  Total Trades:         {result.get('total_trades', 0):>12}")
        lines.append(
            f"  Wins / Losses:        {result.get('wins', 0):>5} / {result.get('losses', 0):<5}"
        )
        lines.append(f"  Win Rate:             {result.get('win_rate', 0):>11.1f}%")
        lines.append(f"  Avg Win:              {result.get('avg_win_pct', 0):>11.2f}%")
        lines.append(f"  Avg Loss:             {result.get('avg_loss_pct', 0):>11.2f}%")
        lines.append(f"  Profit Factor:        {result.get('profit_factor', 0):>12}")
        lines.append(
            f"  Max Consecutive Loss: {result.get('max_consecutive_losses', 0):>12}"
        )
        lines.append(
            f"  Avg Holding Bars:     {result.get('avg_holding_bars', 0):>12.1f}"
        )
        lines.append("")

        # Trade list
        trades = result.get("trades", [])
        if trades:
            lines.append(hr("─"))
            lines.append("  TRADE LOG".center(w))
            lines.append(hr("─"))
            lines.append("")
            lines.append(
                f"  {'#':>3}  {'Entry Date':<17}  {'Symbol':<10}  {'Entry':>10}  "
                f"{'Exit':>10}  {'PnL%':>7}  {'PnL$':>10}  {'Reason':<10}  {'Bars':>4}"
            )
            lines.append("  " + "─" * (w - 2))

            for i, t in enumerate(trades, 1):
                pnl_sign = "+" if t["pnl_pct"] > 0 else ""
                lines.append(
                    f"  {i:>3}  {t.get('entry_time', ''):<17}  {t['symbol']:<10}  "
                    f"{t['entry_price']:>10.4f}  {t['exit_price']:>10.4f}  "
                    f"{pnl_sign}{t['pnl_pct']:>6.2f}%  {t['pnl_usdt']:>+9.2f}  "
                    f"{t['reason']:<10}  {t['holding_bars']:>4}"
                )

            lines.append("")

        # Per-symbol summary (if multi-symbol)
        if "per_symbol" in result:
            lines.append(hr("─"))
            lines.append("  PER-SYMBOL SUMMARY".center(w))
            lines.append(hr("─"))
            lines.append("")
            lines.append(
                f"  {'Symbol':<12}  {'Return':>8}  {'Trades':>6}  {'WinRate':>7}  {'MaxDD':>7}"
            )
            lines.append("  " + "─" * 46)
            for sym, s in result.get("per_symbol", {}).items():
                lines.append(
                    f"  {sym:<12}  {s['total_return_pct']:>+7.2f}%  {s['total_trades']:>6}  "
                    f"{s['win_rate']:>6.1f}%  {s['max_drawdown_pct']:>6.2f}%"
                )
            lines.append("")

        lines.append(hr())
        return "\n".join(lines)
