"""WO-1003-6: robust loss functions for systematic parameter optimisation.

freqtrade-style loss functions (SharpeHyperOptLossDaily /
MaxDrawDownHyperOptLoss analogues). Every function maps a metrics dict to a
float where LOWER IS BETTER, so any of them can plug into a single
minimisation loop (Bayesian / TPE study or the existing grid search sort).

Expected metrics keys (all optional, defensive defaults keep the functions
pure and side-effect free — they never touch IO):
    sharpe             : backtest Sharpe ratio
    total_return_pct   : cumulative return %
    max_drawdown_pct   : max drawdown % (positive number)
    n_trades           : trade count
    robustness_pct     : walk-forward OOS robustness (0-100, optional)

Trade-count penalty (freqtrade HyperOptLoss philosophy): a configuration
that barely trades can show a fantastic Sharpe on 2 lucky trades — it must
not outrank a slightly-worse-Sharpe configuration that trades actively.
"""
from typing import Callable, Dict

MIN_TRADES_DEFAULT = 5
_TRADE_PENALTY_PER_MISSING = 0.5   # per trade below the minimum
_NO_TRADES_LOSS = 1e6              # effectively reject zero-trade configs


def _trades_penalty(n_trades: int, min_trades: int = MIN_TRADES_DEFAULT) -> float:
    if n_trades <= 0:
        return _NO_TRADES_LOSS
    if n_trades < min_trades:
        return (min_trades - n_trades) * _TRADE_PENALTY_PER_MISSING
    return 0.0


def sharpe_daily_loss(metrics: Dict) -> float:
    """Maximise Sharpe, penalise under-trading.

    Analogue of freqtrade SharpeHyperOptLossDaily: daily-return Sharpe with a
    trade-count penalty so the optimiser cannot converge on never-trading
    parameter sets.
    """
    sharpe = float(metrics.get("sharpe", 0.0) or 0.0)
    penalty = _trades_penalty(int(metrics.get("n_trades", 0) or 0))
    return -sharpe + penalty


def max_drawdown_loss(metrics: Dict) -> float:
    """Risk-weighted return — analogue of MaxDrawDownHyperOptLoss.

    loss = -return_pct / (1 + max_dd_pct / 10)

    Higher return lowers the loss; deeper drawdown inflates the denominator
    and thus the loss, even at equal return.
    """
    ret = float(metrics.get("total_return_pct", 0.0) or 0.0)
    dd = max(float(metrics.get("max_drawdown_pct", 0.0) or 0.0), 0.0)
    penalty = _trades_penalty(int(metrics.get("n_trades", 0) or 0))
    return -ret / (1.0 + dd / 10.0) + penalty


def calmar_loss(metrics: Dict) -> float:
    """Calmar-style: return over drawdown with a drawdown floor.

    loss = -return_pct / max(max_dd_pct, 5.0)  (5% floor keeps tiny-DD
    configs from exploding the ratio on noise).
    """
    ret = float(metrics.get("total_return_pct", 0.0) or 0.0)
    dd = max(float(metrics.get("max_drawdown_pct", 0.0) or 0.0), 5.0)
    penalty = _trades_penalty(int(metrics.get("n_trades", 0) or 0))
    return -ret / dd + penalty


def robust_sharpe_loss(metrics: Dict) -> float:
    """Sharpe discounted by walk-forward OOS robustness.

    loss = -sharpe * (robustness_pct / 100) + trades penalty

    Prefers parameters whose edge holds out-of-sample: a Sharpe 1.5 config
    that only survives 40% of OOS splits (0.60) ranks below a Sharpe 1.0
    config surviving 80% (0.80).
    """
    sharpe = float(metrics.get("sharpe", 0.0) or 0.0)
    robust = float(metrics.get("robustness_pct", 100.0) or 0.0) / 100.0
    penalty = _trades_penalty(int(metrics.get("n_trades", 0) or 0))
    return -sharpe * robust + penalty


LOSS_FUNCTIONS: Dict[str, Callable[[Dict], float]] = {
    "sharpe_daily": sharpe_daily_loss,
    "max_drawdown": max_drawdown_loss,
    "calmar": calmar_loss,
    "robust_sharpe": robust_sharpe_loss,
}

DEFAULT_LOSS = "sharpe_daily"


def get_loss(name: str) -> Callable[[Dict], float]:
    """Look up a loss function by name (raises on unknown names)."""
    try:
        return LOSS_FUNCTIONS[name]
    except KeyError:
        raise ValueError(
            f"Unknown loss function {name!r}; available: "
            f"{sorted(LOSS_FUNCTIONS)}"
        ) from None
