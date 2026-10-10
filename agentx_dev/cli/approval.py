"""The gate between the model and the tools: what runs, what asks, what is refused."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

MODES = ("default", "accept-edits", "plan")

TOOL_KINDS = {
    "read_path": "read", "list_directory": "read", "find_files": "read", "grep": "read",
    "write_file": "edit", "edit_file": "edit",
    "run_shell": "command", "run_python": "command",
    "delete_path": "destructive", "move_file": "destructive",
}
_TWO_WORD_COMMANDS = frozenset({"git", "npm", "pnpm", "yarn", "pip", "cargo", "go", "docker", "make"})
_COMPOUND_MARKERS = (";", "&", "|", "`", "$(", "<", ">", "\n")

PLAN_REFUSAL = "Refused: plan mode is on. Propose a plan in your answer; do not change anything."
NO_WAY_TO_ASK = "Refused: there is no way to ask the user here."


@dataclass(frozen=True)
class Request:
    """What the gate asks the user about."""

    tool: str
    kind: str
    subject: str
    args: Mapping[str, Any]


@dataclass
class Answer:
    """The user's answer: ``y`` (this once), ``a`` (don't ask again) or ``n`` (no, with a reason)."""

    choice: str
    reason: str = ""


def kind_of(tool: str) -> str:
    """read / edit / command / destructive. A tool the gate does not know counts as a command."""
    return TOOL_KINDS.get(tool, "command")


def _squash(text: Any) -> str:
    return " ".join(str(text).split())


def subject_of(tool: str, args: Mapping[str, Any]) -> str:
    """The short text a rule is matched against and a prompt names."""
    if tool == "run_shell":
        return _squash(args.get("command", ""))
    if tool == "run_python":
        code = str(args.get("code", "")).strip()
        return _squash(code.splitlines()[0]) if code else ""
    if tool == "move_file":
        return f"{args.get('source', '')} -> {args.get('destination', '')}"
    if "path" in args:
        return str(args["path"])
    for value in args.values():
        if isinstance(value, str):
            return value
    return ""


def command_prefix(command: str) -> str:
    """What "don't ask again" remembers: the first word, or the first two for git, npm, pip and friends."""
    words = command.split()
    if not words:
        return ""
    if words[0] in _TWO_WORD_COMMANDS and len(words) > 1:
        return " ".join(words[:2])
    return words[0]


def rule_matches(rule: str, tool: str, subject: str) -> bool:
    """``"tool:glob"`` matches that tool when the glob matches the subject; ``"tool"`` matches every call."""
    name, sep, pattern = rule.partition(":")
    if not fnmatch.fnmatchcase(tool, name.strip()):
        return False
    return (not sep) or fnmatch.fnmatchcase(subject, pattern)


def _is_compound(tool: str, args: Mapping[str, Any]) -> bool:
    """A shell command that chains, pipes, redirects or substitutes: a rule or grant for its first
    word must not approve the rest of it."""
    if tool != "run_shell":
        return False
    command = str(args.get("command", ""))
    return any(marker in command for marker in _COMPOUND_MARKERS)


class Gate:
    """Decides, for each tool call, to run it, ask the user, or refuse it.

    ``ask`` is a function ``(Request) -> Answer``. With ``ask=None`` nothing can be asked, so every
    call that would ask is refused. ``check`` returns ``None`` to go ahead, or the refusal text to
    hand back to the model as the tool's result."""

    def __init__(self, *, mode: str = "default", allow: Iterable[str] = (), deny: Iterable[str] = (),
                 ask: Optional[Callable[[Request], Answer]] = None) -> None:
        self.allow = list(allow)
        self.deny = list(deny)
        self.ask = ask
        self.grants: set = set()
        self._mode = "default"
        self.set_mode(mode)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of: {', '.join(MODES)}")
        self._mode = mode

    def cycle_mode(self) -> str:
        self._mode = MODES[(MODES.index(self._mode) + 1) % len(MODES)]
        return self._mode

    def check(self, tool: str, args: Mapping[str, Any]) -> Optional[str]:
        kind = kind_of(tool)
        subject = subject_of(tool, args)
        for rule in self.deny:
            if rule_matches(rule, tool, subject):
                return f"Refused: the settings deny rule '{rule}' blocks this."
        if self._mode == "plan" and kind != "read":
            return PLAN_REFUSAL
        if kind == "read":
            return None
        compound = _is_compound(tool, args)
        if kind != "destructive" and not compound:
            if any(rule_matches(rule, tool, subject) for rule in self.allow) or self._granted(tool, subject):
                return None
        if kind == "edit" and self._mode == "accept-edits":
            return None
        if self.ask is None:
            return NO_WAY_TO_ASK
        answer = self.ask(Request(tool=tool, kind=kind, subject=subject, args=dict(args)))
        if answer.choice == "n":
            return f"The user declined: {answer.reason}" if answer.reason else "The user declined this action."
        if answer.choice == "a" and kind != "destructive" and not compound:
            self._remember(tool, kind, subject)
        return None

    def _granted(self, tool: str, subject: str) -> bool:
        if tool == "run_shell":
            return command_prefix(subject) in self.grants
        return tool == "run_python" and "run_python" in self.grants

    def _remember(self, tool: str, kind: str, subject: str) -> None:
        if kind == "edit":
            self._mode = "accept-edits"
        elif tool == "run_shell":
            prefix = command_prefix(subject)
            if prefix:
                self.grants.add(prefix)
        elif tool == "run_python":
            self.grants.add("run_python")
