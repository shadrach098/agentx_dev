"""Tests for ToolRegistry -- dispatch, dup-guard, circuit breaker, timeouts."""

import time
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from agentx_dev import (
    StandardTool, StructuredTool,
    ToolRegistry, CircuitBreaker, CircuitBreakerConfig,
)
from agentx_dev.Agents.Agent import ToolError


class TestDispatch:

    def test_dispatch_standard_tool(self):
        def add_one(x: str) -> str:
            return f"got {x}"

        tool = StandardTool(func=add_one, name="one", description="d")
        reg = ToolRegistry([tool])
        result = reg.dispatch("one", "hi")
        assert result == "got hi"

    def test_dispatch_structured_tool(self):
        class Args(BaseModel):
            a: int
            b: int

        def add(a: int, b: int) -> str:
            return str(a + b)

        tool = StructuredTool(func=add, args_schema=Args, name="add", description="d")
        reg = ToolRegistry([tool])
        result = reg.dispatch("add", {"a": 2, "b": 3})
        assert result == "5"

    def test_dispatch_json_string_args(self):
        class Args(BaseModel):
            x: str

        tool = StructuredTool(
            func=lambda x: x.upper(), args_schema=Args, name="up", description="d",
        )
        reg = ToolRegistry([tool])
        assert reg.dispatch("up", '{"x": "hi"}') == "HI"

    def test_dispatch_bare_string_to_single_field_schema(self):
        # Model sometimes passes bare "." instead of {"path": "."}
        class Args(BaseModel):
            path: str

        tool = StructuredTool(
            func=lambda path: f"listed {path}", args_schema=Args,
            name="ls", description="d",
        )
        reg = ToolRegistry([tool])
        assert reg.dispatch("ls", ".") == "listed ."

    def test_dispatch_unknown_tool_returns_toolerror(self):
        reg = ToolRegistry([])
        result = reg.dispatch("nope", "x")
        assert isinstance(result, ToolError)
        assert "not found" in str(result).lower()

    def test_dispatch_openai_functions_prefix(self):
        """Model sometimes emits 'functions.tool_name' instead of 'tool_name'."""
        tool = StandardTool(
            func=lambda x: f"ran {x}", name="mytool", description="d",
        )
        reg = ToolRegistry([tool])
        assert reg.dispatch("functions.mytool", "hi") == "ran hi"

    def test_dispatch_captures_exception_as_toolerror(self):
        def boom(x: str) -> str:
            raise RuntimeError("upstream failed")

        tool = StandardTool(func=boom, name="boom", description="d")
        reg = ToolRegistry([tool])
        result = reg.dispatch("boom", "x")
        assert isinstance(result, ToolError)
        assert "upstream failed" in str(result)


class TestDuplicateCallGuard:

    def test_warns_on_third_repeat(self):
        calls = []

        def track(x: str) -> str:
            calls.append(x)
            return f"result-{len(calls)}"

        tool = StandardTool(func=track, name="t", description="d")
        reg = ToolRegistry([tool])

        r1 = reg.dispatch("t", "same")
        r2 = reg.dispatch("t", "same")
        r3 = reg.dispatch("t", "same")   # 3rd -- warning prepended
        assert "WARNING" in str(r3), f"expected warning, got: {r3!r}"

    def test_refuses_on_fifth_repeat(self):
        tool = StandardTool(func=lambda x: "ok", name="t", description="d")
        reg = ToolRegistry([tool])
        for _ in range(4):
            reg.dispatch("t", "same")
        result = reg.dispatch("t", "same")
        assert isinstance(result, ToolError)
        assert "refused" in str(result).lower()

    def test_different_args_reset_counter(self):
        tool = StandardTool(func=lambda x: x, name="t", description="d")
        reg = ToolRegistry([tool])
        for _ in range(4):
            reg.dispatch("t", "same")
        # Different arg should reset and succeed cleanly
        result = reg.dispatch("t", "different")
        assert not isinstance(result, ToolError)


class TestCircuitBreaker:

    def test_breaker_trips_after_threshold(self):
        cb = CircuitBreaker("t", CircuitBreakerConfig(failure_threshold=3))
        assert cb.state == "closed"
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()
        assert cb.state == "open"
        assert not cb.can_attempt()

    def test_success_resets_counter(self):
        cb = CircuitBreaker("t", CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"

    def test_recovery_transitions_to_half_open(self):
        cb = CircuitBreaker(
            "t", CircuitBreakerConfig(failure_threshold=2, recovery_timeout_sec=0.05),
        )
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.06)
        assert cb.state == "half_open"

    def test_registry_short_circuits_open_breaker(self):
        called = []

        def fn(x: str) -> str:
            called.append(x)
            raise RuntimeError("nope")

        tool = StandardTool(func=fn, name="t", description="d")
        reg = ToolRegistry(
            [tool],
            circuit_breaker_config=CircuitBreakerConfig(
                failure_threshold=2, recovery_timeout_sec=60,
            ),
        )
        # Two failures -- trips the breaker.
        reg.dispatch("t", "a")
        reg.dispatch("t", "b")
        assert reg.get_breaker("t").state == "open"
        # Third call should NOT hit the tool function.
        result = reg.dispatch("t", "c")
        assert isinstance(result, ToolError)
        assert "circuit breaker open" in str(result).lower()
        assert len(called) == 2


class TestTimeout:

    def test_sync_timeout(self):
        def slow(x: str) -> str:
            time.sleep(0.3)
            return "done"

        tool = StandardTool(func=slow, name="slow", description="d")
        tool.timeout_sec = 0.05
        reg = ToolRegistry([tool])
        result = reg.dispatch("slow", "x")
        assert isinstance(result, ToolError)
        assert "timed out" in str(result).lower()


class TestRegistryHelpers:

    def test_has(self):
        tool = StandardTool(func=lambda x: x, name="foo", description="d")
        reg = ToolRegistry([tool])
        assert reg.has("foo")
        assert not reg.has("bar")

    def test_names_block(self):
        tools = [
            StandardTool(func=lambda x: x, name=n, description="d")
            for n in ["a", "b", "c"]
        ]
        reg = ToolRegistry(tools)
        assert reg.names_block() == "a, b, c"

    def test_to_tool_specs_shape(self):
        class Args(BaseModel):
            q: str

        tool = StructuredTool(
            func=lambda q: q, args_schema=Args, name="s", description="d",
        )
        reg = ToolRegistry([tool])
        specs = reg.to_tool_specs()
        assert len(specs) == 1
        assert specs[0]["name"] == "s"
        assert "q" in specs[0]["parameters"]["properties"]
