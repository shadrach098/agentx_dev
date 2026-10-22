"""Keeping the request small: old tool results are replaced by a one-line stub in what the model is sent.

Every request carries the whole conversation, so a 6 KB file read at step 2 is paid for again at steps 3, 4, 5, ...
By step 15 a turn has sent each early result many times over. ``trim_tool_results`` returns a copy of the messages
in which every big tool result except the newest few is replaced by a stub that says what it was and how big. The
real history is untouched (it is what gets saved, and what a model can ask for again by calling the tool).

Nothing here changes which messages exist or how they pair up: a stubbed ``tool`` message keeps its
``tool_call_id`` and ``name``, so providers that check the pairing are still satisfied."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

OBSERVATION_PREFIX = "Observation: "          # how a text-mode (no native tool calls) result is framed


@dataclass(frozen=True)
class TrimPolicy:
    """Which results stay whole: the newest ones, as long as together they are within ``keep_chars`` characters,
    and never fewer than ``keep_last`` (``keep_chars=0`` keeps exactly ``keep_last``). A budget rather than a
    count, because keeping only the newest few made agents re-read the same files again and again.
    ``min_chars``: a result shorter than this is never stubbed (a stub is about 150 characters, so there is
    nothing to win). ``exempt``: tools whose results are never stubbed and do not count towards the window (a
    skill's text is instructions for the task, not data to look at once). Inside the window too, an older copy
    of a result that comes again, word for word, later on is stubbed: the newer copy is right there."""
    keep_last: int = 4
    min_chars: int = 800
    exempt: Tuple[str, ...] = ("use_skill",)
    keep_chars: int = 40_000


def _stub(name: Optional[str], chars: int) -> str:
    what = f"{name} result" if name else "result"
    return (f"[Earlier {what} ({chars:,} characters) left out of this request to save tokens. You already used it; "
            "read it again only if you need its exact text now.]")


def _duplicate_stub(name: Optional[str], chars: int) -> str:
    what = f"{name} result" if name else "Result"
    return f"[{what} ({chars:,} characters): the same text comes again later in this conversation.]"


def _result_info(message: Dict[str, Any]) -> Optional[Tuple[Optional[str], str]]:
    """``(tool name or None, text)`` when ``message`` is a tool result, else None."""
    content = message.get("content")
    if not isinstance(content, str):
        return None
    if message.get("role") == "tool":
        return message.get("name"), content
    if message.get("role") == "user" and content.startswith(OBSERVATION_PREFIX):
        return None, content[len(OBSERVATION_PREFIX):]
    return None


def _shorten_arguments(message: Dict[str, Any], policy: TrimPolicy) -> Dict[str, Any]:
    """``message`` with the long string values in its tool calls' JSON arguments replaced (the content a
    ``write_file`` call carried, say). Still valid JSON. Unchanged when there is nothing long to replace."""
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return message
    changed = False
    new_calls = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        raw = function.get("arguments") if isinstance(function, dict) else None
        if not isinstance(raw, str) or len(raw) < policy.min_chars:
            new_calls.append(call)
            continue
        try:
            args = json.loads(raw)
        except ValueError:
            new_calls.append(call)
            continue
        if not isinstance(args, dict):
            new_calls.append(call)
            continue
        shortened = {key: (f"[argument of {len(value):,} characters removed to save tokens]"
                           if isinstance(value, str) and len(value) >= policy.min_chars else value)
                     for key, value in args.items()}
        if shortened == args:
            new_calls.append(call)
            continue
        changed = True
        new_call = dict(call)
        new_call["function"] = {**function, "arguments": json.dumps(shortened)}
        new_calls.append(new_call)
    return {**message, "tool_calls": new_calls} if changed else message


def trim_tool_results(messages: Sequence[Dict[str, Any]], policy: TrimPolicy) -> List[Dict[str, Any]]:
    """A copy of ``messages`` with old big tool results (and old big call arguments) stubbed. See ``TrimPolicy``.
    The window of newest results is left whole, except an older copy of a result that comes again word for word.
    Applying it again to its own output changes nothing."""
    out = list(messages)
    candidates: List[int] = []
    for index, message in enumerate(out):
        info = _result_info(message)
        if info is not None and info[0] not in policy.exempt:
            candidates.append(index)
    if not candidates:
        return out
    # The window: the newest results within the size budget, and at least keep_last of them.
    kept = 0
    used = 0
    boundary = len(out)
    for index in reversed(candidates):
        size = len(_result_info(out[index])[1])             # type: ignore[index]
        if kept < policy.keep_last or used + size <= policy.keep_chars:
            kept += 1
            used += size
            boundary = index
        else:
            break
    if policy.keep_last <= 0 and policy.keep_chars <= 0:
        boundary = len(out)
    seen = set()                                            # texts of newer results (newest first)
    for index in reversed(candidates):
        name, text = _result_info(out[index])               # type: ignore[misc]
        if len(text) < policy.min_chars:
            continue
        if text in seen:
            stub = _duplicate_stub(name, len(text))
        elif index < boundary:
            stub = _stub(name, len(text))
        else:
            seen.add(text)
            continue
        seen.add(text)
        content = stub if name is not None or out[index].get("role") == "tool" else OBSERVATION_PREFIX + stub
        out[index] = {**out[index], "content": content}
    for index in range(boundary):
        if out[index].get("role") == "assistant":
            out[index] = _shorten_arguments(out[index], policy)
    return out


def coerce_trim_policy(value: Any) -> Optional[TrimPolicy]:
    """``None``/``False`` -> no trimming; ``True`` -> the default policy; a ``TrimPolicy`` as it is."""
    if value is None or value is False:
        return None
    if value is True:
        return TrimPolicy()
    if isinstance(value, TrimPolicy):
        return value
    raise TypeError(f"trim_results= must be True, None or a TrimPolicy, got {type(value).__name__}")
