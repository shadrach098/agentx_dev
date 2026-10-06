"""AsyncSupervisor + ask_user: same behaviour as the sync class, with async and sync callbacks."""

import asyncio
import json

import pytest

from agentx_dev import AgentType, AsyncAgentRunner, AsyncSupervisor, CostBudgetExceeded, Persistence
from agentx_dev.SubAgents import SpawnConfig
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import PLANNER_MARK, ScriptedRunner, new_agent_step, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?", "why": "none named"}]})
ANSWER = "Notion, Obsidian, Coda"
ASK_MARK = "ASKING THE OPERATOR"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def run(sup, task="task"):
    return asyncio.run(sup.run(task))


async def collect(sup, task="task"):
    return [e async for e in sup.astream(task)]


def asking_worker(question="Which file?"):
    turns = []

    def script(messages):
        turns.append(1)
        if len(turns) == 1:
            return make_react_response("ask_user", {"question": question})
        return make_final("worker used: " + str(messages[-1]))
    return AsyncAgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)


class TestPlannerAsks:
    def test_an_async_callback_answers_the_planner(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker", "go"))], synth="Compared.")
        worker = ScriptedRunner()
        asked = []

        async def ask(q):
            asked.append(q)
            return ANSWER
        result = run(supervisor(model, worker, ask_user=ask), "Compare our competitors")
        first, second = model.planner_prompts()
        assert ASK_MARK in first and "OPERATOR ANSWERS" in second and ANSWER in second
        # the asker receives the question with the planner's reason appended (see Operator._shown)
        assert asked == ["Which three competitors should I compare?\n(context: none named)"]
        assert result.query == "Compare our competitors" and result.content == "Compared."
        assert result.asked[0]["source"] == "planner" and result.asked[0]["answered"] is True
        assert "OPERATOR ANSWERS" in worker.calls[0][0]

    def test_a_plain_function_works_too(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        result = run(supervisor(model, ask_user=lambda q: ANSWER))
        assert result.asked[0]["answered"] is True

    def test_no_answer_plans_on_assumptions(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        result = run(supervisor(model, ask_user=lambda q: None))
        assert "No operator answered" in model.planner_prompts()[1] and result.outcome == "done"

    def test_events_show_the_question_and_the_answer_before_the_plan(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        events = asyncio.run(collect(supervisor(model, ask_user=lambda q: ANSWER)))
        types = [e["type"] for e in events]
        assert types.index("question") < types.index("answer") < types.index("plan")

    def test_unset_changes_nothing(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        result = run(supervisor(model))
        assert ASK_MARK not in model.planner_prompts()[0] and result.asked == []

    def test_a_plan_wins_over_an_ask_and_zero_questions_hides_the_option(self):
        both = json.dumps({"plan": [step("s1", "worker")], "ask": [{"question": "ignored?"}]})
        asked = []
        result = run(supervisor(router(plans=[both]), ask_user=lambda q: asked.append(q) or "x"))
        assert asked == [] and result.asked == []
        model = router(plans=[plan_json(step("s1", "worker"))])
        run(supervisor(model, ask_user=lambda q: "x", max_questions=0))
        assert ASK_MARK not in model.planner_prompts()[0]

    def test_the_constructor_validates_ask_user(self):
        with pytest.raises(TypeError):
            supervisor(router(), ask_user="yes")


class TestAgentsAskMidRun:
    def test_an_async_specialist_asks_and_is_restored(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = asking_worker()
        asked = []
        result = run(supervisor(model, worker, ask_user=lambda q: asked.append(q) or "report.md"))
        assert asked == ["Which file?"] and "report.md" in result.subtasks[0].content
        assert result.asked[0]["source"] == "worker"
        assert not worker.registry.has("ask_user")

    def test_a_sync_specialist_under_the_async_supervisor_can_ask_too(self):
        from agentx_dev import AgentRunner
        turns = []

        def script(messages):
            turns.append(1)
            return (make_react_response("ask_user", {"question": "Which file?"})
                    if len(turns) == 1 else make_final("sync worker done"))
        sync_worker = AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)
        model = router(plans=[plan_json(step("s1", "worker"))])
        result = run(supervisor(model, sync_worker, ask_user=lambda q: "report.md"))
        assert result.asked[0]["answered"] is True and result.subtasks[0].content == "sync worker done"

    def test_a_planner_defined_helper_can_ask(self):
        turns = []

        def sub(messages):
            turns.append(1)
            return (make_react_response("ask_user", {"question": "Which three competitors?"})
                    if len(turns) == 1 else make_final("helper done"))
        model = router(plans=[plan_json(new_agent_step("s1", "analyst", "You analyse.", tools=["web"]))], sub=sub)
        result = run(supervisor(model, spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
                                ask_user=lambda q: ANSWER))
        assert result.asked[0]["source"] == "analyst" and result.subtasks[0].content == "helper done"

    def test_two_parallel_helpers_asking_the_same_thing_ask_the_human_once(self):
        model = router(plans=[plan_json(step("a", "w1"), step("b", "w2"))])
        asked = []
        sup = AsyncSupervisor(model=model, verbose=False, ask_user=lambda q: asked.append(q) or "report.md",
                              agents={"w1": ("one", asking_worker()), "w2": ("two", asking_worker("which file?"))})
        result = run(sup)
        assert len(asked) == 1
        assert sorted(r["deduped"] for r in result.asked) == [False, True]
        assert all("report.md" in s.content for s in result.subtasks)

    def test_a_later_step_sees_an_earlier_steps_answer(self):
        model = router(plans=[plan_json(step("s1", "worker"), step("s2", "scribe", deps=["s1"]))])
        scribe = ScriptedRunner()
        sup = AsyncSupervisor(model=model, verbose=False, ask_user=lambda q: "report.md",
                              agents={"worker": ("asks", asking_worker()), "scribe": ("writes", scribe)})
        run(sup)
        assert "OPERATOR ANSWERS" in scribe.calls[0][0] and "report.md" in scribe.calls[0][0]


class TestPersistent:
    def test_the_run_budget_is_wired_to_the_channel(self):
        from agentx_dev.Runner.Persistence import PausableClock
        model = router(plans=[plan_json(step("s1", "worker"))])
        sup = supervisor(model, ask_user=lambda q: "x", persistence=Persistence(max_minutes=5))
        run(sup)
        assert isinstance(sup._operator.budget._clock, PausableClock)

    def test_recovery_planning_gets_the_answers(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        sup = supervisor(model, worker, ask_user=lambda q: ANSWER, max_subtask_retries=0,
                         persistence=Persistence(max_minutes=5, max_replans=1))
        run(sup, "Compare our competitors")
        prompts = model.planner_prompts()
        assert len(prompts) == 3 and "OPERATOR ANSWERS" in prompts[2] and ASK_MARK not in prompts[2]


class TestBudgetStopAfterQuestions:
    def test_a_budget_stop_while_replanning_still_streams_the_question_and_answer(self):
        planner_calls = []

        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                planner_calls.append(1)
                if len(planner_calls) == 1:
                    return ASK_PLAN
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "unused"
        sup = supervisor(MockModel(script=script), ask_user=lambda q: ANSWER,
                         persistence=Persistence(max_minutes=5))
        events = asyncio.run(collect(sup, "Compare our competitors"))
        types = [e["type"] for e in events]
        assert types.index("question") < types.index("answer") < types.index("budget")
        assert ANSWER not in str(events[:-1])
        result = events[-1]["result"]
        assert result.outcome == "out_of_budget"
        assert result.asked[0]["source"] == "planner" and result.asked[0]["answered"] is True
