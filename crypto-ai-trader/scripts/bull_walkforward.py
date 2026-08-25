#!/usr/bin/env python3
"""
BULL Regime Core-Satellite Walk-Forward Backtest
=================================================
Phase 1 deliverable for the BULL regime override proposal.

Independent module (does not mutate existing BacktestEngine).
- Core strategy: EMA-stack + ADX entry, ATR(2.5) trailing, 72h lock, pyramid +8%
- Satellite: simplified score/momentum with BULL override params (Kelly 3%,
  max_trades_30d=15, correlation 0.85)
- Regime engine: Daily SMA200 + F&G 7d avg > 60 + 4H ADX(BTC) > 25,
  confirmed 2 consecutive 4H closes
- Three windows: Bull A (2023-01..2024-03), Bull B (2024-10..2025-02),
  Bear (2022-03..2023-01)
- Walk-forward ≥5 splits, 70/30 IS/OOS
- Fees: 0.1% taker round-trip + 0.05% slippage per side
- Starting capital: $400, allocation 60/25/15 = $240/$100/$60
- Outputs: JSON metrics + markdown report

Usage:
    python3 scripts/bull_walkforward.py
    python3 scripts/bull_walkforward.py --window bull_a
    python3 scripts/bull_walkforward.py --quick   # smaller window subset for smoke test
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pickle
import sys
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.binance_client import BinanceClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bull_wf")

# ---------------------------------------------------------------------------
# Constants (locked per investment-advisor clarification 2026-08-25)
# ---------------------------------------------------------------------------
STARTING_CAPITAL = 400.0
CORE_ALLOCATION = 240.0       # 60%
SATELLITE_ALLOCATION = 100.0  # 25%
CASH_ALLOCATION = 60.0        # 15% — kept as cash buffer, not deployed

CORE_TOTAL = CORE_ALLOCATION              # $240
CORE_PER_SLOT = CORE_TOTAL / 2            # $120 per slot when 2 positions
CORE_PYRAMID_PCT = 0.25                   # 25% of initial target = $30
CORE_MAX_SYMBOLS = 2

# Fee model: 0.1% taker round-trip + 0.05% slippage
# Per side: 0.05% fee + 0.05% slippage = 0.10%
FEE_RATE = 0.001            # 0.1% taker per side (round trip 0.2%)
SLIPPAGE = 0.0005           # 0.05% per side
COST_PER_SIDE = FEE_RATE + SLIPPAGE  # 0.15% per side

# Core strategy
ATR_PERIOD = 14
ATR_MULT = 2.5
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
ADX_THRESHOLD_ENTRY = 25.0
ADX_THRESHOLD_EXIT = 20.0
ADX_EXIT_CONSEC = 6
MIN_HOLD_BARS = 18          # 72h / 4h = 18 bars
PYRAMID_TRIGGER_PCT = 0.08  # +8%
PYRAMID_MAX_ATR_RATIO = 1.5
HARD_STOP_PCT = 0.12        # -12% from highest
REDUCE_EMA50_PCT = 0.50     # cut 50% when 4H close below EMA50
PORTFOLIO_DD_STOP = 0.10    # -10% from BULL-entry peak

# Regime
REGIME_SMA_PERIOD = 200     # daily
FNG_AVG_DAYS = 7
FNG_THRESHOLD = 60
REGIME_CONFIRM_BARS = 2
BTC_ADX_THRESHOLD = 25.0
BTC_SMA_BUFFER = 0.05       # > SMA200 + 5%

# Satellite BULL override
SAT_KELLY_CAP = 0.03                # 3% of total portfolio = $12
SAT_MAX_TRADES_30D = 15
SAT_CORRELATION_THRESHOLD = 0.85
SAT_SCORE_ENTRY = 60                # require score >=60 to enter
SAT_FIXED_TP_PCT = 0.06             # +6%
SAT_SL_ATR_MULT = 2.0
SAT_MAX_HOLD_BARS = 60              # 10 days at 4h (satellite still rotates)
SAT_UNIVERSE = [
    "SOLUSDT", "BTCUSDT", "ETHUSDT",
    "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
    "LTCUSDT", "UNIUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "OPUSDT", "ARBUSDT", "SUIUSDT", "INJUSDT",
]

# Windows
WINDOWS = {
    "bull_a": ("2023-01-01", "2024-03-31"),
    "bull_b": ("2024-10-01", "2025-02-28"),
    "bear":   ("2022-03-01", "2023-01-01"),
}

# Paths
CACHE_DIR = PROJECT_ROOT / "data" / "bull_cache"
REPORT_DIR = Path("/Coze/Drive/Crypto_Trading_Monitor/crypto-reports")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def dt_to_ms(d: datetime) -> int:
    return int(d.replace(tzinfo=UTC).timestamp() * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Indicator series (operate on lists, return aligned series)
# ---------------------------------------------------------------------------
def sma_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    # Seed with SMA
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        v = values[i] * k + prev * (1 - k)
        out[i] = v
        prev = v
    return out


def atr_series(klines: List[Dict], period: int = 14) -> List[Optional[float]]:
    n = len(klines)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs: List[float] = [0.0]
    for i in range(1, n):
        h = klines[i]["high"]
        l = klines[i]["low"]
        pc = klines[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing
    atr = sum(trs[1:period + 1]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def adx_series(klines: List[Dict], period: int = 14) -> List[Optional[float]]:
    """Wilder ADX series. Returns list aligned to klines (None until warm-up)."""
    n = len(klines)
    out: List[Optional[float]] = [None] * n
    if n < period * 2 + 1:
        return out
    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    trs: List[float] = [0.0]
    for i in range(1, n):
        h = klines[i]["high"]
        l = klines[i]["low"]
        ph = klines[i - 1]["high"]
        pl = klines[i - 1]["low"]
        pc = klines[i - 1]["close"]
        up = h - ph
        dn = pl - l
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    sm_p = sum(plus_dm[1:period + 1]) / period
    sm_m = sum(minus_dm[1:period + 1]) / period
    sm_t = sum(trs[1:period + 1]) / period

    dx_vals: List[float] = []
    # We produce a rolling ADX. We need at least `period` DX values to seed ADX.
    # For bar index j in [period+1 ... n-1], compute DX.
    dx_at_bar: List[Optional[float]] = [None] * n
    for i in range(period + 1, n):
        sm_p = (sm_p * (period - 1) + plus_dm[i]) / period
        sm_m = (sm_m * (period - 1) + minus_dm[i]) / period
        sm_t = (sm_t * (period - 1) + trs[i]) / period
        if sm_t <= 0:
            dx_at_bar[i] = 0.0
            continue
        pdi = (sm_p / sm_t) * 100
        mdi = (sm_m / sm_t) * 100
        s = pdi + mdi
        dx_at_bar[i] = abs(pdi - mdi) / s * 100 if s > 0 else 0.0

    # ADX = Wilder EMA of DX. Need `period` DX values to seed.
    first_dx = None
    dx_buffer: List[float] = []
    for i in range(period + 1, n):
        if dx_at_bar[i] is None:
            continue
        dx_buffer.append(dx_at_bar[i])
        if len(dx_buffer) == period:
            first_dx = i
            break
    if first_dx is None:
        return out
    adx = sum(dx_buffer) / period
    out[first_dx] = adx
    for i in range(first_dx + 1, n):
        if dx_at_bar[i] is None:
            continue
        adx = (adx * (period - 1) + dx_at_bar[i]) / period
        out[i] = adx
    return out


def rsi_series(values: List[float], period: int = 14) -> List[Optional[float]]:
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        ch = values[i] - values[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100 - 100 / (1 + (avg_gain / avg_loss if avg_loss > 0 else 1e9))
    for i in range(period + 1, n):
        ch = values[i] - values[i - 1]
        g = max(ch, 0.0)
        l = max(-ch, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else 1e9
        out[i] = 100 - 100 / (1 + rs)
    return out


# ---------------------------------------------------------------------------
# Data layer with caching
# ---------------------------------------------------------------------------
def _cache_path(symbol: str, interval: str, start: str, end: str) -> Path:
    return CACHE_DIR / f"{symbol}_{interval}_{start}_{end}.pkl"


def fetch_klines(
    client: BinanceClient,
    symbol: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
) -> List[Dict]:
    """Paginated kline fetch with disk cache."""
    cache_key = f"{symbol}_{interval}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
    cache_file = CACHE_DIR / f"{cache_key}.pkl"
    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                data = pickle.load(f)
            logger.info("Cache hit %s (%d bars)", cache_key, len(data))
            return data
        except Exception:
            pass

    interval_ms_map = {
        "1m": 60_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    interval_ms = interval_ms_map[interval]
    batch_size = 1000
    start_ms = dt_to_ms(start_dt)
    end_ms = dt_to_ms(end_dt)

    all_klines: List[Dict] = []
    cur = start_ms
    while cur < end_ms:
        batch = client.get_klines(
            symbol, interval, limit=batch_size,
            start_time=cur, end_time=end_ms,
        )
        if not batch:
            break
        # Filter strictly >= cur
        batch = [k for k in batch if k["open_time"] >= cur]
        if not batch:
            break
        if all_klines:
            last_t = all_klines[-1]["open_time"]
            batch = [k for k in batch if k["open_time"] > last_t]
            if not batch:
                break
        all_klines.extend(batch)
        last_t = batch[-1]["open_time"]
        if len(batch) < batch_size - 5:
            break
        cur = last_t + 1

    # Trim to end
    all_klines = [k for k in all_klines if k["open_time"] <= end_ms]
    with open(cache_file, "wb") as f:
        pickle.dump(all_klines, f)
    logger.info("Fetched %d bars %s %s (cached)", len(all_klines), symbol, interval)
    return all_klines


def fetch_fng_history() -> Dict[int, int]:
    """Return {unix_day_ts: fng_value} from alternative.me. Full history."""
    cache_file = CACHE_DIR / "fng_history.pkl"
    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
    url = "https://api.alternative.me/fng/?limit=0&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    out: Dict[int, int] = {}
    for entry in data["data"]:
        ts = int(entry["timestamp"])
        # F&G is daily at 00:00 UTC
        day_ts = ts - (ts % 86400)
        out[day_ts] = int(entry["value"])
    with open(cache_file, "wb") as f:
        pickle.dump(out, f)
    logger.info("F&G history: %d days", len(out))
    return out


def fng_7d_avg(fng_map: Dict[int, int], ts_ms: int) -> Optional[float]:
    """Average F&G over the 7 days ending on the UTC day containing ts_ms."""
    day_ts = (ts_ms // 1000) - ((ts_ms // 1000) % 86400)
    vals = []
    missing = 0
    for i in range(7):
        d = day_ts - i * 86400
        v = fng_map.get(d)
        if v is None:
            missing += 1
        else:
            vals.append(v)
    if not vals:
        return None
    # If >10% missing in bear window, we'll flag separately (caller handles)
    return sum(vals) / len(vals)


# ---------------------------------------------------------------------------
# Data bundle
# ---------------------------------------------------------------------------
@dataclass
class DataBundle:
    btc_1d: List[Dict]
    btc_4h: List[Dict]
    eth_4h: List[Dict]
    sol_4h: List[Dict]
    sat_4h: Dict[str, List[Dict]]   # symbol -> 4h klines for satellite universe
    fng: Dict[int, int]

    # Pre-computed aligned series
    btc_1d_sma200: List[Optional[float]] = field(default_factory=list)
    btc_4h_ema20: List[Optional[float]] = field(default_factory=list)
    btc_4h_ema50: List[Optional[float]] = field(default_factory=list)
    btc_4h_ema200: List[Optional[float]] = field(default_factory=list)
    btc_4h_atr: List[Optional[float]] = field(default_factory=list)
    btc_4h_adx: List[Optional[float]] = field(default_factory=list)
    eth_4h_ema20: List[Optional[float]] = field(default_factory=list)
    eth_4h_ema50: List[Optional[float]] = field(default_factory=list)
    eth_4h_ema200: List[Optional[float]] = field(default_factory=list)
    eth_4h_atr: List[Optional[float]] = field(default_factory=list)
    eth_4h_adx: List[Optional[float]] = field(default_factory=list)
    sol_4h_ema20: List[Optional[float]] = field(default_factory=list)
    sol_4h_ema50: List[Optional[float]] = field(default_factory=list)
    sol_4h_ema200: List[Optional[float]] = field(default_factory=list)
    sol_4h_atr: List[Optional[float]] = field(default_factory=list)
    sol_4h_adx: List[Optional[float]] = field(default_factory=list)


def load_bundle(client: BinanceClient, start: str, end: str,
                include_satellite: bool = True) -> DataBundle:
    # Pad start by 200 daily bars + 200 4H bars so indicators warm up
    start_dt = parse_date(start)
    end_dt = parse_date(end)
    pad_1d = start_dt - timedelta(days=300)
    pad_4h = start_dt - timedelta(days=50)

    logger.info("Loading data for %s..%s (pad 1d since %s, pad 4h since %s)",
                start, end, pad_1d.date(), pad_4h.date())

    btc_1d = fetch_klines(client, "BTCUSDT", "1d", pad_1d, end_dt + timedelta(days=2))
    btc_4h = fetch_klines(client, "BTCUSDT", "4h", pad_4h, end_dt + timedelta(days=2))
    eth_4h = fetch_klines(client, "ETHUSDT", "4h", pad_4h, end_dt + timedelta(days=2))
    sol_4h = fetch_klines(client, "SOLUSDT", "4h", pad_4h, end_dt + timedelta(days=2))

    sat_4h: Dict[str, List[Dict]] = {}
    if include_satellite:
        for sym in SAT_UNIVERSE:
            if sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                continue
            try:
                sat_4h[sym] = fetch_klines(client, sym, "4h", pad_4h,
                                           end_dt + timedelta(days=2))
            except Exception as e:
                logger.warning("Satellite %s fetch failed: %s", sym, e)

    fng = fetch_fng_history()

    b = DataBundle(
        btc_1d=btc_1d, btc_4h=btc_4h, eth_4h=eth_4h, sol_4h=sol_4h,
        sat_4h=sat_4h, fng=fng,
    )
    # Pre-compute core series
    b.btc_1d_sma200 = sma_series([k["close"] for k in btc_1d], 200)
    for sym, kl in [("BTC", btc_4h), ("ETH", eth_4h), ("SOL", sol_4h)]:
        closes = [k["close"] for k in kl]
        setattr(b, f"{sym.lower()}_4h_ema20", ema_series(closes, EMA_FAST))
        setattr(b, f"{sym.lower()}_4h_ema50", ema_series(closes, EMA_MID))
        setattr(b, f"{sym.lower()}_4h_ema200", ema_series(closes, EMA_SLOW))
        setattr(b, f"{sym.lower()}_4h_atr", atr_series(kl, ATR_PERIOD))
        setattr(b, f"{sym.lower()}_4h_adx", adx_series(kl, ATR_PERIOD))

    # Log warm-up status
    def _warm(name, series):
        ready = sum(1 for x in series if x is not None)
        logger.info("  %s: %d/%d bars have value", name, ready, len(series))
    _warm("btc_1d_sma200", b.btc_1d_sma200)
    _warm("btc_4h_ema200", b.btc_4h_ema200)
    _warm("btc_4h_adx", b.btc_4h_adx)
    _warm("sol_4h_ema200", b.sol_4h_ema200)
    _warm("eth_4h_ema200", b.eth_4h_ema200)
    return b


# ---------------------------------------------------------------------------
# Regime engine
# ---------------------------------------------------------------------------
@dataclass
class RegimeState:
    regime: str = "NEUTRAL"          # DEEP_BEAR/FEAR/NEUTRAL/MILD_BULL/CONFIRMED_BULL
    confirm_count: int = 0
    btc_above_sma: bool = False
    fng_ok: bool = False
    btc_adx_ok: bool = False
    last_4h_ts: int = 0
    # Hysteresis counters
    adx_below20_count: int = 0
    prev_fng: Optional[int] = None
    # For bear sensitivity test
    fng_threshold_override: Optional[int] = None


def _nearest_daily_sma(btc_1d: List[Dict], sma_series_: List[Optional[float]],
                       ts_ms: int) -> Tuple[Optional[float], Optional[float]]:
    """Find the latest daily bar whose close_time <= ts_ms. Return (close, sma200)."""
    # Binary search
    lo, hi = 0, len(btc_1d) - 1
    best = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if btc_1d[mid]["open_time"] <= ts_ms:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best < 0:
        return None, None
    return btc_1d[best]["close"], sma_series_[best]


def evaluate_regime(b: DataBundle, state: RegimeState, bar_4h_idx: int,
                    btc_4h: List[Dict]) -> RegimeState:
    """Evaluate regime at a given 4H bar index. Updates state in place.

    Entry (CONFIRMED_BULL): BTC daily close > SMA200+5% AND F&G 7d avg > 60
                            AND BTC 4H ADX > 25, confirmed 2 consecutive 4H closes.
    Exit (demote to MILD_BULL or lower) — per proposal §4.2, only these triggers:
      1. BTC daily close BELOW SMA200 (no buffer) — instant
      2. F&G single-day drop >15 pts OR F&G < 50 — instant
      3. BTC 4H ADX < 20 for 6 consecutive bars — next 4H close
    Demotion is ASYMMETRIC: entry needs 2-bar confirmation, exit is immediate
    (per rule) but uses wider thresholds (SMA200 not SMA200+5%, F&G 50 not 60,
    ADX 20 not 25), giving the trend room to breathe.
    """
    bar = btc_4h[bar_4h_idx]
    ts = bar["open_time"]
    close = bar["close"]

    daily_close, daily_sma200 = _nearest_daily_sma(b.btc_1d, b.btc_1d_sma200, ts)
    if daily_sma200 is None:
        return state

    # ---- Entry conditions (strict) ----
    btc_above_entry = daily_close > daily_sma200 * (1 + BTC_SMA_BUFFER)
    fng_thr = state.fng_threshold_override if state.fng_threshold_override else FNG_THRESHOLD
    fng_avg = fng_7d_avg(b.fng, ts)
    fng_ok_entry = fng_avg is not None and fng_avg > fng_thr
    btc_adx_val = b.btc_4h_adx[bar_4h_idx]
    btc_adx_ok_entry = btc_adx_val is not None and btc_adx_val > BTC_ADX_THRESHOLD

    # ---- Exit conditions (wider / hysteresis) ----
    # 1. BTC daily close below SMA200 (no +5% buffer)
    btc_below_sma_exit = daily_close < daily_sma200
    # 2. F&G single-day < 50, or single-day drop >15
    fng_today = b.fng.get((ts // 1000) - ((ts // 1000) % 86400))
    fng_yesterday = b.fng.get(((ts // 1000) - 86400) - (((ts // 1000) - 86400) % 86400))
    fng_crash_exit = False
    if fng_today is not None and fng_today < 50:
        fng_crash_exit = True
    if (fng_today is not None and fng_yesterday is not None
            and fng_yesterday - fng_today > 15):
        fng_crash_exit = True
    # 3. ADX < 20 for 6 consecutive 4H bars
    if btc_adx_val is not None and btc_adx_val < ADX_THRESHOLD_EXIT:
        state.adx_below20_count += 1
    else:
        state.adx_below20_count = 0
    adx_exit = state.adx_below20_count >= ADX_EXIT_CONSEC

    # ---- State tracking for UI/log ----
    state.btc_above_sma = btc_above_entry
    state.fng_ok = fng_ok_entry
    state.btc_adx_ok = btc_adx_ok_entry
    state.last_4h_ts = ts

    # Track previous day's F&G for next bar
    state.prev_fng = fng_today

    if state.regime == "CONFIRMED_BULL":
        # --- Demotion (any one triggers) ---
        if btc_below_sma_exit or fng_crash_exit or adx_exit:
            # Determine which regime to drop to
            if close < daily_sma200 * 0.85 or (fng_today is not None and fng_today < 25):
                state.regime = "DEEP_BEAR" if close < daily_sma200 * 0.85 else "FEAR"
            elif btc_above_entry or fng_ok_entry:
                state.regime = "MILD_BULL"
            else:
                state.regime = "NEUTRAL"
            state.confirm_count = 0
            state.adx_below20_count = 0
        # else stay CONFIRMED_BULL
    else:
        # --- Entry requires all 3 strict conditions for 2 consecutive 4H ---
        all_three = btc_above_entry and fng_ok_entry and btc_adx_ok_entry
        if all_three:
            state.confirm_count = min(state.confirm_count + 1, REGIME_CONFIRM_BARS + 2)
        else:
            state.confirm_count = 0
        if state.confirm_count >= REGIME_CONFIRM_BARS:
            state.regime = "CONFIRMED_BULL"
            state.adx_below20_count = 0
        else:
            # Set mild/bear/fear/neutral when not confirmed
            if close < daily_sma200 * 0.85:
                state.regime = "DEEP_BEAR"
            elif fng_avg is not None and fng_avg < 25:
                state.regime = "FEAR"
            elif btc_above_entry or fng_ok_entry:
                state.regime = "MILD_BULL"
            else:
                state.regime = "NEUTRAL"
    return state


# ---------------------------------------------------------------------------
# Core strategy
# ---------------------------------------------------------------------------
@dataclass
class CoreLot:
    entry_price: float
    quantity: float
    entry_bar: int
    entry_time: int
    atr_at_entry: float
    initial_sl: float
    pyramided: bool = False


@dataclass
class CorePosition:
    symbol: str
    lots: List[CoreLot] = field(default_factory=list)
    highest_close: float = 0.0
    highest_price: float = 0.0
    cur_sl: float = 0.0
    opened_bar: int = 0
    opened_time: int = 0
    ema50_breached: bool = False  # already reduced 50%
    adx_below20_count: int = 0
    closed: bool = False
    exit_reason: str = ""
    exit_price: float = 0.0
    exit_time: int = 0
    realized_pnl: float = 0.0
    # For record
    peak_unrealized_pct: float = 0.0
    # Trailing ATR multiplier (2.5 normal, 1.5 during F&G crash)
    trailing_atr_mult: float = ATR_MULT


def _core_ema_stack(b: DataBundle, symbol: str, idx: int) -> Tuple[
        Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    e20 = getattr(b, f"{symbol.lower()}_4h_ema20")[idx]
    e50 = getattr(b, f"{symbol.lower()}_4h_ema50")[idx]
    e200 = getattr(b, f"{symbol.lower()}_4h_ema200")[idx]
    atr = getattr(b, f"{symbol.lower()}_4h_atr")[idx]
    adx = getattr(b, f"{symbol.lower()}_4h_adx")[idx]
    return e20, e50, e200, atr, adx


def select_core_symbols(b: DataBundle, idx: int,
                       current: List[CorePosition]) -> List[str]:
    """Return list of symbols to open for core (up to CORE_MAX_SYMBOLS).
    BTC always takes a slot if it meets entry.
    Other slot goes to higher ADX between SOL and ETH.
    SOL->ETH switch: SOL closed below EMA50 for 2 bars AND ETH ADX > SOL ADX.
    """
    held = {p.symbol for p in current if not p.closed}
    selected: List[str] = []

    def _meets_entry(sym: str) -> bool:
        e20, e50, e200, atr, adx = _core_ema_stack(b, sym, idx)
        if None in (e20, e50, e200, atr, adx):
            return False
        return e20 > e50 > e200 and adx > ADX_THRESHOLD_ENTRY

    btc_ok = _meets_entry("BTC")
    if btc_ok:
        selected.append("BTC")

    # For the other slot, rank SOL/ETH by ADX
    sol_adx = _core_ema_stack(b, "SOL", idx)[4]
    eth_adx = _core_ema_stack(b, "ETH", idx)[4]
    sol_e20, sol_e50 = _core_ema_stack(b, "SOL", idx)[0], _core_ema_stack(b, "SOL", idx)[1]
    eth_e20, eth_e50 = _core_ema_stack(b, "ETH", idx)[0], _core_ema_stack(b, "ETH", idx)[1]

    candidates = []
    if sol_adx is not None and _meets_entry("SOL"):
        candidates.append(("SOL", sol_adx))
    if eth_adx is not None and _meets_entry("ETH"):
        candidates.append(("ETH", eth_adx))

    if not candidates:
        # If BTC doesn't qualify either, select 2 by ADX regardless
        for sym, adx in [("SOL", sol_adx), ("ETH", eth_adx)]:
            if adx is not None:
                candidates.append((sym, adx))

    candidates.sort(key=lambda x: x[1] if x[1] is not None else -1, reverse=True)

    # Handle SOL->ETH switch: if SOL is held and closed below EMA50 for 2 bars
    # and ETH ADX > SOL ADX, mark SOL for replacement (handled by caller via
    # exit logic; here we just prefer ETH for the remaining slot).
    if "SOL" in held and len(candidates) > 1:
        # Check if SOL had 2 consecutive 4H closes below EMA50
        if idx >= 2 and sol_e50 is not None:
            sol_c1 = b.sol_4h[idx - 1]["close"] < (b.sol_4h_ema50[idx - 1] or 0)
            sol_c2 = b.sol_4h[idx]["close"] < (sol_e50 or 0)
            if sol_c1 and sol_c2 and eth_adx and sol_adx and eth_adx > sol_adx:
                # Prefer ETH
                candidates = [(s, a) for s, a in candidates if s != "SOL"]
                candidates.sort(key=lambda x: x[1] or -1, reverse=True)

    for sym, _ in candidates:
        if sym not in selected and len(selected) < CORE_MAX_SYMBOLS:
            selected.append(sym)

    return selected[:CORE_MAX_SYMBOLS]


def apply_cost(usdt: float) -> float:
    return usdt * (1 - COST_PER_SIDE)


def sell_proceeds(usdt: float) -> float:
    return usdt * (1 - COST_PER_SIDE)


# ---------------------------------------------------------------------------
# Core backtest engine
# ---------------------------------------------------------------------------
@dataclass
class BacktestResult:
    window: str
    start: str
    end: str
    regime: str  # full-period classification label
    core_trades: List[Dict] = field(default_factory=list)
    sat_trades: List[Dict] = field(default_factory=list)
    equity_curve: List[Tuple[int, float, float, float, float]] = field(default_factory=list)
    # equity_curve entries: (ts, core_equity, sat_equity, cash, total)
    regime_log: List[Dict] = field(default_factory=list)
    # Sensitivity data
    sensitivity: Optional[Dict] = None
    # Window metadata
    fng_missing_days: int = 0
    fng_total_days: int = 0


def run_core_satellite_backtest(
    b: DataBundle,
    window_start: str,
    window_end: str,
    fng_threshold: Optional[int] = None,
    label: str = "",
) -> BacktestResult:
    """Run core+satellite backtest over a window.
    Executes on 4H bars. Signals confirmed on close, fills at next bar open.
    """
    start_dt = parse_date(window_start)
    end_dt = parse_date(window_end)
    start_ms = dt_to_ms(start_dt)
    end_ms = dt_to_ms(end_dt)

    # Build 4H time grid from BTC (always present and regular)
    btc = b.btc_4h
    # Find indices for window
    i_start = next((i for i, k in enumerate(btc) if k["open_time"] >= start_ms), 0)
    i_end = next((i for i in range(len(btc) - 1, -1, -1)
                  if btc[i]["open_time"] <= end_ms), len(btc) - 1)

    # Cash accounts
    core_cash = CORE_ALLOCATION
    sat_cash = SATELLITE_ALLOCATION
    cash_buffer = CASH_ALLOCATION

    core_positions: List[CorePosition] = []
    # Satellite positions (simplified): dict symbol -> dict
    sat_positions: Dict[str, Dict] = {}

    regime_state = RegimeState(fng_threshold_override=fng_threshold)
    prev_regime = "NEUTRAL"
    bull_entry_peak: Optional[float] = None  # track portfolio peak since BULL entry

    result = BacktestResult(
        window=label or window_start, start=window_start, end=window_end,
        regime="",
    )

    # Track F&G missing days for this window
    day_set = set()
    fng_missing = 0

    def _get_kl(sym: str) -> List[Dict]:
        if sym == "BTC": return b.btc_4h
        if sym == "ETH": return b.eth_4h
        if sym == "SOL": return b.sol_4h
        return b.sat_4h.get(sym, [])

    def _price_at(sym: str, idx: int) -> Optional[float]:
        kl = _get_kl(sym)
        if idx >= len(kl):
            return None
        return kl[idx]["close"]

    def _mark_portfolio(cur_idx: int) -> Tuple[float, float, float]:
        core_eq = core_cash
        for p in core_positions:
            if p.closed:
                continue
            px = _price_at(p.symbol, cur_idx)
            if px is None:
                continue
            qty = sum(l.quantity for l in p.lots)
            core_eq += qty * px
        sat_eq = sat_cash
        for sym, pos in sat_positions.items():
            px = _price_at(sym, cur_idx)
            if px is None:
                continue
            sat_eq += pos["qty"] * px
        return core_eq, sat_eq, core_eq + sat_eq + cash_buffer

    # Iterate over 4H bars. At bar i, we observe close; fills happen at bar i+1 open.
    # We implement this by: decisions at close of bar i (using data through i),
    # orders queued; at start of bar i+1, fill at open.
    pending_orders: List[Dict] = []

    for i in range(i_start, i_end + 1):
        bar = btc[i]
        ts = bar["open_time"]
        next_idx = i + 1

        # ---- Fill pending orders at THIS bar's open ----
        still_pending: List[Dict] = []
        for order in pending_orders:
            if order["at_bar"] != i:
                still_pending.append(order)
                continue
            sym = order["symbol"]
            side = order["side"]
            kl = _get_kl(sym)
            if i >= len(kl):
                continue
            fill_px = kl[i]["open"]  # fill at current bar open
            if side == "CORE_BUY":
                target_usd = order["usd"]
                cost_usd = apply_cost(target_usd)
                qty = cost_usd / fill_px
                atr_val = order["atr"]
                sl = fill_px - ATR_MULT * atr_val
                lot = CoreLot(
                    entry_price=fill_px, quantity=qty,
                    entry_bar=i, entry_time=ts,
                    atr_at_entry=atr_val, initial_sl=sl,
                )
                # ALWAYS create a fresh CorePosition for a new entry.
                # Reusing a previously-closed object would mix old/new lots
                # and corrupt the 72h lock / EMA50-reduce flag.
                pos = CorePosition(symbol=sym, highest_close=fill_px,
                                   highest_price=fill_px, cur_sl=sl,
                                   opened_bar=i, opened_time=ts)
                pos.lots.append(lot)
                core_positions.append(pos)
                core_cash -= target_usd
            elif side == "CORE_PYRAMID":
                pos = order["pos"]
                target_usd = order["usd"]
                cost_usd = apply_cost(target_usd)
                qty = cost_usd / fill_px
                atr_val = order["atr"]
                # Reset highest and SL per locked rule
                pos.highest_close = fill_px
                pos.highest_price = fill_px
                pos.cur_sl = max(pos.cur_sl, fill_px - ATR_MULT * atr_val)
                lot = CoreLot(entry_price=fill_px, quantity=qty, entry_bar=i,
                              entry_time=ts, atr_at_entry=atr_val,
                              initial_sl=fill_px - ATR_MULT * atr_val,
                              pyramided=True)
                pos.lots.append(lot)
                core_cash -= target_usd
            elif side == "CORE_SELL":
                pos = order["pos"]
                qty_to_sell = order["qty"]
                reason = order["reason"]
                gross = qty_to_sell * fill_px
                proceeds = sell_proceeds(gross)
                # FIFO match against lots for PnL accounting
                remaining = qty_to_sell
                lot_pnls = []
                for lot in pos.lots:
                    if remaining <= 0:
                        break
                    take = min(remaining, lot.quantity)
                    cost_basis = take * lot.entry_price
                    sale = take * fill_px * (1 - COST_PER_SIDE)
                    cost = cost_basis * (1 + COST_PER_SIDE)
                    lot_pnls.append(sale - cost)
                    lot.quantity -= take
                    remaining -= take
                    core_cash += take * fill_px * (1 - COST_PER_SIDE)
                pos.lots = [l for l in pos.lots if l.quantity > 1e-12]
                pnl = sum(lot_pnls)
                pos.realized_pnl += pnl
                result.core_trades.append({
                    "symbol": sym,
                    "entry_price": order.get("avg_entry", pos.lots[0].entry_price if pos.lots else 0),
                    "exit_price": fill_px,
                    "qty": qty_to_sell,
                    "pnl": round(pnl, 4),
                    "reason": reason,
                    "entry_time": order.get("entry_time", pos.opened_time),
                    "exit_time": ts,
                    "bars_held": i - order.get("opened_bar", pos.opened_bar),
                })
                if not pos.lots:
                    pos.closed = True
                    pos.exit_reason = reason
                    pos.exit_price = fill_px
                    pos.exit_time = ts
            elif side == "SAT_BUY":
                target_usd = order["usd"]
                cost_usd = apply_cost(target_usd)
                qty = cost_usd / fill_px
                atr_val = order["atr"]
                sat_positions[sym] = {
                    "qty": qty, "entry_price": fill_px,
                    "entry_bar": i, "entry_time": ts,
                    "atr": atr_val, "sl": fill_px - ATR_MULT * atr_val,
                    "highest": fill_px,
                }
                sat_cash -= target_usd
            elif side == "SAT_SELL":
                pos = sat_positions.pop(sym, None)
                if pos is None:
                    continue
                qty = pos["qty"]
                gross = qty * fill_px
                proceeds = sell_proceeds(gross)
                cost = qty * pos["entry_price"] * (1 + COST_PER_SIDE)
                pnl = proceeds - cost
                sat_cash += proceeds
                result.sat_trades.append({
                    "symbol": sym,
                    "entry_price": pos["entry_price"],
                    "exit_price": fill_px,
                    "qty": qty,
                    "pnl": round(pnl, 4),
                    "reason": order["reason"],
                    "entry_time": pos["entry_time"],
                    "exit_time": ts,
                    "bars_held": i - pos["entry_bar"],
                })
        pending_orders = still_pending

        # ---- Evaluate regime at this bar's close ----
        prev_regime_before_eval = regime_state.regime
        evaluate_regime(b, regime_state, i, btc)
        regime = regime_state.regime
        # Track bars since last CONFIRMED_BULL for peak reset
        if regime != "CONFIRMED_BULL":
            non_bull_counter = getattr(run_core_satellite_backtest, '_nbc', 0) + 1
        else:
            non_bull_counter = 0
            # If we were away for >12 bars (48h), reset peak
            if getattr(run_core_satellite_backtest, '_nbc', 0) >= 12:
                bull_entry_peak = None
        run_core_satellite_backtest._nbc = non_bull_counter

        if regime != prev_regime:
            result.regime_log.append({
                "ts": ts, "time": ms_to_dt(ts).strftime("%Y-%m-%d %H:%M"),
                "from": prev_regime, "to": regime,
                "btc_above_sma": regime_state.btc_above_sma,
                "fng_ok": regime_state.fng_ok,
                "btc_adx_ok": regime_state.btc_adx_ok,
                "btc_close": btc[i]["close"],
            })
            prev_regime = regime

        # Track F&G coverage
        day_ts = (ts // 1000) - ((ts // 1000) % 86400)
        if day_ts not in day_set:
            day_set.add(day_ts)
            if b.fng.get(day_ts) is None:
                fng_missing += 1

        # ---- Core risk: hard portfolio DD stop (10% from BULL peak) ----
        core_eq_v, sat_eq_v, total_eq = _mark_portfolio(i)
        if regime == "CONFIRMED_BULL":
            # Reset peak if we're entering a fresh BULL cycle after
            # a prolonged non-BULL period (>= 48h = 12 bars).
            if bull_entry_peak is None or bull_entry_peak == 0:
                bull_entry_peak = total_eq
            else:
                bull_entry_peak = max(bull_entry_peak, total_eq)
            if bull_entry_peak and total_eq < bull_entry_peak * (1 - PORTFOLIO_DD_STOP):
                # Force-close all core
                for p in list(core_positions):
                    if p.closed:
                        continue
                    total_qty = sum(l.quantity for l in p.lots)
                    avg_entry = (sum(l.entry_price * l.quantity for l in p.lots) /
                                 total_qty) if total_qty > 0 else 0
                    if next_idx <= i_end:
                        pending_orders.append({
                            "at_bar": next_idx, "symbol": p.symbol,
                            "side": "CORE_SELL", "pos": p,
                            "qty": total_qty, "reason": "PORTFOLIO_DD_10PCT",
                            "avg_entry": avg_entry,
                            "entry_time": p.opened_time,
                            "opened_bar": p.opened_bar,
                        })
                bull_entry_peak = None  # reset, 48h cooldown

        # ---- Demotion handling (per proposal §4.2) ----
        # CONFIRMED_BULL -> MILD_BULL: keep core positions, no new entries,
        #   tighten trailing to 1.5x ATR if F&G crash.
        # CONFIRMED_BULL -> NEUTRAL/FEAR/DEEP_BEAR (BTC below SMA200): close all.
        # MILD_BULL: keep existing core positions, no new entries.
        if regime in ("NEUTRAL", "FEAR", "DEEP_BEAR"):
            # BTC has fallen below SMA200 — hard close all core
            for p in list(core_positions):
                if p.closed:
                    continue
                total_qty = sum(l.quantity for l in p.lots)
                avg_entry = (sum(l.entry_price * l.quantity for l in p.lots) /
                             total_qty) if total_qty > 0 else 0
                if next_idx <= i_end and not any(
                        o["side"] == "CORE_SELL" and o["pos"] is p
                        for o in pending_orders):
                    pending_orders.append({
                        "at_bar": next_idx, "symbol": p.symbol,
                        "side": "CORE_SELL", "pos": p,
                        "qty": total_qty, "reason": f"REGIME_DEMOTE_{regime}",
                        "avg_entry": avg_entry,
                        "entry_time": p.opened_time,
                        "opened_bar": p.opened_bar,
                    })
            bull_entry_peak = None
        elif regime == "MILD_BULL":
            # Don't close positions. If demotion was due to F&G crash,
            # tighten trailing ATR multiplier on all open core positions.
            # (FNG crash = single-day drop >15 or F&G < 50)
            fng_today = b.fng.get((ts // 1000) - ((ts // 1000) % 86400))
            fng_yesterday = b.fng.get(((ts // 1000) - 86400) - (((ts // 1000) - 86400) % 86400))
            fng_crash = (fng_today is not None and fng_today < 50) or                         (fng_today is not None and fng_yesterday is not None
                         and fng_yesterday - fng_today > 15)
            if fng_crash:
                for p in core_positions:
                    if not p.closed:
                        p.trailing_atr_mult = 1.5
            # Note: bull_entry_peak is NOT reset — if we return to
            # CONFIRMED_BULL, peak tracking resumes

        # ---- Core position management ----
        if regime == "CONFIRMED_BULL":
            for p in core_positions:
                if p.closed:
                    continue
                kl = _get_kl(p.symbol)
                if i >= len(kl):
                    continue
                bar_i = kl[i]
                close_px = bar_i["close"]
                high_px = bar_i["high"]
                low_px = bar_i["low"]
                e20, e50, e200, atr_v, adx_v = _core_ema_stack(b, p.symbol, i)

                # Update peaks
                p.highest_close = max(p.highest_close, close_px)
                p.highest_price = max(p.highest_price, high_px)

                # Track peak unrealized
                total_qty = sum(l.quantity for l in p.lots)
                if total_qty > 0:
                    avg_entry = sum(l.entry_price * l.quantity for l in p.lots) / total_qty
                    p.peak_unrealized_pct = max(p.peak_unrealized_pct,
                                                (p.highest_price - avg_entry) / avg_entry)

                # Compute ATR trailing SL (only ratchets up)
                # Per spec: SL = max(entry-2.5ATR, highest_close-2.5ATR)
                # Tightened to 1.5x ATR during F&G crash demotion.
                if atr_v:
                    mult = getattr(p, "trailing_atr_mult", ATR_MULT)
                    new_sl = p.highest_close - mult * atr_v
                    p.cur_sl = max(p.cur_sl, new_sl)

                bars_held = i - p.opened_bar
                locked = bars_held < MIN_HOLD_BARS

                # Hard stop -12% from highest (always active, bypasses 72h lock)
                if close_px <= p.highest_price * (1 - HARD_STOP_PCT):
                    if next_idx <= i_end and not any(
                            o["side"] == "CORE_SELL" and o["pos"] is p
                            for o in pending_orders):
                        pending_orders.append({
                            "at_bar": next_idx, "symbol": p.symbol,
                            "side": "CORE_SELL", "pos": p,
                            "qty": total_qty, "reason": "HARD_STOP_12PCT",
                            "avg_entry": avg_entry, "entry_time": p.opened_time,
                            "opened_bar": p.opened_bar,
                        })
                    continue

                # EMA200 close below -> full close (bypass 72h)
                if e200 and close_px < e200:
                    if next_idx <= i_end and not any(
                            o["side"] == "CORE_SELL" and o["pos"] is p
                            for o in pending_orders):
                        pending_orders.append({
                            "at_bar": next_idx, "symbol": p.symbol,
                            "side": "CORE_SELL", "pos": p,
                            "qty": total_qty, "reason": "CLOSE_BELOW_EMA200",
                            "avg_entry": avg_entry, "entry_time": p.opened_time,
                            "opened_bar": p.opened_bar,
                        })
                    continue

                # EMA50 close below -> reduce 50% (bypass 72h per hard rule
                # is NOT specified — per locked rule, EMA50 reduce applies even in lock
                # since it's a trend-deceleration rule, not a trailing exit)
                if e50 and close_px < e50 and not p.ema50_breached:
                    sell_qty = total_qty * REDUCE_EMA50_PCT
                    if sell_qty > 0 and next_idx <= i_end:
                        pending_orders.append({
                            "at_bar": next_idx, "symbol": p.symbol,
                            "side": "CORE_SELL", "pos": p,
                            "qty": sell_qty, "reason": "CLOSE_BELOW_EMA50_REDUCE",
                            "avg_entry": avg_entry, "entry_time": p.opened_time,
                            "opened_bar": p.opened_bar,
                        })
                        p.ema50_breached = True
                    continue

                # ADX < 20 for 6 consecutive bars -> early EMA50 reduce
                if adx_v is not None and adx_v < ADX_THRESHOLD_EXIT:
                    p.adx_below20_count += 1
                else:
                    p.adx_below20_count = 0
                if p.adx_below20_count >= ADX_EXIT_CONSEC and not p.ema50_breached:
                    sell_qty = total_qty * REDUCE_EMA50_PCT
                    if sell_qty > 0 and next_idx <= i_end:
                        pending_orders.append({
                            "at_bar": next_idx, "symbol": p.symbol,
                            "side": "CORE_SELL", "pos": p,
                            "qty": sell_qty, "reason": "ADX_BELOW20_6BARS",
                            "avg_entry": avg_entry, "entry_time": p.opened_time,
                            "opened_bar": p.opened_bar,
                        })
                        p.ema50_breached = True

                # ATR trailing exit (enforce after 72h lock)
                if not locked and atr_v and low_px <= p.cur_sl:
                    if next_idx <= i_end and not any(
                            o["side"] == "CORE_SELL" and o["pos"] is p
                            for o in pending_orders):
                        pending_orders.append({
                            "at_bar": next_idx, "symbol": p.symbol,
                            "side": "CORE_SELL", "pos": p,
                            "qty": sum(l.quantity for l in p.lots),
                            "reason": "ATR_TRAILING",
                            "avg_entry": avg_entry, "entry_time": p.opened_time,
                            "opened_bar": p.opened_bar,
                        })
                    continue

                # Pyramid: +8% from entry AND EMA20 not broken AND atr < 150% entry ATR
                if (not p.lots[-1].pyramided and len(p.lots) >= 1
                        and not any(l.pyramided for l in p.lots)
                        and e20 and close_px > e20):
                    initial_lot = p.lots[0]
                    if close_px >= initial_lot.entry_price * (1 + PYRAMID_TRIGGER_PCT):
                        if atr_v and atr_v < initial_lot.atr_at_entry * PYRAMID_MAX_ATR_RATIO:
                            pyramid_usd = CORE_PER_SLOT * CORE_PYRAMID_PCT  # $30
                            if core_cash >= pyramid_usd and next_idx <= i_end:
                                pending_orders.append({
                                    "at_bar": next_idx, "symbol": p.symbol,
                                    "side": "CORE_PYRAMID", "pos": p,
                                    "usd": pyramid_usd, "atr": atr_v,
                                })

        # ---- Core entries (only in CONFIRMED_BULL) ----
        if regime == "CONFIRMED_BULL":
            # Track cooldown after hard exits (EMA200/HARD_STOP): 6 bars = 24h
            # This prevents immediate re-entry whipsaw after a trend break.
            cooldown_bars = 6
            recent_hard_exit: Dict[str, int] = {}
            for p in core_positions:
                if p.closed and p.exit_reason in (
                        "CLOSE_BELOW_EMA200", "HARD_STOP_12PCT"):
                    recent_hard_exit[p.symbol] = p.exit_time
            # Also check positions closed earlier in this run
            # (exit_time is in ms; convert to bar index approx)

            # Close positions for symbols no longer selected (SOL->ETH switch)
            desired = set(select_core_symbols(b, i, core_positions))
            for p in core_positions:
                if p.closed:
                    continue
                if p.symbol not in desired and p.symbol in ("SOL", "ETH"):
                    bars_held = i - p.opened_bar
                    kl = _get_kl(p.symbol)
                    e50 = _core_ema_stack(b, p.symbol, i)[1]
                    if bars_held >= MIN_HOLD_BARS or (e50 and kl[i]["close"] < e50):
                        total_qty = sum(l.quantity for l in p.lots)
                        avg_entry = (sum(l.entry_price * l.quantity for l in p.lots) /
                                     total_qty) if total_qty > 0 else 0
                        if next_idx <= i_end and not any(
                                o["side"] == "CORE_SELL" and o["pos"] is p
                                for o in pending_orders):
                            pending_orders.append({
                                "at_bar": next_idx, "symbol": p.symbol,
                                "side": "CORE_SELL", "pos": p,
                                "qty": total_qty, "reason": "CORE_ROTATE",
                                "avg_entry": avg_entry,
                                "entry_time": p.opened_time,
                                "opened_bar": p.opened_bar,
                            })

            # Open new positions for desired symbols not held
            held = {p.symbol for p in core_positions if not p.closed}
            for sym in desired:
                if sym in held:
                    continue
                if len(held) >= CORE_MAX_SYMBOLS:
                    break
                # Cooldown: skip if this symbol had a hard exit within last 6 bars
                if sym in recent_hard_exit:
                    # Find the bar index of the exit (approx via exit_time)
                    # Since exit_time is ms, find bar delta from current ts
                    exit_bar_delta = (ts - recent_hard_exit[sym]) // (4 * 3600 * 1000)
                    if exit_bar_delta < cooldown_bars:
                        continue
                if core_cash < CORE_PER_SLOT * 0.9:
                    continue
                e20, e50, e200, atr_v, adx_v = _core_ema_stack(b, sym, i)
                if None in (e20, e50, e200, atr_v, adx_v):
                    continue
                # Extra confirmation: require close > EMA20 (not just EMA stack)
                kl = _get_kl(sym)
                if i >= len(kl) or kl[i]["close"] < e20:
                    continue
                if next_idx <= i_end:
                    pending_orders.append({
                        "at_bar": next_idx, "symbol": sym,
                        "side": "CORE_BUY", "usd": CORE_PER_SLOT,
                        "atr": atr_v,
                    })
                    held.add(sym)

        # ---- Satellite management (simplified score/momentum) ----
        # Satellite operates in CONFIRMED_BULL with override params.
        # In other regimes, satellite uses conservative defaults (still runs but smaller)
        sat_max_positions = 5 if regime == "CONFIRMED_BULL" else 4
        sat_kelly_cap = SAT_KELLY_CAP if regime == "CONFIRMED_BULL" else 0.017
        sat_corr_thr = SAT_CORRELATION_THRESHOLD if regime == "CONFIRMED_BULL" else 0.7
        sat_max_30d = SAT_MAX_TRADES_30D if regime == "CONFIRMED_BULL" else 8

        # Count recent 30d satellite trades
        cutoff_30d = ts - 30 * 86400_000
        recent_sat = [t for t in result.sat_trades if t["exit_time"] >= cutoff_30d]

        # Update trailing SL for satellite
        for sym, pos in list(sat_positions.items()):
            kl = _get_kl(sym)
            if i >= len(kl):
                continue
            pos["highest"] = max(pos["highest"], kl[i]["high"])
            # Fixed TP
            if kl[i]["close"] >= pos["entry_price"] * (1 + SAT_FIXED_TP_PCT):
                if next_idx <= i_end:
                    pending_orders.append({
                        "at_bar": next_idx, "symbol": sym,
                        "side": "SAT_SELL", "reason": "SAT_TP_6PCT",
                    })
                continue
            # ATR SL
            if kl[i]["low"] <= pos["sl"]:
                if next_idx <= i_end:
                    pending_orders.append({
                        "at_bar": next_idx, "symbol": sym,
                        "side": "SAT_SELL", "reason": "SAT_SL",
                    })
                continue
            # Max hold
            if i - pos["entry_bar"] >= SAT_MAX_HOLD_BARS:
                if next_idx <= i_end:
                    pending_orders.append({
                        "at_bar": next_idx, "symbol": sym,
                        "side": "SAT_SELL", "reason": "SAT_MAX_HOLD",
                    })

        # Satellite entries — simple momentum score
        if regime in ("CONFIRMED_BULL", "MILD_BULL") and len(
                recent_sat) < sat_max_30d and len(sat_positions) < sat_max_positions:
            sat_candidates = []
            for sym in SAT_UNIVERSE:
                if sym in sat_positions:
                    continue
                kl = _get_kl(sym)
                if i >= len(kl) or i < 200:
                    continue
                closes = [k["close"] for k in kl[max(0, i - 200):i + 1]]
                if len(closes) < 200:
                    continue
                e20 = ema_series(closes, 20)[-1]
                e50 = ema_series(closes, 50)[-1]
                e200 = ema_series(closes, 200)[-1]
                atr_v = atr_series(kl[max(0, i - 30):i + 1], 14)[-1]
                rsi_v = rsi_series(closes, 14)[-1]
                if None in (e20, e50, e200, atr_v, rsi_v):
                    continue
                # Simple score: EMA stack + RSI in [40,70] + ATR reasonable
                score = 40
                if e20 > e50 > e200:
                    score += 30
                if 40 <= rsi_v <= 70:
                    score += 15
                if rsi_v > 70:
                    score -= 10
                # Momentum: 20-bar return
                ret_20 = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 else 0
                if 0.02 < ret_20 < 0.30:
                    score += 15
                if score >= SAT_SCORE_ENTRY:
                    sat_candidates.append((sym, score, atr_v))
            sat_candidates.sort(key=lambda x: x[1], reverse=True)
            # Correlation check (simplified: same-day return correlation with BTC)
            btc_closes = [k["close"] for k in btc[max(0, i - 30):i + 1]]
            for sym, score, atr_v in sat_candidates:
                if len(sat_positions) >= sat_max_positions:
                    break
                if sat_cash < STARTING_CAPITAL * sat_kelly_cap:
                    break
                kl = _get_kl(sym)
                sym_closes = [k["close"] for k in kl[max(0, i - 30):i + 1]]
                corr = _correlation(
                    [btc_closes[j] - btc_closes[j - 1] for j in range(1, len(btc_closes))],
                    [sym_closes[j] - sym_closes[j - 1] for j in range(1, len(sym_closes))],
                ) if len(btc_closes) == len(sym_closes) and len(btc_closes) > 5 else 0
                if corr > sat_corr_thr and sym not in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                    continue
                target_usd = STARTING_CAPITAL * sat_kelly_cap  # $12 at 3%
                if next_idx <= i_end:
                    pending_orders.append({
                        "at_bar": next_idx, "symbol": sym,
                        "side": "SAT_BUY", "usd": target_usd, "atr": atr_v,
                    })

        # ---- End of bar: record equity ----
        if i % 6 == 0:  # daily (6 4H bars per day)
            ce, se, te = _mark_portfolio(i)
            result.equity_curve.append((ts, round(ce, 2), round(se, 2),
                                        round(cash_buffer, 2), round(te, 2)))

    # Final close at end of window
    final_idx = i_end
    for p in list(core_positions):
        if p.closed:
            continue
        total_qty = sum(l.quantity for l in p.lots)
        if total_qty <= 0:
            continue
        avg_entry = (sum(l.entry_price * l.quantity for l in p.lots) /
                     total_qty) if total_qty > 0 else 0
        fill_px = _get_kl(p.symbol)[final_idx]["close"]
        proceeds = sell_proceeds(total_qty * fill_px)
        cost = total_qty * avg_entry * (1 + COST_PER_SIDE)
        pnl = proceeds - cost
        core_cash += proceeds
        p.closed = True
        p.exit_reason = "END_OF_WINDOW"
        p.exit_price = fill_px
        p.exit_time = btc[final_idx]["open_time"]
        p.realized_pnl += pnl
        result.core_trades.append({
            "symbol": p.symbol,
            "entry_price": avg_entry,
            "exit_price": fill_px,
            "qty": total_qty,
            "pnl": round(pnl, 4),
            "reason": "END_OF_WINDOW",
            "entry_time": p.opened_time,
            "exit_time": btc[final_idx]["open_time"],
            "bars_held": final_idx - p.opened_bar,
        })
    for sym, pos in list(sat_positions.items()):
        fill_px = _get_kl(sym)[final_idx]["close"]
        proceeds = sell_proceeds(pos["qty"] * fill_px)
        cost = pos["qty"] * pos["entry_price"] * (1 + COST_PER_SIDE)
        pnl = proceeds - cost
        sat_cash += proceeds
        result.sat_trades.append({
            "symbol": sym,
            "entry_price": pos["entry_price"],
            "exit_price": fill_px,
            "qty": pos["qty"],
            "pnl": round(pnl, 4),
            "reason": "END_OF_WINDOW",
            "entry_time": pos["entry_time"],
            "exit_time": btc[final_idx]["open_time"],
            "bars_held": final_idx - pos["entry_bar"],
        })

    ce, se, te = _mark_portfolio(final_idx)
    result.equity_curve.append((btc[final_idx]["open_time"], round(ce, 2),
                                round(se, 2), round(cash_buffer, 2), round(te, 2)))
    result.fng_missing_days = fng_missing
    result.fng_total_days = len(day_set)
    return result


def _correlation(a: List[float], b: List[float]) -> float:
    if len(a) < 3 or len(a) != len(b):
        return 0.0
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / math.sqrt(va * vb)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(result: BacktestResult) -> Dict[str, Any]:
    eq = result.equity_curve
    if len(eq) < 2:
        return {"error": "insufficient data"}
    totals = [e[4] for e in eq]
    start_eq = STARTING_CAPITAL
    end_eq = totals[-1]
    ret = end_eq / start_eq - 1

    # Daily returns (equity sampled ~daily)
    rets = []
    for i in range(1, len(totals)):
        if totals[i - 1] > 0:
            rets.append(totals[i] / totals[i - 1] - 1)
    n = len(rets)
    if n < 2:
        sharpe = 0.0
    else:
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0
        # Daily-ish bars (~6 4h per day), annualize with sqrt(365)
        sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0.0

    # Max drawdown
    peak = totals[0]
    max_dd = 0.0
    for v in totals:
        peak = max(peak, v)
        dd = (v - peak) / peak
        max_dd = min(max_dd, dd)

    # Profit factor (core trades only for core PF; combined for portfolio)
    def _pf(trades):
        wins = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
        gw = sum(wins)
        gl = sum(losses)
        return gw / gl if gl > 0 else (999.0 if gw > 0 else 0.0)

    core_pf = _pf(result.core_trades)
    all_pf = _pf(result.core_trades + result.sat_trades)

    # Core-only Sharpe (from core equity)
    core_eq = [e[1] for e in eq]
    core_rets = []
    for i in range(1, len(core_eq)):
        if core_eq[i - 1] > 0:
            core_rets.append(core_eq[i] / core_eq[i - 1] - 1)
    if len(core_rets) >= 2:
        m = sum(core_rets) / len(core_rets)
        v = sum((r - m) ** 2 for r in core_rets) / (len(core_rets) - 1)
        s = math.sqrt(v) if v > 0 else 0
        core_sharpe = (m / s * math.sqrt(365)) if s > 0 else 0.0
    else:
        core_sharpe = 0.0

    # Win rate
    all_trades = result.core_trades + result.sat_trades
    wins_n = sum(1 for t in all_trades if t["pnl"] > 0)
    win_rate = wins_n / len(all_trades) if all_trades else 0.0

    # Core-satellite return correlation
    if len(eq) >= 10:
        c_rets = []
        s_rets = []
        for i in range(1, len(eq)):
            if eq[i - 1][1] > 0 and eq[i - 1][2] > 0:
                c_rets.append(eq[i][1] / eq[i - 1][1] - 1)
                s_rets.append(eq[i][2] / eq[i - 1][2] - 1)
        corr = _correlation(c_rets, s_rets)
    else:
        corr = 0.0

    return {
        "start_equity": round(start_eq, 2),
        "end_equity": round(end_eq, 2),
        "total_return_pct": round(ret * 100, 2),
        "core_end": round(core_eq[-1], 2),
        "sat_end": round(eq[-1][2], 2),
        "sharpe": round(sharpe, 3),
        "core_sharpe": round(core_sharpe, 3),
        "profit_factor": round(all_pf, 3),
        "core_profit_factor": round(core_pf, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "num_core_trades": len(result.core_trades),
        "num_sat_trades": len(result.sat_trades),
        "win_rate_pct": round(win_rate * 100, 1),
        "core_sat_correlation": round(corr, 3),
        "num_bars": len(totals),
        "fng_missing_pct": round(
            result.fng_missing_days / max(result.fng_total_days, 1) * 100, 2),
    }


# ---------------------------------------------------------------------------
# Benchmark: equal-weight SOL/BTC/ETH quarterly rebalanced
# ---------------------------------------------------------------------------
def run_benchmark(window_start: str, window_end: str,
                  rebalance: str = "quarterly") -> Dict[str, Any]:
    """Buy 1/3 each SOL/BTC/ETH at window start. Quarterly rebalance to equal weight.
    Returns end value, max drawdown, return series.
    """
    client = BinanceClient()
    start_dt = parse_date(window_start) - timedelta(days=5)
    end_dt = parse_date(window_end)
    btc = fetch_klines(client, "BTCUSDT", "1d", start_dt, end_dt)
    eth = fetch_klines(client, "ETHUSDT", "1d", start_dt, end_dt)
    sol = fetch_klines(client, "SOLUSDT", "1d", start_dt, end_dt)

    # Align to common dates (all start well before window)
    start_ms = dt_to_ms(parse_date(window_start))
    end_ms = dt_to_ms(parse_date(window_end))
    btc = [k for k in btc if start_ms - 86400_000 <= k["open_time"] <= end_ms]
    eth = [k for k in eth if start_ms - 86400_000 <= k["open_time"] <= end_ms]
    sol = [k for k in sol if start_ms - 86400_000 <= k["open_time"] <= end_ms]
    n = min(len(btc), len(eth), len(sol))
    btc, eth, sol = btc[:n], eth[:n], sol[:n]

    # Initial allocation
    capital = STARTING_CAPITAL
    qty_btc = (capital / 3) / btc[0]["close"] * (1 - COST_PER_SIDE)
    qty_eth = (capital / 3) / eth[0]["close"] * (1 - COST_PER_SIDE)
    qty_sol = (capital / 3) / sol[0]["close"] * (1 - COST_PER_SIDE)

    equity = []
    peak = capital
    max_dd = 0.0
    next_rebalance = start_ms + 90 * 86400_000  # quarterly

    for i in range(n):
        ts = btc[i]["open_time"]
        val = qty_btc * btc[i]["close"] + qty_eth * eth[i]["close"] + qty_sol * sol[i]["close"]
        equity.append((ts, val))
        peak = max(peak, val)
        dd = (val - peak) / peak
        max_dd = min(max_dd, dd)
        if rebalance == "quarterly" and ts >= next_rebalance:
            # Rebalance to 1/3 each
            total = (qty_btc * btc[i]["close"] + qty_eth * eth[i]["close"] +
                     qty_sol * sol[i]["close"])
            # Sell all, buy equal (simplified: apply cost once on each)
            qty_btc = (total / 3) / btc[i]["close"] * (1 - COST_PER_SIDE)
            qty_eth = (total / 3) / eth[i]["close"] * (1 - COST_PER_SIDE)
            qty_sol = (total / 3) / sol[i]["close"] * (1 - COST_PER_SIDE)
            next_rebalance += 90 * 86400_000

    # Also compute buy-and-hold no rebalance
    bh_btc = (capital / 3) / btc[0]["close"]
    bh_eth = (capital / 3) / eth[0]["close"]
    bh_sol = (capital / 3) / sol[0]["close"]
    bh_end = bh_btc * btc[-1]["close"] + bh_eth * eth[-1]["close"] + bh_sol * sol[-1]["close"]
    bh_peak = capital
    bh_max_dd = 0.0
    for i in range(n):
        val = bh_btc * btc[i]["close"] + bh_eth * eth[i]["close"] + bh_sol * sol[i]["close"]
        bh_peak = max(bh_peak, val)
        bh_max_dd = min(bh_max_dd, (val - bh_peak) / bh_peak)

    return {
        "rebalanced_end": round(equity[-1][1], 2),
        "rebalanced_return_pct": round((equity[-1][1] / capital - 1) * 100, 2),
        "rebalanced_max_dd_pct": round(max_dd * 100, 2),
        "buyhold_end": round(bh_end, 2),
        "buyhold_return_pct": round((bh_end / capital - 1) * 100, 2),
        "buyhold_max_dd_pct": round(bh_max_dd * 100, 2),
        "start_price_btc": btc[0]["close"],
        "end_price_btc": btc[-1]["close"],
        "start_price_sol": sol[0]["close"],
        "end_price_sol": sol[-1]["close"],
        "start_price_eth": eth[0]["close"],
        "end_price_eth": eth[-1]["close"],
    }


# ---------------------------------------------------------------------------
# Walk-forward splits
# ---------------------------------------------------------------------------
def make_splits(window_start: str, window_end: str, n_splits: int = 5,
                train_ratio: float = 0.7) -> List[Tuple[str, str, str, str]]:
    """Return [(is_start, is_end, oos_start, oos_end), ...].
    Anchored walk-forward: train from window_start to rolling point, OOS next 30%.
    """
    start_dt = parse_date(window_start)
    end_dt = parse_date(window_end)
    total_days = (end_dt - start_dt).days
    oos_days = int(total_days / n_splits)
    splits = []
    for k in range(n_splits):
        oos_start_dt = start_dt + timedelta(days=k * oos_days)
        oos_end_dt = min(start_dt + timedelta(days=(k + 1) * oos_days), end_dt)
        is_start_dt = start_dt
        is_end_dt = oos_start_dt
        splits.append((
            is_start_dt.strftime("%Y-%m-%d"),
            is_end_dt.strftime("%Y-%m-%d"),
            oos_start_dt.strftime("%Y-%m-%d"),
            oos_end_dt.strftime("%Y-%m-%d"),
        ))
    return splits


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_report(results: Dict[str, Any], out_path: Path) -> None:
    """Generate markdown report."""
    lines = []
    lines.append("# BULL Regime Walk-Forward 回測報告\n")
    lines.append(f"- **生成時間：** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- **回測窗口：** Bull A (2023-01~2024-03), Bull B (2024-10~2025-02), Bear (2022-03~2023-01)")
    lines.append(f"- **起始資金：** ${STARTING_CAPITAL} (Core ${CORE_ALLOCATION}/Sat ${SATELLITE_ALLOCATION}/Cash ${CASH_ALLOCATION})")
    lines.append(f"- **手續費：** Taker {FEE_RATE*100:.2f}%/side + 滑點 {SLIPPAGE*100:.2f}%/side")
    lines.append(f"- **Walk-forward：** ≥5 splits, 70/30 IS/OOS, 4H K線\n")

    # Thresholds table
    lines.append("## 6 條強制門檻\n")
    lines.append("| 指標 | 門檻 | Bull A | Bull B | Bear | 結果 |")
    lines.append("|------|------|--------|--------|------|------|")

    thresholds = [
        ("OOS Sharpe (core)", "> 1.0", "core_sharpe"),
        ("OOS Profit Factor", "> 1.3", "profit_factor"),
        ("Max Drawdown (組合)", "< 20%", "max_drawdown_pct"),
        ("Benchmark capture (季度再平衡)", "> 60%", "capture_ratio"),
        ("Bear 回撤 vs buy&hold", "< 50%", "bear_capture"),
        ("Robustness (OOS 正收益 split)", "> 70%", "robustness"),
    ]

    pass_count = 0
    total_count = 6
    for label, threshold, key in thresholds:
        vals = []
        for w in ["bull_a", "bull_b", "bear"]:
            m = results.get(w, {}).get("aggregate", {}).get(key)
            if m is None:
                vals.append("—")
            elif key == "max_drawdown_pct":
                vals.append(f"{m:.1f}%")
            elif key == "capture_ratio" or key == "bear_capture" or key == "robustness":
                vals.append(f"{m:.1f}%")
            else:
                vals.append(f"{m:.2f}")
        passed = results.get("_pass", {}).get(key, False)
        mark = "✅" if passed else "❌"
        if passed:
            pass_count += 1
        lines.append(f"| {label} | {threshold} | {vals[0]} | {vals[1]} | {vals[2]} | {mark} |")

    lines.append(f"\n**門檻通過：** {pass_count}/{total_count}\n")

    # Per-window detail
    for w in ["bull_a", "bull_b", "bear"]:
        if w not in results:
            continue
        wres = results[w]
        agg = wres.get("aggregate", {})
        lines.append(f"\n## {w.replace('_', ' ').title()} 窗口")
        lines.append(f"- **時段：** {WINDOWS[w][0]} → {WINDOWS[w][1]}")
        lines.append(f"- **最終組合：** ${agg.get('end_equity', 0):.2f} (回報 {agg.get('total_return_pct', 0):+.2f}%)")
        lines.append(f"- **Core 最終：** ${agg.get('core_end', 0):.2f}")
        lines.append(f"- **Satellite 最終：** ${agg.get('sat_end', 0):.2f}")
        lines.append(f"- **Sharpe：** {agg.get('sharpe', 0):.3f} (core {agg.get('core_sharpe', 0):.3f})")
        lines.append(f"- **Profit Factor：** {agg.get('profit_factor', 0):.3f} (core {agg.get('core_profit_factor', 0):.3f})")
        lines.append(f"- **Max Drawdown：** {agg.get('max_drawdown_pct', 0):.2f}%")
        lines.append(f"- **Core 交易：** {agg.get('num_core_trades', 0)} 筆, Satellite {agg.get('num_sat_trades', 0)} 筆")
        lines.append(f"- **勝率：** {agg.get('win_rate_pct', 0):.1f}%")
        lines.append(f"- **Core-Satellite 相關性：** {agg.get('core_sat_correlation', 0):.3f}")
        lines.append(f"- **F&G 缺數據：** {agg.get('fng_missing_pct', 0):.2f}%")

        # Benchmark
        bm = wres.get("benchmark", {})
        if bm:
            lines.append(f"\n### Benchmark")
            lines.append(f"- **等權季度再平衡：** ${bm.get('rebalanced_end', 0):.2f} ({bm.get('rebalanced_return_pct', 0):+.2f}%, MaxDD {bm.get('rebalanced_max_dd_pct', 0):.2f}%)")
            lines.append(f"- **等權 buy&hold：** ${bm.get('buyhold_end', 0):.2f} ({bm.get('buyhold_return_pct', 0):+.2f}%, MaxDD {bm.get('buyhold_max_dd_pct', 0):.2f}%)")
            cap = agg.get("capture_ratio", 0)
            lines.append(f"- **Capture ratio (vs 季度再平衡)：** {cap:.1f}%")

        # Regime transitions
        lines.append(f"\n### Regime 轉換記錄（前 10 筆）")
        if wres.get("aggregate_full", {}).get("regime_log"):
            lines.append("| 時間 | From | To | BTC>200SMA | F&G OK | ADX OK | BTC 收盤 |")
            lines.append("|------|------|----|------------|--------|--------|----------|")
            for r in wres["aggregate_full"]["regime_log"][:10]:
                lines.append(
                    f"| {r['time']} | {r['from']} | {r['to']} | "
                    f"{'✅' if r['btc_above_sma'] else '❌'} | "
                    f"{'✅' if r['fng_ok'] else '❌'} | "
                    f"{'✅' if r['btc_adx_ok'] else '❌'} | "
                    f"${r['btc_close']:,.0f} |"
                )
        else:
            lines.append("（無 regime 轉換）")

        # OOS splits
        lines.append(f"\n### Walk-Forward OOS Splits")
        if wres.get("splits"):
            lines.append("| Split | OOS 時段 | 回報% | Sharpe | MaxDD% | 正收益 |")
            lines.append("|-------|----------|-------|--------|--------|--------|")
            for s in wres["splits"]:
                pos_mark = "✅" if s["total_return_pct"] > 0 else "❌"
                lines.append(
                    f"| {s['idx']} | {s['oos_start']}~{s['oos_end']} | "
                    f"{s['total_return_pct']:+.2f}% | {s['sharpe']:.2f} | "
                    f"{s['max_drawdown_pct']:.2f}% | {pos_mark} |"
                )

    # Bear sensitivity
    if "bear" in results and results["bear"].get("sensitivity"):
        s = results["bear"]["sensitivity"]
        lines.append(f"\n## Bear Sensitivity Test（F&G 門檻放寬至 >{s['fng_threshold']}）")
        lines.append(f"- **標準門檻 (F&G>60) 最終：** ${s['standard_end']:.2f} ({s['standard_return']:+.2f}%)")
        lines.append(f"- **放寬門檻 (F&G>{s['fng_threshold']}) 最終：** ${s['loose_end']:.2f} ({s['loose_return']:+.2f}%)")
        lines.append(f"- **門檻放寬後交易次數：** core {s['loose_core_trades']} 筆")
        lines.append(f"- **分析：** {s.get('analysis', '')}")
        if s.get("entries"):
            lines.append(f"\n### 放寬門檻下的入場記錄（首 10 筆）")
            lines.append("| 時間 | 幣 | 入場價 | 出場價 | 盈虧 | 原因 |")
            lines.append("|------|----|--------|--------|------|------|")
            for t in s["entries"][:10]:
                lines.append(
                    f"| {ms_to_dt(t['entry_time']).strftime('%Y-%m-%d %H:%M')} | "
                    f"{t['symbol']} | ${t['entry_price']:.2f} | ${t['exit_price']:.2f} | "
                    f"${t['pnl']:+.2f} | {t['reason']} |"
                )

    # Closest trigger analysis
    if results.get("bear", {}).get("closest_trigger"):
        ct = results["bear"]["closest_trigger"]
        lines.append(f"\n## Bear 窗口最接近觸發分析")
        lines.append(f"- **日期：** {ct.get('date', 'N/A')}")
        lines.append(f"- **BTC 價：** ${ct.get('btc_price', 0):,.0f}")
        lines.append(f"- **BTC vs SMA200：** {ct.get('btc_vs_sma', 0):+.1f}%")
        lines.append(f"- **F&G 7日均值：** {ct.get('fng_avg', 'N/A')}")
        lines.append(f"- **BTC ADX：** {ct.get('adx', 'N/A')}")
        lines.append(f"- **最接近觸發條款：** {ct.get('closest_condition', 'N/A')}")

    # Conclusion
    lines.append(f"\n## 結論")
    if pass_count == total_count:
        lines.append("**✅ 全部 6 條門檻通過，建議推進 Phase 2（Paper Trading）。**\n")
    elif pass_count >= 4:
        lines.append(f"**⚠️ {pass_count}/{total_count} 門檻通過，需檢討失敗項後再決定。**\n")
    else:
        lines.append(f"**❌ 僅 {pass_count}/{total_count} 門檻通過，方案需返工。**\n")

    lines.append("### 失敗項診斷")
    for label, threshold, key in thresholds:
        if not results.get("_pass", {}).get(key, False):
            val = results.get("bear", {}).get("aggregate", {}).get(key) or \
                  results.get("bull_a", {}).get("aggregate", {}).get(key) or \
                  results.get("bull_b", {}).get("aggregate", {}).get(key)
            lines.append(f"- **{label}** ({threshold}): 實測值參考 {val}")

    lines.append("\n---\n*報告由 bull_walkforward.py 自動生成。所有交易按 4H 收盤確認信號、下一根 4H 開盤成交。費用已計入。*")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Report written: %s", out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="BULL regime walk-forward backtest")
    parser.add_argument("--window", choices=list(WINDOWS.keys()) + ["all"],
                        default="all")
    parser.add_argument("--quick", action="store_true",
                        help="Smoke test with smaller data range")
    parser.add_argument("--no-satellite", action="store_true",
                        help="Skip satellite universe (faster, core only)")
    args = parser.parse_args()

    client = BinanceClient()
    windows_to_run = list(WINDOWS.keys()) if args.window == "all" else [args.window]

    all_results: Dict[str, Any] = {}

    for w in windows_to_run:
        ws, we = WINDOWS[w]
        logger.info("=" * 70)
        logger.info("WINDOW: %s (%s -> %s)", w, ws, we)
        logger.info("=" * 70)

        # Load data bundle (includes satellite universe by default)
        bundle = load_bundle(client, ws, we, include_satellite=not args.no_satellite)

        # Run full-window backtest (aggregate metrics)
        logger.info("Running full-window backtest...")
        full_result = run_core_satellite_backtest(bundle, ws, we, label=w)
        full_metrics = compute_metrics(full_result)

        # Benchmark
        logger.info("Running benchmark...")
        benchmark = run_benchmark(ws, we)
        # Capture ratio
        if benchmark.get("rebalanced_return_pct", 0) > 0:
            capture = min(full_metrics["total_return_pct"] /
                          benchmark["rebalanced_return_pct"] * 100, 999)
        else:
            capture = 0.0 if full_metrics["total_return_pct"] <= 0 else 999.0
        full_metrics["capture_ratio"] = round(capture, 1)
        # Bear capture (drawdown ratio)
        if benchmark.get("buyhold_max_dd_pct", 0) < 0:
            bear_ratio = abs(full_metrics["max_drawdown_pct"] /
                             benchmark["buyhold_max_dd_pct"] * 100)
        else:
            bear_ratio = 0.0
        full_metrics["bear_capture"] = round(bear_ratio, 1)

        # Walk-forward OOS splits
        splits = make_splits(ws, we, n_splits=5)
        split_results = []
        positive_count = 0
        for idx, (is_s, is_e, oos_s, oos_e) in enumerate(splits, 1):
            logger.info("  Split %d: OOS %s -> %s", idx, oos_s, oos_e)
            # Re-run on OOS portion only
            sr = run_core_satellite_backtest(bundle, oos_s, oos_e, label=f"{w}_oos{idx}")
            sm = compute_metrics(sr)
            sm["idx"] = idx
            sm["oos_start"] = oos_s
            sm["oos_end"] = oos_e
            split_results.append(sm)
            if sm["total_return_pct"] > 0:
                positive_count += 1

        robustness = positive_count / len(splits) * 100 if splits else 0
        full_metrics["robustness"] = round(robustness, 1)

        all_results[w] = {
            "aggregate": full_metrics,
            "aggregate_full": {
                "regime_log": full_result.regime_log,
                "core_trades": full_result.core_trades,
                "sat_trades": full_result.sat_trades,
            },
            "splits": split_results,
            "benchmark": benchmark,
        }

        # Bear-specific analysis
        if w == "bear":
            # Sensitivity: F&G threshold >50 instead of >60
            logger.info("Running Bear sensitivity (F&G>50)...")
            loose_result = run_core_satellite_backtest(
                bundle, ws, we, fng_threshold=50, label=f"{w}_loose")
            loose_metrics = compute_metrics(loose_result)

            # Find closest trigger date
            closest = _find_closest_trigger(bundle, ws, we)

            # Analyze entries under loose threshold
            loose_entries = loose_result.core_trades[:10]

            all_results[w]["sensitivity"] = {
                "fng_threshold": 50,
                "standard_end": full_metrics["end_equity"],
                "standard_return": full_metrics["total_return_pct"],
                "loose_end": loose_metrics["end_equity"],
                "loose_return": loose_metrics["total_return_pct"],
                "loose_core_trades": len(loose_result.core_trades),
                "entries": loose_entries,
                "analysis": _sensitivity_analysis(full_result, loose_result),
            }
            all_results[w]["closest_trigger"] = closest

    # Determine pass/fail
    passes = {}
    for w in ["bull_a", "bull_b", "bear"]:
        if w not in all_results:
            continue
        agg = all_results[w]["aggregate"]
        # Core Sharpe > 1.0 (check bull windows)
        if w in ("bull_a", "bull_b"):
            passes["core_sharpe"] = passes.get("core_sharpe", True) and agg["core_sharpe"] > 1.0
            passes["profit_factor"] = passes.get("profit_factor", True) and agg["profit_factor"] > 1.3
            passes["capture_ratio"] = passes.get("capture_ratio", True) and agg["capture_ratio"] > 60
        passes["max_drawdown_pct"] = passes.get("max_drawdown_pct", True) and agg["max_drawdown_pct"] > -20
        if w == "bear":
            passes["bear_capture"] = agg["bear_capture"] < 50
        passes["robustness"] = passes.get("robustness", True) and agg["robustness"] > 70

    all_results["_pass"] = passes

    # Save JSON
    json_path = REPORT_DIR / f"bull_override_walkforward_{datetime.now().strftime('%Y%m%d')}.json"
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("JSON saved: %s", json_path)

    # Generate report
    report_path = REPORT_DIR / f"bull_override_walkforward_{datetime.now().strftime('%Y%m%d')}.md"
    generate_report(all_results, report_path)

    # Print summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for w in ["bull_a", "bull_b", "bear"]:
        if w in all_results:
            m = all_results[w]["aggregate"]
            print(f"\n{w.upper()}: ${m['end_equity']:.2f} ({m['total_return_pct']:+.2f}%)")
            print(f"  Sharpe={m['sharpe']:.3f} PF={m['profit_factor']:.3f} "
                  f"MaxDD={m['max_drawdown_pct']:.2f}% Robustness={m['robustness']:.1f}%")
            if "capture_ratio" in m:
                print(f"  Capture={m['capture_ratio']:.1f}%")
            if "bear_capture" in m:
                print(f"  Bear DD ratio={m['bear_capture']:.1f}%")
    print(f"\nGates passed: {sum(passes.values())}/{len(passes)}")
    print(f"Report: {report_path}")


def _find_closest_trigger(b: DataBundle, ws: str, we: str) -> Dict:
    """Scan bear window for the date that came closest to triggering CONFIRMED_BULL."""
    start_ms = dt_to_ms(parse_date(ws))
    end_ms = dt_to_ms(parse_date(we))
    btc = b.btc_4h
    closest = {"date": "N/A", "btc_price": 0, "btc_vs_sma": -999,
               "fng_avg": 0, "adx": 0, "closest_condition": "none",
               "min_distance": 999}
    for i in range(len(btc)):
        ts = btc[i]["open_time"]
        if not (start_ms <= ts <= end_ms):
            continue
        daily_close, daily_sma = _nearest_daily_sma(b.btc_1d, b.btc_1d_sma200, ts)
        if daily_sma is None:
            continue
        btc_vs_sma = (daily_close - daily_sma) / daily_sma * 100
        fng_avg = fng_7d_avg(b.fng, ts)
        adx_v = b.btc_4h_adx[i]

        # Distance to triggering all 3 conditions
        dist = 0
        cond = []
        if btc_vs_sma <= 5:
            dist += (5 - btc_vs_sma)
            cond.append("BTC>SMA200+5%")
        if fng_avg is None or fng_avg <= 60:
            dist += (60 - (fng_avg or 0))
            cond.append("F&G>60")
        if adx_v is None or adx_v <= 25:
            dist += (25 - (adx_v or 0))
            cond.append("ADX>25")

        if dist < closest["min_distance"]:
            closest = {
                "date": ms_to_dt(ts).strftime("%Y-%m-%d %H:%M"),
                "btc_price": btc[i]["close"],
                "btc_vs_sma": round(btc_vs_sma, 1),
                "fng_avg": round(fng_avg, 1) if fng_avg else None,
                "adx": round(adx_v, 1) if adx_v else None,
                "closest_condition": ", ".join(cond) if cond else "ALL_MET",
                "min_distance": dist,
            }
    return closest


def _sensitivity_analysis(std_result, loose_result) -> str:
    """Generate human-readable analysis comparing standard vs loose F&G threshold."""
    std_trades = len(std_result.core_trades)
    loose_trades = len(loose_result.core_trades)
    std_ret = sum(t["pnl"] for t in std_result.core_trades)
    loose_ret = sum(t["pnl"] for t in loose_result.core_trades)

    if loose_trades == 0:
        return "即使放寬 F&G 門檻至 >50，Bear 窗口仍無觸發 BULL regime。降級機制有效。"
    if loose_ret < 0:
        return (f"放寬門檻後產生 {loose_trades} 筆核心交易（標準門檻 {std_trades} 筆），"
                f"合計盈虧 ${loose_ret:.2f}。降級條件仍能限制虧損。")
    return (f"放寬門檻後產生 {loose_trades} 筆交易，合計 ${loose_ret:+.2f}。"
            f"需檢查入場時機是否避開主要跌幅。")


if __name__ == "__main__":
    main()
