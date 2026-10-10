"""The tool set agentx gives the model: the framework's default tools, sandboxed to the project and
wrapped in the gate."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence

from agentx_dev.DefaultTools import DefaultTools, Permissions, _resolve_for_ops
from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.approval import Gate

GATE_FAILED = "Refused: the approval check failed, so nothing was run."
# The arguments that name a place on disk; ``cwd`` is run_shell's working folder.
PATH_ARGUMENTS = ("path", "source", "destination", "cwd")


def _as_the_tool_will_see_paths(args: Dict[str, Any], permissions: Optional[Permissions]) -> Dict[str, Any]:
    """``args`` with each path argument replaced by the file the tool will really act on.

    The tools resolve a path with the framework's own ``_resolve_for_ops`` (a rooted path such as ``/x``
    that lies outside the sandbox means ``<workspace>/x``, for example). Calling that same function here, with
    the same ``Permissions`` the tools were built with, means the gate judges, and the prompt names, the
    path the tool opens, and the tool is handed that same path. A path the tool rejects (outside the
    sandbox) is left as written: the gate still sees it and the tool raises as it always does."""
    if permissions is None:
        return args
    resolved = dict(args)
    for key in PATH_ARGUMENTS:
        value = resolved.get(key)
        if isinstance(value, str) and value:
            try:
                resolved[key] = str(_resolve_for_ops(value, permissions))
            except Exception:
                pass
    return resolved


def gate_tool(tool: StructuredTool, gate: Gate, *, permissions: Optional[Permissions] = None,
              lock: Optional[Lock] = None) -> StructuredTool:
    """``tool`` with the gate in front: same name, schema and description; the function asks the gate first
    and hands the model the refusal text instead of running when the gate says no.

    The function takes keyword arguments only (a positional call is a ``TypeError`` before anything runs).
    The arguments are validated through the tool's schema first, exactly as the framework's runner does
    before it calls the function, so the gate judges the same dict the tool receives: defaults filled in,
    values coerced, unknown arguments dropped, and (with ``permissions``) paths resolved the way the tool
    resolves them. If the gate itself fails, the call is refused (fail closed). ``lock`` is held around the
    gate check only, so with parallel tool workers one approval prompt is open at a time."""
    name = tool.name
    inner = tool.func
    schema = tool.args_schema
    approvals = lock if lock is not None else Lock()

    def gated(**kwargs):
        try:
            args = schema(**kwargs).model_dump()
        except Exception as error:                  # invalid arguments: nothing runs, the model can correct them
            return f"ERROR: invalid arguments for {name}: {error}"
        args = _as_the_tool_will_see_paths(args, permissions)
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
    approvals = Lock()                   # shared: one approval prompt at a time across all these tools
    return [gate_tool(tool, gate, permissions=permissions, lock=approvals)
            for tool in DefaultTools.build(permissions)]
