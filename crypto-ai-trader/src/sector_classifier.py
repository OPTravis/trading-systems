"""
Smart Sector Classifier - Auto-classify crypto symbols into sectors.

Hybrid approach:
1. LLM-based classification for new coins (one-time, cached)
2. Correlation-based clustering for sector validation (weekly)

Sectors: AI, AI_INFRA, AI_AGENT, CORE, MEME, L2DEFI, RWA, OTHER
"""

import json
import logging
import random as _random
import time
from typing import Dict, List, Optional, Set

# Python 3.11.15 removed random.randbits; numpy expects it
if not hasattr(_random, "randbits"):
    _random.randbits = _random.getrandbits  # type: ignore[attr-defined]

import requests

from src.llm_client import get_llm_client
from src.utils import get_project_root

logger = logging.getLogger(__name__)

# Sector definitions (base classification)
BASE_SECTORS: Dict[str, List[str]] = {
    "AI_INFRA": [
        "RNDR",
        "FET",
        "AGIX",
        "TAO",
        "GRT",
        "OCEAN",
    ],
    "AI_AGENT": [
        "PAAL",
        "AI16Z",
        "VIRTUAL",
        "AIXBT",
        "GRIFT",
        "VANA",
        "IO",
    ],
    "CORE": [
        "BTC",
        "ETH",
        "SOL",
        "BNB",
        "AVAX",
        "ADA",
        "DOT",
        "MATIC",
        "ATOM",
        "NEAR",
    ],
    "MEME": [
        "DOGE",
        "SHIB",
        "PEPE",
        "WIF",
        "BONK",
        "FLOKI",
        "BRETT",
        "POPCAT",
    ],
    "L2DEFI": [
        "ARB",
        "OP",
        "STRK",
        "ZK",
        "INJ",
        "SUI",
        "SEI",
        "APT",
        "AAVE",
        "UNI",
        "CRV",
        "LDO",
    ],
    "RWA": ["ON", "ENA"],
}

# Merge AI sub-sectors for backward compatibility
AI_SYMBOLS = BASE_SECTORS["AI_INFRA"] + BASE_SECTORS["AI_AGENT"]
BASE_SECTORS["AI"] = AI_SYMBOLS

# File paths
_CLASSIFICATION_FILE = get_project_root() / "data" / "sector_classifications.json"
_CLUSTERING_FILE = get_project_root() / "data" / "sector_clusters.json"


