from agentx_dev.Agents import AgentFormattor, AgentCompletion, AgentPrompt
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Agents.Agent import StandardParser, ToolCall, ToolError
from agentx_dev.Tools import StandardTool, StructuredTool, logger
from typing import Dict, Callable, List, Type, Optional, Any
from pydantic import BaseModel, Field

import asyncio
import json
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
            "here automatically — you don't need to prefix them."
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
            future = pool.submit(fn)
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

    def _cache_get(self, name: str, args):
        cache = getattr(self, "_cache", None)
        if cache is None:
            return None
        from agentx_dev.Cache import generate_cache_key
        key = generate_cache_key(name, args)
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
        if cache is None:
            return
        from agentx_dev.Cache import generate_cache_key
        key = generate_cache_key(name, args)
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
                result = ToolError(
                    f"Tool '{name}' not found. Please double-check the tool name.",
                    tool=name,
                )

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
                result = dup_payload + str(result)
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
                result = ToolError(
                    f"Tool '{name}' not found. Please double-check the tool name.",
                    tool=name,
                )

            if not isinstance(result, ToolError):
                self._cache_set(name, args, result)
            if breaker is not None:
                if isinstance(result, ToolError):
                    breaker.record_failure()
                else:
                    breaker.record_success()
            if dup_verdict == "warn" and not isinstance(result, ToolError):
                result = dup_payload + str(result)
            return result
        finally:
            self._obs_end(event, result)


# Imports are kept light at module level; initialization is deferred to first
# AgentRunner.__init__() call so `import agentx_dev.Runner.AgentRun` is
# side-effect-free.
from agentx_dev.AutoSetup import get_auto_setup, ensure_initialized
from agentx_dev.Config import config
from agentx_dev.Observability import observability, EventType

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


