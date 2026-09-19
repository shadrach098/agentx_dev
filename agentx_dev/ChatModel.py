from typing import Optional, Union, Dict, List, Iterable, Literal, Any, Type, Iterator, AsyncIterator
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.chat.completion_create_params import (
    FunctionCall, Function, ResponseFormat
)
from openai._types import NOT_GIVEN, NotGiven
from pydantic import BaseModel
from agentx_dev.Media import (
    content_for_openai, content_for_anthropic, content_for_openai_responses,
)
import asyncio
import json
import re
import logging, os

logger = logging.getLogger(__name__)

# Matches a backslash that does NOT begin a valid JSON escape sequence
# (", \, /, b, f, n, r, t, or u). Used to repair the single most common
# way an LLM breaks tool-argument JSON: emitting a code string or file
# path with a raw backslash — ``re.findall(r'\d+')``, ``C:\Users`` — that
# is legal Python/text but illegal JSON.
_INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _parse_tool_arguments(raw):
    """Parse an LLM tool call's ``arguments`` string into a dict.

    Returns ``(parsed, error)``:
      - ``(dict, None)``  on success (including the empty/None case,
        which yields ``({}, None)``).
      - ``(None, str)``   when the string can't be parsed even after a
        repair attempt; ``str`` is the original JSON error message.

    Repair step: models routinely emit a Python snippet or a Windows
    path as an argument value, producing invalid JSON escapes (``\\d``,
    ``\\U``, ``C:\\Users``). Before giving up we double every backslash
    that isn't already part of a valid JSON escape, which turns ``\\d``
    into ``\\\\d`` (decodes back to a literal ``\\d``) while leaving
    ``\\n`` / ``\\t`` / ``\\uXXXX`` untouched. This recovers the common
    case with zero extra round-trips; genuinely broken JSON (unbalanced
    braces, truncation) still falls through to the error return so the
    loop can ask the model to resend.
    """
    if not raw:
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        try:
            return json.loads(_INVALID_JSON_ESCAPE.sub(r'\\\\', raw)), None
        except json.JSONDecodeError:
            return None, str(e)


from abc import ABC, abstractmethod
import time as _time
import threading as _threading


class RetryBudgetExceeded(Exception):
    """Raised when an agent's cumulative retry budget is used up.

    The intent is operational: a misbehaving agent (or a flaky upstream)
    shouldn't be able to burn through the API quota one retry at a time
    across many calls. A budget caps total retries over the lifetime of
    a single ``BaseChatModel`` instance.
    """


class CostBudgetExceeded(Exception):
    """Raised when cumulative LLM spend on a model exceeds the configured
    ``budget_usd``. Designed to halt a runaway agent BEFORE the bill
    arrives — checked after every token-usage update, so the cap is
    enforced within a few API calls of being breached.

    Carries ``spent_usd`` and ``limit_usd`` so error handlers can log /
    alert on how far over the line the agent went.
    """

    def __init__(self, spent_usd: float, limit_usd: float, message: Optional[str] = None):
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        msg = message or (
            f"Cost budget exceeded: spent ${spent_usd:.4f}, "
            f"limit ${limit_usd:.4f}"
        )
        super().__init__(msg)


class TokenBucket:
    """Simple thread-safe token-bucket rate limiter.

    ``capacity`` tokens accumulate at ``refill_per_sec`` per second.
    ``acquire()`` blocks until a token is available, then deducts one.

    For LLM call rate-limiting at the request level. Not for per-token
    accounting — request count only.
    """

    def __init__(self, capacity: float, refill_per_sec: float):
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("TokenBucket capacity and refill_per_sec must be positive")
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last = _time.monotonic()
        self._lock = _threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available and consume them.

        Returns the wait time slept (0.0 if no wait was needed).
        """
        slept = 0.0
        while True:
            with self._lock:
                now = _time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return slept
                # Compute exact wait needed and release the lock during sleep.
                needed = tokens - self._tokens
                wait = needed / self.refill_per_sec
            _time.sleep(wait)
            slept += wait


class TokenUsage:
    """Accumulating token-usage counter for a BaseChatModel instance.

    Tracks input + output tokens per call (``last``) and across the model's
    lifetime (``total``). Subclasses of BaseChatModel call ``record(input,
    output)`` after every API response that exposes a usage block; calls
    without usage data leave the counters unchanged.

    Thread-safe via an internal RLock so concurrent agents sharing a model
    can both update counts safely.
    """

    def __init__(self):
        self._lock = _threading.RLock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_calls = 0
        self.last_input_tokens = 0
        self.last_output_tokens = 0
        # Anthropic prompt-cache counters. Providers that don't cache leave
        # these at zero. cache_read = tokens billed at the discounted read
        # rate; cache_creation = tokens billed at the (slightly premium)
        # write rate. total_input_tokens above already INCLUDES both, so
        # cost estimation stays correct without subtracting.
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0
        self.last_cache_read_tokens = 0
        self.last_cache_creation_tokens = 0

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        with self._lock:
            self.last_input_tokens = int(input_tokens or 0)
            self.last_output_tokens = int(output_tokens or 0)
            self.last_cache_read_tokens = int(cache_read_tokens or 0)
            self.last_cache_creation_tokens = int(cache_creation_tokens or 0)
            self.total_input_tokens += self.last_input_tokens
            self.total_output_tokens += self.last_output_tokens
            self.total_cache_read_tokens += self.last_cache_read_tokens
            self.total_cache_creation_tokens += self.last_cache_creation_tokens
            self.total_calls += 1

    def reset(self) -> None:
        """Zero out the cumulative counters."""
        with self._lock:
            self.total_input_tokens = 0
            self.total_output_tokens = 0
            self.total_calls = 0
            self.last_input_tokens = 0
            self.last_output_tokens = 0
            self.total_cache_read_tokens = 0
            self.total_cache_creation_tokens = 0
            self.last_cache_read_tokens = 0
            self.last_cache_creation_tokens = 0

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of cumulative input tokens served from the prompt cache.

        Zero if no calls, or if the provider doesn't report cache stats,
        or if the run wasn't cached. Useful as a quick diagnostic:
        ``print(model.usage.cache_hit_ratio)`` should be > 0 on the
        second call in a session when ``enable_prompt_cache=True``.
        """
        if self.total_input_tokens <= 0:
            return 0.0
        return self.total_cache_read_tokens / self.total_input_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def last_total_tokens(self) -> int:
        return self.last_input_tokens + self.last_output_tokens

    def estimate_cost(self, input_price_per_1k: float, output_price_per_1k: float) -> float:
        """Multiply cumulative tokens by per-1k prices. Caller provides
        the prices (model + provider specific; pull from their pricing page)."""
        return (
            (self.total_input_tokens / 1000.0) * input_price_per_1k
            + (self.total_output_tokens / 1000.0) * output_price_per_1k
        )

    def __repr__(self) -> str:
        return (
            f"TokenUsage(calls={self.total_calls}, "
            f"input={self.total_input_tokens}, output={self.total_output_tokens}, "
            f"total={self.total_tokens})"
        )


