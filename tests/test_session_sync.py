"""Supervisor(ask_when_done=..., follow_up=...): the framework asks what to do next, and can go on."""

import json

import pytest

from agentx_dev import CostBudgetExceeded, Persistence, Supervisor
from agentx_dev.Operator import NoChannel
from agentx_dev.Runner.Persistence import RunBudget
from tests.conftest import MockModel
from tests.memory_helpers import FakeStore
from tests.subagent_helpers import PLANNER_MARK, ScriptedRunner, plan_json, router, step

PLAN = plan_json(step("s1", "worker", "do it"))
ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})


def asker_of(*replies):
    """An ask_user function that answers from a list (None when it runs out) and records what it saw."""
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
    return Supervisor(model=model, agents={"worker": ("does the work", ScriptedRunner())}, ask_user=ask, **kw)


class TestOff:
    def test_nothing_is_asked_and_the_result_is_unchanged(self):
        ask = asker_of("never used")
        model = router(plans=[PLAN])
        events = list(sup(model, ask).stream("task"))
        assert ask.seen == []
        assert {e["type"] for e in events}.isdisjoint({"round", "checkpoint"})
        result = events[-1]["result"]
        assert result.next_step is None and result.rounds == []


class TestAskWhenDone:
    def test_one_round_then_the_question(self):
        ask = asker_of("write the emails")
        model = router(plans=[PLAN], synth="Final.")
        result = sup(model, ask, ask_when_done=True).run("task")
        assert len(checkpoints(ask)) == 1
        shown = checkpoints(ask)[0]
        assert "Final." in shown and "Outcome: done." in shown and shown.endswith('(blank or "done" to finish)')
        assert result.next_step == "write the emails"
        assert len(model.planner_prompts()) == 1               # nothing ran on the reply
        assert result.rounds == []
        assert [r["source"] for r in result.asked] == ["supervisor"]

    def test_the_prompt_is_labelled_for_the_supervisor(self):
        ask = asker_of("x")
        result = sup(router(plans=[PLAN]), ask, ask_when_done=True).run("task")
        assert result.asked[0]["question"] == "What should I do next?"

    @pytest.mark.parametrize("reply", ["done", "Quit.", "That's all!"])
    def test_a_stop_word_is_still_returned_as_typed(self, reply):
        result = sup(router(plans=[PLAN]), asker_of(reply), follow_up=True).run("task")
        assert result.next_step == reply and result.rounds == []

    @pytest.mark.parametrize("reply", ["", None])
    def test_a_blank_or_missing_reply_is_none(self, reply):
        result = sup(router(plans=[PLAN]), asker_of(reply), follow_up=True).run("task")
        assert result.next_step is None

    @pytest.mark.parametrize("raised,reason", [(RuntimeError("boom"), "error"), (NoChannel(), "no_channel")])
    def test_an_asker_that_cannot_answer_ends_quietly(self, raised, reason):
        result = sup(router(plans=[PLAN]), asker_of(raised), follow_up=True).run("task")
        assert result.next_step is None and result.asked[-1]["reason"] == reason

    def test_it_asks_whatever_the_outcome_and_says_so(self):
        failing = ScriptedRunner(("gave up", "stuck"), ("gave up", "stuck"))
        ask = asker_of("try something else")
        s = Supervisor(model=router(plans=[PLAN]), agents={"worker": ("w", failing)}, verbose=False,
                       ask_user=ask, ask_when_done=True)
        result = s.run("task")
        assert result.outcome != "done" and "Outcome: " + result.outcome in checkpoints(ask)[0]
        assert result.next_step == "try something else"

    def test_a_huge_answer_is_cut_in_the_prompt_but_not_in_the_result(self):
        ask = asker_of("x")
        result = sup(router(plans=[PLAN], synth="A" * 10000), ask, ask_when_done=True).run("task")
        assert len(result.content) == 10000
        assert len(checkpoints(ask)[0]) < 3300 and "(full text in result.content)" in checkpoints(ask)[0]

    def test_it_uses_no_question_slot(self):
        result = sup(router(plans=[PLAN]), asker_of("thanks"), ask_when_done=True, max_questions=0).run("task")
        assert result.next_step == "thanks"


