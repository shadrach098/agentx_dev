"""
Tool result caching for AgentX framework.

This module provides caching mechanisms to avoid redundant tool executions:
- In-memory caching with TTL (Time To Live)
- Persistent file-based caching
- LRU (Least Recently Used) cache
- Custom cache key generation
"""

from typing import Any, Optional, Callable, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from abc import ABC, abstractmethod
from pathlib import Path
import json
import hashlib
import logging
from functools import wraps

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Represents a single cache entry."""
    key: str
    value: Any
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    ttl_seconds: Optional[int] = None

    def is_expired(self) -> bool:
        """Check if the cache entry has expired."""
        if self.ttl_seconds is None:
            return False
        age = (datetime.now() - self.created_at).total_seconds()
        return age > self.ttl_seconds

    def touch(self):
        """Update last accessed time and increment access count."""
        self.last_accessed = datetime.now()
        self.access_count += 1


class BaseCache(ABC):
    """Abstract base class for cache implementations."""

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Retrieve value from cache."""
        pass

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store value in cache."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete value from cache."""
        pass

    @abstractmethod
    def clear(self):
        """Clear all cache entries."""
        pass

    @abstractmethod
    def has(self, key: str) -> bool:
        """Check if key exists in cache."""
        pass


class InMemoryCache(BaseCache):
    """
    Simple in-memory cache with TTL support.

    Fast but data is lost when program terminates.
    """

    def __init__(self, default_ttl: Optional[int] = None):
        """
        Initialize in-memory cache.

        Args:
            default_ttl: Default time-to-live in seconds (None = no expiration).
        """
        import threading
        self.default_ttl = default_ttl
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                del self._cache[key]
                return None
            entry.touch()
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store value in cache with optional TTL."""
        ttl = ttl if ttl is not None else self.default_ttl
        entry = CacheEntry(
            key=key,
            value=value,
            created_at=datetime.now(),
            last_accessed=datetime.now(),
            ttl_seconds=ttl,
        )
        with self._lock:
            self._cache[key] = entry

    def delete(self, key: str) -> bool:
        """Delete entry from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self):
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()

    def has(self, key: str) -> bool:
        """Check if key exists and is not expired."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False
            if entry.is_expired():
                del self._cache[key]
                return False
            return True

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns number of removed entries."""
        expired_keys = [key for key, entry in self._cache.items() if entry.is_expired()]
        for key in expired_keys:
            self.delete(key)
        return len(expired_keys)

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_entries = len(self._cache)
        expired = sum(1 for entry in self._cache.values() if entry.is_expired())
        return {
            "total_entries": total_entries,
            "active_entries": total_entries - expired,
            "expired_entries": expired
        }


class LRUCache(BaseCache):
    """
    Least Recently Used (LRU) cache with fixed capacity.

    Automatically evicts least recently used entries when capacity is reached.
    """

    def __init__(self, capacity: int = 100, default_ttl: Optional[int] = None):
        """
        Initialize LRU cache.

        Args:
            capacity: Maximum number of entries to store.
            default_ttl: Default time-to-live in seconds.
        """
        import threading
        self.capacity = capacity
        self.default_ttl = default_ttl
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        """Get value and mark as recently used."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.is_expired():
                del self._cache[key]
                return None
            entry.touch()
            self._cache[key] = self._cache.pop(key)
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store value, evicting LRU entry if at capacity."""
        ttl = ttl if ttl is not None else self.default_ttl
        with self._lock:
            if key in self._cache:
                self._cache[key].value = value
                self._cache[key].created_at = datetime.now()
                self._cache[key].ttl_seconds = ttl
                self._cache[key] = self._cache.pop(key)
                return
            if len(self._cache) >= self.capacity:
                lru_key = next(iter(self._cache))
                del self._cache[lru_key]
                logger.debug(f"Evicted LRU entry: {lru_key}")
            self._cache[key] = CacheEntry(
                key=key,
                value=value,
                created_at=datetime.now(),
                last_accessed=datetime.now(),
                ttl_seconds=ttl,
            )

    def delete(self, key: str) -> bool:
        """Delete entry from cache."""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    def clear(self):
        """Clear all entries."""
        with self._lock:
            self._cache.clear()

    def has(self, key: str) -> bool:
        """Check if key exists and is not expired."""
        entry = self._cache.get(key)
        if entry is None:
            return False
        if entry.is_expired():
            self.delete(key)
            return False
        return True


