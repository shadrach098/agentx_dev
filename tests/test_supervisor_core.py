"""Supervisor and AsyncSupervisor share one core (planning, replanning, synthesis, retries, rounds) written as
coroutines. The sync Supervisor runs them without an event loop, so it keeps what a sync API promises: everything
runs in the caller's thread, it works where an event loop is already running (a notebook), and closing its stream
early cleans up."""

import asyncio
import threading

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncSupervisor, Supervisor
from agentx_dev.Supervisor import _iterate_sync, _run_sync
from tests.conftest import MockModel, make_final
from tests.subagent_helpers import plan_json, router, step


def specialist(answer="done", seen=None):
    def script(messages):
        if seen is not None:
            seen.append(threading.get_ident())
        return make_final(answer)
    return AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)


def two_steps():
    return router(plans=[plan_json(step("a", "reader", "read"), step("b", "writer", "write", deps=["a"]))])


class TestTheSyncSupervisor:
    def test_every_step_runs_in_the_callers_thread(self):
        seen = []
        agents = {"reader": ("Reads.", specialist("read it", seen)), "writer": ("Writes.", specialist("ok", seen))}
        result = Supervisor(model=two_steps(), agents=agents, verbose=False).run("job")
        assert [sub.content for sub in result.subtasks] == ["read it", "ok"]
        assert seen and set(seen) == {threading.get_ident()}

    def test_it_runs_inside_a_running_event_loop(self):
        agents = {"reader": ("Reads.", specialist("read it")), "writer": ("Writes.", specialist("ok"))}

        async def in_a_notebook():
            return Supervisor(model=two_steps(), agents=agents, verbose=False).run("job")

        result = asyncio.run(in_a_notebook())
        assert [sub.agent for sub in result.subtasks] == ["reader", "writer"]

    def test_stopping_the_stream_early_restores_the_registry(self):
        agents = {"reader": ("Reads.", specialist("read it")), "writer": ("Writes.", specialist("ok"))}
        sup = Supervisor(model=two_steps(), agents=agents, verbose=False)
        before = dict(sup.agents)
        stream = sup.stream("job")
        for event in stream:
            if event["type"] == "subtask_result":
                break
        stream.close()
        assert sup.agents == before

    def test_an_error_in_a_step_reaches_the_caller_unchanged(self):
        class Boom(BaseException):
            pass

        def explode(messages):
            raise Boom()

        agents = {"reader": ("Reads.", AgentRunner(model=MockModel(script=explode), agent=AgentType.ReAct,
                                                    tools=[], verbose=False)),
                  "writer": ("Writes.", specialist())}
        with pytest.raises(Boom):
            Supervisor(model=two_steps(), agents=agents, verbose=False).run("job")


class TestTheDriver:
    def test_a_coroutine_that_never_suspends_runs_to_its_value(self):
        async def add(a, b):
            return a + b
        assert _run_sync(add(2, 3)) == 5

    def test_a_coroutine_that_suspends_is_refused(self):
        async def waits():
            await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="AsyncSupervisor"):
            _run_sync(waits())

    def test_closing_the_sync_iterator_runs_the_generators_finally(self):
        closed = []

        async def events():
            try:
                for n in range(5):
                    yield n
            finally:
                closed.append(True)

        it = _iterate_sync(events())
        assert next(it) == 0 and next(it) == 1
        it.close()
        assert closed == [True]


def test_both_classes_share_the_core():
    for name in ("_plan_once", "_plan", "_plan_recovery", "_synthesize", "_attempts", "_round_events",
                 "_session_events"):
        assert name not in vars(Supervisor) and name not in vars(AsyncSupervisor), name
