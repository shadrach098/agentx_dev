"""The gate between the model and the tools: what runs, what asks, what is refused."""

from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import sys
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
# Characters that chain, pipe, redirect, substitute or group. Any character outside printable ASCII
# (control characters, NBSP, Unicode spaces and line breaks) counts as one too; see ``_is_marked``.
_COMPOUND_MARKERS = (";", "&", "|", "`", "$(", "<", ">", "\n", "(", ")")
# The tools whose rule patterns are path-normalized (``move_file`` included; its subject is "src -> dst").
_PATH_TOOLS = frozenset({
    "read_path", "list_directory", "find_files", "grep", "write_file", "edit_file", "delete_path", "move_file",
})
_FOLDS_CASE = sys.platform in ("win32", "darwin")
# "Don't ask again" is never offered for a first word that is, or starts with, one of these stems: they
# run whatever they are handed next (``python3.12-dbg``, ``php8.2``, ``ts-node``, ``pythonw``...).
NO_GRANT_PREFIXES = (
    "python", "pypy", "ipython", "node", "deno", "bun", "tsx", "ts-node", "ruby", "perl", "php", "lua", "java",
    "dotnet", "awk", "gawk", "rscript", "cscript", "wscript", "mshta", "pwsh", "powershell", "bash", "zsh",
    "fish", "wsl", "uv", "uvx", "pipx", "npx", "sudo", "ssh", "eval", "exec", "xargs", "nohup", "timeout",
    "source",
)
# Short stems that would also catch harmless names (``shellcheck``, ``environment-cli``) match by equality only.
NO_GRANT_EXACT = frozenset({"sh", "su", "cmd", "time", "env", "call", "start", "py", "pyw", ".",
                             "nice", "doas", "setsid", "stdbuf", "watch", "busybox", "dash", "ksh", "tclsh",
                             "julia", "osascript", "rundll32", "forfiles"})
NO_GRANT = frozenset(NO_GRANT_PREFIXES) | NO_GRANT_EXACT
# Two-word prefixes that run a package the model names.
_NO_GRANT_RUNNERS = frozenset({"npm exec", "npm x", "pnpm dlx", "pnpm exec", "yarn dlx", "yarn exec"})
_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
_PLAIN_WORD = re.compile(r"[A-Za-z0-9._+/-]+")
_MAX_SUBJECTS = 200        # strings of an unknown tool's arguments a rule is matched against
_MAX_NODES = 10_000        # nodes the argument walk visits before giving up (and treating the call as compound)
_ABSOLUTE_PATTERN = re.compile(r"(/|[A-Za-z]:/)")
_GLOB_CHARS = re.compile(r"[*?\[]")

PLAN_REFUSAL = "Refused: plan mode is on. Propose a plan in your answer; do not change anything."
NO_WAY_TO_ASK = "Refused: there is no way to ask the user here."
UNSUPPORTED_PATH = "Refused: that path form is not supported. Use a normal relative or absolute path."
NOT_AN_OBJECT = "Refused: the tool arguments were not an object."
TOO_LARGE = "Refused: the tool arguments are too large to check."
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


