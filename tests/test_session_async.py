"""AsyncSupervisor(ask_when_done=..., follow_up=...): the same session, awaited."""

import asyncio
import json

from agentx_dev import AsyncSupervisor, CostBudgetExceeded, Persistence
from agentx_dev.Operator import NoChannel
from tests.conftest import MockModel
from tests.memory_helpers import FakeStore
from tests.subagent_helpers import PLANNER_MARK, ScriptedRunner, plan_json, router, step

PLAN = plan_json(step("s1", "worker", "do it"))
ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})


def asker_of(*replies):
    queue = list(replies)
    seen = []

    def ask(question):
        seen.append(question)
        item = queue.pop(0) if queue else None
        if isinstance(item, BaseException):
            raise item
        return item
    ask.seen = seen
    return ask


def checkpoints(ask):
    return [q for q in ask.seen if "What should I do next?" in q]


def sup(model, ask, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", ScriptedRunner())},
                           ask_user=ask, **kw)


def run(supervisor, task="task"):
    return asyncio.run(supervisor.run(task))


class TestOff:
    def test_nothing_is_asked_and_the_result_is_unchanged(self):
        ask = asker_of("never used")
        result = run(sup(router(plans=[PLAN]), ask))
        assert ask.seen == [] and result.next_step is None and result.rounds == []


class TestAskWhenDone:
    def test_one_round_then_the_question(self):
        ask = asker_of("write the emails")
        model = router(plans=[PLAN], synth="Final.")
        result = run(sup(model, ask, ask_when_done=True))
        shown = checkpoints(ask)[0]
        assert "Final." in shown and "Outcome: done." in shown
        assert result.next_step == "write the emails" and result.rounds == []
        assert len(model.planner_prompts()) == 1
        assert [r["source"] for r in result.asked] == ["supervisor"]

    def test_an_asker_that_raises_ends_quietly(self):
        result = run(sup(router(plans=[PLAN]), asker_of(NoChannel()), follow_up=True))
        assert result.next_step is None and result.asked[-1]["reason"] == "no_channel"

    def test_it_asks_whatever_the_outcome(self):
        failing = ScriptedRunner(("gave up", "stuck"), ("gave up", "stuck"))
        ask = asker_of("try something else")
        s = AsyncSupervisor(model=router(plans=[PLAN]), agents={"worker": ("w", failing)}, verbose=False,
                            ask_user=ask, ask_when_done=True)
        result = run(s)
        assert result.outcome != "done" and "Outcome: " + result.outcome in checkpoints(ask)[0]


class TestFollowUp:
    def test_two_rounds_then_done(self):
        ask = asker_of("compare pricing", "done")
        model = router(plans=[PLAN, PLAN], synth="Final.")
        result = run(sup(model, ask, follow_up=True), "Research X")
        second = model.planner_prompts()[1]
        assert "WHAT HAS HAPPENED SO FAR" in second and "The operator now asks: compare pricing" in second
        assert result.query == "compare pricing" and result.next_step == "done"
        assert [r["round"] for r in result.rounds] == [1, 2]

    def test_events_end_with_one_completion_and_mark_the_rounds(self):
        async def collect():
            s = sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True)
            return [e async for e in s.astream("task")]
        events = asyncio.run(collect())
        types = [e["type"] for e in events]
        assert types.count("completion") == 1 and types[-1] == "completion"
        assert types.count("round") == 1 and types.count("checkpoint") == 2
        assert [e["source"] for e in events if e["type"] == "question"] == ["supervisor", "supervisor"]

    def test_the_cap_stops_the_questions(self):
        ask = asker_of("a", "b", "c")
        model = router(plans=[PLAN, PLAN, PLAN])
        result = run(sup(model, ask, follow_up=True, max_follow_ups=2))
        assert len(model.planner_prompts()) == 3 and len(checkpoints(ask)) == 2
        assert len(result.rounds) == 3 and result.next_step is None

    def test_a_question_answered_in_round_one_is_not_asked_again(self):
        ask = asker_of("Notion, Obsidian, Coda", "more please", "done")
        model = router(plans=[ASK_PLAN, PLAN, ASK_PLAN, PLAN])
        result = run(sup(model, ask, follow_up=True), "Compare our competitors")
        assert len([q for q in ask.seen if "Which three competitors" in q]) == 1
        assert "Notion, Obsidian, Coda" in model.planner_prompts()[-1] and len(result.rounds) == 2

    def test_only_round_one_is_saved_to_memory(self):
        store = FakeStore()
        run(sup(router(plans=[PLAN, PLAN], synth="Final."), asker_of("more", "done"), follow_up=True,
                memory=store))
        assert store.kinds_added().count("run_result") == 1

    def test_no_round_starts_after_the_cost_budget_is_spent(self):
        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "Final."
        model = MockModel(script=script)
        model.planner_prompts = lambda: [str(c[0]["content"]) for c in model.calls
                                         if PLANNER_MARK in str(c[0]["content"])]
        ask = asker_of("try again")
        result = run(sup(model, ask, follow_up=True, persistence=Persistence(max_minutes=5)))
        assert result.outcome == "out_of_budget" and "cost limit reached" in checkpoints(ask)[0]
        assert result.next_step == "try again" and result.rounds == []
        assert len(model.planner_prompts()) == 1
