"""The gate between the model and the tools: what runs, what asks, what is refused."""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Union

MODES = ("default", "accept-edits", "plan")

TOOL_KINDS = {
    "read_path": "read", "list_directory": "read", "find_files": "read", "grep": "read",
    "write_file": "edit", "edit_file": "edit",
    "run_shell": "command", "run_python": "command",
    "delete_path": "destructive", "move_file": "destructive",
}
_TWO_WORD_COMMANDS = frozenset({"git", "npm", "pnpm", "yarn", "pip", "cargo", "go", "docker", "make"})
# Characters that chain, pipe, redirect, substitute or group. Any character outside printable ASCII
# (control characters, NBSP, Unicode spaces and line breaks) counts as one too; see ``_is_marked``.
_COMPOUND_MARKERS = (";", "&", "|", "`", "$(", "<", ">", "\n", "(", ")")
# The tools whose rule patterns are path-normalized (``move_file`` included; its subject is "src -> dst").
_PATH_TOOLS = frozenset({
    "read_path", "list_directory", "find_files", "grep", "write_file", "edit_file", "delete_path", "move_file",
})
_FOLDS_CASE = sys.platform in ("win32", "darwin")
# "Don't ask again" is never offered for these: they run whatever they are handed next.
NO_GRANT = frozenset({
    "python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php", "bash", "sh", "zsh", "fish",
    "pwsh", "powershell", "cmd", "npx", "sudo", "su", "ssh", "eval", "exec", "env", "xargs", "nohup",
    "timeout", "time", "pythonw", "nodejs", "wsl", "start", "uv", "uvx", "pipx", ".", "source", "call",
})
# Two-word prefixes that run a package the model names.
_NO_GRANT_RUNNERS = frozenset({"npm exec", "npm x", "pnpm dlx", "pnpm exec", "yarn dlx", "yarn exec"})
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
_VERSIONED_INTERPRETER = re.compile(r"(python|pypy)[\d.]*")
_ABSOLUTE_PATTERN = re.compile(r"(/|[A-Za-z]:/)")
_GLOB_CHARS = re.compile(r"[*?\[]")

PLAN_REFUSAL = "Refused: plan mode is on. Propose a plan in your answer; do not change anything."
NO_WAY_TO_ASK = "Refused: there is no way to ask the user here."
UNSUPPORTED_PATH = "Refused: that path form is not supported. Use a normal relative or absolute path."
NOT_AN_OBJECT = "Refused: the tool arguments were not an object."
DECLINED = "The user declined this action."


@dataclass(frozen=True)
class Request:
    """What the gate asks the user about."""

    tool: str
    kind: str
    subject: str
    args: Mapping[str, Any]
    remember: bool = True      # False when answering "a" could not be remembered, so the prompt must not offer it
    grant: str = ""            # what answering "a" will remember, for the prompt to print ("" when remember is False)


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


def unusual_path(path: Any) -> bool:
    """Windows path forms the gate cannot judge by name: UNC and device paths (``\\\\host\\x``, ``\\\\?\\C:\\x``),
    alternate data streams (``README.md::$DATA``) and drive-relative paths (``C:README.md``). Always False
    off Windows, where these are ordinary file names."""
    if os.name != "nt":
        return False
    text = str(path)
    if text[:2] in ("\\\\", "//", "\\/", "/\\"):
        return True
    for index, char in enumerate(text):
        if char == ":" and not (index == 1 and text[0].isalpha()):
            return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha() and (len(text) == 2 or text[2] not in "\\/")


def _resolve(text: str, cwd: Union[str, Path]) -> Optional[str]:
    """``text`` resolved against ``cwd``: a path relative to it (as posix) when it lies inside, the absolute
    posix path when it lies outside, or None when it cannot be resolved."""
    try:
        base = Path(cwd).resolve()
        full = Path(base, text).resolve()
        try:
            return full.relative_to(base).as_posix()
        except ValueError:
            return full.as_posix()
    except (OSError, ValueError, RuntimeError):
        return None


