"""run_shell and run_python stop at their time limit, even when the command started other programs.

On Windows ``subprocess.run(timeout=)`` killed only the shell and then waited, with no limit, for the output pipes
to close; a program the shell had started (``ssh``, ``sleep``, a dev server) kept them open, so a 5-minute limit
became as long as that program ran: agentx showed ``Running run_shell... 7m 28s``. A command that ended but left a
program running in the background (``cmd &`` without redirecting its output) also held the tool until the limit."""

import shutil
import sys
import time

import pytest

from agentx_dev import DefaultTools, Permissions
from agentx_dev.DefaultTools import _resolve_shell_interpreter

PY = sys.executable.replace("\\", "/")


def tool(name, **perms):
    permissions = Permissions(execute_shell=True, execute_python=True, **perms)
    return next(t for t in DefaultTools.build(permissions) if t.name == name)


def has_shell():
    return shutil.which(_resolve_shell_interpreter()[0]) is not None or \
        __import__("os").path.isfile(_resolve_shell_interpreter()[0])


needs_shell = pytest.mark.skipif(not has_shell(), reason="no shell to run")


@needs_shell
class TestRunShell:
    def test_a_program_the_shell_started_is_stopped_at_the_limit(self):
        shell = tool("run_shell", shell_timeout_sec=2)
        started = time.monotonic()
        result = shell.func(command=f'"{PY}" -c "import time; time.sleep(20)"')
        took = time.monotonic() - started
        assert took < 10, f"run_shell took {took:.1f}s with a 2s limit"
        assert "timed out after 2" in result

    def test_a_background_program_does_not_hold_the_result(self):
        shell = tool("run_shell", shell_timeout_sec=30)
        started = time.monotonic()
        result = shell.func(command=f'("{PY}" -c "import time; time.sleep(20)") & echo started')
        took = time.monotonic() - started
        assert took < 10, f"run_shell waited {took:.1f}s for a background program"
        assert "started" in result
        assert "still running" in result and "> log" in result        # says what to do instead

    def test_normal_commands_unchanged(self):
        shell = tool("run_shell", shell_timeout_sec=30)
        result = shell.func(command="echo hello; echo oops 1>&2; exit 3")
        assert "--- stdout ---\nhello" in result and "--- stderr ---\noops" in result and "(exit code 3)" in result

    def test_output_before_the_limit_is_kept(self):
        shell = tool("run_shell", shell_timeout_sec=2)
        result = shell.func(command=f'echo before; "{PY}" -c "import time; time.sleep(20)"')
        assert "timed out" in result and "before" in result

    def test_it_cannot_read_the_terminal(self):
        shell = tool("run_shell", shell_timeout_sec=10)
        started = time.monotonic()
        result = shell.func(command=f'"{PY}" -c "import sys; print(repr(sys.stdin.read()))"')
        assert time.monotonic() - started < 8 and "''" in result


class TestRunPython:
    def test_a_child_process_is_stopped_at_the_limit(self):
        python = tool("run_python", python_timeout_sec=2)
        code = ("import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
                "time.sleep(20)\n")
        started = time.monotonic()
        result = python.func(code=code)
        took = time.monotonic() - started
        assert took < 10, f"run_python took {took:.1f}s with a 2s limit"
        assert "timed out after 2" in result

    def test_normal_code_unchanged(self):
        python = tool("run_python", python_timeout_sec=30)
        result = python.func(code="print('hi ✓')")
        assert "hi ✓" in result


def _alive(pid):
    import os
    import subprocess as sp
    if sys.platform == "win32":
        out = sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class TestCtrlC:
    """Ctrl+C while a command runs (full-screen mode raises KeyboardInterrupt in the worker) stops the command and
    everything it started, instead of leaving them running."""

    def test_the_command_and_its_children_are_stopped(self, tmp_path):
        import threading
        from agentx_dev.DefaultTools import _run_bounded
        from agentx_dev.cli.fullscreen import raise_in
        pid_file = tmp_path / "child.pid"
        code = ("import os, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n")
        outcome = {}

        def work():
            try:
                _run_bounded([sys.executable, "-c", code], timeout=120, env=None, cwd=None)
                outcome["result"] = "finished"
            except KeyboardInterrupt:
                outcome["result"] = "interrupted"
        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.monotonic() + 20
        while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        child = int(pid_file.read_text())
        assert raise_in(worker)
        worker.join(15)
        assert not worker.is_alive() and outcome.get("result") == "interrupted"
        time.sleep(1)
        assert not _alive(child), "the command's child kept running after Ctrl+C"
