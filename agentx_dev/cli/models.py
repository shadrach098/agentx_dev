"""Choosing the chat model (a --model name, then settings, then whichever API key is set) and the extra models
team helpers may run on."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from agentx_dev.ChatModel import GPT, Claude

ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"
MAX_TEAM_MODELS = 4                  # extra models a team job may use, the chat's own not counted
MAX_LISTED_MODELS = MAX_TEAM_MODELS + 1   # a list may also name the chat's own model (to give it a note)
MAX_ERROR_NOTE_CHARS = 160            # a ModelError's message, as shown in a "Not used" line
MAX_USE_CHARS = 200                  # a note on what a helper model is for (a longer one is shortened)


# Offered when the saved model's provider has no key but this provider's key is set (the first is suggested).
SUGGESTED_MODELS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "claude": (
        ("claude-opus-5-5", "strong at coding and agent work"),
        ("claude-sonnet-5-5", "fast and capable, about half the price of Opus"),
        ("claude-haiku-5-5", "the cheapest and fastest, for simple work"),
        ("claude-fable-5-1", "the most capable, and the most expensive"),
    ),
    "gpt": (),                       # GPT model names change often: the user types one
}


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


def other_provider(name: Optional[str], env: Mapping[str, str]) -> Optional[str]:
    """When ``name``'s provider has no key but the other provider's key is set: that other provider (``"claude"``
    or ``"gpt"``). Otherwise None, and the missing key is simply an error."""
    provider = provider_for(name or "")
    if provider is None or _key(env, key_variable(provider)):
        return None
    other = "gpt" if provider == "claude" else "claude"
    return other if _key(env, key_variable(other)) else None


def _no_key(provider: str) -> ModelError:
    variable = ANTHROPIC_KEY if provider == "claude" else OPENAI_KEY
    return ModelError(f"No API key for {provider}: set the {variable} environment variable.")


def _key(env: Mapping[str, str], variable: str) -> str:
    """The variable's value without surrounding whitespace; blank or non-text counts as missing ('')."""
    value = env.get(variable)
    return value.strip() if isinstance(value, str) else ""


# A reasoning model can take minutes on a big request, so wait up to 5 minutes for an answer. The client itself
# does not retry (a hidden retry made one stuck call wait 4 minutes per attempt); BaseChatModel retries instead,
# a timeout once at most, and the app shows each retry (``on_retry``).
MODEL_TIMEOUT_SEC = 300.0
CLIENT_RETRIES = 0


def claude_model_id(name: str) -> str:
    """A Claude model name as the API knows it: a version written with a dot (``claude-opus-5.5``) becomes the
    dashed id (``claude-opus-5-5``), which is the only form the API accepts. A dated or dashed name is unchanged."""
    return re.sub(r"(?<=\d)\.(?=\d)", "-", name.strip())


def _claude(name: Optional[str], key: str) -> Any:
    """A Claude model as agentx uses it: with prompt caching on (the system prompt, the tools and the conversation
    so far are read from Anthropic's cache on the next step, billed at about a tenth). Without it every step of a
    long run sent the whole conversation again at full price: one helper used 229k tokens on 8 tool calls."""
    kwargs: Dict[str, Any] = {"model": name} if name else {}
    return Claude(api_key=key, timeout=MODEL_TIMEOUT_SEC, max_retries=CLIENT_RETRIES, enable_prompt_cache=True,
                  **kwargs)


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
            if provider == "claude":
                return _claude(claude_model_id(name), key)
            return GPT(model=name, api_key=key, timeout=MODEL_TIMEOUT_SEC, max_retries=CLIENT_RETRIES)
        anthropic_key = _key(env, ANTHROPIC_KEY)
        if anthropic_key:
            return _claude(None, anthropic_key)
        openai_key = _key(env, OPENAI_KEY)
        if openai_key:
            return GPT(api_key=openai_key, timeout=MODEL_TIMEOUT_SEC, max_retries=CLIENT_RETRIES)
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
    """A model team helpers may run on; ``name`` is the label in the helper names (``coder@<name>``) and ``use``
    the user's note on what it is for ("" when none)."""
    name: str
    model: Any
    use: str = ""


