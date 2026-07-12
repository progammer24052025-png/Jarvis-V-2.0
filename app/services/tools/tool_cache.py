"""
Tool Result Cache for J.A.R.V.I.S.
====================================
Caches tool execution results with per-tool TTL to avoid redundant calls.
Thread-safe. Tracks hit/miss statistics for monitoring.

Example: system_info, pc_health, weather, wifi_info change rarely and
don't need to be re-executed on every user request.
"""

import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("J.A.R.V.I.S")


class ToolCache:
    """
    In-memory cache for tool results with TTL.

    Cache key = tool_name + sorted JSON hash of params.
    Each entry stores (result, expiry_timestamp).
    """

    def __init__(self):
        self._cache: Dict[str, Tuple[Any, float]] = {}   # key -> (result, expires_at)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self, tool_name: str, params: list) -> Optional[Any]:
        """
        Return cached result if available and not expired.
        Returns None on miss or expiry.
        """
        key = self._make_key(tool_name, params)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None

            result, expires_at = entry
            if time.time() > expires_at:
                # Expired — remove it
                del self._cache[key]
                self._misses += 1
                logger.debug("[TOOL-CACHE] Expired: %s", tool_name)
                return None

            self._hits += 1
            logger.info("[TOOL-CACHE] Hit: %s (age %.0fs)", tool_name,
                        time.time() - (expires_at - self._get_ttl(tool_name, params)))
            return result

    def set(self, tool_name: str, params: list, result: Any, ttl_seconds: int) -> None:
        """Store a result in the cache with a TTL in seconds."""
        if ttl_seconds <= 0:
            return  # don't cache
        key = self._make_key(tool_name, params)
        expires_at = time.time() + ttl_seconds
        with self._lock:
            self._cache[key] = (result, expires_at)
        logger.debug("[TOOL-CACHE] Stored: %s (TTL %ds)", tool_name, ttl_seconds)

    def invalidate(self, tool_name: str) -> int:
        """
        Remove all cached entries for a specific tool.
        Returns the number of entries removed.
        """
        removed = 0
        with self._lock:
            keys_to_remove = [
                k for k in self._cache
                if k.startswith(f"{tool_name}:")
            ]
            for k in keys_to_remove:
                del self._cache[k]
                removed += 1
        if removed:
            logger.info("[TOOL-CACHE] Invalidated %d entries for %s", removed, tool_name)
        return removed

    def clear(self) -> int:
        """Remove all cached entries. Returns count removed."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
        logger.info("[TOOL-CACHE] Cleared %d entries", count)
        return count

    def stats(self) -> dict:
        """Return cache statistics for monitoring."""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0.0
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_pct": round(hit_rate, 1),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _make_key(tool_name: str, params: list) -> str:
        """Create a deterministic cache key from tool name + params."""
        # Sort params for consistent hashing regardless of order
        params_str = json.dumps(sorted(params, key=str), sort_keys=True, default=str)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
        return f"{tool_name}:{params_hash}"

    @staticmethod
    def _get_ttl(tool_name: str, params: list) -> float:
        """Estimate original TTL from cache entry (for logging only)."""
        # This is approximate — we don't store the TTL separately.
        # Used only for debug logging, so returning 0 is fine.
        return 0.0


# Global cache instance
tool_cache = ToolCache()
