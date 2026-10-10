"""The tool set agentx gives the model: the framework's default tools, sandboxed to the project and
wrapped in the gate."""

from __future__ import annotations

from pathlib import Path
import weakref
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence

from agentx_dev.DefaultTools import DefaultTools, Permissions, _is_inside_any, _resolve_for_ops
from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.approval import Gate

UNSUPPORTED_PATH = "Refused: that path form is not supported. Use a normal relative or absolute path."
GATE_FAILED = "Refused: the approval check failed, so nothing was run."
# The arguments that name a place on disk; ``cwd`` is run_shell's working folder.
PATH_ARGUMENTS = ("path", "source", "destination", "cwd")


def _reroot(value: str, permissions: Permissions) -> str:
    """``value`` as the project-relative text the tool will treat it as, when the tool re-roots it.

    ``_resolve_for_ops`` reads a rooted path with no drive letter (such as ``/x``, or one rooted with a backslash) whose literal location is
    outside the sandbox as ``<workspace>/x``, while the gate would read it as a path from the filesystem
    root. That is the only case where the two disagree, so it is the only one rewritten, to the text
    ``x``; every other path stays exactly as the model wrote it (so a rule on a link's name still matches).
    Raises ``ValueError`` if the rewritten text would not reach the same file."""
    if value[:1] not in ("/", "\\") or Path(value).drive:
        return value
    if _is_inside_any(Path(value).resolve(), permissions.effective_allowed_paths()):
        return value                                    # a real path inside the sandbox keeps its meaning
    stripped = value.lstrip("/\\") or "."
    if _resolve_for_ops(stripped, permissions) != _resolve_for_ops(value, permissions):
        raise ValueError(value)
    return stripped


def _as_the_tool_will_see_paths(args: Dict[str, Any], permissions: Optional[Permissions]) -> Dict[str, Any]:
    """``args`` with each rooted-but-outside-the-sandbox path argument replaced by the project-relative text
    the tool reads it as (see ``_reroot``); the gate and the tool are then given this same dict. A path the
    tool rejects (outside the sandbox) is left as written: the gate still sees it and the tool raises as it
    always does."""
    if permissions is None:
        return args
    rewritten = dict(args)
    for key in PATH_ARGUMENTS:
        value = rewritten.get(key)
        if isinstance(value, str) and value:
            try:
                rewritten[key] = _reroot(value, permissions)
            except ValueError:
                raise
            except Exception:                   # the tool rejects it (outside the sandbox): leave it as written
                pass
    return rewritten


_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_LOCKS_GUARD = Lock()


def approval_lock(gate: Gate) -> Lock:
    """The one lock for ``gate``: every tool set built on the same gate shares it, so only one approval
    prompt is open at a time. It lives as long as the gate does."""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(gate)
        if lock is None:
            lock = _LOCKS[gate] = Lock()
        return lock


def gate_tool(tool: StructuredTool, gate: Gate, *, permissions: Optional[Permissions] = None,
              lock: Optional[Lock] = None) -> StructuredTool:
    """``tool`` with the gate in front: same name, schema and description; the function asks the gate first
    and hands the model the refusal text instead of running when the gate says no.

    The function takes keyword arguments only (a positional call is a ``TypeError`` before anything runs).
    The arguments are validated through the tool's schema first, exactly as the framework's runner does
    before it calls the function, so the gate judges the same dict the tool receives: defaults filled in,
    values coerced, unknown arguments dropped, and (with ``permissions``) a rooted path the tool re-roots into
    the workspace written as the project-relative text it means. If the gate itself fails, the call is refused (fail closed). ``lock`` (default: the
    gate's own, see ``approval_lock``) is held around the gate check only, so with parallel tool workers one
    approval prompt is open at a time."""
    name = tool.name
    inner = tool.func
    schema = tool.args_schema
    approvals = lock if lock is not None else approval_lock(gate)

    def gated(**kwargs):
        try:
            args = schema(**kwargs).model_dump()
        except Exception as error:                  # invalid arguments: nothing runs, the model can correct them
            return f"ERROR: invalid arguments for {name}: {error}"
        try:
            args = _as_the_tool_will_see_paths(args, permissions)
        except ValueError:                          # a rooted path whose two readings disagree: do not guess
            return UNSUPPORTED_PATH
        try:
            with approvals:
                refusal = gate.check(name, dict(args))
        except Exception:                           # the gate failing must never let the tool through
            return GATE_FAILED
        if refusal is not None:
            return refusal
        return inner(**args)

    # The framework fingerprints a tool by its function's module and qualified name; give each its own.
    gated.__name__ = name
    gated.__qualname__ = f"gated_tool.{name}"
    gated.__module__ = __name__
    wrapped = StructuredTool(func=gated, args_schema=schema, name=name, description=tool.description)
    wrapped.cacheable = False            # a cached approval or refusal must never be replayed for a later call
    return wrapped


def build_tools(cwd: Path, add_dirs: Sequence[Path], gate: Gate) -> List[StructuredTool]:
    """Every default tool, sandboxed to ``cwd`` (plus ``add_dirs``), each behind ``gate``."""
    permissions = Permissions(
        read_files=True, list_directories=True, write_files=True, edit_files=True,
        delete_files=True, move_files=True, execute_python=True, execute_shell=True,
        workspace=str(cwd), allowed_paths=[str(cwd), *[str(d) for d in add_dirs]],
        auto_create_paths=False,         # a mistyped --add-dir must not create a folder
    )
    return [gate_tool(tool, gate, permissions=permissions)
            for tool in DefaultTools.build(permissions)]
