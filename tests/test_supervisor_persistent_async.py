"""Persistent AsyncSupervisor: the sync scenarios on the async scheduler."""

import asyncio
import json
from types import SimpleNamespace

from agentx_dev import CostBudgetExceeded, Persistence
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.Supervisor import AsyncSupervisor
from tests.conftest import MockModel
from tests.test_supervisor_persistent import ScriptedRunner, plan, step

P = Persistence(max_minutes=5)


class AsyncScriptedRunner(ScriptedRunner):
    async def Initialize(self, query, _budget=None):          # type: ignore[override]
        return ScriptedRunner.Initialize(self, query, _budget)


def supervisor(model, agents, persistence=P, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return AsyncSupervisor(model=model, agents={k: (k, v) for k, v in agents.items()}, verbose=False,
                           persistence=persistence, **kw)


def run(sup, task="task"):
    return asyncio.run(sup.run(task))


class TestRecovery:
    def test_replans_a_failing_step_and_finishes_in_round_two(self):
        worker = AsyncScriptedRunner(("gave up", "stuck"))
        fixer = AsyncScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer", "another way")), "All done."])
        result = run(supervisor(model, {"worker": worker, "fixer": fixer}))
        assert result.outcome == "done" and result.content == "All done."
        assert [(s.step_id, s.superseded) for s in result.subtasks] == [("s1", True), ("fix", False)]
        assert "RECOVERY ROUND 2" in model.calls[1][0]["content"]
        assert len(worker.calls) == 1 and len(fixer.calls) == 1

    def test_never_reruns_completed_steps(self):
        a = AsyncScriptedRunner(("A-RESULT", "done"))
        b = AsyncScriptedRunner(("nope", "stuck"))
        fixer = AsyncScriptedRunner(("fixed with A", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")),
                                  plan(step("fix", "fixer", "use A", deps=["s1"])), "Done."])
        result = run(supervisor(model, {"a": a, "b": b, "fixer": fixer}))
        assert len(a.calls) == 1 and "A-RESULT" in fixer.calls[0][0] and result.outcome == "done"

    def test_stops_after_max_replans_without_progress(self):
        worker = AsyncScriptedRunner(("x", "stuck"))
        fixer = AsyncScriptedRunner(("y", "stuck"), ("z", "stuck"), ("w", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("f1", "fixer")),
                                  plan(step("f2", "fixer")), "Could not finish."])
        result = run(supervisor(model, {"worker": worker, "fixer": fixer},
                                persistence=Persistence(max_minutes=5, max_replans=2)))
        assert len(fixer.calls) == 2 and len(model.calls) == 4 and result.outcome == "stuck"

    def test_a_planner_failure_keeps_the_results(self):
        worker = AsyncScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "this is not json", "Partial answer."])
        result = run(supervisor(model, {"worker": worker}))
        assert result.content == "Partial answer." and not result.subtasks[0].superseded

    def test_astream_emits_a_replan_event(self):
        worker = AsyncScriptedRunner(("gave up", "stuck"))
        fixer = AsyncScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer")), "Done."])

        async def collect():
            return [e async for e in supervisor(model, {"worker": worker, "fixer": fixer}).astream("task")]

        events = asyncio.run(collect())
        replans = [e for e in events if e["type"] == "replan"]
        assert len(replans) == 1 and replans[0]["round"] == 2 and replans[0]["unresolved"] == ["s1"]

    def test_closing_astream_early_in_a_recovery_round_cancels_running_subtasks(self):
        """Mirror of the run_plan early-exit test, but the cancellation must happen in a RECOVERY round."""
        finished = []

        class Fast:
            tools = []
            persistence = None

            async def Initialize(self, query, _budget=None):
                return SimpleNamespace(content="fast", outcome="done", output=None, progress=None)

        class Slow:
            tools = []
            persistence = None

            async def Initialize(self, query, _budget=None):
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    finished.append("cancelled")
                    raise
                return SimpleNamespace(content="slow", outcome="done", output=None, progress=None)

        worker = AsyncScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")),
                                  plan(step("rf", "fast"), step("rs", "slow")), "final"])
        sup = supervisor(model, {"worker": worker, "fast": Fast(), "slow": Slow()})

        async def scenario():
            gen = sup.astream("task")
            in_recovery = False
            async for event in gen:
                if event["type"] == "replan":
                    in_recovery = True
                elif in_recovery and event["type"] == "subtask_result":
                    break          # the fast recovery step is done; the slow one is in flight
            await gen.aclose()
            return list(finished)          # must already be populated when aclose() returns

        assert asyncio.run(scenario()) == ["cancelled"]


class TestSharedBudget:
    def test_every_specialist_gets_the_same_deadline(self):
        a, b = AsyncScriptedRunner(("1", "done")), AsyncScriptedRunner(("2", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")), "ok"])
        run(supervisor(model, {"a": a, "b": b}))
        assert isinstance(a.calls[0][1], RunBudget) and a.calls[0][1] is b.calls[0][1]

    def test_persistence_is_applied_and_restored(self):
        worker = AsyncScriptedRunner(("ok", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), "ok"])
        run(supervisor(model, {"worker": worker}))
        assert worker.seen_persistence == [P] and worker.persistence is None

    def test_a_spent_budget_skips_the_work(self, monkeypatch):
        monkeypatch.setattr(RunBudget, "start",
                            classmethod(lambda cls, minutes, clock=None: cls(0.0, lambda: 1.0)))
        worker = AsyncScriptedRunner(("ok", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = run(supervisor(model, {"worker": worker}))
        assert worker.calls == [] and len(model.calls) == 2
        assert result.outcome == "out_of_time" and result.subtasks[0].outcome == "out_of_time"

    def test_a_specialist_that_hit_the_cost_cap_ends_the_run(self):
        worker = AsyncScriptedRunner(("partial", "out_of_budget"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = run(supervisor(model, {"worker": worker}))
        assert len(worker.calls) == 1 and len(model.calls) == 2 and result.outcome == "out_of_budget"

    def test_a_cost_error_during_synthesis_falls_back_to_the_results(self):
        n = []

        def script(messages):
            n.append(1)
            if len(n) == 1:
                return plan(step("s1", "worker"))
            raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)

        result = run(supervisor(MockModel(script=script), {"worker": AsyncScriptedRunner(("fine", "done"))}))
        assert result.content.startswith("Stopped: the cost budget was reached") and "A: fine" in result.content


def test_without_persistence_a_failure_is_not_replanned():
    worker = AsyncScriptedRunner(("gave up", "stuck"))
    model = MockModel(script=[plan(step("s1", "worker")), "Final."])
    result = asyncio.run(AsyncSupervisor(model=model, agents={"worker": ("w", worker)}, verbose=False,
                                         max_subtask_retries=0).run("task"))
    assert len(model.calls) == 2 and result.outcome == "stuck" and worker.calls[0][1] is None
