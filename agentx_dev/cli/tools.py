"""The tool set agentx gives the model: the framework's default tools, sandboxed to the project and
wrapped in the gate."""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence

from agentx_dev.DefaultTools import DefaultTools, Permissions
from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.approval import Gate

GATE_FAILED = "Refused: the approval check failed, so nothing was run."


def gate_tool(tool: StructuredTool, gate: Gate) -> StructuredTool:
    """``tool`` with the gate in front: same name, schema and description; the function asks the gate first
    and hands the model the refusal text instead of running when the gate says no.

    The function takes keyword arguments only (a positional call is a ``TypeError`` before anything runs).
    The arguments are validated through the tool's schema first, exactly as the framework's runner does
    before it calls the function, so the gate judges the same dict the tool receives: defaults filled in,
    values coerced, unknown arguments dropped. If the gate itself fails, the call is refused (fail closed)."""
    name = tool.name
    inner = tool.func
    schema = tool.args_schema

    def gated(**kwargs):
        try:
            args = schema(**kwargs).model_dump()
        except Exception as error:                  # invalid arguments: nothing runs, the model can correct them
            return f"ERROR: invalid arguments for {name}: {error}"
        try:
            refusal = gate.check(name, dict(args))
        except Exception:                           # the gate failing must never let the tool through
            return GATE_FAILED
        if refusal is not None:
            return refusal
        return inner(**args)

    wrapped = StructuredTool(func=gated, args_schema=schema, name=name, description=tool.description)
    wrapped.cacheable = False            # a cached approval or refusal must never be replayed for a later call
    return wrapped


def build_tools(cwd: Path, add_dirs: Sequence[Path], gate: Gate) -> List[StructuredTool]:
    """Every default tool, sandboxed to ``cwd`` (plus ``add_dirs``), each behind ``gate``."""
    permissions = Permissions(
        read_files=True, list_directories=True, write_files=True, edit_files=True,
        delete_files=True, move_files=True, execute_python=True, execute_shell=True,
        workspace=str(cwd), allowed_paths=[str(cwd), *[str(d) for d in add_dirs]],
    )
    return [gate_tool(tool, gate) for tool in DefaultTools.build(permissions)]
