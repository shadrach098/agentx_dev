"""Commands that keep running: background tasks (started by the agent) and terminals (opened by the user).

One ``TaskManager`` per agentx process. Each task runs through the shell ``run_shell`` uses, in its own process group
(so the whole tree can be stopped), and a reader thread copies its stdout and stderr, merged, into a log file under
``~/.agentx/tasks/<project>/<run>/<id>.log``. The log is binary; it is decoded and stripped of escape codes when read.
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agentx_dev import safe_exe
from agentx_dev.DefaultTools import Permissions, _build_child_env, _kill_tree, _resolve_shell_interpreter
from agentx_dev.cli.sessions import project_slug

LOG_CAP_BYTES = 5 * 1024 * 1024      # a log never grows past this ...
LOG_HEAD_BYTES = 64 * 1024           # ... it keeps its first 64 KB and drops the oldest of the rest
MAX_TASKS = 8
MAX_TERMINALS = 4

if sys.platform == "win32":
    TERMINAL_ARGV = ["powershell", "-NoLogo", "-NoProfile", "-Command", "-"]
else:
    TERMINAL_ARGV = [os.environ.get("SHELL") or "/bin/sh"]


class TaskLimit(Exception):
    """Too many running tasks or terminals; the message names the running ones."""


@dataclass
class Task:
    id: str
    kind: str                         # "task" | "terminal"
    command: str
    cwd: str
    owner: str
    name: str
    started_at: float
    ended_at: Optional[float]
    exit_code: Optional[int]
    state: str                        # "running" | "exited" | "stopped" | "failed"
    log: Path


_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_OTHER_ESC = re.compile(r"\x1b[@-Z\\-_]|\x1b[()][0-9A-Za-z]")


def strip_codes(text: str) -> str:
    """ANSI and OSC escapes removed; a line rewritten with ``\\r`` (a progress bar) keeps only its last state."""
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _OTHER_ESC.sub("", text)
    text = text.replace("\r\n", "\n")
    if "\r" not in text:
        return text
    out = []
    for line in text.split("\n"):
        parts = [p for p in line.split("\r") if p]
        out.append(parts[-1] if parts else "")
    return "\n".join(out)


def _new_job(proc: "subprocess.Popen"):
    """Windows: a job object holding ``proc`` and everything it starts, so the tree stays reachable after the shell
    itself has exited (``taskkill /T`` needs the parent alive). ``None`` elsewhere or when it cannot be made."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        if not k.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            k.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _job_terminate(job) -> None:
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateJobObject(job, 1)
    except Exception:
        pass


def _job_close(job) -> None:
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle(job)
    except Exception:
        pass


class _Run:
    """What the manager keeps beside a ``Task``: the process, its log file and the truncation bookkeeping."""

    def __init__(self, task: Task, proc: Optional[subprocess.Popen]):
        self.task = task
        self.proc = proc
        self.lock = threading.Lock()
        self.fh = None
        self.dropped = 0              # bytes cut out of the log, so offsets handed out stay valid
        self.head_len: Optional[int] = None   # set once the log has been trimmed: bytes of the kept head
        self.marker_len = 0           # bytes of the "dropped" marker line between head and tail
        self.stopping = False
        self.job = None               # Windows job object holding the whole tree
        self.kill_lock = threading.Lock()
        self.eof = threading.Event()  # the output pipe has closed: nothing more will be written
        self.reader: Optional[threading.Thread] = None


