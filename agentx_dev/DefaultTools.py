"""Built-in file / folder / code-execution tools with capability-based permissions.

Most agents need to read files, write files, and execute code. Every user has
been hand-rolling these tools — same code, same bugs (no path-traversal
guards, no timeouts, no output caps). This module ships the canonical
implementations behind a permission gate so users opt in explicitly:

    from agentx_dev import AgentRunner, AgentType, DefaultTools, Permissions

    perms = Permissions(
        read_files=True,
        list_directories=True,
        write_files=True,
        edit_files=False,           # not granted → tool not registered
        execute_python=False,       # not granted → tool not registered
        allowed_paths=["./workspace"],   # all file ops constrained to this subtree
        python_timeout_sec=10,
    )
    tools = DefaultTools.build(perms)
    runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=tools)

Security model:

  - Default ``Permissions()`` denies everything. Opt-in only.
  - ``allowed_paths`` constrains every file operation to one of the listed
    subtrees. ``../`` traversal is blocked because ``Path.resolve()`` flattens
    them before the subtree check.
  - ``execute_python`` runs the snippet in a fresh subprocess with a wall-clock
    timeout and a max-output-bytes cap so a runaway script can't lock the
    agent or fill memory with output.
  - Tools enforce their permission flag at every call, not just at
    registration time. Flipping ``perms.write_files = False`` mid-run blocks
    subsequent writes, even though the tool was registered.
  - Tools that aren't permitted are NOT registered with the runner by default
    (``include_denied=False``). The agent never sees them as options.
    Pass ``include_denied=True`` to register every default tool — denied ones
    return a clean "access denied" error when called.

What this does NOT defend against:

  - A malicious user-defined tool. ``Permissions`` only governs the built-ins.
  - Resource exhaustion in non-Python tools (file reads of huge files —
    ``max_file_size_bytes`` is enforced for write, not read).
  - Network access from inside the Python subprocess. There's no network
    sandbox; that would need OS-level firewall rules.
  - Cryptographic-grade isolation. The subprocess inherits this process's
    environment variables; sensitive API keys in ``os.environ`` are visible
    to executed code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from agentx_dev.Tools import StructuredTool


# Convention matches Claude Code's .claude/settings.local.json — project-local
# config that the user discovers naturally and edits once per project. The
# leading dot keeps it out of casual listings; .agentx/ is a directory so we
# can grow (workspaces, allowlists, per-tool overrides) without renaming.
DEFAULT_PERMISSIONS_PATH = Path(".agentx") / "permissions.json"


def _mint_session_dir(
    base: str,
    *,
    prefix: str = "run_",
    session_id: Optional[str] = None,
) -> Path:
    """Create a fresh subdirectory under ``base`` and return its absolute
    path. Used by ``Permissions.new_session`` to give every run its own
    non-clobbering workspace.

    Name shape: ``<prefix><UTC timestamp>_<8-char id>``. The timestamp
    makes runs sortable; the random suffix guarantees uniqueness even
    when two runs mint sessions in the same second.

    Args:
        base: Parent directory. Created if it doesn't exist.
        prefix: Prefix on the generated name.
        session_id: Optional override — if given, that literal name is
            used (still under ``base``). Useful for reproducing a
            specific run's output layout in tests.

    Returns:
        Absolute Path to the created directory.
    """
    from datetime import datetime, timezone
    import re as _re
    import uuid

    base_path = Path(base).resolve()
    base_path.mkdir(parents=True, exist_ok=True)

    if session_id is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        short = uuid.uuid4().hex[:8]
        name = f"{prefix}{stamp}_{short}"
    else:
        # Strict validation: block path separators, traversal, drive
        # letters, null bytes, and control characters. Without this a
        # caller who plumbs an untrusted value through (e.g. a job id
        # from a web request) could pass "../../../tmp/pwn" and mint a
        # directory OUTSIDE `base` — which Permissions.new_session
        # then sets as both workspace AND allowed_paths, giving the
        # agent full access to a foreign directory. Regex mirrors what
        # is safe on both POSIX and Windows filesystems.
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(
                "session_id must be a non-empty string; "
                f"got {type(session_id).__name__}"
            )
        if not _re.fullmatch(r"[A-Za-z0-9._-]{1,120}", session_id):
            raise ValueError(
                f"session_id {session_id!r} rejected — allowed characters "
                f"are letters, digits, '.', '_' , '-' (max 120 chars). "
                f"Path separators, '..', ':', null bytes and control "
                f"characters are blocked to prevent sandbox escape."
            )
        if session_id in (".", "..") or session_id.startswith("."):
            # A leading dot on Windows is legal filesystem-wise but
            # creates hidden dirs — an unlikely-legitimate session id.
            # ".." alone is caught by the regex already; guard the
            # explicit "." too so we can't mint the base itself.
            raise ValueError(
                f"session_id {session_id!r} rejected — must not equal "
                f"'.' or '..' and must not start with '.'"
            )
        name = session_id

    session_dir = base_path / name
    # Belt-and-braces: after joining, the resolved path MUST still be
    # inside base_path. Redundant given the regex above but cheap and
    # closes any future gap if the regex is relaxed.
    resolved = session_dir.resolve()
    try:
        resolved.relative_to(base_path)
    except ValueError:
        raise ValueError(
            f"session_id {session_id!r} would resolve outside base "
            f"({base_path}) — refusing to mint session dir."
        )
    session_dir.mkdir(parents=True, exist_ok=False)
    return session_dir


def mint_session_dir(
    base: str = "./workspace",
    *,
    prefix: str = "run_",
    session_id: Optional[str] = None,
) -> str:
    """Public helper: mint a fresh session directory and return its
    absolute path as a string.

    Use when you want the fresh directory but plan to build the
    Permissions object yourself (e.g. sharing one session dir across
    multiple specialists in a Supervisor). If you just want a
    ready-to-use Permissions, call ``Permissions.new_session()``
    instead — it wraps this."""
    return str(_mint_session_dir(base, prefix=prefix, session_id=session_id))


@dataclass
class Permissions:
    """Capability flags for the built-in tools.

    Default is deny-all — every flag must be set to True explicitly. Sandbox
    constraints (``allowed_paths``, timeouts, size caps) tighten the granted
    capabilities further.
    """

    # File operations
    read_files: bool = False
    list_directories: bool = False
    write_files: bool = False
    edit_files: bool = False
    delete_files: bool = False
    move_files: bool = False

    # Code execution
    execute_python: bool = False
    # Shell command execution (bash on Unix, git-bash or powershell on
    # Windows). SIGNIFICANTLY more dangerous than execute_python because
    # once granted the model has arbitrary access to the host command
    # line — allowed_paths becomes largely advisory (bash can cd, use
    # absolute paths, spawn subshells). Grant only to trusted agents.
    execute_shell: bool = False

    # Sandbox: every file operation is constrained to one of these subtrees.
    # ``None`` means no restriction — DO NOT USE in production with untrusted
    # LLM output; an agent with no path constraint can rm -rf you.
    allowed_paths: Optional[List[str]] = None

    # Primary workspace root. When set, file tools accept SHORT relative paths
    # (e.g. "inspect.py") and auto-resolve them inside this directory —
    # write_file(path="inspect.py") writes to workspace/inspect.py. Full or
    # already-inside-workspace paths ("./workspace/inspect.py",
    # "workspace/subdir/x.py") pass through without double-nesting. Absolute
    # paths are honored as-is (subject to allowed_paths).
    #
    # If workspace is set and allowed_paths is None, allowed_paths defaults
    # to [workspace] so the sandbox is automatically the workspace root.
    workspace: Optional[str] = None

    # Resource caps
    python_timeout_sec: float = 10.0
    python_max_output_bytes: int = 100_000
    shell_timeout_sec: float = 30.0
    shell_max_output_bytes: int = 40_000
    max_file_size_bytes: int = 10 * 1024 * 1024   # 10 MB

    # When True, ``run_python`` gains a persistent ``state`` dict that
    # survives across calls. Backed by a pickle file inside the workspace
    # so the subprocess model (isolation + timeout enforcement) is kept.
    # Prevents the "re-define python_files every call" waste seen in
    # multi-step analysis tasks. Only picklable objects survive round
    # trips; non-picklable values get dropped with a note in the reply.
    python_persistent_state: bool = False

    # Auto-create workspace/allowed_paths directories if they don't exist
    # yet. Default True so agents Just Work — the alternative is every
    # user rediscovering the same "why does write_file say path doesn't
    # exist" issue and sprinkling Path(...).mkdir() around their entry
    # points. Set False when you want strict path checking (e.g. a
    # production job that must fail loudly if the ops team didn't
    # provision the sandbox).
    auto_create_paths: bool = True

    # Environment variables to expose to run_python / run_shell
    # subprocesses. By default the framework passes a MINIMAL scrubbed
    # env (PATH, HOME, LANG, TEMP, PYTHONIOENCODING, PYTHONUTF8, plus
    # SystemRoot on Windows) instead of the full parent env. This
    # prevents `print(os.environ)` from an execute_python agent
    # exfiltrating OPENAI_API_KEY / ANTHROPIC_API_KEY / AWS_* / DB URLs
    # that happened to be set on the host.
    #
    # Add names to this list to whitelist specific vars — e.g.
    # ``subprocess_env_passthrough=["OPENAI_API_KEY"]`` if the agent
    # legitimately needs to call OpenAI from inside run_python. Setting
    # this to the sentinel string ``"*"`` (as the ONLY entry) restores
    # the pre-3.0.6 behaviour of full-env passthrough — use only when
    # you fully trust the agent AND the host has no other secrets.
    subprocess_env_passthrough: Optional[List[str]] = None

    def __post_init__(self):
        """Create workspace + allowed_paths directories that don't yet
        exist. Runs after every constructor path (positional, keyword,
        classmethod, from_file). Silently ignores paths that already
        exist as files (that's an error for later — mkdir would raise
        the wrong exception). Paths that fail to create (permissions,
        readonly fs) are left alone — the first tool call will surface
        a clean error instead of blowing up construction."""
        if not self.auto_create_paths:
            return
        candidates: List[str] = []
        if self.workspace:
            candidates.append(self.workspace)
        if self.allowed_paths:
            candidates.extend(self.allowed_paths)
        seen: set = set()
        for p in candidates:
            if not p or p in seen:
                continue
            seen.add(p)
            try:
                target = Path(p)
                if target.exists():
                    continue
                target.mkdir(parents=True, exist_ok=True)
            except (OSError, PermissionError):
                # A tool call that actually needs this path will fail
                # cleanly with an "access denied" or "does not exist"
                # error. Construction is not the place to raise.
                continue

    def effective_allowed_paths(self) -> Optional[List[str]]:
        """Where file ops may go. Explicit ``allowed_paths`` wins; if
        that's None but ``workspace`` is set, the workspace is used;
        otherwise unrestricted (None)."""
        if self.allowed_paths is not None:
            return self.allowed_paths
        if self.workspace is not None:
            return [self.workspace]
        return None

    # ------------------------------------------------------------------
    # Convenience presets — picked for the common use cases. Each one is
    # an honest opinion about what's safe; callers can still build their
    # own Permissions(...) from scratch.
    # ------------------------------------------------------------------

    @classmethod
    def deny_all(cls) -> "Permissions":
        """The default. Every capability denied."""
        return cls()

    @classmethod
    def read_only(cls, allowed_paths: Optional[List[str]] = None) -> "Permissions":
        """Agent can read files + list directories. No writes, no exec."""
        return cls(
            read_files=True,
            list_directories=True,
            allowed_paths=allowed_paths,
        )

    @classmethod
    def full_access(cls, allowed_paths: List[str]) -> "Permissions":
        """Read/write/edit/delete + Python exec, all constrained to the
        listed paths. Suitable for an agent working inside a project
        directory.

        Named ``full_access`` (not ``workspace``) because ``workspace`` is
        a dataclass field on this class — a classmethod with the same
        name would shadow the field's default value at class-body
        evaluation time and every instance would come out with a bound-
        method value in place of the string path. Yes, that bit us once."""
        return cls(
            read_files=True,
            list_directories=True,
            write_files=True,
            edit_files=True,
            delete_files=True,
            move_files=True,
            execute_python=True,
            allowed_paths=list(allowed_paths),
        )

    @classmethod
    def new_session(
        cls,
        base: str = "./workspace",
        *,
        prefix: str = "run_",
        session_id: Optional[str] = None,
        **kwargs,
    ) -> "Permissions":
        """Mint a fresh per-run subdirectory under ``base`` and return a
        Permissions object scoped to it.

        Purpose: when you run the same agent multiple times (batch jobs,
        parallel Supervisor sub-tasks, or just repeat testing) every run
        writes to a distinct directory, so no run can clobber another's
        ``report.md`` / output files.

        The minted directory is set as BOTH ``workspace`` (write-relative
        paths land there by default) AND ``allowed_paths`` (nothing can
        escape the session). Existing sibling runs stay untouched.

        Args:
            base: Parent directory the session subdir goes under. Created
                if missing. Default ``./workspace``.
            prefix: Prefix on the generated session-dir name. Default
                ``"run_"`` — final name looks like ``run_20260717T143022_a3f2``.
            session_id: Force a specific session id instead of generating
                one (useful for tests / reproducing a specific run).
            **kwargs: Any Permissions field. Sensible defaults for a
                working agent: read/write/edit/list files, execute_python.
                Override to widen or narrow. Do NOT pass ``allowed_paths``
                or ``workspace`` — they'd fight the whole point of this
                method.

        Returns:
            Permissions instance with ``workspace`` and ``allowed_paths``
            set to the newly-created session directory (absolute path
            string), plus whatever capability flags kwargs specified.

        Raises:
            TypeError: If ``allowed_paths`` or ``workspace`` is passed
                via kwargs — those are what this method OWNS.
        """
        if "allowed_paths" in kwargs or "workspace" in kwargs:
            raise TypeError(
                "Permissions.new_session() sets allowed_paths and workspace "
                "itself — don't pass them as kwargs. Change `base` to point "
                "at a different parent directory instead."
            )

        # Reasonable defaults for an agent that needs to work inside a
        # session dir. Every one is overridable via kwargs.
        defaults = dict(
            read_files=True,
            list_directories=True,
            write_files=True,
            edit_files=True,
            execute_python=True,
        )
        defaults.update(kwargs)

        session_dir = _mint_session_dir(base, prefix=prefix, session_id=session_id)
        return cls(
            allowed_paths=[str(session_dir)],
            workspace=str(session_dir),
            **defaults,
        )

    # ------------------------------------------------------------------
    # File-backed config — auto-create on first read so a fresh project
    # gets a discoverable .agentx/permissions.json the user can edit
    # once. Same shape as Claude Code's .claude/settings.local.json.
    # ------------------------------------------------------------------

    @classmethod
    def from_file(
        cls,
        path: Optional[Path] = None,
        *,
        create_if_missing: bool = True,
    ) -> "Permissions":
        """Load permissions from a JSON config file.

        Default path is ``./.agentx/permissions.json`` (project-local —
        matches the Claude Code convention so users familiar with that
        find it naturally). If the file doesn't exist and
        ``create_if_missing=True`` (default), writes a deny-all template
        the user can edit. Subsequent runs load whatever they've set.

        Raises ``FileNotFoundError`` when the file is missing and
        ``create_if_missing=False`` — useful for production where you
        want startup to fail loudly if config wasn't deployed.

        Raises ``ValueError`` on unknown field names so a typo
        (``read_filez: true``) fails fast instead of silently granting
        nothing.
        """
        target = Path(path) if path is not None else DEFAULT_PERMISSIONS_PATH
        if not target.exists():
            if not create_if_missing:
                raise FileNotFoundError(
                    f"Permissions config not found: {target}. "
                    f"Pass create_if_missing=True to auto-generate a template."
                )
            cls._write_default_template(target)
        return cls._load_from_path(target)

    @classmethod
    def _write_default_template(cls, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        defaults = cls.deny_all()
        body = asdict(defaults)
        # Prepend a guidance block so a user opening this file for the
        # first time knows what to do without reading docs.
        out = {
            "_comment": (
                "Edit this file to control which capabilities the agent has. "
                "Set any *_files / *_directories / execute_python flag to true "
                "to grant. Constrain file ops with allowed_paths (list of "
                "filesystem subtrees; ../ traversal is blocked). Resource "
                "caps protect against runaway tools."
            ),
            "_docs": "https://github.com/shadrach098/Bruce_framework",
            **body,
        }
        _write_json_secure(path, out)

    @classmethod
    def _load_from_path(cls, path: Path) -> "Permissions":
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Permissions config at {path} is not valid JSON: {e}"
                ) from e
        if not isinstance(data, dict):
            raise ValueError(
                f"Permissions config at {path} must be a JSON object, "
                f"got {type(data).__name__}"
            )
        # Strip metadata keys (anything starting with underscore — comment,
        # docs link, version, future house-keeping fields).
        clean = {k: v for k, v in data.items() if not k.startswith("_")}
        valid = {f.name for f in fields(cls)}
        unknown = set(clean) - valid
        if unknown:
            raise ValueError(
                f"Unknown fields in {path}: {sorted(unknown)}. "
                f"Valid: {sorted(valid)}. Fix the typo or remove the field."
            )
        return cls(**clean)

    def save(self, path: Optional[Path] = None) -> Path:
        """Persist this Permissions instance to disk. Useful after
        mutating a loaded config programmatically (e.g. ops dashboards
        that toggle capabilities). Atomic via tmp + rename so a crashed
        write can't corrupt the file. File is created with mode 0o600
        so another local user can't pre-create / read / replace it."""
        target = Path(path) if path is not None else DEFAULT_PERMISSIONS_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_json_secure(target, asdict(self))
        return target


