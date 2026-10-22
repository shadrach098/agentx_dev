"""Keeping the request small: old tool results are replaced by a one-line stub in what the model is sent.

Every request carries the whole conversation, so a 6 KB file read at step 2 is paid for again at steps 3, 4, 5, ...
By step 15 a turn has sent each early result many times over. ``trim_tool_results`` returns a copy of the messages
in which every big tool result except the newest few is replaced by a stub that says what it was and how big. The
real history is untouched (it is what gets saved, and what a model can ask for again by calling the tool).

Nothing here changes which messages exist or how they pair up: a stubbed ``tool`` message keeps its
``tool_call_id`` and ``name``, so providers that check the pairing are still satisfied."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

OBSERVATION_PREFIX = "Observation: "          # how a text-mode (no native tool calls) result is framed


@dataclass(frozen=True)
class TrimPolicy:
    """Which results stay whole: the newest ones, as long as together they are within ``keep_chars`` characters,
    and never fewer than ``keep_last`` (``keep_chars=0`` keeps exactly ``keep_last``). A budget rather than a
    count, because keeping only the newest few made agents re-read the same files again and again. ``keep_last``
    is 1 (the result the model has just asked for): a larger floor holds big file reads past the budget and moves
    the window's start at every request, which stops the provider's cache from reusing it.
    ``min_chars``: a result shorter than this is never stubbed (a stub is about 150 characters, so there is
    nothing to win). ``exempt``: tools whose results are never stubbed and do not count towards the window (a
    skill's text is instructions for the task, not data to look at once). Inside the window too, an older copy
    of a result that comes again, word for word, later on is stubbed: the newer copy is right there.

    ``step_chars``: how far the window's start jumps when the whole results pass ``keep_chars`` (default: half
    of ``keep_chars``). The whole results then come to between ``keep_chars - step_chars`` and ``keep_chars``.
    Jumping in steps rather than stubbing one more result at every request keeps the start of the request the
    same from one request to the next, which is what lets a provider's prompt cache (OpenAI's automatic one,
    Anthropic's) bill it at its cheaper cached rate. ``step_chars=0`` slides the window at every request.

    ``shrink_exempt_at``: once the conversation (all of it, before trimming) passes this many characters, the
    results of ``exempt`` tools are stubbed too, all but the newest big one: an agent that has read five skills
    otherwise sends all five again at every step. The stub says the skill still applies and how to open it again.
    Measured on the whole conversation, which only grows during a turn, so a stubbed skill stays stubbed (no
    flipping back and forth, which would also spoil the cache). ``0`` never stubs them."""
    keep_last: int = 1
    min_chars: int = 800
    exempt: Tuple[str, ...] = ("use_skill",)
    keep_chars: int = 40_000
    step_chars: Optional[int] = None
    shrink_exempt_at: int = 60_000

    def step(self) -> int:
        return max(0, self.keep_chars // 2 if self.step_chars is None else self.step_chars)


FILE_READERS = ("read_path",)                 # tools whose stub names the file and says how to look again cheaply
_NUMBERED_ROW = re.compile(r"(\d+)\t")


def _clean_path(path: str) -> str:
    """A model-written path made safe to quote in a stub: one line, at most 200 characters."""
    flat = "".join(c if c.isprintable() else "?" for c in path)
    return flat if len(flat) <= 200 else flat[:197] + "..."


def _extent(text: str) -> str:
    """'240 lines', or 'lines 120-180' for a line-numbered slice."""
    rows = text.split("\n")
    first = _NUMBERED_ROW.match(rows[0])
    if first:
        last = next((m for m in (_NUMBERED_ROW.match(row) for row in reversed(rows)) if m), first)
        return f"lines {int(first.group(1)):,}-{int(last.group(1)):,}"
    return f"{len(rows):,} lines"


def _stub(name: Optional[str], chars: int, path: Optional[str] = None, text: str = "") -> str:
    if path is not None:
        return (f"[Earlier {name} result for {path} ({chars:,} characters, {_extent(text)}) left out of this request "
                "to save tokens. Use what you noted from it; to check a detail, grep that file or read only the "
                "lines you need (offset and limit) instead of reading it all again.]")
    what = f"{name} result" if name else "result"
    return (f"[Earlier {what} ({chars:,} characters) left out of this request to save tokens. Use what you noted "
            "from it; run it again only if you need its exact text now.]")


def _duplicate_stub(name: Optional[str], chars: int, path: Optional[str] = None) -> str:
    what = f"{name} result" if name else "Result"
    if path is not None:
        what += f" for {path}"
    return f"[{what} ({chars:,} characters): the same text comes again later in this conversation.]"


def _read_paths(messages: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """``tool_call_id -> path`` for every call to a file-reading tool that names a path."""
    paths: Dict[str, str] = {}
    for message in messages:
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or function.get("name") not in FILE_READERS:
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict) and isinstance(args.get("path"), str) and isinstance(call.get("id"), str):
                paths[call["id"]] = _clean_path(args["path"])
    return paths


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


def _conversation_chars(messages: Sequence[Dict[str, Any]]) -> int:
    """Characters of every message's text and every call's arguments."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                raw = function.get("arguments") if isinstance(function, dict) else None
                if isinstance(raw, str):
                    total += len(raw)
    return total


