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

    def test_the_spinner_says_you_can_type_and_shows_the_line(self):
        import io
        from rich.console import Console
        from agentx_dev.cli.ui import Progress
        console = Console(file=io.StringIO(), force_terminal=True, width=120, color_system=None)
        progress = Progress(console)
        progress.typeahead = TypeAhead(lambda text: None, keys=FakeKeys())
        progress.start("Thinking")
        try:
            with console.capture() as shown:
                console.print(progress)
            assert "type to add to the task" in shown.get()
            progress.typeahead.feed("use gold")
            with console.capture() as shown:
                console.print(progress)
            assert "❯ use gold" in shown.get() and "Enter sends it" in shown.get()
        finally:
            progress.stop()

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
        assert "also check the footer" in all_seen_by(model)
        assert "also check the footer  ✓ read by the agent" in text
        assert "Queued" not in text and "Sent to the team" not in text
        assert app._waiting_shown() == []

    def test_a_command_typed_during_a_turn_runs_after_it(self, tmp_path):
        def script(messages):
            app.typed_while_working("/team-models clear")          # it changes a setting: it waits
            return make_final("done")
        app, out, cwd, home = make_app(tmp_path, Lines("hello", "/exit"), MockModel(script=script))
        app.start()
        text = out.getvalue()
        assert "/team-models clear  ✓ running it now" in text and "Kept:" not in text
        assert app._waiting_shown() == []

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

    def test_a_message_waits_under_the_spinner_until_it_is_read(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        app.typed_while_working("use gold buttons")
        app.typed_while_working("/model claude-haiku-5-5")
        assert app._waiting_shown() == [("use gold buttons", "waiting for the agent's next step"),
                                        ("/model claude-haiku-5-5", "runs when the agent is done")]
        assert out.getvalue() == ""                                    # nothing printed until it is read
        assert app._take_typed() == ["use gold buttons"]
        app._read_by_agent("use gold buttons")
        assert app._waiting_shown() == [("/model claude-haiku-5-5", "runs when the agent is done")]
        assert out.getvalue().count("use gold buttons  ✓ read by the agent") == 1

    def test_a_team_note_is_marked_read_by_the_first_helper_only(self, tmp_path):
        import agentx_dev.cli.team as team
        from agentx_dev.cli.team import _Job, StepStatus, _running_status
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        job = _Job()
        team._active_job = job
        try:
            app.typed_while_working("add a wishlist")
            assert app._waiting_shown() == [("add a wishlist", "waiting for a helper's next step")]
            for helper in ("bag-flow-tracer", "account-ui-builder"):
                token = _running_status.set(StepStatus(1, helper))
                try:
                    assert job.new_notes() == ["add a wishlist"]
                finally:
                    _running_status.reset(token)
        finally:
            team._active_job = None
        text = out.getvalue()
        assert text.count("add a wishlist  ✓ read by bag-flow-tracer") == 1 and "account-ui-builder" not in text
        assert app._waiting_shown() == []

    def test_a_team_note_no_helper_read_goes_to_the_agent_when_the_job_ends(self, tmp_path):
        import agentx_dev.cli.team as team
        from agentx_dev.cli.team import _Job
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        team._active_job = _Job()
        try:
            app.typed_while_working("no delivery as a gift")
        finally:
            team._active_job = None
        app._team_notes_to_agent()
        assert "no delivery as a gift  ✓ read by the agent" in out.getvalue()
        assert app._waiting_shown() == []

    def test_the_spinner_draws_the_waiting_messages(self):
        import io
        from rich.console import Console
        from agentx_dev.cli.ui import Progress
        console = Console(file=io.StringIO(), force_terminal=True, width=120, color_system=None)
        progress = Progress(console)
        progress.waiting = lambda: [("add a wishlist", "waiting for a helper's next step")]
        progress.start("Team is working")
        try:
            with console.capture() as shown:
                console.print(progress)
            assert "◌ add a wishlist  · waiting for a helper's next step" in shown.get()
        finally:
            progress.stop()

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


class TestStepOrder:
    def test_steps_ready_at_once_start_in_plan_order(self, tmp_path):
        import time as clock
        started = []

        def work(messages):
            text = str(messages)
            for name in ("one", "two", "three", "four"):
                if f"do {name}" in text and name not in started:
                    started.append(name)
            clock.sleep(0.2)
            return make_final("done")
        plan = [step("s1", "coder", "do one"), step("s2", "coder", "do two", deps=["s1"]),
                step("s3", "coder", "do three"), step("s4", "explorer", "do four", deps=["s2", "s3"])]
        result, seen = team_job(tmp_path, plan, work)
        dispatched = [data["query"] for kind, data in seen if kind == "dispatch"]
        assert dispatched == ["do one", "do two", "do three", "do four"]

    def test_a_step_does_not_wait_for_an_earlier_one_that_is_not_ready(self, tmp_path):
        def work(messages):
            return make_final("done")
        plan = [step("s1", "coder", "do one", deps=["s2"]), step("s2", "coder", "do two")]
        result, seen = team_job(tmp_path, plan, work)
        assert [data["query"] for kind, data in seen if kind == "dispatch"] == ["do two", "do one"]


def test_the_spinner_shows_minutes_after_a_minute():
    import io
    from rich.console import Console
    from agentx_dev.cli.ui import Progress
    from agentx_dev.cli.ui_text import short_time
    assert [short_time(s) for s in (45, 60, 683, 3725)] == ["45s", "1m 00s", "11m 23s", "1h 02m"]
    console = Console(file=io.StringIO(), force_terminal=True, width=120, color_system=None)
    progress = Progress(console)
    progress.start("Team is working")
    try:
        progress._since -= 683
        with console.capture() as shown:
            console.print(progress)
        assert "Team is working... 11m 23s" in shown.get()
    finally:
        progress.stop()


class TestAToolTheHelperDoesNotHave:
    def test_the_helper_is_told_and_carries_on(self, tmp_path):
        calls = []

        def work(messages):
            calls.append(str(messages))
            if len(calls) == 1:                      # the runner cannot edit: it tries anyway
                return make_react_response("edit_file", {"path": "a.txt", "old": "x", "new": "y"})
            return make_final("checks ran; a.txt needs x changed to y")
        result, seen = team_job(tmp_path, [step("s1", "runner", "run the checks and fix what fails")], work)
        assert "is not a registered tool" in calls[1]
        done = [data for kind, data in seen if kind == "result"][0]
        assert done["ok"] and "a.txt needs x changed to y" in str(done.get("text"))

    def test_a_helper_with_no_answer_is_a_failed_step_not_done(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from agentx_dev import AgentRunner
        from agentx_dev.cli.team import NO_REPORT
        ended = SimpleNamespace(content="(agent emitted an unrecognized action and no answer text - try rephrasing "
                                        "or re-run)", outcome="done", tool_calls=[])
        monkeypatch.setattr(AgentRunner, "Initialize", lambda self, *args, **kwargs: ended)
        result, seen = team_job(tmp_path, [step("s1", "runner", "run the checks")], lambda messages: "")
        done = [data for kind, data in seen if kind == "result"][-1]
        assert not done["ok"]
        assert NO_REPORT.split(":")[0] in str(done.get("text")) + result
        assert "unrecognized action" not in str(done.get("text"))

    def test_the_planner_is_told_to_match_tools_to_the_step(self, tmp_path):
        model = router(plans=[plan_json(step("s1", "explorer", "look"))], synth="Done.",
                       agent=lambda messages: make_final("looked"))
        run_team_job("the job", model=model, tools=build_tools(tmp_path, [], Gate(mode="auto", cwd=tmp_path)),
                     addendum="", notify=lambda kind, data: None)
        assert "Give a step only to a helper that has every tool the step needs" in model.planner_prompts()[0]