class FileCache(BaseCache):
    """
    Persistent file-based cache.

    Stores cache entries as files, surviving program restarts.
    """

    def __init__(self, cache_dir: str = ".agentx_cache", default_ttl: Optional[int] = None):
        """
        Initialize file cache.

        Args:
            cache_dir: Directory to store cache files.
            default_ttl: Default time-to-live in seconds.
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.default_ttl = default_ttl

    def _get_filepath(self, key: str) -> Path:
        """Get file path for a cache key."""
        # Hash the key to create a valid filename
        key_hash = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{key_hash}.cache"

    @staticmethod
    def _entry_to_dict(entry: CacheEntry) -> dict:
        return {
            "key": entry.key,
            "value": entry.value,
            "created_at": entry.created_at.isoformat(),
            "last_accessed": entry.last_accessed.isoformat(),
            "access_count": entry.access_count,
            "ttl_seconds": entry.ttl_seconds,
        }

    @staticmethod
    def _entry_from_dict(d: dict) -> CacheEntry:
        return CacheEntry(
            key=d["key"],
            value=d["value"],
            created_at=datetime.fromisoformat(d["created_at"]),
            last_accessed=datetime.fromisoformat(d["last_accessed"]),
            access_count=d.get("access_count", 0),
            ttl_seconds=d.get("ttl_seconds"),
        )

    def get(self, key: str) -> Optional[Any]:
        """Load value from file cache."""
        filepath = self._get_filepath(key)

        if not filepath.exists():
            return None

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                entry = self._entry_from_dict(json.load(f))

            if entry.is_expired():
                self.delete(key)
                return None

            entry.touch()
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self._entry_to_dict(entry), f)

            return entry.value

        except Exception as e:
            logger.error(f"Error loading cache entry: {e}", exc_info=True)
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Store value in file cache.

        ``value`` must be JSON-serializable. Pickle was previously used but is
        an arbitrary-code-execution vector if the cache directory is writable
        by anything else, so JSON is now the only supported encoding.
        """
        ttl = ttl if ttl is not None else self.default_ttl
        filepath = self._get_filepath(key)

        entry = CacheEntry(
            key=key,
            value=value,
            created_at=datetime.now(),
            last_accessed=datetime.now(),
            ttl_seconds=ttl,
        )

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self._entry_to_dict(entry), f)
        except TypeError as e:
            logger.error(
                f"Cache value for key {key!r} is not JSON-serializable: {e}. "
                "Skipping cache write."
            )
        except Exception as e:
            logger.error(f"Error saving cache entry: {e}", exc_info=True)

    def delete(self, key: str) -> bool:
        """Delete cache file."""
        filepath = self._get_filepath(key)

        if filepath.exists():
            try:
                filepath.unlink()
                return True
            except Exception as e:
                logger.error(f"Error deleting cache file: {e}", exc_info=True)
        return False

    def clear(self):
        """Delete all cache files."""
        for filepath in self.cache_dir.glob("*.cache"):
            try:
                filepath.unlink()
            except Exception as e:
                logger.error(f"Error deleting cache file {filepath}: {e}")

    def has(self, key: str) -> bool:
        """Check if cached value exists and is not expired."""
        filepath = self._get_filepath(key)
        if not filepath.exists():
            return False
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                entry = self._entry_from_dict(json.load(f))
        except Exception:
            return False
        if entry.is_expired():
            self.delete(key)
            return False
        return True


def generate_cache_key(tool_name: str, *args, **kwargs) -> str:
    """
    Generate a cache key from tool name and arguments.

    Args:
        tool_name: Name of the tool.
        *args: Positional arguments.
        **kwargs: Keyword arguments.

    Returns:
        Cache key string.
    """
    # Create a stable string representation
    key_parts = [tool_name]

    # Add positional args
    for arg in args:
        key_parts.append(str(arg))

    # Add keyword args (sorted for stability)
    for k in sorted(kwargs.keys()):
        key_parts.append(f"{k}={kwargs[k]}")

    key_string = "|".join(key_parts)

    # Hash for shorter keys
    return hashlib.sha256(key_string.encode()).hexdigest()


def cached_tool(cache: BaseCache, ttl: Optional[int] = None, key_func: Optional[Callable] = None):
    """
    Decorator to add caching to tool functions.

    Args:
        cache: Cache instance to use.
        ttl: Time-to-live in seconds.
        key_func: Custom function to generate cache key (default: generate_cache_key).

    Usage:
        cache = InMemoryCache(default_ttl=300)

        @cached_tool(cache, ttl=60)
        def expensive_tool(input: str) -> str:
            # Expensive operation
            return result
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key
            if key_func:
                cache_key = key_func(func.__name__, *args, **kwargs)
            else:
                cache_key = generate_cache_key(func.__name__, *args, **kwargs)

            # Try to get from cache
            cached_value = cache.get(cache_key)
            if cached_value is not None:
                logger.debug(f"Cache hit for {func.__name__}: {cache_key[:16]}...")
                return cached_value

            # Execute function
            logger.debug(f"Cache miss for {func.__name__}: {cache_key[:16]}...")
            result = func(*args, **kwargs)

            # Store in cache
            cache.set(cache_key, result, ttl=ttl)

            return result

        return wrapper
    return decorator


# Global cache instance for convenience. The lock guards swap-in / read-out
# under concurrent agents; individual cache implementations are responsible
# for their own internal thread-safety.
import threading as _threading
_global_cache_lock = _threading.Lock()
_global_cache = InMemoryCache(default_ttl=300)  # 5 minutes default


def get_global_cache() -> BaseCache:
    """Get the global cache instance."""
    with _global_cache_lock:
        return _global_cache


def set_global_cache(cache: BaseCache):
    """Set a custom global cache instance."""
    global _global_cache
    with _global_cache_lock:
        _global_cache = cache
