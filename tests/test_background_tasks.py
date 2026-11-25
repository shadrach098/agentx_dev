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