def normalize_path(path: Any, cwd: Optional[Union[str, Path]] = None) -> str:
    """One spelling per path, so a rule cannot be dodged by ``~``, ``./``, ``..``, backslashes, an absolute
    path into the project, trailing dots or spaces (Windows) or letter case (Windows, macOS). With ``cwd``
    the path is resolved against it and made relative when it lies inside."""
    text = str(path)
    if text.startswith("~"):
        text = os.path.expanduser(text)
    text = text.replace("\\", "/")
    if os.name == "nt":
        text = "/".join(part if part in ("", ".", "..") else part.rstrip(". ") for part in text.split("/"))
    resolved = _resolve(text, cwd) if cwd is not None else None
    text = resolved if resolved is not None else posixpath.normpath(text)
    while text.startswith("./"):
        text = text[2:]
    return text.casefold() if _FOLDS_CASE else text


def subject_of(tool: str, args: Mapping[str, Any], cwd: Optional[Union[str, Path]] = None) -> str:
    """The short text a rule is matched against and a prompt names."""
    if tool == "run_shell":
        return _squash(args.get("command", ""))
    if tool == "run_python":
        code = str(args.get("code", "")).strip()
        return _squash(code.splitlines()[0]) if code else ""
    if tool == "move_file":
        return f"{normalize_path(args.get('source', ''), cwd)} -> {normalize_path(args.get('destination', ''), cwd)}"
    if tool in _PATH_TOOLS and "path" in args:
        return normalize_path(args["path"], cwd)
    if isinstance(args.get("command"), str):          # whatever the tool is called, the command is the subject
        return _squash(args["command"])
    if "path" in args:
        return normalize_path(args["path"], cwd)
    for value in args.values():
        if isinstance(value, str):
            return value
    return ""


def command_prefix(command: str) -> str:
    """What "don't ask again" remembers: the first word, or the first two for git, npm, pip and friends.
    Words are split on spaces and tabs only, never on Unicode whitespace."""
    if not command.strip():
        return ""
    words = re.split(r"[ \t]+", command.strip())
    if words[0] in _TWO_WORD_COMMANDS and len(words) > 1:
        return " ".join(words[:2])
    return words[0]


def _path_spelling(text: str) -> str:
    """Slashes, a leading ``./`` and (on Windows and macOS) case, so a path rule and a path subject compare fairly."""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.casefold() if _FOLDS_CASE else text


def _anchor_pattern(pattern: str, cwd: Optional[Union[str, Path]]) -> str:
    """Spell an absolute pattern the way subjects are spelled: relative to ``cwd`` when its literal
    directory lies inside it, otherwise as the resolved absolute path."""
    if pattern.startswith("~"):
        pattern = os.path.expanduser(pattern).replace("\\", "/")
    if cwd is None or not _ABSOLUTE_PATTERN.match(pattern):
        return pattern
    glob = _GLOB_CHARS.search(pattern)
    literal = pattern[:glob.start()] if glob else pattern
    cut = literal.rfind("/") + 1
    directory, tail = pattern[:cut], pattern[cut:]
    resolved = _resolve(directory, cwd)
    if resolved is None:
        return pattern
    if resolved == ".":
        return tail
    return resolved.rstrip("/") + "/" + tail


def rule_matches(rule: str, tool: str, subject: str, *, cwd: Optional[Union[str, Path]] = None) -> bool:
    """``"tool:glob"`` matches that tool when the glob matches the subject; ``"tool"`` matches every call.
    For the path tools both sides are compared in one spelling (see ``normalize_path``); ``cwd`` lets an
    absolute pattern inside the project match the relative subjects."""
    name, sep, pattern = rule.partition(":")
    if not fnmatch.fnmatchcase(tool, name.strip()):
        return False
    if not sep:
        return True
    if tool in _PATH_TOOLS:
        pattern = pattern.replace("\\", "/")
        if tool != "move_file":
            pattern = _anchor_pattern(pattern, cwd)
        pattern, subject = _path_spelling(pattern), _path_spelling(subject)
    return fnmatch.fnmatchcase(subject, pattern)


def _is_marked(text: str) -> bool:
    """Whether ``text`` holds a compound marker, or any character outside printable ASCII (tab allowed)."""
    if any(marker in text for marker in _COMPOUND_MARKERS):
        return True
    return any((ord(char) < 32 and char != "\t") or ord(char) > 126 for char in text)