def _call_args(messages: Sequence[Dict[str, Any]], names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """``tool_call_id -> arguments`` for every call to one of ``names`` whose arguments are a JSON object."""
    found: Dict[str, Dict[str, Any]] = {}
    for message in messages:
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or function.get("name") not in names or not isinstance(call.get("id"), str):
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(args, dict):
                found[call["id"]] = args
    return found


def _exempt_stub(tool: Optional[str], chars: int, args: Optional[Dict[str, Any]]) -> str:
    skill = args.get("name") if args else None
    if tool == "use_skill" and isinstance(skill, str) and skill.strip():
        file = args.get("file") if isinstance(args.get("file"), str) and args.get("file") else "SKILL.md"
        skill, file = _clean_path(skill.strip()), _clean_path(file)
        return (f"[The {skill} skill ({file}, {chars:,} characters) was read earlier in this conversation and still "
                f"applies; its text is left out of this request to save tokens. To see it again, call "
                f'use_skill(name="{skill}", file="{file}").]')
    what = f"{tool} result" if tool else "result"
    return f"[Earlier {what} ({chars:,} characters) left out of this request to save tokens; call it again if you need it.]"


def _stub_older_exempt(out: List[Dict[str, Any]], policy: TrimPolicy) -> None:
    """Stub every big result of an ``exempt`` tool except the newest one, in place."""
    big = [index for index, message in enumerate(out)
           if (info := _result_info(message)) is not None and info[0] in policy.exempt
           and len(info[1]) >= policy.min_chars]
    args = _call_args(out, policy.exempt)
    for index in big[:-1]:
        tool, text = _result_info(out[index])                # type: ignore[misc]
        out[index] = {**out[index], "content": _exempt_stub(tool, len(text), args.get(out[index].get("tool_call_id")))}


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
    if policy.shrink_exempt_at > 0 and _conversation_chars(messages) > policy.shrink_exempt_at:
        _stub_older_exempt(out, policy)
    if not candidates:
        return out
    # The window. Each result has a fixed start: the characters of all the results before it. A result that starts
    # before ``level`` is outside the window. ``level`` only moves in whole steps, so the stubbed results (and
    # with them the start of the request) stay the same until the whole results grow by another step.
    sizes = [len(_result_info(out[index])[1]) for index in candidates]       # type: ignore[index]
    total = sum(sizes)
    if policy.keep_chars <= 0:
        level = total
    elif total <= policy.keep_chars:
        level = 0
    else:
        step = policy.step()
        over = total - policy.keep_chars
        level = -(-over // step) * step if step > 0 else over               # ``over`` rounded up to a step
    outside = 0                                             # how many of the oldest results are stubbed
    start = 0
    for size in sizes:
        if start >= level:
            break
        outside += 1
        start += size
    outside = min(outside, max(0, len(candidates) - policy.keep_last))      # never fewer than keep_last whole
    boundary = candidates[outside] if outside < len(candidates) else len(out)
    seen = set()                                            # texts of newer results (newest first)
    paths = _read_paths(out)
    for index in reversed(candidates):
        name, text = _result_info(out[index])               # type: ignore[misc]
        if len(text) < policy.min_chars:
            continue
        path = paths.get(out[index].get("tool_call_id")) if name in FILE_READERS else None
        if text in seen:
            stub = _duplicate_stub(name, len(text), path)
        elif index < boundary:
            stub = _stub(name, len(text), path, text)
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
