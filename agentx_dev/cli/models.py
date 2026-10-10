"""Choosing the chat model: a --model name, then settings, then whichever API key is set."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from agentx_dev.ChatModel import GPT, Claude

ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"


class ModelError(Exception):
    """The model cannot be built; the message says what to do."""


def provider_for(name: str) -> Optional[str]:
    """``"claude"`` for names starting with claude, ``"gpt"`` for gpt-* and o<digit>* names, else None."""
    lowered = name.strip().lower()
    if lowered.startswith("claude"):
        return "claude"
    if lowered.startswith("gpt") or re.match(r"o\d", lowered):
        return "gpt"
    return None


def _no_key(provider: str) -> ModelError:
    variable = ANTHROPIC_KEY if provider == "claude" else OPENAI_KEY
    return ModelError(f"No API key for {provider}: set the {variable} environment variable.")


def _key(env: Mapping[str, str], variable: str) -> str:
    """The variable's value without surrounding whitespace; blank or non-text counts as missing ('')."""
    value = env.get(variable)
    return value.strip() if isinstance(value, str) else ""


def choose_model(name: Optional[str], env: Mapping[str, str]) -> Any:
    """Build the chat model. With ``name`` the provider follows the name; without it the first key found
    (Anthropic, then OpenAI) decides and the class's own default model is used."""
    try:
        if name:
            provider = provider_for(name)
            if provider is None:
                shown = repr(name if len(name) <= 60 else name[:60] + "...")      # repr escapes control characters
                raise ModelError(
                    f"Cannot tell which provider runs {shown}: start the name with 'claude' or 'gpt'.")
            key = _key(env, ANTHROPIC_KEY if provider == "claude" else OPENAI_KEY)
            if not key:
                raise _no_key(provider)
            return (Claude if provider == "claude" else GPT)(model=name, api_key=key)
        anthropic_key = _key(env, ANTHROPIC_KEY)
        if anthropic_key:
            return Claude(api_key=anthropic_key)
        openai_key = _key(env, OPENAI_KEY)
        if openai_key:
            return GPT(api_key=openai_key)
    except ModelError:
        raise
    except ImportError as e:
        raise ModelError(str(e)) from e
    except Exception as e:                      # a constructor's own message could echo the key: name the type only
        raise ModelError(f"Could not set up the model ({type(e).__name__}).") from None
    raise ModelError(f"No API key found: set {ANTHROPIC_KEY} (for Claude) or {OPENAI_KEY} (for GPT).")


def model_label(model: Any) -> str:
    """The model's name for the status bar."""
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    defaults = getattr(model, "defaults", None)          # GPT keeps its model name here
    if isinstance(defaults, dict):
        value = defaults.get("model")
        if isinstance(value, str) and value:
            return value
    return type(model).__name__