class SectorClassifier:
    """Auto-classify symbols into sectors using LLM + correlation clustering."""

    # Per-sector exposure limits (P1: AI cap reduced from 80 → 50)
    SECTOR_LIMITS: Dict[str, int] = {
        "AI": 50,
        "AI_INFRA": 50,
        "AI_AGENT": 50,
        "CORE": 30,
        "MEME": 30,
        "L2DEFI": 30,
        "RWA": 30,
        "OTHER": 30,
    }

    def __init__(self, llm_client=None):
        self.llm_client = llm_client
        self._classifications: Dict[str, str] = self._load_classifications()
        self._clusters: Dict = self._load_clusters()
        self._build_reverse_lookup()

    def _build_reverse_lookup(self):
        """Build symbol -> sector mapping from BASE_SECTORS + saved classifications."""
        self._symbol_to_sector: Dict[str, str] = {}
        for sector, symbols in BASE_SECTORS.items():
            if sector == "AI":
                continue  # Skip merged, use sub-sectors
            for sym in symbols:
                self._symbol_to_sector[sym.upper()] = sector
        # Override with saved classifications (includes AI sub-sector splits)
        for sym, sector in self._classifications.items():
            self._symbol_to_sector[sym.upper()] = sector

    def _load_classifications(self) -> Dict[str, str]:
        """Load LLM classifications from JSON."""
        try:
            if _CLASSIFICATION_FILE.exists():
                with open(_CLASSIFICATION_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("classifications", {})
        except Exception as e:
            logger.warning(f"Failed to load sector classifications: {e}")
        return {}

    def _save_classifications(self) -> bool:
        """Save classifications to JSON."""
        try:
            _CLASSIFICATION_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CLASSIFICATION_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "classifications": self._classifications,
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "version": 2,
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
            return True
        except Exception as e:
            logger.error(f"Failed to save sector classifications: {e}")
            return False

    def _load_clusters(self) -> Dict:
        """Load correlation clustering results."""
        try:
            if _CLUSTERING_FILE.exists():
                with open(_CLUSTERING_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load sector clusters: {e}")
        return {}

    def _save_clusters(self, clusters: Dict) -> bool:
        """Save clustering results to JSON."""
        try:
            _CLUSTERING_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CLUSTERING_FILE, "w", encoding="utf-8") as f:
                json.dump(clusters, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Failed to save sector clusters: {e}")
            return False

    def get_sector(self, symbol: str) -> Optional[str]:
        """Get sector for a symbol (with USDT suffix stripping)."""
        s = symbol.upper()
        if s.endswith("USDT"):
            s = s[:-4]
        # Check cached classifications first
        if s in self._symbol_to_sector:
            return self._symbol_to_sector[s]
        # Try LLM classification if not found
        sector = self._classify_with_llm(s)
        if sector:
            self._symbol_to_sector[s] = sector
            return sector
        return None

    def classify_position(self, symbol: str) -> str:
        """Return sector name or 'OTHER' for unclassified symbols."""
        sector = self.get_sector(symbol)
        return sector if sector else "OTHER"

    def get_sector_limit(self, sector: str) -> int:
        """Get exposure limit for a sector."""
        return self.SECTOR_LIMITS.get(sector, 30)

    def _classify_with_llm(self, symbol: str) -> Optional[str]:
        """Use LLM (DeepSeek primary, auto-fallback to OpenAI) to classify a new symbol into a sector.

        Returns sector name or None if classification failed.
        """
        # Check if already classified
        if symbol in self._classifications:
            return self._classifications[symbol]

        try:
            # Build prompt
            "\n".join(
                [
                    f"- {k}: {', '.join(v[:5])}{'...' if len(v) > 5 else ''}"
                    for k, v in BASE_SECTORS.items()
                    if k != "AI"
                ]
            )

            prompt = f"""Classify the cryptocurrency {symbol} into exactly one of these sectors:

AI_INFRA - AI infrastructure (GPUs, compute, oracles, data protocols)
AI_AGENT - AI agents, character tokens, AI platforms
CORE - Major L1 blockchains (BTC, ETH, SOL, foundational protocols)
MEME - Meme/community tokens
L2DEFI - Layer 2 scaling, DeFi protocols, cross-chain bridges, interoperability protocols
RWA - Real-world assets, tokenized securities
OTHER - None of the above

{symbol} is a known cryptocurrency. If it is LayerZero (omnichain interoperability), classify as L2DEFI. If it is Berachain (modular EVM), classify as CORE. If it is Monad (high-performance L1), classify as CORE.

Answer with only the sector name in uppercase."""

            # Use centralized LLM client with automatic fallback
            llm = get_llm_client()
            result = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model="deepseek-v4-flash",
                max_tokens=500,
                temperature=0.1,
            )

            if result is None:
                logger.warning(
                    f"All LLM providers failed for sector classification of {symbol}"
                )
                return None

            content = result["content"].upper()
            provider_used = result.get("provider", "unknown")

            # Validate sector name
            valid_sectors = set(BASE_SECTORS.keys()) - {"AI"} | {"OTHER"}
            if content in valid_sectors:
                sector = content
            else:
                # Try to find any valid sector in the response
                found = None
                for vs in sorted(valid_sectors, key=len, reverse=True):
                    if vs in content:
                        found = vs
                        break
                if found:
                    sector = found
                else:
                    logger.warning(
                        f"LLM returned invalid sector '{content}' for {symbol}, using OTHER"
                    )
                    sector = "OTHER"

            # Save classification
            self._classifications[symbol] = sector
            self._save_classifications()
            logger.info(
                f"SectorClassifier: {symbol} -> {sector} (LLM via {provider_used})"
            )
            return sector

        except Exception as e:
            logger.error(f"LLM classification failed for {symbol}: {e}")
            return None

    def run_correlation_clustering(
        self,
        symbols: List[str],
        min_correlation: float = 0.6,
        max_clusters: int = 8,
    ) -> Dict:
        """Run correlation-based clustering to validate/refine sectors.

        Args:
            symbols: List of symbols to analyze (e.g., ['BTC', 'ETH', 'VIRTUAL', 'VANA'])
            min_correlation: Minimum correlation to be in same cluster
            max_clusters: Maximum number of clusters

        Returns:
            {
                clusters: {cluster_id: [symbols]},
                correlations: {symbol: {symbol: corr_value}},
                suggested_sectors: {symbol: suggested_sector},
                ai_split_recommended: bool,
            }
        """
        try:
            # Fetch 30-day daily closes
            histories = {}
            for sym in symbols:
                url = f"https://api.binance.com/api/v3/klines?symbol={sym}USDT&interval=1d&limit=30"
                try:
                    r = requests.get(url, timeout=10)
                    data = r.json()
                    if isinstance(data, list) and len(data) >= 15:
                        closes = [float(c[4]) for c in data]
                        histories[sym] = closes
                except Exception as e:
                    logger.warning(f"Clustering: failed to fetch {sym}: {e}")
                time.sleep(0.2)  # Rate limit

            if len(histories) < 3:
                logger.warning("Clustering: insufficient data (< 3 symbols)")
                return {
                    "clusters": {},
                    "correlations": {},
                    "suggested_sectors": {},
                    "ai_split_recommended": False,
                }

            import numpy as np

            # Compute log-return correlations
            symbols_with_data = list(histories.keys())
            returns = {}
            for sym in symbols_with_data:
                prices = np.array(histories[sym])
                returns[sym] = np.diff(np.log(prices))

            corr_matrix: Dict[str, Dict[str, float]] = {}
            for i, sym_a in enumerate(symbols_with_data):
                corr_matrix[sym_a] = {}
                for sym_b in symbols_with_data:
                    if sym_a == sym_b:
                        corr_matrix[sym_a][sym_b] = 1.0
                    else:
                        if len(returns[sym_a]) == len(returns[sym_b]):
                            corr = np.corrcoef(returns[sym_a], returns[sym_b])[0, 1]
                            corr_matrix[sym_a][sym_b] = (
                                float(corr) if not np.isnan(corr) else 0.0
                            )
                        else:
                            corr_matrix[sym_a][sym_b] = 0.0

            # Simple greedy clustering
            clusters: Dict[int, List[str]] = {}
            assigned: Set[str] = set()
            cluster_id = 0

            for sym in sorted(
                symbols_with_data,
                key=lambda s: -sum(
                    abs(corr_matrix[s].get(other, 0))
                    for other in symbols_with_data
                    if other != s
                ),
            ):
                if sym in assigned:
                    continue

                # Start new cluster
                clusters[cluster_id] = [sym]
                assigned.add(sym)

                # Add highly correlated symbols
                for other in symbols_with_data:
                    if other in assigned:
                        continue
                    avg_corr = float(
                        np.mean(
                            [
                                abs(corr_matrix[sym].get(m, 0))
                                for m in clusters[cluster_id]
                            ]
                        )
                    )
                    if avg_corr >= min_correlation:
                        clusters[cluster_id].append(other)
                        assigned.add(other)

                cluster_id += 1
                if cluster_id >= max_clusters:
                    break

            # Assign remaining
            for sym in symbols_with_data:
                if sym not in assigned:
                    # Find best cluster
                    best_cluster = 0
                    best_corr = 0.0
                    for cid, members in clusters.items():
                        avg_corr = float(
                            np.mean([abs(corr_matrix[sym].get(m, 0)) for m in members])
                        )
                        if avg_corr > best_corr:
                            best_corr = avg_corr
                            best_cluster = cid
                    clusters[best_cluster].append(sym)
                    assigned.add(sym)

            # Detect AI split recommendation
            ai_symbols = [
                s
                for s in symbols_with_data
                if self.classify_position(s + "USDT") in ("AI", "AI_INFRA", "AI_AGENT")
            ]
            ai_split_recommended = False
            if len(ai_symbols) >= 4:
                # Check if AI_INFRA and AI_AGENT have low cross-correlation
                infra = [s for s in ai_symbols if s in BASE_SECTORS.get("AI_INFRA", [])]
                agents = [
                    s for s in ai_symbols if s in BASE_SECTORS.get("AI_AGENT", [])
                ]
                if infra and agents:
                    cross_corrs = []
                    for infra_sym in infra:
                        for a in agents:
                            cross_corrs.append(
                                abs(corr_matrix.get(infra_sym, {}).get(a, 0))
                            )
                    avg_cross = np.mean(cross_corrs) if cross_corrs else 1.0
                    if avg_cross < 0.5:
                        ai_split_recommended = True
                        logger.info(
                            f"Clustering: AI split recommended (infra-agent corr={avg_cross:.2f})"
                        )

            result = {
                "clusters": clusters,
                "correlations": corr_matrix,
                "suggested_sectors": {},  # TODO: map clusters to sectors
                "ai_split_recommended": ai_split_recommended,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            self._save_clusters(result)
            return result

        except Exception as e:
            logger.error(f"Correlation clustering failed: {e}")
            return {
                "clusters": {},
                "correlations": {},
                "suggested_sectors": {},
                "ai_split_recommended": False,
            }


# Backward-compatible interface for RiskManager
class SectorExposure:
    """Legacy wrapper - delegates to SectorClassifier."""

    MAX_SECTOR_PCT = 30
    # P1: AI sector cap reduced from 80 → 50 (consistent with SectorClassifier)
    SECTOR_LIMITS: Dict[str, int] = {
        "AI": 50,
        "AI_INFRA": 50,
        "AI_AGENT": 50,
    }

    # Keep old sector definitions for compatibility
    SECTORS: Dict[str, List[str]] = {
        "AI": [
            "RNDR",
            "FET",
            "AGIX",
            "TAO",
            "GRT",
            "OCEAN",
            "PAAL",
            "AI16Z",
            "VIRTUAL",
            "AIXBT",
            "GRIFT",
            "VANA",
            "IO",
        ],
        "CORE": [
            "BTC",
            "ETH",
            "SOL",
            "BNB",
            "AVAX",
            "ADA",
            "DOT",
            "MATIC",
            "ATOM",
            "NEAR",
        ],
        "MEME": [
            "DOGE",
            "SHIB",
            "PEPE",
            "WIF",
            "BONK",
            "FLOKI",
            "BRETT",
            "POPCAT",
        ],
        "L2DEFI": [
            "ARB",
            "STRK",
            "ZK",
            "INJ",
            "SUI",
            "SEI",
            "APT",
            "AAVE",
            "UNI",
            "CRV",
            "LDO",
        ],
        "RWA": ["ON", "ENA"],
    }

    # Build reverse lookup
    _SYMBOL_TO_SECTOR: Dict[str, str] = {}
    for _sector, _symbols in SECTORS.items():
        for _sym in _symbols:
            _SYMBOL_TO_SECTOR[_sym.upper()] = _sector

    _classifier: Optional[SectorClassifier] = None

    @classmethod
    def _get_classifier(cls) -> SectorClassifier:
        if cls._classifier is None:
            cls._classifier = SectorClassifier()
        return cls._classifier

    @classmethod
    def get_sector(cls, symbol: str) -> Optional[str]:
        s = symbol.upper()
        if s.endswith("USDT"):
            s = s[:-4]
        # Try classifier first (includes LLM classifications)
        classifier = cls._get_classifier()
        sector = classifier.get_sector(symbol)
        if sector:
            return sector
        # Fallback to static mapping
        return cls._SYMBOL_TO_SECTOR.get(s)

    @classmethod
    def classify_position(cls, symbol: str) -> str:
        sector = cls.get_sector(symbol)
        return sector if sector else "OTHER"

    @classmethod
    def get_sector_limit(cls, sector: str) -> int:
        return cls.SECTOR_LIMITS.get(sector, cls.MAX_SECTOR_PCT)

    def check(self, positions: List[Dict]) -> Dict:
        """Check current sector exposure against limits."""
        if not positions:
            all_sectors = list(self.SECTORS.keys()) + ["AI_INFRA", "AI_AGENT", "OTHER"]
            return {
                "allowed_sectors": all_sectors,
                "blocked_sectors": [],
                "details": {},
                "total_value": 0,
            }

        sector_values: Dict[str, float] = {}
        total_value = 0.0

        for pos in positions:
            symbol = pos.get("symbol") or pos.get("asset", "").upper()
            value = float(pos.get("value_usdt", 0))
            if value <= 0:
                continue
            sector = self.classify_position(symbol)
            sector_values[sector] = sector_values.get(sector, 0) + value
            total_value += value

        details: Dict[str, Dict] = {}
        allowed_sectors: List[str] = []
        blocked_sectors: List[str] = []

        all_sector_names = list(
            set(list(self.SECTORS.keys()) + ["AI_INFRA", "AI_AGENT", "OTHER"])
        )
        for sector in all_sector_names:
            sector_val = sector_values.get(sector, 0)
            pct = (sector_val / total_value * 100) if total_value > 0 else 0
            limit_pct = self.get_sector_limit(sector)

            details[sector] = {
                "value_usdt": round(sector_val, 2),
                "pct": round(pct, 2),
                "limit_pct": limit_pct,
                "remaining_pct": round(max(limit_pct - pct, 0), 2),
            }

            if pct >= limit_pct:
                blocked_sectors.append(sector)
            else:
                allowed_sectors.append(sector)

        return {
            "allowed_sectors": allowed_sectors,
            "blocked_sectors": blocked_sectors,
            "details": details,
            "total_value": round(total_value, 2),
        }

    def is_sector_allowed(
        self, symbol: str, positions: List[Dict], new_value_usdt: float = 0
    ) -> bool:
        """Check if adding a new position would exceed sector limit."""
        sector = self.classify_position(symbol)
        if not positions:
            return True

        total_value = sum(float(p.get("value_usdt", 0)) for p in positions)
        if total_value <= 0:
            return True

        sector_value = 0.0
        for pos in positions:
            pos_symbol = (pos.get("symbol") or pos.get("asset", "")).upper()
            if self.classify_position(pos_symbol) == sector:
                sector_value += float(pos.get("value_usdt", 0))

        if new_value_usdt <= 0:
            new_value_usdt = total_value * 0.02
        adjusted_total = total_value + new_value_usdt
        adjusted_sector = sector_value + new_value_usdt
        sector_pct = (
            (adjusted_sector / adjusted_total) * 100 if adjusted_total > 0 else 0
        )
        limit_pct = self.get_sector_limit(sector)
        allowed = sector_pct < limit_pct

        if not allowed:
            logger.info(
                "SectorExposure: %s (%s) blocked – sector at %.1f%% (limit %d%%)",
                symbol,
                sector,
                sector_pct,
                limit_pct,
            )

        return allowed