class AgentRunner:
    """
        The main engine that orchestrates the agent's execution loop.

        This class connects all the components of the framework: the LLM, the tools,
        and the prompt. It manages the agent's state, including its conversational
        history and internal scratchpad, and runs the primary "reason-act" cycle.

        Set ``use_function_calling=True`` to route the AgentType parser through
        the model's native tool-calling API (when supported) instead of parsing
        JSON out of the assistant text. The parser model itself (e.g.
        ``React_``) is forwarded as the forced tool, so the model returns
        ``Thought``/``action``/``action_input`` as structured arguments.
    """

    def __init__(
        self,
        model: BaseChatModel,
        Agent: AgentFormattor | AgentPrompt | str | None = None,
        tools: List[StandardTool | StructuredTool] | None = None,
        max_iterations: int | None = None,
        auto_cache: bool = True,
        auto_memory: bool = False,
        use_function_calling: bool = False,
        verbose: bool = True,
        *,
        agent: AgentFormattor | AgentPrompt | str | None = None,
        permissions: Any = None,
        include_denied_tools: bool = False,
        system_addendum: Optional[str] = None,
    ):
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
        """
        Initializes the AgentRunner.

        Args:
            model (BaseChatModel): The LLM model.
            Agent: The agent's prompt template (string, AgentFormattor, or AgentPrompt).
            tools (List): A list of tool instances available to the agent.
            max_iterations (Optional[int]): Maximum tool-calling cycles.
            auto_cache (bool): Enable automatic tool result caching (default: True).
            auto_memory (bool): Enable automatic memory management (default: False).
            use_function_calling (bool): Route the AgentType parser through the
                model's native tool-calling API. Requires the chat model to
                implement ``call_with_tools`` (GPT and Claude do).

        Raises:
            TypeError: If any item in ``tools`` is not a StandardTool/StructuredTool.
        """
        self.Query = ""
        self.Agent = Agent
        self.tools = tools
        self.max_iterations = max_iterations if max_iterations else 4
        self.model = model
        self.use_function_calling = use_function_calling
        self.verbose = verbose
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
        instance. Returns ``None`` if the response should be treated as a
        final text answer (caller is responsible for surfacing it).
        """
        # Function-calling path: dict from call_with_tools
        if isinstance(response_or_call, dict) and "type" in response_or_call:
            if response_or_call["type"] == "tool_use":
                return self.parser.from_function_call(response_or_call["input"])
            return None  # plain text — treat as final answer

        # JSON-text path
        parsed = self.parser.from_json(response_or_call)
        if isinstance(parsed, self.parser):
            return parsed
        return None  # convert_to_json returned None — text final answer

    def _iter_run(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
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

        self.Query = user_input

        tool_info = {
            'tools': self._tool_prompt_block,
            'tool_names': self._tool_names_block,
            'user_input': user_input,
        }
        system_prompt = self.Agent.prompt.format_map(tool_info)
        if self.system_addendum:
            # Append after the template's own instructions so the
            # role-specific rules read as an override / final word.
            system_prompt = system_prompt + "\n\n" + self.system_addendum

        working_history: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

        effective_history = chat_history
        if self.auto_memory and self._memory and effective_history is None:
            effective_history = self._memory.get_messages()

        if effective_history and isinstance(effective_history, list):
            for r in effective_history:
                if r.get('role') and r.get('content'):
                    working_history.append({'role': r['role'], 'content': r['content']})

        working_history.append({"role": "user", "content": user_input})

        if not isinstance(self.model, BaseChatModel):
            raise TypeError(
                f"The 'model' object must inherit from BaseChatModel, "
                f"but got type {type(self.model).__name__}."
            )

        parser_tool_spec = self.Agent.to_tool_spec() if self.use_function_calling else None
        parser_tool_name = self.parser.__name__ if self.use_function_calling else None

        count = 1
        tool_calls: List[ToolCall] = []
        steps: List[str] = []
        final_answer: Optional[str] = None

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
        LOOP_FORCE_STOP = 3   # 3 identical calls in a row → abort

        while count <= self.max_iterations:
            if self.use_function_calling:
                call_result = self.model.call_with_tools(
                    messages=working_history,
                    tools=[parser_tool_spec],
                    force_tool=parser_tool_name,
                )
                parser_instance = self._resolve_parser_step(call_result)

                if parser_instance is None:
                    final_answer = call_result.get("text", "")
                    working_history.append({"role": "assistant", "content": final_answer})
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
                if stream_tokens:
                    # Token-level streaming: yield text_delta events as the
                    # LLM emits chunks; accumulate into the full response so
                    # the rest of the loop (parser + tool dispatch) sees a
                    # complete string. Non-streaming models yield once via
                    # the default stream_text fallback.
                    parts: List[str] = []
                    for chunk in self.model.stream_text(working_history):
                        parts.append(chunk)
                        yield {"type": "text_delta", "content": chunk}
                    response = "".join(parts)
                else:
                    response = self.model.Initialize(messages=working_history)
                working_history.append({"role": "assistant", "content": response})
                parser_instance = self._resolve_parser_step(response)

                if parser_instance is None:
                    final_answer = response
                    yield {"type": "final", "content": final_answer}
                    break

            action = _read_action(parser_instance)
            action_input = _read_action_input(parser_instance)

            thought = getattr(parser_instance, "Thought", None)
            if thought:
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
                final_answer = action_input
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
            known_tools = set(self.func) | set(self.args)
            if not action or action not in known_tools:
                if action_input and str(action_input).strip():
                    final_answer = str(action_input)
                elif action and action.strip() and " " in action:
                    # Looks like the model put a sentence in `action`
                    # and left action_input empty.
                    final_answer = str(action)
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

            # Loop-level spiral check — same (action, args) as the last
            # turn? Count it. Three in a row and we bail with a synthesized
            # Final_Answer built from whatever real data was gathered
            # before the spiral. Catches BOTH the "refused" spiral and
            # the "tool not found" spiral (gpt-4o-mini did both in
            # traces). Cheaper than dispatch — we abort before the tool
            # even runs on the 3rd repeat.
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

            yield {"type": "tool_call", "name": action, "args": action_input}
            if self.verbose:
                print(f"\x1B[3;33m[tool] Invoking '{action}' with args: {action_input}\x1B[0m")
            else:
                logger.info(f"tool invoke: {action}({action_input})")
            tool_response = self.Tool_Runner(action, action_input)
            is_error = isinstance(tool_response, ToolError)
            yield {"type": "tool_result", "name": action,
                   "result": str(tool_response), "is_error": is_error}
            if self.verbose:
                print(f"\x1B[32m[tool] Response: {str(tool_response)[:300]}\x1B[0m")
            else:
                logger.info(f"tool response: {str(tool_response)[:300]}")

            # #15: when use_function_calling=True we have a matching
            # tool_call_id from the assistant turn above; carry it through
            # so the provider can correlate. Plain text-mode keeps the
            # legacy role="function" shape unchanged.
            tool_call_id = getattr(self, "_last_function_call_id", None) if self.use_function_calling else None
            if is_error:
                err_msg = {
                    "role": "tool" if tool_call_id else "function",
                    "name": "tool_call_error",
                    "content": f"Error: {tool_response}",
                }
                if tool_call_id:
                    err_msg["tool_call_id"] = tool_call_id
                working_history.append(err_msg)
            else:
                ok_msg = {
                    "role": "tool" if tool_call_id else "function",
                    "name": action,
                    "content": str(tool_response),
                }
                if tool_call_id:
                    ok_msg["tool_call_id"] = tool_call_id
                working_history.append(ok_msg)
                tool_calls.append(ToolCall(
                    name=action,
                    args={"Input": action_input},
                    result=str(tool_response),
                ))
            self._last_function_call_id = None

            count += 1

        # If we exited the loop without a Final_Answer (hit max_iterations),
        # synthesize a useful summary of what actually happened. The old
        # 'No final answer returned.' string was useless — the caller had
        # no idea what the agent tried, which tools ran, or where it
        # got stuck. Now they get a compact recap.
        if final_answer is None:
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
        )
        yield {"type": "completion", "completion": completion}

    def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
    ) -> AgentCompletion:
        """The main agent execution loop. Returns the final AgentCompletion.

        Accepts either ``ChatHistory=`` (legacy) or ``chat_history=`` (PEP 8).
        For step-by-step event streaming, use ``stream()`` instead.

        When ``output_schema`` is provided, the final answer text is parsed
        as JSON and validated against the schema; the resulting Pydantic
        instance is placed on ``completion.output``. If the text can't be
        parsed / doesn't fit the schema, a ``ValueError`` is raised
        wrapping the underlying parse / validation error — no silent
        misclassification of malformed output as a valid response.
        """
        if ChatHistory is not None and chat_history is not None:
            raise TypeError("Pass either 'ChatHistory' or 'chat_history', not both.")
        if chat_history is not None:
            ChatHistory = chat_history

        completion: Optional[AgentCompletion] = None
        for event in self._iter_run(user_input, ChatHistory):
            if event["type"] == "completion":
                completion = event["completion"]
        assert completion is not None, "Loop exited without yielding completion"

        if output_schema is not None:
            completion.output = self._parse_to_schema(completion.content, output_schema)
        return completion

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
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        *,
        stream_tokens: bool = False,
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
        yield from self._iter_run(user_input, chat_history, stream_tokens=stream_tokens)

    def invoke(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
        output_schema: Optional[Type[BaseModel]] = None,
    ) -> AgentCompletion:
        """Canonical entry point. Alias for ``Initialize``."""
        return self.Initialize(
            user_input, ChatHistory,
            chat_history=chat_history,
            output_schema=output_schema,
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
