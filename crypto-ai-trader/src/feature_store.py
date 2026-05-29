"""
Feature Store Module - Redis-backed feature store for training-serving consistency.

Provides two namespaces:
  - 'online': live features with 1-hour TTL (stale data protection)
  - 'training': historical snapshots stored in sorted sets for model training

Integration point: price_predictor.py can call snapshot_for_training() after each prediction.
"""

import json
import logging
import time
from typing import Dict, List, Optional

import redis

logger = logging.getLogger(__name__)

# Constants
ONLINE_EXPIRY_SECONDS = 3600  # 1 hour
MAX_TRAINING_SAMPLES = 10_000
REDIS_HOST = "localhost"
REDIS_PORT = 6379


class FeatureStore:
    """Redis-backed feature store with in-memory fallback."""

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT, db: int = 0):
        self._redis_available = False
        self._fallback: Dict[str, Dict] = {}  # key -> {field: value}
        self._fallback_sorted: Dict[str, list] = {}  # key -> [(score, member), ...]
        try:
            self._r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
            self._r.ping()
            self._redis_available = True
            logger.info("Connected to Redis at %s:%d", host, port)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.warning("Redis unavailable (%s), using in-memory fallback", e)
            self._r = None

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _online_key(self, symbol: str) -> str:
        return f"features:online:{symbol}"

    def _training_key(self, symbol: str) -> str:
        return f"features:training:{symbol}"

    def _feature_key(self, symbol: str) -> str:
        return f"features:{symbol}"

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def store_features(self, symbol: str, features: Dict, namespace: str = "online") -> bool:
        """Store a feature dict for *symbol* under *namespace*."""
        if namespace == "online":
            key = self._online_key(symbol)
        else:
            key = self._feature_key(symbol)  # generic fallback for non-online namespaces

        try:
            if self._redis_available:
                pipe = self._r.pipeline()
                pipe.delete(key)
                if features:
                    pipe.hset(key, mapping={k: json.dumps(v) for k, v in features.items()})
                if namespace == "online":
                    pipe.expire(key, ONLINE_EXPIRY_SECONDS)
                pipe.execute()
            else:
                self._fallback[key] = {k: json.dumps(v) for k, v in features.items()}
            return True
        except Exception as e:
            logger.error("store_features failed: %s", e)
            return False

    def get_features(self, symbol: str, namespace: str = "online") -> Optional[Dict]:
        """Retrieve stored features for *symbol*."""
        if namespace == "online":
            key = self._online_key(symbol)
        else:
            key = self._feature_key(symbol)

        try:
            if self._redis_available:
                data = self._r.hgetall(key)
                if not data:
                    return None
            else:
                data = self._fallback.get(key)
                if not data:
                    return None
            return {k: json.loads(v) for k, v in data.items()}
        except Exception as e:
            logger.error("get_features failed: %s", e)
            return None

    def snapshot_for_training(
        self, symbol: str, label: int, timestamp: float = None
    ) -> bool:
        """
        Copy the current online features for *symbol* into the training namespace
        with an associated *label* and *timestamp*.
        """
        features = self.get_features(symbol, namespace="online")
        if features is None:
            logger.warning("No online features found for %s – snapshot skipped", symbol)
            return False

        ts = timestamp if timestamp is not None else time.time()
        member = json.dumps({
            "features": features,
            "label": label,
            "timestamp": ts,
        })

        key = self._training_key(symbol)

        try:
            if self._redis_available:
                self._r.zadd(key, {member: ts})
                # Auto-prune oldest samples beyond the cap
                count = self._r.zcard(key)
                if count > MAX_TRAINING_SAMPLES:
                    self._r.zremrangebyrank(key, 0, count - MAX_TRAINING_SAMPLES - 1)
            else:
                if key not in self._fallback_sorted:
                    self._fallback_sorted[key] = []
                self._fallback_sorted[key].append((ts, member))
                # Sort and prune
                self._fallback_sorted[key].sort(key=lambda x: x[0])
                if len(self._fallback_sorted[key]) > MAX_TRAINING_SAMPLES:
                    self._fallback_sorted[key] = self._fallback_sorted[key][-MAX_TRAINING_SAMPLES:]
            return True
        except Exception as e:
            logger.error("snapshot_for_training failed: %s", e)
            return False

    def get_training_data(
        self, symbol: str = None, limit: int = 1000
    ) -> List[Dict]:
        """
        Return training samples.  If *symbol* is ``None``, return data for all symbols.
        Each element: ``{"features": {...}, "label": int, "timestamp": float}``.
        """
        results: List[Dict] = []

        try:
            if self._redis_available:
                if symbol:
                    keys = [self._training_key(symbol)]
                else:
                    keys = [
                        k for k in self._r.scan_iter("features:training:*")
                        if not k.startswith("features:training::")
                    ]
                for key in keys:
                    raw = self._r.zrange(key, 0, limit - 1, withscores=True)
                    for member, _score in raw:
                        results.append(json.loads(member))
                    if symbol:
                        break  # only one key
            else:
                if symbol:
                    keys = [self._training_key(symbol)]
                else:
                    keys = list(self._fallback_sorted.keys())
                for key in keys:
                    entries = self._fallback_sorted.get(key, [])
                    for _, member in entries[:limit]:
                        results.append(json.loads(member))
        except Exception as e:
            logger.error("get_training_data failed: %s", e)

        return results[:limit]

    def get_feature_names(self) -> List[str]:
        """Return all known feature keys across online and training namespaces."""
        names = set()
        try:
            if self._redis_available:
                # Scan online keys
                for key in self._r.scan_iter("features:online:*"):
                    names.update(self._r.hkeys(key))
            else:
                for key, data in self._fallback.items():
                    if "features:online:" in key:
                        names.update(data.keys())
        except Exception as e:
            logger.error("get_feature_names failed: %s", e)
        return sorted(names)

    def clear_namespace(self, namespace: str) -> int:
        """Delete all keys belonging to *namespace*.  Returns count of deleted keys."""
        count = 0
        try:
            if self._redis_available:
                pattern = f"features:{namespace}:*"
                keys = list(self._r.scan_iter(pattern))
                if keys:
                    count = self._r.delete(*keys)
            else:
                to_delete = [
                    k for k in self._fallback
                    if k.startswith(f"features:{namespace}:")
                ]
                for k in to_delete:
                    del self._fallback[k]
                count = len(to_delete)
        except Exception as e:
            logger.error("clear_namespace failed: %s", e)
        return count

    def get_stats(self) -> Dict:
        """Return per-namespace key count and Redis memory usage."""
        stats: Dict = {}
        try:
            if self._redis_available:
                for ns in ("online", "training"):
                    pattern = f"features:{ns}:*"
                    keys = list(self._r.scan_iter(pattern))
                    stats[f"{ns}_count"] = len(keys)
                info = self._r.info("memory")
                stats["memory_used_bytes"] = info.get("used_memory", 0)
                stats["memory_peak_bytes"] = info.get("used_memory_peak", 0)
                stats["backend"] = "redis"
            else:
                online_count = sum(
                    1 for k in self._fallback if k.startswith("features:online:")
                )
                training_count = sum(
                    1 for k in self._fallback_sorted if k.startswith("features:training:")
                )
                stats["online_count"] = online_count
                stats["training_count"] = training_count
                stats["backend"] = "in_memory"
        except Exception as e:
            logger.error("get_stats failed: %s", e)
        return stats

    # ------------------------------------------------------------------ #
    #  Fallback helper (for testing)
    # ------------------------------------------------------------------ #

    def force_fallback(self):
        """Force the store into in-memory fallback mode (used by verification script)."""
        self._redis_available = False
        self._r = None
        logger.warning("FeatureStore forced into in-memory fallback mode")


# ---------------------------------------------------------------------------
#  Module-level convenience instance
# ---------------------------------------------------------------------------
_store_instance: Optional[FeatureStore] = None


def get_store() -> FeatureStore:
    """Return (and lazily create) the global FeatureStore instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = FeatureStore()
    return _store_instance