def _attach_media(messages: List[Dict[str, Any]], media: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """Return ``messages`` with ``media`` added to the LAST user turn.

    Lets every entry point take ``media=[...]`` the way the agent runner
    does: ``llm.invoke("Describe this", media=["photo.jpg"])``. A string
    user turn becomes ``[text, *media]``; a list turn gets the media
    appended. With no user turn, a media-only user turn is added. The
    caller's list and dicts are not mutated."""
    from agentx_dev.Media import to_media_part
    parts = [to_media_part(m) for m in (media or [])]
    if not parts:
        return messages
    out = [dict(m) if isinstance(m, dict) else m for m in messages]
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                m["content"] = list(content) + parts
            elif content:
                m["content"] = [{"type": "text", "text": str(content)}] + parts
            else:
                m["content"] = parts
            return out
    out.append({"role": "user", "content": parts})
    return out


class StructuredOutputRunnable:
    """LangChain-style wrapper produced by ``BaseChatModel.with_structured_output``.

    Calling ``.invoke(input)`` (or piping into one) forces the bound chat model
    to call the schema as a tool, validates the result with Pydantic, and
    returns the parsed instance. ``.ainvoke`` is the async sibling.

    ``input`` accepts either:
      - a ``str`` — wrapped as ``[{"role": "user", "content": input}]``
      - a list of message dicts — passed through unchanged
      - a dict with a ``"messages"`` key — common when piped from a prompt
        template (handles the ``{"ocr_text": RunnablePassthrough()} |
        prompt_template | llm.with_structured_output(...)`` pattern)
    """

    def __init__(
        self,
        model: "BaseChatModel",
        schema: Type[BaseModel],
        method: str = "function_calling",
        include_raw: bool = False,
    ):
        if method not in ("function_calling",):
            raise ValueError(
                f"with_structured_output method={method!r} is not supported "
                "(only 'function_calling' is implemented)"
            )
        self.model = model
        self.schema = schema
        self.method = method
        self.include_raw = include_raw
        from agentx_dev.Agents.Agent import to_tool_spec
        self._tool_spec = to_tool_spec(schema)
        self._tool_name = schema.__name__

    @staticmethod
    def _to_messages(input_value: Any) -> List[Dict[str, Any]]:
        if isinstance(input_value, str):
            return [{"role": "user", "content": input_value}]
        if isinstance(input_value, dict) and "messages" in input_value:
            return list(input_value["messages"])
        if isinstance(input_value, list):
            return input_value
        raise TypeError(
            f"StructuredOutputRunnable expected str, message list, or dict with "
            f"'messages'; got {type(input_value).__name__}"
        )

    def invoke(self, input_value: Any, *, media: Optional[Iterable[Any]] = None) -> Any:
        """Run the forced schema call. ``media=[...]`` attaches images /
        PDFs / audio to the last user turn, e.g. a scanned document for a
        verification schema."""
        messages = _attach_media(self._to_messages(input_value), media)
        result = self.model.call_with_tools(
            messages=messages,
            tools=[self._tool_spec],
            force_tool=self._tool_name,
        )
        return self._parse(result)

    async def ainvoke(self, input_value: Any, *, media: Optional[Iterable[Any]] = None) -> Any:
        messages = _attach_media(self._to_messages(input_value), media)
        result = await self.model.async_call_with_tools(
            messages=messages,
            tools=[self._tool_spec],
            force_tool=self._tool_name,
        )
        return self._parse(result)

    __call__ = invoke

    def _parse(self, result: Dict[str, Any]):
        if result.get("type") != "tool_use":
            # A provider that couldn't honour the forced tool choice (see
            # GPT's Responses fallback) may answer with the JSON as text.
            # Accept it if it validates; otherwise say what came back.
            text = result.get("text", "") or ""
            try:
                from agentx_dev.Agents.Agent import convert_to_json
                data = convert_to_json(text)
                if isinstance(data, dict):
                    parsed = self.schema(**data)
                    return {"raw": result, "parsed": parsed} if self.include_raw else parsed
            except Exception:
                pass
            raise ValueError(
                f"Model returned text instead of a {self._tool_name!r} tool call: "
                f"{text[:200]!r}"
            )
        parsed = self.schema(**result["input"])
        if self.include_raw:
            return {"raw": result, "parsed": parsed}
        return parsed

    def __or__(self, other):
        """Right-side composition: ``runnable | downstream`` calls
        ``downstream(self.invoke(input))``."""
        runnable = self

        class _Piped:
            def invoke(self, input_value):
                return other(runnable.invoke(input_value))

            async def ainvoke(self, input_value):
                return other(await runnable.ainvoke(input_value))

            __call__ = invoke

        return _Piped()

    def __ror__(self, other):
        """Left-side composition: ``upstream | self`` calls
        ``self.invoke(upstream(input))``. ``upstream`` may be any callable
        (function, prompt template, etc.) that returns input acceptable to
        ``_to_messages``."""
        runnable = self

        class _Piped:
            def invoke(self, input_value):
                upstream_out = other.invoke(input_value) if hasattr(other, "invoke") else other(input_value)
                return runnable.invoke(upstream_out)

            async def ainvoke(self, input_value):
                if hasattr(other, "ainvoke"):
                    upstream_out = await other.ainvoke(input_value)
                elif hasattr(other, "invoke"):
                    upstream_out = other.invoke(input_value)
                else:
                    upstream_out = other(input_value)
                return await runnable.ainvoke(upstream_out)

            __call__ = invoke

        return _Piped()


# ----------------------------------------------------------------------
# Model-compatibility adaptation
# ----------------------------------------------------------------------
# Model generations disagree about which request parameters they accept,
# and the disagreement moves every release:
#   * reasoning_effort -- absent on gpt-4o-era models; low/medium/high on
#     o1/o3/o4; minimal..high on gpt-5; none..high on gpt-5.1; some newer
#     models add xhigh and drop none.
#   * max_tokens -- rejected by reasoning models, which want
#     max_completion_tokens.
#   * temperature -- reasoning models accept only the default.
#   * Claude: temperature + top_p together are rejected by newer models,
#     thinking is rejected by older ones, max_tokens has per-model caps.
# A static table of "what model X accepts" goes stale the week a model
# ships. Instead the providers say exactly what they reject in the 400
# body ("Unsupported value: 'reasoning_effort' does not support 'none'
# ... Supported values are: 'low', 'medium', 'high'"). The models below
# read that, make the smallest adjustment that satisfies it, retry, and
# remember the adjustment per model so later calls never pay the 400.
# Every adjustment is logged. Pass adapt_params=False to get the raw
# provider error instead.

_MAX_PARAM_FIXES = 4
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh")
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
# Never adapted away -- these define the request itself.
_PROTECTED_PARAMS = frozenset({"model", "messages", "tools", "tool_choice",
                               "stream", "system"})


def _is_openai_reasoning_model(model: Any) -> bool:
    name = str(model or "").lower().rsplit("/", 1)[-1]
    return name.startswith(_OPENAI_REASONING_PREFIXES)


def _nearest_effort(requested: Any, supported: List[str]) -> Optional[str]:
    """Closest supported reasoning_effort to the requested one, on the
    none < minimal < low < medium < high < xhigh scale."""
    ordered = [v for v in _EFFORT_ORDER if v in supported]
    if not ordered:
        return supported[0] if supported else None
    try:
        want = _EFFORT_ORDER.index(str(requested))
    except ValueError:
        return ordered[0]
    return min(ordered, key=lambda v: (abs(_EFFORT_ORDER.index(v) - want),
                                       -_EFFORT_ORDER.index(v)))


def _error_text(exc: Exception) -> str:
    """The provider's own error message, once.

    Prefer the structured body: the SDK's ``str(exc)`` / ``.message`` embed
    a repr of that same body, so concatenating both duplicated every
    phrase -- and a parser reading "Supported values are: ..." then also
    picked up the REJECTED value from the other copy."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
    return str(getattr(exc, "message", None) or exc)


def _is_set(kwargs: Dict[str, Any], key: str) -> bool:
    return key in kwargs and kwargs[key] is not NOT_GIVEN and kwargs[key] is not None


def _openai_param_fix(exc: Exception, kwargs: Dict[str, Any]):
    """Map an OpenAI 400 to one parameter adjustment.

    Returns ``(param, action)`` with action ``("drop",)``,
    ``("rename", new_name)`` or ``("value", new_value)``; ``None`` when the
    error isn't a parameter-compatibility error we can act on."""
    if getattr(exc, "status_code", None) != 400:
        return None
    code = getattr(exc, "code", None)
    param = getattr(exc, "param", None)
    text = _error_text(exc)

    if not param:
        m = (re.search(r"Unsupported (?:parameter|value): '([^']+)'", text)
             or re.search(r"Unrecognized request argument supplied: (\w+)", text))
        if m:
            param = m.group(1)
    # The Responses API nests it as reasoning.effort; the framework keeps it
    # flat as reasoning_effort on both endpoints, so one learned fix
    # covers both.
    if param and param.startswith("reasoning"):
        param = "reasoning_effort"
    if not param or param in _PROTECTED_PARAMS or not _is_set(kwargs, param):
        return None

    unsupported_param = (code == "unsupported_parameter"
                         or "Unsupported parameter" in text
                         or "Unrecognized request argument" in text)
    unsupported_value = code == "unsupported_value" or "Unsupported value" in text
    if not (unsupported_param or unsupported_value):
        return None

    if unsupported_param:
        m = re.search(r"Use '([^']+)' instead", text)
        if m and m.group(1) not in kwargs:
            return param, ("rename", m.group(1))
        return param, ("drop",)

    # Unsupported value: move to the nearest value the model lists, else
    # drop the parameter and let the model use its default.
    tail = text.split("Supported values are:", 1)
    if len(tail) == 2 and param == "reasoning_effort":
        # Values end at the sentence's full stop; nothing after it counts.
        supported = re.findall(r"'([^']+)'", tail[1].split(".", 1)[0])
        choice = _nearest_effort(kwargs[param], supported)
        if choice and choice != kwargs[param]:
            return param, ("value", choice)
    return param, ("drop",)


# Claude: which optional params to give up first when a 400 names several.
_ANTHROPIC_OPTIONAL = ("top_k", "top_p", "temperature", "thinking", "stop_sequences")


def _anthropic_param_fix(exc: Exception, kwargs: Dict[str, Any]):
    """Map an Anthropic 400 to one parameter adjustment (see
    ``_openai_param_fix`` for the return shape)."""
    if getattr(exc, "status_code", None) != 400:
        return None
    text = _error_text(exc)
    low = text.lower()

    m = re.search(r"max_tokens:\s*(\d+)\s*>\s*(\d+)", text)
    if m and _is_set(kwargs, "max_tokens"):
        limit = int(m.group(2))
        if limit < int(kwargs["max_tokens"]):
            return "max_tokens", ("value", limit)

    for name in _ANTHROPIC_OPTIONAL:
        if _is_set(kwargs, name) and re.search(rf"\b{name}\b", low):
            return name, ("drop",)
    return None


def _apply_action(kwargs: Dict[str, Any], param: str, action: tuple) -> None:
    if not _is_set(kwargs, param):
        return
    if action[0] == "drop":
        kwargs.pop(param, None)
    elif action[0] == "rename":
        kwargs[action[1]] = kwargs.pop(param)
    elif action[0] == "value":
        kwargs[param] = action[1]


def _describe(param: str, action: tuple, old: Any) -> str:
    if action[0] == "drop":
        return f"dropped {param}={old!r}"
    if action[0] == "rename":
        return f"sent {param} as {action[1]}"
    return f"changed {param} {old!r} -> {action[1]!r}"


class _ParamAdapter:
    """Per-model memory of parameter adjustments, shared by GPT and Claude."""

    def __init__(self, provider: str, fixer, enabled: bool):
        self.provider = provider
        self._fixer = fixer
        self.enabled = enabled
        self.learned: Dict[str, Dict[str, tuple]] = {}

    def apply(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(kwargs)
        for param, action in self.learned.get(str(out.get("model")), {}).items():
            _apply_action(out, param, action)
        return out

    def learn(self, kwargs: Dict[str, Any], exc: Exception) -> bool:
        """Record a fix for ``exc`` and apply it to ``kwargs`` in place.
        False when the error isn't one we can adapt to."""
        if not self.enabled:
            return False
        fix = self._fixer(exc, kwargs)
        if fix is None:
            return False
        param, action = fix
        old = kwargs.get(param)
        model = str(kwargs.get("model"))
        self.learned.setdefault(model, {})[param] = action
        _apply_action(kwargs, param, action)
        logger.warning(
            f"{self.provider} model {model!r} rejected a request parameter; "
            f"{_describe(param, action, old)} and retried. Remembered for this "
            f"model. (Provider said: {_error_text(exc)[:200]})"
        )
        return True

    def call(self, fn, kwargs: Dict[str, Any]):
        kwargs = self.apply(kwargs)
        for _ in range(_MAX_PARAM_FIXES):
            try:
                return fn(**kwargs)
            except Exception as e:
                if not self.learn(kwargs, e):
                    raise
        return fn(**kwargs)

    async def acall(self, fn, kwargs: Dict[str, Any]):
        kwargs = self.apply(kwargs)
        for _ in range(_MAX_PARAM_FIXES):
            try:
                return await fn(**kwargs)
            except Exception as e:
                if not self.learn(kwargs, e):
                    raise
        return await fn(**kwargs)


def _anthropic_text(response: Any) -> str:
    """Join the text blocks of a Messages response.

    ``response.content[0].text`` breaks on any response whose first block
    isn't text -- with extended thinking the first block is a ``thinking``
    block, so the old accessor raised or returned the wrong thing."""
    parts = [getattr(b, "text", "") for b in (getattr(response, "content", None) or [])
             if getattr(b, "type", None) == "text"]
    return "".join(parts)


def _system_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


# ----------------------------------------------------------------------
# OpenAI Responses API (/v1/responses)
# ----------------------------------------------------------------------
# Some OpenAI models refuse function tools combined with reasoning on
# /v1/chat/completions and point at /v1/responses instead, e.g.
#   "Function tools with reasoning_effort are not supported for gpt-6-astra
#    in /v1/chat/completions. To use function tools, use /v1/responses or
#    set reasoning_effort to 'none'."
# When the model also rejects 'none', the Responses API is the ONLY way to
# call tools on it. GPT therefore switches a model's tool calls to the
# Responses API when the provider says to, remembers the switch, and keeps
# the rest of the framework's chat-completions-shaped history unchanged --
# the translation happens only at the wire.

# Parameters that exist on responses.create; anything else from the chat
# defaults (seed, stop, n, penalties, logit_bias, ...) has no Responses
# equivalent and is left out, with one WARNING.
_RESPONSES_PARAMS = frozenset({
    "model", "input", "instructions", "tools", "tool_choice", "reasoning",
    "max_output_tokens", "temperature", "top_p", "parallel_tool_calls",
    "store", "metadata", "user", "service_tier", "top_logprobs",
    "extra_headers", "extra_query", "extra_body", "timeout",
})


def _wants_responses_api(exc: Exception) -> bool:
    """True when OpenAI says this request must go through /v1/responses."""
    return getattr(exc, "status_code", None) == 400 and "/v1/responses" in _error_text(exc)


def _normalize_tool_spec_for_openai_responses(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Chat-completions or generic tool spec -> Responses function tool.

    Responses tools are flat (no nested ``function`` key). ``strict`` is
    False because framework schemas aren't guaranteed to meet strict
    mode's requirements (every property required, no extra keys)."""
    if spec.get("type") == "function" and "function" in spec:
        fn = spec["function"]
        name, desc, params = fn["name"], fn.get("description", ""), fn.get("parameters", {})
    elif spec.get("type") == "function" and "name" in spec and "function" not in spec:
        return spec                                   # already Responses-shaped
    else:
        name = spec["name"]
        desc = spec.get("description", "")
        params = spec.get("parameters") or spec.get("input_schema") or {}
    return {"type": "function", "name": name, "description": desc,
            "parameters": params or {"type": "object", "properties": {}},
            "strict": False}


def _messages_for_openai_responses(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Framework (chat-completions-shaped) history -> Responses ``input`` items.

    - assistant turns with ``tool_calls`` -> ``function_call`` items
    - ``role="tool"`` results            -> ``function_call_output`` items
    - everything else                    -> role/content messages, media
                                            rendered as input_* parts

    ``function_call`` items are sent WITHOUT an ``id``: an item that carries
    the server-side ``fc_`` id must be accompanied by its reasoning item,
    which a stateless client doesn't keep. ``call_id`` alone pairs each
    call with its output."""
    items: List[Dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "assistant" and m.get("tool_calls"):
            text = _system_text(content) if isinstance(content, list) else (content or "")
            if text:
                items.append({"role": "assistant", "content": text})
            for call in m["tool_calls"]:
                fn = call.get("function", {})
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "call_unknown",
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments") or "{}",
                })
            continue
        if role in ("tool", "function") and m.get("tool_call_id"):
            items.append({"type": "function_call_output",
                          "call_id": m["tool_call_id"],
                          "output": content if isinstance(content, str) else json.dumps(content, default=str)})
            continue
        if role not in ("system", "developer", "user", "assistant"):
            # Legacy role="function" without an id: an observation for the model.
            role = "user"
        if role == "assistant":
            # Assistant history is plain text on this endpoint.
            text = _system_text(content) if isinstance(content, list) else (content or "")
            if not text:
                continue
            items.append({"role": "assistant", "content": text})
            continue
        if content is None or content == "":
            continue
        items.append({"role": role, "content": content_for_openai_responses(content)})
    return items


def _parse_responses_output(response: Any) -> Dict[str, Any]:
    """Responses output items -> the framework's call_with_tools result."""
    tool_calls = []
    texts = []
    for item in getattr(response, "output", None) or []:
        itype = getattr(item, "type", None)
        if itype == "function_call":
            raw = getattr(item, "arguments", "") or "{}"
            args, arg_error = _parse_tool_arguments(raw)
            call_id = getattr(item, "call_id", None) or getattr(item, "id", None)
            if arg_error is not None:
                logger.error(f"Model returned non-JSON tool arguments for "
                             f"'{getattr(item, 'name', '')}': {raw!r}")
                return {"type": "invalid_tool_args", "name": getattr(item, "name", ""),
                        "id": call_id, "raw": raw, "error": arg_error}
            tool_calls.append({"name": getattr(item, "name", ""), "input": args, "id": call_id})
        elif itype == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "output_text":
                    texts.append(getattr(part, "text", "") or "")
    if tool_calls:
        first = tool_calls[0]
        return {"type": "tool_use", "name": first["name"], "input": first["input"],
                "id": first["id"], "tool_calls": tool_calls}
    return {"type": "text", "text": "".join(texts)}


def _normalize_tool_spec_for_openai(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either a generic ``to_tool_spec`` dict or a pre-built OpenAI tool dict."""
    if spec.get("type") == "function" and "function" in spec:
        return spec
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec.get("description", ""),
            "parameters": spec.get("parameters") or spec.get("input_schema") or {},
        },
    }


def _messages_for_openai(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pass-through that preserves the OpenAI tool-use convention.

    The agentx runner writes assistant messages with a ``tool_calls`` list
    and tool replies with ``tool_call_id`` (see #15 in TODO.md). Both are
    already the exact shape OpenAI's chat completions API expects, so we
    just copy through. Plain ``{role, content}`` dicts pass through
    unchanged too — the OpenAI API accepts both.
    """
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        # Defensive copy so the runner's history isn't mutated by the SDK.
        copy = {k: v for k, v in m.items()}
        # Media parts (Media objects, canonical blocks, or Anthropic
        # blocks) become OpenAI image_url / file / input_audio parts.
        if isinstance(copy.get("content"), list) or not isinstance(
                copy.get("content"), (str, type(None))):
            copy["content"] = content_for_openai(copy["content"])
        out.append(copy)
    return out


def _messages_for_anthropic(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate agentx normalized messages to Anthropic content-block format.

    The runner stores assistant tool-uses as
    ``{role: 'assistant', tool_calls: [{id, function: {name, arguments}}]}``
    and tool results as ``{role: 'tool', tool_call_id: ..., content: ...}``.
    Anthropic expects content BLOCKS:
      - assistant with tool_use: ``{role: 'assistant', content: [{type: 'tool_use', id, name, input}]}``
      - tool result: ``{role: 'user', content: [{type: 'tool_result', tool_use_id, content}]}``

    Plain ``{role, content: str}`` dicts pass through unchanged.
    """
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        role = m.get("role")

        # Assistant with tool_calls — translate to content blocks.
        if role == "assistant" and m.get("tool_calls"):
            blocks = []
            text = m.get("content") or ""
            if text:
                blocks.append({"type": "text", "text": text})
            for call in m["tool_calls"]:
                fn = call.get("function", {})
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    inp = {}
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id") or "call_unknown",
                    "name": fn.get("name", ""),
                    "input": inp,
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        # Tool result — translate to user message with tool_result block.
        if role in ("tool", "function") and m.get("tool_call_id"):
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": str(m.get("content", "")),
                }],
            })
            continue

        # Everything else — pass through, rendering any media parts
        # (Media objects, canonical blocks, or OpenAI-native parts) as
        # Anthropic image / document blocks.
        out.append({"role": m.get("role"),
                    "content": content_for_anthropic(m.get("content", ""))})
    return out


def _stamp_cache_on_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``msg`` whose content ends with a cache_control block.

    Anthropic's cache_control marker attaches to a content BLOCK, not to a
    message. If the message content is already a list of blocks, we tag
    the last one; if it's a plain string, we wrap it as a one-block list
    with the marker.
    """
    role = msg.get("role")
    content = msg.get("content")
    if isinstance(content, list) and content:
        new_blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        last = new_blocks[-1]
        if isinstance(last, dict):
            new_blocks[-1] = {**last, "cache_control": {"type": "ephemeral"}}
        return {"role": role, "content": new_blocks}
    if isinstance(content, str) and content:
        return {
            "role": role,
            "content": [{
                "type": "text",
                "text": content,
                "cache_control": {"type": "ephemeral"},
            }],
        }
    return msg


def _normalize_tool_spec_for_anthropic(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either a generic ``to_tool_spec`` dict or a pre-built Anthropic tool dict."""
    if "input_schema" in spec and "name" in spec:
        return spec
    if spec.get("type") == "function" and "function" in spec:
        fn = spec["function"]
        return {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}),
        }
    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "input_schema": spec.get("parameters", {}),
    }


class BaseChatModel(ABC):
    """Abstract base class. All chat model integrations must implement Initialize().

    Operational controls (apply to every retry path):

      - ``rate_limit_per_sec``: requests per second (token-bucket; ``None`` = no
        limit). When set, every retry attempt counts against the bucket so a
        burst-then-retry storm gets throttled like any other traffic.
      - ``retry_budget``: max total retries across the whole model lifetime.
        Once exhausted, ``_with_retry`` raises ``RetryBudgetExceeded`` instead
        of issuing yet another attempt. ``None`` = unbounded (legacy).

    Both are off by default so existing tests don't change behavior. Configure
    via ``configure_limits(rate_limit_per_sec=..., retry_budget=...)`` after
    construction (sets are applied to the same model instance).
    """

    _rate_limiter: Optional[TokenBucket] = None
    _retry_budget: Optional[int] = None
    _retries_used: int = 0
    _retries_lock: Any = None  # threading.Lock — Lock() is a factory, not a class
    _usage: Optional["TokenUsage"] = None

    @property
    def usage(self) -> "TokenUsage":
        """Cumulative token usage for this model instance.

        Subclasses (GPT, Claude) call ``self.usage.record(input, output)``
        after every API response that returns a usage block. Lazy-init so
        BaseChatModel instances without subclass init logic still work.
        """
        if self._usage is None:
            self._usage = TokenUsage()
        return self._usage

    def configure_limits(
        self,
        *,
        rate_limit_per_sec: float | None = None,
        rate_limit_burst: float | None = None,
        retry_budget: int | None = None,
        budget_usd: float | None = None,
        input_price_per_1k: float | None = None,
        output_price_per_1k: float | None = None,
    ) -> "BaseChatModel":
        """Set per-instance operational limits. Idempotent.

        Args:
            rate_limit_per_sec: Token-bucket request rate.
            rate_limit_burst: Max burst size (defaults to ``max(rate_limit_per_sec, 1.0)``).
            retry_budget: Lifetime cap on retries. Raises
                ``RetryBudgetExceeded`` when exhausted.
            budget_usd: Hard cost cap. After every API response that
                returns usage, recompute spend via
                ``input_price_per_1k`` × input + ``output_price_per_1k``
                × output. Raises ``CostBudgetExceeded`` the moment
                cumulative spend crosses ``budget_usd``. Requires both
                price args; raises ``ValueError`` if either is missing.
            input_price_per_1k / output_price_per_1k: Provider prices.
                Pull from the model's pricing page — the framework
                doesn't bake in a pricing table (changes too often).
        """
        if rate_limit_per_sec is not None:
            burst = rate_limit_burst if rate_limit_burst is not None else max(rate_limit_per_sec, 1.0)
            self._rate_limiter = TokenBucket(capacity=burst, refill_per_sec=rate_limit_per_sec)
        self._retry_budget = retry_budget
        self._retries_used = 0
        if self._retries_lock is None:
            self._retries_lock = _threading.Lock()

        if budget_usd is not None:
            if input_price_per_1k is None or output_price_per_1k is None:
                raise ValueError(
                    "configure_limits(budget_usd=...) requires both "
                    "input_price_per_1k and output_price_per_1k so spend "
                    "can be computed from usage. Pull current prices from "
                    "the model's pricing page."
                )
            self._cost_budget_usd = float(budget_usd)
            self._input_price_per_1k = float(input_price_per_1k)
            self._output_price_per_1k = float(output_price_per_1k)
        return self

    def _check_cost_budget(self) -> None:
        """Raise ``CostBudgetExceeded`` if cumulative spend has crossed
        the configured ``budget_usd``. No-op if no budget set.

        Called by ``_record_usage_counts`` immediately after every
        ``self.usage.record(...)``. Enforcing here (not in
        TokenUsage.record itself) keeps TokenUsage provider-agnostic and
        avoids the dep cycle TokenUsage → BaseChatModel → TokenUsage.
        """
        if getattr(self, "_cost_budget_usd", None) is None:
            return
        spent = self.usage.estimate_cost(
            input_price_per_1k=self._input_price_per_1k,
            output_price_per_1k=self._output_price_per_1k,
        )
        if spent > self._cost_budget_usd:
            raise CostBudgetExceeded(spent_usd=spent, limit_usd=self._cost_budget_usd)

    def _record_usage_counts(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Single funnel for usage recording — every subclass call site
        goes through here so cost-budget enforcement applies uniformly to
        non-streaming, streaming, and tool-calling paths.

        ``cache_read_tokens`` / ``cache_creation_tokens`` are optional
        Anthropic prompt-cache counters. Providers that don't cache pass
        0 (the default) and the cache-ratio metric on TokenUsage stays 0.
        """
        self.usage.record(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        self._check_cost_budget()

    def _consume_retry_budget(self) -> None:
        if self._retry_budget is None:
            return
        if self._retries_lock is None:
            self._retries_lock = _threading.Lock()
        with self._retries_lock:
            if self._retries_used >= self._retry_budget:
                raise RetryBudgetExceeded(
                    f"Retry budget of {self._retry_budget} exhausted on "
                    f"{type(self).__name__}; refusing further retries."
                )
            self._retries_used += 1

    @abstractmethod
    def Initialize(self, messages) -> str:
        """Send messages to the LLM and return the response string.

        Kept abstract for backward compatibility (existing subclasses and
        tests implement this name). Prefer ``invoke`` in new code — it's
        the same thing.
        """
        ...

    @staticmethod
    def _coerce_messages(messages: Any) -> List[Dict[str, Any]]:
        """Normalize the ``invoke`` / ``ainvoke`` input into an OpenAI-style
        message list.

        Matches the shape ``StructuredOutputRunnable._to_messages`` accepts,
        so ``llm.invoke("hi")`` and
        ``llm.with_structured_output(S).invoke("hi")`` no longer diverge —
        the historical hazard was that the structured path silently wrapped
        a bare string while the plain ``invoke`` forwarded it straight to
        ``client.chat.completions.create(messages="hi")`` which errored
        with a provider-side ``Invalid type for 'messages'``. The type hint
        (``Iterable[ChatCompletionMessageParam]``) can't catch it because
        ``str`` is an ``Iterable``.

        Accepts:
          - ``str`` — wrapped as one user message
          - ``list`` of dicts — passed through unchanged
          - dict with a ``"messages"`` key — piped-prompt shape
        """
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        if isinstance(messages, list):
            return messages
        if isinstance(messages, dict) and "messages" in messages:
            return list(messages["messages"])
        # Anything else falls through unchanged — subclasses that accept
        # exotic shapes still work; only the string case is normalized.
        return messages

    def invoke(self, messages, *, media: Optional[Iterable[Any]] = None) -> str:
        """Canonical entry point. Alias for ``Initialize`` with input
        normalization. See ``_coerce_messages`` for the accepted shapes.

        ``media=[...]`` attaches images / PDFs / audio (paths, URLs,
        ``Media``, or part dicts) to the last user turn -- the same
        argument ``AgentRunner.invoke`` takes."""
        return self.Initialize(_attach_media(self._coerce_messages(messages), media))

    async def async_initialize(self, messages) -> str:
        """
        Async wrapper around Initialize(). Runs in a thread pool so it
        does not block the event loop. Override for a native async implementation.

        Applies the same input normalization as ``invoke`` so that
        ``ainvoke("hi")`` and ``ainvoke([{'role': 'user', ...}])`` both work
        — and neither silently forwards a bare string to the provider.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.Initialize, self._coerce_messages(messages)
        )

    async def ainvoke(self, messages, *, media: Optional[Iterable[Any]] = None) -> str:
        """Canonical async entry point. Alias for ``async_initialize``;
        takes ``media=`` like ``invoke``."""
        return await self.async_initialize(_attach_media(self._coerce_messages(messages), media))

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        force_tool: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Native function-calling entry point.

        Implementations must return a dict of the form::

            {"type": "tool_use", "name": str, "input": dict}
            # or
            {"type": "text", "text": str}

        ``tools`` accepts either the provider-native spec or the generic
        ``to_tool_spec`` shape — each subclass normalizes as needed.

        Subclasses without a native function-calling backend may leave this
        unimplemented; callers should fall back to ``Initialize`` + JSON
        parsing in that case.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement call_with_tools"
        )

    async def async_call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        force_tool: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Async wrapper. Override for native async implementations."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.call_with_tools(messages, tools, force_tool=force_tool),
        )

    def stream_text(self, messages: List[Dict[str, Any]]) -> Iterator[str]:
        """Yield text deltas from the LLM as they arrive.

        Default implementation falls back to a single yield containing the
        whole response from ``Initialize`` — non-streaming providers can
        rely on this without overriding. Subclasses with native streaming
        (GPT, Claude) override to yield real token-level deltas.
        """
        yield self.Initialize(messages)

    async def astream_text(self, messages: List[Dict[str, Any]]) -> AsyncIterator[str]:
        """Async sibling of ``stream_text``. Default falls back to a single
        yield over ``async_initialize``."""
        yield await self.async_initialize(messages)

    def with_structured_output(
        self,
        schema: Type[BaseModel],
        *,
        method: str = "function_calling",
        include_raw: bool = False,
    ) -> "StructuredOutputRunnable":
        """Return a runnable that forces the model to fill ``schema`` via tool-calling.

        Mirrors the LangChain API. Example::

            class Receipt(BaseModel):
                merchant: str
                total: float

            extractor = llm.with_structured_output(Receipt, method="function_calling")
            receipt = extractor.invoke("Coffee shop, $4.50")
            # → Receipt(merchant='Coffee shop', total=4.5)

        Composes with ``|`` so it can sit at the end of a pipeline::

            pipeline = prompt_template | llm.with_structured_output(Receipt)
            receipt = pipeline.invoke({"ocr_text": "..."})

        Args:
            schema: A Pydantic ``BaseModel`` subclass.
            method: Currently only ``"function_calling"`` is supported.
            include_raw: If True, ``.invoke`` returns ``{"raw": <tool-use
                dict>, "parsed": <schema instance>}`` instead of the parsed
                instance alone.
        """
        return StructuredOutputRunnable(
            model=self, schema=schema, method=method, include_raw=include_raw,
        )

    @staticmethod
    def _is_non_retryable(exc: Exception) -> bool:
        """Return True for HTTP 4xx client errors that won't succeed on
        retry (bad request, auth error, permission error, not found,
        unprocessable entity, etc.).

        Exceptions from the retry rule:
          - 408 Request Timeout — genuinely transient, retry
          - 429 Too Many Requests — the whole point of exponential backoff

        Detection via a ``status_code`` attribute matches how the OpenAI
        and Anthropic SDKs raise their errors (openai.BadRequestError,
        anthropic.AuthenticationError, etc. — all subclass their SDK's
        APIStatusError which carries status_code).
        """
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            return False
        if not (400 <= status < 500):
            return False
        # 408 (timeout) and 429 (rate limit) ARE retryable — don't skip.
        return status not in (408, 429)

    def _with_retry(self, fn, max_retries: int = 3, base_delay: float = 0.1):
        """
        Call fn() with exponential backoff on failure.

        Honors per-instance rate limit (token bucket on every attempt) and
        retry budget (counts each retry against a lifetime cap; raises
        ``RetryBudgetExceeded`` when used up). Both default to off.

        Aborts retries immediately on non-retryable 4xx errors (400 bad
        request, 401 auth error, 403 permission denied, 404 not found,
        422 unprocessable). Those aren't transient — retrying just
        wastes wall-clock + tokens + retry budget. 408 and 429 remain
        retryable (transient by definition).
        """
        import time
        last_exc = None
        for attempt in range(max_retries):
            if self._rate_limiter is not None:
                self._rate_limiter.acquire()
            try:
                return fn()
            except Exception as e:
                last_exc = e
                if self._is_non_retryable(e):
                    # DEBUG, not WARNING: the caller either recovers (a
                    # parameter fix or the Responses API switch, each with
                    # its own WARNING) or logs the final failure itself.
                    # At WARNING this printed the original rejection text
                    # even on calls that then succeeded, which read as the
                    # old error coming back.
                    logger.debug(
                        f"LLM call failed with non-retryable error "
                        f"(HTTP {getattr(e, 'status_code', '?')}): {e}. "
                        "Skipping retries."
                    )
                    raise
                if attempt < max_retries - 1:
                    # Count this against the budget; raises if exhausted.
                    self._consume_retry_budget()
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"LLM call attempt {attempt + 1} failed ({e}), retrying in {delay:.2f}s")
                    time.sleep(delay)
        raise last_exc


class GPT(BaseChatModel):
    """
    Wrapper class for interacting with OpenAI's Chat Completions API using configurable defaults.

    Attributes:
        api_key (Optional[str]): API key for OpenAI. Defaults to the 'OPENAI_API_KEY' environment variable.
        client (OpenAI): An instance of the OpenAI client.
        defaults (dict): Default parameters for chat completion requests.
        timeout (float): Timeout in seconds for requests.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        organization: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        model: str = "gpt-4o",
        temperature: float | NotGiven = NOT_GIVEN,
        max_tokens: int | NotGiven = NOT_GIVEN,
        top_p: float | NotGiven = NOT_GIVEN,
        n: int | NotGiven = NOT_GIVEN,
        stream_options: Any | NotGiven = NOT_GIVEN,
        stop: Union[str, List[str]] | NotGiven = NOT_GIVEN,
        presence_penalty: float | NotGiven = NOT_GIVEN,
        frequency_penalty: float | NotGiven = NOT_GIVEN,
        logit_bias: Dict[str, int] | NotGiven = NOT_GIVEN,
        user: str | NotGiven = NOT_GIVEN,
        functions: Iterable[Function] | NotGiven = NOT_GIVEN,
        function_call: FunctionCall | NotGiven = NOT_GIVEN,
        tool_choice: Any | NotGiven = NOT_GIVEN,
        tools: Iterable[Any] | NotGiven = NOT_GIVEN,
        logprobs: bool | NotGiven = NOT_GIVEN,
        top_logprobs: int | NotGiven = NOT_GIVEN,
        response_format: ResponseFormat | NotGiven = NOT_GIVEN,
        seed: int | NotGiven = NOT_GIVEN,
        service_tier: Literal["auto", "default"] | NotGiven = NOT_GIVEN,
        metadata: Dict[str, str] | NotGiven = NOT_GIVEN,
        store: bool | NotGiven = NOT_GIVEN,
        parallel_tool_calls: bool | NotGiven = NOT_GIVEN,
        reasoning_effort: str | NotGiven = NOT_GIVEN,
        adapt_params: bool = True,
        use_responses_api: Optional[bool] = None,
    ):
        """
        ``reasoning_effort`` accepts any value a model generation uses
        ("none", "minimal", "low", "medium", "high", "xhigh"). If the
        model rejects the value -- or the parameter -- the call adapts
        (see ``adapt_params``) instead of failing.

        ``adapt_params`` (default True): when the API rejects a request
        parameter for this model (e.g. ``reasoning_effort="none"`` on a
        model that only takes low/medium/high, ``max_tokens`` on a
        reasoning model, ``temperature`` on a model that only allows
        the default), make the smallest change the error asks for,
        retry, and remember it for this model. Logged at WARNING. Set
        False to surface the raw provider error instead.

        ``use_responses_api`` (default None = automatic): some models
        refuse function tools with reasoning on /v1/chat/completions and
        name /v1/responses as the fix. Automatic mode switches that
        model's tool calls to the Responses API when the provider says
        so, retries, and remembers it (logged at WARNING). True routes
        every non-streaming call through the Responses API; False never
        does and surfaces the provider error instead. Token streaming
        (``stream_text``) always uses chat completions.
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(
            api_key=self.api_key,
            organization=organization,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

        self.defaults = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "n": n,
            "stream_options": stream_options,
            "stop": stop,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "logit_bias": logit_bias,
            "user": user,
            "functions": functions,
            "function_call": function_call,
            "tool_choice": tool_choice,
            "tools": tools,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
            "response_format": response_format,
            "seed": seed,
            "service_tier": service_tier,
            "metadata": metadata,
            "store": store,
            "parallel_tool_calls": parallel_tool_calls,
            # OpenAI reasoning models (gpt-5.x, o1, o3, o4) accept a
            # 'reasoning_effort' parameter. On /v1/chat/completions,
            # function tools + reasoning_effort=medium/high can conflict
            # and produce a 400. Passing reasoning_effort='none' fixes it
            # (disables reasoning for this call); or route through
            # /v1/responses (not the default endpoint here).
            "reasoning_effort": reasoning_effort,
        }

        self.timeout = timeout
        self._params = _ParamAdapter("OpenAI", _openai_param_fix, adapt_params)
        self.use_responses_api = use_responses_api
        # Models the provider told us need /v1/responses for tool calls.
        self._responses_models: set = set()
        # Per-model tool_choice to use instead of a forced one.
        self._relaxed_tool_choice: Dict[str, Any] = {}
        self._warned_dropped: set = set()

    def _request_kwargs(self, exclude: Iterable[str] = ()) -> Dict[str, Any]:
        """Defaults for one request, minus ``exclude``, with known
        generation quirks pre-applied so the common case never costs a
        rejected call: reasoning models take ``max_completion_tokens``."""
        skip = set(exclude)
        kw = {k: v for k, v in self.defaults.items() if k not in skip}
        if (_is_openai_reasoning_model(kw.get("model")) and _is_set(kw, "max_tokens")
                and not _is_set(kw, "max_completion_tokens")):
            kw["max_completion_tokens"] = kw.pop("max_tokens")
        return kw

    def _create(self, **kwargs):
        return self._params.call(self.client.chat.completions.create, kwargs)

    def _responses_create_flat(self, **kw):
        """Call responses.create from FLAT framework kwargs.

        Parameter adaptation runs on the flat names (``reasoning_effort``,
        ``max_tokens``), so a fix learned on either endpoint applies to
        both; the nesting / renaming for the Responses API happens here,
        at the wire."""
        kw = dict(kw)
        effort = kw.pop("reasoning_effort", NOT_GIVEN)
        if effort is not NOT_GIVEN and effort is not None:
            kw["reasoning"] = {"effort": effort}
        tokens = NOT_GIVEN
        for name in ("max_completion_tokens", "max_tokens"):
            value = kw.pop(name, NOT_GIVEN)
            if tokens is NOT_GIVEN and value is not NOT_GIVEN and value is not None:
                tokens = value
        if tokens is not NOT_GIVEN:
            kw["max_output_tokens"] = tokens
        dropped = sorted(k for k, v in kw.items()
                         if k not in _RESPONSES_PARAMS and v is not NOT_GIVEN and v is not None)
        for k in list(kw):
            if k not in _RESPONSES_PARAMS:
                kw.pop(k)
        new_drops = [k for k in dropped if k not in self._warned_dropped]
        if new_drops:
            self._warned_dropped.update(new_drops)
            logger.warning(
                f"OpenAI Responses API has no equivalent for {new_drops}; "
                "those settings are not sent on this endpoint."
            )
        # Responses stores requests server-side by default; chat completions
        # does not. Keep the framework's behaviour the same on both.
        if kw.get("store", NOT_GIVEN) in (NOT_GIVEN, None):
            kw["store"] = False
        kw = {k: v for k, v in kw.items() if v is not NOT_GIVEN}
        return self.client.responses.create(**kw)

    def _uses_responses(self, model: Any, *, for_tools: bool) -> bool:
        if self.use_responses_api is True:
            return True
        if self.use_responses_api is False:
            return False
        return for_tools and str(model) in self._responses_models

    def _switch_to_responses(self, model: Any, exc: Exception) -> bool:
        """Record that ``model`` needs the Responses API for tool calls, if
        the provider said so and automatic mode is on."""
        if self.use_responses_api is not None or not _wants_responses_api(exc):
            return False
        self._responses_models.add(str(model))
        logger.warning(
            f"OpenAI model {str(model)!r} can't call tools on /v1/chat/completions "
            "with the current settings; switching its tool calls to the Responses "
            f"API (/v1/responses) and retrying. Remembered for this model. "
            f"(Provider said: {_error_text(exc)[:200]})"
        )
        return True

    def _record_responses_usage(self, response: Any) -> None:
        u = getattr(response, "usage", None)
        if u is not None:
            self._record_usage_counts(
                input_tokens=getattr(u, "input_tokens", 0) or 0,
                output_tokens=getattr(u, "output_tokens", 0) or 0,
            )

    def _call_with_tools_responses(self, messages, tools, force_tool):
        normalized = [_normalize_tool_spec_for_openai_responses(t) for t in tools]
        tool_choice: Any = {"type": "function", "name": force_tool} if force_tool else "auto"
        model_key = str(self.defaults.get("model"))
        if force_tool and model_key in self._relaxed_tool_choice:
            tool_choice = self._relaxed_tool_choice[model_key]
        kwargs = self._request_kwargs(
            ("tools", "tool_choice", "functions", "function_call", "response_format")
        )
        kwargs.update(input=_messages_for_openai_responses(list(messages)),
                      tools=normalized, tool_choice=tool_choice)
        # A forced tool choice is what structured output relies on. If the
        # model refuses one (with reasoning on, some do), relax it step by
        # step: 'required' still guarantees a tool call and, with a single
        # tool, it is the same tool; 'auto' is the last resort, and
        # StructuredOutputRunnable then accepts the JSON as text.
        fallbacks = ["required", "auto"] if force_tool else []
        if isinstance(tool_choice, str) and tool_choice in fallbacks:
            fallbacks = fallbacks[fallbacks.index(tool_choice) + 1:]
        while True:
            try:
                response = self._with_retry(
                    lambda: self._params.call(self._responses_create_flat, kwargs),
                    max_retries=3, base_delay=0.1,
                )
                break
            except Exception as e:
                if (fallbacks and getattr(e, "status_code", None) == 400
                        and (getattr(e, "param", None) == "tool_choice"
                             or "tool_choice" in _error_text(e))):
                    relaxed = fallbacks.pop(0)
                    if relaxed == "required" and len(normalized) != 1:
                        relaxed = fallbacks.pop(0) if fallbacks else relaxed
                    self._relaxed_tool_choice[model_key] = relaxed
                    logger.warning(
                        f"OpenAI model {model_key!r} rejected a forced tool choice on "
                        f"the Responses API; retrying with tool_choice={relaxed!r}. "
                        f"Remembered for this model. (Provider said: {_error_text(e)[:200]})"
                    )
                    kwargs["tool_choice"] = relaxed
                    continue
                logger.error(f"Error during Responses API call (tools): {e}")
                raise
        self._record_responses_usage(response)
        return _parse_responses_output(response)

    def _initialize_responses(self, messages, extra_headers, extra_query, extra_body, timeout):
        kwargs = self._request_kwargs(
            ("tools", "tool_choice", "functions", "function_call", "response_format")
        )
        kwargs.update(input=_messages_for_openai_responses(list(messages)),
                      extra_headers=extra_headers, extra_query=extra_query,
                      extra_body=extra_body, timeout=timeout or self.timeout)
        response = self._with_retry(
            lambda: self._params.call(self._responses_create_flat, kwargs),
            max_retries=3, base_delay=0.1,
        )
        self._record_responses_usage(response)
        parsed = _parse_responses_output(response)
        return parsed.get("text", "") if parsed["type"] == "text" else ""

    def Initialize(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        stream: Optional[bool] | NotGiven = NOT_GIVEN,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ):
        if self._uses_responses(self.defaults.get("model"), for_tools=False):
            try:
                return self._initialize_responses(messages, extra_headers, extra_query,
                                                  extra_body, timeout)
            except Exception as e:
                logger.error(f"Error during Responses API call: {e}")
                raise
        logger.debug("Calling OpenAI chat.completions.create")
        try:
            chat_messages = _messages_for_openai(list(messages))
        except ValueError:
            # e.g. a document URL: chat completions can't express it.
            if self.use_responses_api is False:
                raise
            return self._initialize_responses(messages, extra_headers, extra_query,
                                              extra_body, timeout)

        def _call_and_record():
            completion = self._create(
                messages=chat_messages,
                **self._request_kwargs(),
                extra_headers=extra_headers,
                extra_query=extra_query,
                extra_body=extra_body,
                timeout=timeout or self.timeout,
            )
            # Track usage from the response (OpenAI returns completion.usage
            # for non-streaming requests).
            u = getattr(completion, "usage", None)
            if u is not None:
                self._record_usage_counts(
                    input_tokens=getattr(u, "prompt_tokens", 0),
                    output_tokens=getattr(u, "completion_tokens", 0),
                )
            return self.extract_content(completion)

        try:
            return self._with_retry(_call_and_record, max_retries=3, base_delay=0.1)
        except Exception as e:
            logger.error(f"Error during chat completion: {str(e)}")
            raise

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        force_tool: Optional[str] = None,
    ) -> Dict[str, Any]:
        model = self.defaults.get("model")
        if self._uses_responses(model, for_tools=True):
            return self._call_with_tools_responses(messages, tools, force_tool)
        original_messages = messages
        normalized = [_normalize_tool_spec_for_openai(t) for t in tools]
        if force_tool:
            tool_choice: Any = {"type": "function", "function": {"name": force_tool}}
        else:
            tool_choice = "auto"

        # Strip conflicting defaults; the explicit args win.
        call_defaults = self._request_kwargs(
            ("tools", "tool_choice", "functions", "function_call", "response_format")
        )

        # #15: translate any tool_use / tool_call_id messages to OpenAI's
        # native shape. Plain {role, content} dicts pass through unchanged.
        # Some inputs have no chat-completions form at all (a document
        # URL); the Responses API can take them, so route there unless
        # the caller pinned chat completions.
        try:
            messages = _messages_for_openai(messages)
        except ValueError:
            if self.use_responses_api is False:
                raise
            logger.info("Request needs the Responses API (chat completions "
                        "can't express it); sending via /v1/responses.")
            return self._call_with_tools_responses(original_messages, tools, force_tool)

        try:
            response = self._with_retry(
                lambda: self._create(
                    messages=messages,
                    tools=normalized,
                    tool_choice=tool_choice,
                    **call_defaults,
                ),
                max_retries=3,
                base_delay=0.1,
            )
        except Exception as e:
            if self._switch_to_responses(model, e):
                return self._call_with_tools_responses(original_messages, tools, force_tool)
            logger.error(f"Error during chat completion (tools): {e}")
            raise

        # Track usage (OpenAI exposes prompt_tokens + completion_tokens).
        u = getattr(response, "usage", None)
        if u is not None:
            self._record_usage_counts(
                input_tokens=getattr(u, "prompt_tokens", 0),
                output_tokens=getattr(u, "completion_tokens", 0),
            )

        msg = response.choices[0].message
        if getattr(msg, "tool_calls", None):
            tool_calls = []
            for call in msg.tool_calls:
                args, arg_error = _parse_tool_arguments(call.function.arguments)
                if arg_error is not None:
                    # Unparseable even after escape repair. Don't raise —
                    # that would unwind the whole agent run (and surface to
                    # a Supervisor as a bare "Invalid \escape" error).
                    # Hand the loop a dedicated result so it can feed the
                    # error back and let the model resend with valid JSON.
                    logger.error(
                        f"Model returned non-JSON tool arguments for "
                        f"'{call.function.name}': {call.function.arguments!r}"
                    )
                    return {
                        "type": "invalid_tool_args",
                        "name": call.function.name,
                        "id": call.id,
                        "raw": call.function.arguments,
                        "error": arg_error,
                    }
                tool_calls.append({"name": call.function.name, "input": args, "id": call.id})
            # Backward-compatible: callers reading .name/.input get the first;
            # callers wanting concurrency iterate `tool_calls`.
            first = tool_calls[0]
            return {
                "type": "tool_use",
                "name": first["name"],
                "input": first["input"],
                "id": first["id"],
                "tool_calls": tool_calls,
            }
        return {"type": "text", "text": msg.content or ""}

    def stream_text(self, messages: List[Dict[str, Any]]) -> Iterator[str]:
        """OpenAI native streaming. Yields the ``delta.content`` of each
        chunk as it arrives; skips chunks without content (e.g. role-only
        opening frame, finish_reason-only closing frame).

        Sets ``stream_options={"include_usage": True}`` so the final chunk
        carries a ``usage`` block — captured into ``self.usage`` so token
        counting works for streamed responses too (was a bug — non-streaming
        responses recorded usage, streamed ones silently didn't).
        """
        call_defaults = self._request_kwargs(
            ("tools", "tool_choice", "stream", "stream_options")
        )
        stream = self._create(
            messages=_messages_for_openai(list(messages)),
            stream=True,
            stream_options={"include_usage": True},
            **call_defaults,
        )
        for chunk in stream:
            u = getattr(chunk, "usage", None)
            if u is not None:
                self._record_usage_counts(
                    input_tokens=getattr(u, "prompt_tokens", 0),
                    output_tokens=getattr(u, "completion_tokens", 0),
                )
            if not chunk.choices:
                continue
            delta = getattr(chunk.choices[0], "delta", None)
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            if text:
                yield text

    async def astream_text(self, messages: List[Dict[str, Any]]) -> AsyncIterator[str]:
        """Async OpenAI streaming via AsyncOpenAI client. Same usage capture
        as the sync sibling."""
        from openai import AsyncOpenAI
        async_client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
        call_defaults = self._request_kwargs(
            ("tools", "tool_choice", "stream", "stream_options")
        )
        stream = await self._params.acall(async_client.chat.completions.create, dict(
            messages=_messages_for_openai(list(messages)),
            stream=True,
            stream_options={"include_usage": True},
            **call_defaults,
        ))
        async for chunk in stream:
            u = getattr(chunk, "usage", None)
            if u is not None:
                self._record_usage_counts(
                    input_tokens=getattr(u, "prompt_tokens", 0),
                    output_tokens=getattr(u, "completion_tokens", 0),
                )
            if not chunk.choices:
                continue
            delta = getattr(chunk.choices[0], "delta", None)
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            if text:
                yield text

    def update_defaults(self, **kwargs):
        for key, value in kwargs.items():
            if key in self.defaults:
                self.defaults[key] = value
                logger.info(f"Updated default parameter {key} = {value}")

    def extract_content(self, completion: ChatCompletion) -> str:
        if completion.choices and len(completion.choices) > 0:
            return completion.choices[0].message.content or ""
        return ""


class Claude(BaseChatModel):
    """
    Chat model wrapper for Anthropic's Claude API.

    Usage:
        model = Claude(model="claude-sonnet-4-6")
        response = model.Initialize([{"role": "user", "content": "Hello"}])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        enable_prompt_cache: bool = False,
        cache_history_after: int = 4,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        thinking: Optional[Dict[str, Any]] = None,
        stop_sequences: Optional[List[str]] = None,
        adapt_params: bool = True,
    ):
        """
        Args (new in 3.1):
            enable_prompt_cache: Opt in to Anthropic prompt caching. When True,
                the system prompt and the tool schema list are marked as
                cache breakpoints on every call. Repeat calls that share the
                same system + tools read those blocks from Anthropic's cache
                (~90% token cost reduction on the cached portion). Safe to
                enable for any conversation whose system prompt + tool list
                is stable; if either changes call-to-call the cache just
                misses and re-populates.
            cache_history_after: When ``enable_prompt_cache=True`` and the
                conversation has more than this many user/assistant turns,
                a third cache breakpoint is placed on the last stable
                assistant message so long histories also benefit. Anthropic
                allows up to 4 cache breakpoints; the framework uses at
                most 3 (system, tools, history), leaving one for callers.

        Args (new in 3.4):
            temperature: Now defaults to None -- not sent, so the API
                default (1.0, identical to the old default) applies. Sending
                it unconditionally conflicted with ``top_p`` on newer models
                and with extended thinking.
            top_p, top_k, stop_sequences: Passed through when set.
            thinking: Extended-thinking config passed through verbatim, e.g.
                ``{"type": "enabled", "budget_tokens": 8000}``. Anthropic
                requires the default temperature with thinking, so a
                temperature/top_k set alongside it is not sent. Thinking with
                native tool calling is not supported yet (the thinking blocks
                are not replayed into later turns).
            adapt_params: When the API rejects an optional parameter for
                this model -- ``top_p`` next to ``temperature``, ``thinking``
                on a model without it, ``max_tokens`` above the model's cap --
                drop or clamp it, retry, and remember it for this model.
                Logged at WARNING. Set False to surface the raw error.
        """
        import anthropic as _anthropic
        self._anthropic = _anthropic
        self.model_name = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._timeout = timeout
        self._max_retries = max_retries
        self._enable_prompt_cache = bool(enable_prompt_cache)
        self._cache_history_after = int(cache_history_after)
        self.top_p = top_p
        self.top_k = top_k
        self.thinking = thinking
        self.stop_sequences = stop_sequences
        self._params = _ParamAdapter("Anthropic", _anthropic_param_fix, adapt_params)
        self.client = _anthropic.Anthropic(
            api_key=self._api_key,
            timeout=timeout,
            max_retries=max_retries,
        )


    def _request_kwargs(self, **extra) -> Dict[str, Any]:
        """Base Messages kwargs: only parameters that are actually set, so
        a model never receives one it wasn't asked to use."""
        kw: Dict[str, Any] = {"model": self.model_name, "max_tokens": self.max_tokens}
        thinking_on = bool(self.thinking) and str(
            (self.thinking or {}).get("type", "enabled")) != "disabled"
        if self.temperature is not None and not (thinking_on and self.temperature != 1):
            kw["temperature"] = self.temperature
        if self.top_p is not None:
            kw["top_p"] = self.top_p
        if self.top_k is not None and not thinking_on:
            kw["top_k"] = self.top_k
        if self.stop_sequences:
            kw["stop_sequences"] = list(self.stop_sequences)
        if self.thinking:
            kw["thinking"] = self.thinking
        kw.update(extra)
        return kw

    def _create(self, client, **kwargs):
        return self._params.call(client.messages.create, kwargs)

    async def _acreate(self, client, **kwargs):
        return await self._params.acall(client.messages.create, kwargs)

    def _open_stream(self, client, kwargs):
        """Enter ``messages.stream`` with the same parameter adaptation as
        ``_create``. The request is sent on ``__enter__``, so that's where a
        rejected parameter surfaces."""
        kwargs = self._params.apply(kwargs)
        for _ in range(_MAX_PARAM_FIXES + 1):
            manager = client.messages.stream(**kwargs)
            try:
                return manager, manager.__enter__()
            except Exception as e:
                if not self._params.learn(kwargs, e):
                    raise
        manager = client.messages.stream(**kwargs)
        return manager, manager.__enter__()

    async def _aopen_stream(self, client, kwargs):
        kwargs = self._params.apply(kwargs)
        for _ in range(_MAX_PARAM_FIXES + 1):
            manager = client.messages.stream(**kwargs)
            try:
                return manager, await manager.__aenter__()
            except Exception as e:
                if not self._params.learn(kwargs, e):
                    raise
        manager = client.messages.stream(**kwargs)
        return manager, await manager.__aenter__()

    def _split_messages(self, messages):
        # #15: first translate any tool_use / tool_call_id messages into
        # Anthropic's content-block format. Plain {role, content} dicts
        # pass through unchanged.
        translated = _messages_for_anthropic(messages)
        system_parts = [_system_text(m["content"]) for m in translated
                        if m.get("role") == "system"]
        conversation = [
            {"role": m["role"], "content": m["content"]}
            for m in translated
            if m.get("role") in ("user", "assistant")
        ]
        system_prompt = "\n\n".join(system_parts) if system_parts else self._anthropic.NOT_GIVEN
        return system_prompt, conversation

    def _prepare_system_for_cache(self, system_prompt):
        """When prompt caching is on, convert the string system prompt into
        a single-block list with cache_control on it. When off (or when the
        system is the NOT_GIVEN sentinel), pass through unchanged so the
        API sees the same shape it always has."""
        if not self._enable_prompt_cache:
            return system_prompt
        if system_prompt is self._anthropic.NOT_GIVEN or not system_prompt:
            return system_prompt
        return [{
            "type": "text",
            "text": str(system_prompt),
            "cache_control": {"type": "ephemeral"},
        }]

    def _prepare_tools_for_cache(self, normalized_tools):
        """Mark the LAST tool spec with cache_control so the whole tool
        block gets cached as a unit (Anthropic caches from the marked
        position back to the start of the section). No-op when caching is
        off or when the tool list is empty."""
        if not self._enable_prompt_cache or not normalized_tools:
            return normalized_tools
        # Defensive copy — never mutate the caller's list.
        out = [dict(t) for t in normalized_tools]
        out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
        return out

    def _prepare_conversation_for_cache(self, conversation):
        """Add a cache_control breakpoint on the last assistant message
        older than the recent tail, so a growing chat history benefits
        from caching. Recent turns stay uncached because they change every
        call. No-op unless caching is on AND the history is long enough."""
        if not self._enable_prompt_cache:
            return conversation
        if len(conversation) < self._cache_history_after:
            return conversation
        # Find the last assistant message that's not in the recent tail
        # (last 2 turns). Marking it caches everything up to and including it.
        cutoff = len(conversation) - 2
        for i in range(cutoff - 1, -1, -1):
            if conversation[i].get("role") == "assistant":
                # Rewrap the message with a cache_control block.
                out = list(conversation)
                out[i] = _stamp_cache_on_message(out[i])
                return out
        return conversation

    def _record_usage(self, response) -> None:
        """Capture Anthropic usage block — both sync and async paths funnel
        through here so the bookkeeping isn't duplicated four times.

        Also captures the prompt-cache counters (``cache_read_input_tokens``
        and ``cache_creation_input_tokens``) when the response has them, so
        callers can inspect ``model.usage.cache_hit_ratio`` to verify their
        cache is actually hitting.
        """
        u = getattr(response, "usage", None)
        if u is not None:
            self._record_usage_counts(
                input_tokens=getattr(u, "input_tokens", 0),
                output_tokens=getattr(u, "output_tokens", 0),
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            )

    def Initialize(self, messages) -> str:
        system_prompt, conversation = self._split_messages(messages)
        system_prompt = self._prepare_system_for_cache(system_prompt)
        conversation = self._prepare_conversation_for_cache(conversation)
        response = self._create(
            self.client,
            **self._request_kwargs(system=system_prompt, messages=conversation),
        )
        self._record_usage(response)
        return _anthropic_text(response)

    async def async_initialize(self, messages) -> str:
        """Native async Claude call using the AsyncAnthropic client."""
        async_client = self._anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )
        system_prompt, conversation = self._split_messages(messages)
        system_prompt = self._prepare_system_for_cache(system_prompt)
        conversation = self._prepare_conversation_for_cache(conversation)
        response = await self._acreate(
            async_client,
            **self._request_kwargs(system=system_prompt, messages=conversation),
        )
        self._record_usage(response)
        return _anthropic_text(response)

    def stream_text(self, messages: List[Dict[str, Any]]) -> Iterator[str]:
        """Anthropic native streaming via ``messages.stream`` context manager.
        Yields text deltas from the assistant's response as they arrive.

        After the stream closes, pulls the final message via
        ``stream.get_final_message()`` and records its usage block — so
        token counting works for streamed Claude responses too.
        """
        system_prompt, conversation = self._split_messages(messages)
        manager, stream = self._open_stream(
            self.client, self._request_kwargs(system=system_prompt, messages=conversation),
        )
        try:
            for text in stream.text_stream:
                if text:
                    yield text
            try:
                final = stream.get_final_message()
                self._record_usage(final)
            except Exception as e:
                logger.warning(f"Failed to record streaming usage: {e}")
        finally:
            manager.__exit__(None, None, None)

    async def astream_text(self, messages: List[Dict[str, Any]]) -> AsyncIterator[str]:
        """Async Anthropic streaming. Same usage capture as the sync sibling."""
        async_client = self._anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )
        system_prompt, conversation = self._split_messages(messages)
        manager, stream = await self._aopen_stream(
            async_client, self._request_kwargs(system=system_prompt, messages=conversation),
        )
        try:
            async for text in stream.text_stream:
                if text:
                    yield text
            try:
                final = await stream.get_final_message()
                self._record_usage(final)
            except Exception as e:
                logger.warning(f"Failed to record streaming usage: {e}")
        finally:
            await manager.__aexit__(None, None, None)

    def call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        force_tool: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized = [_normalize_tool_spec_for_anthropic(t) for t in tools]
        normalized = self._prepare_tools_for_cache(normalized)
        system_prompt, conversation = self._split_messages(messages)
        system_prompt = self._prepare_system_for_cache(system_prompt)
        conversation = self._prepare_conversation_for_cache(conversation)

        kwargs: Dict[str, Any] = self._request_kwargs(
            system=system_prompt, messages=conversation, tools=normalized,
        )
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

        response = self._create(self.client, **kwargs)
        self._record_usage(response)

        tool_calls = [
            {"name": b.name, "input": dict(b.input), "id": getattr(b, "id", None)}
            for b in response.content
            if getattr(b, "type", None) == "tool_use"
        ]
        if tool_calls:
            first = tool_calls[0]
            return {
                "type": "tool_use",
                "name": first["name"],
                "input": first["input"],
                "id": first["id"],
                "tool_calls": tool_calls,
            }

        text_parts = [
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        return {"type": "text", "text": "".join(text_parts)}

    async def async_call_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        force_tool: Optional[str] = None,
    ) -> Dict[str, Any]:
        async_client = self._anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )
        normalized = [_normalize_tool_spec_for_anthropic(t) for t in tools]
        normalized = self._prepare_tools_for_cache(normalized)
        system_prompt, conversation = self._split_messages(messages)
        system_prompt = self._prepare_system_for_cache(system_prompt)
        conversation = self._prepare_conversation_for_cache(conversation)

        kwargs: Dict[str, Any] = self._request_kwargs(
            system=system_prompt, messages=conversation, tools=normalized,
        )
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

        response = await self._acreate(async_client, **kwargs)
        self._record_usage(response)

        tool_calls = [
            {"name": b.name, "input": dict(b.input), "id": getattr(b, "id", None)}
            for b in response.content
            if getattr(b, "type", None) == "tool_use"
        ]
        if tool_calls:
            first = tool_calls[0]
            return {
                "type": "tool_use",
                "name": first["name"],
                "input": first["input"],
                "id": first["id"],
                "tool_calls": tool_calls,
            }

        text_parts = [
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        ]
        return {"type": "text", "text": "".join(text_parts)}

    # ------------------------------------------------------------------
    # Anthropic Batch API (3.1)
    # ------------------------------------------------------------------

    def batch(
        self,
        requests: List[Any],
        *,
        poll_interval_sec: float = 15.0,
        max_wait_sec: float = 86400.0,   # 24h -- Anthropic's max batch TTL
    ) -> List[Any]:
        """Submit many prompts to Anthropic's Batch API and block until
        all complete. Returns results in the SAME ORDER as ``requests``.

        Anthropic's batch endpoint charges 50% of standard pricing. Best
        for embarrassingly-parallel workloads (evals, data labeling,
        bulk extraction) where a few minutes of latency is fine.

        Args:
            requests: List of one of:
                - a plain string (wrapped as one user message)
                - a list of message dicts
                - a full request dict shaped like the Anthropic Batch
                  API accepts: ``{"custom_id": str, "params": {...}}``
                  where ``params`` matches ``messages.create``. If you
                  pass this shape you own ``custom_id``; otherwise the
                  framework assigns ``req_0, req_1, ...``.
            poll_interval_sec: How often to poll the batch status.
            max_wait_sec: Give up after this long. Raises TimeoutError.

        Returns:
            List of results, same length + order as ``requests``. Each
            result is one of:
              - the assistant text (str)   -- on success
              - a dict ``{"error": str, "type": str}``  -- on per-request
                failure (validation, over-limit, canceled).

        Cost recording: input/output tokens land in ``self.usage`` when
        the batch finishes, so ``model.usage`` remains a source of truth
        for spend across both sync + batch calls.
        """
        import time as _time

        normalized = []
        for idx, req in enumerate(requests):
            if isinstance(req, dict) and "params" in req:
                custom_id = str(req.get("custom_id") or f"req_{idx}")
                params = dict(req["params"])
            else:
                if isinstance(req, str):
                    messages = [{"role": "user", "content": req}]
                elif isinstance(req, list):
                    messages = req
                elif isinstance(req, dict) and "messages" in req:
                    messages = list(req["messages"])
                else:
                    raise TypeError(
                        f"batch request #{idx} must be str, message list, "
                        f"{{messages: [...]}} dict, or {{custom_id, params}} "
                        f"dict; got {type(req).__name__}"
                    )
                system_parts = [m["content"] for m in messages if m.get("role") == "system"]
                conversation = [
                    {"role": m["role"], "content": m["content"]}
                    for m in messages
                    if m.get("role") in ("user", "assistant")
                ]
                _sep = chr(10) + chr(10)
                system_prompt = _sep.join(system_parts) if system_parts else self._anthropic.NOT_GIVEN
                system_prompt = self._prepare_system_for_cache(system_prompt)
                custom_id = f"req_{idx}"
                params = self._request_kwargs(
                    messages=_messages_for_anthropic(conversation),
                )
                if system_prompt is not self._anthropic.NOT_GIVEN:
                    params["system"] = system_prompt
            normalized.append({"custom_id": custom_id, "params": params})

        batches_api = None
        for path in ("messages.batches", "beta.messages.batches"):
            obj = self.client
            for part in path.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj is not None:
                batches_api = obj
                break
        if batches_api is None:
            raise RuntimeError(
                "This anthropic SDK version does not expose the Batch API. "
                "Upgrade with: pip install -U 'anthropic>=0.36'"
            )

        submission = batches_api.create(requests=normalized)
        batch_id = submission.id
        logger.info(f"Anthropic batch submitted: {batch_id} ({len(normalized)} requests)")

        deadline = _time.monotonic() + max_wait_sec
        while True:
            status = batches_api.retrieve(batch_id)
            proc = getattr(status, "processing_status", None) or getattr(status, "status", None)
            if proc == "ended":
                break
            if _time.monotonic() > deadline:
                raise TimeoutError(
                    f"Anthropic batch {batch_id} did not finish within "
                    f"{max_wait_sec}s (last status: {proc})"
                )
            _time.sleep(poll_interval_sec)

        by_id: Dict[str, Any] = {}
        results_iter = batches_api.results(batch_id)
        for entry in results_iter:
            cid = entry.custom_id
            result = entry.result
            kind = getattr(result, "type", None)
            if kind == "succeeded":
                message = result.message
                text_parts = []
                for block in message.content:
                    if getattr(block, "type", None) == "text":
                        text_parts.append(block.text)
                usage = getattr(message, "usage", None)
                if usage is not None:
                    self._record_usage_counts(
                        input_tokens=getattr(usage, "input_tokens", 0),
                        output_tokens=getattr(usage, "output_tokens", 0),
                        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    )
                by_id[cid] = "".join(text_parts)
            elif kind == "errored":
                by_id[cid] = {
                    "error": str(getattr(result, "error", "unknown")),
                    "type": "errored",
                }
            elif kind == "canceled":
                by_id[cid] = {"error": "canceled", "type": "canceled"}
            elif kind == "expired":
                by_id[cid] = {"error": "expired (24h TTL)", "type": "expired"}
            else:
                by_id[cid] = {"error": f"unknown result type {kind!r}", "type": "unknown"}

        return [by_id.get(r["custom_id"], {"error": "no result", "type": "missing"})
                for r in normalized]

