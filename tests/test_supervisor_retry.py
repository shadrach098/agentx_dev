"""Tests for the Supervisor's bounded, informed sub-task repair loop.

Before this, a sub-task that raised was recorded as an error and the
Supervisor moved straight to synthesis — "it failed" — with no attempt
to feed the error back and let the specialist try again. Now a failed
sub-task is re-dispatched up to ``max_subtask_retries`` times, with the
prior error appended to the query so the specialist knows what to fix.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agentx_dev.Supervisor import Supervisor, AsyncSupervisor
from tests.conftest import MockModel


def plan_model(agent_name, final="Done."):
    """MockModel scripted to return a one-step plan, then a synthesis."""
    plan = json.dumps({"plan": [{"agent": agent_name, "query": "do the thing"}]})
    return MockModel(script=[plan, final])


class FlakyRunner:
    """Fake AgentRunner: raises for the first ``fail_times`` calls, then
    returns a completion. Records every query it was dispatched."""

    def __init__(self, fail_times, content, tools=None):
        self.fail_times = fail_times
        self.content = content
        self.calls = []
        self.tools = tools or []

    def Initialize(self, query):
        self.calls.append(query)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("Invalid \\escape: line 1 column 598 (char 597)")
        return SimpleNamespace(content=self.content)


class AsyncFlakyRunner(FlakyRunner):
    async def Initialize(self, query):  # type: ignore[override]
        self.calls.append(query)
        if len(self.calls) <= self.fail_times:
            raise RuntimeError("Invalid \\escape: line 1 column 598 (char 597)")
        return SimpleNamespace(content=self.content)


class TestSyncSupervisorRetry:

    def test_recovers_on_retry(self):
        runner = FlakyRunner(fail_times=1, content="scraped 12 links")
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            max_subtask_retries=1,
        )
        result = sup.run("scrape the site")
        assert len(runner.calls) == 2, "should have retried once"
        assert result.subtasks[0].error is None
        assert result.subtasks[0].content == "scraped 12 links"

    def test_retry_query_carries_the_error(self):
        runner = FlakyRunner(fail_times=1, content="ok")
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            max_subtask_retries=1,
        )
        sup.run("scrape the site")
        retry_query = runner.calls[1]
        assert "Invalid \\escape" in retry_query
        assert "previous attempt" in retry_query.lower()

    def test_gives_up_after_budget_and_records_error(self):
        runner = FlakyRunner(fail_times=99, content="never")
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            max_subtask_retries=2,
        )
        result = sup.run("scrape the site")
        assert len(runner.calls) == 3, "1 initial + 2 retries"
        assert result.subtasks[0].error is not None

    def test_retries_disabled_is_single_attempt(self):
        runner = FlakyRunner(fail_times=99, content="never")
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            max_subtask_retries=0,
        )
        result = sup.run("scrape the site")
        assert len(runner.calls) == 1
        assert result.subtasks[0].error is not None

    def test_default_enables_one_retry(self):
        # The whole point of Layer 2: don't quit on the first failure by
        # default.
        runner = FlakyRunner(fail_times=1, content="recovered")
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
        )
        result = sup.run("scrape the site")
        assert len(runner.calls) == 2
        assert result.subtasks[0].content == "recovered"


class TestAsyncSupervisorRetry:

    def test_recovers_on_retry_sequential(self):
        runner = AsyncFlakyRunner(fail_times=1, content="scraped 12 links")
        sup = AsyncSupervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            sequential=True,
            max_subtask_retries=1,
        )
        result = asyncio.run(sup.run("scrape the site"))
        assert len(runner.calls) == 2
        assert result.subtasks[0].error is None
        assert result.subtasks[0].content == "scraped 12 links"

    def test_recovers_on_retry_concurrent(self):
        runner = AsyncFlakyRunner(fail_times=1, content="ok")
        sup = AsyncSupervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            max_subtask_retries=1,
        )
        result = asyncio.run(sup.run("scrape the site"))
        assert len(runner.calls) == 2
        assert result.subtasks[0].error is None

    def test_gives_up_after_budget(self):
        runner = AsyncFlakyRunner(fail_times=99, content="never")
        sup = AsyncSupervisor(
            model=plan_model("worker"),
            agents={"worker": ("a worker", runner)},
            verbose=False,
            max_subtask_retries=2,
        )
        result = asyncio.run(sup.run("scrape the site"))
        assert len(runner.calls) == 3
        assert result.subtasks[0].error is not None
