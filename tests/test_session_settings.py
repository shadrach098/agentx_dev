"""Constructor settings, the result fields, and the pure helpers of the ask-when-done session."""

import pytest

from agentx_dev import AsyncSupervisor, Supervisor
from agentx_dev.Supervisor import (
    SubtaskResult, SupervisorResult, _cap_text, _follow_up_task, _outcome_line,
)
from tests.subagent_helpers import ScriptedRunner, router


def make(cls, **kw):
    return cls(model=router(), agents={"w": ("w", ScriptedRunner())}, verbose=False, **kw)


@pytest.mark.parametrize("cls", [Supervisor, AsyncSupervisor])
class TestSettings:
    def test_everything_is_off_by_default(self, cls):
        sup = make(cls)
        assert (sup.ask_when_done, sup.follow_up, sup.max_follow_ups) == (False, False, 5)

    def test_follow_up_implies_ask_when_done(self, cls):
        sup = make(cls, ask_user=lambda q: "x", follow_up=True)
        assert sup.follow_up and sup.ask_when_done

    def test_ask_when_done_alone_does_not_follow_up(self, cls):
        sup = make(cls, ask_user=lambda q: "x", ask_when_done=True)
        assert sup.ask_when_done and not sup.follow_up

    @pytest.mark.parametrize("flag", ["ask_when_done", "follow_up"])
    @pytest.mark.parametrize("ask", [None, False])
    def test_either_flag_needs_ask_user(self, cls, flag, ask):
        with pytest.raises(ValueError, match="need ask_user"):
            make(cls, ask_user=ask, **{flag: True})

    def test_max_follow_ups_is_clamped_to_zero(self, cls):
        assert make(cls, ask_user=lambda q: "x", follow_up=True, max_follow_ups=-3).max_follow_ups == 0


class TestResultFields:
    def test_they_default_to_nothing(self):
        r = SupervisorResult(query="q", content="c")
        assert r.next_step is None and r.rounds == []


def sub(**kw):
    return SubtaskResult(agent="w", query="q", content="c", **kw)


class TestOutcomeLine:
    def test_done(self):
        assert _outcome_line(SupervisorResult(query="q", content="c")) == "Outcome: done."

    def test_partial_counts_failed_steps(self):
        r = SupervisorResult(query="q", content="c", outcome="partial",
                             subtasks=[sub(), sub(error="boom", outcome="stuck")])
        assert _outcome_line(r) == "Outcome: partial (1 of 2 steps failed)."

    def test_stuck_with_no_failed_step_has_no_count(self):
        assert _outcome_line(SupervisorResult(query="q", content="c", outcome="stuck")) == "Outcome: stuck."

    def test_spawn_bookkeeping_is_not_counted(self):
        spawn = SubtaskResult(agent="__spawn__", query="q", content="c")
        r = SupervisorResult(query="q", content="c", outcome="partial",
                             subtasks=[spawn, sub(), sub(error="boom", outcome="stuck")])
        assert "1 of 2 steps" in _outcome_line(r)

    def test_the_two_limits_are_named(self):
        assert _outcome_line(SupervisorResult(query="q", content="c", outcome="out_of_time")) == \
            "Outcome: out_of_time (time limit reached)."
        assert _outcome_line(SupervisorResult(query="q", content="c", outcome="out_of_budget")) == \
            "Outcome: out_of_budget (cost limit reached)."


class TestFollowUpTask:
    def rounds(self):
        return [{"round": 1, "query": "Research X", "content": "X is a thing.", "outcome": "done"},
                {"round": 2, "query": "now pricing", "content": "Pricing is $5.", "outcome": "partial"}]

    def test_original_then_history_then_the_new_instruction(self):
        text = _follow_up_task("Research X", self.rounds(), "write the emails")
        assert text.startswith("Research X\n\nWHAT HAS HAPPENED SO FAR")
        assert "Round 1, outcome done:\nX is a thing." in text
        assert "Round 2 (the operator asked: now pricing), outcome partial:\nPricing is $5." in text
        assert text.rstrip().endswith("Do not repeat work that is already done.")
        assert "The operator now asks: write the emails" in text

    def test_an_earlier_answer_is_capped(self):
        # "y" also occurs in the fixed text ("already"), so the bound leaves room for it.
        rounds = [{"round": 1, "query": "q", "content": "y" * 9000, "outcome": "done"}]
        assert _follow_up_task("q", rounds, "go").count("y") < 3100

    def test_cap_text(self):
        assert _cap_text("short", 10) == "short"
        assert _cap_text("z" * 50, 10) == "z" * 10 + " ..."
        assert _cap_text(None, 10) == ""
