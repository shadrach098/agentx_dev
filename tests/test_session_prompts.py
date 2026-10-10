"""With ask_when_done on, the Supervisor asks "what next?" itself, so the planner and the helpers
are told not to ask it (a double question, and from round 2 a replayed old answer)."""

import asyncio

import pytest

from agentx_dev import AsyncSupervisor, Supervisor
from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step


def planner_prompt(cls, **kw):
    model = router(plans=[plan_json(step("s1", "worker", "research"))])
    sup = cls(model=model, agents={"worker": ("worker", ScriptedRunner())}, verbose=False,
              ask_user=lambda q: "done", **kw)
    if cls is AsyncSupervisor:
        asyncio.run(sup.run("research this, and when done ask me what to do next"))
    else:
        sup.run("research this, and when done ask me what to do next")
    return model.planner_prompts()[0]


@pytest.mark.parametrize("cls", [Supervisor, AsyncSupervisor])
class TestPlannerNote:
    def test_with_ask_when_done_the_planner_plans_only_the_work(self, cls):
        text = planner_prompt(cls, ask_when_done=True)
        assert op.SESSION_PLAN_NOTE.strip() in text
        assert "call ask_user at that point" not in text
        assert op.CHECKPOINT_PLAN_NOTE.strip() not in text

    def test_with_follow_up_too(self, cls):
        text = planner_prompt(cls, follow_up=True)
        assert op.SESSION_PLAN_NOTE.strip() in text and "call ask_user at that point" not in text

    def test_flag_off_the_old_note_is_unchanged(self, cls):
        text = planner_prompt(cls)
        assert op.CHECKPOINT_PLAN_NOTE.strip() in text and "call ask_user at that point" in text
        assert op.SESSION_PLAN_NOTE.strip() not in text


class TestPlanNote:
    def test_the_answered_addition_stays_in_session_mode(self):
        ch = OperatorChannel.create(lambda q: "Notion")
        ch.session_checkpoint = True
        ch.ask("Which competitors?", source="planner")
        assert ch.plan_note() == op.SESSION_PLAN_NOTE + op.ANSWERED_PLAN_NOTE

    def test_the_default_is_off(self):
        ch = OperatorChannel.create(lambda q: "x")
        assert ch.session_checkpoint is False and ch.plan_note() == op.CHECKPOINT_PLAN_NOTE

    def test_the_session_note_says_the_supervisor_asks_itself(self):
        note = op.SESSION_PLAN_NOTE
        assert "Supervisor itself asks" in note and "do not plan a step" in note
        assert "do not tell any specialist to ask it" in note and "Plan only the work" in note


class TestHelperAddendum:
    def _addendum(self, operator):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(), operator=operator)
        built = policy.build(AgentSpec(name="scout", instructions="find", tools=("web",)))
        return built.runner.system_addendum

    def test_in_session_mode_the_helper_is_told_not_to_ask_what_next(self):
        ch = OperatorChannel.create(lambda q: "x")
        ch.session_checkpoint = True
        text = self._addendum(ch)
        assert "If a fact you need is missing" in text
        assert "tells you to check with the user" not in text
        assert op.ASK_ADDENDUM_SESSION.strip() in text and "asks the operator what to do next" in text

    def test_flag_off_it_is_unchanged(self):
        text = self._addendum(OperatorChannel.create(lambda q: "x"))
        assert op.ASK_ADDENDUM_LINE.strip() in text and "tells you to check with the user" in text
        assert op.ASK_ADDENDUM_SESSION.strip() not in text

    def test_the_line_is_the_base_plus_the_check_in_clause(self):
        assert op.ASK_ADDENDUM_LINE == op.ASK_ADDENDUM_BASE + op.ASK_ADDENDUM_CHECKIN

    def test_no_channel_no_ask_line(self):
        assert "ask_user" not in self._addendum(None)


@pytest.mark.parametrize("cls", [Supervisor, AsyncSupervisor])
def test_the_supervisor_marks_the_channel_it_creates(cls):
    kw = dict(model=router(), agents={"worker": ("w", ScriptedRunner())}, verbose=False,
              ask_user=lambda q: "x")
    assert cls(ask_when_done=True, **kw)._new_operator_channel().session_checkpoint is True
    assert cls(follow_up=True, **kw)._new_operator_channel().session_checkpoint is True
    assert cls(**kw)._new_operator_channel().session_checkpoint is False
