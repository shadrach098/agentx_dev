"""The task manager: real processes, so every wait is on a state with a deadline, never a fixed sleep."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentx_dev.cli import background
from agentx_dev.cli.background import TaskLimit, TaskManager, strip_codes

PY = sys.executable.replace("\\", "/")


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
    assert until(lambda: task.exit_code is not None)
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
    assert until(lambda: task.exit_code is not None)
    mgr.stop_all()
    assert until(lambda: not pid_alive(pid), 10)


def test_stop_during_start_kills_the_new_process(mgr, tmp_path, monkeypatch):
    """stop() lands between registering the task and the spawn: the process must not be left running."""
    real_popen = subprocess.Popen
    box = {}

    def popen_after_stop(*a, **kw):
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

    handler = background._chained_handler(previous, stop_all=lambda: calls.append("stopped"))
    handler(signal.SIGTERM, None)
    assert calls == ["stopped"] and seen == [("prev", signal.SIGTERM)]

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
