"""Cost-budget enforcement on the streaming paths.

``configure_limits(budget_usd=...)`` raises ``CostBudgetExceeded`` from the
usage funnel. The Claude streaming paths record usage inside a broad
``except Exception`` (so a bookkeeping hiccup can't kill a stream whose text
was already delivered) -- that handler used to swallow the budget error too,
so a streaming run could never hit its cap and persistent runs never ended
``out_of_budget``.
"""

import asyncio
import types

import anthropic
import openai
import pytest

from agentx_dev import GPT, Claude, CostBudgetExceeded

MSGS = [{"role": "user", "content": "hi"}]

# 100k in + 50k out at $0.001 / $0.002 per 1k  ->  $0.20 against a $0.01 cap.
HEAVY = dict(input_tokens=100_000, output_tokens=50_000)
LIMITS = dict(budget_usd=0.01, input_price_per_1k=0.001, output_price_per_1k=0.002)


# --- Claude fakes -----------------------------------------------------------

class _ClaudeStream:
    def __init__(self, usage, final_error=None):
        self._usage, self._final_error = usage, final_error
        self.text_stream = iter(["he", "llo"])

    def get_final_message(self):
        if self._final_error:
            raise self._final_error
        return types.SimpleNamespace(usage=self._usage)


class _ClaudeManager:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        return self.stream

    def __exit__(self, *exc):
        return False


class _AsyncClaudeStream:
    def __init__(self, usage, final_error=None):
        self._usage, self._final_error = usage, final_error

        async def gen():
            for t in ("he", "llo"):
                yield t
        self.text_stream = gen()

    async def get_final_message(self):
        if self._final_error:
            raise self._final_error
        return types.SimpleNamespace(usage=self._usage)


class _AsyncClaudeManager:
    def __init__(self, stream):
        self.stream = stream

    async def __aenter__(self):
        return self.stream

    async def __aexit__(self, *exc):
        return False


def _claude(*, final_error=None, **tokens):
    usage = types.SimpleNamespace(**(tokens or dict(input_tokens=1, output_tokens=1)))
    c = Claude(api_key="x")
    c.client = types.SimpleNamespace(messages=types.SimpleNamespace(
        stream=lambda **kw: _ClaudeManager(_ClaudeStream(usage, final_error))))
    # astream_text builds its own AsyncAnthropic client.
    c._anthropic = types.SimpleNamespace(
        NOT_GIVEN=anthropic.NOT_GIVEN,
        AsyncAnthropic=lambda **kw: types.SimpleNamespace(messages=types.SimpleNamespace(
            stream=lambda **k: _AsyncClaudeManager(_AsyncClaudeStream(usage, final_error)))))
    return c


async def _adrain(agen):
    return [t async for t in agen]


class TestClaudeStreamingBudget:
    def test_sync_stream_raises_when_over_budget(self):
        c = _claude(**HEAVY)
        c.configure_limits(**LIMITS)
        with pytest.raises(CostBudgetExceeded):
            list(c.stream_text(MSGS))
        assert c.usage.total_input_tokens == HEAVY["input_tokens"]

    def test_async_stream_raises_when_over_budget(self):
        c = _claude(**HEAVY)
        c.configure_limits(**LIMITS)
        with pytest.raises(CostBudgetExceeded):
            asyncio.run(_adrain(c.astream_text(MSGS)))

    def test_sync_stream_under_budget_is_untouched(self):
        c = _claude(input_tokens=10, output_tokens=5)
        c.configure_limits(**LIMITS)
        assert "".join(c.stream_text(MSGS)) == "hello"

    def test_async_stream_under_budget_is_untouched(self):
        c = _claude(input_tokens=10, output_tokens=5)
        c.configure_limits(**LIMITS)
        assert "".join(asyncio.run(_adrain(c.astream_text(MSGS)))) == "hello"

    def test_stream_without_a_budget_is_untouched(self):
        c = _claude(**HEAVY)
        assert "".join(c.stream_text(MSGS)) == "hello"

    def test_other_recording_errors_are_still_swallowed(self):
        """A failed ``get_final_message`` must not kill a delivered stream."""
        c = _claude(final_error=RuntimeError("boom"))
        c.configure_limits(**LIMITS)
        assert "".join(c.stream_text(MSGS)) == "hello"
        c2 = _claude(final_error=RuntimeError("boom"))
        c2.configure_limits(**LIMITS)
        assert "".join(asyncio.run(_adrain(c2.astream_text(MSGS)))) == "hello"


# --- GPT fakes --------------------------------------------------------------

def _gpt_chunks(prompt_tokens, completion_tokens):
    delta = lambda t: types.SimpleNamespace(choices=[types.SimpleNamespace(
        delta=types.SimpleNamespace(content=t))], usage=None)
    return [
        delta("he"), delta("llo"),
        types.SimpleNamespace(choices=[], usage=types.SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)),
    ]


def _gpt(prompt_tokens, completion_tokens):
    g = GPT(api_key="x")
    g.client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(
            create=lambda **kw: iter(_gpt_chunks(prompt_tokens, completion_tokens)))))
    return g


def _patch_async_openai(monkeypatch, prompt_tokens, completion_tokens):
    async def create(**kw):
        async def gen():
            for chunk in _gpt_chunks(prompt_tokens, completion_tokens):
                yield chunk
        return gen()

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))))


class TestGPTStreamingBudget:
    def test_sync_stream_raises_when_over_budget(self):
        g = _gpt(100_000, 50_000)
        g.configure_limits(**LIMITS)
        with pytest.raises(CostBudgetExceeded):
            list(g.stream_text(MSGS))

    def test_async_stream_raises_when_over_budget(self, monkeypatch):
        _patch_async_openai(monkeypatch, 100_000, 50_000)
        g = GPT(api_key="x")
        g.configure_limits(**LIMITS)
        with pytest.raises(CostBudgetExceeded):
            asyncio.run(_adrain(g.astream_text(MSGS)))

    def test_sync_stream_under_budget_is_untouched(self):
        g = _gpt(10, 5)
        g.configure_limits(**LIMITS)
        assert "".join(g.stream_text(MSGS)) == "hello"
