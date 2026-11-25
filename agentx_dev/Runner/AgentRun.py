from agentx_dev.Agents import AgentFormattor, AgentCompletion, AgentPrompt
from agentx_dev.ChatModel import BaseChatModel, CostBudgetExceeded
from agentx_dev.Runner.Fallback import ContextWindowTooSmall, FallbackFailed, fails_for_good
from agentx_dev.Runner.Persistence import (
    OUTCOME_ITERATION_LIMIT, OUTCOME_STUCK, BudgetExpired, Persistence, PersistenceMixin, RunBudget, RunStuck,
    compact_history, run_persistent, unrecognized_action_headline,
)
from agentx_dev.Agents.Agent import StandardParser, ToolCall, ToolError
from agentx_dev.Runner.Changes import changed_paths
from agentx_dev.Runner.Trim import (
    coerce_trim_policy as _coerce_trim_policy, cut_newest_result, cut_text, request_chars, trim_to_save,
    trim_tool_results,
)
from agentx_dev.Pictures import ToolContent, content_text, has_pictures, picture_parts, stub_old_pictures
from agentx_dev.Tools import StandardTool, StructuredTool, logger
from typing import Dict, Callable, List, Type, Optional, Any, Sequence, Tuple
from pydantic import BaseModel, Field

import asyncio
import contextvars
import json
import math
import re
import time as _time
import threading as _threading
from dataclasses import dataclass


def _format_tool_for_prompt(tool) -> str:
    """Return a clean, LLM-readable tool description for the system prompt."""
    from agentx_dev.Tools import StructuredTool as ST
    try:
        from agentx_dev.AsyncTools import AsyncStructuredTool as AST
    except ImportError:
        AST = type(None)
    if isinstance(tool, (ST, AST)):
        param_names = list(tool.args_schema.model_fields.keys())
        return f"{tool.description} (required params: {', '.join(param_names)})"
    return tool.description


@dataclass
class CircuitBreakerConfig:
    """How aggressively to trip a tool's circuit breaker.

    ``failure_threshold``: consecutive ToolError returns (or raised
    exceptions) before the breaker trips. Default 5 — small enough to
    catch a sticky bad endpoint quickly, large enough to ride out
    transient blips that retry would handle.

    ``recovery_timeout_sec``: how long the breaker stays open before
    allowing a single half-open probe call. Default 60s — short enough
    to recover quickly when the upstream comes back, long enough to
    avoid hammering a broken service.
    """
    failure_threshold: int = 5
    recovery_timeout_sec: float = 60.0


class CircuitBreaker:
    """Three-state breaker (closed / open / half-open) for one tool.

    State transitions::

        closed --(N consecutive failures)--> open
        open --(recovery_timeout elapsed)--> half_open
        half_open --(success)--> closed
        half_open --(failure)--> open (with fresh timeout)

    Designed for tool dispatch where ``ToolError`` returns count as
    failures. Raised exceptions are also failures (the dispatch path
    wraps them as ToolError anyway, but we count them at the breaker
    level too for robustness).

    Thread-safe via an internal RLock so concurrent agents sharing a
    runner can both update state safely.
    """

    def __init__(self, name: str, config: CircuitBreakerConfig):
        self.name = name
        self.config = config
        self._consecutive_failures = 0
        self._open_until: float = 0.0
        self._lock = _threading.RLock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._consecutive_failures < self.config.failure_threshold:
                return "closed"
            if _time.monotonic() >= self._open_until:
                # Recovery timeout elapsed — allow one probe.
                return "half_open"
            return "open"

    def can_attempt(self) -> bool:
        """True if dispatch should call the underlying tool; False if the
        breaker is open and the call should short-circuit."""
        return self.state != "open"

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.config.failure_threshold:
                self._open_until = _time.monotonic() + self.config.recovery_timeout_sec

    def reset(self) -> None:
        """Manually close the breaker (e.g. after fixing the upstream)."""
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self.state}, "
            f"failures={self._consecutive_failures}/{self.config.failure_threshold})"
        )


# Sentinel used by _unwrap_std_args to signal "call with no positional
# args" (distinct from "call with the value None"). Module-level so
# instance methods and free functions can identify it via `is`.
_NO_ARG = object()


# Set of case-insensitive strings that should be treated as the model's
# "I'm done, return this as the final answer" signal. Exact string
# "Final_Answer" is the canonical form the prompt template teaches;
# the tolerant variants exist because under use_function_calling=True
# the schema doesn't constrain `action` to any specific value, so
# LLMs (especially gpt-4o-mini) emit "Final Answer" (with space) or
# "final_answer" (lowercase) or "finalanswer" (no separator). Any of
# those should end the loop, not get routed to Tool_Runner.
#
# Deliberately excludes bare "answer" or "final" — those are too
# likely to collide with a user-defined tool of the same name.
_TERMINAL_ACTION_VARIANTS = frozenset({
    "final_answer", "finalanswer",
})


class _ParserRecoverableError(Exception):
    """Raised by ``AgentRunner._resolve_parser_step`` when the model's
    text response is not valid JSON AND the regex salvager could not
    extract an action from it.

    The main loop catches this, appends a targeted error observation
    (telling the model exactly how to fix the JSON), and continues the
    loop up to ``max_iterations``. The alternative -- letting
    ``json.JSONDecodeError`` propagate -- crashed the run whenever a
    large ``action_input`` string (a 1200-word markdown draft, an
    escaped code blob) broke the outer envelope's escaping.

    Attributes:
        reason: The ``JSONDecodeError`` message.
        raw_text: The raw assistant text that failed to parse. Kept for
            logging + so callers can debug what the model actually
            emitted.
    """

    def __init__(self, reason: str, raw_text: str):
        self.reason = reason
        self.raw_text = raw_text
        super().__init__(reason)


# Regexes used by _salvage_react_json. Compiled once at import time.
# Action names must look like real tool identifiers -- letters, digits,
# underscore, dot, hyphen only. This rejects the case where malformed
# JSON contains a "value" that could accidentally match as the "action"
# (e.g. an action_input string that happens to start with a quote).
_SALVAGE_ACTION_RE = re.compile(r'"action"\s*:\s*"([A-Za-z_][A-Za-z0-9_.\- ]{0,79})"')
_SALVAGE_THOUGHT_RE = re.compile(r'"Thought"\s*:\s*"([^"]{0,4000})"')
_SALVAGE_ACTION_INPUT_DICT_RE = re.compile(
    r'"action_input"\s*:\s*(\{.*?\})\s*[,}\s]', re.DOTALL
)