class TestFollowUp:
    def test_two_rounds_then_done(self):
        ask = asker_of("compare pricing", "done")
        model = router(plans=[PLAN, PLAN], synth="Final.")
        result = sup(model, ask, follow_up=True).run("Research X")
        assert len(model.planner_prompts()) == 2
        second = model.planner_prompts()[1]
        assert "Research X" in second and "WHAT HAS HAPPENED SO FAR" in second and "Final." in second
        assert "The operator now asks: compare pricing" in second
        assert result.query == "compare pricing" and result.next_step == "done"
        assert [r["round"] for r in result.rounds] == [1, 2]
        assert result.rounds[0] == {"round": 1, "query": "Research X", "content": "Final.", "outcome": "done"}
        assert len(checkpoints(ask)) == 2

    def test_asked_lists_every_checkpoint_in_order(self):
        result = sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True).run("task")
        assert [r["source"] for r in result.asked] == ["supervisor", "supervisor"]

    def test_events_end_with_one_completion_and_mark_the_rounds(self):
        events = list(sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True).stream("task"))
        types = [e["type"] for e in events]
        assert types.count("completion") == 1 and types[-1] == "completion"
        assert types.count("round") == 1 and types.count("checkpoint") == 2 and types.count("final") == 2
        assert types.index("checkpoint") < types.index("round")
        round_event = next(e for e in events if e["type"] == "round")
        assert round_event["round"] == 2 and round_event["query"] == "more"
        assert [e["source"] for e in events if e["type"] == "question"] == ["supervisor", "supervisor"]

    def test_the_cap_stops_the_questions(self):
        ask = asker_of("a", "b", "c")
        model = router(plans=[PLAN, PLAN, PLAN])
        result = sup(model, ask, follow_up=True, max_follow_ups=2).run("task")
        assert len(model.planner_prompts()) == 3 and len(checkpoints(ask)) == 2
        assert len(result.rounds) == 3 and result.next_step is None

    def test_zero_follow_ups_behaves_like_ask_when_done(self):
        model = router(plans=[PLAN, PLAN])
        result = sup(model, asker_of("do more"), follow_up=True, max_follow_ups=0).run("task")
        assert len(model.planner_prompts()) == 1 and result.next_step == "do more" and result.rounds == []

    def test_a_question_answered_in_round_one_is_not_asked_again(self):
        ask = asker_of("Notion, Obsidian, Coda", "more please", "done")
        model = router(plans=[ASK_PLAN, PLAN, ASK_PLAN, PLAN])
        result = sup(model, ask, follow_up=True).run("Compare our competitors")
        assert len([q for q in ask.seen if "Which three competitors" in q]) == 1
        second = model.planner_prompts()[-1]
        assert "OPERATOR ANSWERS" in second and "Notion, Obsidian, Coda" in second
        assert len(result.rounds) == 2

    def test_only_round_one_is_saved_to_memory(self):
        store = FakeStore()
        sup(router(plans=[PLAN, PLAN], synth="Final."), asker_of("more", "done"), follow_up=True,
            memory=store).run("task")
        assert store.kinds_added().count("run_result") == 1

    def test_each_round_gets_a_fresh_time_budget(self, monkeypatch):
        starts = []
        real = RunBudget.start
        monkeypatch.setattr(RunBudget, "start", staticmethod(lambda *a, **k: starts.append(1) or real(*a, **k)))
        sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True,
            persistence=Persistence(max_minutes=5)).run("task")
        assert len(starts) == 2

    def test_no_round_starts_after_the_cost_budget_is_spent(self):
        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "Final."
        model = MockModel(script=script)
        model.planner_prompts = lambda: [str(c[0]["content"]) for c in model.calls
                                         if PLANNER_MARK in str(c[0]["content"])]
        ask = asker_of("try again")
        result = sup(model, ask, follow_up=True, persistence=Persistence(max_minutes=5)).run("task")
        assert result.outcome == "out_of_budget" and "cost limit reached" in checkpoints(ask)[0]
        assert result.next_step == "try again" and result.rounds == []
        assert len(model.planner_prompts()) == 1
