"""Private request-local model roster for LLM selection boundaries.

Only ``choices`` may enter a selection prompt. Keep this object and model objects
host-side. Configured comments must describe capability/purpose, not identities:
we remove known labels and common provider/model tokens, but do not promise
inference-proof anonymity or detect every alias, spelling or biographical clue.
This module does not change human configuration or grant execution permissions.
"""
from __future__ import annotations

import re
import secrets
from typing import Any, Sequence

from .models import HelperModel, model_label


class BlindModelRoster:
    """Map opaque IDs to existing configured models without an identity fallback."""

    def __init__(self, model: Any, extra_models: Sequence[HelperModel] = (),
                 chat_use: str = "") -> None:
        configured = [(model, chat_use)] + [(item.model, item.use or "") for item in extra_models]
        identities = [item.name for item in extra_models]
        identities.extend(model_label(item) for item, _ in configured)
        # Match complete configured labels and their components, including custom aliases.
        tokens = {part for name in identities for part in
                  [str(name), *re.split(r"[^\w]+", str(name))] if len(part) >= 3}
        known = re.compile(
            r"\b(?:anthropic|openai|google|gemini|claude|gpt|deepseek|ollama|"
            r"mistral|qwen|llama|sonnet|opus|haiku|grok|xai|cohere)\b"
            r"(?:[-/.][\w.-]+)*", re.IGNORECASE)

        def comment(text: str) -> str:
            text = known.sub("[identity removed]", str(text))
            for token in sorted(tokens, key=len, reverse=True):
                text = re.sub(re.escape(token), "[identity removed]", text, flags=re.IGNORECASE)
            return " ".join(text.split())

        self.sanitize_comment = comment
        self.__models = {}
        choices = []
        for configured_model, use in configured:
            opaque_id = "model_" + secrets.token_hex(12)
            self.__models[opaque_id] = configured_model
            choices.append((opaque_id, comment(use)))
        self.choices = tuple(choices)

    def resolve(self, opaque_id: str) -> Any:
        """Reject unknown/stale/empty IDs; never echo input or a configured identity."""
        if not isinstance(opaque_id, str) or opaque_id not in self.__models:
            raise ValueError("Invalid model selection ID.")
        return self.__models[opaque_id]
