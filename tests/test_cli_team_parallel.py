"""Team jobs: read-only steps side by side, a step that changes things alone, the re-read guard, each step's token
share, and the hand-over that keeps every prompt and screen update on the thread that started the job."""

import threading
import time

import pytest

from agentx_dev.cli import team
from agentx_dev.cli.approval import Answer, Gate
from agentx_dev.cli.team import MIN_STEP_TOKENS, on_main, run_team_job, step_token_share
from agentx_dev.cli.tools import build_tools
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import plan_json, router, step
from tests.test_cli_app import Lines, make_app
from tests.test_cli_team_app import team_model


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    return {t.name: t for t in build_tools(tmp_path, [], Gate(mode="auto", cwd=tmp_path))}


@pytest.fixture(autouse=True)
def no_step_left_behind():
    yield
    team._local.step = None


def in_step(tool, job=None, helper="explorer"):
    current = team._Step(helper, job or team._Job())
    return team._step_tool(tool, current), current


class TestRereadGuard:
    def test_a_third_whole_read_of_an_unchanged_file_is_answered_instead(self, tools):
        read, _ = in_step(tools["read_path"])
        assert "x = 1" in read.func(path="a.py")
        assert "x = 1" in read.func(path="./a.py")                 # the same file, written another way
        assert read.func(path="a.py").startswith("Not read again: a.py was already read in full 2 times")
        assert "x = 1" in read.func(path="a.py", offset=1, limit=1)   # a part of it is still read

    def test_an_edit_or_a_command_lets_the_file_be_read_again(self, tools):
        current = team._Step("coder", team._Job())
        read, edit, python = (team._step_tool(tools[name], current) for name in ("read_path", "edit_file", "run_python"))
        read.func(path="a.py")
        read.func(path="a.py")
        edit.func(path="a.py", find="x = 1", replace="x = 2")
        assert "x = 2" in read.func(path="a.py")
        read.func(path="a.py")
        python.func(code="print(1)")                                 # a command may have changed any file
        assert "x = 2" in read.func(path="a.py")

    def test_a_failed_read_does_not_count(self, tools):
        read, current = in_step(tools["read_path"])
        for _ in range(3):
            read.func(path="missing.py")
        assert current.reads == {}

    def test_the_wrapped_tool_keeps_its_name_schema_and_mcp_marks(self, tools):
        tool = tools["grep"]
        tool.mcp_server = "srv"
        wrapped, _ = in_step(tool)
        assert (wrapped.name, wrapped.args_schema, wrapped.mcp_server) == ("grep", tool.args_schema, "srv")
        assert wrapped.cacheable is False


class TestStepShare:
    def test_the_share_is_the_limit_and_at_least_the_minimum(self):
        assert step_token_share(0) == 0
        assert step_token_share(50_000) == MIN_STEP_TOKENS == 100_000
        assert step_token_share(300_000) == 300_000

    def test_replies_count_against_the_step_and_a_spent_step_is_told_to_report(self, tools):
        read, current = in_step(tools["read_path"], team._Job(step_tokens=20))
        model = MockModel()
        team._local.step = current
        model._record_usage_counts(input_tokens=10, output_tokens=5)
        team.note_usage(model)
        team._local.step = None
        assert current.tokens == 15 and "x = 1" in read.func(path="a.py")
        team._local.step = current
        model._record_usage_counts(input_tokens=10, output_tokens=5)
        team.note_usage(model)
        team._local.step = None
        assert read.func(path="a.py").startswith(
            "Not run: this step has used its share of the token limit (30 of 20 tokens)")

    def test_outside_a_step_nothing_is_counted(self):
        team.note_usage(MockModel())

    def test_a_helper_whose_job_is_over_stops(self, tools):
        job = team._Job()
        read, current = in_step(tools["read_path"], job)
        job.stopped = True
        with pytest.raises(team.JobStopped):
            read.func(path="a.py")
        team._local.step = current
        with pytest.raises(KeyboardInterrupt):
            team.note_usage(MockModel())


class TestOrder:
    def test_a_plan_without_dependencies_keeps_its_order_but_reads_go_side_by_side(self):
        plan = [step("a", "explorer"), step("b", "explorer@gpt-5.5"), step("c", "coder"), step("d", "explorer"),
                step("e", "runner")]
        deps = {s["id"]: s["depends_on"] for s in team._ordered(plan)}
        assert deps == {"a": [], "b": [], "c": ["a", "b"], "d": ["a", "b", "c"], "e": ["a", "b", "c", "d"]}
        assert "depends_on" not in plan[0]                          # the planner's own plan is not changed

    def test_a_plan_with_its_own_dependencies_is_left_alone(self):
        plan = [step("a", "explorer"), step("b", "coder", deps=["a"])]
        assert team._ordered(plan) is plan


def job(tmp_path, plan, work, gate=None, **kw):
    seen = []
    model = router(plans=[plan_json(*plan)], synth="All done.", agent=work)
    tools = build_tools(tmp_path, [], gate or Gate(mode="auto", cwd=tmp_path))
    result = run_team_job("the job", model=model, tools=tools, addendum="",
                          notify=lambda kind, data: seen.append((kind, data, threading.current_thread())), **kw)
    return result, seen


