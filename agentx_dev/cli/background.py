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
IDLE_RELEASE_SEC = 0.2               # read_from(release_idle=True): an unfinished line this quiet is handed out
STOP_WAIT_SEC = 5.0                  # stop() waits this long for the tree to be gone after killing it

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


def _escape_end(data: bytes, i: int) -> Optional[int]:
    """Where the escape sequence starting at ``data[i]`` (ESC) ends, or ``None`` when it is not complete yet."""
    n = len(data)
    if i + 1 >= n:
        return None
    kind = data[i + 1]
    if kind == 0x5B:                                   # CSI: parameters, intermediates, one final byte
        j = i + 2
        while j < n and 0x30 <= data[j] <= 0x3F:
            j += 1
        while j < n and 0x20 <= data[j] <= 0x2F:
            j += 1
        return j + 1 if j < n else None
    if kind == 0x5D:                                   # OSC: up to BEL or ESC \ (another ESC ends it as well)
        j = i + 2
        while j < n:
            if data[j] == 0x07:
                return j + 1
            if data[j] == 0x1B:
                if j + 1 >= n:
                    return None
                return j + 2 if data[j + 1] == 0x5C else j
            j += 1
        return None
    if kind in (0x28, 0x29):                           # a character set: ESC ( B
        return i + 3 if i + 2 < n else None
    return i + 2


def _utf8_end(data: bytes) -> int:
    """``len(data)``, less a UTF-8 character cut short at its end."""
    n = len(data)
    for back in range(1, min(4, n) + 1):
        byte = data[n - back]
        if byte & 0xC0 == 0x80:                        # a continuation byte: look further back for its lead
            continue
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            return n - back if back < need else n
        return n
    return n


def _releasable(pending: bytes) -> bytes:
    """The part of an unfinished line that can be handed out on its own: never into an escape sequence that is not
    complete yet, never half a UTF-8 character."""
    end, i = len(pending), 0
    while i < len(pending):
        if pending[i] != 0x1B:
            i += 1
            continue
        stop = _escape_end(pending, i)
        if stop is None:
            end = i
            break
        i = stop
    head = pending[:end]
    return head[:_utf8_end(head)]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_BASIC_ACCOUNTING = 1                     # JobObjectBasicAccountingInformation
_JOB_EXTENDED_LIMITS = 9                      # JobObjectExtendedLimitInformation


