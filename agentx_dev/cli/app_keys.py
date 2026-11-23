"""API keys set from the app window: kept encrypted by the system's credential store (``keystore``), loaded at the
start (a saved key wins over the environment variable), and switched to at the next message. Only the last four
characters of a key are ever shown or logged."""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Optional

from agentx_dev.cli import keystore
from agentx_dev.cli.models import ModelError, key_variable, provider_for


class KeyCommands:
    """Mixed into ``ChatApp``. ``_key_store`` is the ``keystore`` module in a real run and None in tests that pass
    their own environment (so a test never reads or writes the user's real keys)."""

    _key_store: Any = None
    _keys_from_env: Optional[Dict[str, str]] = None    # the variables as they were before saved keys were loaded
    _key_changes: Optional[Dict[str, Optional[str]]] = None   # variable -> new key (None: removed), not yet used
    _key_lock = threading.Lock()

    def _key_names(self) -> List[str]:
        return list(keystore.VARIABLES)

    def load_stored_keys(self) -> List[str]:
        """At the start: the keys saved from the window go into this run's variables (they win). Names them, never
        the values."""
        if self._keys_from_env is None:
            self._keys_from_env = {name: str(self.env.get(name) or "") for name in self._key_names()}
        store = self._key_store
        if store is None:
            return []
        try:
            found = store.saved()
        except Exception:
            return []
        for name, value in found.items():
            self.env[name] = value
        return sorted(found)

    def key_status(self) -> List[Dict[str, Any]]:
        """Each provider for the window: whether a key is set, where it comes from ("saved", "environment" or ""),
        and its last four characters. Never the key."""
        if self._keys_from_env is None:
            self._keys_from_env = {name: str(self.env.get(name) or "") for name in self._key_names()}
        store = self._key_store
        with self._key_lock:
            pending = dict(self._key_changes or {})
        rows = []
        for provider, (variable, label) in keystore.PROVIDERS.items():
            stored = None
            if store is not None:
                try:
                    stored = store.get(variable)
                except Exception:
                    stored = None
            if variable in pending:
                stored = pending[variable]
            outside = self._keys_from_env.get(variable) or ""
            value = stored or outside or str(self.env.get(variable) or "")
            source = "saved" if stored else ("environment" if value else "")
            rows.append({"provider": provider, "label": label, "variable": variable, "set": bool(value),
                         "source": source, "tail": keystore.tail(value), "pending": variable in pending,
                         "shadows": bool(stored and outside and outside != stored)})
        return rows

    def change_key(self, provider: str, value: Optional[str]) -> str:
        """The window's Save (``value``) or Remove (None) for ``provider``: saved in (or removed from) the credential
        store now, used from the next message. Returns what to tell the user; raises ``keystore.KeyStoreError``."""
        if provider not in keystore.PROVIDERS:
            raise keystore.KeyStoreError("Unknown provider.")
        variable, label = keystore.PROVIDERS[provider]
        store = self._key_store
        if store is None:
            raise keystore.KeyStoreError("Keys cannot be saved in this run.")
        if value is None:
            if not store.remove(variable):
                return f"No {label} key was saved."
            with self._key_lock:
                self._key_changes = {**(self._key_changes or {}), variable: None}
            fallback = (self._keys_from_env or {}).get(variable) or ""
            return (f"{label} key removed. " + ("agentx goes back to " + variable + " from your next message."
                    if fallback else "The model keeps the old key until agentx starts again or you switch model."))
        key = store.save(variable, value)
        with self._key_lock:
            self._key_changes = {**(self._key_changes or {}), variable: key}
        return f"{label} key saved (ends in {keystore.tail(key)}). agentx uses it from your next message."

    def apply_key_changes(self) -> None:
        """Before the next message or command: the keys changed in the window go into this run's variables, and
        the chat model is built again when its provider's key changed (the conversation stays)."""
        with self._key_lock:
            changes, self._key_changes = dict(self._key_changes or {}), None
        if not changes:
            return
        for variable, value in changes.items():
            if value:
                self.env[variable] = value
            else:
                fallback = (self._keys_from_env or {}).get(variable) or os.environ.get(variable) or ""
                if fallback:
                    self.env[variable] = fallback
        provider = provider_for(str(getattr(self, "requested_model", "") or getattr(self, "model_label", "") or ""))
        if provider not in ("claude", "gpt") or key_variable(provider) not in changes or not str(self.env.get(key_variable(provider)) or ""):
            return
        if getattr(self, "session", None) is None or getattr(self, "model", None) is None:
            return
        try:
            self.switch_model(self.requested_model)
        except ModelError as e:
            self.error(self._safe(e))
        except Exception as e:
            self.error(f"Could not use the new key ({type(e).__name__}).")
        else:
            self.notice(f"Using your new {keystore.PROVIDERS['claude' if provider == 'claude' else 'openai'][1]} key "
                        f"(ends in {keystore.tail(self.env[key_variable(provider)])}).")
