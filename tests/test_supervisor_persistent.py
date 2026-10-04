"""Persistent Supervisor: recovery rounds under one shared budget (sync)."""

import json
from types import SimpleNamespace

import pytest

from agentx_dev import CostBudgetExceeded, Persistence
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.Supervisor import (
    SpawnConfig, Supervisor, _rename_colliding_ids, _sanitize_plan, _topo_order,
)
from tests.conftest import MockModel

P = Persistence(max_minutes=5)


def step(id_, agent, query="q", deps=None):
    d = {"id": id_, "agent": agent, "query": query}
    if deps:
        d["depends_on"] = deps
    return d


def plan(*steps):
    return json.dumps({"plan": list(steps)})


class ScriptedRunner:
    """Fake specialist. Each call returns the next (content, outcome)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.tools = []
        self.persistence = None
        self.seen_persistence = []

    def Initialize(self, query, _budget=None):
        self.calls.append((query, _budget))
        self.seen_persistence.append(self.persistence)
        content, outcome = self.results.pop(0) if self.results else ("ok", "done")
        progress = {"failed": ["tool(x) -> boom [rung 1]"]} if outcome != "done" else None
        return SimpleNamespace(content=content, outcome=outcome, output=None, progress=progress)


def supervisor(model, agents, persistence=P, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return Supervisor(model=model, agents={k: (k, v) for k, v in agents.items()}, verbose=False,
                      persistence=persistence, **kw)


class TestRecovery:
    def test_replans_a_failing_step_and_finishes_in_round_two(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer", "another way")), "All done."])
        result = supervisor(model, {"worker": worker, "fixer": fixer}).run("task")
        assert result.outcome == "done" and result.content == "All done."
        assert [(s.step_id, s.superseded, s.outcome) for s in result.subtasks] == [
            ("s1", True, "stuck"), ("fix", False, "done")]
        prompt = model.calls[1][0]["content"]
        assert "RECOVERY ROUND 2" in prompt and "[s1] worker" in prompt and "tool(x) -> boom" in prompt
        assert len(worker.calls) == 1 and len(fixer.calls) == 1

    def test_never_reruns_completed_steps_and_a_recovery_step_can_depend_on_them(self):
        a = ScriptedRunner(("A-RESULT", "done"))
        b = ScriptedRunner(("nope", "stuck"))
        fixer = ScriptedRunner(("fixed with A", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")),
                                  plan(step("fix", "fixer", "use A", deps=["s1"])), "Done."])
        result = supervisor(model, {"a": a, "b": b, "fixer": fixer}).run("task")
        assert len(a.calls) == 1
        assert "A-RESULT" in fixer.calls[0][0]
        assert result.outcome == "done"

    def test_a_recovery_step_reusing_an_old_id_gets_a_new_one(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("s1", "fixer")), "Done."])
        result = supervisor(model, {"worker": worker, "fixer": fixer}).run("task")
        assert [s.step_id for s in result.subtasks] == ["s1", "r2_s1"]

    def test_stops_after_max_replans_without_progress(self):
        worker = ScriptedRunner(("x", "stuck"))
        fixer = ScriptedRunner(("y", "stuck"), ("z", "stuck"), ("w", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("f1", "fixer")),
                                  plan(step("f2", "fixer")), "Could not finish."])
        result = supervisor(model, {"worker": worker, "fixer": fixer},
                            persistence=Persistence(max_minutes=5, max_replans=2)).run("task")
        assert len(fixer.calls) == 2 and len(model.calls) == 4
        assert result.outcome == "stuck" and result.content == "Could not finish."

    def test_a_planner_failure_keeps_the_results_and_goes_to_synthesis(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "this is not json", "Partial answer."])
        result = supervisor(model, {"worker": worker}).run("task")
        assert result.content == "Partial answer." and result.outcome == "stuck"
        assert result.subtasks[0].step_id == "s1" and not result.subtasks[0].superseded

    def test_the_synthesis_prompt_names_the_unfinished_steps(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "not json", "Partial."])
        supervisor(model, {"worker": worker}).run("task")
        synth_prompt = model.calls[2][0]["content"]
        assert "did NOT finish" in synth_prompt and "[s1] worker" in synth_prompt

    def test_stream_emits_a_replan_event(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer")), "Done."])
        events = list(supervisor(model, {"worker": worker, "fixer": fixer}).stream("task"))
        replans = [e for e in events if e["type"] == "replan"]
        assert len(replans) == 1 and replans[0]["round"] == 2 and replans[0]["unresolved"] == ["s1"]
        assert replans[0]["plan"][0]["id"] == "fix"
        assert [e["type"] for e in events if e["type"] in ("plan", "completion")] == ["plan", "completion"]


class TestSharedBudget:
    def test_every_specialist_gets_the_same_deadline(self):
        a, b = ScriptedRunner(("1", "done")), ScriptedRunner(("2", "done"))
        model = MockModel(script=[plan(step("s1", "a"), step("s2", "b")), "ok"])
        supervisor(model, {"a": a, "b": b}).run("task")
        budget_a, budget_b = a.calls[0][1], b.calls[0][1]
        assert isinstance(budget_a, RunBudget) and budget_a is budget_b

    def test_runners_without_their_own_settings_get_the_supervisors_for_the_run_only(self):
        worker = ScriptedRunner(("ok", "done"))
        own = ScriptedRunner(("ok", "done"))
        own.persistence = Persistence(max_minutes=1)
        model = MockModel(script=[plan(step("s1", "worker"), step("s2", "own")), "ok"])
        supervisor(model, {"worker": worker, "own": own}).run("task")
        assert worker.seen_persistence == [P] and worker.persistence is None
        assert own.seen_persistence[0].max_minutes == 1

    def test_a_spent_budget_skips_the_work_and_reports_out_of_time(self, monkeypatch):
        monkeypatch.setattr(RunBudget, "start",
                            classmethod(lambda cls, minutes, clock=None: cls(0.0, lambda: 1.0)))
        worker = ScriptedRunner(("ok", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = supervisor(model, {"worker": worker}).run("task")
        assert worker.calls == [] and len(model.calls) == 2
        assert result.subtasks[0].outcome == "out_of_time" and result.outcome == "out_of_time"

    def test_a_specialist_that_hit_the_cost_cap_ends_the_run_as_out_of_budget(self):
        worker = ScriptedRunner(("partial", "out_of_budget"))
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        result = supervisor(model, {"worker": worker}).run("task")
        assert len(worker.calls) == 1 and len(model.calls) == 2      # no retry, no replan
        assert result.outcome == "out_of_budget"

    def test_a_cost_error_during_synthesis_falls_back_to_the_results(self):
        n = []

        def script(messages):
            n.append(1)
            if len(n) == 1:
                return plan(step("s1", "worker"))
            raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)

        result = supervisor(MockModel(script=script), {"worker": ScriptedRunner(("fine", "done"))}).run("task")
        assert result.content.startswith("Stopped: the cost budget was reached") and "A: fine" in result.content


class TestRetriesAndCleanup:
    @pytest.mark.parametrize("outcome", ["out_of_budget", "out_of_time"])
    def test_a_spent_budget_outcome_is_not_retried(self, outcome):
        worker = ScriptedRunner(*[("partial", outcome)] * 5)
        model = MockModel(script=[plan(step("s1", "worker")), "Stopped."])
        supervisor(model, {"worker": worker}, max_subtask_retries=2).run("task")
        assert len(worker.calls) == 1

    def test_other_unfinished_outcomes_still_use_the_retries(self):
        worker = ScriptedRunner(*[("x", "stuck")] * 5)
        model = MockModel(script=[plan(step("s1", "worker")), "not json", "Final."])
        supervisor(model, {"worker": worker}, max_subtask_retries=2).run("task")
        assert len(worker.calls) == 3

    def test_closing_the_stream_early_restores_the_runner_persistence(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        fixer = ScriptedRunner(("fixed", "done"))
        model = MockModel(script=[plan(step("s1", "worker")), plan(step("fix", "fixer")), "Done."])
        gen = supervisor(model, {"worker": worker, "fixer": fixer}).stream("task")
        for event in gen:
            if event["type"] == "replan":
                assert worker.persistence is P       # applied while the run is live
                break
        gen.close()
        assert worker.persistence is None and fixer.persistence is None

    def test_a_runner_without_a_budget_parameter_still_works_in_persistent_mode(self):
        class Plain:
            tools = []
            persistence = None

            def Initialize(self, query):
                return SimpleNamespace(content="plain result", outcome="done", output=None, progress=None)

        model = MockModel(script=[plan(step("s1", "plain")), "Final."])
        result = supervisor(model, {"plain": Plain()}).run("task")
        assert result.outcome == "done" and result.subtasks[0].content == "plain result"


class TestDefaultModeUntouched:
    def test_without_persistence_a_failure_is_not_replanned(self):
        worker = ScriptedRunner(("gave up", "stuck"))
        model = MockModel(script=[plan(step("s1", "worker")), "Final."])
        result = Supervisor(model=model, agents={"worker": ("w", worker)}, verbose=False,
                            max_subtask_retries=0).run("task")
        assert len(model.calls) == 2 and result.outcome == "stuck"
        assert worker.calls[0][1] is None            # no shared budget handed out


class TestSpawnedSpecialists:
    def test_a_spawned_specialist_inherits_persistence(self, tmp_path):
        sup = Supervisor(
            model=MockModel(script=[]), agents={}, verbose=False, persistence=P,
            spawn_config=SpawnConfig(enabled=True, auto_spawn=True, auto_spawn_allowed_caps={"files"},
                                     allowed_paths=[str(tmp_path)]),
        )
        name, _ = sup._handle_spawn({"name": "scout", "description": "reads files", "capabilities": ["files"]})
        assert name == "scout" and sup.agents["scout"].runner.persistence is P


class TestPlanHelpers:
    def test_sanitize_keeps_dependencies_on_earlier_rounds_and_drops_ghosts(self):
        sane, repairs = _sanitize_plan(
            [{"id": "fix", "agent": "x", "query": "q", "depends_on": ["s1", "ghost"]}], known_ids={"s1"})
        assert sane[0]["depends_on"] == ["s1"] and any("ghost" in r for r in repairs)
        assert _topo_order(sane) == [0]

    def test_rename_colliding_ids_rewrites_references_inside_the_plan(self):
        out = _rename_colliding_ids(
            [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a", "s1"]}], {"a", "s1"}, 3)
        assert [s["id"] for s in out] == ["r3_a", "b"] and out[1]["depends_on"] == ["r3_a", "s1"]
