"""Summarising a long conversation before it overflows the model's limit.

The history is cut on a user message (so a tool call is never separated from its result), everything older than
the last few turns is turned into one summary by a model call that the caller passes in, and the summary
replaces it. If the call fails nothing is changed. No terminal output and no I/O here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

THRESHOLD_CHARS = 150_000           # about 40,000 tokens; a rough stand-in
KEEP_USER_MESSAGES = 6
TOOL_RESULT_CHARS = 500
MESSAGE_CHARS = 2000
MAX_INPUT_CHARS = 60_000
RETRY_AFTER_MESSAGES = 10
MIN_SUMMARY_CHARS = 20
MAX_SUMMARY_CHARS = 6000
SUMMARY_TAG = "[Summary of the earlier conversation, written by agentx]"
SUMMARY_ACK = "Understood. I will continue from that summary."
SUMMARY_PROMPT_MARK = "Summarise this earlier part"
SUMMARY_PROMPT = (
    SUMMARY_PROMPT_MARK + " of a coding conversation in at most 500 words, so that work can continue from the "
    "summary alone. Cover what the user wanted, what was decided, which files were changed, the commands that "
    "mattered and what is still open. Do not invent anything. Reply with only the summary.\n\n---\n")

FRAMEWORK_MARK = "[framework]"      # the runner's nudges and error notes, sent as user messages
OBSERVATION_MARK = "Observation:"   # the runner's text (JSON) mode feeds a tool result back as a user message

Message = Dict[str, Any]


@dataclass
class Compaction:
    status: str                      # "none" (nothing to do), "failed" (history unchanged) or "done"
    history: List[Message]
    replaced: int = 0
    summary: str = ""


def _text(value: Any) -> str:
    try:
        return value if isinstance(value, str) else ("" if value is None else str(value))
    except Exception:
        return ""


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " ..."


def message_size(message: Any) -> int:
    if not isinstance(message, dict):
        return 0
    size = len(_text(message.get("content")))
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        size += len(_text(function.get("arguments") if isinstance(function, dict) else ""))
    return size


def history_size(history: List[Message]) -> int:
    return sum(message_size(message) for message in history)


def _is_observation(message: Any) -> bool:
    return (isinstance(message, dict) and message.get("role") == "user"
            and _text(message.get("content")).lstrip().startswith(OBSERVATION_MARK))


def _is_turn_start(message: Any) -> bool:
    return (isinstance(message, dict) and message.get("role") == "user"
            and not _text(message.get("content")).lstrip().startswith(FRAMEWORK_MARK)
            and not _is_observation(message))


def _leading_system(history: List[Message]) -> int:
    count = 0
    for message in history:
        if isinstance(message, dict) and message.get("role") == "system":
            count += 1
        else:
            break
    return count


def split_point(history: List[Message], keep: int = KEEP_USER_MESSAGES) -> Optional[int]:
    """Index of the first message to keep: the ``keep``-th user message from the end. None when there is nothing
    older to summarise."""
    starts = [index for index, message in enumerate(history) if _is_turn_start(message)]
    if len(starts) <= keep:
        return None
    cut = starts[-keep]
    return cut if cut > _leading_system(history) else None


def needs_compaction(history: List[Message], threshold: int = THRESHOLD_CHARS) -> bool:
    return history_size(history) > threshold and split_point(history) is not None


def transcript(older: List[Message], limit: int = MAX_INPUT_CHARS) -> str:
    """The older messages as plain text for the summarising call: tool results cut to ``TOOL_RESULT_CHARS``,
    other messages to ``MESSAGE_CHARS``, and the whole text to ``limit`` (the oldest part is left out)."""
    lines: List[str] = []
    for message in older:
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), _text(message.get("content")).strip()
        if _is_observation(message):
            lines.append("Tool result: " + _cut(content[len(OBSERVATION_MARK):].strip(), TOOL_RESULT_CHARS))
        elif role == "user":
            lines.append("User: " + _cut(content, MESSAGE_CHARS))
        elif role == "assistant":
            if content:
                lines.append("Assistant: " + _cut(content, MESSAGE_CHARS))
            for call in message.get("tool_calls") or []:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                lines.append(f"Assistant called {_text(function.get('name'))}({_cut(_text(function.get('arguments')), 200)})")
        elif role == "tool":
            lines.append(f"Tool {_text(message.get('name'))} result: " + _cut(content, TOOL_RESULT_CHARS))
    text = "\n".join(lines)
    return text if len(text) <= limit else "(earlier part omitted)\n" + text[-limit:]


def summarise(older: List[Message], call: Callable[[str], str]) -> Optional[str]:
    """One model call. None when it raises or answers with too little to be a summary."""
    try:
        reply = call(SUMMARY_PROMPT + transcript(older))
    except Exception:
        return None
    text = _text(reply).strip()
    return text[:MAX_SUMMARY_CHARS] if len(text) >= MIN_SUMMARY_CHARS else None


def apply_summary(history: List[Message], cut: int, summary: str) -> List[Message]:
    """The leading system messages, then the summary (a user message and a short assistant reply), then the
    messages from ``cut`` on, unchanged."""
    lead = _leading_system(history)
    return (list(history[:lead])
            + [{"role": "user", "content": SUMMARY_TAG + "\n" + summary},
               {"role": "assistant", "content": SUMMARY_ACK}]
            + list(history[cut:]))


def compact(history: List[Message], call: Callable[[str], str], *, threshold: int = THRESHOLD_CHARS,
            keep: int = KEEP_USER_MESSAGES) -> Compaction:
    """Summarise ``history`` when it is over ``threshold`` characters. Never changes the input list: the result
    holds the new history, or the very same list when nothing was done or the summary failed."""
    if history_size(history) <= threshold:
        return Compaction("none", history)
    cut = split_point(history, keep)
    if cut is None:
        return Compaction("none", history)
    lead = _leading_system(history)
    older = history[lead:cut]
    summary = summarise(older, call)
    if summary is None:
        return Compaction("failed", history)
    return Compaction("done", apply_summary(history, cut, summary), len(older), summary)
