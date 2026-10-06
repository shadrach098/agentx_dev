"""The ask_user tool, attach_ask_user, helper wiring, and the public ask_human_tool."""

import asyncio

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, StandardTool
from agentx_dev import Operator as op
from agentx_dev.Operator import OperatorChannel, attach_ask_user, make_ask_tool
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import router


def runner(model=None):
    return AgentRunner(model=model or router(), agent=AgentType.ReAct, tools=[], verbose=False)


def arunner(model=None):
    return AsyncAgentRunner(model=model or router(), agent=AgentType.ReAct, tools=[], verbose=False)


class TestAttach:
    def test_the_tool_is_attached_for_the_block_and_removed_after(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: "Notion")
        with attach_ask_user({"worker": r}, ch):
            assert r.registry.has("ask_user")
            assert r.registry.dispatch("ask_user", {"question": "Which?"}) == "[operator] Notion"
        assert not r.registry.has("ask_user")

    def test_the_tool_is_removed_even_if_the_block_raises(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: "x")
        with pytest.raises(RuntimeError):
            with attach_ask_user({"worker": r}, ch):
                raise RuntimeError("boom")
        assert not r.registry.has("ask_user")

    def test_no_channel_attaches_nothing(self):
        r = runner()
        with attach_ask_user({"worker": r}, None):
            assert not r.registry.has("ask_user")

    def test_a_runner_with_its_own_ask_user_tool_is_left_alone(self):
        mine = StandardTool(func=lambda question: "mine", name="ask_user", description="my own")
        r = AgentRunner(model=router(), agent=AgentType.ReAct, tools=[mine], verbose=False)
        ch = OperatorChannel.create(lambda q: "framework")
        with attach_ask_user({"worker": r}, ch):
            assert r.registry.dispatch("ask_user", {"question": "q"}) == "mine"
        assert r.registry.has("ask_user")                 # still the developer's tool

    def test_objects_that_cannot_take_a_tool_are_skipped(self):
        class Plain:
            pass
        ch = OperatorChannel.create(lambda q: "x")
        with attach_ask_user({"plain": Plain()}, ch):
            pass

    def test_the_tool_is_never_cached(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: "x")
        with attach_ask_user({"worker": r}, ch):
            tool = next(t for t in r.tools if t.name == "ask_user")
            assert tool.cacheable is False


class TestTheToolAnswers:
    def test_no_answer_is_a_plain_note_not_an_error(self):
        r = runner()
        ch = OperatorChannel.create(lambda q: None)
        with attach_ask_user({"worker": r}, ch):
            out = r.registry.dispatch("ask_user", {"question": "q"})
        assert out == op.NO_ANSWER_TEXT

    def test_the_source_is_the_agent_name_and_the_context_is_passed(self):
        r = runner()
        seen = []
        ch = OperatorChannel.create(lambda q: seen.append(q) or "a")
        with attach_ask_user({"researcher": r}, ch):
            r.registry.dispatch("ask_user", {"question": "Which?", "context": "step 1"})
        assert seen == ["Which?\n(context: step 1)"]
        assert ch.records[0]["source"] == "researcher"

    def test_a_model_driven_agent_asks_and_uses_the_answer(self):
        turns = []

        def script(messages):
            turns.append(messages)
            if len(turns) == 1:
                return make_react_response("ask_user", {"question": "Which competitors?"})
            return make_final("compared " + str(messages))
        r = runner(MockModel(script=script))
        ch = OperatorChannel.create(lambda q: "Notion, Obsidian, Coda")
        with attach_ask_user({"worker": r}, ch):
            result = r.invoke("Compare our competitors")
        assert "Notion, Obsidian, Coda" in result.content
        assert "[operator] Notion, Obsidian, Coda" in str(turns[1])

    def test_an_async_runner_gets_an_async_tool(self):
        r = arunner()
        ch = OperatorChannel.create(lambda q: "Coda", is_async=True)
        with attach_ask_user({"worker": r}, ch):
            out = asyncio.run(r.registry.adispatch("ask_user", {"question": "Which?"}))
        assert out == "[operator] Coda" and not r.registry.has("ask_user")


class TestHelpers:
    def spec(self):
        return AgentSpec(name="helper", instructions="You help.", tools=("web",), origin="plan")

    def test_a_helper_built_with_a_channel_gets_the_tool_and_the_addendum_line(self):
        ch = OperatorChannel.create(lambda q: "x")
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(), operator=ch)
        built = policy.build(self.spec())
        assert built.runner.registry.has("ask_user")
        assert op.ASK_ADDENDUM_LINE.strip() in built.runner.system_addendum

    def test_a_helper_built_without_a_channel_does_not(self):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router())
        built = policy.build(self.spec())
        assert not built.runner.registry.has("ask_user")
        assert "ask_user" not in built.runner.system_addendum

    def test_an_async_helper_gets_an_async_tool(self):
        ch = OperatorChannel.create(lambda q: "Coda", is_async=True)
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router(),
                             is_async=True, operator=ch)
        built = policy.build(self.spec())
        out = asyncio.run(built.runner.registry.adispatch("ask_user", {"question": "Which?"}))
        assert out == "[operator] Coda"

    def test_a_pool_tool_named_ask_user_wins_on_the_helper(self):
        clash = StandardTool(func=lambda question: "pool answer", name="ask_user", description="pool tool")
        ch = OperatorChannel.create(lambda q: "framework answer")
        cfg = SpawnConfig(enabled=True, tools=[clash], capabilities={"web"})
        policy = SpawnPolicy(cfg, router(), operator=ch)
        built = policy.build(AgentSpec(name="h", instructions="x", tools=("ask_user",), origin="plan"))
        reg = built.runner.registry
        assert reg.has("ask_user")
        assert reg.dispatch("ask_user", {"question": "q"}) == "pool answer"     # the developer's tool
        assert [t.name for t in built.runner.tools].count("ask_user") == 1
        assert ch.asked == 0                                                    # the framework tool was not added
        assert op.ASK_ADDENDUM_LINE.strip() in built.runner.system_addendum     # addendum behaviour unchanged


class TestAskHumanTool:
    def test_it_is_exported_and_asks_through_the_builtin_asker(self, monkeypatch):
        import agentx_dev
        assert agentx_dev.ask_human_tool is op.ask_human_tool
        seen = []
        monkeypatch.setattr(op, "builtin_asker",
                            lambda q, *, prefix="", timeout=None: seen.append((q, prefix, timeout)) or "Notion")
        tool = op.ask_human_tool(prompt_prefix="[triager]", ask_timeout=9.0)
        assert tool.name == "ask_human"
        assert tool.func(question="Which?", context="c") == "[operator] Notion"
        assert seen == [("Which?\n(context: c)", "[triager]", 9.0)]

    def test_no_channel_and_timeout_and_blank_give_the_no_answer_note(self, monkeypatch):
        tool = op.ask_human_tool()
        for outcome in (op.NoChannel("x"), op.AskTimeout("x")):
            def fail(q, *, prefix="", timeout=None, outcome=outcome):
                raise outcome
            monkeypatch.setattr(op, "builtin_asker", fail)
            assert tool.func(question="q") == op.NO_ANSWER_TEXT
        monkeypatch.setattr(op, "builtin_asker", lambda q, *, prefix="", timeout=None: "  ")
        assert tool.func(question="q") == op.NO_ANSWER_TEXT