def _text(value: Any) -> str:
    """``str(value)`` that cannot raise on a model-supplied object (a 5000-deep list, say)."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:       # includes RecursionError
        return ""


def _squash(text: Any) -> str:
    return " ".join(_text(text).split())


def unusual_path(path: Any) -> bool:
    """Windows path forms the gate cannot judge by name, so it refuses them: UNC and device paths
    (``\\\\host\\x``, ``\\\\?\\C:\\x``, ``\\??\\C:\\x``), alternate data streams (``README.md::$DATA``),
    drive-relative paths (``C:README.md``) and any component that ends in a dot or a space (Windows drops
    those, so ``README.md.`` or ``. /.env`` name a different file than they read). Always False off
    Windows, where these are ordinary file names."""
    if os.name != "nt":
        return False
    text = _text(path)
    unified = text.replace("\\", "/")
    if unified.startswith("//") or unified.startswith("/??/"):
        return True
    for index, char in enumerate(text):
        if char == ":" and not (index == 1 and text[0].isalpha()):
            return True
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha() and (len(text) == 2 or text[2] not in "\\/"):
        return True
    return any(part not in ("", ".", "..") and part.endswith((".", " ")) for part in unified.split("/"))


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


def _finish(text: str) -> str:
    while text.startswith("./"):
        text = text[2:]
    return text.casefold() if _FOLDS_CASE else text


def _lexical(text: str, cwd: Optional[Union[str, Path]]) -> str:
    """``text`` with ``.`` and ``..`` folded away on the text alone (the file system is never touched). With
    ``cwd``, a path that lies lexically inside it becomes the relative remainder, so ``../proj/src/a.py``
    and ``<abs>/proj/src/a.py`` both give ``src/a.py``; any other path keeps its normalized spelling."""
    plain = posixpath.normpath(text)
    if cwd is None:
        return plain
    try:
        base = os.path.abspath(os.fspath(cwd))
        joined = os.path.abspath(os.path.join(base, text))
        if os.path.normcase(os.path.commonpath([base, joined])) != os.path.normcase(base):
            return plain
        return os.path.relpath(joined, base).replace("\\", "/")
    except (ValueError, OSError, TypeError):
        return plain


def path_forms(path: Any, cwd: Optional[Union[str, Path]] = None) -> list:
    """The two spellings a path rule is matched against. ``~`` is not expanded (the tools do not).

    * the lexical form: backslashes as ``/``, ``.`` and ``..`` folded away on the text alone (no file system),
      relative to ``cwd`` when the path lies lexically inside it, so ``../x`` stays ``../x`` and a link named
      ``secrets`` is still ``secrets/...``;
    * the resolved form (only with ``cwd``): the path resolved against ``cwd``, links followed, relative to
      ``cwd`` when it lies inside and absolute otherwise.

    Letter case is folded on Windows and macOS. The list holds one entry when the two agree."""
    text = _text(path).replace("\\", "/")
    forms = [_finish(_lexical(text, cwd))]
    if cwd is not None:
        resolved = _resolve(text, cwd)
        if resolved is not None and _finish(resolved) not in forms:
            forms.append(_finish(resolved))
    return forms


def normalize_path(path: Any, cwd: Optional[Union[str, Path]] = None) -> str:
    """The resolved form of ``path`` (the lexical form when there is no ``cwd``): what a prompt shows."""
    return path_forms(path, cwd)[-1]


def _first_line(code: Any) -> str:
    text = _text(code).strip()
    return _squash(text.splitlines()[0]) if text else ""


def move_final_paths(source: Any, destination: Any, cwd: Optional[Union[str, Path]] = None) -> list:
    """Where ``move_file`` really puts the file, as paths spelled like ``destination``, when ``destination`` is
    an existing folder: ``shutil.move`` then moves the source *into* it, to ``destination/<name>``. Empty when
    the destination is not an existing folder (the file lands at ``destination`` itself). The name is the
    source's name after links are followed (what the tool moves) first, then the name as written."""
    dest = _text(destination)
    source_text = _text(source)
    if unusual_path(source_text) or unusual_path(dest):
        return []        # the gate refuses these before touching the disk; drawing a tool line must not either
    try:
        base = Path(cwd).resolve() if cwd is not None else Path.cwd()
        if not Path(base, dest).resolve().is_dir():
            return []
        names = [Path(base, source_text).resolve().name]
    except (OSError, ValueError, RuntimeError):
        return []
    written = posixpath.basename(source_text.replace("\\", "/").rstrip("/"))
    if written and written not in (".", ".."):
        names.append(written)
    folder = dest.rstrip("/\\")
    rooted = not folder and dest[:1] in ("/", "\\")           # the destination is "/" itself
    return [f"{folder}/{name}" if folder or rooted else name for name in dict.fromkeys(names) if name]


def subject_of(tool: str, args: Mapping[str, Any], cwd: Optional[Union[str, Path]] = None) -> str:
    """The short text a prompt names (the resolved path, the command, the first line of code)."""
    if tool == "move_file":
        destination = args.get("destination", "")
        final = move_final_paths(args.get("source", ""), destination, cwd)    # into a folder: where it lands
        return (f"{normalize_path(args.get('source', ''), cwd)} -> "
                f"{normalize_path(final[0] if final else destination, cwd)}")
    if tool in _PATH_TOOLS and "path" in args:
        return normalize_path(args["path"], cwd)
    return subjects_of(tool, args, cwd)[0]