def _salvage_react_json(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of ``{Thought, action, action_input}`` from
    an assistant response whose outer JSON envelope failed to parse.

    Handles the common failure mode: the outer envelope is well-formed
    at the ``action`` key level, but a very long ``action_input`` string
    contains unescaped newlines / quotes / backticks (typical when the
    model produced a large markdown draft embedded as a JSON string).

    Strategy — regex the two fixed-shape fields (``action`` is always a
    short token, ``Thought`` is short-to-medium) and try both container
    shapes for ``action_input`` (dict form first, then string form).

    Returns ``None`` when even the ``action`` key cannot be found -- at
    that point the response is beyond salvage and the caller should
    feed the raw parse error back to the model.
    """
    action_m = _SALVAGE_ACTION_RE.search(text)
    if not action_m:
        return None
    action = action_m.group(1)

    thought_m = _SALVAGE_THOUGHT_RE.search(text)
    thought = thought_m.group(1) if thought_m else ""

    # action_input can be a JSON dict or a string. Try dict first.
    dict_m = _SALVAGE_ACTION_INPUT_DICT_RE.search(text)
    action_input: Any = None
    if dict_m:
        try:
            action_input = json.loads(dict_m.group(1))
        except json.JSONDecodeError:
            # Nested dict failed -- fall through to string handling.
            action_input = None
    if action_input is None:
        # String form: take everything between the FIRST quote after
        # "action_input": and the last quote before the closing brace
        # of the envelope. Not perfect but recovers the common case.
        start = text.find('"action_input"')
        if start == -1:
            return {"Thought": thought, "action": action, "action_input": ""}
        colon = text.find(":", start)
        # Find the first quote after the colon
        q1 = text.find('"', colon + 1)
        if q1 == -1:
            return {"Thought": thought, "action": action, "action_input": ""}
        # Find the last quote in the string that likely closes the value.
        # Use the last quote before the closing } of the outer envelope.
        end_brace = text.rfind("}")
        if end_brace == -1 or end_brace <= q1:
            end_brace = len(text)
        q2 = text.rfind('"', q1 + 1, end_brace)
        action_input = text[q1 + 1:q2] if q2 > q1 else ""

    return {"Thought": thought, "action": action, "action_input": action_input}


def _coerce_runner_input(
    value: Any,
    chat_history: Optional[List[Dict[str, str]]] = None,
) -> tuple:
    """Back-compat 2-tuple form of :func:`_coerce_runner_input_media`
    (drops any media found in the trailing user turn)."""
    query, hist, _media = _coerce_runner_input_media(value, chat_history)
    return query, hist


def _coerce_runner_input_media(
    value: Any,
    chat_history: Optional[List[Dict[str, str]]] = None,
    media: Optional[List[Any]] = None,
) -> tuple:
    """Normalize the ``invoke``/``ainvoke``/``stream``/``astream`` input into
    ``(user_input: str, chat_history: Optional[list])``.

    Runner entry points historically required ``(user_input: str, ...,
    chat_history: list | None)``. That's the same silent-shape hazard
    ``BaseChatModel.invoke`` had before 3.0.6 fixed it: someone thinking
    in chat-model shapes calls ``runner.invoke([{"role":"user","content":"hi"}])``,
    the list gets fed to ``prompt.format_map({"user_input": [...]})``,
    and the model sees ``[{'role': 'user', 'content': 'hi'}]`` as raw
    text in its system prompt. No exception, wrong prompt.

    Accepted shapes:
      - ``str``                              → ``(str, chat_history)``
      - ``list[dict]``                       → last user msg splits off as
        query; earlier msgs (including the system if present, though the
        runner will add its own) become the chat_history. If ``chat_history``
        was ALSO passed, they concatenate (positional history first, then
        the list's earlier msgs) — same "both work" contract the ChatModel
        coercion follows.
      - ``dict`` with ``"messages"`` key     → recurses with that list
      - anything else                        → TypeError

    Empty string / message list with no user turn raises TypeError so a
    caller can't accidentally kick off a run with no query.

    Media: returns ``(query, history, media_parts)``. Images / PDFs /
    audio in the trailing user turn's content list are split off into
    ``media_parts`` (the query keeps only its text) and merged with any
    explicit ``media=``. Previously the whole content list was run
    through ``str()``, so an attached image reached the model as a
    base64 wall of text inside the prompt. Earlier turns keep their
    list content intact for the provider translators."""
    from agentx_dev.Media import split_text_and_media, to_media_part
    extra = [to_media_part(m) for m in (media or [])]
    if isinstance(value, str):
        return value, chat_history, extra
    if isinstance(value, dict) and "messages" in value:
        return _coerce_runner_input_media(list(value["messages"]), chat_history, media)
    if isinstance(value, list):
        # Walk backwards for the trailing user turn — that's the query.
        # Everything before it (excluding the system message which the
        # runner assembles itself) becomes the history.
        last_user_idx = None
        for i in range(len(value) - 1, -1, -1):
            m = value[i]
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            raise TypeError(
                "runner.invoke(messages=...) requires at least one "
                "{'role': 'user', ...} message in the list; got none."
            )
        query, found_media = split_text_and_media(value[last_user_idx].get("content", ""))
        prior: List[Dict[str, str]] = []
        for i, m in enumerate(value):
            if i == last_user_idx:
                continue
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            # Drop any system turn — the runner builds its own from the
            # AgentType template + system_addendum + sandbox hint. A
            # caller-supplied system message would either duplicate that
            # or fight it.
            if role in (None, "system"):
                continue
            content = m.get("content")
            if content is None:
                continue
            # Lists (text + media parts) stay lists; the model's
            # translator renders them for its provider.
            if not isinstance(content, (list, str)):
                content = str(content)
            prior.append({"role": role, "content": content})
        # Concat with any explicit chat_history the caller also passed —
        # positional first, then earlier turns from the list, so the list
        # ordering wins for the immediate context.
        merged = list(chat_history) if chat_history else []
        merged.extend(prior)
        return query, (merged if merged else None), found_media + extra
    raise TypeError(
        f"runner input must be str, list of message dicts, or "
        f"{{'messages': [...]}}; got {type(value).__name__}"
    )


def _is_terminal_action(action: str) -> bool:
    """True if ``action`` should end the loop with action_input as the
    final answer. Accepts Final_Answer, Final Answer, final_answer,
    FINAL_ANSWER, finalanswer — any capitalization + underscore/space
    variant. Empty string is not terminal (caller handles that as
    'unrecognized action' via the implicit-final guardrail)."""
    if not action or not isinstance(action, str):
        return False
    normalized = action.strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in _TERMINAL_ACTION_VARIANTS


# A step with no action and no answer (often a reply cut off by the output limit) is asked for again this many
# times in a run before the run ends with "unrecognized action".
EMPTY_STEP_RETRIES = 2
EMPTY_STEP_NOTE = (
    "[framework] error: your last step had no action and no answer; it may have been cut off by the output limit. "
    "Call a tool, or give action=\"Final_Answer\" with your answer in action_input. If the answer was long, give a "
    "shorter one."
)


# How a message the user typed while the agent worked is put into the run (see ``AgentRunner.interjections``).
INTERJECTION_PREFIX = "[The user wrote this while you were working; take it into account from now on]\n"


def _take_interjections(runner: Any, history: List[Dict[str, Any]]) -> List[str]:
    """The messages ``runner.interjections()`` hands over, each added to ``history`` as a user message. A broken
    source gives none: it must never stop the run."""
    source = getattr(runner, "interjections", None)
    if source is None:
        return []
    try:
        texts = [str(text).strip() for text in (source() or [])]
    except Exception:
        logger.warning("interjections source raised; ignored", exc_info=True)
        return []
    texts = [text for text in texts if text]
    for text in texts:
        history.append({"role": "user", "content": INTERJECTION_PREFIX + text})
    return texts


def _is_empty_step(action: Any, action_input: Any) -> bool:
    """True for a step with neither an action nor any answer text."""
    return not str(action or "").strip() and (not action_input or not str(action_input).strip())


# Proactive counterpart to the text-turn nudge. The nudge REACTS after a
# model narrates instead of acting; this clause is injected into the
# system prompt up front so a tool-using agent is told, before its first
# turn, not to narrate in the first place. Only added when the agent
# actually has tools — a tool-less chat agent SHOULD answer in prose, and
# telling it to "call a tool instead" would be nonsense.
_ACT_DONT_ANNOUNCE = (
    "ACT, DON'T ANNOUNCE: when a tool can do what the user asked, CALL THE "
    "TOOL — do not reply \"I'll do X\", \"Let me do X\", or \"just a second\" "
    "and stop, because that ends your turn with the work still undone. Perform "
    "the action through the tool first, then report what you DID, in past "
    "tense (\"I've set your goal to ...\"). Reply in plain prose only when no "
    "tool applies or the task is already complete."
)


def _observation_content(result: Any) -> Any:
    """What goes in the history for a tool result: its text, or (a result with a picture) its content parts, so
    the model is sent the picture itself."""
    return result if has_pictures(result) else str(result)


def _tool_observation_message(
    content: Any,
    *,
    tool_call_id: Optional[str],
    name: str,
) -> Dict[str, Any]:
    """Build the history message that feeds a tool result back to the model.

    Two shapes, chosen by whether a native tool-call handshake exists:

    - **Function-calling mode** (``tool_call_id`` present): the assistant
      turn carried a real ``tool_calls`` block, so we reply with the
      provider-native ``role="tool"`` message that correlates via
      ``tool_call_id``. This is what modern OpenAI requires.

    - **Text mode** (``tool_call_id`` is None): the model emitted plain
      JSON, not a native tool call — there is no id to correlate. The old
      code used ``role="function"`` here, but newer OpenAI models reject
      that role outright ("does not support 'function' with this model")
      and Anthropic never accepted it. Feed the result back as a plain
      ``role="user"`` turn framed as an ``Observation:`` — accepted by
      every provider and model generation, and matching the ReAct
      template's own few-shot convention (``Observation: ...``).
    """
    if tool_call_id:
        return {
            "role": "tool",
            "name": name,
            "content": content,
            "tool_call_id": tool_call_id,
        }
    if has_pictures(content):
        return {"role": "user", "content": [{"type": "text", "text": f"Observation: {content_text(content)}"},
                                            *picture_parts(content)]}
    return {"role": "user", "content": f"Observation: {content}"}


def _text_turn_nudge_message(native: bool, tool_names: str) -> str:
    """The correction fed back when a turn ends with prose and no action.

    Models trained on conversational preambles routinely announce an
    intention instead of acting — "I'll look up your recent scores. Just
    a second!" — and then end the turn. Without this nudge every "no
    action found" branch of the loop promotes that preamble to the final
    answer, so the user gets a promise instead of a result.

    Phrased as a framework message rather than a user turn's worth of
    persona text so it reads as machine feedback and doesn't bleed into
    the model's voice.
    """
    if native:
        return (
            "[framework] Your last turn was plain text with no tool call, so "
            "nothing happened and the user saw nothing. Do not announce what "
            "you are about to do — do it. Call one of these tools now: "
            f"{tool_names}. If you already have everything you need, call "
            "'respond' with the complete final answer."
        )
    return (
        "[framework] Your last turn had no valid action, so nothing happened "
        "and the user saw nothing. Do not announce what you are about to do — "
        f"do it. Set action to one of [{tool_names}] to call a tool, or set "
        'action to "Final_Answer" with your complete reply in action_input.'
    )


def _build_sandbox_hint(perms: Any) -> Optional[str]:
    """Generate a short block describing where the runner is allowed to
    operate on disk. Injected into the system prompt when Permissions
    is passed to AgentRunner so the model knows its own scope without
    every caller having to hand-write "your workspace is X" into every
    task string.

    Only mentions the file-system side. Returns None when there's no
    meaningful sandbox to describe (no workspace, no allowed_paths).
    """
    from pathlib import Path
    workspace = getattr(perms, "workspace", None)
    allowed = getattr(perms, "allowed_paths", None) or []
    if not workspace and not allowed:
        return None

    lines = [
        "FILESYSTEM SANDBOX (auto-enforced by the tools — the paths "
        "here are the ONLY places your file operations are allowed to "
        "reach; the tools will reject anything outside):"
    ]
    if workspace:
        try:
            abs_ws = str(Path(workspace).resolve())
        except Exception:
            abs_ws = str(workspace)
        lines.append(
            f"- Workspace root: {workspace}  (absolute: {abs_ws})"
        )
        lines.append(
            "  Unqualified paths like 'report.md' or 'notes/x.txt' resolve "
            "here automatically — you don't need to prefix them. A "
            "leading slash ('/report.md') also means the workspace root "
            "for the file tools, NOT the filesystem root."
        )
        lines.append(
            "  run_python and run_shell START IN the workspace, so "
            "open('report.md') finds it. Inside Python code do NOT write "
            "'/report.md' -- to Python that is the drive/filesystem root. "
            f"Use 'report.md' or the absolute path {abs_ws}."
        )

    # List other allowed paths that aren't just the workspace.
    other = [p for p in allowed if p and p != workspace]
    if other:
        lines.append(
            "- Additional allowed subtree(s) (usable for reads, and for "
            "writes if write capabilities are granted):"
        )
        for p in other:
            try:
                abs_p = str(Path(p).resolve())
            except Exception:
                abs_p = str(p)
            lines.append(f"    · {p}  (absolute: {abs_p})")

    lines.append(
        "You can discover what's inside a subtree with list_directory "
        "or find_files — you do NOT need the caller to enumerate paths "
        "for you."
    )

    # Project-level guide discovery. AGENTX.md (Claude.md-style file) is
    # a project-scoped instruction sheet the agent should read before
    # acting — the file's contents govern conventions (tool patterns,
    # spawn rules, common failure modes, etc.). We add a POINTER, not
    # the file contents — reading is on-demand so it costs tokens only
    # when the model actually opens it. Searched in workspace first,
    # then each allowed subtree, then process cwd. First hit wins.
    guide_path = _find_project_guide(workspace, allowed)
    if guide_path:
        lines.append("")
        lines.append(
            f"PROJECT GUIDE: an AGENTX.md file exists at {guide_path}. "
            "READ IT FIRST via read_path — it documents the conventions "
            "this codebase expects (tool patterns, sandbox layout, common "
            "failure modes, when to spawn specialists, how to author "
            "custom tools). Following the guide will avoid the anti-"
            "patterns the framework's guards catch."
        )
    return "\n".join(lines)


def _find_project_guide(
    workspace: Optional[str],
    allowed: list,
) -> Optional[str]:
    """Locate an AGENTX.md project guide the runner should point the model
    at. Search order: workspace root, each allowed subtree, then the
    process cwd. Returns the first absolute path found, else None.

    Only checks for `AGENTX.md` (case-sensitive on POSIX, tolerant on
    Windows) — keeps the discovery predictable. Users who want a
    different filename can inject their own reference via
    ``system_addendum``."""
    from pathlib import Path
    candidates: list = []
    if workspace:
        candidates.append(workspace)
    candidates.extend([p for p in (allowed or []) if p and p != workspace])
    candidates.append(".")   # process cwd as last resort
    for base in candidates:
        try:
            guide = Path(base) / "AGENTX.md"
            if guide.is_file():
                return str(guide.resolve())
        except (OSError, PermissionError):
            continue
    return None


# Names of the response formats an agent can be run with. Models sometimes write one of
# these where a tool name belongs; the unknown-tool message says so.
_FORMAT_NAMES = frozenset({"React_", "StandardParser", "ChainOfThought", "ZeroShot",
                           "FewShot", "Instruction_Tuned_"})


NO_ANSWER_TEXT = "The model gave no answer."
NO_ANSWER_OUTCOME = "no_answer"
_CALL_SCHEMA: "contextvars.ContextVar[Any]" = contextvars.ContextVar("agentx_call_schema", default=None)
_TOOL_CALL_ID: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("agentx_tool_call_id", default=None)


def current_tool_call_id() -> Optional[str]:
    """The id of the tool call a tool is running for (function calling and native binding), or None (text mode,
    or outside a tool). A tool that keeps something about its call (agentx's team job) files it under this id."""
    return _TOOL_CALL_ID.get()


def _settle_empty_final(final_answer: Any, output: Any, tool_calls: List[ToolCall]) -> Tuple[Any, Optional[str]]:
    """What an empty final answer becomes: ``(content, outcome override)``.

    A model that ends with nothing (whitespace only) after no tool calls gave no answer: the run ends
    ``"no_answer"``. After tool calls the work was done, so the outcome stays and the content says what was
    called (and which files were written). An answer that is there, or a structured ``output`` to fill, is left alone."""
    blank = final_answer is None or (not final_answer.strip() if isinstance(final_answer, str) else not final_answer)
    if output is not None or not blank:
        return final_answer, None
    if not tool_calls:
        return NO_ANSWER_TEXT, NO_ANSWER_OUTCOME
    names = list(dict.fromkeys(call.name for call in tool_calls))
    note = "The model gave no final answer. It called: " + ", ".join(names)
    paths = changed_paths(tool_calls, ("write_file", "edit_file"))
    return (note + f" (files changed: {', '.join(paths)})" if paths else note), None


# The result _close_open_tool_calls gives a final-answer call: bookkeeping for the provider, nothing the model did.
_FINAL_DELIVERED = "(final answer delivered)"


def _close_open_tool_calls(history: List[Dict[str, Any]]) -> None:
    """Answer a trailing assistant ``tool_calls`` message that has no tool message after it.

    With function calling the model delivers its final answer as a call to the response-format function, and
    the run ends without a tool result for it. A provider rejects the next request when that history is
    resumed ("An assistant message with 'tool_calls' must be followed by tool messages"), so the history a
    run returns always closes the call."""
    last = None
    for index in range(len(history) - 1, -1, -1):
        role = history[index].get("role")
        if role == "assistant":
            last = index
            break
        if role not in ("tool", "function"):
            return
    if last is None:
        return
    calls = history[last].get("tool_calls") or []
    answered = {m.get("tool_call_id") for m in history[last + 1:]}
    for call in calls:
        call_id = call.get("id")
        if call_id and call_id not in answered:
            history.append({
                "role": "tool",
                "name": (call.get("function") or {}).get("name", ""),
                "tool_call_id": call_id,
                "content": _FINAL_DELIVERED,
            })


def _resolve_binding(
    model: Any, use_function_calling: Optional[bool], bind_tools_natively: Optional[bool],
) -> Tuple[bool, bool]:
    """``(use_function_calling, bind_tools_natively)`` for ``model``, from what the caller passed (None: choose).

    Historical default was text mode, but text-mode ReAct is fragile: an ``action_input`` holding a long string
    with unescaped newlines, quotes or code fences breaks ``json.loads`` on the envelope and the loop dies.
    Native function calling lets the SDK handle escaping. So (3.1.7+) None prefers function calling when the
    model actually implements ``call_with_tools``, and text mode otherwise.
    ``bind_tools_natively=None`` asks the model: one that sets ``prefers_native_tools`` (a small local model,
    which fails the ReAct format) is bound natively. An explicit ``use_function_calling=True`` beats that
    preference; an explicit True/False is kept as given. Native binding forces function calling off (they are
    mutually exclusive by design); asking for both raises ``ValueError``."""
    if bind_tools_natively is None:
        bind_tools_natively = (
            bool(getattr(model, "prefers_native_tools", False))
            and use_function_calling is not True
        )
    if use_function_calling is None:
        if bind_tools_natively:
            use_function_calling = False
        else:
            base_cwt = getattr(BaseChatModel, "call_with_tools", None)
            model_cwt = getattr(type(model), "call_with_tools", None)
            use_function_calling = model_cwt is not None and model_cwt is not base_cwt
    if bind_tools_natively and use_function_calling:
        raise ValueError(
            "AgentRunner: use_function_calling and bind_tools_natively "
            "are mutually exclusive. Function-calling routes through the "
            "AgentType parser; native binding skips it. Pick one."
        )
    return use_function_calling, bind_tools_natively


# Failures that end a run by the framework's own rules (time, cost, stuck): never a reason to switch models,
# and never reported as the fallback failing.
_FRAMEWORK_STOPS = (BudgetExpired, CostBudgetExceeded, RunStuck)

# Told to the fallback model when it takes over a run (after the history it inherits).
_FALLBACK_NOTE = (
    "[framework] The earlier model ({from}) stopped: {reason}. You are taking over this task. What it already "
    "did is above; check it and finish the task without redoing steps that are done."
)


# The context window budget (``context_window=``). Requests are measured in characters and the window in tokens:
# a model is assumed to read CHARS_PER_TOKEN characters per token, or fewer once a reply's usage shows it does.
CHARS_PER_TOKEN = 4
REQUEST_SHARE = 0.85            # the share of the window a request may use; the rest is for the answer
SUMMARY_SHARE = 0.5             # the share of the window the summary call's own input may use
_KEEP_RECENT = 2                # messages a summary of the turn keeps (the newest call and its results)


def _usage_calls(model: Any) -> int:
    """How many replies with usage ``model`` has recorded (0 when it keeps no usage)."""
    calls = getattr(getattr(model, "usage", None), "total_calls", 0)
    return calls if isinstance(calls, int) else 0


class _SwitchedToFallback(Exception):
    """Raised by a model call that failed for good after the runner moved to its fallback model. The loop
    yields ``event`` and runs the same step again, in the fallback's mode."""

    def __init__(self, event: Dict[str, str]):
        super().__init__(event.get("reason", ""))
        self.event = event


def _call_name(call: Dict[str, Any]) -> Any:
    return (call.get("function") or {}).get("name")


def _binds_every_call(history: List[Dict[str, Any]], bound: set) -> bool:
    """Whether every assistant tool call in ``history`` is to a function in ``bound``."""
    return all(_call_name(c) in bound for m in history if m.get("role") == "assistant"
               for c in (m.get("tool_calls") or []))


def _join_content(first: Any, second: Any) -> Any:
    """Two messages' contents as one: strings joined by a blank line, otherwise one list of parts."""
    if isinstance(first, str) and isinstance(second, str):
        return "\n\n".join(text for text in (first, second) if text)

    def parts(content: Any) -> List[Any]:
        if isinstance(content, list):
            return list(content)
        return [{"type": "text", "text": content}] if content else []
    return parts(first) + parts(second)


def _carry_over_history(history: List[Dict[str, Any]], bound: set, keep: Optional[int] = None) -> Optional[int]:
    """Make ``history`` valid for a mode that binds only the functions named in ``bound`` (in place), and
    return the new index of the message that was at ``keep`` (None when ``keep`` is None).

    An assistant tool call to any other function becomes the text ``[earlier step] called <name> with <args>``
    and its result the user message ``[earlier result] <text>``, so nothing in the history refers to a tool the
    model cannot see. Valid calls and their results are kept as they are; a result of a call turned to text
    waits until the tool messages answering the calls that were kept have been placed. The bookkeeping result
    of a delivered final answer is dropped. Then consecutive user (or assistant) messages are joined, blank line
    between, because strict chat templates (LM Studio, llama.cpp ``--jinja``) refuse two turns in a row from
    the same role; an assistant message with tool calls only takes in the text before it."""
    pieces: List[Tuple[int, Dict[str, Any]]] = []           # (index in history it came from, message)
    kept_ids: set = set()
    waiting: List[Tuple[int, Dict[str, Any]]] = []
    for index, message in enumerate(history):
        role = message.get("role")
        if role == "tool":
            text = content_text(message.get("content"))
            if message.get("tool_call_id") in kept_ids:
                pieces.append((index, message))
            elif text != _FINAL_DELIVERED:
                waiting.append((index, {"role": "user", "content": f"[earlier result] {text}"}))
            continue
        pieces.extend(waiting)
        waiting = []
        calls = message.get("tool_calls") if role == "assistant" else None
        if not calls:
            pieces.append((index, message))
            continue
        kept = [c for c in calls if _call_name(c) in bound]
        steps = [f"[earlier step] called {_call_name(c) or '?'} with {(c.get('function') or {}).get('arguments', '')}"
                 for c in calls if _call_name(c) not in bound]
        kept_ids.update(c.get("id") for c in kept)
        if not steps:
            pieces.append((index, message))
            continue
        carried = {"role": "assistant", "content": "\n".join([content_text(message.get("content"))] + steps).strip()}
        if kept:
            carried["tool_calls"] = kept
        pieces.append((index, carried))
    pieces.extend(waiting)

    out: List[Dict[str, Any]] = []
    origins: List[set] = []
    for index, message in pieces:
        last = out[-1] if out else None
        role = message.get("role")
        if (last is not None and role in ("user", "assistant") and role == last.get("role")
                and not last.get("tool_calls")):
            joined = {**last, "content": _join_content(last.get("content"), message.get("content"))}
            if message.get("tool_calls"):
                joined["tool_calls"] = message["tool_calls"]
            out[-1] = joined
            origins[-1].add(index)
            continue
        out.append(message)
        origins.append({index})
    history[:] = out
    if keep is None:
        return None
    return next(i for i, came_from in enumerate(origins) if keep in came_from)


class ToolRegistry:
    """Owns tool storage, name lookup, and prompt-block generation.

    Extracted from ``AgentRunner`` / ``AsyncAgentRunner`` so:
      1. The two runners stop duplicating the registration loop.
      2. A future ``Loop`` class (TODO #14 next slice) has a clean object
         to delegate name lookups to instead of reaching into private dicts.
      3. The dispatch helpers in each runner stay where they are — they're
         the next thing to extract, but moving them now would risk breaking
         the cache / observability hooks, so they remain in the runner for
         this slice.

    Accepts any mix of StandardTool, StructuredTool, AsyncStandardTool,
    AsyncStructuredTool. Lookup keys are the tool ``name`` strings, with
    helper predicates ``has`` / ``is_async`` / ``is_structured`` so callers
    don't need to peek at the private buckets.
    """

    def __init__(
        self,
        tools: List,
        *,
        default_timeout_sec: Optional[float] = None,
        circuit_breaker_config: Optional[CircuitBreakerConfig] = None,
    ):
        """Build a registry.

        ``default_timeout_sec`` caps wall-clock time per tool dispatch.
        ``None`` (default) means no timeout for backward compatibility,
        but production deployments should set this to avoid wedging the
        agent loop on a hung tool. Async tools use ``asyncio.wait_for``
        which cancels cleanly; sync tools use a thread + future, which
        returns to the caller on timeout but cannot kill the underlying
        thread (Python limitation) — so a misbehaving sync tool can leak
        a thread but won't block subsequent dispatches.

        Individual tools can override the default via a ``timeout_sec``
        attribute set on the tool instance (StandardTool / StructuredTool
        already accept arbitrary attrs — just do
        ``tool.timeout_sec = 30`` after construction).

        ``circuit_breaker_config`` enables per-tool circuit breakers.
        ``None`` (default) means no breakers — backward compat. When set,
        every registered tool gets its own breaker with these defaults.
        Individual tools can override via ``tool.circuit_breaker`` (a
        ``CircuitBreakerConfig`` instance). A tripped breaker returns a
        ToolError immediately on subsequent calls, until the recovery
        timeout elapses.
        """
        self._tools: List = []
        self.sync_std: Dict[str, Callable] = {}
        self.sync_struct: Dict[str, Dict] = {}
        self.async_std: Dict[str, Callable] = {}
        self.async_struct: Dict[str, Dict] = {}
        self._default_timeout_sec = default_timeout_sec
        self._tool_by_name: Dict[str, Any] = {}
        self._default_breaker_config = circuit_breaker_config
        self._breakers: Dict[str, CircuitBreaker] = {}
        # Duplicate-call detection. Some models (notably gpt-4o-mini)
        # spiral by re-issuing an IDENTICAL tool call dozens of times
        # instead of using the result they already have. We track the
        # last call's (name, args-signature); repeats above the warn
        # threshold get an admonition prepended, and repeats above the
        # stop threshold return a hard error refusing to run the call.
        # Any DIFFERENT call resets both counters.
        self._last_call_sig: Optional[Tuple[str, str]] = None
        self._last_call_repeat_count: int = 0
        self._dup_warn_after: int = 2      # 3rd identical call gets a warning
        self._dup_stop_after: int = 4      # 5th identical call is refused
        for tool in tools:
            self._register_one(tool)

    @staticmethod
    def _args_signature(args: Any) -> str:
        """Deterministic hash-friendly representation of tool args used
        for duplicate detection. Sort keys so {a:1,b:2} matches
        {b:2,a:1}. Falls back to repr() when json.dumps can't handle a
        value — the goal is 'same call twice' detection, not
        cryptographic uniqueness."""
        import json
        try:
            return json.dumps(args, sort_keys=True, default=repr)
        except Exception:
            return repr(args)

    def _check_and_track_duplicate(self, name: str, args: Any):
        """Update the repeat counter for (name, args) and decide whether
        to WARN or REFUSE. Returns:
          - ('ok', None)               → proceed normally
          - ('warn', message)          → run the call, but prepend
                                          ``message`` to the result
          - ('stop', ToolError)         → refuse the call, return the
                                          ToolError instead"""
        sig = (name, self._args_signature(args))
        if sig == self._last_call_sig:
            self._last_call_repeat_count += 1
        else:
            self._last_call_sig = sig
            self._last_call_repeat_count = 1
        n = self._last_call_repeat_count

        if n > self._dup_stop_after:
            return "stop", ToolError(
                f"refused: '{name}' has been called {n} times in a row with "
                "identical arguments. Use the result from the previous call "
                "instead of re-issuing the same request. If you need a "
                "different result, change the arguments; if you have the "
                "data you need, PROCEED to the next step of your task.",
                tool=name,
            )
        if n > self._dup_warn_after:
            return "warn", (
                f"[framework] WARNING: this is repeat call #{n} of '{name}' "
                f"with the same arguments. The result has not changed. "
                f"Do NOT call this again — use the data above and move on. "
                f"One more identical call will be refused.\n\n"
            )
        return "ok", None

    def _register_one(self, tool) -> None:
        from agentx_dev.Tools import StandardTool, StructuredTool
        try:
            from agentx_dev.AsyncTools import AsyncStandardTool, AsyncStructuredTool
        except ImportError:
            AsyncStandardTool = AsyncStructuredTool = type(None)

        if isinstance(tool, StandardTool):
            self.sync_std[tool.name] = tool.func
        elif isinstance(tool, StructuredTool):
            self.sync_struct[tool.name] = {"func": tool.func, "args_schema": tool.args_schema}
        elif isinstance(tool, AsyncStandardTool):
            self.async_std[tool.name] = tool.func
        elif isinstance(tool, AsyncStructuredTool):
            self.async_struct[tool.name] = {"func": tool.func, "args_schema": tool.args_schema}
        else:
            raise TypeError(
                f"Unsupported tool type '{type(tool).__name__}'. "
                "Expected StandardTool / StructuredTool / AsyncStandardTool / AsyncStructuredTool."
            )
        self._tools.append(tool)
        self._tool_by_name[tool.name] = tool

        # Set up a circuit breaker for this tool if either the registry
        # has a default config OR the tool brought its own.
        per_tool_cfg = getattr(tool, "circuit_breaker", None)
        cfg = per_tool_cfg if per_tool_cfg is not None else self._default_breaker_config
        if cfg is not None:
            self._breakers[tool.name] = CircuitBreaker(tool.name, cfg)

    def unregister(self, name: str) -> None:
        """Remove a tool by name (a no-op for unknown names). The runner-level
        ``func`` / ``args`` views point at these dicts, so they follow."""
        self.sync_std.pop(name, None)
        self.sync_struct.pop(name, None)
        self.async_std.pop(name, None)
        self.async_struct.pop(name, None)
        self._tools = [t for t in self._tools if t.name != name]
        self._tool_by_name.pop(name, None)
        self._breakers.pop(name, None)

    def get_breaker(self, name: str) -> Optional[CircuitBreaker]:
        """Inspect a tool's breaker (useful for tests + ops dashboards).
        Returns None if breakers aren't configured for that tool."""
        return self._breakers.get(name)

    @staticmethod
    def _unwrap_std_args(args: Any) -> Any:
        """Decide what to pass to a StandardTool's single-arg func.

        The LLM's tool-call payload arrives here as ``args``. Cases we
        handle so a StandardTool taking one string arg is robust to how
        the model chose to shape its call:

        - None or empty container → sentinel _NO_ARG (caller invokes
          ``func()`` with zero args).
        - Single-key dict like ``{"code": "..."}`` or ``{"task": "..."}``
          → unwrap to the sole value. The model sometimes picks an
          arbitrary key name (especially inside multi_tool_use.parallel
          batches), so a StandardTool that expects one string shouldn't
          crash on the choice of key.
        - Anything else → passthrough as-is.
        """
        if args is None:
            return _NO_ARG
        if isinstance(args, dict):
            if not args:
                return _NO_ARG
            if len(args) == 1:
                return next(iter(args.values()))
            return args
        if isinstance(args, (list, tuple, str, bytes)) and len(args) == 0:
            return _NO_ARG
        return args

    def _timeout_for(self, name: str) -> Optional[float]:
        """Resolve the effective timeout for a tool: per-tool override
        (``tool.timeout_sec``) wins over the registry default."""
        tool = self._tool_by_name.get(name)
        per_tool = getattr(tool, "timeout_sec", None) if tool is not None else None
        if per_tool is not None:
            return float(per_tool)
        return self._default_timeout_sec

    @staticmethod
    def _run_with_sync_timeout(fn, timeout: Optional[float]):
        """Run ``fn()`` with a wall-clock timeout.

        ``None`` → no timeout (just call). Otherwise: submit to a
        single-worker ThreadPoolExecutor and ``.result(timeout=...)``.
        On timeout, raises ``TimeoutError`` (the builtin, NOT
        ``concurrent.futures.TimeoutError``) for a cross-version-friendly
        type the caller can match.

        Caveat: the underlying thread keeps running — Python threads
        can't be killed. Callers handle the timeout as a return; a
        misbehaving tool leaks one thread per timeout.
        """
        if timeout is None:
            return fn()
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool_timeout") as pool:
            future = pool.submit(contextvars.copy_context().run, fn)     # the call id (and schema) go along
            try:
                return future.result(timeout=timeout)
            except _FutTimeout as e:
                raise TimeoutError(f"tool exceeded {timeout}s timeout") from e

    @property
    def tools(self) -> List:
        """All registered tool instances (preserves insertion order)."""
        return list(self._tools)

    @property
    def names(self) -> List[str]:
        return [t.name for t in self._tools]

    def has(self, name: str) -> bool:
        return (
            name in self.sync_std
            or name in self.sync_struct
            or name in self.async_std
            or name in self.async_struct
        )

    def is_async(self, name: str) -> bool:
        return name in self.async_std or name in self.async_struct

    def is_structured(self, name: str) -> bool:
        return name in self.sync_struct or name in self.async_struct

    def prompt_block(self) -> str:
        """LLM-readable bulleted list of every tool with name + description."""
        return "\n".join(f"- {t.name} : {_format_tool_for_prompt(t)}" for t in self._tools)

    def names_block(self) -> str:
        return ", ".join(t.name for t in self._tools)

    def _unknown_tool_message(self, name: str) -> str:
        """What the model reads when it names a tool that does not exist.

        A bare "not found" left models guessing, and some answered with a response-format
        name such as ``React_`` as if it were a tool, so the message lists what IS callable."""
        if self._tools:
            valid = f"Valid tools: {', '.join(self.names)}."
        else:
            valid = "You have no tools in this run."
        if name in _FORMAT_NAMES:
            what = (f"'{name}' is the name of your response format, not a tool; "
                    "do not put it in an action.")
        else:
            what = f"Tool '{name}' not found."
        return f"{what} {valid} To finish, reply with Final_Answer."

    def to_tool_specs(self) -> List[Dict[str, Any]]:
        """Build provider-agnostic tool specs for every registered tool.

        Used when the AgentRunner binds user tools as NATIVE function-
        calling tools (instead of wrapping them inside an AgentType
        parser). Each spec follows the shape ``call_with_tools`` accepts:

            {"name": str, "description": str, "parameters": <JSON Schema>}

        StructuredTool / AsyncStructuredTool use their Pydantic args_schema.
        StandardTool / AsyncStandardTool have no schema, so we synthesize
        a single-string ``input`` parameter — matches how Tool_Runner
        already calls them (one positional string arg).
        """
        specs: List[Dict[str, Any]] = []
        for tool in self._tools:
            if hasattr(tool, "args_schema") and tool.args_schema is not None:
                specs.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.args_schema.model_json_schema(),
                })
            else:
                specs.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {"type": "string", "description": "Tool input"},
                        },
                        "required": ["input"],
                    },
                })
        return specs

    def append(self, tool) -> None:
        """Add a tool after construction (used by AsyncAgentRunner's
        auto-added batch_concurrent tool)."""
        self._register_one(tool)

    # ------------------------------------------------------------------
    # Dispatch — the second slice of #14 / kills #13 in one move.
    # ------------------------------------------------------------------
    # AgentRunner and AsyncAgentRunner each used to carry a ~70-line
    # Tool_Runner method that ran cache + observability hooks around the
    # actual tool call. Both lived inside the runner, both had the same
    # bug surface, and async added a fifth case (the sync runner version
    # didn't know about async tools so it returned a generic
    # "tool not found"). We pull all of that into the registry: it owns
    # the storage, so it owns the dispatch.
    #
    # The runner still owns deciding *which* tool to call (parser /
    # action lookup) and the loop event yields — the registry just runs
    # the chosen call.

    def configure_cache(self, cache, cache_ttl=None) -> None:
        """Attach a cache for dispatch to read/write through."""
        self._cache = cache
        self._cache_ttl = cache_ttl

    @property
    def cache(self):
        return getattr(self, "_cache", None)

    def _tool_fingerprint(self, name: str) -> str:
        """Stable identity of the callable currently registered under
        ``name``.

        The tool cache is a PROCESS-WIDE singleton keyed on
        (tool_name, args), so two runners in one process whose tools
        merely share a name -- ``search``, ``fetch``, ``query``, the
        usual suspects, and routine across Supervisor specialists --
        collided: the second runner was served the first one's result
        and its own function never ran. Fold the implementation's
        module+qualname into the key so same-name/different-impl misses
        while genuinely identical tools still share, including across
        processes for the disk-backed FileCache (which rules out id()).
        """
        for table in (self.sync_std, self.async_std):
            fn = table.get(name)
            if fn is not None:
                break
        else:
            fn = None
            for table in (self.sync_struct, self.async_struct):
                spec = table.get(name)
                if spec is not None:
                    fn = spec.get("func")
                    break
        if fn is None:
            return name
        mod = getattr(fn, "__module__", "?")
        qual = getattr(fn, "__qualname__", getattr(fn, "__name__", "?"))
        return f"{mod}.{qual}"

    def _uncacheable(self, name: str) -> bool:
        """A tool marked ``cacheable = False`` (side effects, or results that depend on the
        caller) always executes."""
        return getattr(self._tool_by_name.get(name), "cacheable", True) is False

    def _cache_get(self, name: str, args):
        cache = getattr(self, "_cache", None)
        if cache is None or self._uncacheable(name):
            return None
        from agentx_dev.Cache import generate_cache_key
        key = generate_cache_key(self._tool_fingerprint(name), args)
        try:
            hit = cache.get(key)
        except Exception as e:
            logger.warning(f"Cache lookup failed for {name}: {e}")
            return None
        if hit is not None:
            logger.info(f"CACHE HIT: {name}")
        return hit

    def _cache_set(self, name: str, args, result) -> None:
        cache = getattr(self, "_cache", None)
        if cache is None or self._uncacheable(name):
            return
        from agentx_dev.Cache import generate_cache_key
        key = generate_cache_key(self._tool_fingerprint(name), args)
        ttl = getattr(self, "_cache_ttl", None)
        try:
            cache.set(key, result, ttl=ttl)
        except Exception as e:
            logger.warning(f"Cache write failed for {name}: {e}")

    def _obs_start(self, name: str, args):
        """Emit a TOOL_CALL_START event if observability is on. Returns the
        event handle (or None) so the caller can pair it with _obs_end.

        Args are scrubbed via redact_secrets so API keys / bearer tokens /
        passwords in tool inputs don't leak into observability hooks
        (which may forward to Datadog/OTel/log files).
        """
        try:
            from agentx_dev.Config import config as _cfg
            from agentx_dev.Observability import observability as _obs, EventType as _ET, redact_secrets as _redact
        except Exception:
            return None
        if not _cfg.observability_enabled:
            return None
        try:
            return _obs.start_event(
                _ET.TOOL_CALL_START,
                data={"tool_name": name, "args": _redact(str(args))[:100]},
            )
        except Exception as e:
            logger.warning(f"Observability start_event failed: {e}")
            return None

    def _obs_end(self, event, result) -> None:
        if event is None:
            return
        try:
            from agentx_dev.Observability import observability as _obs, redact_secrets as _redact
            _obs.end_event(event, data={"result": _redact(str(result))[:100]})
        except Exception as e:
            logger.warning(f"Observability end_event failed: {e}")

    def _normalize_args(self, args, schema):
        """Accept either a JSON string, a dict, or (as a fallback) a bare
        non-JSON string for single-field tools.

        The bare-string fallback covers the common LLM mistake of passing
        ``action_input: "."`` to a tool whose schema is ``{path: str}``
        instead of ``action_input: {"path": "."}``. Without this, the
        framework crashes with ``json.JSONDecodeError`` and the agent
        usually retries with the same broken shape until max_iterations.

        Raises whatever pydantic / json raises so dispatch can wrap it
        as a ToolError.
        """
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                # Fallback: if exactly one required field in the schema,
                # treat the bare string as that field's value. Multi-field
                # schemas re-raise — there's no unambiguous mapping.
                required = [
                    name for name, f in schema.model_fields.items()
                    if f.is_required()
                ]
                if len(required) == 1:
                    return schema(**{required[0]: args})
                raise
        return schema(**args)

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """Strip OpenAI's ``functions.`` prefix if the model emitted it.
        GPT sometimes calls tools as ``functions.list_directory`` instead
        of ``list_directory`` — same intent, different label."""
        if isinstance(name, str) and name.startswith("functions."):
            return name[len("functions."):]
        return name

    def _dispatch_multi_parallel(self, args: Any) -> Any:
        """Handle OpenAI's undocumented ``multi_tool_use.parallel`` meta-tool.

        GPT with function-calling occasionally emits::

            {"name": "multi_tool_use.parallel",
             "input": {"tool_uses": [
                 {"recipient_name": "functions.list_directory",
                  "parameters": {"path": "."}},
                 ...
             ]}}

        Sync dispatch runs each sub-call sequentially. Result is a
        concatenated string with each sub-tool's output labeled so the
        model can tell which produced what.
        """
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return ToolError(
                    "multi_tool_use.parallel got malformed JSON args",
                    tool="multi_tool_use.parallel",
                )
        uses = args.get("tool_uses") if isinstance(args, dict) else None
        if not isinstance(uses, list):
            return ToolError(
                "multi_tool_use.parallel expected {'tool_uses': [...]}",
                tool="multi_tool_use.parallel",
            )
        parts: List[str] = []
        for use in uses:
            recipient = self._normalize_tool_name(use.get("recipient_name", ""))
            params = use.get("parameters", {})
            sub_result = self.dispatch(recipient, params)
            parts.append(f"[{recipient}]\n{sub_result}")
        return "\n\n".join(parts)

    async def _adispatch_multi_parallel(self, args: Any) -> Any:
        """Async sibling — genuine parallelism via asyncio.gather."""
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return ToolError(
                    "multi_tool_use.parallel got malformed JSON args",
                    tool="multi_tool_use.parallel",
                )
        uses = args.get("tool_uses") if isinstance(args, dict) else None
        if not isinstance(uses, list):
            return ToolError(
                "multi_tool_use.parallel expected {'tool_uses': [...]}",
                tool="multi_tool_use.parallel",
            )
        async def _one(use):
            recipient = self._normalize_tool_name(use.get("recipient_name", ""))
            params = use.get("parameters", {})
            return recipient, await self.adispatch(recipient, params)
        pairs = await asyncio.gather(*(_one(u) for u in uses), return_exceptions=True)
        parts: List[str] = []
        for pair in pairs:
            if isinstance(pair, BaseException):
                parts.append(f"[error]\n{pair}")
            else:
                recipient, sub_result = pair
                parts.append(f"[{recipient}]\n{sub_result}")
        return "\n\n".join(parts)

    def dispatch(self, name: str, args) -> Any:
        """Sync dispatch.

        Returns the tool's result, or a ``ToolError`` for any failure path
        (tool not found, validation error, function raised, async tool
        invoked synchronously, **timeout exceeded**). Observability events
        are always paired — every ``_obs_start`` has a matching
        ``_obs_end`` via the try/finally at the bottom, including on the
        ToolError exit paths.

        If a timeout is configured (registry default or per-tool
        ``timeout_sec``) and the call exceeds it, returns a ToolError.
        Sync timeouts use a thread + Future — the timeout returns to the
        caller cleanly, but the underlying thread CANNOT be killed (Python
        limitation), so a hung sync tool leaks one thread for the rest of
        the process. Async tools use ``asyncio.wait_for`` which cancels
        properly. Prefer async tools for anything that might hang.
        """
        # OpenAI-quirk normalization: models sometimes emit
        # 'functions.list_directory' instead of 'list_directory', or the
        # synthetic 'multi_tool_use.parallel' meta-tool for batching
        # calls. Handle both BEFORE the cache lookup so cache keys stay
        # canonical.
        name = self._normalize_tool_name(name)
        if name == "multi_tool_use.parallel":
            return self._dispatch_multi_parallel(args)

        # Duplicate-call guard: refuse or warn before doing any work.
        # Stop-mode short-circuits before the cache lookup so the
        # rejection is loud and the repeat counter doesn't get lost.
        dup_verdict, dup_payload = self._check_and_track_duplicate(name, args)
        if dup_verdict == "stop":
            return dup_payload

        cached = self._cache_get(name, args)
        if cached is not None:
            if dup_verdict == "warn":
                return dup_payload + str(cached)
            return cached

        # Circuit breaker check — if the tool is currently tripped, short-
        # circuit before doing any work or observability. Returns ToolError
        # so the agent loop reacts the same as any other tool failure.
        breaker = self._breakers.get(name)
        if breaker is not None and not breaker.can_attempt():
            return ToolError(
                f"circuit breaker open for '{name}': tripped after "
                f"{breaker.config.failure_threshold} consecutive failures; "
                f"will retry after {breaker.config.recovery_timeout_sec}s",
                tool=name,
            )

        event = self._obs_start(name, args)
        result: Any = None
        timeout = self._timeout_for(name)
        try:
            if name in self.sync_struct:
                spec = self.sync_struct[name]
                try:
                    validated = self._normalize_args(args, spec["args_schema"])
                    result = self._run_with_sync_timeout(
                        lambda: spec["func"](**validated.model_dump()), timeout)
                    logger.info(f"ACTION: {name}, INPUT: {args}, RESULT: {result}")
                except TimeoutError as e:
                    logger.warning(f"Tool '{name}' timed out after {timeout}s")
                    result = ToolError(f"tool '{name}' timed out after {timeout}s", tool=name, cause=e)
                except Exception as e:
                    logger.error(f"Error executing structured tool '{name}': {e}", exc_info=True)
                    result = ToolError(f"executing structured tool '{name}': {e}", tool=name, cause=e)
            elif name in self.sync_std:
                try:
                    func = self.sync_std[name]
                    logger.info(f"ACTION: {name}, INPUT: {args}")
                    call_arg = self._unwrap_std_args(args)
                    result = self._run_with_sync_timeout(
                        lambda: (func() if call_arg is _NO_ARG else func(call_arg)),
                        timeout,
                    )
                    logger.info(f"RESULT: {result}")
                except TimeoutError as e:
                    logger.warning(f"Tool '{name}' timed out after {timeout}s")
                    result = ToolError(f"tool '{name}' timed out after {timeout}s", tool=name, cause=e)
                except Exception as e:
                    logger.error(f"Error executing standard tool '{name}': {e}", exc_info=True)
                    result = ToolError(f"executing standard tool '{name}': {e}", tool=name, cause=e)
            elif name in self.async_std or name in self.async_struct:
                result = ToolError(
                    f"Tool '{name}' is async but dispatch() was called synchronously. "
                    "Use adispatch() from AsyncAgentRunner.",
                    tool=name,
                )
            else:
                logger.warning(
                    f"Tool '{name}' not found. Available tools: {self.names}"
                )
                result = ToolError(self._unknown_tool_message(name), tool=name)

            if not isinstance(result, ToolError):
                self._cache_set(name, args, result)
            # Feed the breaker the outcome AFTER deciding what to return so
            # the result type is settled. Success → reset counter; failure
            # → bump counter and maybe trip.
            if breaker is not None:
                if isinstance(result, ToolError):
                    breaker.record_failure()
                else:
                    breaker.record_success()
            # Prepend the duplicate-call warning if applicable. Only for
            # non-error results — a ToolError already conveys "something
            # went wrong" and the model shouldn't be confused about which
            # message applies.
            if dup_verdict == "warn" and not isinstance(result, ToolError):
                result = (ToolContent([{"type": "text", "text": dup_payload}, *result]) if has_pictures(result)
                          else dup_payload + str(result))
            return result
        finally:
            self._obs_end(event, result)

    async def adispatch(self, name: str, args) -> Any:
        """Async dispatch — handles all four tool types.

        Same shape as ``dispatch`` but ``await``s async tools and falls
        through to the sync buckets for sync tools, so AsyncAgentRunner
        can register a mix of both. Also handles the same OpenAI
        quirks (``functions.`` prefix, ``multi_tool_use.parallel``
        meta-tool) as the sync path.
        """
        name = self._normalize_tool_name(name)
        if name == "multi_tool_use.parallel":
            return await self._adispatch_multi_parallel(args)

        # Duplicate-call guard — mirrors the sync dispatch path.
        dup_verdict, dup_payload = self._check_and_track_duplicate(name, args)
        if dup_verdict == "stop":
            return dup_payload

        cached = self._cache_get(name, args)
        if cached is not None:
            if dup_verdict == "warn":
                return dup_payload + str(cached)
            return cached

        # Circuit breaker check (same semantics as sync dispatch).
        breaker = self._breakers.get(name)
        if breaker is not None and not breaker.can_attempt():
            return ToolError(
                f"circuit breaker open for '{name}': tripped after "
                f"{breaker.config.failure_threshold} consecutive failures; "
                f"will retry after {breaker.config.recovery_timeout_sec}s",
                tool=name,
            )

        event = self._obs_start(name, args)
        result: Any = None
        timeout = self._timeout_for(name)
        try:
            async def _await_with_timeout(coro):
                """asyncio.wait_for so the cancellation propagates cleanly
                into the coroutine. ``None`` timeout means no cap."""
                if timeout is None:
                    return await coro
                return await asyncio.wait_for(coro, timeout=timeout)

            if name in self.async_struct:
                spec = self.async_struct[name]
                try:
                    # batch_concurrent passes a list of requests; the schema
                    # expects them wrapped in {"requests": [...]} — handle here
                    # so callers don't have to special-case this auto-added tool.
                    parsed = args
                    if isinstance(args, str):
                        parsed = json.loads(args)
                    if name == "batch_concurrent" and isinstance(parsed, list):
                        parsed = {"requests": parsed}
                    validated = spec["args_schema"](**parsed)
                    logger.info(f"ASYNC ACTION: {name}, INPUT: {parsed}")
                    result = await _await_with_timeout(spec["func"](**validated.model_dump()))
                    logger.info(f"RESULT: {result}")
                except asyncio.TimeoutError as e:
                    logger.warning(f"Async tool '{name}' timed out after {timeout}s")
                    result = ToolError(f"tool '{name}' timed out after {timeout}s", tool=name, cause=e)
                except Exception as e:
                    logger.error(f"Error executing async structured tool '{name}': {e}", exc_info=True)
                    result = ToolError(f"executing async structured tool '{name}': {e}", tool=name, cause=e)
            elif name in self.async_std:
                try:
                    func = self.async_std[name]
                    logger.info(f"ASYNC ACTION: {name}, INPUT: {args}")
                    call_arg = self._unwrap_std_args(args)
                    result = await _await_with_timeout(
                        func() if call_arg is _NO_ARG else func(call_arg)
                    )
                    logger.info(f"RESULT: {result}")
                except asyncio.TimeoutError as e:
                    logger.warning(f"Async tool '{name}' timed out after {timeout}s")
                    result = ToolError(f"tool '{name}' timed out after {timeout}s", tool=name, cause=e)
                except Exception as e:
                    logger.error(f"Error executing async standard tool '{name}': {e}", exc_info=True)
                    result = ToolError(f"executing async standard tool '{name}': {e}", tool=name, cause=e)
            elif name in self.sync_struct:
                spec = self.sync_struct[name]
                try:
                    validated = self._normalize_args(args, spec["args_schema"])
                    result = self._run_with_sync_timeout(
                        lambda: spec["func"](**validated.model_dump()), timeout)
                    logger.info(f"ACTION: {name}, INPUT: {args}, RESULT: {result}")
                except TimeoutError as e:
                    logger.warning(f"Tool '{name}' timed out after {timeout}s")
                    result = ToolError(f"tool '{name}' timed out after {timeout}s", tool=name, cause=e)
                except Exception as e:
                    logger.error(f"Error executing structured tool '{name}': {e}", exc_info=True)
                    result = ToolError(f"executing structured tool '{name}': {e}", tool=name, cause=e)
            elif name in self.sync_std:
                try:
                    func = self.sync_std[name]
                    logger.info(f"ACTION: {name}, INPUT: {args}")
                    call_arg = self._unwrap_std_args(args)
                    result = self._run_with_sync_timeout(
                        lambda: (func() if call_arg is _NO_ARG else func(call_arg)),
                        timeout,
                    )
                    logger.info(f"RESULT: {result}")
                except TimeoutError as e:
                    logger.warning(f"Tool '{name}' timed out after {timeout}s")
                    result = ToolError(f"tool '{name}' timed out after {timeout}s", tool=name, cause=e)
                except Exception as e:
                    logger.error(f"Error executing standard tool '{name}': {e}", exc_info=True)
                    result = ToolError(f"executing standard tool '{name}': {e}", tool=name, cause=e)
            else:
                logger.warning(
                    f"Tool '{name}' not found. Available tools: {self.names}"
                )
                result = ToolError(self._unknown_tool_message(name), tool=name)

            if not isinstance(result, ToolError):
                self._cache_set(name, args, result)
            if breaker is not None:
                if isinstance(result, ToolError):
                    breaker.record_failure()
                else:
                    breaker.record_success()
            if dup_verdict == "warn" and not isinstance(result, ToolError):
                result = (ToolContent([{"type": "text", "text": dup_payload}, *result]) if has_pictures(result)
                          else dup_payload + str(result))
            return result
        finally:
            self._obs_end(event, result)