class TestSideBySide:
    def test_read_only_steps_run_at_the_same_time(self, tmp_path):
        together = threading.Barrier(3, timeout=10)

        def work(messages):
            together.wait()                     # each explorer waits here until all three are running
            return make_final("found")

        result, seen = job(tmp_path, [step("a", "explorer", "look A"), step("b", "explorer", "look B"),
                                      step("c", "explorer", "look C")], work)
        assert result.startswith("Team job finished (outcome: done).") and result.count(" - done: ") == 3
        assert [kind for kind, _, _ in seen].count("dispatch") == 3
        assert all(thread is threading.main_thread() for _, _, thread in seen)    # the screen is drawn from here

    def test_a_step_that_changes_things_runs_alone(self, tmp_path):
        spans = {}
        lock = threading.Lock()

        def work(messages):
            text = str(messages)
            name = next(q for q in ("look C", "edit B", "look A") if q in text)
            start = time.monotonic()
            time.sleep(0.3)
            with lock:
                spans[name] = (start, time.monotonic())
            return make_final("ok")

        result, _ = job(tmp_path, [step("a", "explorer", "look A"), step("b", "coder", "edit B"),
                                   step("c", "explorer", "look C", deps=["a"])], work)
        assert result.count(" - done: ") == 3
        coder = spans["edit B"]
        for name in ("look A", "look C"):
            other = spans[name]
            assert coder[1] <= other[0] or other[1] <= coder[0], f"the coder ran beside {name}"

    def test_an_approval_from_a_helper_is_asked_on_the_calling_thread(self, tmp_path):
        threads = []

        def asker(request):
            threads.append(threading.current_thread())
            return Answer("n", "no")

        gate = Gate(cwd=tmp_path)
        gate.ask = lambda request: on_main(asker, request)
        replies = [make_react_response("write_file", {"path": "b.txt", "content": "x", "if_exists": "refuse"}),
                   make_final("declined, stopped")]
        result, _ = job(tmp_path, [step("a", "coder", "write it")], lambda messages: replies.pop(0), gate=gate)
        assert threads == [threading.main_thread()] and not (tmp_path / "b.txt").exists()

    def test_a_question_from_the_team_is_asked_on_the_calling_thread(self, tmp_path):
        import json
        from tests.subagent_helpers import PLANNER_MARK
        threads = []
        plans = [json.dumps({"plan": [], "ask": [{"question": "Which folder?", "why": "scope"}]}),
                 plan_json(step("a", "explorer", "look"))]

        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                return plans.pop(0)
            return make_final("done") if "Team role:" in str(messages[0]["content"]) else "All done."

        def ask(question):
            threads.append(threading.current_thread())
            return "docs"

        run_team_job("the job", model=MockModel(script=script), tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)),
                     addendum="", notify=lambda kind, data: None, ask_user=ask)
        assert threads == [threading.main_thread()]

    def test_ctrl_c_stops_the_job_without_waiting_for_the_helpers(self, tmp_path):
        release = threading.Event()

        def work(messages):
            release.wait(10)
            return make_final("late")

        def notify(kind, data):
            if kind == "dispatch":
                raise KeyboardInterrupt

        started = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                run_team_job("the job", model=router(plans=[plan_json(step("a", "explorer", "look"))], agent=work),
                             tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)), addendum="", notify=notify)
            assert time.monotonic() - started < 4
        finally:
            release.set()
        assert team._active is None


class TestOnMain:
    def test_outside_a_job_it_is_a_plain_call(self):
        assert on_main(lambda a, b: a + b, 2, 3) == 5
        assert on_main(lambda: 1, wait=False) is None

    def test_a_helper_thread_of_a_finished_job_is_stopped(self):
        stopped = team._Job()
        stopped.stopped = True
        failures = []

        def worker():
            team._local.step = team._Step("explorer", stopped)
            try:
                on_main(print, "never")
            except team.JobStopped:
                failures.append("stopped")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert failures == ["stopped"]


class TestTheAppShowsHelpersLikeTheChat:
    def test_a_helpers_looking_calls_are_one_line(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")

        def helper(messages, n):
            calls = [("read_path", {"path": "a.txt"}), ("grep", {"pattern": "hello"}), ("read_path", {"path": "a.txt"})]
            return make_react_response(*calls[n - 1]) if n <= len(calls) else make_final("helper report")

        model = team_model([step("s1", "explorer", "look around")], helper=helper)
        app, out, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), model, mode="auto", width=300)
        assert app.start() == 0
        text = out.getvalue()
        assert "✓ explorer · look around  Done (3 tool uses · " in text
        assert "read_path" not in text and "grep" not in text                 # the calls are not printed one by one
        before = len(out.getvalue())
        app.show_team_log()
        log = out.getvalue()[before:]
        assert "explorer · look around  (3 tool uses · " in log and "read_path  a.txt" in log
        assert log.count("read_path") == 2 and log.count("grep") == 1 and log.count("⎿") == 3


