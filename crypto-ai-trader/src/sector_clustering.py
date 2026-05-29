"""
Sector Clustering Analysis - Weekly correlation-based sector validation.

Run this weekly to:
1. Compute 30-day price correlations for all tracked symbols
2. Detect if AI_INFRA and AI_AGENT should be split/merged
3. Suggest reclassifications for misclassified symbols
4. Generate report for review

Usage:
    python -m src.sector_clustering
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Set

import numpy as np
import requests

from src.sector_classifier import SectorClassifier, BASE_SECTORS
from src.utils import get_project_root

logger = logging.getLogger(__name__)

# Output paths
_REPORT_FILE = get_project_root() / "data" / "sector_clustering_report.json"


def fetch_price_history(symbol: str, days: int = 30) -> List[float]:
    """Fetch daily closing prices from Binance."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit={days}"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if isinstance(data, list) and len(data) >= 15:
            return [float(c[4]) for c in data]
    except Exception as e:
        logger.warning(f"Failed to fetch {symbol}: {e}")
    return []


def compute_log_return_correlation(prices_a: List[float], prices_b: List[float]) -> float:
    """Compute Pearson correlation of log returns."""
    if len(prices_a) != len(prices_b) or len(prices_a) < 15:
        return 0.0
    returns_a = np.diff(np.log(np.array(prices_a)))
    returns_b = np.diff(np.log(np.array(prices_b)))
    if len(returns_a) < 2 or np.std(returns_a) == 0 or np.std(returns_b) == 0:
        return 0.0
    corr = np.corrcoef(returns_a, returns_b)[0, 1]
    return float(corr) if not np.isnan(corr) else 0.0


def run_clustering_analysis(
    symbols: List[str],
    classifier: SectorClassifier = None,
    min_correlation: float = 0.5,
) -> Dict:
    """Run full clustering analysis on a list of symbols.

    Returns report dict with recommendations.
    """
    if classifier is None:
        classifier = SectorClassifier()

    logger.info(f"Clustering analysis: {len(symbols)} symbols")

    # Fetch price histories
    histories = {}
    for sym in symbols:
        hist = fetch_price_history(sym)
        if hist:
            histories[sym] = hist
        time.sleep(0.2)

    valid_symbols = list(histories.keys())
    logger.info(f"Valid price data: {len(valid_symbols)} symbols")

    if len(valid_symbols) < 3:
        return {"error": "Insufficient data"}

    # Compute correlation matrix
    corr_matrix = {}
    for sym_a in valid_symbols:
        corr_matrix[sym_a] = {}
        for sym_b in valid_symbols:
            if sym_a == sym_b:
                corr_matrix[sym_a][sym_b] = 1.0
            else:
                corr_matrix[sym_a][sym_b] = compute_log_return_correlation(
                    histories[sym_a], histories[sym_b]
                )

    # Analyze AI sector split
    ai_symbols = [s for s in valid_symbols
                  if classifier.classify_position(s + "USDT") in ("AI", "AI_INFRA", "AI_AGENT")]

    ai_infra = [s for s in ai_symbols if s in BASE_SECTORS.get("AI_INFRA", [])]
    ai_agent = [s for s in ai_symbols if s in BASE_SECTORS.get("AI_AGENT", [])]

    infra_agent_corrs = []
    for i in ai_infra:
        for a in ai_agent:
            if i in corr_matrix and a in corr_matrix[i]:
                infra_agent_corrs.append(abs(corr_matrix[i][a]))

    avg_infra_agent_corr = np.mean(infra_agent_corrs) if infra_agent_corrs else 1.0

    # Within-group correlations
    infra_corrs = []
    for i in range(len(ai_infra)):
        for j in range(i + 1, len(ai_infra)):
            infra_corrs.append(abs(corr_matrix[ai_infra[i]][ai_infra[j]]))

    agent_corrs = []
    for i in range(len(ai_agent)):
        for j in range(i + 1, len(ai_agent)):
            agent_corrs.append(abs(corr_matrix[ai_agent[i]][ai_agent[j]]))

    avg_infra_corr = np.mean(infra_corrs) if infra_corrs else 0.0
    avg_agent_corr = np.mean(agent_corrs) if agent_corrs else 0.0

    # Recommendations
    recommendations = []

    if avg_infra_agent_corr < 0.5 and avg_infra_corr > 0.6 and avg_agent_corr > 0.6:
        recommendations.append({
            "type": "AI_SPLIT",
            "message": f"AI_INFRA and AI_AGENT are weakly correlated ({avg_infra_agent_corr:.2f}). Keep separate sectors.",
            "infra_agent_corr": round(avg_infra_agent_corr, 3),
            "infra_internal_corr": round(avg_infra_corr, 3),
            "agent_internal_corr": round(avg_agent_corr, 3),
        })
    elif avg_infra_agent_corr > 0.7:
        recommendations.append({
            "type": "AI_MERGE",
            "message": f"AI_INFRA and AI_AGENT are highly correlated ({avg_infra_agent_corr:.2f}). Consider merging to single AI sector.",
            "infra_agent_corr": round(avg_infra_agent_corr, 3),
        })

    # Detect misclassified symbols (high correlation with different sector)
    misclassified = []
    for sym in valid_symbols:
        current_sector = classifier.classify_position(sym + "USDT")
        if current_sector == "OTHER":
            continue

        # Find which sector it correlates most with
        sector_corrs = {}
        for sector_name, sector_symbols in BASE_SECTORS.items():
            if sector_name == "AI":
                continue
            sector_syms = [s for s in sector_symbols if s in corr_matrix and s != sym]
            if sector_syms:
                avg_corr = np.mean([abs(corr_matrix[sym].get(s, 0)) for s in sector_syms])
                sector_corrs[sector_name] = avg_corr

        if sector_corrs:
            best_sector = max(sector_corrs, key=sector_corrs.get)
            best_corr = sector_corrs[best_sector]
            if best_sector != current_sector and best_corr > 0.7:
                misclassified.append({
                    "symbol": sym,
                    "current_sector": current_sector,
                    "suggested_sector": best_sector,
                    "correlation": round(best_corr, 3),
                })

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbols_analyzed": len(valid_symbols),
        "ai_sector": {
            "symbols": ai_symbols,
            "infra": ai_infra,
            "agent": ai_agent,
            "infra_agent_correlation": round(avg_infra_agent_corr, 3),
            "infra_internal_correlation": round(avg_infra_corr, 3),
            "agent_internal_correlation": round(avg_agent_corr, 3),
        },
        "recommendations": recommendations,
        "misclassified": misclassified,
        "correlation_matrix": {k: {k2: round(v2, 3) for k2, v2 in v.items()}
                               for k, v in corr_matrix.items()},
    }

    # Save report
    try:
        _REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info(f"Clustering report saved to {_REPORT_FILE}")
    except Exception as e:
        logger.error(f"Failed to save clustering report: {e}")

    return report