def key_variable(provider: str) -> str:
    """The environment variable that holds the key for ``provider`` ("claude" or "gpt")."""
    return ANTHROPIC_KEY if provider == "claude" else OPENAI_KEY


def _acceptable_name(name: str) -> bool:
    """A model name is a plain token: at most 100 characters, no spaces, no control characters and no "=" (which
    separates a name from its note)."""
    return (len(name) <= 100 and name.isprintable() and "=" not in name
            and not any(ch.isspace() for ch in name))


def _shown_name(name: str) -> str:
    """The name as a message shows it: at most 40 characters, repr-escaped so control characters stay visible
    as text and cannot reach the output raw."""
    return repr(name if len(name) <= 40 else name[:40] + "...")


def _shortened_use(text: str) -> str:
    """A note cut to ``MAX_USE_CHARS``, at the last space when there is one, ending in "..."."""
    cut = text[:MAX_USE_CHARS - 3]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:.") + "..."


def _split_entry(value: Any, problems: List[str]) -> Tuple[Any, str]:
    """``(name, note)`` from a ``teamModels`` entry: ``"name"``, ``"name=note"`` or ``{"model": ..., "use": ...}``.
    A note with control characters is dropped and one longer than ``MAX_USE_CHARS`` is shortened, each with a
    problem line; the name is kept."""
    if isinstance(value, dict):
        name, use = value.get("model"), value.get("use") or ""          # "use": null means no note
    elif isinstance(value, str) and "=" in value:
        name, use = value.split("=", 1)
    else:
        name, use = value, ""
    if not isinstance(use, str):
        problems.append(f"'teamModels': the note for {_shown_name(str(name))} is not text; ignored")
        return name, ""
    use = use.strip()
    if use and not use.isprintable():
        problems.append(f"'teamModels': the note for {_shown_name(str(name))} has control characters; ignored")
        return name, ""
    if len(use) > MAX_USE_CHARS:
        problems.append(f"'teamModels': the note for {_shown_name(str(name))} is longer than {MAX_USE_CHARS} "
                        "characters; shortened")
        use = _shortened_use(use)
    return name, use


def parse_team_models(values: Sequence[Any]) -> Tuple[List[str], Dict[str, str], List[str]]:
    """The usable entries of a ``teamModels`` list: their names in order, the notes some of them carry (what the
    model is for, keyed by name), and one problem line per entry or note that was dropped (not a plain model
    name, an unknown provider, past the first ``MAX_LISTED_MODELS``, a bad note). Repeats, in any case, are
    dropped silently (the first one's note is kept)."""
    names: List[str] = []
    uses: Dict[str, str] = {}
    problems: List[str] = []
    seen = set()
    for value in values:
        value, use = _split_entry(value, problems)
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
        if use:
            uses[name] = use
    if len(names) > MAX_LISTED_MODELS:
        problems.append(f"'teamModels' lists {len(names)} models; only the first {MAX_LISTED_MODELS} are kept "
                        f"({MAX_TEAM_MODELS} extra models, plus the chat's own if you list it)")
        names = names[:MAX_LISTED_MODELS]
        uses = {name: use for name, use in uses.items() if name in names}
    return names, uses, problems


def clean_team_models(values: Sequence[Any]) -> Tuple[List[str], List[str]]:
    """``parse_team_models`` without the notes: the usable names and the problem lines."""
    names, _, problems = parse_team_models(values)
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
        if len(usable) == MAX_TEAM_MODELS:
            notes.append(f"Not used: {name} (only {MAX_TEAM_MODELS} extra models)")
            continue
        usable.append(name)
    return usable, notes


