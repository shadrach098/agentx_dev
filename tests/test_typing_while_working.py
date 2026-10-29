"""Typing while the agent works: the reader, the runner hook, where each typed line goes, team notes and /skip."""

import asyncio
import threading

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, StandardTool
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.team import USER_NOTES, run_team_job, skip_team_step, team_note
from agentx_dev.cli.tools import build_tools
from agentx_dev.cli.typeahead import TypeAhead
from agentx_dev.Runner.AgentRun import INTERJECTION_PREFIX
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import plan_json, router, step
from tests.test_cli_app import Lines, all_seen_by, make_app

STEADY = StandardTool(func=lambda x: f"ok {x}", name="steady", description="works")


# -- the reader ------------------------------------------------------------------------------------------------

class FakeKeys:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.entered = 0
        self.left = 0

    def enter(self):
        self.entered += 1

    def leave(self):
        self.left += 1

    def read(self):
        return self.chunks.pop(0) if self.chunks else ""


class TestTypeAhead:
    def test_typing_editing_and_enter(self):
        sent = []
        typed = TypeAhead(sent.append, keys=FakeKeys())
        typed.feed("helo")
        typed.feed("\x08lo there")
        assert typed.line() == "hello there" and sent == []
        typed.feed("\r")
        assert sent == ["hello there"] and typed.line() == ""

    def test_escape_clears_and_arrow_keys_are_ignored(self):
        sent = []
        typed = TypeAhead(sent.append, keys=FakeKeys())
        typed.feed("abc\x1b[Dd")                      # left arrow: ignored
        assert typed.line() == "abcd"
        typed.feed("\x1b")
        assert typed.line() == ""
        typed.feed("\r")
        assert sent == []                              # an empty line is not sent

    def test_keys_are_read_only_while_resumed_and_not_held(self):
        got = threading.Event()
        sent = []
        keys = FakeKeys(["hi\r"])
        typed = TypeAhead(lambda text: (sent.append(text), got.set()), keys=keys)
        with typed.hold():
            typed.resume()
            assert keys.entered == 0                   # held: the prompt gets the keys
        assert keys.entered == 1
        assert got.wait(5) and sent == ["hi"]
        typed.pause()
        assert keys.left >= 1
        typed.close()

    def test_unavailable_without_a_terminal(self):
        assert TypeAhead(lambda text: None).available is False        # pytest's stdin is not a terminal


# -- the runner hook -----------------------------------------------------------------------------------------

def queue_once(text):
    pending = [text]

    def take():
        out = list(pending)
        pending.clear()
        return out
    return take


class TestInterjections:
    def test_a_message_reaches_the_sync_run_at_its_next_step(self):
        model = MockModel(script=[make_react_response("steady", "a"), make_final("done")])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[STEADY], verbose=False)
        calls = []

        def take():
            calls.append(1)
            return ["also b"] if len(calls) == 2 else []
        runner.interjections = take
        events = list(runner.stream("go"))
        assert {"type": "interjection", "content": "also b"} in events
        history = events[-1]["completion"].history
        assert any(m.get("content") == INTERJECTION_PREFIX + "also b" for m in history)
        assert "also b" in str(model.calls[1])

    def test_a_message_reaches_the_async_run(self):
        model = MockModel(script=[make_react_response("steady", "a"), make_final("done")])
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[STEADY], verbose=False)
        runner.interjections = queue_once("also b")
        asyncio.run(runner.ainvoke("go"))
        assert "also b" in str(model.calls[0])

    def test_a_broken_source_is_ignored(self):
        runner = AgentRunner(model=MockModel(script=[make_final("done")]), agent=AgentType.ReAct, tools=[STEADY],
                             verbose=False)

        def broken():
            raise RuntimeError("no")
        runner.interjections = broken
        assert runner.invoke("go").content == "done"


# -- where a typed line goes in the app ----------------------------------------------------------------------

class TestTheApp:
    def test_a_message_typed_during_a_turn_reaches_the_agent(self, tmp_path):
        replies = []

        def script(messages):
            replies.append(1)
            if len(replies) == 1:
                app.typed_while_working("also check the footer")
                return make_react_response("list_directory", {"path": "."})
            return make_final("done")
        model = MockModel(script=script)
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), model)
        app.start()
        text = out.getvalue()
        assert "also check the footer" in all_seen_by(model) and "Queued" in text
        assert "the agent read your message" in text

    def test_a_command_typed_during_a_turn_runs_after_it(self, tmp_path):
        def script(messages):
            app.typed_while_working("/settings")
            return make_final("done")
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=script))
        app.start()
        text = out.getvalue()
        assert "Kept: /settings runs when the agent is done." in text
        assert "Running what you typed while the agent worked: /settings" in text

    def test_a_message_the_agent_did_not_read_is_the_next_message(self, tmp_path):
        seen = []

        def script(messages):
            seen.append(str(messages))
            if len(seen) == 1:
                app.typed_while_working("and the header")
            return make_final("done")
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=script))
        app.start()
        assert len(seen) == 2 and "and the header" in seen[1]

    def test_skip_without_a_team_job(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        app.typed_while_working("/skip 2")
        assert "No team job is running." in out.getvalue()
        app._team_plan = [{"agent": "explorer"}]
        app.typed_while_working("/skip 5")
        assert "No step 5: the plan has 1 steps." in out.getvalue()


# -- team jobs ---------------------------------------------------------------------------------------------------

def team_job(tmp_path, plan, work):
    seen = []
    model = router(plans=[plan_json(*plan)], synth="All done.", agent=work)
    tools = build_tools(tmp_path, [], Gate(mode="auto", cwd=tmp_path))
    result = run_team_job("the job", model=model, tools=tools, addendum="",
                          notify=lambda kind, data: seen.append((kind, data)))
    return result, seen


class TestTeam:
    def test_a_note_reaches_the_running_helper_and_the_main_agent(self, tmp_path):
        calls = []

        def work(messages):
            calls.append(str(messages))
            if len(calls) == 1:
                assert team_note("use gold buttons")
                return make_react_response("list_directory", {"path": "."})
            return make_final("looked")
        result, _ = team_job(tmp_path, [step("s1", "explorer", "look")], work)
        assert "use gold buttons" in calls[1]                          # read at the helper's next step
        assert USER_NOTES.split("(")[0] in result and "- use gold buttons" in result
        assert team_note("too late") is False                          # the job is over

    def test_skip_drops_a_step_that_has_not_started(self, tmp_path):
        def work(messages):
            assert skip_team_step("2")
            return make_final("looked")
        result, seen = team_job(tmp_path, [step("s1", "explorer", "look"), step("s2", "coder", "change")], work)
        results = [data for kind, data in seen if kind == "result"]
        assert [r["agent"] for r in results] == ["explorer", "coder"]
        assert results[0]["ok"] and results[1]["skipped"]
        assert "skipped" in result
