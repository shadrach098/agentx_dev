"""AsyncSupervisor(memory=...): the same behaviour as the sync class, with store calls off the loop."""

import asyncio
import json
import threading

import pytest

from agentx_dev import AsyncSupervisor, CostBudgetExceeded, Persistence
from agentx_dev import SupervisorMemory as sm
from agentx_dev.Embeddings import HashEmbeddings, VectorStore
from tests.memory_helpers import FakeStore, answer_hit, hit
from agentx_dev.Runner.Persistence import BudgetExpired
from tests.conftest import MockModel, make_final
from tests.subagent_helpers import PLANNER_MARK, SYNTH_MARK, ScriptedRunner, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?", "why": "none named"}]})
QUESTION = "Which three competitors should I compare?"
ANSWER = "Notion, Obsidian, Coda"
FACT = "Our fiscal year starts in April."
MARK = "FROM MEMORY"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def run(sup, task="task"):
    return asyncio.run(sup.run(task))


async def collect(sup, task="task"):
    return [e async for e in sup.astream(task)]


def synth_prompt(model):
    return next(str(c[0]["content"]) for c in model.calls if "answering the user's question" in str(c[0]["content"]))


class TestRead:
    def test_the_planner_sees_the_block_and_synthesis_does_not(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        store = FakeStore([hit(FACT)])
        result = run(supervisor(model, memory=store), "Compare our competitors")
        assert MARK in model.planner_prompts()[0] and MARK not in synth_prompt(model)
        assert result.query == "Compare our competitors"

    def test_every_step_gets_a_block_and_the_store_is_called_off_the_loop(self):
        model = router(plans=[plan_json(step("a", "worker", "look up A"), step("b", "worker", "look up B"))])
        worker = ScriptedRunner()
        store = FakeStore([hit(FACT)])
        loop_thread = {}

        async def go():
            loop_thread["id"] = threading.get_ident()
            return await supervisor(model, worker, memory=store).run("task")
        asyncio.run(go())
        assert all(MARK in call[0] for call in worker.calls) and len(worker.calls) == 2
        assert {s[0] for s in store.searches} == {"task", "look up A", "look up B"}
        assert all(t != loop_thread["id"] for t in store.search_threads)

    def test_parallel_steps_with_the_same_query_search_once(self):
        model = router(plans=[plan_json(step("a", "worker", "same query"), step("b", "worker", "same query"))])
        store = FakeStore([hit(FACT)])
        run(supervisor(model, memory=store))
        assert [s[0] for s in store.searches].count("same query") == 1

    def test_the_planner_is_told_not_to_ask_what_memory_answers(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        run(supervisor(model, memory=FakeStore([hit(FACT)]), ask_user=lambda q: "x"))
        assert sm.MEMORY_ASK_LINE.strip() in model.planner_prompts()[0]

    def test_events_and_top_k_zero(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        events = asyncio.run(collect(supervisor(model, memory=FakeStore([hit(FACT)]))))
        mem = [e for e in events if e["type"] == "memory"]
        assert [(e["stage"], e["hits"]) for e in mem] == [("plan", 1), ("step", 1)]
        assert [e["type"] for e in events].index("memory") < [e["type"] for e in events].index("plan")
        store = FakeStore([hit(FACT)])
        run(supervisor(router(plans=[plan_json(step("s1", "worker"))]), memory=store, memory_top_k=0))
        assert store.searches == [] and store.kinds_added() == ["run_result"]


class TestWrite:
    def test_a_completed_run_saves_its_final_answer(self):
        model = router(plans=[plan_json(step("s1", "worker"))], synth="The final answer.")
        store = FakeStore()
        result = run(supervisor(model, memory=store), "Compare our competitors")
        assert store.added[0][0] == ["Task: Compare our competitors\nResult: The final answer."]
        assert [m["kind"] for m in result.memory] == ["run_result"]

    def test_an_unfinished_run_and_a_read_only_memory_save_nothing(self):
        store = FakeStore()
        result = run(supervisor(router(plans=[plan_json(step("s1", "worker"))]),
                                ScriptedRunner(("", "stuck")), memory=store, max_subtask_retries=0))
        assert result.outcome != "done" and store.added == []
        store2 = FakeStore()
        run(supervisor(router(plans=[plan_json(step("s1", "worker"))]), memory=store2, memory_write=False))
        assert store2.added == []

    def test_an_operator_answer_is_saved_with_an_async_callback(self):
        async def ask(q):
            return ANSWER
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore()
        result = run(supervisor(model, memory=store, ask_user=ask))
        assert store.kinds_added() == ["operator_answer", "run_result"]
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
        result = run(sup)
        assert result.content.startswith("Stopped: the cost budget was reached.")
        assert store.added == [] and result.memory == []

    def test_the_stub_does_not_overwrite_a_good_earlier_result(self):
        store = VectorStore(embeddings=HashEmbeddings())
        sm.RunMemory(store).remember_result("task", "GOOD OLD ANSWER")
        sup = supervisor(synth_model([plan_json(step("s1", "worker"))], over_budget), memory=store,
                         memory_min_score=0.0, persistence=Persistence(max_minutes=5))
        run(sup)
        assert len(store) == 1 and "GOOD OLD ANSWER" in store._texts[0] and "Stopped" not in store._texts[0]

    def test_a_synthesis_stopped_by_the_time_limit_saves_nothing(self):
        def out_of_time():
            raise BudgetExpired("time is up")
        store = FakeStore()
        result = run(supervisor(synth_model([plan_json(step("s1", "worker"))], out_of_time), memory=store,
                                persistence=Persistence(max_minutes=5)))
        assert result.content.startswith("Stopped: the time limit was reached.") and store.added == []

    def test_an_empty_synthesis_saves_nothing(self):
        store = FakeStore()
        result = run(supervisor(synth_model([plan_json(step("s1", "worker"))], lambda: "  "), memory=store))
        assert result.outcome == "done" and store.added == [] and result.memory == []

    def test_the_next_run_saves_again_after_a_cut_off_one(self):
        store = FakeStore()
        answers = [over_budget, lambda: "Fine now."]
        sup = supervisor(synth_model([plan_json(step("s1", "worker")), plan_json(step("s1", "worker"))],
                                     lambda: answers.pop(0)()), memory=store,
                         persistence=Persistence(max_minutes=5))
        run(sup)
        assert store.added == []
        run(sup)
        assert store.kinds_added() == ["run_result"]


class TestExactReuse:
    def test_a_stored_answer_is_used_instead_of_asking(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        store = FakeStore([answer_hit(QUESTION, ANSWER)])
        result = run(supervisor(model, memory=store, ask_user=lambda q: pytest.fail("asked the operator")))
        assert result.asked[0]["from_memory"] is True and ANSWER in model.planner_prompts()[1]

    def test_a_second_run_with_a_real_store_does_not_ask_again(self):
        store = VectorStore(embeddings=HashEmbeddings())
        asked = []
        run(supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                       memory_min_score=0.0, ask_user=lambda q: asked.append(q) or ANSWER))
        assert len(asked) == 1 and len(store) == 2
        result = run(supervisor(router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))]), memory=store,
                                memory_min_score=0.0, ask_user=lambda q: pytest.fail("asked again")))
        assert result.asked[0]["from_memory"] is True