def _kernel32():
    """kernel32 with the job-object calls typed (Windows only)."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.c_void_p]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _job_structs():
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]
    return ExtendedLimits, Accounting


def _new_job(proc: "subprocess.Popen"):
    """Windows: a job object holding ``proc`` and everything it starts, so the tree stays reachable after the shell
    itself has exited (``taskkill /T`` needs the parent alive). It is set to kill everything in it when its last
    handle closes, and agentx keeps that handle until it exits: however agentx ends (a crash, Task Manager, the
    console window closed), Windows closes the handle and the tree goes with it. ``None`` elsewhere or when it
    cannot be made."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k = _kernel32()
        job = k.CreateJobObjectW(None, None)
        if not job:
            return None
        ExtendedLimits, _ = _job_structs()
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k.SetInformationJobObject(job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits), ctypes.sizeof(limits))
        if not k.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            k.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _job_kills_on_close(job) -> bool:
    """Whether ``job`` is set to kill its processes when its last handle closes."""
    try:
        import ctypes
        ExtendedLimits, _ = _job_structs()
        limits = ExtendedLimits()
        if not _kernel32().QueryInformationJobObject(job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                                     ctypes.sizeof(limits), None):
            return False
        return bool(limits.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    except Exception:
        return False


def _job_active(job) -> int:
    """How many processes of ``job`` are alive (0 when it cannot be read)."""
    try:
        import ctypes
        _, Accounting = _job_structs()
        info = Accounting()
        if not _kernel32().QueryInformationJobObject(job, _JOB_BASIC_ACCOUNTING, ctypes.byref(info),
                                                     ctypes.sizeof(info), None):
            return 0
        return int(info.ActiveProcesses)
    except Exception:
        return 0


def _job_terminate(job) -> None:
    try:
        _kernel32().TerminateJobObject(job, 1)
    except Exception:
        pass


def _job_close(job) -> None:
    try:
        _kernel32().CloseHandle(job)
    except Exception:
        pass


# POSIX: why the shell is not reaped as soon as it exits. A task's shell leads a process group and a session of its
# own (``start_new_session``), so the group's id is the shell's pid. Once the shell is reaped, that number is free:
# when the group's last member ends, the system may give it to an unrelated program, which may lead a group of its
# own with it, and a later ``killpg`` would kill that program. While the shell is an unreaped zombie its pid is
# still taken, so no other group can have that id, and only the shell's own descendants can be in its session. So
# the shell is reaped only once nothing in its group runs, and ``killpg`` is sent only before that.

_PS = "/bin/ps" if os.path.exists("/bin/ps") else "ps"


def _group_members(pgid: int) -> Optional[List[int]]:
    """POSIX: the pids in process group ``pgid`` that still run (zombies do not count); ``None`` when that cannot be
    told. Linux reads ``/proc``; elsewhere (macOS) ``ps`` is asked."""
    found: List[int] = []
    if os.path.isdir("/proc/self"):
        try:
            names = os.listdir("/proc")
        except OSError:
            names = None
        if names is not None:
            for name in names:
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", "rb") as f:
                        data = f.read()
                except OSError:
                    continue                    # ended while the list was read
                fields = data[data.rfind(b")") + 2:].split()     # after "(command)": state, ppid, pgrp, ...
                try:
                    if len(fields) >= 3 and int(fields[2]) == pgid and fields[0] not in (b"Z", b"X", b"x"):
                        found.append(int(name))
                except ValueError:
                    continue
            return found
    try:
        done = subprocess.run([_PS, "-A", "-o", "pid=,pgid=,stat="], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(pgid) and not parts[2].startswith("Z") and parts[0].isdigit():
            found.append(int(parts[0]))
    return found


def _group_has_live(pgid: int) -> Optional[bool]:
    """POSIX: whether process group ``pgid`` has a member that still runs; ``None`` when that cannot be told."""
    members = _group_members(pgid)
    return None if members is None else bool(members)


class _Reaped(Exception):
    """The shell was reaped by something other than its ``Popen`` (its id is no longer held)."""


def _peek_exit(pid: int) -> Optional[int]:
    """POSIX: the exit code of child ``pid`` if it has ended, read without reaping it; ``None`` while it runs or
    where the system cannot do that (``os.waitid`` is missing: macOS before Python 3.13). Codes follow ``Popen``: a
    signal is its negative number. Raises ``_Reaped`` when ``pid`` is no longer a child to wait for."""
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        return None
    try:
        info = waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        raise _Reaped()
    if info is None or info.si_pid == 0:
        return None
    return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status


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
        self.job = None               # Windows job object holding the whole tree (kept open until agentx exits)
        self.pgid: Optional[int] = None   # POSIX: the process group this run started, while its id is still held
                                          # (the shell not yet reaped); None once it is not (see _group_has_live)
        self.kill_lock = threading.Lock()
        self.eof = threading.Event()  # the output pipe has closed: nothing more will be written
        self.reader: Optional[threading.Thread] = None
        self.wrote_at = 0.0           # monotonic time of the last write to the log


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
        self._retired: List[_Run] = []       # cleared from the list, but something of them may still run
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
            if sys.platform != "win32":
                run.pgid = proc.pid          # start_new_session: the shell leads a group of its own
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
        if sys.platform == "win32":
            code: Optional[int] = run.proc.wait()
        else:
            code = self._wait_unreaped(run)
        if code is not None:
            with run.lock:
                run.task.exit_code = code    # the shell's own code; the task stays running while anything holds the pipe
        if run.reader:
            run.reader.join()                # a descendant (``cmd &``) may keep the pipe, and the task, alive
        if sys.platform != "win32":
            self._reap_if_done(run)          # the usual case: nothing is left, the shell is reaped now
        with run.lock:
            if run.task.ended_at is None:
                run.task.ended_at = time.time()
            if run.task.state == "running":
                run.task.state = "stopped" if run.stopping else "exited"
        try:
            if run.proc.stdin:
                run.proc.stdin.close()
        except Exception:
            pass
        # The job (Windows) stays open: a program that wrote its output elsewhere (``npm run dev > dev.log &``)
        # held no pipe and may still run; stop() and stop_all() reach it through the job.
        with run.lock:
            if run.fh is not None:
                run.fh.close()
                run.fh = None
        if sys.platform != "win32":
            delay = 0.1                      # a program the task left running (``> dev.log &``): reap once it ends
            while not self._reap_if_done(run):
                time.sleep(delay)
                delay = min(delay * 2, 5.0)

    def _wait_unreaped(self, run: _Run) -> Optional[int]:
        """POSIX: wait for the shell to exit without reaping it, so its id (the group's) stays held; its exit code,
        or ``None`` where it cannot be read without reaping (then it is set when the shell is reaped)."""
        delay = 0.02
        while True:
            with run.kill_lock:
                if run.proc.returncode is not None:      # reaped already (by _reap_if_done, or a caller's poll())
                    return run.proc.returncode
                if run.pgid is None:
                    return None
                try:
                    code = _peek_exit(run.proc.pid)
                except _Reaped:
                    run.pgid = None                      # reaped behind Popen's back: the id is no longer ours
                    return None
            if code is not None:
                return code
            if getattr(os, "waitid", None) is None:
                return None                              # cannot tell without reaping: _reap_if_done does it later
            time.sleep(delay)
            delay = min(delay * 1.5, 0.5)

    def _reap_if_done(self, run: _Run) -> bool:
        """POSIX: reap the shell once nothing in its group runs any more, and stop treating the group id as ours;
        whether that has happened (``True``: nothing of the tree is left)."""
        proc = run.proc
        if proc is None:
            return True
        with run.kill_lock:
            if run.pgid is None:
                return True
            if proc.returncode is not None:              # someone else reaped it: the id is no longer held
                run.pgid = None
            else:
                if _group_has_live(run.pgid):
                    return False
                # Nothing in the group runs (or that cannot be read): poll() reaps a finished shell, and leaves one
                # that still runs alone (only possible when the group could not be read).
                if proc.poll() is None:
                    return False
                run.pgid = None
        with run.lock:
            if run.task.exit_code is None:
                run.task.exit_code = proc.returncode
        return True

    def _kill(self, run: _Run) -> None:
        """Kill the whole tree, whether or not the shell is still there. Windows: the job object (and ``taskkill
        /T`` while the shell runs, for a tree that has no job). POSIX: the shell's process group, as long as the
        shell is not reaped (so the group id cannot belong to anything else)."""
        proc = run.proc
        if proc is None:
            return
        with run.kill_lock:
            if sys.platform == "win32":
                if run.job is not None:
                    _job_terminate(run.job)
                if proc.poll() is None:
                    _kill_tree(proc)
            elif run.pgid is not None and proc.returncode is None:
                try:
                    os.killpg(run.pgid, signal.SIGKILL)
                except OSError:
                    pass

    def _alive(self, run: _Run) -> bool:
        """Whether anything of ``run``'s tree still runs: the shell, or (Windows) a process in its job, or (POSIX) a
        member of the group the shell led. POSIX: a tree found gone has its shell reaped here."""
        proc = run.proc
        if proc is None:
            return False
        if sys.platform != "win32":
            return not self._reap_if_done(run)
        if proc.poll() is None:
            return True
        if run.job is not None:
            return _job_active(run.job) > 0
        return False

    def _kill_and_wait(self, run: _Run) -> bool:
        """Kill ``run``'s tree and wait up to ``STOP_WAIT_SEC`` for it to be gone; whether it is."""
        self._kill(run)
        deadline = time.monotonic() + STOP_WAIT_SEC
        while self._alive(run) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self._alive(run)

    def _append(self, run: _Run, data: bytes) -> None:
        with run.lock:
            fh = run.fh
            if fh is None:
                return
            try:
                fh.seek(0, 2)
                fh.write(data)
                fh.flush()
                run.wrote_at = time.monotonic()
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

    def read_from(self, id: str, offset: int, release_idle: bool = False) -> Tuple[str, int]:
        """Text written after logical byte ``offset`` and the new offset (offsets survive the log being trimmed).

        Only whole lines are returned, so a multi-byte character or an escape sequence is never cut between two
        calls; an unfinished last line waits for the next call unless the task has ended or it passed 64 KB.
        ``release_idle`` (the window): an unfinished line nothing was written to for ``IDLE_RELEASE_SEC`` (a prompt
        such as ``Name: ``) is handed out too, up to a whole character and a whole escape sequence.
        """
        run = self._run(id)
        with run.lock:
            try:
                data = run.task.log.read_bytes()
            except OSError:
                data = b""
            head_len, marker_len, dropped = run.head_len, run.marker_len, run.dropped
            quiet = time.monotonic() - run.wrote_at >= IDLE_RELEASE_SEC
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
        else:
            take = chunk[:nl + 1]
            if release_idle and quiet and len(take) < len(chunk):
                take += _releasable(chunk[len(take):])
            if not take:
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
                run.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                # To what runs in the terminal, not to its shell: a shell without job control that gets SIGINT
                # ends, and the terminal with it. Only while the group id is still held (see _group_has_live).
                with run.kill_lock:
                    if run.pgid is not None and run.proc.returncode is None:
                        for pid in _group_members(run.pgid) or []:
                            if pid != run.proc.pid:
                                try:
                                    os.kill(pid, signal.SIGINT)
                                except OSError:
                                    pass                  # ended meanwhile
        except Exception:
            pass

    def leftovers(self, id: str) -> bool:
        """Whether a task that is no longer running left something running (a program that wrote its output to a
        file, so no pipe kept the task open). ``KeyError`` for an unknown id."""
        run = self._run(id)
        return run.task.state != "running" and self._alive(run)

    def stop(self, id: str) -> Task:
        """Stop ``id``'s whole tree. A running task is marked ``stopped`` once the kill worked; a kill that failed
        leaves it ``running`` (and ``stop_all`` tries again at exit). A task that already ended keeps its state, and
        what it left running is killed."""
        run = self._run(id)
        with run.lock:
            running = run.task.state == "running"
            if running:
                run.stopping = True
            if running and run.proc is None:            # not spawned yet: _launch kills it as soon as it is
                run.task.state = "stopped"
                run.task.ended_at = time.time()
                return run.task
        if not running:
            if self._alive(run):
                self._kill_and_wait(run)
            return run.task
        gone = self._kill_and_wait(run)
        if gone and run.reader:
            run.reader.join(timeout=2)
        with run.lock:
            if run.task.exit_code is None and run.proc.returncode is not None:
                run.task.exit_code = run.proc.returncode
            if gone:
                if run.task.state == "running":
                    run.task.state = "stopped"
                if run.task.ended_at is None:
                    run.task.ended_at = time.time()
            elif run.task.state == "running":
                run.stopping = False                    # it did not stop; if it ends by itself it "exited"
        return run.task

    def stop_all(self) -> None:
        """Stop every task and terminal, and kill whatever any of them left running, whatever its state (at exit,
        and ``/tasks stop all``)."""
        for run in list(self._runs.values()) + list(self._retired):
            try:
                if run.task.state == "running" and self._runs.get(run.task.id) is run:
                    self.stop(run.task.id)
                elif self._alive(run):
                    self._kill_and_wait(run)
            except Exception:
                pass

    # -- the records --------------------------------------------------------------------------------------------

    def get(self, id: str) -> Optional[Task]:
        run = self._runs.get(id)
        return run.task if run else None

    def list(self) -> List[Task]:
        return [r.task for r in list(self._runs.values())]

    def clear_finished(self) -> int:
        """Drop the tasks that are not running from the list. One that left something running is kept aside, so
        ``stop_all`` still reaches it."""
        n = 0
        with self._lock:
            for tid, run in list(self._runs.items()):
                if run.task.state != "running":
                    del self._runs[tid]
                    n += 1
                    if self._alive(run):
                        self._retired.append(run)
                    elif run.job is not None:
                        with run.kill_lock:
                            _job_close(run.job)
                            run.job = None
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


def install_signal_handlers() -> bool:
    """POSIX: SIGHUP and SIGTERM end the process without ``atexit``, and tasks live in their own sessions, so they
    would be orphaned. Stop them first. Main thread only (``signal.signal`` refuses elsewhere); a no-op on Windows,
    off the main thread, and once installed. Safe before any manager exists: the handler stops the managers there
    are when the signal comes. ``agentx --ui`` calls it on the main thread, as its app is built on another one.
    Whether the handlers are installed now."""
    global _signals_installed
    if _signals_installed or sys.platform == "win32":
        return _signals_installed
    if threading.current_thread() is not threading.main_thread():
        return False
    try:
        for name in ("SIGHUP", "SIGTERM"):
            sig = getattr(signal, name)
            signal.signal(sig, _chained_handler(signal.getsignal(sig)))
        _signals_installed = True
    except (ValueError, OSError):
        pass
    return _signals_installed


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
        install_signal_handlers()
        return mgr