# Imports are kept light at module level; initialization is deferred to first
# AgentRunner.__init__() call so `import agentx_dev.Runner.AgentRun` is
# side-effect-free.
from agentx_dev.AutoSetup import get_auto_setup, ensure_initialized
from agentx_dev.Config import config
from agentx_dev.Observability import observability, EventType
from agentx_dev.RunLog import RunLog, _model_name

_auto_setup = None  # populated lazily by _ensure_auto_setup()


def _ensure_auto_setup():
    """Idempotent: initialize auxiliary subsystems the first time a runner needs them."""
    global _auto_setup
    if _auto_setup is None:
        ensure_initialized()
        _auto_setup = get_auto_setup()
    return _auto_setup


def _read_action(parser_instance: BaseModel) -> str:
    """Return the action field regardless of whether the parser uses
    'action' (ReAct/Standard/ZeroShot/FewShot/InstructionTuned) or 'Action'
    (ChainOfThought)."""
    if hasattr(parser_instance, "action"):
        return getattr(parser_instance, "action")
    if hasattr(parser_instance, "Action"):
        return getattr(parser_instance, "Action")
    raise AttributeError(
        f"Parser {type(parser_instance).__name__} has neither 'action' nor 'Action'"
    )


def _read_action_input(parser_instance: BaseModel):
    if hasattr(parser_instance, "action_input"):
        return getattr(parser_instance, "action_input")
    if hasattr(parser_instance, "Action_Input"):
        return getattr(parser_instance, "Action_Input")
    raise AttributeError(
        f"Parser {type(parser_instance).__name__} has neither 'action_input' nor 'Action_Input'"
    )