def main():
    """CLI entry point for weekly clustering analysis."""
    import argparse

    parser = argparse.ArgumentParser(description="Sector clustering analysis")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to analyze")
    parser.add_argument("--min-corr", type=float, default=0.5, help="Minimum correlation threshold")
    args = parser.parse_args()

    if args.symbols:
        symbols = args.symbols
    else:
        # Default: analyze all known sectors + top 50 USDT pairs
        symbols = set()
        for sector_syms in BASE_SECTORS.values():
            symbols.update(sector_syms)
        symbols = sorted(symbols)

    classifier = SectorClassifier()
    report = run_clustering_analysis(symbols, classifier, args.min_corr)

    # Print summary
    print(f"\n=== Sector Clustering Report ({report['timestamp']}) ===")
    print(f"Symbols analyzed: {report['symbols_analyzed']}")

    if report.get("recommendations"):
        print("\nRecommendations:")
        for rec in report["recommendations"]:
            print(f"  [{rec['type']}] {rec['message']}")

    ai = report.get("ai_sector", {})
    print(f"\nAI Sector Analysis:")
    print(f"  INFRA symbols: {ai.get('infra', [])}")
    print(f"  AGENT symbols: {ai.get('agent', [])}")
    print(f"  Cross-correlation: {ai.get('infra_agent_correlation', 'N/A')}")

    if report.get("misclassified"):
        print(f"\nPotentially misclassified:")
        for m in report["misclassified"]:
            print(f"  {m['symbol']}: {m['current_sector']} -> {m['suggested_sector']} (corr={m['correlation']})")

    print(f"\nFull report: {_REPORT_FILE}")


if __name__ == "__main__":
    main()
