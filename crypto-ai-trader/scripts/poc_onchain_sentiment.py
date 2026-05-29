#!/usr/bin/env python3
"""
PoC: On-Chain + Sentiment scoring for MarketScanner.

Tests:
1. DeFiLlama API → chain-level TVL change → 0-100 score
2. Alternative.me F&G → market emotion → 0-100 score
3. Integration stub showing how to wire into _calculate_weighted_score()

Run: cd ~/crypto-ai-trader && .venv/bin/python scripts/poc_onchain_sentiment.py
"""

import json
import sys
from typing import Dict, Optional

import requests

# ---------------------------------------------------------------------------
# DeFiLlama — On-Chain Factor
# ---------------------------------------------------------------------------

class DeFiLlamaOnChain:
    """Fetch DeFiLlama chain TVL data and produce a 0-100 on-chain health score."""

    BASE = "https://api.llama.fi"
    MAJOR_CHAINS = ["Ethereum", "BSC", "Arbitrum", "Base", "Solana", "Avalanche", "Polygon"]

    def _get_chain_tvl_change(self, chain: str) -> Optional[float]:
        """Fetch 1-day TVL change % for a single chain from historical data."""
        try:
            resp = requests.get(
                f"{self.BASE}/v2/historicalChainTvl/{chain}",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if len(data) < 2:
                return None
            tvl_now = data[-1]["tvl"]
            tvl_yesterday = data[-2]["tvl"]
            if tvl_yesterday <= 0:
                return None
            return (tvl_now - tvl_yesterday) / tvl_yesterday * 100
        except Exception:
            return None

    def get_chain_tvl_changes(self) -> Dict[str, float]:
        """Return {chain_name: tvl_change_24h_pct} for major chains."""
        changes = {}
        for chain in self.MAJOR_CHAINS:
            chg = self._get_chain_tvl_change(chain)
            if chg is not None:
                changes[chain] = chg
        return changes

    def get_onchain_score(self) -> float:
        """Compute a 0-100 on-chain health score.

        Logic:
        - Aggregate TVL change across major chains (weighted by TVL)
        - Positive aggregate change → bullish on-chain (50-100)
        - Negative aggregate change → bearish on-chain (0-50)
        - Extreme changes (>±10%) capped at edges
        """
        changes = self.get_chain_tvl_changes()
        if not changes:
            return 50.0  # neutral on failure

        # Simple average across available chains
        avg_change = sum(changes.values()) / len(changes)

        # Map -10%..+10% → 0..100
        score = 50.0 + (avg_change * 5.0)
        return max(0.0, min(100.0, score))


# ---------------------------------------------------------------------------
# Alternative.me Fear & Greed — Sentiment Factor
# ---------------------------------------------------------------------------

class FearGreedSentiment:
    """Fetch F&G index and produce a 0-100 sentiment score."""

    URL = "https://api.alternative.me/fng/"

    def get_score(self) -> float:
        """Return 0-100 sentiment score.

        F&G index is already 0-100:
        - 0   = Extreme Fear (bearish, potential bottom)
        - 50  = Neutral
        - 100 = Extreme Greed (bullish, potential top)

        For LONG-biased spot trading we invert slightly:
        - Extreme fear (0-20)  → score 80-100 (contrarian buy opportunity)
        - Fear (21-40)         → score 60-80
        - Neutral (41-60)      → score 40-60
        - Greed (61-80)        → score 20-40
        - Extreme greed (81-100) → score 0-20 (contrarian caution)
        """
        try:
            resp = requests.get(self.URL, params={"limit": 1, "format": "json"}, timeout=10)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data:
                return 50.0
            fng = int(data[0]["value"])

            # Invert for contrarian LONG bias
            if fng <= 20:
                return 90.0 + (20 - fng) * 0.5   # 90-100
            elif fng <= 40:
                return 70.0 + (40 - fng) * 1.0   # 70-90
            elif fng <= 60:
                return 50.0 + (60 - fng) * 1.0   # 50-70
            elif fng <= 80:
                return 30.0 + (80 - fng) * 1.0   # 30-50
            else:
                return 10.0 + (100 - fng) * 0.5  # 10-30
        except Exception as e:
            print(f"[ERROR] F&G fetch failed: {e}", file=sys.stderr)
            return 50.0


# ---------------------------------------------------------------------------
# Integration Demo
# ---------------------------------------------------------------------------

def demo_integration():
    """Show how scores fit into scanner's _calculate_weighted_score()."""
    onchain = DeFiLlamaOnChain()
    sentiment = FearGreedSentiment()

    print("=" * 60)
    print("PoC: On-Chain + Sentiment Scoring")
    print("=" * 60)

    # 1. On-chain
    print("\n[1] DeFiLlama On-Chain")
    changes = onchain.get_chain_tvl_changes()
    print(f"    Top chains TVL change (24h):")
    for name, chg in sorted(changes.items(), key=lambda x: abs(x[1]), reverse=True)[:8]:
        print(f"      {name:15s}: {chg:+.2f}%")
    onchain_score = onchain.get_onchain_score()
    print(f"    → On-Chain Score: {onchain_score:.1f}/100")

    # 2. Sentiment
    print("\n[2] Fear & Greed Sentiment")
    sentiment_score = sentiment.get_score()
    print(f"    → Sentiment Score: {sentiment_score:.1f}/100")

    # 3. Proposed weight integration
    print("\n[3] Proposed Weight Integration")
    print("""
    Current _calculate_weighted_score() weights (sum=100%):
      Technical (1h)      20%
      Multi-TF Trend      20%
      Volume/Momentum     10%
      Sentiment (Funding) 10%  ← RENAME to "Funding/OI"
      Price Action        10%
      OBV Divergence      10%
      Consolidation       10%
      BB Squeeze           5%
      RSI Divergence       5%

    PROPOSED new weights (sum=100%):
      Technical (1h)      15%  (-5)
      Multi-TF Trend      15%  (-5)
      Volume/Momentum     10%  (keep)
      Funding/OI           8%  (-2)
      Price Action         8%  (-2)
      OBV Divergence       8%  (-2)
      Consolidation        8%  (-2)
      BB Squeeze           4%  (-1)
      RSI Divergence       4%  (-1)
      On-Chain             10%  (+NEW)
      Market Sentiment     10%  (+NEW)
    """)

    # 4. Simulate combined score for a hypothetical coin
    print("[4] Simulated Combined Score (example coin)")
    f1 = 75.0   # technical
    f2 = 80.0   # trend
    f3 = 60.0   # volume
    f4 = 55.0   # funding/oi
    f5 = 70.0   # price action
    f7 = 65.0   # obv
    f8 = 50.0   # consolidation
    f9 = 40.0   # bb squeeze
    f10 = 45.0  # rsi div
    f_onchain = onchain_score
    f_sentiment = sentiment_score

    new_score = (
        0.15 * f1 +
        0.15 * f2 +
        0.10 * f3 +
        0.08 * f4 +
        0.08 * f5 +
        0.08 * f7 +
        0.08 * f8 +
        0.04 * f9 +
        0.04 * f10 +
        0.10 * f_onchain +
        0.10 * f_sentiment
    )
    print(f"    Technical={f1:.0f} Trend={f2:.0f} Vol={f3:.0f} "
          f"Funding={f4:.0f} PA={f5:.0f} OBV={f7:.0f} "
          f"Consol={f8:.0f} BB={f9:.0f} RSI={f10:.0f}")
    print(f"    On-Chain={f_onchain:.1f} Sentiment={f_sentiment:.1f}")
    print(f"    → COMBINED SCORE: {new_score:.1f}/100")
    print("=" * 60)


if __name__ == "__main__":
    demo_integration()