def team_models_line(chat_label: str, names: Sequence[str], env: Mapping[str, str]) -> str:
    """The approval panel's line: the chat's model first, then the usable extra models."""
    usable, _ = usable_team_models(chat_label, names, env)
    shown_chat = str(chat_label) if _acceptable_name(str(chat_label)) else _shown_name(str(chat_label))
    return "Helper models: " + ", ".join([f"{shown_chat} (chat)", *usable])


def build_team_models(names: Sequence[str], env: Mapping[str, str], *, chat_label: str = "",
                      factory: Optional[Callable[[str, Mapping[str, str]], Any]] = None,
                      uses: Optional[Mapping[str, str]] = None) -> Tuple[List[HelperModel], List[str]]:
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
            built.append(HelperModel(name, build(name, env), (uses or {}).get(name, "")))
        except ModelError as error:
            message = " ".join(str(error).split())
            if len(message) > MAX_ERROR_NOTE_CHARS:
                message = message[:MAX_ERROR_NOTE_CHARS - 3] + "..."
            notes.append(f"Not used: {name} ({message or 'could not be set up: ModelError'})")
        except Exception as error:
            notes.append(f"Not used: {name} (could not be set up: {type(error).__name__})")
    return built, notes


# -- notes written by the chat model ---------------------------------------------------------------------------

AUTO_PREFIX = "auto: "                  # marks a note the chat model wrote; one the user wrote is never replaced
MAX_AUTO_CHARS = 80 - len(AUTO_PREFIX)    # the chat model is asked for 60


def is_auto_note(note: str) -> bool:
    return isinstance(note, str) and note.startswith(AUTO_PREFIX)


def describe_prompt(names: Sequence[str]) -> str:
    """The one question the chat model is asked about the models a team may use."""
    listed = "\n".join(f"- {name}" for name in names)
    return ("A coding assistant can run helper steps on these models:\n" + listed + "\n\n"
            "For each one, write what it is best at for coding work in at most 60 characters, for example "
            "'hard coding and debugging', 'fast reading and search' or 'cheap, simple edits'. Only describe a "
            "model you reliably know; for a model you do not know, or one newer than what you know, write "
            "unknown. Do not guess from the name. Reply with only a JSON object mapping each name to its note.")


MAX_REPLY_CHARS = 20_000                # a reply longer than this is not searched for JSON


def _first_object(text: str) -> Optional[dict]:
    """The first JSON object in ``text`` (prose before or after it, even with braces, is ignored)."""
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            data, _ = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            return data
        index = text.find("{", index + 1)
    return None


def parse_descriptions(reply: Any, names: Sequence[str]) -> Optional[Dict[str, str]]:
    """The notes in the chat model's reply to ``describe_prompt``, keyed by the names asked about (matched in any
    case), each marked with ``AUTO_PREFIX``. A value starting with "unknown", a note that is not plain text of at
    most ``MAX_AUTO_CHARS`` and a name that was not asked about give nothing. None when the reply holds no JSON
    object at all (unreadable, which is not the same as "unknown")."""
    text = reply[:MAX_REPLY_CHARS] if isinstance(reply, str) else ""
    data = _first_object(text)
    if data is None:
        return None
    wanted = {str(name).lower(): str(name) for name in names}
    notes: Dict[str, str] = {}
    for key, value in data.items():
        name = wanted.get(str(key).strip().lower())
        if name is None or not isinstance(value, str):
            continue
        note = " ".join(value.split())
        if note.lower().startswith(AUTO_PREFIX.strip()):           # "auto: fast" from a model that copied the marker
            note = note[len(AUTO_PREFIX.strip()):].strip()
        note = note.rstrip(".")
        if not note or note.lower().startswith("unknown") or len(note) > MAX_AUTO_CHARS or not note.isprintable():
            continue
        notes[name] = AUTO_PREFIX + note
    return notes


def without_auto_prefix(note: str) -> str:
    """``note`` as the planner reads it: the ``auto:`` marker is for the user, not the planner."""
    return note[len(AUTO_PREFIX):] if is_auto_note(note) else note
