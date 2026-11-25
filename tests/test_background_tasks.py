"""The task manager: real processes, so every wait is on a state with a deadline, never a fixed sleep."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentx_dev.cli import background
from agentx_dev.cli.background import TaskLimit, TaskManager, strip_codes

PY = sys.executable.replace("\\", "/")
# whether a shell's exit code can be read while it still holds its group (macOS before Python 3.13 cannot)
CAN_PEEK = sys.platform == "win32" or hasattr(__import__("os"), "waitid")


def until(check, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return check()


def script(tmp_path: Path, name: str, body: str) -> str:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f'"{PY}" "{str(f).replace(chr(92), "/")}"'


@pytest.fixture
def mgr(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    m = TaskManager(tmp_path / "home", work)
    yield m
    m.stop_all()


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    import os
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_start_returns_while_it_runs(mgr, tmp_path):
    t0 = time.monotonic()
    task = mgr.start(script(tmp_path, "sleep.py", "import time; time.sleep(60)"))
    assert time.monotonic() - t0 < 3
    assert task.state == "running"
    assert task.id == "t1"
    assert task.log.parent.name == mgr.run_id
    stopped = mgr.stop(task.id)
    assert until(lambda: stopped.state == "stopped", 5)
    assert mgr.running_count() == 0


def test_output_is_kept_and_read_back(mgr, tmp_path):
    task = mgr.start(script(tmp_path, "p.py", "import sys; print('hello'); sys.exit(3)"))
    assert until(lambda: task.state == "exited")
    assert task.exit_code == 3
    assert "hello" in mgr.output(task.id)
    text, offset = mgr.read_from(task.id, 0)
    assert "hello" in text
    assert mgr.read_from(task.id, offset) == ("", offset)


def test_stop_ends_the_whole_tree(mgr, tmp_path):
    pidfile = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text("import os, time\nopen(%r, 'w').write(str(os.getpid()))\ntime.sleep(60)\n" % str(pidfile),
                     encoding="utf-8")
    parent = script(tmp_path, "parent.py",
                    "import subprocess, sys, time\nsubprocess.Popen([sys.executable, %r])\ntime.sleep(60)\n"
                    % str(child))
    task = mgr.start(parent)
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    pid = int(pidfile.read_text())
    assert pid_alive(pid)
    mgr.stop(task.id)
    assert until(lambda: not pid_alive(pid), 10)


def test_escape_codes_and_bad_bytes(mgr, tmp_path):
    code = ("import sys\n"
            r"sys.stdout.buffer.write(b'\x1b[31mred\x1b[0m\n50%\r100%\nbad\xff\n')" "\n"
            "sys.stdout.flush()\n")
    task = mgr.start(script(tmp_path, "esc.py", code))
    assert until(lambda: task.state == "exited")
    out = mgr.output(task.id)
    assert "red" in out and "\x1b" not in out
    assert "100%" in out and "50%" not in out
    assert "\ufffd" in out


def test_strip_codes_directly():
    assert strip_codes("\x1b]0;title\x07ok\x1b[1;32m!") == "ok!"
    assert strip_codes("a\r\nb") == "a\nb"


def test_limits(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    m = TaskManager(tmp_path / "home", work, max_tasks=2)
    try:
        sleeper = script(tmp_path, "s.py", "import time; time.sleep(60)")
        m.start(sleeper)
        m.start(sleeper)
        with pytest.raises(TaskLimit) as e:
            m.start(sleeper)
        assert "t1" in str(e.value) and "t2" in str(e.value)
    finally:
        m.stop_all()


def test_terminal_takes_input(mgr):
    term = mgr.open_terminal()
    assert term.kind == "terminal" and term.id == "term1"
    mgr.send(term.id, "echo hi-there")
    assert until(lambda: "hi-there" in mgr.output(term.id).replace("$ echo hi-there", ""), 5)
    assert "$ echo hi-there" in mgr.output(term.id)
    with pytest.raises(ValueError):
        mgr.send(mgr.start(script(Path(mgr.cwd), "x.py", "import time; time.sleep(30)")).id, "x")


def test_log_is_capped(mgr, tmp_path, monkeypatch):
    monkeypatch.setattr(background, "LOG_CAP_BYTES", 10 * 1024)
    monkeypatch.setattr(background, "LOG_HEAD_BYTES", 1024)
    task = mgr.start(script(tmp_path, "big.py",
                            "for i in range(2000):\n    print('line%05d ' % i + 'x' * 40)\n"))
    assert until(lambda: task.state == "exited")
    data = task.log.read_bytes()
    assert len(data) < 12 * 1024
    assert data.startswith(b"line00000")
    assert data.rstrip().endswith(b"line01999 " + b"x" * 40)


def test_unknown_id_and_stop_twice(mgr, tmp_path):
    with pytest.raises(KeyError):
        mgr.output("nope")
    with pytest.raises(KeyError):
        mgr.read_from("nope", 0)
    task = mgr.start(script(tmp_path, "q.py", "print(1)"))
    assert until(lambda: task.state == "exited")
    assert mgr.stop(task.id).state == "exited"
    assert mgr.clear_finished() == 1 and mgr.get(task.id) is None


def test_stop_all_at_exit(mgr, tmp_path):
    sleeper = script(tmp_path, "s.py", "import time; time.sleep(60)")
    a, b = mgr.start(sleeper), mgr.start(sleeper)
    assert mgr.running_count() == 2
    mgr.stop_all()
    assert a.state == "stopped" and b.state == "stopped"
    assert mgr.running_count() == 0


# -- fix round 1 ------------------------------------------------------------------------------------------------

def split_writer(tmp_path: Path, name: str, parts: list) -> tuple:
    """A command that writes ``parts`` one by one, each only after ``go<n>`` exists, so a test decides the chunking."""
    body = ("import os, sys, time\n"
            "parts = %r\n"
            "for i, part in enumerate(parts):\n"
            "    if i:\n"
            "        while not os.path.exists(%r + str(i)):\n"
            "            time.sleep(0.02)\n"
            "    sys.stdout.buffer.write(part)\n"
            "    sys.stdout.buffer.flush()\n") % (parts, str(tmp_path / "go").replace("\\", "/"))
    return script(tmp_path, name, body), lambda i: (tmp_path / f"go{i}").write_text("x")


def collector(mgr, tid, needle):
    got, state = [], {"off": 0}

    def collect():
        t, state["off"] = mgr.read_from(tid, state["off"])
        got.append(t)
        return needle in "".join(got)
    return got, collect


def test_utf8_split_across_writes_is_not_garbled(mgr, tmp_path):
    cmd, go = split_writer(tmp_path, "u.py", [b"caf\xc3", b"\xa9 ok\n"])
    task = mgr.start(cmd)
    # the first write ends inside a character and has no newline: nothing is handed out yet
    assert until(lambda: task.log.exists() and task.log.stat().st_size >= 4)
    assert mgr.read_from(task.id, 0) == ("", 0)
    go(1)
    got, collect = collector(mgr, task.id, "ok")
    assert until(collect)
    joined = "".join(got)
    assert "caf\u00e9 ok" in joined and "\ufffd" not in joined


def test_escape_sequence_split_across_writes(mgr, tmp_path):
    cmd, go = split_writer(tmp_path, "e.py", [b"\x1b[3", b"1mred\x1b[0m\n"])
    task = mgr.start(cmd)
    assert until(lambda: task.log.exists() and task.log.stat().st_size >= 3)
    assert mgr.read_from(task.id, 0) == ("", 0)
    go(1)
    got, collect = collector(mgr, task.id, "red")
    assert until(collect)
    assert "".join(got).strip() == "red"


def test_carriage_return_update_in_a_later_write(mgr, tmp_path):
    cmd, go = split_writer(tmp_path, "r.py", [b"50%", b"\r100%\n"])
    task = mgr.start(cmd)
    assert until(lambda: task.log.exists() and task.log.stat().st_size >= 3)
    assert mgr.read_from(task.id, 0) == ("", 0)
    go(1)
    got, collect = collector(mgr, task.id, "100%")
    assert until(collect)
    joined = "".join(got)
    assert "50%" not in joined and "100%" in joined


def test_unfinished_last_line_is_released_when_the_task_ends(mgr, tmp_path):
    task = mgr.start(script(tmp_path, "n.py", "import sys; sys.stdout.write('no newline'); sys.stdout.flush()"))
    assert until(lambda: task.state == "exited")
    text, off = mgr.read_from(task.id, 0)
    assert text == "no newline" and off == len(b"no newline")


def test_an_idle_unfinished_line_is_released_when_asked(mgr, tmp_path):
    """A prompt (``Name: ``) waits for its answer with no newline: the window asks for it once it stopped growing."""
    cmd, go = split_writer(tmp_path, "prompt.py", [b"Name: ", b"alice\n"])
    task = mgr.start(cmd)
    assert until(lambda: task.log.exists() and task.log.stat().st_size >= 6)
    assert mgr.read_from(task.id, 0) == ("", 0)                  # the tools' default: whole lines only
    assert until(lambda: mgr.read_from(task.id, 0, release_idle=True) == ("Name: ", 6), 5)
    go(1)
    got, state = [], {"off": 6}

    def collect():
        t, state["off"] = mgr.read_from(task.id, state["off"], release_idle=True)
        got.append(t)
        return "alice" in "".join(got)
    assert until(collect)
    assert "".join(got) == "alice\n"


def test_an_unfinished_line_still_growing_is_not_released(mgr, tmp_path, monkeypatch):
    monkeypatch.setattr(background, "IDLE_RELEASE_SEC", 3600)
    cmd, _ = split_writer(tmp_path, "grow.py", [b"Name: ", b"x\n"])
    task = mgr.start(cmd)
    assert until(lambda: task.log.exists() and task.log.stat().st_size >= 6)
    assert mgr.read_from(task.id, 0, release_idle=True) == ("", 0)


def test_an_idle_release_never_cuts_a_character_or_an_escape(mgr, tmp_path):
    cmd, go = split_writer(tmp_path, "cut.py", [b"caf\xc3", b"\xa9 \x1b[3", b"1mred\x1b[0m\n"])
    task = mgr.start(cmd)
    assert until(lambda: task.log.exists() and task.log.stat().st_size >= 4)
    assert until(lambda: mgr.read_from(task.id, 0, release_idle=True) == ("caf", 3), 5)
    go(1)
    assert until(lambda: task.log.stat().st_size >= 9)
    assert until(lambda: mgr.read_from(task.id, 3, release_idle=True) == ("é ", 6), 5)
    go(2)
    got, state = [], {"off": 6}

    def collect():
        t, state["off"] = mgr.read_from(task.id, state["off"], release_idle=True)
        got.append(t)
        return "red" in "".join(got)
    assert until(collect)
    assert "".join(got) == "red\n" and "�" not in "".join(got)


def test_releasable_part_of_an_unfinished_line():
    cut = background._releasable
    assert cut(b"plain") == b"plain"
    assert cut(b"ab\x1b") == b"ab"
    assert cut(b"ab\x1b[1;3") == b"ab"
    assert cut(b"ab\x1b[1;31mcd") == b"ab\x1b[1;31mcd"
    assert cut(b"ab\x1b]0;title") == b"ab"
    assert cut(b"ab\x1b]0;title\x07cd") == b"ab\x1b]0;title\x07cd"
    assert cut(b"ab\x1b]0;t\x1b\\cd") == b"ab\x1b]0;t\x1b\\cd"
    assert cut(b"ab\x1b(") == b"ab"
    assert cut(b"ab\x1b(Bc") == b"ab\x1b(Bc"
    assert cut(b"x\xe2\x82") == b"x"
    assert cut(b"x\xe2\x82\xac") == b"x\xe2\x82\xac"
    assert cut(b"x\xf0\x9f\x98") == b"x"


def test_read_from_across_a_trim(mgr, tmp_path, monkeypatch):
    monkeypatch.setattr(background, "LOG_CAP_BYTES", 10 * 1024)
    monkeypatch.setattr(background, "LOG_HEAD_BYTES", 1024)
    task = mgr.start(script(tmp_path, "big2.py",
                            "for i in range(2000):\n    print('line%05d ' % i + 'x' * 40)\n"))
    assert until(lambda: task.state == "exited")
    log = task.log.read_bytes().decode()
    assert log.startswith("line00000") and "bytes of older output dropped" in log
    lines = log.replace("\r\n", "\n").split("\n")
    marker = next(i for i, l in enumerate(lines) if l.startswith("[... "))
    assert lines[marker - 1].startswith("line")                  # the head ends on a whole line
    assert lines[marker + 1].startswith("line")                  # the tail starts on a whole line
    assert len(lines[marker - 1]) == len(lines[marker + 1]) == 50
    text, off = mgr.read_from(task.id, 0)
    assert text.startswith("line00000") and "older output dropped" in text
    assert text.rstrip().endswith("line01999 " + "x" * 40)
    assert mgr.read_from(task.id, off) == ("", off)
    # a reader whose offset fell inside the dropped part is told so, then continues
    t2, off2 = mgr.read_from(task.id, 2000)
    assert "older output dropped" in t2 and off2 == off


def test_offsets_stay_valid_while_it_trims(mgr, tmp_path, monkeypatch):
    monkeypatch.setattr(background, "LOG_CAP_BYTES", 10 * 1024)
    monkeypatch.setattr(background, "LOG_HEAD_BYTES", 1024)
    task = mgr.start(script(tmp_path, "big3.py",
                            "import time\nfor i in range(3000):\n    print('line%05d ' % i + 'x' * 40, flush=True)\n"
                            "    if i % 100 == 0:\n        time.sleep(0.02)\n"))
    seen, off = [], 0
    end = time.monotonic() + 60
    while time.monotonic() < end:
        done = task.state != "running"
        t, off = mgr.read_from(task.id, off)
        seen.append(t)
        if done:
            break
        time.sleep(0.01)
    joined = "".join(seen)
    nums = [int(l[4:9]) for l in joined.split("\n") if l.startswith("line")]
    assert nums == sorted(nums) and len(nums) == len(set(nums))     # nothing repeated, nothing out of order
    assert nums[0] == 0 and nums[-1] == 2999


def test_passthrough_variable_reaches_a_task(tmp_path, monkeypatch):
    from agentx_dev.DefaultTools import Permissions
    monkeypatch.setenv("AGENTX_TEST_PASSTHROUGH", "visible-value")
    monkeypatch.setenv("AGENTX_TEST_HIDDEN", "hidden-value")
    work = tmp_path / "w"
    work.mkdir()
    perms = Permissions(execute_shell=True, workspace=str(work),
                        subprocess_env_passthrough=["AGENTX_TEST_PASSTHROUGH"])
    m = TaskManager(tmp_path / "home", work, permissions=perms)
    try:
        task = m.start(script(tmp_path, "env.py",
                              "import os; print(os.environ.get('AGENTX_TEST_PASSTHROUGH'), "
                              "os.environ.get('AGENTX_TEST_HIDDEN'))"))
        assert until(lambda: task.state == "exited")
        assert "visible-value None" in m.output(task.id)
    finally:
        m.stop_all()


def outliving_child(tmp_path, tag, body_after_pid):
    pidfile = tmp_path / f"{tag}.pid"
    child = tmp_path / f"{tag}_child.py"
    child.write_text("import os, sys, time\nopen(%r, 'w').write(str(os.getpid()))\n%s"
                     % (str(pidfile).replace("\\", "/"), body_after_pid), encoding="utf-8")
    parent = script(tmp_path, f"{tag}_spawner.py",
                    "import subprocess, sys\nsubprocess.Popen([sys.executable, %r])\n"
                    % str(child).replace("\\", "/"))
    return parent, pidfile


def test_task_whose_child_outlives_the_shell_stays_running_and_stoppable(mgr, tmp_path):
    gate = tmp_path / "gate"
    parent, pidfile = outliving_child(
        tmp_path, "gc",
        "while not os.path.exists(%r):\n    time.sleep(0.02)\nprint('late-output', flush=True)\ntime.sleep(60)\n"
        % str(gate).replace("\\", "/"))
    task = mgr.start(parent)
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    pid = int(pidfile.read_text())
    # the shell has exited (its code is recorded) but the child holds the output pipe: still running
    assert until(lambda: task.exit_code is not None) or not CAN_PEEK
    assert task.state == "running" and mgr.running_count() == 1
    # and what the child prints later still lands in the log
    gate.write_text("go")
    assert until(lambda: "late-output" in mgr.output(task.id), 15)
    assert pid_alive(pid)
    mgr.stop(task.id)
    assert task.state == "stopped"
    assert until(lambda: not pid_alive(pid), 10)


def test_stop_all_reaches_a_child_that_outlived_its_shell(mgr, tmp_path):
    parent, pidfile = outliving_child(tmp_path, "gc2", "time.sleep(60)\n")
    task = mgr.start(parent)
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    pid = int(pidfile.read_text())
    assert until(lambda: task.exit_code is not None) or not CAN_PEEK
    mgr.stop_all()
    assert until(lambda: not pid_alive(pid), 10)


def test_stop_during_start_kills_the_new_process(mgr, tmp_path, monkeypatch):
    """stop() lands between registering the task and the spawn: the process must not be left running."""
    real_popen = subprocess.Popen
    box = {}

    def popen_after_stop(*a, **kw):
        if "proc" in box:                    # the kill's own taskkill (Popen is patched everywhere)
            return real_popen(*a, **kw)
        mgr.stop("t1")                       # the task is registered by now, with no process yet
        box["proc"] = real_popen(*a, **kw)
        return box["proc"]

    monkeypatch.setattr(background.subprocess, "Popen", popen_after_stop)
    task = mgr.start(script(tmp_path, "slow.py", "import time; time.sleep(60)"))
    assert task.state == "stopped"
    assert until(lambda: box["proc"].poll() is not None, 10)


def test_interrupt_a_terminal_command(mgr, tmp_path):
    term = mgr.open_terminal()
    sleeper = tmp_path / "long.py"
    sleeper.write_text("import time\nprint('long-started', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    path = str(sleeper).replace("\\", "/")
    mgr.send(term.id, f'& "{PY}" "{path}"' if sys.platform == "win32" else f'"{PY}" "{path}"')
    assert until(lambda: "long-started" in mgr.output(term.id), 15)
    mgr.interrupt(term.id)
    mgr.send(term.id, "echo after-interrupt")
    assert until(lambda: "after-interrupt" in mgr.output(term.id).replace("$ echo after-interrupt", ""), 15)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_signal_handler_stops_tasks_then_chains():
    import os
    import signal
    calls = []
    seen = []

    def previous(signum, frame):
        seen.append(("prev", signum))

    import threading
    threads = []

    def stop_all():
        threads.append(threading.current_thread())
        calls.append("stopped")

    handler = background._chained_handler(previous, stop_all=stop_all)
    handler(signal.SIGTERM, None)
    assert calls == ["stopped"] and seen == [("prev", signal.SIGTERM)]
    assert threads and threads[0] is not threading.current_thread()    # never on the interrupted thread itself

    calls.clear()
    handler = background._chained_handler(signal.SIG_IGN, stop_all=lambda: calls.append("stopped"))
    handler(signal.SIGHUP, None)                   # ignored before: stop the tasks, then just return
    assert calls == ["stopped"]

    sent, restored = [], []
    orig_kill, orig_signal = os.kill, signal.signal
    try:
        os.kill = lambda pid, sig: sent.append((pid, sig))
        signal.signal = lambda sig, h: restored.append((sig, h))
        handler = background._chained_handler(signal.SIG_DFL, stop_all=lambda: calls.append("stopped2"))
        handler(signal.SIGTERM, None)
    finally:
        os.kill, signal.signal = orig_kill, orig_signal
    assert restored == [(signal.SIGTERM, signal.SIG_DFL)]
    assert sent == [(os.getpid(), signal.SIGTERM)] and calls[-1] == "stopped2"

    sent.clear()
    restored.clear()
    try:                                           # None (a handler not set from Python) is treated as SIG_DFL
        os.kill = lambda pid, sig: sent.append((pid, sig))
        signal.signal = lambda sig, h: restored.append((sig, h))
        background._chained_handler(None, stop_all=lambda: calls.append("stopped3"))(signal.SIGHUP, None)
    finally:
        os.kill, signal.signal = orig_kill, orig_signal
    assert restored == [(signal.SIGHUP, signal.SIG_DFL)] and sent == [(os.getpid(), signal.SIGHUP)]


def test_a_stop_that_hangs_does_not_hold_the_signal_forever():
    import threading
    release = threading.Event()
    after = []
    handler = background._chained_handler(lambda signum, frame: after.append(signum),
                                          stop_all=lambda: release.wait(30), timeout=0.2)
    began = time.monotonic()
    handler(15, None)
    release.set()
    assert after == [15] and time.monotonic() - began < 5


def test_shared_retries_the_signal_handlers_on_each_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(background, "install_signal_handlers", lambda: calls.append(1))
    first = background.shared(tmp_path / "home", tmp_path)
    second = background.shared(tmp_path / "home", tmp_path)
    assert first is second and len(calls) == 2


def test_install_signal_handlers_is_safe_before_any_manager_and_off_the_main_thread(monkeypatch):
    import threading
    monkeypatch.setattr(background, "_signals_installed", False)
    monkeypatch.setattr(background, "_shared", {})              # no manager yet
    registered = []
    monkeypatch.setattr(background.signal, "signal", lambda sig, h: registered.append((sig, h)))
    off_main = []
    worker = threading.Thread(target=lambda: off_main.append(background.install_signal_handlers()))
    worker.start()
    worker.join()
    assert off_main == [False] and registered == []             # another thread: nothing is touched
    installed = background.install_signal_handlers()
    if sys.platform == "win32":
        assert installed is False and registered == []
        return
    import signal
    assert installed is True and [sig for sig, _ in registered] == [signal.SIGHUP, signal.SIGTERM]
    assert background.install_signal_handlers() is True and len(registered) == 2      # once
    background._stop_every_manager()                            # what the handler runs: fine with no manager


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_the_ui_launcher_installs_the_handlers_on_the_main_thread(monkeypatch, tmp_path):
    import argparse
    import signal
    from agentx_dev.cli import app as app_module
    from agentx_dev.cli import webui
    monkeypatch.setattr(background, "_signals_installed", False)
    registered = []
    monkeypatch.setattr(background.signal, "signal", lambda sig, h: registered.append(sig))
    monkeypatch.setattr(app_module, "setup_logging", lambda home: None)
    seen_at_run = []

    class Front:
        def __init__(self, **kw):
            self.app = None

        def run(self, start):
            seen_at_run.append(list(registered))
            return 0
    monkeypatch.setattr(webui, "WebFront", Front)
    args = argparse.Namespace(port=0, no_open=True, remote=False)
    assert app_module._run_ui(args) == 0
    assert seen_at_run == [[signal.SIGHUP, signal.SIGTERM]]


def test_the_ui_launcher_asks_for_the_handlers_before_the_window_runs(monkeypatch):
    import argparse
    import threading
    from agentx_dev.cli import app as app_module
    from agentx_dev.cli import webui
    order = []
    monkeypatch.setattr(app_module, "install_signal_handlers",
                        lambda: order.append(("install", threading.current_thread() is threading.main_thread())))
    monkeypatch.setattr(app_module, "setup_logging", lambda home: None)

    class Front:
        def __init__(self, **kw):
            self.app = None

        def run(self, start):
            order.append(("run", None))
            return 0
    monkeypatch.setattr(webui, "WebFront", Front)
    assert app_module._run_ui(argparse.Namespace(port=0, no_open=True, remote=False)) == 0
    assert order == [("install", True), ("run", None)]


# -- final review fixes: nothing is left running --------------------------------------------------------------------

def redirected_child(tmp_path, tag):
    """A command whose child writes its output to a file (``npm run dev > dev.log 2>&1 &``): the shell exits, the
    output pipe closes and the task counts as exited, while the child keeps running."""
    pidfile = tmp_path / f"{tag}.pid"
    child = tmp_path / f"{tag}_child.py"
    child.write_text("import os, time\nopen(%r, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
                     % str(pidfile).replace("\\", "/"), encoding="utf-8")
    log = str(tmp_path / f"{tag}.log").replace("\\", "/")
    parent = script(tmp_path, f"{tag}_parent.py",
                    "import subprocess, sys\nlog = open(%r, 'w')\n"
                    "subprocess.Popen([sys.executable, %r], stdout=log, stderr=log, stdin=subprocess.DEVNULL)\n"
                    % (log, str(child).replace("\\", "/")))
    return parent, pidfile


def started_redirected(mgr, tmp_path, tag):
    parent, pidfile = redirected_child(tmp_path, tag)
    task = mgr.start(parent)
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    pid = int(pidfile.read_text())
    assert until(lambda: task.state == "exited")          # its pipe closed: as far as the log goes it ended
    assert pid_alive(pid)
    return task, pid


def test_stop_all_kills_a_child_that_redirected_its_output(mgr, tmp_path):
    task, pid = started_redirected(mgr, tmp_path, "rd1")
    assert mgr.leftovers(task.id)
    mgr.stop_all()
    assert until(lambda: not pid_alive(pid), 10)
    assert not mgr.leftovers(task.id)


def test_stop_on_an_exited_task_kills_what_it_left_running(mgr, tmp_path):
    from agentx_dev.cli.task_tools import stop_text
    task, pid = started_redirected(mgr, tmp_path, "rd2")
    assert stop_text(mgr, task) == f"Stopped what {task.id} left running."
    assert until(lambda: not pid_alive(pid), 10)
    assert task.state == "exited"                       # its own exit code still stands
    assert stop_text(mgr, task) == f"{task.id} had already ended (exit code {task.exit_code})."


def test_clear_finished_keeps_what_a_task_left_running_stoppable(mgr, tmp_path):
    task, pid = started_redirected(mgr, tmp_path, "rd3")
    assert mgr.clear_finished() == 1 and mgr.get(task.id) is None
    mgr.stop_all()
    assert until(lambda: not pid_alive(pid), 10)


def test_stop_marks_stopped_only_after_the_kill_and_a_failed_kill_is_retried_at_exit(mgr, tmp_path, monkeypatch):
    task = mgr.start(script(tmp_path, "s.py", "import time; time.sleep(60)"))
    pid = mgr._run(task.id).proc.pid
    real_kill = mgr._kill
    seen = []
    monkeypatch.setattr(background, "STOP_WAIT_SEC", 0.3)
    monkeypatch.setattr(mgr, "_kill", lambda run: seen.append(run.task.state))     # a kill that does nothing
    stopped = mgr.stop(task.id)
    assert seen == ["running"]                          # not marked stopped before the kill was tried
    assert stopped.state == "running" and pid_alive(pid)
    monkeypatch.setattr(mgr, "_kill", real_kill)
    mgr.stop_all()                                      # at exit: tried again
    assert until(lambda: not pid_alive(pid), 10)
    assert task.state == "stopped"


@pytest.mark.skipif(sys.platform != "win32", reason="the job object is Windows only")
def test_windows_job_kills_the_tree_when_agentx_dies(tmp_path):
    """agentx ending any way at all (a crash, Task Manager, the console window closed) closes the job handle, and
    the job is set to kill everything in it then."""
    pidfile = tmp_path / "orphan.pid"
    child = tmp_path / "orphan_child.py"
    child.write_text("import os, time\nopen(%r, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
                     % str(pidfile).replace("\\", "/"), encoding="utf-8")
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from agentx_dev.cli.background import TaskManager\n"
        "m = TaskManager(%r, %r)\n"
        "m.start('\"%s\" \"%s\"')\n"
        "while not (os.path.exists(%r) and open(%r).read().strip()):\n"
        "    time.sleep(0.05)\n"
        "os._exit(1)\n"                                   # no atexit, no stop_all: as if it crashed
        % (str(Path(background.__file__).parents[2]), str(tmp_path / "home"), str(tmp_path),
           PY, str(child).replace("\\", "/"), str(pidfile), str(pidfile)), encoding="utf-8")
    done = subprocess.run([sys.executable, str(harness)], timeout=60)
    assert done.returncode == 1
    pid = int(pidfile.read_text())
    try:
        assert until(lambda: not pid_alive(pid), 10)
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


@pytest.mark.skipif(sys.platform != "win32", reason="the job object is Windows only")
def test_windows_job_is_set_to_kill_on_close(mgr, tmp_path):
    task = mgr.start(script(tmp_path, "s.py", "import time; time.sleep(60)"))
    job = mgr._run(task.id).job
    assert job is not None and background._job_kills_on_close(job)
    mgr.stop(task.id)


# -- POSIX: the group id is used only while the shell is unreaped, so it can never name someone else's group ----------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


@posix_only
def test_a_finished_task_releases_its_group_and_is_never_signalled_again(mgr, tmp_path, monkeypatch):
    import os
    task = mgr.start(script(tmp_path, "quick.py", "import sys; sys.exit(3)"))
    run = mgr._run(task.id)
    assert until(lambda: task.state == "exited" and run.pgid is None)
    assert task.exit_code == 3 and run.proc.returncode == 3          # reaped, its code kept
    sent = []
    monkeypatch.setattr(background.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    # as if the id had since been given to an unrelated program leading a group of its own
    monkeypatch.setattr(background, "_group_has_live", lambda pgid: True)
    assert mgr.stop(task.id).state == "exited"
    mgr.stop_all()
    mgr.interrupt(task.id)
    assert not mgr.leftovers(task.id)
    assert sent == []


@posix_only
def test_the_shell_stays_unreaped_while_what_it_left_runs_then_is_reaped(mgr, tmp_path):
    import os
    task, pid = started_redirected(mgr, tmp_path, "pin1")
    run = mgr._run(task.id)
    shell = run.proc.pid
    assert run.pgid == shell and run.proc.returncode is None        # the id is still held by the zombie shell
    assert os.getpgid(pid) == shell
    if hasattr(os, "waitid"):
        assert task.exit_code == 0                                   # read without reaping
    if os.path.isdir("/proc/self"):
        state = open(f"/proc/{shell}/stat", "rb").read().rsplit(b")", 1)[1].split()[0]
        assert state == b"Z"
    mgr.stop(task.id)                                                # kills what it left, through the held group
    assert until(lambda: not pid_alive(pid), 10)
    assert until(lambda: run.pgid is None and run.proc.returncode is not None, 10)
    assert task.state == "exited" and task.exit_code == 0


@posix_only
def test_what_a_task_left_running_ending_by_itself_releases_the_group(mgr, tmp_path):
    gate = tmp_path / "gate2"
    pidfile = tmp_path / "self_end.pid"
    child = tmp_path / "self_end_child.py"
    child.write_text("import os, time\nopen(%r, 'w').write(str(os.getpid()))\n"
                     "while not os.path.exists(%r):\n    time.sleep(0.02)\n"
                     % (str(pidfile), str(gate)), encoding="utf-8")
    parent = script(tmp_path, "self_end_parent.py",
                    "import subprocess, sys\n"
                    "subprocess.Popen([sys.executable, %r], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
                    " stdin=subprocess.DEVNULL)\n" % str(child))
    task = mgr.start(parent)
    run = mgr._run(task.id)
    assert until(lambda: pidfile.exists() and pidfile.read_text().strip())
    assert until(lambda: task.state == "exited")
    assert run.pgid is not None and mgr.leftovers(task.id)
    gate.write_text("go")
    assert until(lambda: run.pgid is None, 15)                       # the wait thread reaps the shell on its own
    assert not mgr.leftovers(task.id)


@posix_only
def test_group_has_live_ignores_zombies(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        assert background._group_has_live(proc.pid) is True
        proc.kill()
        assert until(lambda: background._group_has_live(proc.pid) is False)    # only the unreaped zombie is left
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_group_has_live_through_ps(tmp_path, monkeypatch):
    import os
    import shutil
    if not shutil.which("ps"):
        pytest.skip("no ps here")
    real_isdir = os.path.isdir
    monkeypatch.setattr(background.os.path, "isdir", lambda p: False if p == "/proc/self" else real_isdir(p))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        assert background._group_has_live(proc.pid) is True
        proc.kill()
        assert until(lambda: background._group_has_live(proc.pid) is False)
    finally:
        proc.kill()
        proc.wait()


@posix_only
def test_peek_exit_reads_the_code_without_reaping():
    import os
    if not hasattr(os, "waitid"):
        pytest.skip("os.waitid is missing here")
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
    try:
        assert until(lambda: background._peek_exit(proc.pid) is not None)
        assert background._peek_exit(proc.pid) == 7                  # still there to read again: not reaped
    finally:
        assert proc.wait(10) == 7
    with pytest.raises(background._Reaped):
        background._peek_exit(proc.pid)


def test_an_unknown_exit_code_is_left_out_of_the_text():
    from agentx_dev.cli import task_tools
    task = background.Task(id="t1", kind="task", command="npm run dev", cwd=".", owner="o", name=None,
                           started_at=time.time() - 5, ended_at=time.time(), exit_code=None, state="exited",
                           log=Path("t1.log"))
    assert task_tools.header(task).startswith("t1 (exited after ")
    assert task_tools.ended_text(task) == "t1 had already ended."
    assert "exited" in task_tools.task_line(task, "o", {}) and "None" not in task_tools.task_line(task, "o", {})
