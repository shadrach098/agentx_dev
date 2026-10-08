"""Supervisor(memory=...): the planner and every step get relevant memory; answers and results are saved."""

import json

import pytest

from agentx_dev import CostBudgetExceeded, Persistence, Supervisor
from agentx_dev import SupervisorMemory as sm
from agentx_dev.Embeddings import HashEmbeddings, VectorStore
from tests.memory_helpers import FakeStore, answer_hit, hit, result_hit
from agentx_dev.Runner.Persistence import BudgetExpired
from tests.conftest import MockModel, make_final
from tests.subagent_helpers import PLANNER_MARK, SYNTH_MARK, ScriptedRunner, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})
QUESTION = "Which three competitors should I compare?"
ANSWER = "Notion, Obsidian, Coda"
FACT = "Our fiscal year starts in April."
MARK = "FROM MEMORY"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return Supervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def synth_prompt(model):
    return next(str(c[0]["content"]) for c in model.calls if "answering the user's question" in str(c[0]["content"]))


class TestRead:
    def test_the_planner_sees_the_block_and_synthesis_does_not(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        result = supervisor(model, memory=store).run("Compare our competitors")
        assert MARK in model.planner_prompts()[0] and FACT in model.planner_prompts()[0]
        assert MARK not in synth_prompt(model)
        assert result.query == "Compare our competitors"
        assert store.searches[0][0] == "Compare our competitors"

    def test_every_dispatched_step_gets_a_block_for_its_own_query(self):
        model = router(plans=[plan_json(step("a", "worker", "look up A"), step("b", "worker", "look up B", deps=["a"]))])
        worker = ScriptedRunner()
        store = FakeStore([hit(FACT)])
        supervisor(model, worker, memory=store).run("task")
        assert all(MARK in call[0] and FACT in call[0] for call in worker.calls) and len(worker.calls) == 2
        assert [s[0] for s in store.searches] == ["task", "look up A", "look up B"]

    def test_a_retry_carries_the_block(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("ok", "done"))
        supervisor(model, worker, memory=FakeStore([hit(FACT)])).run("task")
        assert len(worker.calls) == 2 and all(MARK in call[0] for call in worker.calls)

    def test_a_recovery_round_gets_the_planner_block(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        supervisor(model, worker, memory=FakeStore([hit(FACT)]), max_subtask_retries=0,
                   persistence=Persistence(max_minutes=5, max_replans=1)).run("task")
        prompts = model.planner_prompts()
        assert len(prompts) == 2 and all(MARK in p for p in prompts)

    def test_the_planner_is_told_not_to_ask_what_memory_answers(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model, memory=FakeStore([hit(FACT)]), ask_user=lambda q: "x").run("task")
        assert sm.MEMORY_ASK_LINE.strip() in model.planner_prompts()[0]

    def test_no_hits_means_no_block_and_no_ask_line(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model, memory=FakeStore([]), ask_user=lambda q: "x").run("task")
        prompt = model.planner_prompts()[0]
        assert MARK not in prompt and sm.MEMORY_ASK_LINE.strip() not in prompt

    def test_memory_events_come_before_the_plan_and_after_each_step(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        events = list(supervisor(model, memory=FakeStore([hit(FACT)])).stream("task"))
        kinds = [e["type"] for e in events]
        mem = [e for e in events if e["type"] == "memory"]
        assert [(e["stage"], e["hits"]) for e in mem] == [("plan", 1), ("step", 1)]
        assert kinds.index("memory") < kinds.index("plan")

    def test_top_k_zero_never_searches_but_still_writes(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        supervisor(model, memory=store, memory_top_k=0).run("task")
        assert store.searches == [] and store.kinds_added() == ["run_result"]

    def test_the_configured_limits_reach_the_store(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        supervisor(model, memory=store, memory_top_k=2, memory_min_score=0.7).run("task")
        assert all(s[1:] == (2, 0.7) for s in store.searches)


class TestWrite:
    def test_a_completed_run_saves_its_final_answer(self):
        model = router(plans=[plan_json(step("s1", "worker"))], synth="The final answer.")
        store = FakeStore()
        result = supervisor(model, memory=store).run("Compare our competitors")
        [(texts, ids, metas)] = store.added
        assert texts == ["Task: Compare our competitors\nResult: The final answer."]
        assert ids == [sm.result_id("Compare our competitors")] and metas[0]["kind"] == "run_result"
        assert [m["kind"] for m in result.memory] == ["run_result"] and result.memory[0]["id"] == ids[0]

    def test_a_run_that_did_not_finish_saves_nothing(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore()
        result = supervisor(model, ScriptedRunner(("", "stuck")), memory=store, max_subtask_retries=0).run("task")
        assert result.outcome != "done" and store.added == [] and result.memory == []

    def test_a_no_plan_run_saves_nothing(self):
        store = FakeStore()
        result = supervisor(router(plans=["not json"]), memory=store).run("task")
        assert result.outcome == "stuck" and store.added == []

    def test_a_read_only_memory_writes_nothing_but_still_reads(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        result = supervisor(model, memory=store, memory_write=False).run("task")
        assert store.added == [] and result.memory == [] and MARK in model.planner_prompts()[0]

    def test_an_operator_answer_is_saved_when_it_is_given(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore()
        result = supervisor(model, memory=store, ask_user=lambda q: ANSWER).run("Compare our competitors")
        assert store.kinds_added() == ["operator_answer", "run_result"]
        assert store.added[0][1] == [sm.answer_id(QUESTION)]
        assert [m["kind"] for m in result.memory] == ["operator_answer", "run_result"]


def synth_model(plans, synth_fn):
    """A role-routing model whose synthesis call is ``synth_fn()`` (it may raise)."""
    queue = list(plans)

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first:
            return queue.pop(0)
        if SYNTH_MARK in first:
            return synth_fn()
        return make_final("agent done")

    return MockModel(script=script)


def over_budget():
    raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)


class TestSynthesisCutOff:
    """A run whose synthesis was cut off by the budget, or came back empty, saves no result."""

    def test_a_synthesis_stopped_by_the_cost_budget_saves_nothing(self):
        store = FakeStore()
        sup = supervisor(synth_model([plan_json(step("s1", "worker"))], over_budget), memory=store,
                         persistence=Persistence(max_minutes=5))
        result = sup.run("task")
        assert result.content.startswith("Stopped: the cost budget was reached.")
        assert store.added == [] and result.memory == []

    def test_the_stub_does_not_overwrite_a_good_earlier_result(self):
        store = VectorStore(embeddings=HashEmbeddings())
        sm.RunMemory(store).remember_result("task", "GOOD OLD ANSWER")
        sup = supervisor(synth_model([plan_json(step("s1", "worker"))], over_budget), memory=store,
                         memory_min_score=0.0, persistence=Persistence(max_minutes=5))
        sup.run("task")
        assert len(store) == 1 and "GOOD OLD ANSWER" in store._texts[0] and "Stopped" not in store._texts[0]

    def test_a_synthesis_stopped_by_the_time_limit_saves_nothing(self):
        def out_of_time():
            raise BudgetExpired("time is up")
        store = FakeStore()
        result = supervisor(synth_model([plan_json(step("s1", "worker"))], out_of_time), memory=store,
                            persistence=Persistence(max_minutes=5)).run("task")
        assert result.content.startswith("Stopped: the time limit was reached.") and store.added == []

    def test_an_empty_synthesis_saves_nothing(self):
        store = FakeStore()
        result = supervisor(synth_model([plan_json(step("s1", "worker"))], lambda: "  "), memory=store).run("task")
        assert result.outcome == "done" and store.added == [] and result.memory == []

    def test_the_next_run_saves_again_after_a_cut_off_one(self):
        store = FakeStore()
        answers = [over_budget, lambda: "Fine now."]
        sup = supervisor(synth_model([plan_json(step("s1", "worker")), plan_json(step("s1", "worker"))],
                                     lambda: answers.pop(0)()), memory=store,
                         persistence=Persistence(max_minutes=5))
        sup.run("task")
        assert store.added == []
        sup.run("task")
        assert store.kinds_added() == ["run_result"]


class TestExactReuse:
    def test_a_stored_answer_is_used_instead_of_asking(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore([answer_hit(QUESTION, ANSWER)])
        result = supervisor(model, memory=store, ask_user=lambda q: pytest.fail("asked the operator")).run("task")
        assert result.asked[0]["from_memory"] is True and result.asked[0]["answered"] is True
        assert ANSWER in model.planner_prompts()[1] and "operator_answer" not in store.kinds_added()

    def test_a_second_run_with_a_real_store_does_not_ask_again(self):
        store = VectorStore(embeddings=HashEmbeddings())
        asked = []
        first = supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                           memory_min_score=0.0, ask_user=lambda q: asked.append(q) or ANSWER)
        first.run("Compare our competitors")
        assert len(asked) == 1 and len(store) == 2
        second = supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                            memory_min_score=0.0, ask_user=lambda q: pytest.fail("asked again"))
        result = second.run("Compare our competitors")
        assert result.asked[0]["from_memory"] is True


class TestRobustness:
    def test_a_failing_store_does_not_fail_the_run(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        result = supervisor(model, memory=FakeStore(boom=True)).run("task")
        assert result.outcome == "done" and result.memory == []

    def test_a_bad_memory_value_is_a_type_error_and_bad_limits_are_value_errors(self):
        with pytest.raises(TypeError):
            supervisor(router(), memory=object())
        with pytest.raises(ValueError):
            supervisor(router(), memory=FakeStore(), memory_top_k=-1)

    def test_nothing_changes_without_memory(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner()
        result = supervisor(model, worker).run("task")
        assert MARK not in model.planner_prompts()[0] and MARK not in worker.calls[0][0]
        assert result.memory == [] and not any(e["type"] == "memory" for e in
                                               supervisor(router(plans=[plan_json(step("s1", "worker"))])).stream("task"))

    def test_a_second_run_on_the_same_supervisor_starts_with_fresh_records(self):
        store = FakeStore()
        sup = supervisor(router(plans=[plan_json(step("s1", "worker")), plan_json(step("s1", "worker"))]), memory=store)
        first = sup.run("task one")
        second = sup.run("task two")
        assert len(first.memory) == 1 and len(second.memory) == 1 and first.memory[0]["id"] != second.memory[0]["id"]