def subjects_of(tool: str, args: Mapping[str, Any], cwd: Optional[Union[str, Path]] = None) -> list:
    """Every text a rule is matched against for this call. A deny rule refuses if any matches; an allow
    rule applies only if all do. Never empty.

    * ``run_shell``: the command; ``run_python``: the first line of the code;
    * a path tool: the lexical and the resolved form of the path (``move_file``: of the pair, and when the
      destination is an existing folder also of the final path ``destination/<name>``);
    * a tool with a string ``command``: that command alone;
    * anything else: every string in the arguments (at most ``_MAX_SUBJECTS``)."""
    if tool == "run_shell":
        return [_squash(args.get("command", ""))]
    if tool == "run_python":
        return [_first_line(args.get("code", ""))]
    if tool == "move_file":
        sources = path_forms(args.get("source", ""), cwd)
        destinations = path_forms(args.get("destination", ""), cwd)
        for final in move_final_paths(args.get("source", ""), args.get("destination", ""), cwd):
            destinations += [form for form in path_forms(final, cwd) if form not in destinations]
        return list(dict.fromkeys(f"{source} -> {destination}" for source in sources for destination in destinations))
    if tool in _PATH_TOOLS and "path" in args:
        return path_forms(args["path"], cwd)
    if isinstance(args.get("command"), str):
        return [_squash(args["command"])]
    found, _ = _strings(args)
    return [_squash(text) for text in found[:_MAX_SUBJECTS]] or [""]


def command_prefix(command: str) -> str:
    """What "don't ask again" remembers: the first word, or the first two for git, npm, pip and friends.
    Words are split on spaces and tabs only, never on Unicode whitespace."""
    command = command.strip(" \t")
    if not command:
        return ""
    words = re.split(r"[ \t]+", command)
    if words[0] in _TWO_WORD_COMMANDS and len(words) > 1:
        return " ".join(words[:2])
    return words[0]


def _path_spelling(text: str) -> str:
    """Slashes, a leading ``./`` and (on Windows and macOS) case, so a path rule and a path subject compare fairly."""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.casefold() if _FOLDS_CASE else text


def _pattern_variants(pattern: str, cwd: Optional[Union[str, Path]]) -> list:
    """The spellings of one path pattern to try: as written, and (with ``cwd``) anchored against the project
    folder the way subjects are spelled, when the pattern is absolute or its literal directory climbs out
    with ``..`` (``../shared/*``). The anchored form is relative to ``cwd`` when the directory lies inside
    it, and the resolved absolute path otherwise."""
    variants = [pattern]
    if cwd is None:
        return variants
    glob = _GLOB_CHARS.search(pattern)
    literal = pattern[:glob.start()] if glob else pattern
    cut = literal.rfind("/") + 1
    directory, tail = pattern[:cut], pattern[cut:]
    if not _ABSOLUTE_PATTERN.match(pattern):
        folded = posixpath.normpath(directory) if directory else ""
        if folded != ".." and not folded.startswith("../"):
            return variants
    resolved = _resolve(directory, cwd)
    if resolved is not None:
        variants.append(tail if resolved == "." else resolved.rstrip("/") + "/" + tail)
    return variants


def _path_patterns(tool: str, pattern: str, cwd: Optional[Union[str, Path]]) -> list:
    """Every spelling of a path rule's pattern to match a subject against. A leading ``~`` is the home
    folder here (the user wrote the settings file); a ``move_file`` pattern ``a -> b`` is handled per side."""
    sides = pattern.split(" -> ", 1) if tool == "move_file" and " -> " in pattern else [pattern]
    options = []
    for side in sides:
        if side.startswith("~"):
            side = os.path.expanduser(side)
        side = side.replace("\\", "/")
        options.append([_path_spelling(variant) for variant in _pattern_variants(side, cwd)])
    if len(options) == 1:
        return options[0]
    return [f"{left} -> {right}" for left in options[0] for right in options[1]]


