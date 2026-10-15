"""A research helper built with no tools was invisible until the answer came back as nonsense.

A real run: the planner defined a "web_researcher" with an empty tools list, the helper could not
look anything up, and a log line claimed it had "the same tools as the registered specialist"
(both just had the framework's ask_user). These tests pin the planner reminder, the log line and
the false-alarm fix."""

import json

from agentx_dev import Supervisor
from agentx_dev.Operator import OperatorChannel
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy, parse_agent_spec, spawn_instruction
from tests.subagent_helpers import ScriptedRunner, new_agent_step, plan_json, router


class TestLegacyKeyAlias:
    def test_capabilities_is_read_when_tools_is_absent(self):
        spec = parse_agent_spec({"name": "a", "instructions": "x", "capabilities": ["web"]})
        assert spec.tools == ("web",)

    def test_tools_wins_when_both_are_given(self):
        spec = parse_agent_spec({"name": "a", "instructions": "x", "tools": ["files"], "capabilities": ["web"]})
        assert spec.tools == ("files",)

    def test_an_explicit_empty_tools_list_stays_empty(self):
        spec = parse_agent_spec({"name": "a", "instructions": "x", "tools": [], "capabilities": ["web"]})
        assert spec.tools == ()


class TestPlannerReminder:
    def text(self):
        return spawn_instruction(SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router()))

    def test_the_planner_is_told_to_check_each_new_agent_has_the_tools_its_job_needs(self):
        text = self.text()
        assert "Before you reply" in text
        assert "website, a search" in text
        assert "empty" in text.split("Before you reply")[1]


class TestSpawnLine:
    def say(self, capsys, requested, operator=None):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(),
                             operator=operator, verbose=True)
        built = policy.build(AgentSpec(name="scout", instructions="find", tools=requested))
        policy._announce(built)
        return capsys.readouterr().out

    def test_a_helper_with_no_tools_is_called_out(self, capsys):
        out = self.say(capsys, ())
        assert "no tools" in out and "only reason" in out

    def test_a_helper_with_tools_is_not(self, capsys):
        assert "only reason" not in self.say(capsys, ("web",))

    def test_ask_user_alone_still_counts_as_no_tools(self, capsys):
        out = self.say(capsys, (), operator=OperatorChannel.create(lambda q: "x"))
        assert "only reason" in out and "ask_user" in out


class TestNoFalseTwin:
    def test_ask_user_is_not_a_shared_tool(self, capsys):
        file_agent = ScriptedRunner()
        plan = plan_json(new_agent_step("s1", "web_researcher", "Research things.", tools=(), query="go"))
        sup = Supervisor(model=router(plans=[plan]), agents={"file_agent": ("files", file_agent)},
                         verbose=True, ask_user=lambda q: "x",
                         spawn_config=SpawnConfig(enabled=True, capabilities={"web"}))
        # the registered specialist gets ask_user for the run, like every agent
        from types import SimpleNamespace
        file_agent.tools = [SimpleNamespace(name="ask_user")]
        sup.run("task")
        assert "has the same tools as" not in capsys.readouterr().out
