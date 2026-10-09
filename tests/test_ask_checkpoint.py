"""A task that says "when done, ask me" must reach ask_user, and the planner must see what each
registered agent can do.

A real run: ask_user=True, the task said to ask the operator what to do when finished, and the
helper wrote "What would you like me to do next?" into its answer instead of calling the tool
(its ask line covered only missing facts). The run then ended with nobody to answer. Also, the
planner saw only each registered agent's description, never its tools."""

import asyncio
import json

from agentx_dev import AsyncSupervisor, StandardTool, Supervisor, ToolRegistry
from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy
from agentx_dev.Supervisor import _render_agent_catalog
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step


class TestHelperAskLine:
    def test_it_covers_a_task_that_says_to_check_with_the_user(self):
        line = op.ASK_ADDENDUM_LINE
        assert "tells you to check with the user" in line
        assert "instead of writing the question in your answer" in line
        assert "act on the reply" in line

    def test_the_missing_fact_rule_is_kept(self):
        assert "cannot find it" in op.ASK_ADDENDUM_LINE and "Do not guess" in op.ASK_ADDENDUM_LINE


class TestPlannerCheckpointNote:
    def _planner_prompt(self, model):
        return model.planner_prompts()[0]

    def _run(self, cls, ask_user):
        model = router(plans=[plan_json(step("s1", "worker", "research then ask"))])
        sup = cls(model=model, agents={"worker": ("worker", ScriptedRunner())}, verbose=False,
                  ask_user=ask_user)
        if cls is AsyncSupervisor:
            asyncio.run(sup.run("research, then ask me what to do next"))
        else:
            sup.run("research, then ask me what to do next")
        return model

    def test_sync_planner_is_told_to_hand_the_check_in_to_the_step(self):
        model = self._run(Supervisor, lambda q: "go on")
        text = self._planner_prompt(model)
        assert "call ask_user at that point" in text
        assert "step whose only job is to ask" in text

    def test_async_planner_is_told_the_same(self):
        model = self._run(AsyncSupervisor, lambda q: "go on")
        assert "call ask_user at that point" in self._planner_prompt(model)

    def test_without_ask_user_the_note_is_absent(self):
        model = self._run(Supervisor, False)
        assert "call ask_user at that point" not in self._planner_prompt(model)


class TestSpawnLine:
    def _say(self, capsys, operator):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(),
                             operator=operator, verbose=True)
        built = policy.build(AgentSpec(name="scout", instructions="find", tools=("web",)))
        policy._announce(built)
        return capsys.readouterr().out, built

    def test_the_verbose_line_shows_that_the_helper_can_ask(self, capsys):
        out, _ = self._say(capsys, OperatorChannel.create(lambda q: "x"))
        assert "tools=['web']" in out and "ask_user" in out

    def test_without_a_channel_it_does_not(self, capsys):
        out, _ = self._say(capsys, None)
        assert "ask_user" not in out

    def test_result_spawned_keeps_its_shape(self, capsys):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(),
                             operator=OperatorChannel.create(lambda q: "x"))
        built = policy.build(AgentSpec(name="scout", instructions="find", tools=("web",)))
        assert built.granted == ["web"]


def _runner_with(*names):
    tools = [StandardTool(func=lambda q: "x", name=n, description="d") for n in names]
    return type("R", (), {"registry": ToolRegistry(tools)})()


class TestCatalogListsTools:
    def test_a_registered_agents_tools_are_listed_for_the_planner(self):
        text = _render_agent_catalog({"file_agent": ("File management", _runner_with("read_path", "write_file"))})
        assert "- file_agent: File management" in text
        assert "tools: read_path, write_file" in text

    def test_framework_added_tools_are_not_listed(self):
        text = _render_agent_catalog({"a": ("d", _runner_with("write_file", "ask_user", "delegate"))})
        assert "write_file" in text and "ask_user" not in text and "delegate" not in text

    def test_a_long_list_is_cut(self):
        names = [f"t{i}" for i in range(30)]
        text = _render_agent_catalog({"a": ("d", _runner_with(*names))})
        assert "t0" in text and "t29" not in text and "more" in text

    def test_a_runner_without_a_registry_adds_nothing(self):
        text = _render_agent_catalog({"a": ("d", ScriptedRunner())})
        assert text == "- a: d"

    def test_a_runner_with_no_tools_adds_nothing(self):
        text = _render_agent_catalog({"a": ("d", _runner_with())})
        assert text == "- a: d"
