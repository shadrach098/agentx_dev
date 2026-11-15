"""API keys saved from the app window, kept encrypted by the system's credential store (``keyring``: Windows
Credential Manager, the macOS Keychain, the Secret Service on Linux), never in a file. A saved key wins over the
same environment variable. Only its last four characters are ever shown; the full key goes no further than the
model it is handed to.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agentx_dev.cli.models import ANTHROPIC_KEY, OPENAI_KEY

SERVICE = "agentx-api-keys"
# provider -> (the environment variable the key stands in for, what the window calls it)
PROVIDERS: Dict[str, tuple] = {
    "claude": (ANTHROPIC_KEY, "Claude (Anthropic)"),
    "openai": (OPENAI_KEY, "GPT (OpenAI)"),
}
VARIABLES = {variable: provider for provider, (variable, _) in PROVIDERS.items()}
MAX_KEY = 512


class KeyStoreError(Exception):
    """Saving or removing a key failed; the message says why and never holds the key."""


def _keyring() -> Any:
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def available() -> bool:
    return _keyring() is not None


def tail(value: str) -> str:
    """The last four characters, the only part of a key that is ever shown."""
    value = str(value or "").strip()
    return value[-4:] if len(value) >= 12 else ""


def check(value: Any) -> str:
    """The key, stripped; raises ``KeyStoreError`` for one that cannot be an API key (spaces, control characters,
    too short or too long). The message never repeats the key."""
    key = str(value or "").strip()
    if not key:
        raise KeyStoreError("Paste a key first.")
    if len(key) < 12 or len(key) > MAX_KEY:
        raise KeyStoreError("That does not look like an API key (wrong length).")
    if any(ch.isspace() or not ch.isprintable() or ord(ch) > 126 for ch in key):
        raise KeyStoreError("That does not look like an API key (it has spaces or other characters a key never has).")
    return key


def get(variable: str) -> Optional[str]:
    """The key saved for ``variable`` (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``), or None."""
    kr = _keyring()
    if kr is None or variable not in VARIABLES:
        return None
    try:
        value = kr.get_password(SERVICE, variable)
    except Exception:
        return None
    return str(value).strip() or None if value else None


def saved() -> Dict[str, str]:
    """Every saved key, by variable name."""
    return {variable: value for variable in VARIABLES if (value := get(variable))}


def save(variable: str, value: Any) -> str:
    """Save (or replace) the key for ``variable``; returns the stripped key."""
    if variable not in VARIABLES:
        raise KeyStoreError("Unknown provider.")
    key = check(value)
    kr = _keyring()
    if kr is None:
        raise KeyStoreError("Saving keys needs the keyring package: pip install keyring")
    try:
        kr.set_password(SERVICE, variable, key)
    except Exception as e:
        raise KeyStoreError(f"The system's credential store refused it ({type(e).__name__}).") from None
    return key


def remove(variable: str) -> bool:
    """Remove the saved key for ``variable``; False when none was saved."""
    kr = _keyring()
    if kr is None or variable not in VARIABLES or get(variable) is None:
        return False
    try:
        kr.delete_password(SERVICE, variable)
    except Exception as e:
        raise KeyStoreError(f"The system's credential store refused it ({type(e).__name__}).") from None
    return True
