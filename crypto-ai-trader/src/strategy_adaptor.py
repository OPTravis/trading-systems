"""
Strategy Adaptor - Dynamic strategy selection based on market regime.

Determines trading strategy based on Fear & Greed Index, BTC trend, and volatility.
"""

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StrategyAdaptor:
    """Adapts trading strategy based on market regime."""

    # Cache for adapt() results
    _cache: Optional[Dict] = None
    _cache_ts: float = 0.0
    _cache_ttl: float = 300  # 5 minutes

    # Cache for BTC daily klines (used by GARCH overlay)
    _btc_klines_cache = None
    _btc_klines_ts: float = 0.0

    # Strategy definitions
    STRATEGIES = {
        "grid": {
            "name": "Grid Trading",
            "description": "Range-bound oscillation capture",
            "enabled_by_default": False,
        },
        "dca": {
            "name": "DCA",
            "description": "Dollar-cost averaging on dips",
            "enabled_by_default": True,
        },
        "trend": {
            "name": "Trend Following",
            "description": "Momentum-based directional trades",
            "enabled_by_default": True,
        },
        "rsi_reversion": {
            "name": "RSI Mean Reversion",
            "description": "RSI oversold/overbought reversal",
            "enabled_by_default": True,
        },
        "bollinger": {
            "name": "Bollinger Bands",
            "description": "Volatility breakout/reversal",
            "enabled_by_default": True,
        },
        "vwap": {
            "name": "VWAP",
            "description": "Volume-weighted average price deviation",
            "enabled_by_default": False,  # FIX-4: Paused — 12 trades, -$10.53, 73% of losses. Needs redesign.
        },
    }
    # DCA parameter mapping by regime
    DCA_REGIME_PARAMS = {
        "EXTREME_FEAR": {
            "interval_hours": 24,
            "dip_threshold_pct": -5.0,
            "max_dca_rounds": 6,
            "order_size_pct": 8,
        },
        "FEAR": {
            "interval_hours": 18,
            "dip_threshold_pct": -4.0,
            "max_dca_rounds": 7,
            "order_size_pct": 8,
        },
        "NEUTRAL": {
            "interval_hours": 12,
            "dip_threshold_pct": -3.0,
            "max_dca_rounds": 8,
            "order_size_pct": 8,
        },
        "GREED": {
            "interval_hours": 8,
            "dip_threshold_pct": -2.0,
            "max_dca_rounds": 10,
            "order_size_pct": 8,
        },
        "EXTREME_GREED": {
            "interval_hours": 6,
            "dip_threshold_pct": -1.5,
            "max_dca_rounds": 12,
            "order_size_pct": 6,
        },
    }

    def __init__(self):
        """Initialize StrategyAdaptor."""
        pass

    @classmethod
    def _determine_regime(cls, fear_greed: int) -> str:
        """Determine market regime from Fear & Greed Index."""
        if fear_greed <= 20:
            return "EXTREME_FEAR"
        elif fear_greed <= 40:
            return "FEAR"
        elif fear_greed <= 60:
            return "NEUTRAL"
        elif fear_greed < 80:
            return "GREED"
        else:
            return "EXTREME_GREED"

    @classmethod
    def _determine_volatility(cls, btc_price_change_24h: float) -> str:
        """Determine volatility regime from BTC 24h change."""
        abs_change = abs(btc_price_change_24h)
        if abs_change < 2:
            return "LOW"
        elif abs_change < 5:
            return "MODERATE"
        elif abs_change < 10:
            return "HIGH"
        else:
            return "EXTREME"

    def adapt(
        self,
        fear_greed: int,
        btc_trend: str,
        btc_price_change_24h: float,
        funding_rate: Optional[float] = None,
        btc_adx: Optional[float] = None,
        btc_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Adapt strategy based on market conditions.

        Returns dict with:
        {
            regime: str,
            strategies: {
                [strategy_name]: {
                    enabled: bool,
                    reason: str,
                    size_multiplier: float,
                    sl_pct: float,
                    tp_levels: [{pct, size_pct}],
                    max_hold_hours: int,
                }
            },
            global: {
                score_threshold: int,
                max_position_pct: float,
                max_total_exposure_pct: float,
                cash_reserve_pct: float,
            },
            volatility_regime: str,
            changes: [str],  # human-readable list of changes made
        }
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        regime = self._determine_regime(fear_greed)
        vol_regime = self._determine_volatility(btc_price_change_24h)

        # HMM regime overlay (Phase 6)
        hmm_regime = None
        hmm_adjustments = {}
        try:
            from src.hmm_regime import HMMRegimeDetector

            detector = HMMRegimeDetector()
            cached = detector.get_cached_prediction()
            if cached and cached.get("confidence", 0) > 0.4:
                hmm_regime = cached["regime"]
                hmm_adjustments = detector.get_strategy_adjustments(hmm_regime)
        except Exception:
            logger.error("HMM regime detection failed, skipping overlay", exc_info=True)

        # CVaR risk overlay (Phase 8)
        cvar_scale = 1.0
        cvar_risk_level = None
        try:
            from src.cvar_risk import CVaRRiskManager

            cvar_mgr = CVaRRiskManager()
            # Compute from trade outcomes
            conn = cvar_mgr._db._get_conn()
            rows = conn.execute(
                "SELECT net_pnl_pct FROM trade_outcomes WHERE status = 'closed' AND net_pnl_pct IS NOT NULL ORDER BY exit_time DESC LIMIT 100"
            ).fetchall()
            if rows and len(rows) >= 10:
                returns = [r["net_pnl_pct"] for r in rows]
                cvar_mgr.compute_cvar(returns, 0.05)
                risk = cvar_mgr.compute_portfolio_risk([])
                cvar_scale = risk.get("position_scale", 1.0)
                cvar_risk_level = risk.get("risk_level")
        except Exception:
            logger.error(
                "CVaR risk overlay calculation failed, defaulting to scale=1.0",
                exc_info=True,
            )

        logger.info(
            f"StrategyAdaptor: regime={regime} btc_trend={btc_trend} "
            f"vol={vol_regime} F&G={fear_greed} BTC_24h={btc_price_change_24h:+.2f}%"
            + (f" HMM={hmm_regime}" if hmm_regime else "")
        )

        # Base settings — read from optimized params if available
        try:
            from src.param_optimizer import ParamOptimizer

            _opt = ParamOptimizer()
            _opt_params = _opt.get_current_params()
            base = {
                "score_threshold": int(_opt_params.get("score_threshold", 60)),
                "max_position_pct": 15,
                "max_total_exposure_pct": 70,
                "cash_reserve_pct": 30,
            }
        except Exception:
            logger.error(
                "ParamOptimizer load failed, using default strategy params",
                exc_info=True,
            )
            base = {
                "score_threshold": 75,
                "max_position_pct": 15,
                "max_total_exposure_pct": 70,
                "cash_reserve_pct": 30,
            }

        # Regime adjustments
        regime_map = {
            "EXTREME_FEAR": {
                "score_threshold": 85,
                "max_position_pct": 8,
                "max_total_exposure_pct": 40,
                "cash_reserve_pct": 60,
            },
            "FEAR": {
                "score_threshold": 80,
                "max_position_pct": 10,
                "max_total_exposure_pct": 50,
                "cash_reserve_pct": 50,
            },
            "NEUTRAL": dict(base),  # shallow copy to prevent mutation drift (C2 fix)
            "GREED": {
                "score_threshold": 75,
                "max_position_pct": 15,
                "max_total_exposure_pct": 70,
                "cash_reserve_pct": 30,
            },
            "EXTREME_GREED": {
                "score_threshold": 85,
                "max_position_pct": 10,
                "max_total_exposure_pct": 50,
                "cash_reserve_pct": 50,
            },
        }

        settings = regime_map.get(regime, base)

        # Audit trail for strategy adaptation decisions
        changes: list = []

        # BTC trend overlay — use actual TrendFilter score, not hardcoded 50
        effective_btc_score = btc_score if btc_score is not None else 50
        if effective_btc_score < 35:
            # BEARISH: risk-off
            changes.append(
                f"BTC trend BEARISH (score={effective_btc_score:.0f}) — risk-off adjustments"
            )
            if regime in ("EXTREME_FEAR", "FEAR"):
                settings["score_threshold"] = max(settings["score_threshold"] + 5, 75)
                settings["max_position_pct"] = max(settings["max_position_pct"] - 3, 5)
                settings["cash_reserve_pct"] = min(
                    settings["cash_reserve_pct"] + 10, 60
                )
            else:
                settings["score_threshold"] = max(settings["score_threshold"] + 10, 80)
                settings["max_position_pct"] = max(settings["max_position_pct"] - 5, 5)
                settings["cash_reserve_pct"] = min(
                    settings["cash_reserve_pct"] + 15, 70
                )
        elif effective_btc_score > 65:
            # BULLISH: can be more aggressive
            changes.append(
                f"BTC trend BULLISH (score={effective_btc_score:.0f}) — aggressive adjustments"
            )
            if regime in ("GREED", "EXTREME_GREED"):
                settings["score_threshold"] = max(settings["score_threshold"] - 5, 60)
                settings["max_position_pct"] = min(settings["max_position_pct"] + 3, 20)
                settings["cash_reserve_pct"] = max(
                    settings["cash_reserve_pct"] - 10, 20
                )
            else:
                settings["score_threshold"] = max(settings["score_threshold"] - 3, 55)
                settings["max_position_pct"] = min(settings["max_position_pct"] + 2, 18)
                settings["cash_reserve_pct"] = max(settings["cash_reserve_pct"] - 5, 25)

        # Volatility overlay
        if vol_regime == "EXTREME":
            changes.append(
                f"Volatility EXTREME ({btc_price_change_24h:+.1f}% BTC) — threshold +10, size −5, hold 24h"
            )
            settings["score_threshold"] = min(settings["score_threshold"] + 10, 90)
            settings["max_position_pct"] = max(settings["max_position_pct"] - 5, 5)
            settings["max_hold_hours"] = 24
        elif vol_regime == "HIGH":
            changes.append(
                f"Volatility HIGH ({btc_price_change_24h:+.1f}% BTC) — threshold +5, size −2, hold 48h"
            )
            settings["score_threshold"] = min(settings["score_threshold"] + 5, 85)
            settings["max_position_pct"] = max(settings["max_position_pct"] - 2, 8)
            settings["max_hold_hours"] = 48
        else:
            settings["max_hold_hours"] = 48

        # Funding rate overlay (funding_rate is in percent, e.g. 0.01 = 0.01%)
        if funding_rate is not None:
            if funding_rate > 0.05:  # > 0.05% per 8h
                changes.append(
                    f"Funding {funding_rate:+.3f}% — crowded long (risk-off: threshold +3)"
                )
                settings["score_threshold"] = min(settings["score_threshold"] + 3, 90)
                settings["cash_reserve_pct"] = min(settings["cash_reserve_pct"] + 5, 60)
            elif funding_rate < -0.05:
                changes.append(
                    f"Funding {funding_rate:+.3f}% — crowded short (opportunity: threshold −3)"
                )
                settings["score_threshold"] = max(settings["score_threshold"] - 3, 50)
                settings["cash_reserve_pct"] = max(settings["cash_reserve_pct"] - 5, 20)

        # Strategy enablement
        strategies: Dict[str, Dict[str, Any]] = {}

        for name, config in self.STRATEGIES.items():
            enabled = config["enabled_by_default"]
            reason = "default"
            size_mult = 1.0

            if regime in ("EXTREME_FEAR", "FEAR"):
                if name == "grid":
                    enabled = False
                    reason = "disabled in fear (range-bound unreliable)"
                    changes.append(f"{name}: disabled — fear regime")
                elif name == "trend":
                    enabled = False
                    reason = "disabled in fear (trend may be false)"
                    changes.append(f"{name}: disabled — fear regime")
                elif name == "dca":
                    size_mult = 1.1
                    reason = "enhanced DCA in fear (buy dips cautiously)"
                    changes.append(
                        f"{name}: size ×1.1 — fear regime DCA (P1: reduced from 1.5)"
                    )
                elif name == "rsi_reversion":
                    size_mult = 1.3
                    reason = "enhanced RSI in fear (oversold bounces)"
                    changes.append(f"{name}: size ×1.3 — fear regime RSI")
                elif name == "bollinger":
                    size_mult = 1.2
                    reason = "enhanced Bollinger in fear (volatility mean reversion)"
                    changes.append(f"{name}: size ×1.2 — fear regime BB")

            elif regime in ("GREED", "EXTREME_GREED"):
                if name == "dca":
                    enabled = False
                    reason = "disabled in greed (no dips to buy)"
                    changes.append(f"{name}: disabled — greed regime")
                elif name == "rsi_reversion":
                    enabled = False
                    reason = "disabled in greed (overbought stays overbought)"
                    changes.append(f"{name}: disabled — greed regime")
                elif name == "grid":
                    if regime == "GREED":
                        enabled = True
                        size_mult = 1.2
                        reason = "enhanced grid in greed (volatility expansion)"
                        changes.append(f"{name}: enabled ×1.2 — greed regime")
                elif name == "trend":
                    size_mult = 1.3
                    reason = "enhanced trend in greed (momentum continuation)"
                    changes.append(f"{name}: size ×1.3 — greed regime trend")

            # Volatility adjustments
            if vol_regime == "EXTREME":
                size_mult *= 0.5
                reason += " | size halved (extreme volatility)"
            elif vol_regime == "HIGH":
                size_mult *= 0.7
                reason += " | size reduced 30% (high volatility)"

            strategies[name] = {
                "enabled": enabled,
                "reason": reason,
                "size_multiplier": round(size_mult, 2),
                "sl_pct": 8.0 if vol_regime in ("HIGH", "EXTREME") else 7.0,
                "tp_levels": [
                    {"pct": 4, "size_pct": 40},
                    {"pct": 8, "size_pct": 40},
                    {"pct": 15, "size_pct": 20},
                ],
                "max_hold_hours": settings.get("max_hold_hours", 48),
            }

            # DCA regime-adaptive stop_loss
            if name == "dca":
                dca_sl_map = {
                    "EXTREME_FEAR": 12.0,
                    "FEAR": 10.0,
                    "NEUTRAL": 8.0,
                    "GREED": 7.0,
                    "EXTREME_GREED": 7.0,
                }
                strategies[name]["sl_pct"] = dca_sl_map.get(regime, 8.0)

        # Apply GARCH-based dynamic SL/TP (Phase 9 — replaces fixed SL/TP)
        # FIX-9: Use 30-day historical returns instead of single 24h data point
        try:
            from src.garch_vol import forecast_volatility, get_dynamic_sl_tp

            # Fetch last 31 daily klines for 30 returns (cached for 5 minutes)
            daily_returns = []
            try:
                _now_ts = time.time()
                if (
                    StrategyAdaptor._btc_klines_cache is not None
                    and (_now_ts - StrategyAdaptor._btc_klines_ts) < 300
                ):
                    kl_data = StrategyAdaptor._btc_klines_cache
                else:
                    import requests as _req

                    kl_resp = _req.get(
                        "https://api.binance.com/api/v3/klines",
                        params={"symbol": "BTCUSDT", "interval": "1d", "limit": 31},  # type: ignore[arg-type]
                        timeout=5,
                    )
                    kl_data = kl_resp.json()
                    StrategyAdaptor._btc_klines_cache = kl_data
                    StrategyAdaptor._btc_klines_ts = _now_ts
                if len(kl_data) >= 2:
                    closes = [float(k[4]) for k in kl_data]
                    daily_returns = [
                        (closes[i] - closes[i - 1]) / closes[i - 1]
                        for i in range(1, len(closes))
                    ]
            except Exception:
                logger.warning(
                    "GARCH: failed to fetch historical klines, falling back to 24h estimate"
                )
                daily_returns = (
                    [btc_price_change_24h / 100] if btc_price_change_24h else [0.02]
                )
            if not daily_returns:
                daily_returns = (
                    [btc_price_change_24h / 100] if btc_price_change_24h else [0.02]
                )
            vol_result = forecast_volatility(daily_returns)
            if vol_result:
                dynamic = get_dynamic_sl_tp("BTC", 100.0, vol_result["forecast_vol"])
                for name, cfg in strategies.items():
                    cfg["sl_pct"] = abs(dynamic["sl_pct"]) * 100
                    base_tp = dynamic["tp_pct"] * 100
                    cfg["tp_levels"][0]["pct"] = base_tp
                    # Scale TP2/TP3 relative to TP1 to avoid inverted order
                    if len(cfg["tp_levels"]) > 1:
                        cfg["tp_levels"][1]["pct"] = round(base_tp * 1.5, 2)
                    if len(cfg["tp_levels"]) > 2:
                        cfg["tp_levels"][2]["pct"] = round(base_tp * 2.0, 2)
                changes.append(
                    f"GARCH {vol_result['vol_regime']}({len(daily_returns)}d): SL={abs(dynamic['sl_pct'])*100:.1f}%, TP={dynamic['tp_pct']*100:.1f}%"
                )
        except Exception as e:
            logger.debug(f"GARCH adjustment unavailable: {e}")

        result: Dict[str, Any] = {
            "regime": regime,
            "hmm_regime": hmm_regime,
            "strategies": strategies,
            "dca_params": self.DCA_REGIME_PARAMS.get(
                regime, self.DCA_REGIME_PARAMS["NEUTRAL"]
            ),
            "global": {
                "score_threshold": settings["score_threshold"],
                "max_position_pct": settings["max_position_pct"],
                "max_total_exposure_pct": settings["max_total_exposure_pct"],
                "cash_reserve_pct": settings["cash_reserve_pct"],
            },
            "changes": changes,
        }

        # Apply HMM adjustments if available and confident
        if hmm_regime and hmm_adjustments:
            # Adjust score threshold
            result["global"]["score_threshold"] += hmm_adjustments.get(
                "score_threshold_adj", 0
            )
            if hmm_adjustments.get("score_threshold_adj", 0) != 0:
                changes.append(
                    f"HMM {hmm_regime}: threshold {hmm_adjustments['score_threshold_adj']:+d}"
                )

            # Adjust strategy enablement based on HMM preferred/avoid
            for name, cfg in strategies.items():
                if name in hmm_adjustments.get("avoid_strategies", []):
                    cfg["enabled"] = False
                    cfg["reason"] = f"disabled by HMM {hmm_regime}"
                    changes.append(f"{name}: disabled — HMM {hmm_regime}")
                elif name in hmm_adjustments.get("preferred_strategies", []):
                    cfg["size_multiplier"] = round(
                        cfg["size_multiplier"]
                        * hmm_adjustments.get("position_scale", 1.0),
                        2,
                    )
                    changes.append(
                        f"{name}: HMM preferred, size ×{hmm_adjustments['position_scale']}"
                    )

        # Apply CVaR risk scaling (Phase 8)
        if cvar_scale != 1.0 and cvar_risk_level:
            for name, cfg in strategies.items():
                cfg["size_multiplier"] = round(cfg["size_multiplier"] * cvar_scale, 2)
            changes.append(f"CVaR {cvar_risk_level}: all sizes ×{cvar_scale}")

        # Apply Contextual Bandit sizing (Phase 9 — replaces PPO/SAC)
        try:
            from src.contextual_bandit import get_contextual_bandit

            bandit = get_contextual_bandit()
            # Build context for bandit
            portfolio_heat = "cold"
            try:
                from src.state_db import get_state_db

                ps = get_state_db()
                positions = ps.portfolio_get_all()
                total_val = sum(
                    p.get("quantity", 0) * p.get("entry_price", 0)
                    for p in positions.values()
                )
                usdt = ps.portfolio_get_cash_balance()
                if total_val + usdt > 0:
                    ratio = total_val / (total_val + usdt)
                    portfolio_heat = (
                        "hot" if ratio > 0.7 else ("warm" if ratio > 0.4 else "cold")
                    )
            except Exception:
                logger.error(
                    "Failed to compute portfolio heat for bandit context", exc_info=True
                )
            bandit_ctx = {
                "hmm_regime": hmm_regime or "sideways",
                "fear_greed": fear_greed,
                "btc_trend": btc_trend,
                "portfolio_heat": portfolio_heat,
            }
            bandit_mult = bandit.recommend_size(bandit_ctx)
            if bandit_mult != 0.8:  # only log if not default
                for name, cfg in strategies.items():
                    cfg["size_multiplier"] = round(
                        cfg["size_multiplier"] * bandit_mult, 2
                    )
                changes.append(f"Bandit: all sizes ×{bandit_mult}")
        except Exception as e:
            logger.debug(f"Contextual bandit unavailable: {e}")

        # Floor: never reduce below 20% of base after ALL overlays (including bandit)
        for name, cfg in strategies.items():
            size_mult = cfg["size_multiplier"]
            if size_mult < 0.20:
                cfg["size_multiplier"] = 0.20
                changes.append(
                    f"{name}: size floor applied (was {size_mult:.2f}, now 0.20)"
                )

        self._cache = result
        self._cache_ts = now
        return result

    def get_enabled_strategies(
        self, fear_greed: int, btc_trend: str, btc_price_change_24h: float
    ) -> List[str]:
        """Get list of enabled strategy names for current conditions."""
        adapted = self.adapt(fear_greed, btc_trend, btc_price_change_24h)
        return [
            name for name, config in adapted["strategies"].items() if config["enabled"]
        ]

    def should_trade(
        self, fear_greed: int, btc_trend: str, btc_price_change_24h: float
    ) -> bool:
        """Check if trading should be enabled for current conditions."""
        self.adapt(fear_greed, btc_trend, btc_price_change_24h)
        enabled = self.get_enabled_strategies(
            fear_greed, btc_trend, btc_price_change_24h
        )
        return len(enabled) > 0
