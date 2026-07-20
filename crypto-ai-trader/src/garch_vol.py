"""GARCH(1,1) Volatility Forecaster for crypto trading."""

import json
import logging
import math
import os
import time
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

DATA_DIR = os.path.expanduser("~/crypto-ai-trader/data")
MIN_DATA_POINTS = 30
ROLLING_WINDOW = 20

VOL_REGIMES = {
    # R:R ≥ 1:1.5 for TP1, 1:2.5 for TP2, 1:4 for TP3
    # Previously SL > TP1 (inverted), causing negative expected value
    "low": {
        "sl_pct": -0.05,
        "tp_pct": 0.08,
        "trailing_activation": 0.02,
        "trailing_step": 0.015,
    },
    "normal": {
        "sl_pct": -0.06,
        "tp_pct": 0.10,
        "trailing_activation": 0.03,
        "trailing_step": 0.02,
    },
    "high": {
        "sl_pct": -0.08,
        "tp_pct": 0.13,
        "trailing_activation": 0.04,
        "trailing_step": 0.03,
    },
    "extreme": {
        "sl_pct": -0.10,
        "tp_pct": 0.15,
        "trailing_activation": 0.06,
        "trailing_step": 0.04,
    },
}


def get_vol_regime(annualized_vol: float) -> str:
    if annualized_vol < 0.30:
        return "low"
    elif annualized_vol < 0.60:
        return "normal"
    elif annualized_vol < 1.00:
        return "high"
    return "extreme"


def _rolling_std_fallback(returns: List[float]) -> float:
    arr = np.array(returns)
    if len(arr) >= ROLLING_WINDOW:
        vol = np.std(arr[-ROLLING_WINDOW:])
    else:
        vol = np.std(arr)
    return vol * math.sqrt(365)


def forecast_volatility(returns: List[float], horizon: int = 1) -> Dict:
    n = len(returns)
    if n < MIN_DATA_POINTS:
        ann_vol = _rolling_std_fallback(returns)
        return {
            "current_vol": ann_vol / math.sqrt(365),
            "forecast_vol": ann_vol / math.sqrt(365),
            "annualized_vol": ann_vol,
            "vol_regime": get_vol_regime(ann_vol),
        }

    arr = np.array(returns) * 100  # scale for arch
    try:
        from arch import arch_model

        am = arch_model(arr, vol="Garch", p=1, q=1, mean="Constant")
        res = am.fit(disp="off")
        fcast = res.forecast(horizon=horizon)
        variance = fcast.variance
        forecast_var = (
            variance.iloc[-1].values[-1]
            if hasattr(variance, "iloc")
            else variance[-1, -1]
        )
        forecast_daily_vol = math.sqrt(forecast_var) / 100
        cond_vol = res.conditional_volatility
        current_daily_vol = (
            math.sqrt(cond_vol.iloc[-1] if hasattr(cond_vol, "iloc") else cond_vol[-1])
            / 100
        )
        ann_vol = forecast_daily_vol * math.sqrt(365)
        return {
            "current_vol": current_daily_vol,
            "forecast_vol": forecast_daily_vol,
            "annualized_vol": ann_vol,
            "vol_regime": get_vol_regime(ann_vol),
        }
    except Exception:
        logger.error(
            "GARCH model forecast failed, falling back to rolling std", exc_info=True
        )
        ann_vol = _rolling_std_fallback(returns)
        return {
            "current_vol": ann_vol / math.sqrt(365),
            "forecast_vol": ann_vol / math.sqrt(365),
            "annualized_vol": ann_vol,
            "vol_regime": get_vol_regime(ann_vol),
        }


def get_dynamic_sl_tp(symbol: str, entry_price: float, current_vol: float) -> Dict:
    ann_vol = current_vol * math.sqrt(365)
    regime = get_vol_regime(ann_vol)
    params = VOL_REGIMES[regime]
    return {
        "sl_pct": params["sl_pct"],
        "tp_pct": params["tp_pct"],
        "trailing_activation": params["trailing_activation"],
        "trailing_step": params["trailing_step"],
    }


def train_from_klines(symbol: str, klines: List[Dict]) -> bool:
    if len(klines) < MIN_DATA_POINTS:
        return False
    closes = [float(k["close"]) for k in klines]
    log_returns = np.diff(np.log(closes)).tolist()
    if len(log_returns) < MIN_DATA_POINTS:
        return False
    try:
        from arch import arch_model

        arr = np.array(log_returns) * 100
        am = arch_model(arr, vol="Garch", p=1, q=1, mean="Constant")
        res = am.fit(disp="off")
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"garch_{symbol}.json")
        # Save model parameters as JSON (not the full model object)
        params = {
            "params": {k: float(v) for k, v in res.params.items()},
            "volatility": (
                float(res.conditional_volatility[-1])
                if hasattr(res, "conditional_volatility")
                else 0.0
            ),
        }
        with open(path, "w") as f:
            json.dump(params, f)
        return True
    except Exception:
        logger.error("GARCH model training failed for %s", symbol, exc_info=True)
        return False


def load_model(symbol: str) -> Optional[dict]:
    """Load saved GARCH parameters and cached forecast.

    Returns a dict with:
      - params: raw GARCH coefficients
      - volatility: cached conditional volatility at save time
      - annualized_vol: pre-computed annualized vol from save time
      - vol_regime: pre-computed regime from save time
      - saved_at: file modification timestamp (for staleness checks)

    Callers should check staleness (e.g., >24h old) and retrain if needed.
    """
    path = os.path.join(DATA_DIR, f"garch_{symbol}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            data = json.load(f)
        # Enrich with derived fields so callers can use directly
        saved_vol = data.get("volatility", 0.0)
        if saved_vol and not data.get("annualized_vol"):
            data["annualized_vol"] = saved_vol * math.sqrt(365) if saved_vol < 1 else saved_vol
            data["vol_regime"] = get_vol_regime(data["annualized_vol"])
        elif not data.get("annualized_vol"):
            data["annualized_vol"] = 0.0
            data["vol_regime"] = "normal"
        data["saved_at"] = os.path.getmtime(path)
        data["stale"] = (time.time() - data["saved_at"]) > 86400  # >24h = stale
        return data
    return None
