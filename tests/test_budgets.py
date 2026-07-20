"""Tests for cost budget + retry budget + rate limit."""

import time

import pytest

from agentx_dev import (
    BaseChatModel, TokenBucket, TokenUsage,
    CostBudgetExceeded, RetryBudgetExceeded,
)


class ScriptedModel(BaseChatModel):
    """Model that records N tokens per call."""

    def __init__(self, input_tokens=100, output_tokens=50):
        self._in = input_tokens
        self._out = output_tokens

    def Initialize(self, messages):
        self._record_usage_counts(input_tokens=self._in, output_tokens=self._out)
        return "ok"


class FlakyModel(BaseChatModel):
    """Fails N times then succeeds."""

    def __init__(self, failures_before_success=3):
        self.remaining = failures_before_success

    def Initialize(self, messages):
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("flaky upstream")
        return "recovered"


class TestTokenUsage:

    def test_records_and_accumulates(self):
        u = TokenUsage()
        u.record(input_tokens=100, output_tokens=50)
        u.record(input_tokens=200, output_tokens=25)
        assert u.total_input_tokens == 300
        assert u.total_output_tokens == 75
        assert u.total_calls == 2
        assert u.last_input_tokens == 200

    def test_estimate_cost(self):
        u = TokenUsage()
        u.record(input_tokens=1000, output_tokens=500)
        cost = u.estimate_cost(0.003, 0.015)
        assert cost == pytest.approx(1000 / 1000 * 0.003 + 500 / 1000 * 0.015)

    def test_cache_hit_ratio(self):
        u = TokenUsage()
        u.record(input_tokens=100, output_tokens=50)
        assert u.cache_hit_ratio == 0.0
        u.record(
            input_tokens=100, output_tokens=50,
            cache_read_tokens=80, cache_creation_tokens=10,
        )
        # 80 cached / 200 total = 0.4
        assert u.cache_hit_ratio == pytest.approx(0.4)


class TestCostBudget:

    def test_budget_not_crossed(self):
        m = ScriptedModel(input_tokens=100, output_tokens=50)
        m.configure_limits(
            budget_usd=1.0,
            input_price_per_1k=0.001,
            output_price_per_1k=0.002,
        )
        m.invoke("hi")   # cost = 0.1/1000*.001 + .05/1000*.002 = 0.0002
        assert m.usage.total_calls == 1

    def test_budget_exceeded_raises(self):
        m = ScriptedModel(input_tokens=100_000, output_tokens=50_000)
        m.configure_limits(
            budget_usd=0.01,
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        )
        with pytest.raises(CostBudgetExceeded) as exc_info:
            m.invoke("hi")
        assert exc_info.value.limit_usd == 0.01
        assert exc_info.value.spent_usd > 0.01

    def test_budget_requires_prices(self):
        m = ScriptedModel()
        with pytest.raises(ValueError):
            m.configure_limits(budget_usd=1.0)   # no prices


class TestRetryBudget:

    def test_exponential_backoff_succeeds(self):
        m = FlakyModel(failures_before_success=2)
        # Use _with_retry directly
        result = m._with_retry(lambda: m.Initialize("hi"), max_retries=5, base_delay=0.001)
        assert result == "recovered"

    def test_retry_budget_exhausts(self):
        m = FlakyModel(failures_before_success=10)
        m.configure_limits(retry_budget=2)
        with pytest.raises((RetryBudgetExceeded, RuntimeError)):
            m._with_retry(lambda: m.Initialize("hi"), max_retries=5, base_delay=0.001)


class TestTokenBucket:

    def test_basic_acquisition(self):
        b = TokenBucket(capacity=2, refill_per_sec=100)
        b.acquire()
        b.acquire()
        # Third should require a small wait since bucket depleted
        start = time.monotonic()
        b.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.005   # >~1/100 sec

    def test_capacity_validation(self):
        with pytest.raises(ValueError):
            TokenBucket(capacity=0, refill_per_sec=1)
        with pytest.raises(ValueError):
            TokenBucket(capacity=1, refill_per_sec=0)


class TestNonRetryable:

    def test_400_not_retried(self):
        m = ScriptedModel()

        class BadRequest(Exception):
            status_code = 400

        def fail():
            raise BadRequest("bad")

        with pytest.raises(BadRequest):
            m._with_retry(fail, max_retries=5)

    def test_429_is_retried(self):
        attempts = []

        class RateLimited(Exception):
            status_code = 429

        def maybe_fail():
            attempts.append(1)
            if len(attempts) < 3:
                raise RateLimited("slow down")
            return "ok"

        m = ScriptedModel()
        result = m._with_retry(maybe_fail, max_retries=5, base_delay=0.001)
        assert result == "ok"
        assert len(attempts) == 3
