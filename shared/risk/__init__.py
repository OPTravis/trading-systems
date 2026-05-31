"""Risk management: Kelly sizing, drawdown breakers, circuit breakers, CVaR."""

from .risk_manager import TrendFilter, TrailingStop, ConsecutiveLossGuard
from .kelly_sizer import KellyPositionSizer as KellySizer
from .drawdown_breaker import DrawdownBreaker
from .correlation_risk import CorrelationRiskManager
from .cvar_risk import CVaRRiskManager
from .daily_loss_breaker import DailyLossBreaker
from .circuit_breaker import CircuitBreaker

__all__ = [
    "TrendFilter", "TrailingStop", "ConsecutiveLossGuard",
    "KellySizer", "DrawdownBreaker", "CorrelationRiskManager",
    "CVaRRiskManager", "DailyLossBreaker", "CircuitBreaker",
]
