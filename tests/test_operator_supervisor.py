"""Supervisor + ask_user: the planner asks up front, agents ask mid-run, answers carry through."""

import json

import pytest

from agentx_dev import AgentRunner, AgentType, Persistence, Supervisor
from agentx_dev import Operator as op
from agentx_dev.SubAgents import SpawnConfig
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import ScriptedRunner, new_agent_step, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})
ANSWER = "Notion, Obsidian, Coda"
ASK_MARK = "ASKING THE OPERATOR"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return Supervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


class TestPlannerAsks:
    def test_the_planner_asks_then_replans_with_the_answers(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker", "compare " + ANSWER))],
                       synth="Compared.")
        worker = ScriptedRunner()
        asked = []
        sup = supervisor(model, worker, ask_user=lambda q: asked.append(q) or ANSWER)
        result = sup.run("Compare the pricing pages of our three competitors")

        first, second = model.planner_prompts()
        assert ASK_MARK in first and "OPERATOR ANSWERS" not in first
        assert "OPERATOR ANSWERS" in second and ANSWER in second and ASK_MARK not in second
        # the asker receives the question with the planner's reason appended (see Operator._shown)
        assert asked == ["Which three competitors should I compare?\n(context: the task does not name them)"]
        assert result.query == "Compare the pricing pages of our three competitors"     # the original
        assert result.content == "Compared." and result.outcome == "done"
        assert result.asked == [{"source": "planner", "question": "Which three competitors should I compare?",
                                 "answered": True, "reason": None, "deduped": False}]

    def test_the_answers_reach_the_dispatched_step_and_synthesis(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker", "go"))])
        worker = ScriptedRunner()
        supervisor(model, worker, ask_user=lambda q: ANSWER).run("Compare our competitors")
        assert "OPERATOR ANSWERS" in worker.calls[0][0] and ANSWER in worker.calls[0][0]
        synth_prompt = next(str(c[0]["content"]) for c in model.calls if "answering the user's question" in str(c[0]["content"]))
        assert "OPERATOR ANSWERS" in synth_prompt and ANSWER in synth_prompt

    def test_no_answer_plans_on_assumptions(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        result = supervisor(model, ask_user=lambda q: None).run("Compare our competitors")
        second = model.planner_prompts()[1]
        assert "No operator answered" in second and "OPERATOR ANSWERS" not in second
        assert result.outcome == "done" and result.asked[0]["answered"] is False
        assert result.asked[0]["reason"] == "declined"

    def test_a_plan_wins_over_an_ask(self):
        both = json.dumps({"plan": [step("s1", "worker")], "ask": [{"question": "ignored?"}]})
        model = router(plans=[both])
        asked = []
        result = supervisor(model, ask_user=lambda q: asked.append(q) or "x").run("task")
        assert asked == [] and result.asked == [] and len(model.planner_prompts()) == 1

    def test_a_malformed_ask_is_the_existing_no_plan_failure(self):
        model = router(plans=[json.dumps({"ask": "what?"})])
        result = supervisor(model, ask_user=lambda q: "x").run("task")
        assert result.content == "Supervisor failed to produce a valid plan." and result.outcome == "stuck"

    def test_the_planner_cannot_ask_twice(self):
        model = router(plans=[ASK_PLAN, ASK_PLAN])
        asked = []
        result = supervisor(model, ask_user=lambda q: asked.append(q) or "x").run("task")
        assert len(asked) == 1 and result.outcome == "stuck"

    def test_the_question_budget_limits_what_the_planner_may_ask(self):
        two = json.dumps({"ask": [{"question": "one?"}, {"question": "two?"}]})
        model = router(plans=[two, plan_json(step("s1", "worker"))])
        asked = []
        supervisor(model, ask_user=lambda q: asked.append(q) or "x", max_questions=1).run("task")
        assert asked == ["one?"] and "at most 1 questions" in model.planner_prompts()[0]

    def test_zero_questions_never_offers_the_ask_option(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model, ask_user=lambda q: "x", max_questions=0).run("task")
        assert ASK_MARK not in model.planner_prompts()[0]

    def test_events_show_the_question_and_whether_it_was_answered(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        sup = supervisor(model, ask_user=lambda q: ANSWER)
        events = list(sup.stream("task"))
        types = [e["type"] for e in events]
        assert types.index("question") < types.index("answer") < types.index("plan")
        question = next(e for e in events if e["type"] == "question")
        answer = next(e for e in events if e["type"] == "answer")
        assert question["source"] == "planner" and "Which three" in question["question"]
        assert answer["answered"] is True and ANSWER not in str(answer)


class TestOff:
    def test_unset_changes_nothing(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = ScriptedRunner()
        result = supervisor(model, worker).run("task")
        assert ASK_MARK not in model.planner_prompts()[0]
        assert "OPERATOR" not in worker.calls[0][0] and result.asked == []
        assert not any(e["type"] in ("question", "answer") for e in supervisor(
            router(plans=[plan_json(step("s1", "worker"))]), None).stream("task"))

    def test_the_constructor_validates_ask_user(self):
        with pytest.raises(TypeError):
            supervisor(router(), ask_user="yes")

        async def ask(q):
            return "x"
        with pytest.raises(TypeError, match="async"):
            supervisor(router(), ask_user=ask)


def asking_worker(question="Which file?"):
    turns = []

    def script(messages):
        turns.append(1)
        if len(turns) == 1:
            return make_react_response("ask_user", {"question": question})
        return make_final("worker used: " + str(messages[-1]))
    return AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[], verbose=False)


class TestAgentsAskMidRun:
    def test_a_registered_specialist_can_ask_and_is_restored_afterwards(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        worker = asking_worker()
        asked = []
        result = supervisor(model, worker, ask_user=lambda q: asked.append(q) or "report.md").run("task")
        assert asked == ["Which file?"] and "report.md" in result.subtasks[0].content
        assert result.asked[0]["source"] == "worker" and result.asked[0]["answered"] is True
        assert not worker.registry.has("ask_user")

    def test_a_later_step_sees_the_answer_an_earlier_step_got(self):
        model = router(plans=[plan_json(step("s1", "worker"), step("s2", "scribe", deps=["s1"]))])
        scribe = ScriptedRunner()
        sup = Supervisor(model=model, verbose=False, ask_user=lambda q: "report.md",
                         agents={"worker": ("asks", asking_worker()), "scribe": ("writes", scribe)})
        sup.run("task")
        assert "OPERATOR ANSWERS" in scribe.calls[0][0] and "report.md" in scribe.calls[0][0]

    def test_a_planner_defined_helper_can_ask(self):
        turns = []

        def sub(messages):
            turns.append(1)
            return (make_react_response("ask_user", {"question": "Which three competitors?"})
                    if len(turns) == 1 else make_final("helper done"))
        model = router(plans=[plan_json(new_agent_step("s1", "analyst", "You analyse.", tools=["web"]))], sub=sub)
        asked = []
        sup = supervisor(model, spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
                         ask_user=lambda q: asked.append(q) or ANSWER)
        result = sup.run("task")
        assert asked == ["Which three competitors?"]
        assert result.asked[0]["source"] == "analyst" and result.subtasks[0].content == "helper done"

    def test_the_same_question_from_two_steps_is_asked_once(self):
        model = router(plans=[plan_json(step("a", "w1"), step("b", "w2"))])
        asked = []
        sup = Supervisor(model=model, verbose=False, ask_user=lambda q: asked.append(q) or "report.md",
                         agents={"w1": ("one", asking_worker()), "w2": ("two", asking_worker("which file?"))})
        result = sup.run("task")
        assert len(asked) == 1
        assert sorted(r["deduped"] for r in result.asked) == [False, True]


class TestPersistent:
    def test_the_run_budget_is_wired_to_the_channel_so_waiting_is_free(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        sup = supervisor(model, ask_user=lambda q: "x", persistence=Persistence(max_minutes=5))
        list(sup.stream("task"))
        from agentx_dev.Runner.Persistence import PausableClock
        assert sup._operator.budget is not None and isinstance(sup._operator.budget._clock, PausableClock)

    def test_recovery_planning_and_helpers_get_the_answers(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        sup = supervisor(model, worker, ask_user=lambda q: ANSWER, max_subtask_retries=0,
                         persistence=Persistence(max_minutes=5, max_replans=1))
        sup.run("Compare our competitors")
        prompts = model.planner_prompts()
        assert len(prompts) == 3 and "OPERATOR ANSWERS" in prompts[2] and ANSWER in prompts[2]
        assert ASK_MARK not in prompts[2]
