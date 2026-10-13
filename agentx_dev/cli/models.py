"""Choosing the chat model (a --model name, then settings, then whichever API key is set) and the extra models
team helpers may run on."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from agentx_dev.ChatModel import GPT, Claude

ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"
MAX_TEAM_MODELS = 4
MAX_ERROR_NOTE_CHARS = 160            # a ModelError's message, as shown in a "Not used" line


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


# -- the extra models team helpers may run on ------------------------------------------------------------------

@dataclass(frozen=True)
class HelperModel:
    """A model team helpers may run on; ``name`` is the label in the helper names (``coder@<name>``)."""
    name: str
    model: Any


def key_variable(provider: str) -> str:
    """The environment variable that holds the key for ``provider`` ("claude" or "gpt")."""
    return ANTHROPIC_KEY if provider == "claude" else OPENAI_KEY


def _acceptable_name(name: str) -> bool:
    """A model name is a plain token: at most 100 characters, no spaces and no control characters."""
    return len(name) <= 100 and name.isprintable() and not any(ch.isspace() for ch in name)


def _shown_name(name: str) -> str:
    """The name as a message shows it: at most 40 characters, repr-escaped so control characters stay visible
    as text and cannot reach the output raw."""
    return repr(name if len(name) <= 40 else name[:40] + "...")


def clean_team_models(values: Sequence[Any]) -> Tuple[List[str], List[str]]:
    """The usable entries of a ``teamModels`` list, in order, and one problem line per entry that was dropped
    (not text, not a plain model name, an unknown provider, past the first ``MAX_TEAM_MODELS``). Repeats, in any
    case, are dropped silently."""
    names: List[str] = []
    problems: List[str] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            problems.append("'teamModels' has an entry that is not a model name; skipped")
            continue
        name = value.strip()
        if not _acceptable_name(name):
            problems.append(f"'teamModels': {_shown_name(name)} is not a model name "
                            "(too long, spaces or control characters); skipped")
            continue
        if provider_for(name) is None:
            problems.append(f"'teamModels': cannot tell which provider runs {_shown_name(name)} "
                            "(start the name with 'claude' or 'gpt'); skipped")
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
    if len(names) > MAX_TEAM_MODELS:
        problems.append(f"'teamModels' lists {len(names)} models; only the first {MAX_TEAM_MODELS} are used")
        names = names[:MAX_TEAM_MODELS]
    return names, problems


def usable_team_models(chat_label: str, names: Sequence[str],
                       env: Mapping[str, str]) -> Tuple[List[str], List[str]]:
    """Which of ``names`` a team job would use now, without building anything, and a "Not used" line for each
    name whose key is not set. The chat's own model (``chat_label``) and repeats are left out silently."""
    usable: List[str] = []
    notes: List[str] = []
    seen = {str(chat_label).strip().lower()}
    for name in names:
        name = str(name).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        if not _acceptable_name(name):
            notes.append(f"Not used: {_shown_name(name)} (not a model name)")
            continue
        provider = provider_for(name)
        if provider is None:
            notes.append(f"Not used: {_shown_name(name)} (cannot tell which provider runs it)")
            continue
        variable = key_variable(provider)
        if not _key(env, variable):
            notes.append(f"Not used: {name} (no {variable})")
            continue
        usable.append(name)
    return usable[:MAX_TEAM_MODELS], notes


def team_models_line(chat_label: str, names: Sequence[str], env: Mapping[str, str]) -> str:
    """The approval panel's line: the chat's model first, then the usable extra models."""
    usable, _ = usable_team_models(chat_label, names, env)
    shown_chat = str(chat_label) if _acceptable_name(str(chat_label)) else _shown_name(str(chat_label))
    return "Helper models: " + ", ".join([f"{shown_chat} (chat)", *usable])


def build_team_models(names: Sequence[str], env: Mapping[str, str], *, chat_label: str = "",
                      factory: Optional[Callable[[str, Mapping[str, str]], Any]] = None
                      ) -> Tuple[List[HelperModel], List[str]]:
    """Build the extra models for one team job with ``factory`` (``choose_model`` by default). A name without a
    key, or whose build raises, is skipped with a "Not used" line. For a ``ModelError`` the line carries its
    message (``choose_model`` writes those to be shown, for example the hint to install an optional package),
    flattened to one line and cut; for any other error only its type is named, because a constructor's message
    could echo the key. ``KeyboardInterrupt`` and ``SystemExit`` pass through."""
    build = factory or choose_model
    usable, notes = usable_team_models(chat_label, names, env)
    built: List[HelperModel] = []
    for name in usable:
        try:
            built.append(HelperModel(name, build(name, env)))
        except ModelError as error:
            message = " ".join(str(error).split())
            if len(message) > MAX_ERROR_NOTE_CHARS:
                message = message[:MAX_ERROR_NOTE_CHARS - 3] + "..."
            notes.append(f"Not used: {name} ({message or 'could not be set up: ModelError'})")
        except Exception as error:
            notes.append(f"Not used: {name} (could not be set up: {type(error).__name__})")
    return built, notes