# --- Secure JSON writer -----------------------------------------------------

def _write_json_secure(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` atomically and with mode 0o600.

    Uses ``os.open(..., O_CREAT|O_WRONLY|O_TRUNC, 0o600)`` so no other
    local user can pre-create the file, and re-chmods after replace to
    fix the case where the file already existed with a wider mode. The
    mode arg is a no-op on Windows (ignored by CRT) but the atomic
    tmp+replace pattern still applies.

    This is the fix for a permissions.json hijack: on a multi-user
    system, another user could otherwise pre-create the config with
    execute_python=True at the target's expected path and win the
    write race — the target's next launch would grant capabilities it
    never intended."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))
    # Re-tighten mode after replace in case ``path`` pre-existed with a
    # wider mode. Ignored on Windows (chmod there only toggles read-only).
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


# --- Subprocess env scrubbing -----------------------------------------------

# Minimum env vars every child needs to run at all. Anything not in this
# set (or in Permissions.subprocess_env_passthrough) is dropped. The list
# is deliberately conservative — Windows-specific vars are included so
# subprocess can locate DLLs / temp / user profile paths, and the two
# PYTHON* vars match what run_python/run_shell were setting already.
_BASE_ENV_KEEP = {
    # Unix + Windows common
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "TMP", "TEMP",
    "PYTHONIOENCODING", "PYTHONUTF8",
    # Windows-specific — required for subprocess creation on Windows;
    # dropping SystemRoot breaks CreateProcess.
    "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "windir", "WINDIR",
}


def _build_child_env(perms: Permissions) -> dict:
    """Return the environment to hand to a run_python / run_shell
    subprocess. Default policy: scrubbed — only vars in _BASE_ENV_KEEP
    plus whatever the caller whitelisted via
    ``Permissions.subprocess_env_passthrough``. This is the fix for env
    exfiltration by ``print(os.environ)`` inside execute_python.

    Backdoor: passing the single-item list ``["*"]`` restores full-env
    passthrough — used by callers who explicitly opted into the old
    behaviour after auditing their host env. Always union in the two
    UTF-8 vars we set unconditionally."""
    passthrough = perms.subprocess_env_passthrough
    if passthrough is not None and len(passthrough) == 1 and passthrough[0] == "*":
        base = dict(os.environ)
    else:
        base = {k: v for k, v in os.environ.items() if k in _BASE_ENV_KEEP}
        if passthrough:
            for name in passthrough:
                if name in os.environ:
                    base[name] = os.environ[name]
    # These two are set by us regardless of caller config — they force
    # UTF-8 across the whole subprocess and match the run_python /
    # run_shell UTF-8 hardening documented in-code.
    base.setdefault("PYTHONIOENCODING", "utf-8")
    base["PYTHONIOENCODING"] = "utf-8"
    base["PYTHONUTF8"] = "1"
    return base


# --- Per-session read tracker -----------------------------------------------

import threading as _threading


class _ReadTracker:
    """Remembers which files the agent has read this session.

    Same safety pattern Claude Code enforces: you must read a file before
    you can edit / overwrite it. This protects against clobbering unknown
    content — the LLM can't 'assume' what's in a file it hasn't seen and
    then blow it away. Applies to writes too: overwriting an EXISTING
    file requires a prior read; creating a NEW file doesn't (there's
    nothing to protect yet).

    One tracker per DefaultTools.build() call, shared across read /
    write / edit tools built from the same call so state is coherent.
    Thread-safe via an internal Lock.
    """

    def __init__(self):
        self._read: set = set()
        self._lock = _threading.Lock()

    @staticmethod
    def _key(resolved: Path) -> str:
        # str(Path.resolve()) is a stable canonical form even after
        # symlink resolution + case normalization on Windows.
        return str(resolved)

    def mark_read(self, resolved: Path) -> None:
        with self._lock:
            self._read.add(self._key(resolved))

    def has_read(self, resolved: Path) -> bool:
        with self._lock:
            return self._key(resolved) in self._read

    def forget(self, resolved: Path) -> None:
        """Clear one entry — used after delete/move so a subsequent
        write can't accidentally think the file is still 'known'."""
        with self._lock:
            self._read.discard(self._key(resolved))


# --- Path validation ---------------------------------------------------------

def _is_inside_any(resolved: Path, allowed: Optional[List[str]]) -> bool:
    """True if ``resolved`` is inside any entry of ``allowed``.
    ``allowed=None`` is treated as "no constraint" and returns True so
    callers can use a single expression."""
    if allowed is None:
        return True
    for a in allowed:
        try:
            resolved.relative_to(Path(a).resolve())
            return True
        except ValueError:
            continue
    return False


def _resolve_for_ops(target: str, perms: Permissions) -> Path:
    """Resolve ``target`` for a file operation, honoring workspace AND
    additional allowed_paths that live outside the workspace.

    Rules (in order):
      1. Absolute path → use as-is (still subject to allowed_paths).
      2. Explicit-relative prefix (starts with ./ ../ .\\ ..\\) + the
         CWD-resolved form lands inside one of the allowed subtrees →
         use the CWD-resolved form. This is what makes
         ``find_files(path="./agentx_dev")`` work when workspace is
         ``./workspace`` and allowed_paths contains ``./agentx_dev``
         as a sibling. Without this rule, workspace-relative resolution
         would produce ``./workspace/agentx_dev`` which doesn't exist.
      3. Otherwise, if workspace is set: try workspace + target first.
         If that lands in the workspace (or any allowed subtree), use it
         — that's the short-name case ``write_file(path="report.md")``.
      4. Fall back to CWD-relative resolution if THAT lands inside any
         allowed subtree — handles already-qualified paths like
         ``./workspace/inspect.py`` when workspace=./workspace.
      5. Give up and let the sandbox check reject via the same code
         path as any other out-of-sandbox path.

    Then apply the sandbox check.

    Handles the common styles cleanly:
      - short name: ``write_file(path="inspect.py")`` with workspace
        "./bruce" → ``./bruce/inspect.py``
      - already-qualified: ``write_file(path="./bruce/inspect.py")`` →
        ``./bruce/inspect.py`` (no double-nesting)
      - sibling allowed-path: ``find_files(path="./agentx_dev")`` with
        workspace "./workspace" AND allowed=[./agentx_dev, ./workspace]
        → ``./agentx_dev`` (not ``./workspace/agentx_dev``)
    """
    allowed = perms.effective_allowed_paths()
    p = Path(target)

    if p.is_absolute():
        resolved = p.resolve()
        _assert_resolved_allowed(target, resolved, allowed)
        return resolved

    has_explicit_prefix = target.startswith(("./", "../", ".\\", "..\\"))

    cwd_resolved = p.resolve()
    workspace_resolved = (
        (Path(perms.workspace) / target).resolve()
        if perms.workspace is not None else None
    )

    # Rule 2: explicit ./ or ../ prefix + hits a sibling allowed subtree.
    # The user typed a specific relative path — respect what they typed.
    if has_explicit_prefix and _is_inside_any(cwd_resolved, allowed):
        # Only prefer CWD when the workspace-relative form would put
        # us at a DIFFERENT location. Same location: doesn't matter.
        if workspace_resolved is None or cwd_resolved != workspace_resolved:
            _assert_resolved_allowed(target, cwd_resolved, allowed)
            return cwd_resolved

    # Rule 3: short-name style — try workspace + target.
    if workspace_resolved is not None and _is_inside_any(workspace_resolved, allowed):
        _assert_resolved_allowed(target, workspace_resolved, allowed)
        return workspace_resolved

    # Rule 4: CWD form as last resort before sandbox rejection.
    if _is_inside_any(cwd_resolved, allowed):
        _assert_resolved_allowed(target, cwd_resolved, allowed)
        return cwd_resolved

    # Rule 5: let the sandbox check reject with a clear error using
    # whichever form we can produce.
    resolved = workspace_resolved if workspace_resolved is not None else cwd_resolved
    _assert_resolved_allowed(target, resolved, allowed)
    return resolved


def _assert_resolved_allowed(
    original: str,
    resolved: Path,
    allowed_paths: Optional[List[str]],
) -> None:
    """Raise PermissionError if ``resolved`` isn't inside any of
    ``allowed_paths``. ``allowed_paths=None`` means no constraint."""
    if allowed_paths is None:
        return
    for allowed in allowed_paths:
        allowed_resolved = Path(allowed).resolve()
        try:
            resolved.relative_to(allowed_resolved)
            return
        except ValueError:
            continue
    raise PermissionError(
        f"access denied: path {original!r} (resolved to {resolved}) is "
        f"outside the allowed sandbox {allowed_paths}"
    )


# Kept as a thin adapter for the older call sites that just want a
# permission-checked Path from a string + allowed_paths (no workspace
# needed). Prefer _resolve_for_ops when you have a Permissions instance.
def _assert_path_allowed(target: str, allowed_paths: Optional[List[str]]) -> Path:
    resolved = Path(target).resolve()
    _assert_resolved_allowed(target, resolved, allowed_paths)
    return resolved


# --- Pydantic schemas --------------------------------------------------------

class _ReadPathArgs(BaseModel):
    """Read a file's contents, optionally a slice of lines."""
    path: str = Field(..., description="File path to read.")
    offset: Optional[int] = Field(
        None,
        description=(
            "Line number to start reading from (1-indexed). Omit or set "
            "to 1 to start at the beginning. Useful for stepping through "
            "a large file in chunks."
        ),
    )
    limit: Optional[int] = Field(
        None,
        description=(
            "Maximum number of lines to read. Omit to read to the end "
            "of the file. When offset+limit is set, output is line-"
            "numbered (like cat -n) to make navigation obvious."
        ),
    )


class _ListDirArgs(BaseModel):
    """List entries in a directory."""
    path: str = Field(..., description="Directory path to list.")


class _WriteFileArgs(BaseModel):
    """Write text content to a file."""
    path: str = Field(..., description="Destination file path.")
    content: str = Field(..., description="Text to write.")
    if_exists: str = Field(
        "refuse",
        description=(
            "What to do when path already exists. "
            "'refuse' (default) — safety mode; require you to have "
            "read the file first (prevents clobbering content you've "
            "never seen). "
            "'rename' — save to a fresh name like 'report_1.md', "
            "'report_2.md'... The tool returns the actual path used. "
            "USE THIS for generating deliverables when you don't want "
            "to overwrite prior runs' output. "
            "'overwrite' — force write over the existing file, no "
            "read-first check. Use when you're intentionally "
            "regenerating and don't care what was there. "
            "'append' — append content to the end of the existing "
            "file. Equivalent to edit_file for tacking on new content."
        ),
    )


class _EditFileArgs(BaseModel):
    """Replace one substring with another in a file. Exactly one match required."""
    path: str = Field(..., description="File to edit.")
    find: str = Field(..., description="Exact substring to locate.")
    replace: str = Field(..., description="Replacement text.")


class _DeletePathArgs(BaseModel):
    """Delete a file or directory."""
    path: str = Field(..., description="Path to delete.")
    recursive: Optional[bool] = Field(
        False,
        description=(
            "Only relevant for directories. When False (default), refuses "
            "to delete a non-empty directory. When True, removes the "
            "directory and every file/subdirectory it contains, in one "
            "shot. Sandbox check still applies — every removed path "
            "must be inside allowed_paths."
        ),
    )


class _MoveArgs(BaseModel):
    """Move or rename a file/directory."""
    source: str = Field(..., description="Source path.")
    destination: str = Field(..., description="Destination path.")


class _RunPythonArgs(BaseModel):
    """Execute a Python snippet in a fresh subprocess."""
    code: str = Field(..., description="Python source code.")


class _RunShellArgs(BaseModel):
    """Execute one shell command in a fresh subprocess."""
    command: str = Field(
        ...,
        description=(
            "The command line to run, exactly as you'd type it in a "
            "terminal. Multi-line scripts are fine — the whole string "
            "is passed to the shell's -c/-Command flag. This is for "
            "ONE-SHOT commands only (git status, ls, curl, grep, "
            "npm install, python -m pytest). Do NOT use it for "
            "long-running processes (npm run dev, dev servers) — "
            "they will just hit the timeout and get killed."
        ),
    )
    cwd: Optional[str] = Field(
        None,
        description=(
            "Working directory for the command. If not given, defaults "
            "to the agent's workspace (or the process's cwd if no "
            "workspace is configured). Must be inside allowed_paths."
        ),
    )


class _GrepArgs(BaseModel):
    """Search file contents for a pattern (grep-style)."""
    pattern: str = Field(..., description="String or regex to search for.")
    path: str = Field(
        ".",
        description="Directory root to search from. Defaults to '.' "
                    "(the workspace root).",
    )
    glob: str = Field(
        "**/*",
        description="File pattern to include (e.g. '**/*.py', 'src/**/*.ts'). "
                    "Recursive by default via '**/'.",
    )
    regex: bool = Field(
        False,
        description="When True, treat pattern as a Python regex. When False "
                    "(default), literal substring match.",
    )
    case_sensitive: bool = Field(
        True,
        description="When False, match case-insensitively.",
    )
    max_matches: int = Field(
        100,
        description="Cap on returned matches. Prevents dumping megabytes "
                    "when a hot pattern hits everywhere.",
    )


class _FindFilesArgs(BaseModel):
    """Find files by name/glob pattern."""
    glob: str = Field(
        ...,
        description="Glob pattern (e.g. '**/*.py', '**/config*.json', "
                    "'src/**/test_*'). Use '**/' for recursive search.",
    )
    path: str = Field(
        ".",
        description="Directory root to search from. Defaults to the "
                    "workspace root.",
    )
    max_results: int = Field(
        200,
        description="Cap on returned paths.",
    )


# Directories every codebase-aware tool should skip by default —
# noise that dwarfs real content and slows scans hugely. Users who
# genuinely need to search these can pass a specific path INTO them.
_GREP_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env",
    "dist", "build", "out", "target",
    ".idea", ".vscode", ".DS_Store",
    ".next", ".nuxt", ".turbo", ".cache",
})


def _matches_glob(rel_posix: str, glob: str) -> bool:
    """Return True if ``rel_posix`` matches ``glob``, where ``**/`` means
    "at any depth, including the root".

    Python's ``fnmatch.fnmatch`` doesn't treat ``/`` or ``**`` specially,
    so a pattern like ``**/*.py`` literally requires a ``/`` in the path
    and never matches ``app.py`` at the search root. Real glob tools
    (bash globstar, ripgrep, minimatch) treat ``**/`` as "zero or more
    directory segments" — this helper normalizes to that behavior."""
    from fnmatch import fnmatch
    if fnmatch(rel_posix, glob):
        return True
    # `**/pattern` should also match `pattern` at the search root
    # (zero intermediate segments).
    if glob.startswith("**/"):
        if fnmatch(rel_posix, glob[3:]):
            return True
    # Handle `dir/**/pattern` matching `dir/pattern` (root of subtree).
    # Collapses one `**/` at a time, defensively bounded.
    reduced = glob
    for _ in range(4):
        if "/**/" not in reduced:
            break
        reduced = reduced.replace("/**/", "/", 1)
        if fnmatch(rel_posix, reduced):
            return True
    return False


def _iter_files_for_search(root: Path, glob: str):
    """Yield paths under ``root`` matching ``glob``, skipping noise
    directories. Iterative walk (not Path.rglob) so we can prune
    entire subtrees like .git without descending into them."""
    if root.is_file():
        try:
            if _matches_glob(root.name, glob) or root.match(glob):
                yield root
        except Exception:
            pass
        return

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune noise dirs in-place so os.walk skips them entirely.
        dirnames[:] = [d for d in dirnames if d not in _GREP_SKIP_DIRS]
        base = Path(dirpath)
        for fn in filenames:
            fp = base / fn
            try:
                rel = fp.relative_to(root)
            except ValueError:
                continue
            if _matches_glob(rel.as_posix(), glob):
                yield fp


# --- Tool implementations (closures over Permissions) ------------------------

def _build_read_path(perms: Permissions, tracker: _ReadTracker) -> StructuredTool:
    def read_path(path: str, offset: Optional[int] = None, limit: Optional[int] = None) -> str:
        if not perms.read_files:
            raise PermissionError("access denied: read_files capability not granted")
        resolved = _resolve_for_ops(path, perms)
        if not resolved.exists():
            return f"ERROR: path does not exist: {path}"
        if resolved.is_dir():
            return f"ERROR: {path} is a directory; use list_directory instead"
        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"ERROR: file is not utf-8 text: {path}"

        # ANY successful read counts — the agent has "seen" this file
        # so write_file / edit_file will now permit changes.
        tracker.mark_read(resolved)

        if offset is None and limit is None:
            return text

        # Line-numbered slice — matches Claude Code's Read output shape.
        # Numbering starts at 1 to match how humans / editors count.
        lines = text.splitlines()
        start = max(1, offset or 1) - 1
        end = start + (limit if limit is not None else len(lines))
        selected = lines[start:end]
        if not selected:
            return f"(no lines in range: offset={offset}, limit={limit}, file has {len(lines)} lines)"
        numbered = "\n".join(f"{i}\t{line}" for i, line in enumerate(selected, start=start + 1))
        return numbered
    return StructuredTool(
        func=read_path, args_schema=_ReadPathArgs,
        name="read_path",
        description=(
            "Read a file's contents. Returns the FULL text when called with "
            "just a path; add offset (1-indexed line number) and/or limit "
            "(max lines) to read a slice — the output is then line-numbered "
            "(like cat -n) so you can navigate. IMPORTANT: reading a file "
            "'unlocks' it for editing / overwriting via edit_file or "
            "write_file. You MUST read a file before you can modify it "
            "(protects against clobbering unknown content). Writing a NEW "
            "file that doesn't exist yet doesn't need a prior read."
        ),
    )


def _build_list_directory(perms: Permissions) -> StructuredTool:
    def list_directory(path: str) -> str:
        if not perms.list_directories:
            raise PermissionError("access denied: list_directories capability not granted")
        resolved = _resolve_for_ops(path, perms)
        if not resolved.exists():
            return f"ERROR: path does not exist: {path}"
        if not resolved.is_dir():
            return f"ERROR: {path} is not a directory"
        entries = sorted(resolved.iterdir(), key=lambda p: p.name)
        if not entries:
            return f"(empty directory: {path})"
        lines = []
        for e in entries:
            tag = "[DIR]" if e.is_dir() else "[FILE]"
            lines.append(f"  {tag} {e.name}")
        return f"{path}:\n" + "\n".join(lines)
    return StructuredTool(
        func=list_directory, args_schema=_ListDirArgs,
        name="list_directory", description="List the entries of a directory.",
    )


def _pick_fresh_path(target: Path) -> Path:
    """Given a path that already exists, return a nearby path that
    does not — 'report.md' → 'report_1.md' → 'report_2.md' → ...
    Preserves the parent + extension so downstream code that expects
    a .md sees a .md."""
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    for i in range(1, 10_000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    # Fantastically unlikely — 10k collisions on the same name. Fall
    # through to appending a random suffix to guarantee uniqueness.
    import uuid
    return parent / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def _build_write_file(perms: Permissions, tracker: _ReadTracker) -> StructuredTool:
    def write_file(
        path: str,
        content: str,
        if_exists: str = "refuse",
    ) -> str:
        if not perms.write_files:
            raise PermissionError("access denied: write_files capability not granted")
        resolved = _resolve_for_ops(path, perms)

        mode = (if_exists or "refuse").lower().strip()
        if mode not in {"refuse", "rename", "overwrite", "append"}:
            return (
                f"ERROR: if_exists={if_exists!r} is not valid. "
                "Use 'refuse' (safety), 'rename' (auto-pick a fresh "
                "name), 'overwrite' (force), or 'append' (add to end)."
            )

        existed = resolved.exists()
        actual_path = resolved
        actual_mode_taken = "created"   # for the return message

        if existed:
            if mode == "refuse":
                # Original safety: require a prior read.
                if not tracker.has_read(resolved):
                    return (
                        f"ERROR: write_file refused — {path} already exists "
                        "but hasn't been read this session. Options: "
                        "(1) call read_path first if you want to see what "
                        "you'd be overwriting; (2) retry with "
                        "if_exists='rename' to save under a fresh name; "
                        "(3) retry with if_exists='overwrite' to force; "
                        "(4) retry with if_exists='append' to add to end."
                    )
                actual_mode_taken = "overwrote (after read)"
            elif mode == "rename":
                actual_path = _pick_fresh_path(resolved)
                actual_mode_taken = f"created (renamed from '{resolved.name}' to avoid collision)"
            elif mode == "overwrite":
                actual_mode_taken = "overwrote (forced)"
            elif mode == "append":
                actual_mode_taken = "appended"

        encoded = content.encode("utf-8")
        if len(encoded) > perms.max_file_size_bytes:
            raise PermissionError(
                f"access denied: content size {len(encoded)} bytes exceeds "
                f"max_file_size_bytes={perms.max_file_size_bytes}"
            )
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append" and existed:
            with actual_path.open("ab") as fh:
                fh.write(encoded)
        else:
            actual_path.write_bytes(encoded)
        # Track the file we actually wrote — that's the one whose
        # content the agent now "knows" for future edits.
        tracker.mark_read(actual_path)
        return f"{actual_mode_taken}: {len(encoded)} bytes to {actual_path}"

    return StructuredTool(
        func=write_file, args_schema=_WriteFileArgs,
        name="write_file",
        description=(
            "Create or update a text file. Optional if_exists param "
            "controls what happens when the path already exists: "
            "'refuse' (default, safe — requires you to have read the "
            "file first this session); 'rename' (auto-pick a fresh "
            "name like report_1.md; use for deliverables you don't "
            "want to clobber); 'overwrite' (force write, no prior "
            "read needed); 'append' (add to end). Returns the actual "
            "path written (important in 'rename' mode). Creating a "
            "NEW file that doesn't yet exist works with the default."
        ),
    )


def _maybe_unescape_json_escapes(s: str) -> str:
    """Un-escape ``\\n``, ``\\t``, ``\\r`` literals when they're clearly
    supposed to be actual whitespace.

    LLMs frequently emit tool args with over-escaped whitespace — the
    JSON string ``"line1\\\\nline2"`` parses to the 12-char string
    ``line1\\nline2`` (backslash + n between the words) rather than a
    real newline. When this string is then written to code, the code
    becomes invalid. Same failure for ``\\t`` (tabs) and ``\\r``.

    Heuristic: if the string contains ``\\n`` or ``\\t`` or ``\\r``
    literal sequences AND contains NO actual newlines/tabs/carriage
    returns of the corresponding kind, treat that as the over-escape
    mistake and swap them for the real characters. Multi-line strings
    that legitimately contain both real and literal escapes fall
    through unchanged.
    """
    def _swap(text: str, literal: str, real: str) -> str:
        if literal in text and real not in text:
            return text.replace(literal, real)
        return text
    out = s
    out = _swap(out, "\\n", "\n")
    out = _swap(out, "\\t", "\t")
    out = _swap(out, "\\r", "\r")
    return out


def _build_edit_file(perms: Permissions, tracker: _ReadTracker) -> StructuredTool:
    def edit_file(path: str, find: str, replace: str) -> str:
        if not perms.edit_files:
            raise PermissionError("access denied: edit_files capability not granted")
        resolved = _resolve_for_ops(path, perms)

        # Common LLM mistake: sending find/replace with escaped \n
        # literals instead of real newlines. Unescape when unambiguous.
        find = _maybe_unescape_json_escapes(find)
        replace = _maybe_unescape_json_escapes(replace)
        if not resolved.exists() or not resolved.is_file():
            return f"ERROR: file does not exist: {path}"

        # Read-before-edit safety: the agent must have read this file
        # this session before it can modify it. Prevents guessing at
        # content and editing something you haven't seen.
        if not tracker.has_read(resolved):
            return (
                f"ERROR: edit_file refused — {path} hasn't been read this "
                f"session. Call read_path({path!r}) first so you know what "
                "the file actually contains, then retry edit_file with a "
                "'find' substring you've actually seen."
            )

        text = resolved.read_text(encoding="utf-8")
        count = text.count(find)
        if count == 0:
            return f"ERROR: substring not found in {path}"
        if count > 1:
            return (
                f"ERROR: substring appears {count} times in {path}; refusing "
                "to edit ambiguously. Include more surrounding context in 'find'."
            )
        new_text = text.replace(find, replace, 1)
        encoded = new_text.encode("utf-8")
        if len(encoded) > perms.max_file_size_bytes:
            raise PermissionError(
                f"access denied: resulting file size {len(encoded)} bytes "
                f"exceeds max_file_size_bytes={perms.max_file_size_bytes}"
            )
        resolved.write_text(new_text, encoding="utf-8")
        # File contents just changed; the agent still knows what's in it
        # (it saw the before, made the edit, so the after is known too).
        tracker.mark_read(resolved)
        return f"edited {path}: replaced 1 occurrence"
    return StructuredTool(
        func=edit_file, args_schema=_EditFileArgs,
        name="edit_file",
        description=(
            "Replace exactly one occurrence of a substring in a file. "
            "IMPORTANT: you MUST call read_path on the file first this "
            "session — edit_file refuses if the file hasn't been read. "
            "Fails if the substring is missing or appears more than once "
            "(add more surrounding context to disambiguate). "
            "USE CASES: fix a typo, tweak one line, change one function "
            "signature. For LARGER changes (rewriting a function, changing "
            "multiple lines, adding several new imports), prefer write_file "
            "with the full new contents — it's more reliable than chaining "
            "edit_file calls. Editing broken code with edit_file often "
            "produces MORE broken code."
        ),
    )


def _build_delete_path(perms: Permissions, tracker: _ReadTracker) -> StructuredTool:
    def delete_path(path: str, recursive: bool = False) -> str:
        if not perms.delete_files:
            raise PermissionError("access denied: delete_files capability not granted")
        resolved = _resolve_for_ops(path, perms)
        if not resolved.exists():
            return f"ERROR: path does not exist: {path}"
        if resolved.is_dir():
            if recursive:
                # Sandbox check happens at the ROOT via _resolve_for_ops
                # above; rmtree only descends into paths already inside
                # the allowed subtree, so we don't need per-child checks.
                # (An attacker can't smuggle a symlink out because
                # Path.resolve on the root already flattened traversal;
                # rmtree by default does NOT follow symlinks either.)
                shutil.rmtree(resolved)
                tracker.mark_read(resolved)
                return f"recursively deleted directory {path}"
            try:
                resolved.rmdir()
            except OSError as e:
                return (
                    f"ERROR: cannot delete non-empty directory {path}: {e}. "
                    "Pass recursive=True to remove the directory and its "
                    "contents in one shot."
                )
        else:
            resolved.unlink()
        # KEEP the path in the tracker as "known". Rationale: the agent
        # explicitly deleted this path — they know about it. If something
        # external (autosave, filesystem lag, race) puts a file back at
        # the same path, the next write_file/edit_file at that path must
        # still be allowed. Forgetting would produce a paradoxical
        # "delete succeeded but you must read the (nonexistent) file
        # before writing" refusal — observed in the wild.
        tracker.mark_read(resolved)
        return f"deleted {path}"
    return StructuredTool(
        func=delete_path, args_schema=_DeletePathArgs,
        name="delete_path",
        description=(
            "Delete a file or a directory. For a directory: pass "
            "recursive=True to remove it and all its contents in one "
            "shot (useful for __pycache__, node_modules, build/, etc.); "
            "otherwise fails on non-empty directories. Sandbox check "
            "applies to the root path — recursive delete cannot escape "
            "allowed_paths. Path can be workspace-relative when "
            "Permissions.workspace is set."
        ),
    )


def _build_move_file(perms: Permissions, tracker: _ReadTracker) -> StructuredTool:
    def move_file(source: str, destination: str) -> str:
        if not perms.move_files:
            raise PermissionError("access denied: move_files capability not granted")
        src = _resolve_for_ops(source, perms)
        dst = _resolve_for_ops(destination, perms)
        if not src.exists():
            return f"ERROR: source does not exist: {source}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        # Track: source is gone, destination is now "known" if source
        # was known (agent has seen this content, it just lives elsewhere).
        if tracker.has_read(src):
            tracker.mark_read(dst)
        tracker.forget(src)
        return f"moved {source} -> {destination}"
    return StructuredTool(
        func=move_file, args_schema=_MoveArgs,
        name="move_file",
        description="Move or rename a file. Both endpoints must be inside the sandbox.",
    )


def _resolve_state_dir(perms: Permissions) -> Optional[Path]:
    """Where the run_python persistent-state pickle should live.
    Prefers the workspace; falls back to the first allowed subtree.
    Returns None if neither is set — the caller then treats persistence
    as a no-op with a clear error message."""
    if perms.workspace:
        return Path(perms.workspace).resolve()
    if perms.allowed_paths:
        return Path(perms.allowed_paths[0]).resolve()
    return None


# Header injected before user code when python_persistent_state=True.
# Loads a `state` dict from the pickle file if its HMAC matches the key
# the parent process passed via AGENTX_STATE_HMAC_KEY (env). Any
# tampering — or a foreign process dropping a malicious pickle into the
# workspace, hoping the next run_python call would auto-load it — makes
# the HMAC check fail and we start fresh instead of unpickling attacker-
# controlled bytes.
#
# The HMAC key lives only in the parent framework process's memory; a
# lower-privilege sibling agent with only `write_files=True` can drop a
# pickle into the workspace but cannot know the key, so its payload is
# rejected. A parent restart means a new key which means old on-disk
# state fails verify; that path was already handled as "start fresh".
_PERSISTENT_STATE_HEADER = r'''
import hmac as _agentx_hmac
import hashlib as _agentx_hashlib
import os as _agentx_os
import pickle as _agentx_pickle

_AGENTX_STATE_PATH = r"{state_path}"
_AGENTX_HMAC_KEY = _agentx_os.environ.get("AGENTX_STATE_HMAC_KEY", "").encode("utf-8")
_AGENTX_HMAC_HEX_LEN = 64   # sha256 hex digest length

try:
    if _AGENTX_HMAC_KEY and _agentx_os.path.exists(_AGENTX_STATE_PATH):
        with open(_AGENTX_STATE_PATH, "rb") as _agentx_f:
            _agentx_blob = _agentx_f.read()
        if len(_agentx_blob) < _AGENTX_HMAC_HEX_LEN:
            raise ValueError("state file too small to contain HMAC prefix")
        _agentx_sig = _agentx_blob[:_AGENTX_HMAC_HEX_LEN].decode("ascii", errors="strict")
        _agentx_payload = _agentx_blob[_AGENTX_HMAC_HEX_LEN:]
        _agentx_expected = _agentx_hmac.new(
            _AGENTX_HMAC_KEY, _agentx_payload, _agentx_hashlib.sha256
        ).hexdigest()
        if not _agentx_hmac.compare_digest(_agentx_sig, _agentx_expected):
            raise ValueError(
                "HMAC mismatch — state file was written by a different "
                "process or has been tampered with; refusing to unpickle"
            )
        state = _agentx_pickle.loads(_agentx_payload)
        if not isinstance(state, dict):
            state = {{}}
    else:
        state = {{}}
except Exception as _agentx_e:
    # Corrupted pickle, missing key, or (most importantly) HMAC mismatch
    # from a malicious drop by another agent. Start fresh rather than
    # propagate the error into the user's code — the model can rebuild.
    print(
        f"[run_python.state] warning: could not load state "
        f"({{_agentx_e}}); starting fresh",
        flush=True,
    )
    state = {{}}
'''

# Footer runs after user code. Walks `state` and drops any values that
# aren't picklable — the model sees a note in stderr listing the
# dropped keys so it knows which variables didn't survive the round
# trip. HMAC-signs the payload so the next call's header can verify.
# Non-fatal.
_PERSISTENT_STATE_FOOTER = r'''
try:
    _agentx_clean = {}
    _agentx_dropped = []
    if isinstance(state, dict):
        for _agentx_k, _agentx_v in state.items():
            try:
                _agentx_pickle.dumps(_agentx_v)
                _agentx_clean[_agentx_k] = _agentx_v
            except Exception:
                _agentx_dropped.append(_agentx_k)
    _agentx_payload = _agentx_pickle.dumps(_agentx_clean)
    if _AGENTX_HMAC_KEY:
        _agentx_sig = _agentx_hmac.new(
            _AGENTX_HMAC_KEY, _agentx_payload, _agentx_hashlib.sha256
        ).hexdigest().encode("ascii")
        with open(_AGENTX_STATE_PATH, "wb") as _agentx_f:
            _agentx_f.write(_agentx_sig + _agentx_payload)
    else:
        # No key configured — skip save rather than write an
        # unverifiable file the next call would reject.
        import sys as _agentx_sys
        print(
            "[run_python.state] warning: no HMAC key in env; state NOT saved",
            file=_agentx_sys.stderr, flush=True,
        )
    if _agentx_dropped:
        import sys as _agentx_sys
        print(f"[run_python.state] note: dropped non-picklable keys {sorted(_agentx_dropped)}; other keys saved.", file=_agentx_sys.stderr, flush=True)
except Exception as _agentx_e:
    import sys as _agentx_sys
    print(f"[run_python.state] warning: failed to save state ({_agentx_e})", file=_agentx_sys.stderr, flush=True)
'''


def _build_run_python(perms: Permissions) -> StructuredTool:
    # Where the persistent-state pickle would live if enabled. Computed
    # once at build time; the file itself is created/updated lazily on
    # first successful run_python call.
    state_path: Optional[Path] = None
    # HMAC key for persistent-state pickle integrity. Generated once per
    # tool build (i.e. per Permissions.build()) and never touches disk —
    # sits in this closure and gets injected into the run_python child
    # subprocess via AGENTX_STATE_HMAC_KEY env var. Rationale: a foreign
    # writer with only write_files=True can drop bytes at the state
    # path, but cannot know this key, so a malicious pickle fails the
    # HMAC check in the header and we start fresh instead of executing
    # attacker-controlled __reduce__ payloads.
    state_hmac_key: Optional[str] = None
    if perms.python_persistent_state:
        state_dir = _resolve_state_dir(perms)
        if state_dir is None:
            # Bad config: persistent state requested but no place to put
            # the pickle. Every run_python call will report this cleanly
            # rather than silently ignoring the flag.
            state_path = None
        else:
            state_path = state_dir / ".run_python_state.pkl"
            import secrets as _secrets
            state_hmac_key = _secrets.token_hex(32)   # 256-bit key

    def run_python(code: str) -> str:
        if not perms.execute_python:
            raise PermissionError("access denied: execute_python capability not granted")

        # Wrap the user's code with load/save prologue if persistence is on.
        if perms.python_persistent_state:
            if state_path is None:
                return (
                    "ERROR: python_persistent_state=True was set but the "
                    "Permissions has no workspace and no allowed_paths, "
                    "so there's nowhere to store the state file. Either "
                    "set workspace=<dir> on Permissions or set "
                    "python_persistent_state=False."
                )
            header = _PERSISTENT_STATE_HEADER.format(state_path=str(state_path))
            code = header + "\n" + code + "\n" + _PERSISTENT_STATE_FOOTER

        try:
            # encoding='utf-8', errors='replace' — on Windows subprocess.run
            # defaults to cp1252 for decoding child stdout/stderr, which
            # explodes on non-Latin-1 bytes (emoji, HTTP response bodies,
            # anything from a library that emits UTF-8). Force UTF-8 with
            # a permissive replace policy so a stray byte can't crash the
            # tool. env override PYTHONIOENCODING=utf-8 makes the CHILD
            # emit UTF-8 too — belt and suspenders.
            # PYTHONIOENCODING covers stdin/stdout/stderr only. PYTHONUTF8=1
            # forces utf-8 for EVERY Python text operation in the subprocess,
            # including open() with no explicit encoding — the common
            # gotcha on Windows where the locale codepage (cp1252) fails
            # to encode non-Latin-1 characters like em-dashes and emoji.
            # Available since Python 3.7 (PEP 540).
            #
            # _build_child_env scrubs the parent env down to a safe base
            # so `print(os.environ)` can't exfil API keys / secrets. Add
            # names to Permissions.subprocess_env_passthrough to opt back
            # in per-var; use ["*"] for full passthrough.
            child_env = _build_child_env(perms)
            # Framework-internal: inject the HMAC key for the persistent-
            # state pickle. Not user-configurable and not surfaced in the
            # passthrough allow-list — the key MUST be per-run-only and
            # never persisted to disk (see _build_run_python for
            # rationale).
            if state_hmac_key is not None:
                child_env["AGENTX_STATE_HMAC_KEY"] = state_hmac_key
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True,
                timeout=perms.python_timeout_sec,
                encoding="utf-8", errors="replace",
                env=child_env,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: code execution timed out after {perms.python_timeout_sec}s"

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # Cap output so a chatty script can't bloat agent history.
        cap = perms.python_max_output_bytes
        if len(stdout.encode("utf-8")) > cap:
            stdout = stdout[:cap] + f"\n... (truncated at {cap} bytes)"
        if len(stderr.encode("utf-8")) > cap:
            stderr = stderr[:cap] + f"\n... (truncated at {cap} bytes)"

        parts = []
        if stdout:
            parts.append(f"--- stdout ---\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"--- stderr ---\n{stderr.rstrip()}")
        if proc.returncode != 0:
            parts.append(f"(exit code {proc.returncode})")
        if parts:
            return "\n".join(parts)
        # The common failure mode: script ran fine, produced no output
        # because the model wrote a bare expression instead of print().
        # Tell it exactly that — turns a confused 'I couldn't run it' into
        # a corrected 'oh, I need to wrap it in print()'.
        return (
            "(no output — your code executed successfully (exit code 0) but "
            "printed nothing to stdout. Only stdout/stderr are returned, so "
            "you must wrap values in print() to see them. For example, change "
            "`status` to `print(status)`, or `result` to `print(result)`. "
            "Then re-run and inspect the output.)"
        )
    base_description = (
        "Execute a Python snippet in a fresh subprocess and return its "
        "stdout+stderr. This is your GENERAL-PURPOSE tool — use it for "
        "ANY task that can be solved with Python code: fetching URLs "
        "(urllib.request or requests), scraping web pages, calling APIs, "
        "parsing HTML/JSON/CSV, doing math, running shell commands, "
        "working with dates, running regex, sorting data, anything. "
        "PRINT what you want to see (only stdout/stderr come back). "
        "IMPORTANT: this tool IS already a subprocess. Do NOT wrap "
        "your code in another subprocess.run(['python', 'file.py']) "
        "call — instead, if you want to run a file you already wrote, "
        "read it with open(...).read() and exec() it, or just put "
        "the code directly here. Subject to wall-clock timeout and "
        "output-size cap."
    )
    if perms.python_persistent_state:
        base_description += (
            "\n\nPERSISTENT STATE (this instance): a `state` dict is "
            "automatically loaded at the top of every call and saved "
            "at the end. Use it to pass data between calls — write "
            "`state['files'] = python_files` in call 1, then read "
            "`python_files = state['files']` in call 2. This avoids "
            "re-computing setup work every time. Only picklable "
            "objects survive (basic types, dicts, lists, tuples, "
            "dataclasses, most numpy/pandas objects); non-picklable "
            "values are dropped with a note in stderr listing the "
            "keys. To clear state, do `state.clear()` or delete the "
            "hidden .run_python_state.pkl file. First call: `state` "
            "is `{}`."
        )
    else:
        base_description += (
            "\n\nThe script runs in a FRESH interpreter each call — "
            "start-from-scratch. Variables and imports do NOT persist "
            "between calls. If you need to reuse setup work, combine "
            "it with your analysis into ONE call."
        )
    return StructuredTool(
        func=run_python, args_schema=_RunPythonArgs,
        name="run_python",
        description=base_description,
    )


def _build_find_files(perms: Permissions) -> StructuredTool:
    def find_files(glob: str, path: str = ".", max_results: int = 200) -> str:
        if not perms.list_directories:
            raise PermissionError("access denied: list_directories capability not granted")
        resolved = _resolve_for_ops(path, perms)
        if not resolved.exists():
            return f"ERROR: path does not exist: {path}"
        if not resolved.is_dir() and not resolved.is_file():
            return f"ERROR: not a valid path: {path}"

        hits: List[Path] = []
        truncated = False
        try:
            for fp in _iter_files_for_search(resolved, glob):
                hits.append(fp)
                if len(hits) >= max_results:
                    truncated = True
                    break
        except Exception as e:
            return f"ERROR: find_files failed: {e}"

        if not hits:
            return f"(no files matched '{glob}' under {path})"

        # Sort by modification time desc so freshest results surface —
        # matches how a human usually wants "find this" queries answered.
        hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        lines = [str(p) for p in hits]
        header = f"{len(hits)} match(es) for '{glob}' under {path}"
        if truncated:
            header += f" (truncated at max_results={max_results})"
        return header + ":\n" + "\n".join(lines)

    return StructuredTool(
        func=find_files, args_schema=_FindFilesArgs,
        name="find_files",
        description=(
            "Find files by name/glob pattern under a directory root. "
            "Use for questions like 'where is X.py', 'what config files "
            "exist here', 'list all typescript files under src'. Skips "
            "noise directories (.git, node_modules, __pycache__, "
            "dist, build, .venv, etc.) automatically. Returns matches "
            "sorted by mtime (newest first). Use '**/' in the glob for "
            "recursive search (e.g. '**/*.py')."
        ),
    )


def _build_grep(perms: Permissions) -> StructuredTool:
    def grep(
        pattern: str,
        path: str = ".",
        glob: str = "**/*",
        regex: bool = False,
        case_sensitive: bool = True,
        max_matches: int = 100,
    ) -> str:
        if not perms.read_files:
            raise PermissionError("access denied: read_files capability not granted")
        resolved = _resolve_for_ops(path, perms)
        if not resolved.exists():
            return f"ERROR: path does not exist: {path}"

        # ReDoS guard: bound pattern length so an attacker can't feed a
        # 100kB nested-quantifier regex that would spin the engine even
        # on short input. 500 chars covers every realistic grep pattern
        # a model would emit.
        _MAX_PATTERN_LEN = 500
        if len(pattern) > _MAX_PATTERN_LEN:
            return (
                f"ERROR: pattern too long ({len(pattern)} > "
                f"{_MAX_PATTERN_LEN} chars); refusing to compile — "
                f"trim the regex or use a coarser match."
            )

        # Compile the matcher once. For non-regex we still use `re` to
        # get case-insensitive matching for free — the pattern gets
        # escaped so `.` and friends stay literal.
        import re as _re
        import time as _time
        flags = 0 if case_sensitive else _re.IGNORECASE
        try:
            if regex:
                matcher = _re.compile(pattern, flags)
            else:
                matcher = _re.compile(_re.escape(pattern), flags)
        except _re.error as e:
            return f"ERROR: invalid regex: {e}"

        # Total-scan-time cap. Python's re engine has no per-call
        # timeout, but we can bound total wall-time by checking the
        # clock between files (cheap) and between every 1000 lines
        # (cheap enough at the resolution we care about). A pathological
        # catastrophic-backtracking pattern will still spin on the ONE
        # line that triggers it, but the total tool call is bounded so
        # the agent loop can move on instead of hanging.
        _SCAN_BUDGET_S = 10.0
        _deadline = _time.monotonic() + _SCAN_BUDGET_S
        _PER_LINE_CHECK = 1000
        timed_out = False

        matches: List[str] = []
        files_scanned = 0
        truncated = False
        try:
            for fp in _iter_files_for_search(resolved, glob):
                if _time.monotonic() >= _deadline:
                    timed_out = True
                    break
                files_scanned += 1
                # Skip files that aren't valid UTF-8 text. Binary files
                # (images, compiled artifacts) would return junk anyway.
                try:
                    with fp.open("r", encoding="utf-8", errors="strict") as fh:
                        for lineno, line in enumerate(fh, start=1):
                            if lineno % _PER_LINE_CHECK == 0 and _time.monotonic() >= _deadline:
                                timed_out = True
                                break
                            if matcher.search(line):
                                # rstrip so a match at end of file
                                # doesn't add a stray blank line.
                                snippet = line.rstrip("\n")
                                if len(snippet) > 300:
                                    snippet = snippet[:300] + "..."
                                matches.append(f"{fp}:{lineno}: {snippet}")
                                if len(matches) >= max_matches:
                                    truncated = True
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
                if truncated or timed_out:
                    break
        except Exception as e:
            return f"ERROR: grep failed: {e}"
        if timed_out:
            return (
                f"ERROR: grep exceeded {_SCAN_BUDGET_S:.0f}s budget after "
                f"scanning {files_scanned} file(s), {len(matches)} match(es) "
                f"collected. The pattern may be catastrophically backtracking "
                f"— simplify it (avoid nested quantifiers like (a+)+ ) or "
                f"narrow 'glob'/'path' to fewer files."
            )

        if not matches:
            return (
                f"(no matches for pattern in {files_scanned} file(s) under {path}, "
                f"glob={glob!r})"
            )
        header = (
            f"{len(matches)} match(es) across {files_scanned} scanned file(s)"
        )
        if truncated:
            header += f" (truncated at max_matches={max_matches})"
        return header + ":\n" + "\n".join(matches)

    return StructuredTool(
        func=grep, args_schema=_GrepArgs,
        name="grep",
        description=(
            "Search FILE CONTENTS for a pattern (grep-style). Use for "
            "questions like 'where do we import X', 'which files call "
            "function foo', 'find all TODO comments'. Returns "
            "file:line: matching-line records. Default is literal "
            "substring match; set regex=True for Python regex syntax. "
            "Scope the search with 'path' (root dir) and 'glob' "
            "('**/*.py' etc.). Auto-skips noise dirs like .git, "
            "node_modules, __pycache__. Binary/non-utf8 files skipped."
        ),
    )


def _resolve_shell_interpreter() -> List[str]:
    """Pick the best available shell for the current platform.

    Priority:
      1. ``$AGENTX_SHELL`` env var — user override, split with shlex
         (``AGENTX_SHELL="bash -euo pipefail -c"`` works).
      2. On non-Windows: ``bash -c`` (fall back to ``sh -c``).
      3. On Windows: git-bash if installed (checks common install
         locations + PATH), else PowerShell (``powershell -Command``).

    Returns a list suitable for the front of ``subprocess.run(...)`` —
    the actual command string gets appended as the final element.
    """
    import shlex
    override = os.environ.get("AGENTX_SHELL")
    if override:
        parts = shlex.split(override)
        if parts:
            return parts

    if sys.platform != "win32":
        # Unix / Mac. bash is standard; sh is the guaranteed fallback.
        if shutil.which("bash"):
            return ["bash", "-c"]
        return ["sh", "-c"]

    # Windows: git-bash gives POSIX semantics the model likely knows.
    # Common install locations:
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if os.path.isfile(candidate):
            return [candidate, "-c"]
    which_bash = shutil.which("bash")
    if which_bash:
        return [which_bash, "-c"]

    # PowerShell fallback. -NoProfile skips user profile scripts
    # (faster, more predictable). -Command runs the command string.
    return ["powershell", "-NoProfile", "-Command"]


def _build_run_shell(perms: Permissions) -> StructuredTool:
    def run_shell(command: str, cwd: Optional[str] = None) -> str:
        if not perms.execute_shell:
            raise PermissionError("access denied: execute_shell capability not granted")

        # Resolve cwd. If provided, honor allowed_paths; if not, use
        # workspace when set — otherwise default to the process cwd.
        if cwd:
            resolved_cwd = _resolve_for_ops(cwd, perms)
            if not resolved_cwd.is_dir():
                return f"ERROR: cwd is not a directory: {cwd}"
            cwd_str = str(resolved_cwd)
        elif perms.workspace:
            cwd_str = str(Path(perms.workspace).resolve())
        else:
            cwd_str = None  # subprocess uses parent's cwd

        interpreter = _resolve_shell_interpreter()

        # Same UTF-8 belt-and-suspenders as run_python — Windows'
        # cp1252 default explodes on emoji / non-Latin-1 output.
        # Scrubbed by default (see _build_child_env); opt vars back in
        # via Permissions.subprocess_env_passthrough.
        child_env = _build_child_env(perms)

        try:
            proc = subprocess.run(
                interpreter + [command],
                capture_output=True, text=True,
                timeout=perms.shell_timeout_sec,
                encoding="utf-8", errors="replace",
                env=child_env,
                cwd=cwd_str,
            )
        except subprocess.TimeoutExpired:
            return (
                f"ERROR: shell command timed out after {perms.shell_timeout_sec}s. "
                "This tool is for one-shot commands only — long-running "
                "processes (dev servers, watchers) will always hit the "
                "timeout. Use a shorter check instead."
            )
        except FileNotFoundError as e:
            return (
                f"ERROR: shell interpreter not available: {e}. "
                f"Tried: {' '.join(interpreter)}. On Windows, install "
                "Git for Windows to get bash, or set AGENTX_SHELL "
                "to point at an alternative shell."
            )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        cap = perms.shell_max_output_bytes
        if len(stdout.encode("utf-8")) > cap:
            stdout = stdout[:cap] + f"\n... (stdout truncated at {cap} bytes)"
        if len(stderr.encode("utf-8")) > cap:
            stderr = stderr[:cap] + f"\n... (stderr truncated at {cap} bytes)"

        parts: List[str] = []
        # Show the interpreter + cwd so the model can debug when the
        # output surprises it (wrong shell, wrong dir).
        parts.append(f"$ ({Path(interpreter[0]).name} in {cwd_str or os.getcwd()}) {command}")
        if stdout:
            parts.append(f"--- stdout ---\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"--- stderr ---\n{stderr.rstrip()}")
        if proc.returncode != 0:
            parts.append(f"(exit code {proc.returncode})")
        elif not stdout and not stderr:
            parts.append("(exit code 0, no output)")
        return "\n".join(parts)

    return StructuredTool(
        func=run_shell, args_schema=_RunShellArgs,
        name="run_shell",
        description=(
            "Run a single shell command and return its stdout + stderr + "
            "exit code. Use for git commands, package managers (npm/pip "
            "install), one-shot builds (npm run build, tsc), curl, tests "
            "(pytest, npm test), and generally anything you'd run in a "
            "terminal. Interpreter: bash on Unix/Mac, git-bash on Windows "
            "if available else PowerShell. Set AGENTX_SHELL env var to "
            "override. IMPORTANT: this is for ONE-SHOT commands only — "
            "long-running processes (npm run dev, python -m http.server, "
            "any dev server or watcher) will hit the shell_timeout_sec "
            "wall-clock timeout and get killed. Optional 'cwd' scopes "
            "the command to a subdirectory of your workspace."
        ),
    )


def _build_denied_stub(name: str, capability: str) -> StructuredTool:
    """When include_denied=True, build a placeholder tool that just refuses.

    Lets the agent see the tool exists but get a clean error if it tries to
    use a capability the user hasn't granted. Useful for surfacing 'oh you
    need to enable this' in development.
    """
    class _AnyArgs(BaseModel):
        """Accepts any input."""
        # No fields — Pydantic accepts an empty dict.

    def denied(**_kwargs) -> str:
        raise PermissionError(
            f"access denied: {capability} capability not granted. "
            f"Set perms.{capability} = True if you intended to allow this."
        )
    return StructuredTool(
        func=denied, args_schema=_AnyArgs,
        name=name,
        description=f"DISABLED: requires perms.{capability}=True. Currently denied.",
    )


# --- Public surface ----------------------------------------------------------

class DefaultTools:
    """Builds the canonical built-in tool list from a Permissions instance.

    Usage::

        tools = DefaultTools.build(Permissions.full_access(["./project"]))
        runner = AgentRunner(model=llm, agent=AgentType.ReAct, tools=tools)
    """

    _TOOL_TABLE = [
        # (tool name, capability flag attr, builder)
        ("read_path",       "read_files",        _build_read_path),
        ("list_directory",  "list_directories",  _build_list_directory),
        ("find_files",      "list_directories",  _build_find_files),
        ("grep",            "read_files",        _build_grep),
        ("write_file",      "write_files",       _build_write_file),
        ("edit_file",       "edit_files",        _build_edit_file),
        ("delete_path",     "delete_files",      _build_delete_path),
        ("move_file",       "move_files",        _build_move_file),
        ("run_python",      "execute_python",    _build_run_python),
        ("run_shell",       "execute_shell",     _build_run_shell),
    ]

    @classmethod
    def build(
        cls,
        permissions: Permissions,
        *,
        include_denied: bool = False,
    ) -> List[StructuredTool]:
        """Return the list of tools the agent can use given ``permissions``.

        By default, denied tools are NOT registered — the agent never sees
        them. Pass ``include_denied=True`` to also register placeholder
        versions that return a clean 'access denied' error when called
        (useful in development for surfacing 'oh, enable this capability').

        Every call creates a NEW _ReadTracker shared across the returned
        read/write/edit/delete/move tools. That means each fresh
        ``DefaultTools.build`` gives you a fresh session — files "known"
        to one runner's tools are unknown to another runner's tools even
        if they point at the same paths. Prevents cross-runner state leaks.
        """
        if not isinstance(permissions, Permissions):
            raise TypeError(
                f"DefaultTools.build expected a Permissions instance, "
                f"got {type(permissions).__name__}"
            )
        tracker = _ReadTracker()
        # Bookkeeping: not every builder needs the tracker (run_python /
        # list_directory don't care), but passing it uniformly keeps the
        # dispatch table simple. Builders that ignore it just drop the arg.
        _TRACKER_USERS = {
            "read_path", "write_file", "edit_file", "delete_path", "move_file",
        }
        out: List[StructuredTool] = []
        for tool_name, cap_attr, builder in cls._TOOL_TABLE:
            if getattr(permissions, cap_attr):
                if tool_name in _TRACKER_USERS:
                    out.append(builder(permissions, tracker))
                else:
                    out.append(builder(permissions))
            elif include_denied:
                out.append(_build_denied_stub(tool_name, cap_attr))
        return out

    @classmethod
    def names(cls) -> List[str]:
        """The tool names this module ships — useful for documentation /
        permission UIs that need to enumerate the capabilities."""
        return [name for name, _cap, _b in cls._TOOL_TABLE]