def rule_matches(rule: str, tool: str, subject: str, *, cwd: Optional[Union[str, Path]] = None) -> bool:
    """``"tool:glob"`` matches that tool when the glob matches the subject; ``"tool"`` matches every call.
    For the path tools both sides are compared in one spelling (see ``path_forms``); ``cwd`` lets an
    absolute pattern, or one that climbs out with ``..``, also match the subject spelled the other way."""
    name, sep, pattern = rule.partition(":")
    if not fnmatch.fnmatchcase(tool, name.strip()):
        return False
    if not sep:
        return True
    if tool in _PATH_TOOLS:
        subject = _path_spelling(subject)
        return any(fnmatch.fnmatchcase(subject, variant) for variant in _path_patterns(tool, pattern, cwd))
    return fnmatch.fnmatchcase(subject, pattern)


def _is_marked(text: str) -> bool:
    """Whether ``text`` holds a compound marker, or any character outside printable ASCII (tab allowed)."""
    if any(marker in text for marker in _COMPOUND_MARKERS):
        return True
    return any((ord(char) < 32 and char != "\t") or ord(char) > 126 for char in text)


def _strings(value: Any) -> tuple:
    """``(strings, truncated)``: every string inside an argument value, in order, found by an iterative walk
    over lists, tuples, sets and mappings (model-controlled input must never recurse). ``truncated`` is True
    when the walk stopped at ``_MAX_NODES`` (a huge or cyclic structure)."""
    found: list = []
    stack = [value]
    nodes = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > _MAX_NODES:
            return found, True
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, Mapping):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(reversed(list(item)))
    return found, False


def _is_compound(tool: str, args: Mapping[str, Any]) -> bool:
    """A command that chains, pipes, redirects, substitutes or groups: a rule or grant for its first word
    must not approve the rest of it. Covers ``run_shell`` (a command that is not a string counts as
    compound) and any command-kind tool that takes a string ``command``; for a tool the gate does not know,
    any string argument may be the command, so all are checked, and a structure too big to scan counts as
    compound."""
    if tool not in TOOL_KINDS:
        found, truncated = _strings(args)
        return truncated or len(found) > _MAX_SUBJECTS or any(_is_marked(text) for text in found)
    command = args.get("command")
    if tool == "run_shell":
        return not isinstance(command, str) or _is_marked(command)
    return kind_of(tool) == "command" and isinstance(command, str) and _is_marked(command)


def _grantable_prefix(command: str) -> Optional[str]:
    """What answering "a" to ``command`` may remember, or None when it must approve only that one call: the
    first word is not plain (letters, digits and ``. _ + / -`` only, so no assignment, quote, expansion,
    glob, brace or backslash), is an interpreter, shell or wrapper, or the command is a two-word one whose
    second word is an option or a package runner."""
    words = re.split(r"[ \t]+", command.strip(" \t"))
    first = words[0]
    if not _PLAIN_WORD.fullmatch(first):
        return None
    name = first.rsplit("/", 1)[-1].casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    if name in NO_GRANT_EXACT or name.startswith(NO_GRANT_PREFIXES):
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
        if tool not in TOOL_KINDS:
            strings, truncated = _strings(args)
            if truncated or len(strings) > _MAX_SUBJECTS:         # a deny rule could not see all of it
                return TOO_LARGE
        if any(unusual_path(path) for path in self._path_arguments(tool, args)):
            return UNSUPPORTED_PATH
        subject = subject_of(tool, args, self.cwd)
        subjects = subjects_of(tool, args, self.cwd)
        for rule in self.deny:                                  # a deny rule refuses if ANY form matches
            if any(rule_matches(rule, tool, text, cwd=self.cwd) for text in subjects):
                return f"Refused: the settings deny rule '{rule}' blocks this."
        if self._mode == "plan" and kind != "read":
            return PLAN_REFUSAL
        if kind == "read":
            return None
        compound = _is_compound(tool, args)
        if kind != "destructive" and not compound:
            # an allow rule applies only if EVERY form matches it
            if any(all(rule_matches(rule, tool, text, cwd=self.cwd) for text in subjects) for rule in self.allow) \
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
