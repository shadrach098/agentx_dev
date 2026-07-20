"""
Global configuration for AgentX framework.

This module provides automatic setup and configuration for all enhanced features.
Users can customize via environment variables or a config file, but everything
works with sensible defaults out of the box.
"""

import os
from typing import Optional
from pathlib import Path
import json


class AgentXConfig:
    """
    Global configuration for AgentX framework.

    All features are enabled by default with sensible settings.
    Can be customized via:
    1. Environment variables
    2. Config file (~/.agentx/config.json)
    3. Direct attribute modification
    """

    def __init__(self):
        # Observability (enabled by default)
        self.observability_enabled = self._get_bool_env("AGENTX_OBSERVABILITY", True)
        self.console_logging = self._get_bool_env("AGENTX_CONSOLE_LOG", True)
        self.file_logging = self._get_bool_env("AGENTX_FILE_LOG", False)
        self.log_file_path = self._get_env("AGENTX_LOG_PATH", ".agentx/logs/agent.jsonl")
        self.metrics_enabled = self._get_bool_env("AGENTX_METRICS", True)
        self.verbose_logging = self._get_bool_env("AGENTX_VERBOSE", False)

        # Caching (enabled by default)
        self.caching_enabled = self._get_bool_env("AGENTX_CACHING", True)
        self.cache_type = self._get_env("AGENTX_CACHE_TYPE", "memory")  # memory, lru, file
        self.cache_ttl = int(self._get_env("AGENTX_CACHE_TTL", "300"))  # 5 minutes
        self.cache_dir = self._get_env("AGENTX_CACHE_DIR", ".agentx/cache")
        self.lru_capacity = int(self._get_env("AGENTX_LRU_CAPACITY", "100"))

        # Memory Management (enabled by default)
        self.memory_enabled = self._get_bool_env("AGENTX_MEMORY", True)
        self.memory_type = self._get_env("AGENTX_MEMORY_TYPE", "token_limited")  # token_limited, sliding_window, importance
        self.max_tokens = int(self._get_env("AGENTX_MAX_TOKENS", "4000"))
        self.sliding_window_size = int(self._get_env("AGENTX_WINDOW_SIZE", "10"))

        # Auto-retry on tool failure
        self.auto_retry = self._get_bool_env("AGENTX_AUTO_RETRY", True)
        self.max_retries = int(self._get_env("AGENTX_MAX_RETRIES", "2"))

        # Load from config file if exists
        self._load_from_file()

    def _get_env(self, key: str, default: str) -> str:
        """Get environment variable or default."""
        return os.environ.get(key, default)

    def _get_bool_env(self, key: str, default: bool) -> bool:
        """Get boolean environment variable."""
        value = os.environ.get(key)
        if value is None:
            return default
        return value.lower() in ('true', '1', 'yes', 'on')

    def _load_from_file(self):
        """Load configuration from file if exists.

        Failures are logged at WARNING. We don't re-raise so the framework
        keeps starting with defaults, but we no longer swallow the error
        silently — a typo in ``config.json`` used to revert to defaults
        with no signal at all.
        """
        config_path = Path.home() / ".agentx" / "config.json"
        if not config_path.exists():
            return
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Could not parse {config_path}: {e}. Using defaults."
            )
            return
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning(
                f"Could not read {config_path}: {e}. Using defaults."
            )
            return
        for key, value in config.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def save_to_file(self):
        """Save current configuration to file."""
        config_path = Path.home() / ".agentx" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        config = {
            "observability_enabled": self.observability_enabled,
            "console_logging": self.console_logging,
            "file_logging": self.file_logging,
            "log_file_path": self.log_file_path,
            "metrics_enabled": self.metrics_enabled,
            "caching_enabled": self.caching_enabled,
            "cache_type": self.cache_type,
            "cache_ttl": self.cache_ttl,
            "memory_enabled": self.memory_enabled,
            "memory_type": self.memory_type,
            "max_tokens": self.max_tokens,
        }

        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)

    def disable_all_enhancements(self):
        """Disable all automatic enhancements (legacy mode)."""
        self.observability_enabled = False
        self.caching_enabled = False
        self.memory_enabled = False

    def enable_all_enhancements(self):
        """Enable all automatic enhancements."""
        self.observability_enabled = True
        self.caching_enabled = True
        self.memory_enabled = True


# Global configuration instance
config = AgentXConfig()


def get_config() -> AgentXConfig:
    """Get the global configuration instance."""
    return config


def set_config(**kwargs):
    """Set configuration options."""
    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)
