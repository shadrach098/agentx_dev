"""The gate between the model and the tools: what runs, what asks, what is refused."""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Union

MODES = ("default", "accept-edits", "plan")

TOOL_KINDS = {
    "read_path": "read", "list_directory": "read", "find_files": "read", "grep": "read",
    "write_file": "edit", "edit_file": "edit",
    "run_shell": "command", "run_python": "command",
    "delete_path": "destructive", "move_file": "destructive",
}
_TWO_WORD_COMMANDS = frozenset({"git", "npm", "pnpm", "yarn", "pip", "cargo", "go", "docker", "make"})
_COMPOUND_MARKERS = (";", "&", "|", "`", "$(", "<", ">", "\n")
_PATH_TOOLS = frozenset({"read_path", "list_directory", "find_files", "grep", "write_file", "edit_file", "delete_path"})
# "Don't ask again" is never offered for these: they run whatever they are handed next.
NO_GRANT = frozenset({
    "python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php", "bash", "sh", "zsh", "fish",
    "pwsh", "powershell", "cmd", "npx", "sudo", "su", "ssh", "eval", "exec", "env", "xargs", "nohup",
    "timeout", "time",
})
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
_VERSIONED_INTERPRETER = re.compile(r"(python|pypy)[\d.]*")

PLAN_REFUSAL = "Refused: plan mode is on. Propose a plan in your answer; do not change anything."
NO_WAY_TO_ASK = "Refused: there is no way to ask the user here."


@dataclass(frozen=True)
class Request:
    """What the gate asks the user about."""

    tool: str
    kind: str
    subject: str
    args: Mapping[str, Any]
    remember: bool = True      # False when answering "a" could not be remembered, so the prompt must not offer it


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


def normalize_path(path: Any, cwd: Optional[Union[str, Path]] = None) -> str:
    """One spelling per path, so a rule cannot be dodged by ``./``, ``..``, backslashes, an absolute path
    into the project, or (on Windows) letter case."""
    text = str(path).replace("\\", "/")
    if cwd is not None:
        try:
            if Path(str(path)).is_absolute() or posixpath.isabs(text):
                text = Path(str(path)).resolve().relative_to(Path(cwd).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    text = posixpath.normpath(text)
    while text.startswith("./"):
        text = text[2:]
    return text.casefold() if os.name == "nt" else text


def subject_of(tool: str, args: Mapping[str, Any], cwd: Optional[Union[str, Path]] = None) -> str:
    """The short text a rule is matched against and a prompt names."""
    if tool == "run_shell":
        return _squash(args.get("command", ""))
    if tool == "run_python":
        code = str(args.get("code", "")).strip()
        return _squash(code.splitlines()[0]) if code else ""
    if tool == "move_file":
        return f"{normalize_path(args.get('source', ''), cwd)} -> {normalize_path(args.get('destination', ''), cwd)}"
    if "path" in args:
        return normalize_path(args["path"], cwd)
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
    if not sep:
        return True
    if tool in _PATH_TOOLS:
        pattern, subject = _path_spelling(pattern), _path_spelling(subject)
    return fnmatch.fnmatchcase(subject, pattern)


def _path_spelling(text: str) -> str:
    """Slashes, a leading ``./`` and (on Windows) case, so a path rule and a path subject compare fairly."""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.casefold() if os.name == "nt" else text


def _is_compound(tool: str, args: Mapping[str, Any]) -> bool:
    """A command that chains, pipes, redirects or substitutes: a rule or grant for its first word must
    not approve the rest of it. Applies to ``run_shell`` and to any command-kind tool (say, an MCP bash
    tool) that takes a string ``command``."""
    command = args.get("command")
    if tool != "run_shell" and not (kind_of(tool) == "command" and isinstance(command, str)):
        return False
    return any(marker in str(command or "") for marker in _COMPOUND_MARKERS)


def _grantable_word(command: str) -> bool:
    """False for an interpreter, shell or wrapper (``python``, ``/usr/bin/bash``, ``sudo.exe``...)."""
    words = command.split()
    if not words:
        return False
    name = words[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name not in NO_GRANT and not _VERSIONED_INTERPRETER.fullmatch(name)


class Gate:
    """Decides, for each tool call, to run it, ask the user, or refuse it.

    ``ask`` is a function ``(Request) -> Answer``. With ``ask=None`` nothing can be asked, so every
    call that would ask is refused. ``check`` returns ``None`` to go ahead, or the refusal text to
    hand back to the model as the tool's result."""

    def __init__(self, *, mode: str = "default", allow: Iterable[str] = (), deny: Iterable[str] = (),
                 ask: Optional[Callable[[Request], Answer]] = None, cwd: Optional[Path] = None) -> None:
        self.allow = list(allow)
        self.deny = list(deny)
        self.ask = ask
        self.cwd = cwd
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
        subject = subject_of(tool, args, self.cwd)
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
        can_remember = self._can_remember(tool, kind, subject, compound)
        answer = self.ask(Request(tool=tool, kind=kind, subject=subject, args=dict(args), remember=can_remember))
        if answer.choice == "n" and answer.reason:
            return f"The user declined: {answer.reason}"
        if answer.choice not in ("y", "a"):             # "n", and anything the gate does not understand
            return "The user declined this action."
        if answer.choice == "a" and can_remember:
            self._remember(tool, kind, subject)
        return None

    def _granted(self, tool: str, subject: str) -> bool:
        return tool == "run_shell" and command_prefix(subject) in self.grants

    @staticmethod
    def _can_remember(tool: str, kind: str, subject: str, compound: bool) -> bool:
        """Whether answering "a" can be honoured. Never for destructive tools, compound commands,
        interpreters/shells/wrappers, or ``run_python`` (which approves only that one call)."""
        if kind == "edit":
            return True
        return tool == "run_shell" and not compound and _grantable_word(subject)

    def _remember(self, tool: str, kind: str, subject: str) -> None:
        if kind == "edit":
            self._mode = "accept-edits"
        elif tool == "run_shell":
            prefix = command_prefix(subject)
            if prefix:
                self.grants.add(prefix)