class AgentRunner(PersistenceMixin):
    """Sync agent runner. Connects a chat model, a prompt template, and a
    set of tools into a reason-act loop.

    Typical use::

        from agentx_dev import AgentRunner, AgentType, Claude, Permissions

        runner = AgentRunner(
            model=Claude(),                             # or GPT()
            agent=AgentType.ReAct,
            tools=[weather_tool],                       # your tools
            permissions=Permissions.full_access(["./workspace"]),
        )
        result = runner.invoke("What's the weather in Paris?")
        print(result.content)

    The runner supports three execution modes, all set via constructor
    flags:

    - **Text mode** (default) -- the model emits JSON matching the
      AgentType parser; the runner parses out the action and dispatches
      the tool. Works with any chat model.
    - **Function-calling mode** (``use_function_calling=True``) -- the
      AgentType parser itself is forwarded as the native FC tool.
      Requires the model to implement ``call_with_tools``.
    - **Native binding mode** (``bind_tools_natively=True``, 3.1) --
      your tools are bound directly as native FC tools; the AgentType
      parser is skipped. Multiple tool_use blocks in one turn dispatch
      concurrently on a bounded thread pool. Mutually exclusive with
      ``use_function_calling``.

    For the async equivalent, see :class:`AsyncAgentRunner`.
    """

    #: ``interjections()``, when set, returns the messages the user sent while the run was going (an empty list
    #: when there are none). It is called before every step; each message is added to the run as
    #: a user message, and ``stream`` yields ``{"type": "interjection", "content": text}`` for it.
    interjections: Optional[Callable[[], Sequence[str]]] = None

    def __init__(
        self,
        model: BaseChatModel,
        Agent: AgentFormattor | AgentPrompt | str | None = None,
        tools: List[StandardTool | StructuredTool] | None = None,
        max_iterations: int | None = None,
        auto_cache: bool = True,
        auto_memory: bool = False,
        use_function_calling: Optional[bool] = None,
        verbose: bool = True,
        bind_tools_natively: Optional[bool] = None,
        parallel_tool_workers: int = 8,
        *,
        agent: AgentFormattor | AgentPrompt | str | None = None,
        permissions: Any = None,
        include_denied_tools: bool = False,
        system_addendum: Optional[str] = None,
        strict_tool_dispatch: bool = False,
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
        persistence: Optional[Persistence] = None,
        delegation: Optional[Any] = None,
        skills: Optional[Any] = None,
        trim_results: Optional[Any] = None,
        log: Any = None,
        fallback_model: Optional[BaseChatModel] = None,
        context_window: Optional[int] = None,
        fallback_addendum: Optional[Any] = None,
        fallback_tools: Optional[Any] = None,
        compact_prompt: Any = False,
    ):
        """Construct an ``AgentRunner``.

        Args:
            model: The chat model backing every LLM call. Any
                ``BaseChatModel`` subclass -- ``Claude()``, ``GPT()``,
                or your own provider adapter.
            Agent: The prompt template + parser pair the runner uses.
                Accepts an ``AgentType`` member
                (``AgentType.ReAct`` / ``AgentType.Chain_of_Thought``
                / ``AgentType.Zero_Shot`` / ``AgentType.Few_Shot`` /
                ``AgentType.Instruction_Tuned``), a custom
                ``AgentFormatter`` instance you built, or a raw
                prompt-template string containing ``{tools}``,
                ``{tool_names}``, and ``{user_input}`` placeholders.
                Aliased as ``agent=`` (lowercase, PEP-8-friendly).
                Passing both ``Agent=`` and ``agent=`` raises
                ``TypeError`` -- pick one.
            tools: Tool instances the LLM can call. Each must be a
                ``StandardTool`` / ``StructuredTool`` /
                ``AsyncStandardTool`` / ``AsyncStructuredTool``.
                Defaults to an empty list. Framework guards apply
                automatically (duplicate-call, timeout, circuit
                breaker, cache, observability). Merged with any
                ``DefaultTools`` produced from ``permissions=`` below;
                collisions raise ``TypeError``.
            max_iterations: Cap on tool-calling cycles per invoke.
                Default 4. Bump for complex multi-step tasks. If the
                loop reaches the cap without a Final_Answer, the
                runner synthesizes a summary of what actually
                happened (last successful tool result + step list)
                so callers never get a bare "No final answer".
            auto_cache: When True (default), tool results are cached
                by ``(name, args)`` signature via
                ``get_global_cache()``. Second identical dispatch is
                a cache hit -- observability emits ``CACHE_HIT``.
                Disable when your tools have side effects
                (``send_email`` / ``write_file``) or time-varying
                outputs (``current_time`` / ``stock_price``).
            auto_memory: When True, the runner instantiates a default
                ``ConversationMemory`` and threads it as chat history
                automatically between invokes. Off by default; most
                real apps prefer explicit ``Session`` + custom
                ``BaseMemory`` (see the memory strategies guide).
            use_function_calling: Route the AgentType parser through
                the model's native tool-calling API (``call_with_tools``)
                instead of parsing JSON out of assistant text. Higher
                accuracy on tool-call correctness AND eliminates a class
                of crashes where a long ``action_input`` string (large
                markdown draft, code blob) breaks ``json.loads`` on the
                response envelope. Mutually exclusive with
                ``bind_tools_natively``.

                Default (3.1.7+): ``None`` -> auto-detect. Set to
                ``True`` if the model class overrides
                ``call_with_tools`` (both ``GPT`` and ``Claude`` do);
                ``False`` if it's still the ``BaseChatModel`` stub, or
                if ``bind_tools_natively=True`` was passed. Pass
                ``True``/``False`` explicitly to override the
                auto-choice. Pre-3.1.7 code that passed nothing
                defaulted to ``False`` (text-mode ReAct); the new
                default matches what most users actually want.
            verbose: Print the running Thought / Action / Observation
                trace to stdout while the loop executes. Convenient
                during development; usually off in production.
                Independent of ``observability_enabled`` (which
                controls structured events, not stdout).
            bind_tools_natively: When True (3.1), the AgentType parser
                is bypassed and your tools are bound directly as
                native function-calling tools. The framework
                auto-registers a synthetic ``respond`` tool the model
                calls to end the loop. Multiple ``tool_use`` blocks
                per turn are dispatched concurrently on a bounded
                ``ThreadPoolExecutor``. Best latency for multi-tool
                turns. Mutually exclusive with
                ``use_function_calling``. Default None: bound natively
                when the model sets ``prefers_native_tools`` (local models).
                Uses a minimal system
                prompt (not the AgentType template) so the parser
                scaffolding doesn't fight the native interface.
            parallel_tool_workers: Max concurrent dispatches per turn
                when ``bind_tools_natively=True``. Default 8. Actual
                worker count is ``min(this, number_of_tool_calls)``.
                Ignored when native binding is off. Only affects
                per-turn parallelism -- separate calls to
                ``runner.invoke`` remain sequential.
            agent: Lowercase PEP-8 alias for ``Agent``. Prefer this
                spelling in new code.
            permissions: A ``Permissions`` instance. When supplied,
                the framework calls ``DefaultTools.build(permissions)``
                and merges the resulting tools into ``tools=``. This
                is how you enable the sandboxed filesystem tools
                (``read_path`` / ``write_file`` / ``run_python``
                etc.) -- denied capabilities aren't registered, so
                the LLM literally cannot call them. Also injects a
                filesystem-sandbox description into the system
                prompt automatically. Name collisions with
                ``tools=`` raise ``TypeError`` at construction.
            include_denied_tools: When True, denied capabilities
                surface as no-op tools that return an error message
                ("this capability is disabled"). Useful for testing
                model behavior against a wider tool surface. Default
                False -- denied tools don't exist to the model.
            system_addendum: A role-specific instruction block
                appended to the AgentType template's system prompt.
                Use to encode a specialist contract
                (e.g. "you MUST call ``write_file`` at the end;
                never just describe the content"). When
                ``permissions=`` is also set, the auto-generated
                sandbox hint is prepended so the role text can
                reference paths. Set this to a lightweight cache
                miss beat -- Anthropic prompt caching kicks in when
                the addendum stays stable across calls.
            strict_tool_dispatch: When True, an unknown ``action``
                that looks like a tool identifier feeds an error
                observation back into the loop
                ("that tool doesn't exist; use one of X or
                Final_Answer") so the model can retry. Default
                False, which routes unknown actions to
                implicit-final. Turn on for models prone to
                misnaming tools (gpt-4o-mini).
            text_turn_nudges: How many times per run the loop may
                re-prompt a model that ended its turn with plain text
                and no tool call. Default 1. Models routinely emit a
                conversational preamble ("I'll look up your scores —
                just a second!") and stop; without a nudge that
                preamble becomes the final answer and the user gets a
                promise instead of a result. Each nudge costs one
                extra LLM call and one iteration, so the budget is
                deliberately small: after it's spent, the text stands
                as the answer. Set to 0 to restore the pre-3.1.4
                behavior (first text turn always terminates the loop).
                Ignored when no tools are registered — there, prose
                genuinely is the answer.
            persistence: Opt-in ``Persistence(...)`` that keeps the run
                working through errors: it reflects and changes approach
                when stuck, compacts a long history, retries transient
                provider errors, and stops on ``max_minutes`` or the
                model's cost cap (``configure_limits(budget_usd=...)``) instead of ``max_iterations``
                (``max_turns`` is the backstop). The completion's
                ``outcome`` says how the run ended. Streamed responses
                (``stream_tokens``) are not retried. Default ``None``
                keeps the ordinary loop.
            delegation: (3.6) An enabled ``SpawnConfig`` gives the runner a
                ``delegate`` tool: it can hand part of its work to a fresh
                sub-agent (clean context, tools clipped to the config's
                ceiling) and get a short summary back. ``runner.spawned``
                lists this run's sub-agents. Default ``None``: no tool.
            skills: ``True`` or a ``Skills(...)``. The runner gets a
                ``use_skill`` tool and a short list of skills (name and
                description) at the end of its system addendum; the model
                reads a skill's full text only when it calls the tool.
                ``True`` uses ``~/.agentx/skills`` and creates the skills
                agentx ships there on first use. The tool list and the
                list of skills are fixed when the runner is built, so the
                prompt stays identical from call to call (prompt-cache
                friendly). Keep the skills folder outside the paths the
                write tools can reach: a skill is instructions the agent
                follows, so an agent that can write one can plant
                instructions for later runs. Default ``None``: nothing
                changes.
            trim_results: ``True`` or a ``TrimPolicy`` (from
                ``agentx_dev.Runner.Trim``). Every request carries the
                whole conversation, so a big tool result is paid for
                again at every later step. With this set, the model is
                sent a one-line stub in place of each big tool result
                outside a window of the newest ones (default: 20,000
                to 40,000 characters, moved in steps so the provider's
                prompt cache can reuse the start of the request, and
                always the newest result; results under
                800 characters and ``use_skill`` results are never
                stubbed; an older word-for-word copy of a later result
                is stubbed too). The saved history is not
                changed. Default ``None``: nothing is trimmed.
            log: See what the runner does while it runs. ``True``
                prints one short line per step to stderr; a file path
                appends to that file (JSON lines for ``.jsonl`` /
                ``.json``, readable lines otherwise); a
                ``logging.Logger`` sends readable lines to your logging
                setup; a function receives each event as a dict. Lines:
                the start, each model call (time, tokens), each tool
                result (ok or error), and the end (outcome, totals).
                Secrets are redacted and long text is cut. See
                ``agentx_dev.RunLog``. Default ``None``: nothing.
            fallback_model: A model (typically ``Local(...)``) that
                takes over when ``model`` fails for good: no credit, a
                rejected key, an unknown model, or no route to the
                provider after the usual retries. The run carries on
                from where it stopped (tool calls already made are not
                made again), in the binding mode the fallback needs,
                with a note telling it what happened; ``stream`` yields ``{"type": "fallback", "from", "to",
                "reason"}`` and a warning is logged. The switch lasts
                for the rest of the runner's life (``fallback_used``
                says so, and every completion carries it as
                ``completion.fallback``) until ``reset_fallback()``.
                A local ``model`` never falls back. If the fallback
                fails too, the run raises ``FallbackFailed``.
                Default ``None``: errors propagate as they are.
            fallback_addendum: The ``system_addendum`` to use once the
                run has moved to ``fallback_model`` (a string, or a
                function that gets the fallback model and returns one,
                so it can be fitted to the fallback's window). The
                sandbox hint and the skill index stay. Default ``None``:
                the same addendum as before the switch.
            fallback_tools: The tools to use once the run has moved to
                ``fallback_model`` (a list, or a function that gets the
                fallback model and returns one, or None for "the same
                tools"), so a small model can get a smaller request. At
                the switch the tools are replaced, the skill index is
                made again for them, and calls in the history to a tool
                the fallback does not have become text. One that fails
                leaves the tools as they were. ``reset_fallback()`` puts
                the runner's own tools back. Default ``None``: the same
                tools.
            compact_prompt: Build requests for a small window: the skill
                index lists at most 20 skills, one short line each
                (``use_skill`` still opens every skill), and with native
                binding the system prompt names the tools without
                repeating their descriptions (their specs carry them).
                ``True``, ``False``, or a function that gets a model and
                says whether, asked for ``model`` when the runner is
                built and for ``fallback_model`` at a switch, so a
                fallback can be compact while the main model is not.
                Default ``False``: requests as they always were.
            context_window: The context window of ``model``, in tokens.
                Default ``None``: the model's own ``context_window``
                (a ``Local`` model knows it after ``check()``). After a
                switch the fallback's own ``context_window`` applies.
                When a window is known, every request is kept to 85% of
                it (the rest is for the answer), measured at 4
                characters a token, or fewer when a reply's token counts
                show the model reads fewer. A longer request is first
                sent with old tool results stubbed (the newest kept
                whole); if that is not enough the history is summarised
                in place; then the newest tool result is cut to the
                room left (its start and end kept). ``stream`` yields
                ``{"type": "context_trimmed", "window", "how"}`` (``how``:
                ``"results"`` or ``"summary"``) the first time either
                happens in a run. When even the instructions and the
                task message do not fit, the request is not sent and
                the run raises ``ContextWindowTooSmall``. No window: the
                requests are sent as they are.

        Raises:
            TypeError: If both ``Agent`` and ``agent`` are passed, if
                ``Agent``/``agent`` is missing, if any item in
                ``tools`` is not a supported tool subclass, or if
                ``permissions`` isn't a ``Permissions`` instance.
            ValueError: If ``use_function_calling`` and
                ``bind_tools_natively`` are both True (mutually
                exclusive modes).
        """
        # PEP 8 alias: prefer lowercase `agent=`. Either argument works; passing
        # both raises so misuse is loud rather than silently ambiguous.
        if Agent is not None and agent is not None:
            raise TypeError("AgentRunner received both 'Agent' and 'agent'; pass only one.")
        if agent is not None:
            Agent = agent
        if Agent is None:
            raise TypeError("AgentRunner missing required argument: 'agent'")
        if tools is None:
            tools = []

        # When permissions= is passed, auto-build DefaultTools and merge
        # them with whatever the user passed in tools=. This is the
        # production-friendly path — no manual import + build dance.
        # Name collisions surface as a clear TypeError so users can't
        # accidentally shadow a default tool (e.g. their own "read_path"
        # silently overriding the sandboxed one).
        # Auto-generated system addendum describing the filesystem
        # sandbox. Populated from Permissions below so the model has
        # explicit knowledge of its workspace + allowed_paths without
        # every user having to hand-write "you can only touch ./X" into
        # every task string.
        auto_sandbox_hint: Optional[str] = None

        if permissions is not None:
            from agentx_dev.DefaultTools import DefaultTools, Permissions
            if not isinstance(permissions, Permissions):
                raise TypeError(
                    f"permissions= must be a Permissions instance, "
                    f"got {type(permissions).__name__}"
                )
            default_tools = DefaultTools.build(
                permissions, include_denied=include_denied_tools,
            )
            user_tool_names = {t.name for t in tools}
            default_tool_names = {t.name for t in default_tools}
            collision = user_tool_names & default_tool_names
            if collision:
                raise TypeError(
                    f"Tool name collision between user tools and DefaultTools: "
                    f"{sorted(collision)}. Rename your tool or drop the "
                    f"capability from permissions to avoid the conflict."
                )
            tools = list(default_tools) + list(tools)
            auto_sandbox_hint = _build_sandbox_hint(permissions)

        # The caller's addendum is kept apart from the parts the runner adds (the sandbox hint before it, the skill
        # index after it), so the fallback's own addendum can take its place at a switch.
        caller_addendum = system_addendum
        skill_index: Optional[str] = None
        self.compact_prompt = compact_prompt
        self._compact = self._compact_for(model)
        self._skills = skills if skills is not None and skills is not False else None
        self._base_tools = list(tools)          # the tools before use_skill (fallback_tools replaces them)
        self._added_tools: List[Any] = []       # tools add_tool registered since (kept at a switch)
        if self._skills is not None:
            # Once, here: the tool list and the skill index stay the same for the life of the runner (until a switch
            # to the fallback brings its own tools or prompt form, see fallback_tools and compact_prompt).
            from agentx_dev.Skills import attach_skills
            tools, skill_index = attach_skills(skills, tools, None, short=self._compact)
            system_addendum = (f"{system_addendum}\n\n{skill_index}" if system_addendum and skill_index
                               else skill_index or system_addendum)
        self._sandbox_hint = auto_sandbox_hint
        self._skill_index = skill_index
        self._caller_addendum = caller_addendum
        self.fallback_addendum = fallback_addendum
        self.fallback_tools = fallback_tools
        self._primary_addendum: Optional[str] = None
        self._primary_caller: Optional[str] = None
        self._primary_setup: Optional[Dict[str, Any]] = None   # the tools and prompt form before a switch

        self.Query = ""
        self.Agent = Agent
        self.tools = tools
        self.max_iterations = max_iterations if max_iterations else 4
        self.model = model
        # What the caller asked for (None: choose), kept so the mode can be chosen again for the fallback model
        # and for the primary after reset_fallback().
        self._binding_asked = (use_function_calling, bind_tools_natively)
        use_function_calling, bind_tools_natively = _resolve_binding(model, *self._binding_asked)
        self.use_function_calling = use_function_calling
        # The model that takes over when `model` fails for good, and the switch once it happened (sticky).
        self.fallback_model = fallback_model
        self.fallback_used: Optional[Dict[str, str]] = None
        # What the fallback is told the earlier model was called (None: its name). A caller that must not let the
        # model's name reach the fallback's answer (agentx's team helpers) sets a neutral label.
        self.fallback_note_label: Optional[str] = None
        self._primary_model: Optional[BaseChatModel] = None
        self.verbose = verbose
        # Native-binding mode: expose the user's tools directly as
        # native function-calling tools to the LLM (skipping the
        # AgentType parser indirection). When the LLM emits multiple
        # tool_use blocks in one turn — the modern GPT/Claude "parallel
        # tool calls" behavior — they are dispatched concurrently on a
        # bounded ThreadPoolExecutor, so a turn with N tool calls costs
        # ~max(latency) instead of sum(latency).
        self.bind_tools_natively = bind_tools_natively
        self.parallel_tool_workers = max(1, int(parallel_tool_workers))
        # Role-specific instructions appended to the system prompt at
        # run time. Use to make a specialist's contract explicit (e.g.
        # "you MUST call write_file, never just describe the content").
        # When Permissions is supplied, an auto-generated sandbox hint
        # (workspace root + allowed subtrees) is prepended so the model
        # doesn't have to be told about its scope in every task string.
        # Composition order — sandbox facts first, role instructions
        # second — so the role text can reference / override the paths.
        if auto_sandbox_hint and system_addendum:
            self.system_addendum = auto_sandbox_hint + "\n\n" + system_addendum
        elif auto_sandbox_hint:
            self.system_addendum = auto_sandbox_hint
        else:
            self.system_addendum = system_addendum

        # When True: on unknown-tool dispatch, feed the model an error
        # observation ("that tool doesn't exist; use one of [X,Y] or
        # Final_Answer") and let the loop iterate rather than
        # immediately falling to implicit-final. Only applies when the
        # unknown action looks like a tool identifier (no spaces) —
        # natural-language actions still go straight to implicit-final
        # since retrying makes no sense there. Default False keeps
        # the 3.0.4 behavior.
        # Declared once at construction instead of on every call. When set,
        # every invoke on this runner coerces its final answer into the
        # schema and puts the validated instance on completion.output.
        # A per-call output_schema= still wins, so one-off shapes stay
        # possible. None keeps the historical behaviour exactly: no
        # coercion, completion.output stays None.
        self.output_schema = output_schema
        self.strict_tool_dispatch = strict_tool_dispatch

        # Budget for re-prompting a model that ends a turn with prose and
        # no tool call. See _nudge_text_only_turn.
        self.text_turn_nudges = max(0, int(text_turn_nudges))

        self.auto_cache = auto_cache and config.caching_enabled
        self.auto_memory = auto_memory and config.memory_enabled
        _setup = _ensure_auto_setup() if (self.auto_cache or self.auto_memory) else None
        self._cache = _setup.get_global_cache() if (_setup and self.auto_cache) else None
        self._memory = _setup.create_memory() if (_setup and self.auto_memory) else None
        self._tool_cache: Dict[str, Any] = {}

        # ToolRegistry owns tool storage, prompt-block generation, and the
        # cache/observability-wrapped dispatch. The runner keeps thin .func /
        # .args dict views so older user code that reaches into runner.func /
        # runner.args keeps working. New code should go through self.registry.
        self.registry = ToolRegistry(self.tools)
        self.registry.configure_cache(self._cache, cache_ttl=config.cache_ttl)
        # After max_iterations and the registry exist: persistent runs override the
        # iteration cap and suspend the tool-result cache (see PersistenceMixin).
        self.persistence = persistence
        self._trim_policy = _coerce_trim_policy(trim_results)
        self.context_window = context_window
        self._chars_per_token: Dict[int, float] = {}     # id(model) -> what its replies' usage showed
        self.log = RunLog.to(log)          # the run log (``log=``); None: nothing is logged
        self._log_run = None               # the run in progress, for the model-call lines
        self.func: Dict[str, Callable] = self.registry.sync_std
        self.args: Dict[str, Dict] = self.registry.sync_struct
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

        if isinstance(Agent, str) and '{tools}' in Agent and '{tool_names}' in Agent and '{user_input}' in Agent:
            self.Agent = AgentFormattor(prompt=Agent, Agent=StandardParser)
            self.parser = self.Agent.Agent
        elif isinstance(Agent, AgentFormattor) and '{tools}' in Agent.prompt and '{tool_names}' in Agent.prompt and '{user_input}' in Agent.prompt:
            logger.debug(f"Formatting prompt from AgentFormattor: {self.Agent.prompt}")
            self.parser = self.Agent.Agent
        elif isinstance(Agent, AgentPrompt) and '{tools}' in Agent.prompt and '{tool_names}' in Agent.prompt and '{user_input}' in Agent.prompt:
            self.Agent = AgentFormattor(prompt=Agent.prompt, Agent=StandardParser)
            self.parser = self.Agent.Agent
        else:
            raise ValueError("The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}, or an AgentFormattor instance.")

        if delegation is not None and delegation.enabled:
            from agentx_dev.SubAgents import SpawnPolicy, make_delegate_tool
            self._delegation = SpawnPolicy(delegation, self.model, persistence=self.persistence,
                                           is_async=False, verbose=self.verbose)
            if delegation.max_depth > 0:
                self.add_tool(make_delegate_tool(self, self._delegation, 1))

    def Tool_Runner(self, tool_name: str, args_str) -> Any:
        """Execute a tool. Delegates to ``self.registry.dispatch``.

        Kept as an instance method for backward compatibility — older user
        code may call ``runner.Tool_Runner(...)`` directly. New callers
        should prefer ``runner.registry.dispatch(...)``.
        """
        return self.registry.dispatch(tool_name, args_str)

    def _resolve_parser_step(self, response_or_call) -> Optional[BaseModel]:
        """
        Convert either a text response or a tool-call dict into a parser
        instance. Returns ``None`` if the response should be treated as
        a final text answer (caller is responsible for surfacing it).

        Raises ``_ParserRecoverableError`` when the text-mode response
        was not valid JSON AND regex salvage failed. The main loop
        catches this and reprompts the model with a targeted fix hint
        instead of crashing the run -- see the try/except block around
        the call site in ``_iter_run``.
        """
        # Function-calling path: dict from call_with_tools
        if isinstance(response_or_call, dict) and "type" in response_or_call:
            if response_or_call["type"] == "tool_use":
                return self.parser.from_function_call(response_or_call["input"])
            return None  # plain text — treat as final answer

        # JSON-text path with recovery. json.JSONDecodeError used to
        # bubble up and crash the whole run whenever a big action_input
        # string (like a 1200-word markdown draft) broke the outer
        # envelope's escaping. Now we (1) try a regex salvage of the
        # {Thought, action, action_input} fields, and (2) if that
        # fails too, hand a _ParserRecoverableError to the caller so
        # the loop can feed the JSON error back to the model as an
        # observation and iterate.
        try:
            parsed = self.parser.from_json(response_or_call)
        except json.JSONDecodeError as e:
            salvaged = _salvage_react_json(response_or_call) \
                if isinstance(response_or_call, str) else None
            if salvaged is not None:
                try:
                    return self.parser(**salvaged)
                except Exception:
                    # Salvage produced fields that don't fit the parser
                    # schema either. Fall through to the retry path.
                    pass
            raise _ParserRecoverableError(str(e), str(response_or_call)) from e
        if isinstance(parsed, self.parser):
            return parsed
        return None  # convert_to_json returned None — text final answer

    def _nudge_text_only_turn(
        self,
        working_history: List[Dict[str, Any]],
        *,
        native: bool,
        budget: int,
        count: int,
        assistant_text: Optional[str] = None,
    ) -> bool:
        """Decide what to do with a turn that produced no tool call.

        Returns True when the caller should append a nudge and ``continue``
        the loop, False when the model's text should stand as the final
        answer. When True, the nudge has already been appended to
        ``working_history``.

        The text stands (returns False) when any of these hold:
          - the nudge budget for this run is spent,
          - one more iteration would blow ``max_iterations`` anyway,
          - no tools are registered — a runner with no tools is a plain
            chat call, and prose IS the answer there; nudging would burn
            a call to be told the same thing again.

        ``assistant_text`` is appended as the assistant turn first. Pass
        None when the caller already recorded it (the JSON-text path does).
        """
        if assistant_text is not None:
            working_history.append({"role": "assistant", "content": assistant_text})
        if budget <= 0 or count >= self.max_iterations or not self.registry.names:
            return False
        working_history.append({
            "role": "user",
            "content": _text_turn_nudge_message(native, self._tool_names_block),
        })
        if self.verbose:
            print(
                "\x1B[1;33m[loop] text-only turn with no tool call; "
                "nudging the model to act instead of announcing\x1B[0m"
            )
        else:
            logger.info("text-only turn with no tool call; nudging")
        return True

    def _feed_back_invalid_tool_args(
        self,
        working_history: List[Dict[str, Any]],
        call_result: Dict[str, Any],
    ) -> None:
        """Handle a ``call_with_tools`` result whose tool arguments were
        malformed JSON (see ``_parse_tool_arguments``). Appends a
        retryable observation so the model can resend the SAME call with
        valid JSON, instead of the run crashing on the unparsed string.

        Records a short assistant placeholder before the framework
        message so the conversation keeps its user/assistant alternation
        — Anthropic rejects two consecutive user turns, and we can't
        reconstruct a valid assistant tool_call from arguments that
        didn't parse.
        """
        name = call_result.get("name", "?")
        error = call_result.get("error", "arguments were not valid JSON")
        working_history.append({
            "role": "assistant",
            "content": f"(my previous call to '{name}' had malformed JSON arguments)",
        })
        working_history.append({
            "role": "user",
            "content": (
                f"[framework] Your previous tool call to '{name}' had arguments "
                f"that were not valid JSON ({error}). This almost always means a "
                f"backslash inside a code string or file path wasn't escaped for "
                f"JSON — write \\\\ for a literal backslash and \\n for a newline "
                f"(e.g. a regex like \\d+ must be sent as \\\\d+). Resend the same "
                f"tool call with valid JSON arguments."
            ),
        })
        if self.verbose:
            print(
                f"\x1B[1;33m[loop] tool args for '{name}' were malformed JSON; "
                f"feeding the error back so the model can resend\x1B[0m"
            )
        else:
            logger.info(f"malformed tool args for '{name}'; feeding error back")

    def _model_view(self, messages):
        """What the model is sent for ``messages``: the same list, or (``trim_results=``) a copy in which old big
        tool results are stubs. The history itself is never changed."""
        trimmed = trim_tool_results(messages, self._trim_policy) if self._trim_policy is not None else messages
        return stub_old_pictures(trimmed)

    def _window(self) -> Optional[int]:
        """The window of the model in use, in tokens, or None: ``context_window=`` for the model the runner was
        built with; after a switch, the fallback's own ``context_window``."""
        window = self.context_window if self.fallback_used is None else None
        if window is None:
            window = getattr(self.model, "context_window", None)
        return window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else None

    def _summary_cap(self) -> Optional[int]:
        """The most characters a summary call's input may have (half the window), or None without a window."""
        window = self._window()
        if window is None:
            return None
        return int(window * self._chars_per_token.get(id(self.model), CHARS_PER_TOKEN) * SUMMARY_SHARE)

    def _fit_window(
        self, history: List[Dict[str, Any]], task_index: int, specs: Optional[List[Any]], failed_text: str = "",
        moved: Callable[[int], None] = lambda index: None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[int]]:
        """What the model is sent for ``history`` (``_model_view``), kept inside its window. Returns
        ``(messages, how, chars)``. ``how``: None, ``"results"`` (tool results are stubbed or cut in what is
        sent) or ``"summary"`` (the history was summarised in place; ``moved`` is told the task message's new
        index when that moves it). ``chars``: the size of the request, None when no window is known (the view is
        then returned as it is). ``specs``: the function specs sent with the messages, which take room too.

        In this order, each only when the one before was not enough: the oldest tool results are stubbed (the
        newest kept whole); the turn so far, then the conversation before it, are summarised; the newest tool
        result is cut to the room left (its start and end kept). Raises ``ContextWindowTooSmall`` when the system
        message and the task message alone do not fit, or when nothing more can be cut."""
        view = self._model_view(history)
        window = self._window()
        if window is None:
            return view, None, None
        per_token = self._chars_per_token.get(id(self.model), CHARS_PER_TOKEN)
        budget = int(window * REQUEST_SHARE * per_token)
        fixed = len(json.dumps(specs, default=str)) if specs else 0

        def size(messages: List[Dict[str, Any]]) -> int:
            return fixed + request_chars(messages)

        def fitted(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """``messages``, or a copy with the oldest tool results stubbed until it fits (the newest kept whole)."""
            if size(messages) <= budget:
                return messages
            messages = trim_to_save(messages, size(messages) - budget)
            return messages if size(messages) <= budget else trim_to_save(messages, size(messages))

        def too_small(chars: int) -> ContextWindowTooSmall:
            return ContextWindowTooSmall(window, math.ceil(chars / per_token))

        task = view[task_index: task_index + 1]
        core = size(view[:1] + task)
        if core > budget:
            raise too_small(core)
        before = size(view)
        if before <= budget:
            return view, None, before
        view = fitted(view)
        how = "results" if size(view) < before else None
        if size(view) > budget:
            cap = self._summary_cap()

            def summarize(text: str) -> str:
                return self.model.Initialize(messages=[{"role": "user", "content": cut_text(text, cap)}])

            # The turn so far, then the conversation before it; the newest messages stay as they are.
            if compact_history(history, task_index=task_index, keep_recent=_KEEP_RECENT, summarize=summarize,
                               failed_text=failed_text):
                how, view = "summary", fitted(self._model_view(history))
            if size(view) > budget and task_index > 1:
                earlier = history[1:task_index]
                if compact_history(earlier, task_index=0, keep_recent=0, summarize=summarize):
                    history[1:task_index] = earlier
                    task_index = 1 + len(earlier)
                    moved(task_index)
                    how, view = "summary", fitted(self._model_view(history))
        if size(view) > budget:
            cut = cut_newest_result(view, size(view) - budget)
            if cut is None or size(cut) > budget:
                raise too_small(size(view))
            how, view = how or "results", cut
        return view, how, size(view)

    def _learn_chars_per_token(self, calls_before: int, chars: Optional[int]) -> None:
        """After a call that sent ``chars`` characters: when the reply reported its prompt tokens (cached ones
        included), keep the characters per token it shows for the model in use. Only ever a stricter budget: a
        ratio above ``CHARS_PER_TOKEN`` (a server that leaves cached tokens out, say) is not trusted."""
        if not chars or _usage_calls(self.model) <= calls_before:
            return
        usage = self.model.usage
        # OpenAI-style usage (GPT, and local servers through it) counts cached tokens inside the prompt total and
        # gives them again apart; Anthropic's input count leaves them out. Adding them twice halved the window.
        parts = (("last_input_tokens",) if getattr(self.model, "input_tokens_include_cached", False) else
                 ("last_input_tokens", "last_cache_read_tokens", "last_cache_creation_tokens"))
        tokens = sum(value for value in (getattr(usage, name, 0) for name in parts) if isinstance(value, int))
        if tokens > 0 and chars / tokens >= 1.0:
            self._chars_per_token[id(self.model)] = min(chars / tokens, CHARS_PER_TOKEN)

    def add_tool(self, tool) -> None:
        """Register ``tool`` on a built runner (used to attach ``delegate`` for the
        duration of a Supervisor run). The caller's original tool list is not mutated."""
        if self.registry.has(tool.name):
            raise ValueError(f"tool '{tool.name}' is already registered")
        self.registry._register_one(tool)
        self.tools = list(self.tools) + [tool]
        self._added_tools.append(tool)
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    def remove_tool(self, name: str) -> None:
        """Remove a tool by name (a no-op for unknown names)."""
        self.registry.unregister(name)
        self.tools = [t for t in self.tools if t.name != name]
        self._added_tools = [t for t in self._added_tools if t.name != name]
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    def _set_tools(self, tools: Sequence[Any]) -> None:
        """Register exactly ``tools`` (in place of the ones registered now), keeping the registry's own settings."""
        for name in list(self.registry.names):
            self.registry.unregister(name)
        for tool in tools:
            self.registry._register_one(tool)
        self.tools = list(tools)
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    def _output_wanted(self) -> Any:
        """The output schema of the running call (per-call, else the runner's own), or None."""
        call = _CALL_SCHEMA.get()
        return call if call is not None else self.output_schema

    def _native_specs(self) -> List[Dict[str, Any]]:
        """The function specs native binding sends: the user's tools plus a synthetic "respond" tool the LLM calls to
        signal completion."""
        specs = list(self.registry.to_tool_specs())
        specs.append({
            "name": "respond",
            "description": (
                "Return the final answer to the user. Call this when you "
                "have all the information you need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "The final answer."},
                },
                "required": ["answer"],
            },
        })
        return specs

    def fixed_request_chars(self, model: Optional[BaseChatModel] = None) -> int:
        """The characters every request on ``model`` (default: the runner's own) carries before any conversation:
        the system prompt for the binding mode that model gets, and the function specs that mode sends. Divided by
        about 4, it is the part of the model's window the conversation can never use. The runner is not changed."""
        use_function_calling, native = _resolve_binding(self.model if model is None else model,
                                                        *self._binding_asked)
        if native:
            specs: Optional[List[Any]] = self._native_specs()
        elif use_function_calling:
            specs = [self.Agent.to_tool_spec()]
        else:
            specs = None
        system = self._system_prompt("", native=native)
        return len(system) + (len(json.dumps(specs, default=str)) if specs else 0)

    def _system_prompt(self, user_input: str, native: Optional[bool] = None) -> str:
        """The system prompt for the current binding mode (``native``: for native binding or not instead)."""
        if self.bind_tools_natively if native is None else native:
            # In native binding mode, the LLM sees the user's tools as
            # real function-calling tools -- the AgentType template's
            # "emit action/action_input JSON" scaffolding would fight the
            # native tool interface, and models often produce a garbled
            # mix of both. Use a minimal system prompt instead: brief
            # role, tool list for context, and the caller's addendum.
            # A compact request (compact_prompt=) names the tools only: their specs carry the descriptions.
            listed = self._tool_names_block if self._compact else self._tool_prompt_block
            system_prompt = (
                "You are an autonomous assistant. You have the following "
                "tools available (called via the platform's native tool "
                "interface -- do NOT emit JSON with 'action' / "
                "'action_input' fields; the tools are already bound):\n"
                f"{listed}\n\n"
                "Call the 'respond' tool with your final answer when you "
                "have enough information."
            )
        else:
            system_prompt = self.Agent.prompt.format_map({
                'tools': self._tool_prompt_block,
                'tool_names': self._tool_names_block,
                'user_input': user_input,
            })
        # Proactively discourage narrate-instead-of-act, but only for
        # agents that actually have tools. Placed before system_addendum
        # so a caller's role instructions still get the final word.
        if self.registry.names:
            system_prompt = system_prompt + "\n\n" + _ACT_DONT_ANNOUNCE
        if self.system_addendum:
            # Append after the template's own instructions so the
            # role-specific rules read as an override / final word.
            system_prompt = system_prompt + "\n\n" + self.system_addendum
        return system_prompt

    def _bound_function_names(self) -> set:
        """The functions the current mode binds: the user's tools and ``respond`` natively, the parser's
        response-format function with function calling, none in text mode."""
        if self.bind_tools_natively:
            return set(self.registry.names) | {"respond"}
        if self.use_function_calling:
            return {self.parser.__name__}
        return set()

    def _use_model(self, model: BaseChatModel) -> None:
        """Run on ``model`` from now on: its binding mode, and the helpers ``delegate`` builds."""
        self.model = model
        self.use_function_calling, self.bind_tools_natively = _resolve_binding(model, *self._binding_asked)
        delegation = getattr(self, "_delegation", None)
        if delegation is not None:
            delegation.model = model

    def _switch_to_fallback(
        self, reason: str, history: List[Dict[str, Any]], user_input: str, task_index: Optional[int] = None,
    ) -> Tuple[Dict[str, str], Optional[int]]:
        """Move the run to ``fallback_model``: its binding mode, the system prompt for that mode, a history valid
        for it (in place) and a note telling it what happened. Returns the ``fallback`` event and the new index
        of the task message (the history may get shorter)."""
        used = {"from": _model_name(self.model), "to": _model_name(self.fallback_model), "reason": reason}
        logger.warning("%s failed (%s); the run continues on the fallback %s", used["from"], reason, used["to"])
        self._primary_model = self.model
        self._use_model(self.fallback_model)
        self.fallback_used = used
        self._use_fallback_tools()
        self._use_fallback_addendum()
        history[0] = {"role": "system", "content": self._system_prompt(user_input)}
        history.append({"role": "user", "content": _FALLBACK_NOTE.format(
            **{**used, "from": self.fallback_note_label or used["from"]})})
        task_index = _carry_over_history(history, self._bound_function_names(), task_index)
        return {"type": "fallback", **used}, task_index

    def _compose_addendum(self, addendum: Optional[str]) -> Optional[str]:
        """The whole addendum for a caller's ``addendum``: the sandbox hint, it, then the skill index."""
        parts = [part for part in (self._sandbox_hint, addendum, self._skill_index) if part]
        return "\n\n".join(parts) if parts else addendum

    def _compact_for(self, model: Any) -> bool:
        """Whether requests on ``model`` are compact (``compact_prompt=``). A function that fails says no."""
        wanted = self.compact_prompt
        if not callable(wanted):
            return bool(wanted)
        try:
            return bool(wanted(model))
        except Exception:
            logger.warning("compact_prompt could not be asked; the request is the full one", exc_info=True)
            return False

    def _use_fallback_tools(self) -> None:
        """At the switch: the tools ``fallback_tools`` gives the fallback, and the prompt form ``compact_prompt`` says
        for it. The skill index is made again for them; the tools ``add_tool`` registered stay. Nothing changes when
        neither differs, and a ``fallback_tools`` that fails (or returns None) leaves the tools as they are."""
        compact = self._compact_for(self.fallback_model)
        wanted, base = self.fallback_tools, None
        if wanted is not None:
            try:
                base = wanted(self.fallback_model) if callable(wanted) else wanted
                base = None if base is None else list(base)
            except Exception:
                logger.warning("the fallback's tools could not be chosen; the run keeps its own", exc_info=True)
                base = None
        if base is None and compact == self._compact:
            return
        base = list(self._base_tools) if base is None else base
        tools, index = base, None
        if self._skills is not None:
            from agentx_dev.Skills import attach_skills
            try:
                tools, index = attach_skills(self._skills, base, None, short=compact)
            except Exception:                         # a tool named use_skill among them, say: keep what there is
                logger.warning("the fallback's tools could not be set up; the run keeps its own", exc_info=True)
                return
        self._primary_setup = {"tools": self.tools, "base": self._base_tools, "index": self._skill_index,
                               "compact": self._compact, "caller": self._caller_addendum,
                               "addendum": self.system_addendum}
        self._set_tools([*tools, *self._added_tools])
        self._base_tools, self._skill_index, self._compact = base, index, compact
        self.system_addendum = self._compose_addendum(self._caller_addendum)

    def _use_fallback_addendum(self) -> None:
        """At the switch: ``fallback_addendum`` (fitted to the fallback, when it is a function) replaces the caller's
        addendum. One that fails leaves the addendum as it was."""
        wanted = self.fallback_addendum
        if wanted is None:
            return
        try:
            text = wanted(self.fallback_model) if callable(wanted) else wanted
        except Exception:
            logger.warning("the fallback's addendum could not be built; the run keeps its own", exc_info=True)
            return
        if text is not None and not isinstance(text, str):
            logger.warning("the fallback's addendum is not text (%s); the run keeps its own", type(text).__name__)
            return
        if self._primary_setup is None:            # else the switch of tools keeps what was there
            self._primary_addendum, self._primary_caller = self.system_addendum, self._caller_addendum
        self._caller_addendum = text
        self.system_addendum = self._compose_addendum(text)

    def reset_fallback(self) -> None:
        """Go back to the model the runner was built with (and its binding mode, its tools and its addendum) after a
        fallback took over."""
        if self._primary_model is not None:
            self._use_model(self._primary_model)
            self._primary_model = None
        setup, self._primary_setup = self._primary_setup, None
        if setup is not None:
            kept = [t for t in self._added_tools if not any(t is p for p in setup["tools"])]
            self._set_tools([*setup["tools"], *kept])
            self._base_tools, self._skill_index, self._compact = setup["base"], setup["index"], setup["compact"]
            self._caller_addendum, self.system_addendum = setup["caller"], setup["addendum"]
        if self._primary_addendum is not None:
            self.system_addendum, self._caller_addendum = self._primary_addendum, self._primary_caller
            self._primary_addendum = None
        self.fallback_used = None

    def _mark_fallback(self, events):
        """Pass ``events`` on, recording the switch on the completion of a run done on the fallback."""
        try:
            for event in events:
                if event.get("type") == "completion" and self.fallback_used is not None:
                    event["completion"].fallback = dict(self.fallback_used)
                yield event
        finally:
            events.close()

    def _iter_run(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ):
        """Run the agent loop as a generator of step events (event types are
        listed in ``_iter_run_core``). With ``persistence`` set the loop runs
        under a ``PersistentRun``: a time or cost limit, or an exhausted
        reflection ladder, ends the run with a normal completion whose
        ``outcome`` says why."""
        self._begin_delegation_run()
        if self.persistence is None:
            events = self._iter_run_core(user_input, chat_history, stream_tokens, media, None)
        else:
            events = run_persistent(self, user_input, chat_history, stream_tokens, media, _budget)
        events = self._mark_fallback(events)
        log = self.log
        if log is None:
            yield from events
            return
        record = log.begin(user_input, model=self.model, agent=getattr(self, "agent_type", None))
        self._log_run = record
        try:
            yield from record.follow(events)
        finally:
            if self._log_run is record:
                self._log_run = None

    def _iter_run_core(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
        state: Any = None,
    ):
        """Run the agent loop as a generator of step events.

        Yields a dict per step with a ``"type"`` discriminator. The final
        event is always ``{"type": "completion", "completion": <AgentCompletion>}``
        so ``Initialize`` can ``return`` it and ``stream`` can hand the same
        sequence to a consumer in real time.

        Event types:
          - ``{"type": "text_delta", "content": str}``  (only when stream_tokens=True
            and the LLM emits text in JSON-text mode)
          - ``{"type": "thought",     "content": str}``
          - ``{"type": "reasoning",   "content": str}``  (3.8: the model's readable reasoning, when the model
            returns it, before that step's call or answer)
          - ``{"type": "tool_call",   "name": str, "args": Any}``
          - ``{"type": "tool_result", "name": str, "result": str, "is_error": bool}``
          - ``{"type": "final",       "content": str}``
          - ``{"type": "completion",  "completion": AgentCompletion}``  (last)

        ``stream_tokens=True`` routes the LLM call through
        ``model.stream_text(...)`` and emits a ``text_delta`` event for
        each chunk. The chunks accumulate into the full response that
        then drives the rest of the loop (parser / tool dispatch), so
        callers get *both* live tokens AND the structured step events.
        Falls back to the single-yield default for non-streaming models.
        """
        agent_event = None
        if config.observability_enabled:
            agent_event = observability.start_event(
                EventType.AGENT_START,
                data={"query": user_input[:100]}
            )
        if state is not None:
            # So run_persistent can end the event when an early exit unwinds this generator.
            state.agent_event = agent_event

        self.Query = user_input

        working_history: List[Dict[str, str]] = [{"role": "system", "content": self._system_prompt(user_input)}]

        effective_history = chat_history
        if self.auto_memory and self._memory and effective_history is None:
            effective_history = self._memory.get_messages()

        if effective_history and isinstance(effective_history, list):
            # Copy prior turns faithfully enough to be replayable. The old
            # filter kept only truthy {role, content}, which silently broke
            # every round-trip of a previous run's completion.history:
            #   * assistant tool-calling turns carry content="" -> dropped,
            #     so the agent lost all memory of its own actions;
            #   * role="tool" messages lost tool_call_id/name -> providers
            #     reject an orphaned tool message;
            #   * a stored system prompt was replayed as history on top of
            #     the freshly built one -> two contradictory system turns.
            for r in effective_history:
                role = r.get('role')
                if not role:
                    continue
                # The runner builds its own system prompt above; a system
                # message arriving as history is a leftover from a prior
                # completion.history and must not be duplicated.
                if role == 'system':
                    continue
                msg = {'role': role, 'content': r.get('content') or ''}
                if r.get('tool_calls'):
                    msg['tool_calls'] = r['tool_calls']
                if role == 'tool':
                    if r.get('tool_call_id'):
                        msg['tool_call_id'] = r['tool_call_id']
                    if r.get('name'):
                        msg['name'] = r['name']
                # Keep a turn only if it carries text OR a tool-call block;
                # a bare empty message is noise either way.
                if msg['content'] or msg.get('tool_calls') or role == 'tool':
                    working_history.append(msg)
            # A history saved by an older version can end with an unanswered final call.
            _close_open_tool_calls(working_history)

        # Text-only turns stay a plain string; with media the turn becomes
        # [text, image, ...] parts, rendered per provider by the model.
        from agentx_dev.Media import user_content
        working_history.append({"role": "user",
                                "content": user_content(user_input, media)})
        task_index = len(working_history) - 1
        # A history from another mode (a function-calling conversation resumed on a natively bound local model,
        # say) carries calls to functions this mode does not bind: carry them over as text. A history that is
        # valid for this mode is left exactly as it is.
        bound = self._bound_function_names()
        if not _binds_every_call(working_history, bound):
            task_index = _carry_over_history(working_history, bound, task_index)

        if not isinstance(self.model, BaseChatModel):
            raise TypeError(
                f"The 'model' object must inherit from BaseChatModel, "
                f"but got type {type(self.model).__name__}."
            )

        def _bound_specs():
            """The function specs the current mode sends: the parser's (function calling), or the user's tools
            plus a synthetic "respond" tool the LLM calls to signal completion (native binding)."""
            parser_spec = self.Agent.to_tool_spec() if self.use_function_calling else None
            parser_name = self.parser.__name__ if self.use_function_calling else None
            native_specs = self._native_specs() if self.bind_tools_natively else None
            return parser_spec, parser_name, native_specs

        parser_tool_spec, parser_tool_name, native_tool_specs = _bound_specs()

        count = 1
        tool_calls: List[ToolCall] = []
        steps: List[str] = []
        final_answer: Optional[str] = None
        outcome = "done"
        if state is not None:
            state.bind(working_history, tool_calls, steps, task_index)

        def _take_over(error: Exception) -> Dict[str, str]:
            """Handle a model call that raised ``error`` (after the model's own retries): move the run to the
            fallback model and return the ``fallback`` event, or raise."""
            nonlocal parser_tool_spec, parser_tool_name, native_tool_specs, task_index
            if isinstance(error, _FRAMEWORK_STOPS):
                raise error
            if self.fallback_used is not None and self.model is self.fallback_model:
                raise FallbackFailed(self.fallback_used, error) from error
            reason = fails_for_good(error)
            if (reason is None or self.fallback_model is None or self.fallback_used is not None
                    or getattr(self.model, "is_local", False)):
                raise error
            event, task_index = self._switch_to_fallback(reason, working_history, user_input, task_index)
            if state is not None:
                state.task_index = task_index                 # the history may have got shorter
            parser_tool_spec, parser_tool_name, native_tool_specs = _bound_specs()
            return event

        request_chars_sent: Optional[int] = None      # the size of the request being sent; None: no window
        trimmed_told = False

        def _request(specs):
            """What the model is sent this step (with ``specs``), kept inside its window. Yields the
            ``context_trimmed`` event the first time old results are stubbed or the history summarised in a run."""
            nonlocal request_chars_sent, trimmed_told

            def moved(index: int) -> None:                # a summary moved the task message
                nonlocal task_index
                task_index = index
                if state is not None:
                    state.task_index = index

            failed = state.ledger.render_failed() if state is not None and self._window() is not None else ""
            messages, how, request_chars_sent = self._fit_window(working_history, task_index, specs, failed, moved)
            if how is not None and not trimmed_told:
                trimmed_told = True
                yield {"type": "context_trimmed", "window": self._window(), "how": how}
            return messages

        def _model_call(fn):
            # Persistent runs wait out transient provider errors (429/5xx/timeouts).
            call = (lambda: state.patient(fn)) if state is not None else fn
            record = self._log_run
            calls_before = _usage_calls(self.model)
            try:
                result = record.model_call(call) if record is not None else call()
            except Exception as error:
                # The loop catches this, yields the event and runs the step again on the fallback.
                raise _SwitchedToFallback(_take_over(error)) from error
            self._learn_chars_per_token(calls_before, request_chars_sent)
            return result

        # Loop-level circuit breaker. The tool-layer dup-guard refuses
        # on the 5th identical call, but a stubborn model (gpt-4o-mini
        # is prone to this) keeps re-issuing anyway — the refuse
        # ToolError is just another message in the history, and the
        # model doesn't back off. Also fires for "tool not found"
        # spirals where the model repeatedly calls a tool it doesn't
        # have. Track consecutive identical (action, args) pairs at
        # the loop level and force-terminate after the threshold with
        # a synthesized Final_Answer built from prior successful tool
        # results.
        last_action_sig: Optional[str] = None
        consecutive_identical_actions = 0
        # Persistent runs use the stuck tracker (reflect, then end) instead of this abort.
        LOOP_FORCE_STOP = 3 if state is None else 10 ** 9

        # Remaining re-prompts for turns that produce prose but no action.
        nudge_budget = self.text_turn_nudges
        empty_steps = 0

        while count <= self.max_iterations:
            for text in _take_interjections(self, working_history):
                yield {"type": "interjection", "content": text}
            if state is not None:
                state.before_turn(self.model, max_input_chars=self._summary_cap())
                yield from state.drain()
            if self.bind_tools_natively:
                # Native mode: LLM picks from the user's tools directly.
                # Multiple tool_use blocks in one turn → concurrent dispatch
                # via ThreadPoolExecutor. Synthetic "respond" tool ends the loop.
                request = yield from _request(native_tool_specs)
                try:
                    call_result = _model_call(lambda: self.model.call_with_tools(
                        messages=request,
                        tools=native_tool_specs,
                        force_tool=None,
                    ))
                except _SwitchedToFallback as switched:
                    yield switched.event
                    continue                              # the same step, in the fallback's mode
                if call_result.get("reasoning"):          # the model's readable reasoning, for whoever shows it
                    yield {"type": "reasoning", "content": str(call_result["reasoning"])}

                if call_result.get("type") == "invalid_tool_args":
                    self._feed_back_invalid_tool_args(working_history, call_result)
                    if state is not None:
                        # Malformed calls feed the stuck tracker too, or a model that keeps
                        # sending them would spin until the deadline.
                        state.after_turn(working_history, [(
                            call_result.get("name") or "_invalid_args",
                            str(call_result.get("raw", ""))[:200],
                            str(working_history[-1]["content"]), True)])
                        yield from state.drain()
                    count += 1
                    continue

                if call_result.get("type") != "tool_use":
                    text = call_result.get("text", "")
                    if self._nudge_text_only_turn(
                        working_history, native=True, budget=nudge_budget,
                        count=count, assistant_text=text,
                    ):
                        nudge_budget -= 1
                        count += 1
                        continue
                    final_answer, override = _settle_empty_final(text, self._output_wanted(), tool_calls)
                    outcome = override or outcome
                    yield {"type": "final", "content": final_answer}
                    break

                turn_calls = call_result.get("tool_calls") or [{
                    "name": call_result["name"],
                    "input": call_result["input"],
                    "id": call_result.get("id") or f"call_{count}_0",
                }]

                # "respond" call → loop terminates with its answer text.
                # A model may batch real tool calls ALONGSIDE respond in one
                # turn ("do X, and here is my answer"). Honouring respond
                # immediately used to discard those calls silently — the
                # tool never ran, never appeared in completion.tool_calls,
                # and nothing told the caller. Dispatch the batch first and
                # let the NEXT turn produce the answer, so the model gets to
                # see the results it asked for.
                respond_call = next((c for c in turn_calls if c["name"] == "respond"), None)
                if respond_call is not None and len(turn_calls) > 1:
                    if self.verbose:
                        others = [c["name"] for c in turn_calls if c["name"] != "respond"]
                        print(
                            f"[3;33m[loop] 'respond' batched with {others}; "
                            f"running the tools first, deferring the answer"
                            f"[0m"
                        )
                    respond_call = None
                    turn_calls = [c for c in turn_calls if c["name"] != "respond"]
                if respond_call is not None:
                    final_answer, override = _settle_empty_final(
                        str(respond_call["input"].get("answer", "")), self._output_wanted(), tool_calls)
                    outcome = override or outcome
                    working_history.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": respond_call.get("id") or f"call_{count}_respond",
                            "type": "function",
                            "function": {
                                "name": "respond",
                                "arguments": json.dumps(respond_call["input"]),
                            },
                        }],
                    })
                    yield {"type": "final", "content": final_answer}
                    break

                non_respond = [c for c in turn_calls if c["name"] != "respond"]

                # Loop-level spiral check, native-mode edition. The
                # text/FC path has had this since 3.0; the native branch
                # `continue`d before ever reaching it, so a model stuck
                # re-issuing the same call ran to max_iterations. The
                # tool-layer dup-guard warns and then refuses, but a
                # stubborn model treats the refusal as just another
                # message and keeps going — which is the exact reason
                # this loop-level stop exists. Signature is the whole
                # TURN (sorted name+args) so batched calls compare too.
                try:
                    turn_sig = json.dumps(
                        sorted(
                            (c["name"], json.dumps(c["input"], sort_keys=True, default=repr))
                            for c in non_respond
                        ),
                        default=repr,
                    )
                except Exception:
                    turn_sig = repr([(c["name"], c["input"]) for c in non_respond])
                if turn_sig == last_action_sig:
                    consecutive_identical_actions += 1
                else:
                    last_action_sig = turn_sig
                    consecutive_identical_actions = 1
                if consecutive_identical_actions >= LOOP_FORCE_STOP:
                    outcome = "stuck"
                    names = ", ".join(sorted({c["name"] for c in non_respond})) or "<none>"
                    if tool_calls:
                        last = tool_calls[-1]
                        final_answer = (
                            f"(Terminated: model issued {consecutive_identical_actions} "
                            f"identical calls to '{names}' in a row. Best available "
                            f"data from last successful call to '{last.name}':\n\n"
                            f"{str(last.result)[:2000]})"
                        )
                    else:
                        final_answer = (
                            f"(Terminated: model issued {consecutive_identical_actions} "
                            f"identical calls to '{names}' in a row without any "
                            "successful tool result to fall back on.)"
                        )
                    if self.verbose:
                        print(
                            "[1;31m[loop] force-stop: "
                            f"{consecutive_identical_actions} identical native turns "
                            f"calling '{names}' — aborting before dispatch[0m"
                        )
                    yield {"type": "final", "content": final_answer}
                    break

                for i, c in enumerate(non_respond):
                    if not c.get("id"):
                        c["id"] = f"call_{count}_{i}"

                working_history.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["input"]),
                        },
                    } for c in non_respond],
                })

                def _run_one(call):
                    # StandardTool got a synthesized {input: str} spec — unwrap.
                    args = call["input"]
                    if not self.registry.is_structured(call["name"]) and isinstance(args, dict) and "input" in args:
                        args = args["input"]
                    token = _TOOL_CALL_ID.set(call.get("id"))
                    try:
                        return self.registry.dispatch(call["name"], args)
                    finally:
                        _TOOL_CALL_ID.reset(token)

                # Concurrent dispatch. return_exceptions equivalent via
                # per-future try/except so one bad tool doesn't kill the turn.
                from concurrent.futures import ThreadPoolExecutor
                workers = max(1, min(self.parallel_tool_workers, len(non_respond)))
                results: List[Any] = [None] * len(non_respond)
                if workers == 1 or len(non_respond) == 1:
                    for i, c in enumerate(non_respond):
                        try:
                            results[i] = _run_one(c)
                        except KeyboardInterrupt:
                            raise                         # Ctrl-C (for example at an ask_user prompt) stops the run
                        except BaseException as e:
                            results[i] = ToolError(
                                f"unhandled exception in dispatch for '{c['name']}': {e}",
                                tool=c["name"], cause=e,
                            )
                else:
                    with ThreadPoolExecutor(
                        max_workers=workers, thread_name_prefix="agent_tool"
                    ) as pool:
                        futures = {pool.submit(_run_one, c): i for i, c in enumerate(non_respond)}
                        for fut, idx in futures.items():
                            try:
                                results[idx] = fut.result()
                            except KeyboardInterrupt:
                                raise
                            except BaseException as e:
                                c = non_respond[idx]
                                results[idx] = ToolError(
                                    f"unhandled exception in dispatch for '{c['name']}': {e}",
                                    tool=c["name"], cause=e,
                                )

                turn_obs: List[Any] = []
                for call, result in zip(non_respond, results):
                    is_error = isinstance(result, ToolError)
                    # Verbose trace parity with text/FC mode: the
                    # bind_tools_natively branch used to print blank
                    # [tool.call.start]/[tool.call.complete] pairs
                    # from the observability layer without the name +
                    # args + response context, making it hard to
                    # debug which tool the model actually invoked in
                    # native mode. Match the [tool] Invoking / [tool]
                    # Response format used by the text and FC paths
                    # (see AgentRun.py `Invoking` prints below the FC
                    # branch) so a single verbose run reads uniformly
                    # regardless of dispatch mode.
                    if self.verbose:
                        print(
                            f"\x1B[3;33m[tool] Invoking '{call['name']}' "
                            f"with args: {call['input']}\x1B[0m"
                        )
                    step_description = f"Step {count}: {call['name']} with {call['input']}"
                    steps.append(step_description)
                    yield {"type": "tool_call", "name": call["name"], "args": call["input"]}
                    yield {"type": "tool_result", "name": call["name"],
                           "result": str(result), "is_error": is_error}
                    if self.verbose:
                        result_str = str(result)
                        preview = result_str[:300] + ("..." if len(result_str) > 300 else "")
                        color = "\x1B[31m" if is_error else "\x1B[32m"
                        label = "Error" if is_error else "Response"
                        print(f"{color}[tool] {label}: {preview}\x1B[0m")
                    if not is_error:
                        tool_calls.append(ToolCall(
                            name=call["name"],
                            args={"Input": call["input"]},
                            result=str(result),
                        ))
                    working_history.append({
                        "role": "tool",
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "content": f"Error: {result}" if is_error else _observation_content(result),
                    })
                    turn_obs.append((call["name"], call["input"], str(result), is_error))
                if state is not None:
                    state.after_turn(working_history, turn_obs)
                    yield from state.drain()
                count += 1
                continue

            if self.use_function_calling:
                request = yield from _request([parser_tool_spec])
                try:
                    call_result = _model_call(lambda: self.model.call_with_tools(
                        messages=request,
                        tools=[parser_tool_spec],
                        force_tool=parser_tool_name,
                    ))
                except _SwitchedToFallback as switched:
                    yield switched.event
                    continue                              # the same step, in the fallback's mode
                if call_result.get("reasoning"):          # the model's readable reasoning, for whoever shows it
                    yield {"type": "reasoning", "content": str(call_result["reasoning"])}

                if call_result.get("type") == "invalid_tool_args":
                    self._feed_back_invalid_tool_args(working_history, call_result)
                    if state is not None:
                        # Malformed calls feed the stuck tracker too, or a model that keeps
                        # sending them would spin until the deadline.
                        state.after_turn(working_history, [(
                            call_result.get("name") or "_invalid_args",
                            str(call_result.get("raw", ""))[:200],
                            str(working_history[-1]["content"]), True)])
                        yield from state.drain()
                    count += 1
                    continue

                parser_instance = self._resolve_parser_step(call_result)

                if parser_instance is None:
                    text = call_result.get("text", "")
                    if self._nudge_text_only_turn(
                        working_history, native=False, budget=nudge_budget,
                        count=count, assistant_text=text,
                    ):
                        nudge_budget -= 1
                        count += 1
                        continue
                    final_answer, override = _settle_empty_final(text, self._output_wanted(), tool_calls)
                    outcome = override or outcome
                    yield {"type": "final", "content": final_answer}
                    break

                # #15: record the assistant turn with a proper tool_calls
                # block so providers can correlate it with the matching
                # tool result below via the OpenAI/Anthropic tool_use_id
                # convention. Plain {role, content} dicts still work for
                # text-only models; the chat-model translators only act
                # on the `tool_calls` field when it's present.
                tool_call_id = call_result.get("id") or f"call_{count}"
                working_history.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": call_result["name"],
                            "arguments": json.dumps(call_result["input"]),
                        },
                    }],
                })
                # Stash for the tool-result message a few lines down.
                self._last_function_call_id = tool_call_id
            else:
                request = yield from _request(None)
                if stream_tokens:
                    # Token-level streaming: yield text_delta events as the
                    # LLM emits chunks; accumulate into the full response so
                    # the rest of the loop (parser + tool dispatch) sees a
                    # complete string. Non-streaming models yield once via
                    # the default stream_text fallback. Streamed responses are
                    # not retried (a response cannot be replayed half-way).
                    parts: List[str] = []
                    calls_before = _usage_calls(self.model)
                    try:
                        for chunk in self.model.stream_text(request):
                            parts.append(chunk)
                            yield {"type": "text_delta", "content": chunk}
                    except Exception as error:
                        yield _take_over(error)
                        continue                          # the same step, in the fallback's mode
                    self._learn_chars_per_token(calls_before, request_chars_sent)
                    response = "".join(parts)
                else:
                    try:
                        response = _model_call(lambda: self.model.Initialize(messages=request))
                    except _SwitchedToFallback as switched:
                        yield switched.event
                        continue                          # the same step, in the fallback's mode
                working_history.append({"role": "assistant", "content": response})
                try:
                    parser_instance = self._resolve_parser_step(response)
                except _ParserRecoverableError as parse_err:
                    # Text-mode envelope was malformed JSON AND regex
                    # salvage failed. Feed the model a targeted fix
                    # hint and let the loop retry (bounded by
                    # max_iterations). Alternative — letting the
                    # JSONDecodeError propagate — killed the run
                    # whenever a long action_input string had
                    # unescaped newlines / quotes / code fences.
                    err_msg = (
                        f"[framework] error: your last response was not "
                        f"valid JSON ({parse_err.reason}). Emit a valid "
                        f"envelope: "
                        f'{{"Thought": "...", "action": "<tool_or_Final_Answer>", '
                        f'"action_input": ...}}. For long text in '
                        f"action_input, escape newlines as \\n, quotes "
                        f'as \\", backslashes as \\\\. Or set '
                        f'"action": "Final_Answer" if you are done.'
                    )
                    working_history.append({"role": "user", "content": err_msg})
                    yield {"type": "tool_result", "name": "_parser",
                           "result": err_msg, "is_error": True}
                    if state is not None:
                        state.after_turn(working_history,
                                         [("_parser", parse_err.raw_text[:200], err_msg, True)])
                        yield from state.drain()
                    if self.verbose:
                        print(
                            f"\x1B[1;33m[loop] parser JSON malformed at "
                            f"iter {count}; retrying with fix hint "
                            f"({parse_err.reason})\x1B[0m"
                        )
                    count += 1
                    if count > self.max_iterations:
                        outcome = "iteration_limit"
                        final_answer = (
                            "(framework: exhausted max_iterations after "
                            "repeated malformed-JSON responses; last raw "
                            f"text: {parse_err.raw_text[:400]!r})"
                        )
                        if state is not None:
                            # Persistent: the ledger report, like every other non-done exit.
                            state.finish(OUTCOME_ITERATION_LIMIT)
                            final_answer = state.report()
                        yield {"type": "final", "content": final_answer}
                        break
                    continue

                if parser_instance is None:
                    # The assistant turn was already recorded above, so
                    # pass assistant_text=None to avoid duplicating it.
                    if self._nudge_text_only_turn(
                        working_history, native=False, budget=nudge_budget,
                        count=count,
                    ):
                        nudge_budget -= 1
                        count += 1
                        continue
                    final_answer, override = _settle_empty_final(response, self._output_wanted(), tool_calls)
                    outcome = override or outcome
                    yield {"type": "final", "content": final_answer}
                    break

            action = _read_action(parser_instance)
            action_input = _read_action_input(parser_instance)

            thought = getattr(parser_instance, "Thought", None)
            if thought:
                if state is not None:
                    state.note_thought(thought)
                yield {"type": "thought", "content": thought}
                if self.verbose:
                    print(f"\x1B[36m[thought] {thought}\x1B[0m")
                else:
                    logger.info(f"thought: {thought}")

            step_description = f"Step {count}: {action} with {action_input}"
            steps.append(step_description)

            # Terminal action check — tolerant matching so the loop ends
            # for the canonical "Final_Answer" AND for the LLM's common
            # variants ("Final Answer", "final_answer", "finalanswer").
            # Under use_function_calling=True the schema doesn't
            # constrain `action` to any spelling, so we accept them all.
            if _is_terminal_action(action):
                final_answer, override = _settle_empty_final(action_input, self._output_wanted(), tool_calls)
                outcome = override or outcome
                yield {"type": "final", "content": final_answer}
                break

            # Normalize BEFORE the known-tools check — OpenAI's FC path
            # sometimes returns tool names with a "functions." prefix
            # ("functions.get_my_scores" instead of "get_my_scores").
            # Without this, a valid tool call falls through to the
            # implicit-final guardrail, which then leaks the model's
            # internal Thought into user-visible text. Rewrite `action`
            # here so the rest of the loop uses the canonical name.
            normalized_action = self.registry._normalize_tool_name(action)
            if normalized_action != action:
                if self.verbose:
                    print(
                        f"\x1B[3;33m[loop] normalized action "
                        f"'{action}' -> '{normalized_action}'\x1B[0m"
                    )
                action = normalized_action

            # Loop-level spiral check — 3 identical (action, args) in a
            # row and we bail. Placed BEFORE the known-tools branch so
            # it catches BOTH kinds of spiral: a valid tool getting
            # re-dispatched (dup-guard also catches this at the tool
            # layer), and an unknown tool being retried via strict-mode
            # (dup-guard would never see those since dispatch skips them).
            try:
                action_sig = f"{action}::{json.dumps(action_input, sort_keys=True, default=repr)}"
            except Exception:
                action_sig = f"{action}::{action_input!r}"
            if action_sig == last_action_sig:
                consecutive_identical_actions += 1
            else:
                last_action_sig = action_sig
                consecutive_identical_actions = 1
            if consecutive_identical_actions >= LOOP_FORCE_STOP:
                outcome = "stuck"
                if tool_calls:
                    last = tool_calls[-1]
                    forced = (
                        f"(Terminated: model issued {consecutive_identical_actions} "
                        f"identical calls to '{action}' in a row. Best available "
                        f"data from last successful call to '{last.name}':\n\n"
                        f"{str(last.result)[:2000]})"
                    )
                else:
                    forced = (
                        f"(Terminated: model issued {consecutive_identical_actions} "
                        f"identical calls to '{action}' in a row without any "
                        "successful tool result to fall back on.)"
                    )
                if self.verbose:
                    print(
                        "\x1B[1;31m[loop] force-stop: "
                        f"{consecutive_identical_actions} identical calls to "
                        f"'{action}' — aborting before dispatch\x1B[0m"
                    )
                final_answer = forced
                yield {"type": "final", "content": final_answer}
                break

            # Guardrail: the model emitted an `action` value that is
            # neither a terminal-answer variant NOR a registered tool
            # (even after normalization). Two scenarios trigger this:
            #   1. use_function_calling=True + missing schema guidance:
            #      the model stuffs its natural-language response text
            #      into `action` (e.g. "Provide a response to the
            #      athlete."). Route to final so the loop terminates.
            #   2. action="" (missing field, Pydantic default).
            # NEVER include `Thought` in the user-facing final — the
            # Thought is internal reasoning ("The user needs their
            # scores; I'll fetch them") and leaking it to the user
            # exposes the agent's introspection. Prefer action_input
            # (that's what the model wanted the user to see). Only fall
            # back to `action` when action_input is empty AND action
            # looks like natural-language text (contains a space, not
            # just an identifier).
            # `multi_tool_use.parallel` is OpenAI's synthetic meta-tool
            # for batching several tool calls into one turn (the model
            # emits it when it wants to fetch N URLs concurrently, run
            # M searches at once, etc.). ToolRegistry.dispatch already
            # unpacks it into the underlying tool calls -- but only if
            # this guardrail lets it through. Add it to the recognized
            # set so a legitimate batching call doesn't fall through to
            # implicit-final and dump its `action_input` (a list of
            # nested tool_use dicts) into the user-visible answer.
            # Union ALL FOUR registry dicts, not just the sync pair.
            # A sync runner can be handed an AsyncStandardTool: the
            # registry accepts it, registry.has() reports it, and it
            # appears in the prompt block the model reads -- so the model
            # calls it, and a sync-only known_tools sent that straight to
            # the implicit-final guardrail. Result was a silently wrong
            # answer (action_input returned as the final) with an empty
            # tool_calls list and no error. AsyncAgentRun.py has always
            # unioned all four; this brings the sync path in line.
            known_tools = (
                set(self.registry.sync_std) | set(self.registry.sync_struct)
                | set(self.registry.async_std) | set(self.registry.async_struct)
                | {"multi_tool_use.parallel"}
            )
            if not action or action not in known_tools:
                # Strict mode: when the unknown `action` looks like a
                # tool identifier (no spaces, short), assume the model
                # was TRYING to call a tool and misnamed it. Feed an
                # error observation back so the model can retry with
                # a valid name, instead of collapsing to implicit-final
                # after the very first mistake. The existing dup-guard
                # + loop-level circuit breaker still bound retries.
                # Only skipped when: strict mode is off, we've hit
                # max_iterations, or the action is clearly the model's
                # response text (contains a space → natural language).
                looks_like_tool_id = (
                    bool(action) and " " not in action and len(action) < 80
                )
                if (
                    self.strict_tool_dispatch
                    and looks_like_tool_id
                    and count < self.max_iterations
                ):
                    available = sorted(known_tools)
                    error_content = (
                        f"[framework] error: '{action}' is not a "
                        f"registered tool. Available tools: {available}. "
                        f"Options: (1) call one of them with the correct "
                        f"argument shape; (2) emit action=\"Final_Answer\" "
                        f"with your final response text in action_input "
                        f"if you're done."
                    )
                    tc_id = getattr(self, "_last_function_call_id", None) if self.use_function_calling else None
                    working_history.append(_tool_observation_message(
                        error_content, tool_call_id=tc_id, name="tool_call_error",
                    ))
                    yield {
                        "type": "tool_result",
                        "name": action,
                        "result": error_content,
                        "is_error": True,
                    }
                    if self.verbose:
                        print(
                            f"\x1B[1;33m[loop] strict-retry: action "
                            f"'{action[:60]}' unknown; feeding error "
                            f"back so the model can retry\x1B[0m"
                        )
                    self._last_function_call_id = None
                    if state is not None:
                        # An unknown tool is a failed call: feed the stuck tracker, or a
                        # model that keeps calling it would retry until the turn limit.
                        state.after_turn(working_history, [(action, action_input, error_content, True)])
                        yield from state.drain()
                    count += 1
                    continue

                if _is_empty_step(action, action_input) and empty_steps < EMPTY_STEP_RETRIES \
                        and count < self.max_iterations:
                    empty_steps += 1
                    tc_id = getattr(self, "_last_function_call_id", None) if self.use_function_calling else None
                    working_history.append(_tool_observation_message(
                        EMPTY_STEP_NOTE, tool_call_id=tc_id, name="tool_call_error",
                    ))
                    self._last_function_call_id = None
                    count += 1
                    continue

                # Non-strict OR strict-can't-help path: implicit-final.
                # Prefer action_input; fall back to action-as-text ONLY
                # when it's natural language (has spaces). NEVER leak
                # Thought — it's internal reasoning that shouldn't reach
                # a user, especially in third-person-persona apps.
                if action_input and str(action_input).strip():
                    final_answer = str(action_input)
                elif action and action.strip() and " " in action:
                    final_answer = str(action)
                elif state is not None:
                    # A persistent run never reports a non-answer as done: end it
                    # stuck, with the ledger report.
                    outcome = "stuck"
                    state.finish(OUTCOME_STUCK, headline=unrecognized_action_headline(action))
                    final_answer = state.report()
                else:
                    final_answer = (
                        "(agent emitted an unrecognized action and no "
                        "answer text — try rephrasing or re-run)"
                    )
                if self.verbose:
                    preview = (action or "<empty>")[:60]
                    print(
                        f"\x1B[1;33m[loop] implicit-final: action "
                        f"'{preview}' is not a registered tool and not "
                        f"a Final_Answer variant; returning "
                        f"action_input as final (Thought NOT leaked)"
                        f"\x1B[0m"
                    )
                yield {"type": "final", "content": final_answer}
                break

            yield {"type": "tool_call", "name": action, "args": action_input}
            if self.verbose:
                print(f"\x1B[3;33m[tool] Invoking '{action}' with args: {action_input}\x1B[0m")
            else:
                logger.info(f"tool invoke: {action}({action_input})")
            call_id = getattr(self, "_last_function_call_id", None) if self.use_function_calling else None
            token = _TOOL_CALL_ID.set(call_id)
            try:
                tool_response = self.Tool_Runner(action, action_input)
            finally:
                _TOOL_CALL_ID.reset(token)
            is_error = isinstance(tool_response, ToolError)
            yield {"type": "tool_result", "name": action,
                   "result": str(tool_response), "is_error": is_error}
            if self.verbose:
                print(f"\x1B[32m[tool] Response: {str(tool_response)[:300]}\x1B[0m")
            else:
                logger.info(f"tool response: {str(tool_response)[:300]}")

            # #15: when use_function_calling=True we have a matching
            # tool_call_id from the assistant turn above; carry it through
            # so the provider can correlate (role="tool"). Text mode has no
            # id, so the observation goes back as a role="user" turn — see
            # _tool_observation_message.
            tool_call_id = getattr(self, "_last_function_call_id", None) if self.use_function_calling else None
            if is_error:
                working_history.append(_tool_observation_message(
                    f"Error: {tool_response}",
                    tool_call_id=tool_call_id, name="tool_call_error",
                ))
            else:
                working_history.append(_tool_observation_message(
                    _observation_content(tool_response), tool_call_id=tool_call_id, name=action,
                ))
                tool_calls.append(ToolCall(
                    name=action,
                    args={"Input": action_input},
                    result=str(tool_response),
                ))
            self._last_function_call_id = None

            if state is not None:
                state.after_turn(working_history,
                                 [(action, action_input, str(tool_response), is_error)])
                yield from state.drain()

            count += 1

        # If we exited the loop without a Final_Answer (hit max_iterations),
        # synthesize a useful summary of what actually happened. The old
        # 'No final answer returned.' string was useless — the caller had
        # no idea what the agent tried, which tools ran, or where it
        # got stuck. Now they get a compact recap.
        if final_answer is None and state is not None:
            # Persistent run out of turns: the same deterministic ledger report as every
            # other early exit, not the "raise max_iterations" recap below.
            outcome = "iteration_limit"
            state.finish(OUTCOME_ITERATION_LIMIT)
            final_answer = state.report()
        elif final_answer is None:
            outcome = "iteration_limit"
            summary_lines = [
                f"Hit max_iterations ({self.max_iterations}) without returning a Final_Answer. "
                f"Here's what the agent completed before running out of turns:"
            ]
            if steps:
                summary_lines.append("")
                summary_lines.append("Steps taken:")
                for s in steps[-6:]:   # last 6 to keep it readable
                    summary_lines.append(f"  - {s}")
            if tool_calls:
                summary_lines.append("")
                summary_lines.append(f"Tool results ({len(tool_calls)} total, showing last 3):")
                for tc in tool_calls[-3:]:
                    preview = str(tc.result)[:200] + ("…" if len(str(tc.result)) > 200 else "")
                    summary_lines.append(f"  - {tc.name}: {preview}")
            summary_lines.append("")
            summary_lines.append(
                "The agent likely needed more iterations — try re-running with "
                "a higher max_iterations, or narrow the task so it finishes sooner."
            )
            final_answer = "\n".join(summary_lines)

        if self.verbose:
            print(f"\x1B[32m[final] {final_answer}\x1B[0m")
        else:
            logger.info(f"final_answer: {final_answer}")

        _close_open_tool_calls(working_history)

        if self.auto_memory and self._memory:
            self._memory.add_message("user", user_input)
            self._memory.add_message("assistant", final_answer)

        if agent_event:
            observability.end_event(agent_event, data={
                "final_answer": str(final_answer)[:100],
                "iterations": count,
                "tool_calls": len(tool_calls),
            })

        completion = AgentCompletion.from_agent(
            model_name=self.model.__class__.__name__,
            query=user_input,
            content=final_answer,
            tool_calls=tool_calls,
            steps=steps,
            history=working_history,
            outcome=outcome,
            progress=state.ledger.to_dict() if state is not None else None,
        )
        yield {"type": "completion", "completion": completion}

    def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ) -> AgentCompletion:
        """The main agent execution loop. Returns the final AgentCompletion.

        ``media`` attaches images / PDFs / audio to this turn -- a list of
        :class:`agentx_dev.Media`, file paths, URLs, or content-part dicts.

        Accepts either ``ChatHistory=`` (legacy) or ``chat_history=`` (PEP 8).
        For step-by-step event streaming, use ``stream()`` instead.

        Structured output. ``output_schema`` may be passed here for a
        one-off shape, or declared once on the constructor
        (``AgentRunner(..., output_schema=QueryIntent)``) so every call on
        this runner returns that shape. The per-call value wins when both
        are set.

        When a schema is in play, the validated Pydantic instance lands on
        ``completion.output`` while ``completion.content`` keeps the
        human-readable answer, so callers get both a machine-readable and a
        display value from one run. The coercion happens AFTER the ReAct
        loop finishes and does not touch tool selection or intermediate
        reasoning — see ``_coerce_to_schema``.
        """
        if ChatHistory is not None and chat_history is not None:
            raise TypeError("Pass either 'ChatHistory' or 'chat_history', not both.")
        if chat_history is not None:
            ChatHistory = chat_history

        completion: Optional[AgentCompletion] = None
        # The loop needs the schema of this call to know a structured answer is wanted.
        token = _CALL_SCHEMA.set(output_schema)
        try:
            for event in self._iter_run(user_input, ChatHistory, media=media, _budget=_budget):
                if event["type"] == "completion":
                    completion = event["completion"]
        finally:
            _CALL_SCHEMA.reset(token)
        assert completion is not None, "Loop exited without yielding completion"

        # Per-call schema beats the constructor default; None on both means
        # no coercion at all and completion.output stays None (unchanged
        # pre-3.2 behaviour).
        schema = output_schema if output_schema is not None else self.output_schema
        # A persistent run that ended early has a report, not an answer: don't try to parse it.
        if schema is not None and not (self.persistence is not None and completion.outcome != "done"):
            completion.output = self._coerce_to_schema(
                completion.content, schema, user_input
            )
        return completion

    def _coerce_to_schema(
        self,
        text: str,
        schema: Type[BaseModel],
        user_input: str = "",
    ) -> BaseModel:
        """Turn the agent's final answer into a validated ``schema`` instance.

        Two strategies, in order:

        1. **Native function calling** (preferred). The schema is sent to
           the model as a forced tool call, so the provider's own
           constrained decoding produces the fields. Nothing is parsed out
           of prose, which removes the whole class of "the model wrote a
           nice paragraph instead of JSON" and "the JSON broke because a
           quote was not escaped" failures.
        2. **Text JSON parsing** (fallback). Used when the model has no
           ``call_with_tools`` implementation, or when the forced call
           itself fails. This is the pre-3.2 behaviour, kept so custom
           models without an FC backend still work.

        This runs AFTER the ReAct loop has already produced its answer. It
        deliberately does not participate in tool selection or intermediate
        reasoning: forcing a schema onto the loop itself makes the model
        try to satisfy the output shape while it is still deciding which
        tool to call, which degrades both.
        """
        from agentx_dev.Agents.Agent import to_tool_spec

        base_cwt = getattr(BaseChatModel, "call_with_tools", None)
        model_cwt = getattr(type(self.model), "call_with_tools", None)
        can_fc = model_cwt is not None and model_cwt is not base_cwt

        if can_fc:
            try:
                spec = to_tool_spec(schema)
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "Convert the assistant's answer into the "
                            f"{schema.__name__} structure by calling the tool. "
                            "Use only information present in the answer. Do "
                            "not invent field values; if the answer does not "
                            "supply a field, use the most conservative "
                            "permitted value."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            (f"Original request:\n{user_input}\n\n" if user_input else "")
                            + f"Answer to structure:\n{text}"
                        ),
                    },
                ]
                result = self.model.call_with_tools(
                    messages=messages,
                    tools=[spec],
                    force_tool=schema.__name__,
                )
                if isinstance(result, dict) and result.get("type") == "tool_use":
                    return schema(**result["input"])
                if self.verbose:
                    print(
                        f"\x1B[1;33m[schema] model returned text instead of a "
                        f"{schema.__name__} call; falling back to JSON "
                        f"parsing\x1B[0m"
                    )
            except Exception as e:
                if self.verbose:
                    print(
                        f"\x1B[1;33m[schema] forced {schema.__name__} call "
                        f"failed ({type(e).__name__}: {e}); falling back to "
                        f"JSON parsing\x1B[0m"
                    )

        # Referenced on the class (not self) because AsyncAgentRunner
        # borrows _coerce_to_schema unbound; the staticmethod resolves
        # regardless of which runner type `self` actually is.
        return AgentRunner._parse_to_schema(text, schema)

    @staticmethod
    def _parse_to_schema(text: str, schema: Type[BaseModel]) -> BaseModel:
        """Parse ``text`` as JSON and validate against ``schema``. Tolerant
        of ```json fences the model sometimes wraps output in.

        Raises ``ValueError`` (wrapping the underlying cause) on parse or
        validation failure so callers can surface a specific error rather
        than getting a silently wrong instance.
        """
        from agentx_dev.Agents.Agent import convert_to_json
        # convert_to_json handles ```json fences and returns dict or None.
        try:
            parsed = convert_to_json(text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"output_schema={schema.__name__}: final answer is JSON-shaped "
                f"but malformed: {e}. Got: {text[:200]!r}"
            ) from e
        if parsed is None:
            raise ValueError(
                f"output_schema={schema.__name__}: final answer is not JSON. "
                f"Got: {text[:200]!r}"
            )
        try:
            return schema(**parsed)
        except Exception as e:
            raise ValueError(
                f"output_schema={schema.__name__}: parsed JSON did not match "
                f"the schema: {e}. Parsed: {parsed!r}"
            ) from e

    def stream(
        self,
        user_input: Any,
        chat_history: Optional[List[Dict[str, str]]] = None,
        *,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
    ):
        """Yield step events as the agent runs.

        Example::

            for event in runner.stream("Greet Alice"):
                if event["type"] == "thought":
                    print("Thinking:", event["content"])
                elif event["type"] == "tool_call":
                    print(f"Calling {event['name']}({event['args']})")
                elif event["type"] == "tool_result":
                    print("Got:", event["result"])
                elif event["type"] == "final":
                    print("Answer:", event["content"])
                elif event["type"] == "completion":
                    full = event["completion"]   # the AgentCompletion

        Pass ``stream_tokens=True`` to additionally receive
        ``{"type": "text_delta", "content": chunk}`` events as the LLM
        emits each token (only when use_function_calling=False and the
        chat model implements streaming — GPT and Claude both do).

        The last event is always ``{"type": "completion", "completion": ...}``
        so callers that want both real-time updates AND the final
        AgentCompletion can have both.
        """
        query, hist, parts = _coerce_runner_input_media(user_input, chat_history, media)
        yield from self._iter_run(query, hist, stream_tokens=stream_tokens, media=parts)

    def invoke(
        self,
        user_input: Any,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
        media: Optional[List[Any]] = None,
    ) -> AgentCompletion:
        """Canonical entry point. Alias for ``Initialize`` with shape
        normalization.

        ``media=[...]`` attaches images / PDFs / audio (``Media``, paths,
        URLs, or part dicts). Media inside a message-list input's last
        user turn is picked up too.

        ``user_input`` may be:
          - ``str``                    — treated as the query (classic path)
          - ``list[dict]``             — chat-model-style message list;
            the last ``{"role":"user"}`` becomes the query and earlier
            non-system turns become the chat history (merged with any
            explicit ``chat_history`` positional).
          - ``{"messages": [...]}``    — same as list.

        Explicit ``chat_history=`` (or legacy ``ChatHistory=``) still
        works and combines with the message-list shape."""
        # Merge legacy alias first so a single history reaches the coercer.
        if ChatHistory is not None and chat_history is not None:
            raise TypeError("Pass either 'ChatHistory' or 'chat_history', not both.")
        base_history = chat_history if chat_history is not None else ChatHistory
        query, hist, parts = _coerce_runner_input_media(user_input, base_history, media)
        return self.Initialize(
            query, None,
            chat_history=hist,
            output_schema=output_schema,
            media=parts,
        )

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}("
            f"Agent={self.Agent.__class__.__name__}, "
            f"Tools={len(self.tools)}, "
            f"ToolNames={[tool.name for tool in self.tools]}, "
            f"MaxIterations={self.max_iterations}, "
            f"FunctionCalling={self.use_function_calling}, "
            f"Model='{self.model}')>"
        )

    def __str__(self) -> str:
        return (
            f"Agent Runner Summary:\n"
            f"  Agent Type     : {self.Agent.__class__.__name__}\n"
            f"  Tools Used     : {[tool.name for tool in self.tools]}\n"
            f"  Max Iterations : {self.max_iterations}\n"
            f"  Function Calls : {self.use_function_calling}\n"
            f"  Model          : {self.model}\n"
            f"  Query          : {self.Query}"
        )
