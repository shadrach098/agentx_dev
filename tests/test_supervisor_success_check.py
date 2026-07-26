"""Opt-in per-sub-task success validation for the Supervisor.

`max_subtask_retries` only retries sub-tasks that RAISE. But a sub-task
can also "succeed" while returning a useless result — a scraper that
saves 0 links, an extractor that finds nothing. `subtask_success_check`
lets the caller supply a predicate that decides whether a *returned*
result is actually acceptable; a rejection is retried (with the reason
fed back) the same way a raised exception is, bounded by
`max_subtask_retries`.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agentx_dev.Supervisor import Supervisor, AsyncSupervisor
from tests.conftest import MockModel


def plan_model(agent_name, final="Done."):
    plan = json.dumps({"plan": [{"agent": agent_name, "query": "do the thing"}]})
    return MockModel(script=[plan, final])


class SequenceRunner:
    """Returns a scripted content per call (last value repeats)."""

    def __init__(self, contents, tools=None):
        self.contents = list(contents)
        self.calls = []
        self.tools = tools or []

    def Initialize(self, query):
        self.calls.append(query)
        idx = min(len(self.calls) - 1, len(self.contents) - 1)
        return SimpleNamespace(content=self.contents[idx])


class AsyncSequenceRunner(SequenceRunner):
    async def Initialize(self, query):  # type: ignore[override]
        self.calls.append(query)
        idx = min(len(self.calls) - 1, len(self.contents) - 1)
        return SimpleNamespace(content=self.contents[idx])


def non_empty(result):
    """Success check: content must be non-empty, else a reason string."""
    if result.content and result.content.strip():
        return True
    return "the sub-task returned empty output — no data was produced"


class TestSyncSuccessCheck:

    def test_retries_until_check_passes(self):
        runner = SequenceRunner(["", "15 links saved"])
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False,
            max_subtask_retries=2,
            subtask_success_check=non_empty,
        )
        result = sup.run("scrape")
        assert len(runner.calls) == 2
        assert result.subtasks[0].error is None
        assert result.subtasks[0].content == "15 links saved"

    def test_reason_is_fed_back_on_retry(self):
        runner = SequenceRunner(["", "ok"])
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, max_subtask_retries=1,
            subtask_success_check=non_empty,
        )
        sup.run("scrape")
        retry_query = runner.calls[1]
        assert "empty output" in retry_query
        assert "success criteria" in retry_query.lower()

    def test_exhausts_and_flags_error(self):
        runner = SequenceRunner([""])  # always empty
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, max_subtask_retries=2,
            subtask_success_check=non_empty,
        )
        result = sup.run("scrape")
        assert len(runner.calls) == 3  # 1 + 2 retries
        assert result.subtasks[0].error is not None

    def test_passing_check_does_not_retry(self):
        runner = SequenceRunner(["good on first try"])
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, max_subtask_retries=3,
            subtask_success_check=non_empty,
        )
        sup.run("scrape")
        assert len(runner.calls) == 1

    def test_no_check_accepts_thin_result(self):
        runner = SequenceRunner([""])
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, max_subtask_retries=2,
        )
        result = sup.run("scrape")
        assert len(runner.calls) == 1
        assert result.subtasks[0].error is None

    def test_broken_check_does_not_wedge_the_run(self):
        def boom(result):
            raise RuntimeError("bad check")
        runner = SequenceRunner(["content"])
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, max_subtask_retries=2,
            subtask_success_check=boom,
        )
        result = sup.run("scrape")
        assert len(runner.calls) == 1  # treated as pass
        assert result.subtasks[0].content == "content"

    def test_bool_false_uses_generic_reason(self):
        runner = SequenceRunner(["x", "y"])
        sup = Supervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, max_subtask_retries=1,
            subtask_success_check=lambda r: r.content == "y",
        )
        result = sup.run("scrape")
        assert len(runner.calls) == 2
        assert result.subtasks[0].content == "y"


class TestAsyncSuccessCheck:

    def test_retries_until_check_passes_sequential(self):
        runner = AsyncSequenceRunner(["", "15 links"])
        sup = AsyncSupervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False, sequential=True,
            max_subtask_retries=2,
            subtask_success_check=non_empty,
        )
        result = asyncio.run(sup.run("scrape"))
        assert len(runner.calls) == 2
        assert result.subtasks[0].error is None
        assert result.subtasks[0].content == "15 links"

    def test_retries_concurrent(self):
        runner = AsyncSequenceRunner(["", "done"])
        sup = AsyncSupervisor(
            model=plan_model("worker"),
            agents={"worker": ("w", runner)},
            verbose=False,
            max_subtask_retries=1,
            subtask_success_check=non_empty,
        )
        result = asyncio.run(sup.run("scrape"))
        assert len(runner.calls) == 2
        assert result.subtasks[0].error is None
