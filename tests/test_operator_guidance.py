"""Guidance that keeps a Supervisor from turning a gap into more questions.

A real run (planner asked, the operator gave one URL) showed three weaknesses: the replan had no
instruction to do the work, so it planned another "ask for the rest" step; the helper it spawned
had no tools, so everything looked like something only the operator could supply; and that helper
spent the shared question budget on clarifying. These tests pin the three fixes.
"""

import asyncio
import json

from agentx_dev import AsyncSupervisor, Persistence, Supervisor
from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel
from agentx_dev.SubAgents import SpawnConfig, SpawnPolicy, spawn_instruction
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step

ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})
PARTIAL = "only https://veltehub.com/#pricing"
ASK_MARK = "ASKING THE OPERATOR"
NOTE = "Do not plan a step whose job is only to ask for more information"


def supervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return Supervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


def asupervisor(model, worker=None, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", worker or ScriptedRunner())}, **kw)


class TestOneQuestionPerAgent:
    def test_an_agent_may_ask_once_and_the_next_question_gets_the_limit_note(self):
        calls = []
        ch = OperatorChannel.create(lambda q: calls.append(q) or "an answer", max_questions=5)
        first = ch.ask("What is the first thing?", source="helper")
        second = ch.ask("And what is the second thing?", source="helper")
        assert first.answered
        assert not second.answered and second.reason == "limit"
        assert calls == ["What is the first thing?"]
        assert ch.asked == 1                                  # the refused ask took no slot

    def test_another_agent_still_has_its_own_question(self):
        ch = OperatorChannel.create(lambda q: "a", max_questions=5)
        assert ch.ask("one?", source="helper_a").answered
        assert ch.ask("two?", source="helper_b").answered
        assert ch.asked == 2

    def test_the_planner_is_not_held_to_one(self):
        ch = OperatorChannel.create(lambda q: "a", max_questions=5)
        assert all(ch.ask(f"question {n}?", source="planner").answered for n in range(3))

    def test_an_agent_repeating_its_own_question_gets_the_cached_answer_not_the_limit(self):
        ch = OperatorChannel.create(lambda q: "Notion", max_questions=5)
        ch.ask("Which competitors?", source="helper")
        again = ch.ask("which competitors?", source="helper")
        assert again.answered and again.text == "Notion" and again.deduped

    def test_the_overall_limit_still_applies_across_agents(self):
        ch = OperatorChannel.create(lambda q: "a", max_questions=1)
        assert ch.ask("one?", source="helper_a").answered
        assert ch.ask("two?", source="helper_b").reason == "limit"

    def test_the_async_path_enforces_it_too(self):
        ch = OperatorChannel.create(lambda q: "a", max_questions=5, is_async=True)

        async def go():
            return await ch.aask("one?", source="helper"), await ch.aask("two?", source="helper")
        first, second = asyncio.run(go())
        assert first.answered and second.reason == "limit"

    def test_the_tool_text_tells_the_agent_to_search_first_and_ask_once(self):
        assert "Search with your tools first" in op.ASK_USER_DESCRIPTION
        assert "at most once" in op.ASK_USER_DESCRIPTION

    def test_a_refused_second_question_explains_itself_to_the_agent(self):
        ch = OperatorChannel.create(lambda q: "a", max_questions=5)
        ch.ask("one?", source="helper")
        text = op.reply_text(ch.ask("two?", source="helper"))
        assert text == op.NO_QUESTIONS_LEFT_TEXT
        assert "stated assumption" in text and "no more questions" in text.lower()
        assert op.reply_text(op.Reply(None, "declined")) == op.NO_ANSWER_TEXT      # other reasons unchanged


class TestAfterTheOperatorAnswers:
    def test_the_replan_is_told_to_do_the_work_not_ask_again(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        supervisor(model, ask_user=lambda q: PARTIAL).run("Compare our competitors")
        first, second = model.planner_prompts()
        assert NOTE not in first and ASK_MARK in first
        assert NOTE in second and "OPERATOR ANSWERS" in second and PARTIAL in second
        assert "give that specialist the tools it needs" in second

    def test_the_async_replan_gets_the_same_note(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        asyncio.run(asupervisor(model, ask_user=lambda q: PARTIAL).run("Compare our competitors"))
        first, second = model.planner_prompts()
        assert NOTE not in first and NOTE in second

    def test_with_no_answer_the_planner_gets_the_assumption_note_instead(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker"))])
        supervisor(model, ask_user=lambda q: None).run("Compare our competitors")
        second = model.planner_prompts()[1]
        assert "No operator answered" in second and NOTE not in second

    def test_nothing_changes_when_ask_user_is_off(self):
        model = router(plans=[plan_json(step("s1", "worker"))])
        supervisor(model).run("Compare our competitors")
        assert NOTE not in model.planner_prompts()[0]

    def test_a_recovery_round_is_told_the_same_thing(self):
        model = router(plans=[ASK_PLAN, plan_json(step("s1", "worker")), plan_json(step("s2", "worker"))])
        worker = ScriptedRunner(("", "stuck"), ("fixed", "done"))
        supervisor(model, worker, ask_user=lambda q: PARTIAL, max_subtask_retries=0,
                   persistence=Persistence(max_minutes=5, max_replans=1)).run("Compare our competitors")
        prompts = model.planner_prompts()
        assert len(prompts) == 3 and NOTE in prompts[2] and ASK_MARK not in prompts[2]


class TestHelpersNeedToolsToLookThingsUp:
    def policy(self, caps):
        return SpawnPolicy(SpawnConfig(enabled=True, capabilities=caps), router())

    def test_the_planner_is_told_a_specialist_with_no_tools_cannot_look_anything_up(self):
        text = spawn_instruction(self.policy({"web"}))
        assert "can only use the tools you give it" in text
        assert "cannot fetch anything" in text

    def test_when_web_is_allowed_the_hint_names_it(self):
        assert '("web" for anything online)' in spawn_instruction(self.policy({"web"}))

    def test_when_web_is_not_allowed_the_hint_does_not_offer_it(self):
        text = spawn_instruction(self.policy({"files_read"}))
        assert "for anything online" not in text and "cannot fetch anything" in text