def _strings(value: Any) -> Iterator[str]:
    """Every string inside an argument value, looking into lists, tuples and mappings."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)


def _is_compound(tool: str, args: Mapping[str, Any]) -> bool:
    """A command that chains, pipes, redirects, substitutes or groups: a rule or grant for its first word
    must not approve the rest of it. Covers ``run_shell`` and any command-kind tool that takes a string
    ``command``; for a tool the gate does not know, any string argument may be the command, so all are checked."""
    if tool not in TOOL_KINDS:
        return any(_is_marked(text) for text in _strings(args))
    command = args.get("command")
    if tool != "run_shell" and not (kind_of(tool) == "command" and isinstance(command, str)):
        return False
    return _is_marked(str(command or ""))


def _grantable_prefix(command: str) -> Optional[str]:
    """What answering "a" to ``command`` may remember, or None when it must approve only that one call: the
    first word is an assignment, quoted, expanded, an interpreter, shell or wrapper, or a two-word command
    whose second word is an option or a package runner."""
    words = re.split(r"[ \t]+", command.strip())
    first = words[0]
    if not first or any(char in first for char in "=\"'`$") or (os.name != "nt" and "\\" in first):
        return None
    name = first.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name in NO_GRANT or _VERSIONED_INTERPRETER.fullmatch(name):
        return None
    prefix = command_prefix(command)
    if first in _TWO_WORD_COMMANDS and len(words) > 1:
        if words[1].startswith("-") or prefix in _NO_GRANT_RUNNERS:
            return None
    return prefix


class Gate:
    """Decides, for each tool call, to run it, ask the user, or refuse it.

    ``ask`` is a function ``(Request) -> Answer``. With ``ask=None`` nothing can be asked, so every
    call that would ask is refused. ``check`` returns ``None`` to go ahead, or the refusal text to
    hand back to the model as the tool's result. It does not raise on any model-supplied arguments."""

    def __init__(self, *, mode: str = "default", allow: Iterable[str] = (), deny: Iterable[str] = (),
                 ask: Optional[Callable[[Request], Answer]] = None, cwd: Optional[Path] = None) -> None:
        self.allow = list(allow)
        self.deny = list(deny)
        self.ask = ask
        if cwd is None:
            try:
                cwd = Path.cwd()
            except OSError:
                cwd = None
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
        if not isinstance(args, Mapping):
            return NOT_AN_OBJECT
        kind = kind_of(tool)
        if any(unusual_path(path) for path in self._path_arguments(tool, args)):
            return UNSUPPORTED_PATH
        subject = subject_of(tool, args, self.cwd)
        for rule in self.deny:
            if rule_matches(rule, tool, subject, cwd=self.cwd):
                return f"Refused: the settings deny rule '{rule}' blocks this."
        if self._mode == "plan" and kind != "read":
            return PLAN_REFUSAL
        if kind == "read":
            return None
        compound = _is_compound(tool, args)
        if kind != "destructive" and not compound:
            if any(rule_matches(rule, tool, subject, cwd=self.cwd) for rule in self.allow) \
                    or self._granted(tool, subject):
                return None
        if kind == "edit" and self._mode == "accept-edits":
            return None
        if self.ask is None:
            return NO_WAY_TO_ASK
        grant = self._grant_for(tool, kind, subject, compound)
        answer = self.ask(Request(tool=tool, kind=kind, subject=subject, args=dict(args),
                                  remember=grant is not None, grant=grant or ""))
        if not isinstance(answer, Answer):
            return DECLINED
        if answer.choice == "n" and answer.reason:
            return f"The user declined: {answer.reason}"
        if answer.choice not in ("y", "a"):             # "n", and anything the gate does not understand
            return DECLINED
        if answer.choice == "a" and grant is not None:
            self._remember(kind, grant)
        return None

    @staticmethod
    def _path_arguments(tool: str, args: Mapping[str, Any]) -> list:
        if tool == "move_file":
            return [args.get("source", ""), args.get("destination", "")]
        if tool in _PATH_TOOLS and "path" in args:
            return [args["path"]]
        return []

    def _granted(self, tool: str, subject: str) -> bool:
        return tool == "run_shell" and command_prefix(subject) in self.grants

    @staticmethod
    def _grant_for(tool: str, kind: str, subject: str, compound: bool) -> Optional[str]:
        """What answering "a" will remember, or None when it cannot be honoured: never for destructive
        tools, compound commands, interpreters/shells/wrappers, unusual first words, or ``run_python``
        (which approves only that one call)."""
        if kind == "edit":
            return "all edits this session"
        if tool == "run_shell" and not compound:
            return _grantable_prefix(subject)
        return None

    def _remember(self, kind: str, grant: str) -> None:
        if kind == "edit":
            self._mode = "accept-edits"
        else:
            self.grants.add(grant)
