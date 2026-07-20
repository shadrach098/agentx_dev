from typing import Optional, Union, Dict, List, Iterable, Literal, Any, Type, Iterator, AsyncIterator
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.chat.completion_create_params import (
    FunctionCall, Function, ResponseFormat
)
from openai._types import NOT_GIVEN, NotGiven
from pydantic import BaseModel
import asyncio
import json
import logging, os

logger = logging.getLogger(__name__)
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

    def invoke(self, input_value: Any) -> Any:
        messages = self._to_messages(input_value)
        result = self.model.call_with_tools(
            messages=messages,
            tools=[self._tool_spec],
            force_tool=self._tool_name,
        )
        return self._parse(result)

    async def ainvoke(self, input_value: Any) -> Any:
        messages = self._to_messages(input_value)
        result = await self.model.async_call_with_tools(
            messages=messages,
            tools=[self._tool_spec],
            force_tool=self._tool_name,
        )
        return self._parse(result)

    __call__ = invoke

    def _parse(self, result: Dict[str, Any]):
        if result.get("type") != "tool_use":
            raise ValueError(
                f"Model returned text instead of a {self._tool_name!r} tool call: "
                f"{result.get('text', '')[:200]!r}"
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

        # Everything else — pass through.
        out.append({"role": m.get("role"), "content": m.get("content", "")})
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

    def invoke(self, messages) -> str:
        """Canonical entry point. Alias for ``Initialize`` with input
        normalization. See ``_coerce_messages`` for the accepted shapes."""
        return self.Initialize(self._coerce_messages(messages))

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

    async def ainvoke(self, messages) -> str:
        """Canonical async entry point. Alias for ``async_initialize``."""
        return await self.async_initialize(messages)

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
                    logger.warning(
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
        reasoning_effort: Literal["none", "low", "medium", "high"] | NotGiven = NOT_GIVEN,
    ):
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

    def Initialize(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        stream: Optional[bool] | NotGiven = NOT_GIVEN,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ):
        logger.debug("Calling OpenAI chat.completions.create")

        def _call_and_record():
            completion = self.client.chat.completions.create(
                messages=messages,
                **self.defaults,
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
        normalized = [_normalize_tool_spec_for_openai(t) for t in tools]
        if force_tool:
            tool_choice: Any = {"type": "function", "function": {"name": force_tool}}
        else:
            tool_choice = "auto"

        # Strip conflicting defaults; the explicit args win.
        call_defaults = {
            k: v for k, v in self.defaults.items()
            if k not in ("tools", "tool_choice", "functions", "function_call", "response_format")
        }

        # #15: translate any tool_use / tool_call_id messages to OpenAI's
        # native shape. Plain {role, content} dicts pass through unchanged.
        messages = _messages_for_openai(messages)

        try:
            response = self._with_retry(
                lambda: self.client.chat.completions.create(
                    messages=messages,
                    tools=normalized,
                    tool_choice=tool_choice,
                    **call_defaults,
                ),
                max_retries=3,
                base_delay=0.1,
            )
        except Exception as e:
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
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    logger.error(f"Model returned non-JSON tool arguments: {call.function.arguments!r}")
                    raise
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
        call_defaults = {
            k: v for k, v in self.defaults.items()
            if k not in ("tools", "tool_choice", "stream", "stream_options")
        }
        stream = self.client.chat.completions.create(
            messages=messages,
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
        call_defaults = {
            k: v for k, v in self.defaults.items()
            if k not in ("tools", "tool_choice", "stream", "stream_options")
        }
        stream = await async_client.chat.completions.create(
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            **call_defaults,
        )
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
        temperature: float = 1.0,
        timeout: float = 60.0,
        max_retries: int = 3,
        enable_prompt_cache: bool = False,
        cache_history_after: int = 4,
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
        self.client = _anthropic.Anthropic(
            api_key=self._api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    def _split_messages(self, messages):
        # #15: first translate any tool_use / tool_call_id messages into
        # Anthropic's content-block format. Plain {role, content} dicts
        # pass through unchanged.
        translated = _messages_for_anthropic(messages)
        system_parts = [m["content"] for m in translated if m.get("role") == "system"]
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
        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=conversation,
        )
        self._record_usage(response)
        return response.content[0].text if response.content else ""

    async def async_initialize(self, messages) -> str:
        """Native async Claude call using the AsyncAnthropic client."""
        async_client = self._anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )
        system_prompt, conversation = self._split_messages(messages)
        system_prompt = self._prepare_system_for_cache(system_prompt)
        conversation = self._prepare_conversation_for_cache(conversation)
        response = await async_client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=conversation,
        )
        self._record_usage(response)
        return response.content[0].text if response.content else ""

    def stream_text(self, messages: List[Dict[str, Any]]) -> Iterator[str]:
        """Anthropic native streaming via ``messages.stream`` context manager.
        Yields text deltas from the assistant's response as they arrive.

        After the stream closes, pulls the final message via
        ``stream.get_final_message()`` and records its usage block — so
        token counting works for streamed Claude responses too.
        """
        system_prompt, conversation = self._split_messages(messages)
        with self.client.messages.stream(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=conversation,
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
            try:
                final = stream.get_final_message()
                self._record_usage(final)
            except Exception as e:
                logger.warning(f"Failed to record streaming usage: {e}")

    async def astream_text(self, messages: List[Dict[str, Any]]) -> AsyncIterator[str]:
        """Async Anthropic streaming. Same usage capture as the sync sibling."""
        async_client = self._anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )
        system_prompt, conversation = self._split_messages(messages)
        async with async_client.messages.stream(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=conversation,
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text
            try:
                final = await stream.get_final_message()
                self._record_usage(final)
            except Exception as e:
                logger.warning(f"Failed to record streaming usage: {e}")

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

        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": conversation,
            "tools": normalized,
        }
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

        response = self.client.messages.create(**kwargs)
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

        kwargs: Dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": conversation,
            "tools": normalized,
        }
        if force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

        response = await async_client.messages.create(**kwargs)
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
                params = {
                    "model": self.model_name,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "messages": conversation,
                }
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

