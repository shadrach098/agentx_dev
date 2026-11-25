"""The task tools in a real conversation, /tasks, and stopping everything when agentx exits.

Real processes: every wait is on a state with a deadline."""
import io
import sys
import time
from types import SimpleNamespace

from rich.console import Console

from agentx_dev.cli import app as app_module
from agentx_dev.cli import background
from agentx_dev.cli import task_tools
from agentx_dev.cli.approval import DECLINED, Answer
from agentx_dev.cli.commands import command_names, run_command
from agentx_dev.cli.tools import LEAN_TOOLS, build_tools, lean_tools, make_permissions
from tests.conftest import MockModel, make_final, make_react_response
from tests.test_cli_app import Lines, all_seen_by, make_app

PY = sys.executable.replace("\\", "/")
TASK_TOOLS = ("start_background", "background_output", "stop_background", "list_background", "send_to_terminal")


def until(check, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return check()


def sleeper(tmp_path, word="task-started"):
    f = tmp_path / f"sleeper-{word}.py"
    f.write_text(f"import time\nprint('{word}', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    return f'"{PY}" "{str(f).replace(chr(92), "/")}"'


def tool(app, name):
    return next(t for t in app.tools if t.name == name)


def call(name, args):
    return make_react_response(name, args)


def room():
    return SimpleNamespace(console=Console(file=io.StringIO()), read=Lines(), read_short=Lines(), select=None)


def test_the_agent_starts_reads_and_stops_a_task(tmp_path):
    cmd = sleeper(tmp_path)
    model = MockModel(script=[call("start_background", {"command": cmd}),
                              call("background_output", {"id": "t1"}),
                              call("stop_background", {"id": "t1"}),
                              make_final("done")])
    app, out, _, _ = make_app(tmp_path, Lines("run it", "/exit"), model, mode="auto")
    try:
        assert app.start() == 0
        seen = all_seen_by(model)
        assert f"Started t1 in the background: {cmd}" in seen
        assert "t1 (running for" in seen
        assert "task-started" in seen
        assert "Stopped t1 (" in seen
        assert app.tasks.get("t1").state == "stopped"
    finally:
        app.tasks.stop_all()


def test_chat_stop_leaves_tasks_running(tmp_path):
    cmd = sleeper(tmp_path)
    calls, states, holder = [], [], []

    def script(messages):
        calls.append(1)
        if len(calls) == 1:
            return call("start_background", {"command": cmd})
        raise KeyboardInterrupt                         # the user's Stop / Esc / Ctrl+C during the turn

    lines = Lines("run it", lambda: states.append(holder[0].tasks.get("t1").state), "/exit")
    app, out, _, _ = make_app(tmp_path, lines, MockModel(script=script), mode="auto")
    holder.append(app)
    try:
        assert app.start() == 0
        assert "Turn interrupted" in out.getvalue()
        assert states == ["running"]
    finally:
        app.tasks.stop_all()


def test_stopping_another_conversations_task_asks(tmp_path):
    a, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    b = app_module.device_app(a, room())
    assert b.tasks is a.tasks and b.gate.mode == "default"
    asked = []

    def ask(request):
        asked.append(request)
        return Answer("n")
    b.gate.ask = ask
    b.session = SimpleNamespace(session_id="conversation-b")
    try:
        other = a.tasks.start(sleeper(tmp_path, "other"), owner="conversation-a")
        result = tool(b, "stop_background").func(id=other.id)
        assert [r.tool for r in asked] == ["stop_other_task"]
        assert other.command in asked[0].subject
        assert result == DECLINED and other.state == "running"

        mine = a.tasks.start(sleeper(tmp_path, "mine"), owner="conversation-b")     # its own: no question
        assert tool(b, "stop_background").func(id=mine.id) == f"Stopped {mine.id} ({mine.command})."
        assert len(asked) == 1 and mine.state == "stopped"

        assert tool(b, "stop_background").func(id=mine.id) == f"{mine.id} had already ended (stopped)."
        assert tool(b, "stop_background").func(id="t99").startswith("ERROR: there is no task t99")
    finally:
        a.tasks.stop_all()


def test_a_terminal_is_stopped_only_after_asking(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    app.session = SimpleNamespace(session_id="conv")
    asked = []
    app.gate.ask = lambda request: (asked.append(request), Answer("n"))[1]
    try:
        term = app.tasks.open_terminal()
        assert tool(app, "stop_background").func(id=term.id) == DECLINED
        assert [r.tool for r in asked] == ["stop_other_task"] and term.state == "running"
    finally:
        app.tasks.stop_all()


def test_tasks_command(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="auto")
    try:
        run_command(app, "/tasks")
        assert "No background tasks." in out.getvalue()
        t1 = app.tasks.start(sleeper(tmp_path, "one"))
        t2 = app.tasks.start(sleeper(tmp_path, "two"))
        assert until(lambda: "one" in app.tasks.output(t1.id))
        run_command(app, "/tasks")
        listed = out.getvalue()
        assert "t1  running" in listed and "t2  running" in listed
        run_command(app, "/tasks show t1")
        assert "t1 (running for" in out.getvalue()
        run_command(app, "/tasks stop t1")
        assert "Stopped t1 (" in out.getvalue() and t1.state == "stopped" and t2.state == "running"
        run_command(app, "/tasks stop all")
        assert app.tasks.running_count() == 0
        run_command(app, "/tasks stop t9")
        assert "There is no task t9" in out.getvalue()
        run_command(app, "/tasks frobnicate")
        assert "Usage: /tasks" in out.getvalue()
        assert "/tasks" in command_names()
        run_command(app, "/help")
        assert "/tasks" in out.getvalue()
    finally:
        app.tasks.stop_all()


def test_lean_tools_include_task_tools(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    names = [t.name for t in lean_tools(app.tools)]
    assert set(TASK_TOOLS) <= set(names) and set(TASK_TOOLS) <= set(LEAN_TOOLS)
    assert not any(name.startswith("browser_") for name in names)
    # the team's helpers get the plain tools, without the task tools (main agent only)
    assert not set(TASK_TOOLS) & {t.name for t in app.base_tools}


def test_exit_stops_everything(tmp_path):
    holder = []
    lines = Lines(lambda: holder[0].tasks.start(sleeper(tmp_path)), "/exit")
    app, _, _, _ = make_app(tmp_path, lines, MockModel(script=[]))
    holder.append(app)
    manager = app.tasks
    try:
        assert app.start() == 0
        assert manager.get("t1") is not None and manager.running_count() == 0
    finally:
        manager.stop_all()


def test_a_device_conversation_ending_leaves_tasks_running(tmp_path):
    a, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    r = room()
    r.read = Lines("/exit")
    b = app_module.device_app(a, r)
    try:
        task = a.tasks.start(sleeper(tmp_path))
        assert b.start() == 0
        assert task.state == "running"
    finally:
        a.tasks.stop_all()


def test_start_result_when_it_exits_at_once_and_limits(tmp_path):
    app, _, cwd, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="auto")
    start = tool(app, "start_background").func
    quick = tmp_path / "quick.py"
    quick.write_text("import sys\nprint('bye-now')\nsys.exit(4)\n", encoding="utf-8")
    try:
        result = start(command=f'"{PY}" "{str(quick).replace(chr(92), "/")}"')
        assert result.splitlines()[0].startswith("Started t1 in the background: ")
        assert "It exited at once with code 4:" in result and "bye-now" in result

        result = start(command=sleeper(tmp_path, "second"), kind="terminal")   # never reaches the manager
        assert result.startswith("Started t2") and app.tasks.get("t2").kind == "task"

        (cwd / "sub").mkdir()
        assert start(command=sleeper(tmp_path, "in-sub"), cwd="sub").startswith("Started t3")
        assert app.tasks.get("t3").cwd == str((cwd / "sub").resolve())
        outside = start(command=sleeper(tmp_path, "outside"), cwd=str(tmp_path))
        assert outside.startswith("ERROR:") and app.tasks.get("t4") is None

        app.tasks.max_tasks = app.tasks.running_count()
        refused = start(command=sleeper(tmp_path, "too-many"))
        assert "already running" in refused and "stop one" in refused

        listed = tool(app, "list_background").func()
        assert "t1  exited (code 4)" in listed and "t3  running" in listed
        assert tool(app, "background_output").func(id="t1").startswith("t1 (exited with code 4 after ")
        assert tool(app, "background_output").func(id="nope").startswith("ERROR: there is no task nope")
    finally:
        app.tasks.stop_all()


def test_start_background_is_judged_as_run_shell(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="plan")
    try:
        result = tool(app, "start_background").func(command=sleeper(tmp_path))
        assert result.startswith("Refused: plan mode") and app.tasks.list() == []
    finally:
        app.tasks.stop_all()


def test_send_to_terminal(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="auto")
    send = tool(app, "send_to_terminal").func
    try:
        term = app.tasks.open_terminal()
        result = send(id=term.id, text="echo hello-from-agent")
        lines = result.splitlines()
        assert lines[0] == f"Sent to {term.id}: echo hello-from-agent" and lines[1] == "Output since:"
        assert until(lambda: "hello-from-agent" in app.tasks.output(term.id).replace("$ echo", ""), 15)
        task = app.tasks.start(sleeper(tmp_path))
        assert send(id=task.id, text="hi") == f"ERROR: {task.id} is not a terminal"
        assert send(id="term9", text="hi").startswith("ERROR: there is no task term9")
    finally:
        app.tasks.stop_all()


def test_send_to_terminal_shows_a_prompt_without_a_newline(tmp_path, monkeypatch):
    monkeypatch.setattr(task_tools, "SEND_WAIT_SEC", 3.0)    # room for a slow machine; the prompt is idle long before
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="auto")
    send = tool(app, "send_to_terminal").func
    asker = tmp_path / "ask.py"
    asker.write_text("import sys\nsys.stdout.write('Continue? [y/N] ')\nsys.stdout.flush()\nsys.stdin.readline()\n",
                     encoding="utf-8")
    path = str(asker).replace("\\", "/")
    try:
        term = app.tasks.open_terminal()
        send(id=term.id, text="echo warmed-up")                # the shell has started before the timed part
        assert until(lambda: "warmed-up" in app.tasks.output(term.id).replace("$ echo", ""), 15)
        command = f'& "{PY}" "{path}"' if sys.platform == "win32" else f'"{PY}" "{path}"'
        result = send(id=term.id, text=command)
        assert result.splitlines()[-1] == "Continue? [y/N] ", result
        assert "Continue? [y/N]" in app.tasks.output(term.id)
    finally:
        app.tasks.stop_all()


def test_make_permissions_is_what_build_tools_uses(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    perms = make_permissions(tmp_path, [extra])
    assert perms.workspace == str(tmp_path) and str(extra) in perms.allowed_paths
    assert perms.execute_shell and not perms.auto_create_paths
    assert build_tools(tmp_path, [extra], app_module.Gate(mode="auto", cwd=tmp_path))


def test_the_app_shares_the_process_manager(tmp_path, monkeypatch):
    seen = []
    real = background.shared

    def spy(home, cwd, permissions=None):
        seen.append(permissions)
        return real(home, cwd, permissions)
    monkeypatch.setattr(app_module, "shared", spy)
    app, _, cwd, home = make_app(tmp_path, Lines(), MockModel(script=[]))
    assert app.tasks is background.shared(home, cwd)
    assert seen and seen[0] is not None and seen[0].workspace == str(cwd)


def test_the_system_prompt_says_when_to_use_start_background():
    from agentx_dev.cli.context import BASE_PROMPT
    assert "start_background" in BASE_PROMPT and "nohup" in BASE_PROMPT and "stop_background" in BASE_PROMPT


def test_a_listing_looks_the_titles_up_once(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]))
    try:
        for owner in ("aaaa1111-x", "bbbb2222-y", "cccc3333-z"):
            app.tasks.start(sleeper(tmp_path, owner[:4]), owner=owner)
        app.tasks.start(sleeper(tmp_path, "mine"), owner="me")
        calls = []

        def titles(owners):
            calls.append(sorted(owners))
            return {"aaaa1111-x": "Fix the login bug"}
        text = task_tools.list_text(app.tasks, "me", titles)
        assert calls == [["aaaa1111-x", "bbbb2222-y", "cccc3333-z"]]
        assert "(started by: Fix the login bug)" in text and "(started by: this conversation)" in text
        assert text.count("(started by: another conversation)") == 2

        calls.clear()
        task_tools.list_text(background.TaskManager(tmp_path / "h", tmp_path), "me", titles)
        assert calls == []                                       # nothing listed: nothing looked up
    finally:
        app.tasks.stop_all()


def test_the_app_reads_the_titles_file_once_and_no_session_file(tmp_path, monkeypatch):
    from agentx_dev.cli import sessions
    app, _, cwd, home = make_app(tmp_path, Lines(), MockModel(script=[]))
    sessions.save_title(home, cwd, "20261124T101500Z-abcd1234.json", "Start the dev server")
    reads = []
    real = app_module.titles_by_short_id
    monkeypatch.setattr(app_module, "titles_by_short_id", lambda h, c: (reads.append(1), real(h, c))[1])
    monkeypatch.setattr(app_module, "list_sessions", lambda *a: (_ for _ in ()).throw(AssertionError("loaded")))
    app.session = SimpleNamespace(session_id="mine")
    try:
        for owner in ("abcd1234-5678-90ef", "ffff0000-1111", "eeee9999"):
            app.tasks.start(sleeper(tmp_path, owner[:4]), owner=owner)
        text = tool(app, "list_background").func()
        assert reads == [1]
        assert "(started by: Start the dev server)" in text
        assert text.count("(started by: another conversation)") == 2
    finally:
        app.tasks.stop_all()


def test_a_task_stopped_while_starting_says_so(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), MockModel(script=[]), mode="auto")
    real = app.tasks.start

    def start_then_stop(*args, **kwargs):
        task = real(*args, **kwargs)
        app.tasks.stop(task.id)                                  # someone else stopped it during the wait
        return task
    app.tasks.start = start_then_stop
    try:
        result = tool(app, "start_background").func(command=sleeper(tmp_path))
        assert "t1 was stopped while starting." in result and "code None" not in result
    finally:
        app.tasks.stop_all()


def test_a_failing_cleanup_step_does_not_skip_stopping_the_tasks(tmp_path):
    class Broken:
        def close(self):
            raise RuntimeError("server gone")

    holder = []
    lines = Lines(lambda: holder[0].tasks.start(sleeper(tmp_path)), "/exit")
    app, _, _, _ = make_app(tmp_path, lines, MockModel(script=[]))
    holder.append(app)
    app._connect_mcp = lambda: setattr(app, "mcp", Broken())
    try:
        assert app.start() == 0
        assert app.tasks.get("t1") is not None and app.tasks.running_count() == 0
    finally:
        app.tasks.stop_all()
