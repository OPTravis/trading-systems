"""
WO-1001-3: lookahead / recursive (repaint) bias analysis for strategies.

Two independent detectors, both pure and deterministic (synthetic klines —
no network, no DB), designed as a pre-launch CI gate:

1. detect_signal_lookahead — the causal contract. BaseStrategy.analyze
   promises "idx provided → use klines[:idx]". A correct implementation
   answers bar j identically whether the future is (a) logically hidden
   via idx=j or (b) physically absent (klines truncated to [:j]). Any
   mismatch means the decision at bar j depends on data after bar j —
   lookahead bias, backtest illusions, live divergence.

2. detect_indicator_repaint — the stability contract. Feeding extra
   future bars beyond idx must NOT change the bar-j answer (idx pins the
   view). Strategies that normalize/recompute over the whole series but
   index at j repaint their own history — recursive bias.

Gate wiring: tests/test_wo1001_bias_gate.py enumerates EVERY strategy
class under src/strategies/ (module scan, not a hardcoded list), so a new
strategy file cannot pass CI without passing the gate. New parameter sets
should additionally be checked via scripts/bias_analysis.py --params.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


# ── deterministic synthetic klines ────────────────────────────────────

def make_synthetic_klines(n: int = 300, seed: int = 42, start: float = 100.0) -> List[Dict[str, float]]:
    """Deterministic multi-regime OHLCV series (trend / chop / spike)."""
    import random
    rng = random.Random(seed)
    klines: List[Dict[str, float]] = []
    px = start
    ts = 1_700_000_000_000
    for i in range(n):
        # regime: slow trend up, mid chop, late trend down
        if i < n // 3:
            drift = 0.004
        elif i < 2 * n // 3:
            drift = 0.0
        else:
            drift = -0.004
        shock = rng.gauss(0, 0.008)
        if i % 97 == 0:                       # occasional volume spike
            shock += rng.choice((-0.03, 0.03))
        o = px
        c = px * (1 + drift + shock)
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        v = 1000 + abs(rng.gauss(500, 250)) + (2000 if i % 97 == 0 else 0)
        klines.append({
            "open_time": ts + i * 3_600_000, "open": round(o, 6),
            "high": round(h, 6), "low": round(l, 6), "close": round(c, 6),
            "volume": round(v, 2),
        })
        px = c
    return klines


# ── reports ───────────────────────────────────────────────────────────

@dataclass
class BiasReport:
    strategy: str
    detector: str
    ok: bool
    bars_checked: int = 0
    mismatches: int = 0
    examples: List[str] = field(default_factory=list)

    def summary(self) -> str:
        flag = "PASS" if self.ok else "FAIL"
        return (f"[{flag}] {self.detector} {self.strategy}: "
                f"{self.mismatches}/{self.bars_checked} mismatches")


# ── detector 1: lookahead ────────────────────────────────────────────

def detect_signal_lookahead(
    strategy: Any, symbol: str = "TESTUSDT",
    klines: Optional[List[Dict]] = None,
    sample_step: int = 7, max_bars: int = 220,
) -> BiasReport:
    """Logical truncation (idx=j) must equal physical truncation ([:j])."""
    klines = klines if klines is not None else make_synthetic_klines()
    rep = BiasReport(strategy=strategy.name, detector="lookahead", ok=True)
    checked = 0
    for j in range(30, min(len(klines), max_bars), sample_step):
        try:
            a = strategy.analyze(symbol, klines, None, idx=j)   # logical cut
            b = strategy.analyze(symbol, klines[:j], None)      # physical cut
        except Exception as exc:
            rep.ok = False
            rep.mismatches += 1
            rep.examples.append(f"bar {j}: exception {exc!r}")
            continue
        checked += 1
        if (a.signal is not b.signal) or (a.reason != b.reason):
            rep.mismatches += 1
            rep.examples.append(
                f"bar {j}: idx={a.signal.value}/{a.reason!r} "
                f"vs trunc={b.signal.value}/{b.reason!r}")
    rep.bars_checked = checked
    rep.ok = rep.mismatches == 0
    return rep


# ── detector 2: recursive / repaint ──────────────────────────────────

def detect_indicator_repaint(
    strategy: Any, symbol: str = "TESTUSDT",
    klines: Optional[List[Dict]] = None,
    tail_bars: int = 40, sample_step: int = 7,
    conf_tol: float = 1e-9, meta_tol: float = 1e-6,
) -> BiasReport:
    """Appending future bars must not change the idx-pinned bar-j answer."""
    klines = klines if klines is not None else make_synthetic_klines()
    rep = BiasReport(strategy=strategy.name, detector="repaint", ok=True)
    if len(klines) <= tail_bars + 50:
        rep.ok = False
        rep.examples.append("not enough klines")
        return rep
    checked = 0
    for j in range(30, len(klines) - tail_bars, sample_step):
        try:
            a = strategy.analyze(symbol, klines, None, idx=j)
            b = strategy.analyze(symbol, klines[: j + tail_bars], None, idx=j)
        except Exception as exc:
            rep.ok = False
            rep.mismatches += 1
            rep.examples.append(f"bar {j}: exception {exc!r}")
            continue
        checked += 1
        if a.signal is not b.signal:
            rep.mismatches += 1
            rep.examples.append(f"bar {j}: signal {a.signal.value} -> {b.signal.value}")
            continue
        if abs((a.confidence or 0) - (b.confidence or 0)) > conf_tol:
            rep.mismatches += 1
            rep.examples.append(
                f"bar {j}: confidence drift {a.confidence} -> {b.confidence}")
            continue
        for k in set(a.metadata) & set(b.metadata):
            va, vb = a.metadata[k], b.metadata[k]
            if isinstance(va, bool) or isinstance(va, str):
                if va != vb:
                    rep.mismatches += 1
                    rep.examples.append(f"bar {j}: meta[{k}] {va} -> {vb}")
                    break
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                if abs(va - vb) > max(meta_tol, abs(va) * meta_tol):
                    rep.mismatches += 1
                    rep.examples.append(f"bar {j}: meta[{k}] {va} -> {vb}")
                    break
    rep.bars_checked = checked
    rep.ok = rep.mismatches == 0
    return rep


# ── strategy discovery (CI gate enumerates the package) ──────────────

def discover_strategies(pkg_name: str = "src.strategies") -> List[Type]:
    """Every BaseStrategy subclass in the strategies package (incl. new files)."""
    import src.strategies as pkg
    from src.strategies.base import BaseStrategy
    found: Dict[str, Type] = {}
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_"):
            continue
        m = importlib.import_module(f"{pkg_name}.{mod.name}")
        for name, obj in inspect.getmembers(m, inspect.isclass):
            if (issubclass(obj, BaseStrategy) and obj is not BaseStrategy
                    and obj.__module__ == m.__name__ and name not in found):
                found[name] = obj
    return list(found.values())


def run_bias_gate(
    strategy_cls: Optional[Type] = None,
    params: Optional[Dict[str, Any]] = None,
    klines: Optional[List[Dict]] = None,
) -> List[BiasReport]:
    """Run both detectors for one strategy class (None → all discovered)."""
    classes = [strategy_cls] if strategy_cls else discover_strategies()
    reports: List[BiasReport] = []
    for cls in classes:
        try:
            inst = cls(params or {})
        except Exception as exc:
            reports.append(BiasReport(
                strategy=cls.__name__, detector="construct", ok=False,
                examples=[f"cannot construct with empty params: {exc!r}"]))
            continue
        reports.append(detect_signal_lookahead(inst, klines=klines))
        reports.append(detect_indicator_repaint(inst, klines=klines))
    return reports