class TestWrappingUp:
    """After a no at the token-limit question, no new team step starts and no skill is read: the job reports with
    what it has, so the turn ends normally instead of running past the wrap-up allowance."""

    def test_no_step_starts_once_the_user_said_to_wrap_up(self, tmp_path):
        state = {"closed": False, "helper_calls": 0}

        def work(messages):
            state["helper_calls"] += 1
            state["closed"] = True                      # the user answers no while the first step runs
            return make_final("first findings")

        def notify(kind, data):
            pass

        model = router(plans=[plan_json(step("a", "runner", "check"), step("b", "explorer", "look", deps=["a"]),
                                        step("c", "explorer", "look more"))], synth="Partial report.", agent=work)
        result = run_team_job("the job", model=model, tools=build_tools(tmp_path, [], Gate(cwd=tmp_path)),
                              addendum="", notify=notify, closed=lambda: state["closed"])
        assert state["helper_calls"] == 1 and "Partial report." in result
        assert "skipped" in result and result.count(" - done: ") == 1

    def test_a_skill_is_not_read_while_wrapping_up(self, tmp_path):
        from agentx_dev.Skills import Skills
        from agentx_dev.Skills.Attach import attach_skills
        seen = []
        closed = {"text": None}
        skills = Skills(tmp_path / "skills", observer=lambda name, args: seen.append(args["name"]),
                        guard=lambda: closed["text"])
        tools, _ = attach_skills(skills, build_tools(tmp_path, [], Gate(cwd=tmp_path)), "")
        use_skill = next(t for t in tools if t.name == "use_skill")
        assert "list_directory" in use_skill.func("exploring-a-project")
        closed["text"] = "Not run: wrap up now."
        assert use_skill.func("exploring-a-project") == "Not run: wrap up now." and seen == ["exploring-a-project"]

    def test_a_no_during_a_team_job_ends_the_turn_normally(self, tmp_path):
        model = team_model([step("s1", "runner", "check"), step("s2", "explorer", "look", deps=["s1"])])
        app, out, _, _ = make_app(tmp_path, Lines("do the big job", "n", "/exit"), model, mode="auto", width=300,
                                  token_limit=20)
        assert app.start() == 0
        text = out.getvalue()
        assert "Wrapping up" in text and "Stopped:" not in text
        assert model.state["helper_calls"] == 0                   # neither step started
        assert "not started" in text and "The team finished; here is the summary." in text


class TestCachedInputCountsAsATenth:
    """Input the provider serves from its cache costs about a tenth, and most of a helper's input is that: the limit
    and each step's share count it as a tenth, so a step is not cut off after a dozen calls."""

    def test_the_count(self):
        from agentx_dev.cli.team import counted_tokens
        assert counted_tokens(200_000, 6_700, 160_000) == 40_000 + 16_000 + 6_700
        assert counted_tokens(100, 5) == 105 and counted_tokens(100, 5, 500) == 15      # never more cached than input

    def test_a_steps_share_counts_cached_input_as_a_tenth_and_its_line_shows_the_cache(self):
        current = team._Step("explorer", team._Job(step_tokens=1_000))
        model = MockModel()
        team._local.step = current
        model._record_usage_counts(input_tokens=5_000, output_tokens=100, cache_read_tokens=4_500)
        team.note_usage(model)
        team._local.step = None
        assert current.tokens == 500 + 450 + 100                     # under its share of 1,000
        snapshot = current.status.snapshot()
        assert (snapshot["tokens"], snapshot["cached"]) == (5_100, 4_500)
        from agentx_dev.cli.ui import step_stats
        assert "5.1k tokens, 88% cached" in step_stats(snapshot)

    def test_the_limit_question_counts_the_cache_as_a_tenth_and_says_so(self, tmp_path):
        answers = Lines("y")
        app, out, _, _ = make_app(tmp_path, Lines(), MockModel(), token_limit=100, read_short=answers)
        model = MockModel()
        app._track_usage_model("m", model, "chat")
        with app._limit_lock:
            app._turn_start, app._turn_start_sent, app._turn_allowed = 0, 0, 100
        model.on_usage = None
        model._record_usage_counts(input_tokens=500, output_tokens=4, cache_read_tokens=450)   # counts 99
        app._check_token_limit(model)
        assert not answers.prompts
        model._record_usage_counts(input_tokens=10, output_tokens=1)                            # counts 110
        app._check_token_limit(model)
        assert answers.prompts and answers.prompts[0].startswith(
            "This message has used 110 tokens (515 sent and received; input from the provider's cache counts as a "
            "tenth")


def test_the_audit_skill_is_open_to_the_helpers(tmp_path):
    from agentx_dev.Skills import Skills
    helpers = team.build_helpers(MockModel(), build_tools(tmp_path, [], Gate(cwd=tmp_path)), "",
                                 skills=Skills(tmp_path / "skills"))
    settings = helpers["explorer"][1]._settings[1]
    from agentx_dev import AgentRunner
    runner = AgentRunner(**settings)
    assert "auditing-a-project" in runner.system_addendum
