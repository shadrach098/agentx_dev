"""Which files a run changed, from its tool calls. Shared by the runner (the note on an empty final answer) and the
CLI (the team's recaps), so the runner never imports the CLI."""

from typing import Any, Dict, List, Tuple

CHANGING_TOOLS = ("write_file", "edit_file", "delete_path")
REFUSAL_STARTS = ("Refused", "The user declined", "ERROR", "Error")


def changed_paths(tool_calls: Any, tools: Tuple[str, ...] = CHANGING_TOOLS) -> List[str]:
    """The paths of the files written, edited or deleted (or just those of ``tools``), in order, de-duplicated; calls that were refused or
    failed (the result starts with one of ``REFUSAL_STARTS``) are left out."""
    paths: Dict[str, None] = {}
    for call in tool_calls or []:
        if getattr(call, "name", "") not in tools:
            continue
        result = str(getattr(call, "result", "") or "").lstrip()
        if result.startswith(REFUSAL_STARTS):
            continue
        args = getattr(call, "args", None)
        args = args.get("Input", args) if isinstance(args, dict) else {}
        path = args.get("path") if isinstance(args, dict) else None
        if path:
            paths.setdefault(str(path), None)
    return list(paths)
