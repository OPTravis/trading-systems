"""
Macro-economic state analyzer.

Tracks key macro indicators and classifies the current regime as
expansion / peak / contraction / trough using the business cycle framework.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"


class MacroPhase(str, Enum):
    EXPANSION = "expansion"
    PEAK = "peak"
    CONTRACTION = "contraction"
    TROUGH = "trough"


@dataclass
class MacroState:
    """Snapshot of current macro-economic conditions."""

    timestamp: datetime
    phase: MacroPhase
    confidence: float  # 0.0-1.0
    fed_funds_rate: Optional[float] = None
    yield_spread_2y10y: Optional[float] = None  # basis points
    gdp_growth_yoy: Optional[float] = None  # percent
    vix_level: Optional[float] = None
    credit_spread_hyg_lqd: Optional[float] = None  # ratio
    unemployment_rate: Optional[float] = None
    cpi_yoy: Optional[float] = None
    summary: str = ""


# ─── FRED helpers ────────────────────────────────────────────────────────────


def _fred_latest(series_id: str, api_key: str) -> Optional[float]:
    """Fetch the latest value for a FRED series."""
    try:
        resp = requests.get(
            FRED_BASE_URL,
            params={  # type: ignore[arg-type]
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if obs:
            val = obs[0].get("value", ".")
            return float(val) if val != "." else None
    except Exception as e:
        logger.error("FRED fetch failed for %s: %s", series_id, e)
    return None


def _fred_cpi_12m_ago(api_key: str) -> Optional[float]:
    """Fetch the CPI value from approximately 12 months ago."""
    try:
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        resp = requests.get(
            FRED_BASE_URL,
            params={  # type: ignore[arg-type]
                "series_id": "CPIAUCSL",
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 1,
                "observation_end": cutoff_str,
            },
            timeout=15,
        )
        resp.raise_for_status()
        obs = resp.json().get("observations", [])
        if obs:
            val = obs[0].get("value", ".")
            return float(val) if val != "." else None
    except Exception as e:
        logger.error("FRED CPI 12m-ago fetch failed: %s", e)
    return None


# ─── MacroAnalyzer ───────────────────────────────────────────────────────────


class MacroAnalyzer:
    """
    Macro-economic regime classifier.

    Tracks Fed funds rate, yield curve spread, GDP growth, VIX,
    and credit spreads to determine the current business cycle phase.
    """

    def __init__(self, fred_api_key: Optional[str] = None):
        self.fred_key = fred_api_key or os.environ.get("FRED_API_KEY", "")
        if not self.fred_key:
            logger.warning("No FRED_API_KEY set — macro data will be unavailable")

    def get_macro_state(self) -> MacroState:
        """
        Fetch current macro indicators and classify the business cycle phase.

        Decision logic:
        - Expansion: positive GDP growth, normal yield curve, low VIX, tight credit
        - Peak: growth slowing, yield curve flattening/inverting, rising VIX
        - Contraction: negative GDP, inverted curve, high VIX, widening credit
        - Trough: bottoming indicators, VIX declining from highs, credit stabilizing
        """
        now = datetime.now(timezone.utc)

        if not self.fred_key:
            return MacroState(
                timestamp=now,
                phase=MacroPhase.EXPANSION,
                confidence=0.1,
                summary="No FRED API key configured. Cannot determine macro state.",
            )

        # Fetch indicators
        fed_rate = _fred_latest("FEDFUNDS", self.fred_key)
        t2y = _fred_latest("DGS2", self.fred_key)
        t10y = _fred_latest("DGS10", self.fred_key)
        gdp = _fred_latest(
            "A191RL1Q225SBEA", self.fred_key
        )  # real GDP growth QoQ annualized
        unrate = _fred_latest("UNRATE", self.fred_key)

        # VIX via yfinance (FRED has VIX as VIXCLS)
        vix = _fred_latest("VIXCLS", self.fred_key)

        # Yield spread
        spread_2y10y = None
        if t2y is not None and t10y is not None:
            spread_2y10y = round((t10y - t2y) * 100, 1)  # basis points

        # Credit spread proxy (HYG/LQD ratio — lower = wider spreads)
        credit_spread = self._get_credit_spread()

        # CPI YoY: fetch current and 12-month-ago CPI values to compute YoY
        cpi_yoy = None
        cpi_yoy_val = _fred_latest(
            "FPCPITOTLZGUSA", self.fred_key
        )  # annual CPI inflation
        if cpi_yoy_val is not None:
            cpi_yoy = cpi_yoy_val
        else:
            # Compute YoY from CPIAUCSL series
            cpi_now = _fred_latest("CPIAUCSL", self.fred_key)
            cpi_prev = _fred_cpi_12m_ago(self.fred_key)
            if cpi_now is not None and cpi_prev is not None and cpi_prev != 0:
                cpi_yoy = round(((cpi_now / cpi_prev) - 1) * 100, 2)

        # ── Classify phase ───────────────────────────────────────────────
        scores = {"expansion": 0, "peak": 0, "contraction": 0, "trough": 0}

        # Yield curve
        if spread_2y10y is not None:
            if spread_2y10y > 50:
                scores["expansion"] += 2
            elif spread_2y10y > 0:
                scores["expansion"] += 1
                scores["peak"] += 1
            elif spread_2y10y > -50:
                scores["peak"] += 2
                scores["contraction"] += 1
            else:
                scores["contraction"] += 3

        # GDP growth
        if gdp is not None:
            if gdp > 2.5:
                scores["expansion"] += 2
            elif gdp > 0:
                scores["expansion"] += 1
                scores["peak"] += 1
            elif gdp > -2:
                scores["contraction"] += 2
            else:
                scores["contraction"] += 3
                scores["trough"] += 1

        # VIX
        if vix is not None:
            if vix < 15:
                scores["expansion"] += 2
            elif vix < 20:
                scores["expansion"] += 1
            elif vix < 30:
                scores["peak"] += 1
                scores["contraction"] += 1
            else:
                scores["contraction"] += 2
                scores["trough"] += 1

        # Credit spreads
        if credit_spread is not None:
            if credit_spread > 0.85:
                scores["expansion"] += 1
            elif credit_spread > 0.80:
                scores["peak"] += 1
            else:
                scores["contraction"] += 2

        # Fed rate trajectory
        if fed_rate is not None:
            if fed_rate < 2.0:
                scores["expansion"] += 1
                scores["trough"] += 1
            elif fed_rate < 4.0:
                scores["expansion"] += 1
            elif fed_rate < 5.5:
                scores["peak"] += 1
            else:
                scores["contraction"] += 1

        # Pick winner
        phase_str = max(scores, key=lambda k: scores[k])
        phase = MacroPhase(phase_str)
        total = sum(scores.values()) or 1
        confidence = round(scores[phase_str] / total, 2)

        summary = self._build_summary(
            phase, fed_rate, spread_2y10y, gdp, vix, credit_spread
        )

        return MacroState(
            timestamp=now,
            phase=phase,
            confidence=confidence,
            fed_funds_rate=fed_rate,
            yield_spread_2y10y=spread_2y10y,
            gdp_growth_yoy=gdp,
            vix_level=vix,
            credit_spread_hyg_lqd=credit_spread,
            unemployment_rate=unrate,
            cpi_yoy=cpi_yoy,
            summary=summary,
        )

    def _get_credit_spread(self) -> Optional[float]:
        """Fetch HYG/LQD price ratio as a credit spread proxy."""
        try:
            import yfinance as yf

            hyg = yf.Ticker("HYG").history(period="5d")
            lqd = yf.Ticker("LQD").history(period="5d")
            if not hyg.empty and not lqd.empty:
                return round(hyg["Close"].iloc[-1] / lqd["Close"].iloc[-1], 4)
        except Exception as e:
            logger.warning("Credit spread fetch failed: %s", e)
        return None

    @staticmethod
    def _build_summary(
        phase: MacroPhase,
        fed_rate: Optional[float],
        spread: Optional[float],
        gdp: Optional[float],
        vix: Optional[float],
        credit: Optional[float],
    ) -> str:
        lines = [f"Macro regime: **{phase.value.upper()}**"]
        if fed_rate is not None:
            lines.append(f"Fed funds rate: {fed_rate:.2f}%")
        if spread is not None:
            inv = "⚠️ INVERTED" if spread < 0 else "normal"
            lines.append(f"2Y-10Y spread: {spread:+.0f}bp ({inv})")
        if gdp is not None:
            lines.append(f"GDP growth (QoQ ann.): {gdp:+.1f}%")
        if vix is not None:
            lines.append(f"VIX: {vix:.1f}")
        if credit is not None:
            lines.append(f"HYG/LQD ratio: {credit:.4f}")
        return "\n".join(lines)