class TaskManager:
    def __init__(self, home: Path, cwd: Path, env: Optional[dict] = None,
                 max_tasks: int = MAX_TASKS, max_terminals: int = MAX_TERMINALS,
                 permissions: Optional[Permissions] = None):
        self.home = Path(home)
        self.cwd = Path(cwd)
        self.max_tasks = max_tasks
        self.max_terminals = max_terminals
        self._env = env
        self._permissions = permissions
        self._runs: Dict[str, _Run] = {}
        self._lock = threading.Lock()
        self._counters = {"task": 0, "terminal": 0}
        self.run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.dir = self.home / ".agentx" / "tasks" / project_slug(self.cwd) / self.run_id

    # -- starting -----------------------------------------------------------------------------------------------

    def _child_env(self) -> dict:
        if self._env is not None:
            return dict(self._env)
        perms = self._permissions or Permissions(execute_shell=True, workspace=str(self.cwd))
        return _build_child_env(perms)

    def start(self, command: str, *, cwd: Optional[Path] = None, owner: str = "", name: str = "",
              kind: str = "task") -> Task:
        argv = _resolve_shell_interpreter() + [command]
        return self._launch(argv, command, cwd, owner, name, kind)

    def open_terminal(self) -> Task:
        argv = list(TERMINAL_ARGV)
        if sys.platform == "win32":
            try:
                argv[0] = safe_exe.powershell()
            except Exception:
                pass
        return self._launch(argv, " ".join(TERMINAL_ARGV[:1]), None, "", "", "terminal")

    def _launch(self, argv: List[str], command: str, cwd: Optional[Path], owner: str, name: str,
                kind: str) -> Task:
        kind = "terminal" if kind == "terminal" else "task"
        with self._lock:
            running = [r.task.id for r in self._runs.values()
                       if r.task.kind == kind and r.task.state == "running"]
            limit = self.max_terminals if kind == "terminal" else self.max_tasks
            if len(running) >= limit:
                what = "terminals" if kind == "terminal" else "background tasks"
                raise TaskLimit(f"{len(running)} {what} are already running ({', '.join(running)}); "
                                f"stop one before starting another.")
            self._counters[kind] += 1
            tid = f"{'term' if kind == 'terminal' else 't'}{self._counters[kind]}"
            self.dir.mkdir(parents=True, exist_ok=True)
            task = Task(id=tid, kind=kind, command=command, cwd=str(cwd or self.cwd), owner=owner, name=name,
                        started_at=time.time(), ended_at=None, exit_code=None, state="running",
                        log=self.dir / f"{tid}.log")
            run = _Run(task, None)
            run.fh = open(task.log, "w+b")
            self._runs[tid] = run
        options: dict = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if kind == "terminal" else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=self._child_env(),
                                    cwd=task.cwd, **options)
        except (OSError, ValueError) as e:
            self._append(run, f"could not start: {e}\n".encode("utf-8"))
            with run.lock:
                task.state = "failed"
                task.ended_at = time.time()
                if run.fh:
                    run.fh.close()
                    run.fh = None
            run.eof.set()
            return task
        with run.lock:
            run.proc = proc
            run.job = _new_job(proc)
            stopped_already = run.stopping
        if stopped_already:                  # stop() ran between the registration and the spawn
            self._kill(run)
        run.reader = threading.Thread(target=self._read, args=(run,), daemon=True)
        run.reader.start()
        threading.Thread(target=self._wait, args=(run,), daemon=True).start()
        return task

    # -- the threads --------------------------------------------------------------------------------------------

    def _read(self, run: _Run) -> None:
        try:
            while True:
                chunk = run.proc.stdout.read1(8192)
                if not chunk:
                    break
                self._append(run, chunk)
        except (OSError, ValueError):
            pass
        finally:
            run.eof.set()
            try:
                run.proc.stdout.close()
            except Exception:
                pass

    def _wait(self, run: _Run) -> None:
        code = run.proc.wait()
        with run.lock:
            run.task.exit_code = code        # the shell's own code; the task stays running while anything holds the pipe
        if run.reader:
            run.reader.join()                # a descendant (``cmd &``) may keep the pipe, and the task, alive
        with run.lock:
            if run.task.ended_at is None:
                run.task.ended_at = time.time()
            if run.task.state == "running":
                run.task.state = "exited"
        try:
            if run.proc.stdin:
                run.proc.stdin.close()
        except Exception:
            pass
        with run.kill_lock:
            if run.job is not None:
                _job_close(run.job)
                run.job = None
        with run.lock:
            if run.fh is not None:
                run.fh.close()
                run.fh = None

    def _kill(self, run: _Run) -> None:
        """Kill the whole tree. POSIX: the process group (still there while any member lives, even if the shell has
        exited). Windows: the job object; without one (``start`` can detach a program beyond any job, and
        ``taskkill /T`` needs the shell alive) only a still-running shell's tree can be reached."""
        proc = run.proc
        if proc is None:
            return
        with run.kill_lock:
            if run.job is not None:
                _job_terminate(run.job)
            if sys.platform != "win32" or proc.poll() is None:
                _kill_tree(proc)

    def _append(self, run: _Run, data: bytes) -> None:
        with run.lock:
            fh = run.fh
            if fh is None:
                return
            try:
                fh.seek(0, 2)
                fh.write(data)
                fh.flush()
                size = fh.tell()
                if size > LOG_CAP_BYTES:
                    self._shrink(run, size)
            except (OSError, ValueError):
                pass

    def _shrink(self, run: _Run, size: int) -> None:
        """Keep the head and the newest part of the rest, half the room each way, so it is not redone at once.

        Both cuts fall on line boundaries and a marker line says how much was dropped between them.
        """
        fh = run.fh
        if run.head_len is None:
            fh.seek(0)
            first = fh.read(self._head())
            nl = first.rfind(b"\n")
            run.head_len = nl + 1 if nl >= 0 else len(first)
            run.marker_len = 0
        head_len = run.head_len
        fh.seek(0)
        head = fh.read(head_len)
        region = head_len + run.marker_len          # where the kept tail starts in the file now
        keep = max((LOG_CAP_BYTES - head_len) // 2, 0)
        start = max(size - keep, region)
        fh.seek(start)
        tail = fh.read()
        if start > region:                           # begin the tail on a fresh line
            nl = tail.find(b"\n")
            cut = nl + 1 if nl >= 0 else 0
            tail = tail[cut:]
            start += cut
        run.dropped += start - region
        lead = b"" if (not head or head.endswith(b"\n")) else b"\n"
        marker = lead + f"[... {run.dropped} bytes of older output dropped ...]\n".encode("utf-8")
        run.marker_len = len(marker)
        fh.seek(0)
        fh.truncate()
        fh.write(head + marker + tail)
        fh.flush()

    # -- reading ------------------------------------------------------------------------------------------------

    def _run(self, id: str) -> _Run:
        run = self._runs.get(id)
        if run is None:
            raise KeyError(id)
        return run

    def _head(self) -> int:
        return min(LOG_HEAD_BYTES, LOG_CAP_BYTES // 4)

    def output(self, id: str, lines: int = 80) -> str:
        run = self._run(id)
        with run.lock:
            try:
                data = run.task.log.read_bytes()
            except OSError:
                data = b""
        text = strip_codes(data.decode("utf-8", errors="replace"))
        if lines and lines > 0:
            parts = text.split("\n")
            trailing = parts[-1] == ""
            body = parts[:-1] if trailing else parts
            text = "\n".join(body[-lines:]) + ("\n" if trailing and body else "")
        return text

    def read_from(self, id: str, offset: int) -> Tuple[str, int]:
        """Text written after logical byte ``offset`` and the new offset (offsets survive the log being trimmed).

        Only whole lines are returned, so a multi-byte character or an escape sequence is never cut between two
        calls; an unfinished last line waits for the next call unless the task has ended or it passed 64 KB.
        """
        run = self._run(id)
        with run.lock:
            try:
                data = run.task.log.read_bytes()
            except OSError:
                data = b""
            head_len, marker_len, dropped = run.head_len, run.marker_len, run.dropped
        if head_len is None:
            pos = offset
        elif offset < head_len:
            pos = offset
        elif offset < head_len + dropped:
            pos = head_len                  # the part wanted was dropped: start at the marker line
        else:
            pos = head_len + marker_len + (offset - head_len - dropped)
        pos = max(0, min(pos, len(data)))
        chunk = data[pos:]
        if not chunk:
            return "", offset
        nl = chunk.rfind(b"\n")
        if run.eof.is_set() or len(chunk) - (nl + 1) > 64 * 1024:
            take = chunk
        elif nl >= 0:
            take = chunk[:nl + 1]
        else:
            return "", offset
        end = pos + len(take)
        if head_len is None or end <= head_len:
            new_offset = end
        else:
            new_offset = end - marker_len + dropped
        return strip_codes(take.decode("utf-8", errors="replace")), new_offset

    # -- acting -------------------------------------------------------------------------------------------------

    def send(self, id: str, text: str) -> None:
        run = self._run(id)
        if run.task.kind != "terminal":
            raise ValueError(f"{id} is not a terminal")
        if run.task.state != "running" or run.proc is None or run.proc.stdin is None:
            raise ValueError(f"{id} is not running")
        self._append(run, f"$ {text}\n".encode("utf-8"))
        try:
            run.proc.stdin.write((text + "\n").encode("utf-8"))
            run.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise ValueError(f"{id} did not take the input: {e}")

    def interrupt(self, id: str) -> None:
        run = self._run(id)
        if run.task.state != "running" or run.proc is None:
            return
        try:
            if sys.platform == "win32":
                import signal
                run.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                import signal
                os.killpg(run.proc.pid, signal.SIGINT)
        except Exception:
            pass

    def stop(self, id: str) -> Task:
        run = self._run(id)
        with run.lock:
            if run.task.state != "running":
                return run.task
            run.task.state = "stopped"
            run.task.ended_at = time.time()
            run.stopping = True
        if run.proc is not None:
            self._kill(run)
            try:
                run.proc.wait(timeout=5)
            except Exception:
                pass
            if run.reader:
                run.reader.join(timeout=2)
            with run.lock:
                if run.task.exit_code is None and run.proc.returncode is not None:
                    run.task.exit_code = run.proc.returncode
        return run.task

    def stop_all(self) -> None:
        for tid in list(self._runs):
            try:
                self.stop(tid)
            except Exception:
                pass

    # -- the records --------------------------------------------------------------------------------------------

    def get(self, id: str) -> Optional[Task]:
        run = self._runs.get(id)
        return run.task if run else None

    def list(self) -> List[Task]:
        return [r.task for r in list(self._runs.values())]

    def clear_finished(self) -> int:
        n = 0
        with self._lock:
            for tid, run in list(self._runs.items()):
                if run.task.state != "running":
                    del self._runs[tid]
                    n += 1
        return n

    def running_count(self) -> int:
        return sum(1 for r in list(self._runs.values()) if r.task.state == "running")


_shared: Dict[Tuple[str, str], TaskManager] = {}
_shared_lock = threading.Lock()
_signals_installed = False


def _stop_every_manager() -> None:
    for mgr in list(_shared.values()):
        try:
            mgr.stop_all()
        except Exception:
            pass


SIGNAL_STOP_WAIT_SEC = 10.0


def _quietly(stop_all) -> None:
    try:
        stop_all()
    except Exception:
        pass


def _chained_handler(previous, stop_all=_stop_every_manager, timeout: float = SIGNAL_STOP_WAIT_SEC):
    """A signal handler that stops every task, then does what the previous handler would have done.

    The tasks are stopped on a helper thread, waited for at most ``timeout`` seconds: the handler runs on the main
    thread between two of its bytecodes, maybe while it holds a task's lock, so stopping them right here could wait
    on that lock forever."""
    def handler(signum, frame):
        worker = threading.Thread(target=_quietly, args=(stop_all,), name="agentx-stop-tasks", daemon=True)
        try:
            worker.start()
            worker.join(timeout)
        except Exception:
            pass
        if callable(previous):
            previous(signum, frame)
        elif previous is None or previous == signal.SIG_DFL:   # None: a handler not set from Python; the default
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        # SIG_IGN: the signal was being ignored, so carry on
    return handler


def _install_signal_handlers() -> None:
    """POSIX: SIGHUP and SIGTERM end the process without ``atexit``, and tasks live in their own sessions, so they
    would be orphaned. Stop them first. Main thread only (``signal.signal`` refuses elsewhere)."""
    global _signals_installed
    if _signals_installed or sys.platform == "win32":
        return
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        for name in ("SIGHUP", "SIGTERM"):
            sig = getattr(signal, name)
            signal.signal(sig, _chained_handler(signal.getsignal(sig)))
        _signals_installed = True
    except (ValueError, OSError):
        pass


def shared(home: Path, cwd: Path, permissions: Optional[Permissions] = None) -> TaskManager:
    """The process-wide manager for this project, made once; its tasks are stopped when agentx exits.

    ``permissions`` (the session's) shapes the environment tasks get, as for ``run_shell``; it is used when the
    manager is first created.
    """
    key = (str(Path(home).resolve()), str(Path(cwd).resolve()))
    with _shared_lock:
        mgr = _shared.get(key)
        if mgr is None:
            mgr = TaskManager(Path(home), Path(cwd), permissions=permissions)
            _shared[key] = mgr
            atexit.register(mgr.stop_all)
        # Each call from the main thread tries again (the first one may have come from another thread); once they
        # are installed this returns at once.
        _install_signal_handlers()
        return mgr