class TestRobustness:
    def test_a_failing_store_does_not_fail_the_run(self):
        result = run(supervisor(router(plans=[plan_json(step("s1", "worker"))]), memory=FakeStore(boom=True)))
        assert result.outcome == "done" and result.memory == []

    def test_validation(self):
        with pytest.raises(TypeError):
            supervisor(router(), memory=object())
        with pytest.raises(ValueError):
            supervisor(router(), memory=FakeStore(), memory_top_k=-1)

    def test_nothing_changes_without_memory(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner()
        result = run(supervisor(model, worker))
        assert MARK not in model.planner_prompts()[0] and MARK not in worker.calls[0][0] and result.memory == []

    def test_a_planning_time_budget_stop_still_reports_what_was_written(self):
        from agentx_dev import CostBudgetExceeded, Persistence
        from tests.conftest import MockModel
        from tests.subagent_helpers import PLANNER_MARK

        planner_calls = []

        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                planner_calls.append(1)
                if len(planner_calls) == 1:
                    return ASK_PLAN
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "unused"
        store = FakeStore()
        sup = supervisor(MockModel(script=script), memory=store, ask_user=lambda q: ANSWER,
                         persistence=Persistence(max_minutes=5))
        result = run(sup, "Compare our competitors")
        assert result.outcome == "out_of_budget"
        assert [m["kind"] for m in result.memory] == ["operator_answer"]
