"""
Automatic setup for AgentX enhanced features.

This module automatically initializes observability, caching, and memory
based on configuration. No manual setup required.
"""

from typing import Optional
import logging
from pathlib import Path

from .Config import config
from .Observability import observability, ConsoleHook, FileHook, MetricsHook
from .Cache import InMemoryCache, LRUCache, FileCache, BaseCache
from .Memory import (
    TokenLimitedMemory, SlidingWindowMemory, ImportanceBasedMemory,
    BaseMemory
)

logger = logging.getLogger(__name__)


class AutoSetup:
    """Handles automatic initialization of enhanced features."""

    def __init__(self):
        self._observability_initialized = False
        self._cache_initialized = False
        self._global_cache: Optional[BaseCache] = None
        self._metrics_hook: Optional[MetricsHook] = None

    def initialize_observability(self):
        """Auto-initialize observability hooks based on config."""
        if self._observability_initialized or not config.observability_enabled:
            return

        try:
            # Console logging
            if config.console_logging:
                console_hook = ConsoleHook(
                    verbose=config.verbose_logging,
                    color=True
                )
                observability.add_hook(console_hook)

            # File logging
            if config.file_logging:
                log_path = Path(config.log_file_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)

                file_hook = FileHook(
                    filepath=str(log_path),
                    append=True
                )
                observability.add_hook(file_hook)

            # Metrics collection
            if config.metrics_enabled:
                self._metrics_hook = MetricsHook()
                observability.add_hook(self._metrics_hook)

            self._observability_initialized = True

        except Exception as e:
            logger.warning(f"Failed to initialize observability: {e}")

    def initialize_cache(self) -> Optional[BaseCache]:
        """Auto-initialize cache based on config."""
        if self._cache_initialized or not config.caching_enabled:
            return self._global_cache

        try:
            if config.cache_type == "memory":
                self._global_cache = InMemoryCache(default_ttl=config.cache_ttl)

            elif config.cache_type == "lru":
                self._global_cache = LRUCache(
                    capacity=config.lru_capacity,
                    default_ttl=config.cache_ttl
                )

            elif config.cache_type == "file":
                cache_dir = Path(config.cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)

                self._global_cache = FileCache(
                    cache_dir=str(cache_dir),
                    default_ttl=config.cache_ttl
                )

            self._cache_initialized = True

        except Exception as e:
            logger.warning(f"Failed to initialize cache: {e}")
            self._global_cache = None

        return self._global_cache

    def create_memory(self) -> Optional[BaseMemory]:
        """Create memory instance based on config."""
        if not config.memory_enabled:
            return None

        try:
            if config.memory_type == "token_limited":
                return TokenLimitedMemory(
                    max_tokens=config.max_tokens,
                    preserve_system=True
                )

            elif config.memory_type == "sliding_window":
                return SlidingWindowMemory(
                    max_messages=config.sliding_window_size,
                    preserve_system=True
                )

            elif config.memory_type == "importance":
                return ImportanceBasedMemory(
                    max_messages=20,
                    importance_threshold=0.3,
                    preserve_system=True
                )

        except Exception as e:
            logger.warning(f"Failed to create memory: {e}")
            return None

    def get_metrics_summary(self):
        """Get metrics summary if metrics are enabled."""
        if self._metrics_hook:
            return self._metrics_hook.get_summary()
        return None

    def get_global_cache(self) -> Optional[BaseCache]:
        """Get the global cache instance."""
        if not self._cache_initialized:
            return self.initialize_cache()
        return self._global_cache


import threading as _threading

# Global auto-setup instance + lock guarding initialization.
_auto_setup = AutoSetup()
_auto_setup_lock = _threading.Lock()


def get_auto_setup() -> AutoSetup:
    """Get the global auto-setup instance."""
    return _auto_setup


def ensure_initialized():
    """Idempotently initialize observability + cache subsystems.

    Safe to call from multiple threads — the lock guarantees the underlying
    initializers run at most once. Previously this was invoked at module
    import time, which made `import agentx_dev` mutate global state before
    the caller had a handle on anything. Now callers (e.g. AgentRunner)
    invoke this lazily on first use.
    """
    with _auto_setup_lock:
        _auto_setup.initialize_observability()
        _auto_setup.initialize_cache()
