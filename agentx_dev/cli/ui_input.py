"""The chat prompt's helpers: what a typed line is (``classify_command``), its colours, the completer and the bottom toolbar. ``prompt_toolkit`` is imported inside ``make_completer`` only."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


from agentx_dev.cli.ui_text import ACCENT, plain


_SLASH_START = re.compile(r"(\s*)/([A-Za-z0-9_-]*)")


_STYLE_CLASS = {"command": "class:command", "skill": "class:skill", "unknown": "class:unknown-command",
                "partial": "class:partial-command"}


def classify_command(line: str, commands: Sequence[str],
                     skills: Optional[Mapping[str, str]] = None) -> Optional[Tuple[str, int]]:
    """``(kind, end)`` for a line that starts with a slash command, where ``end`` is where the ``/name`` ends;
    None for anything else (a message, ``/tmp/build.log``, ``/README.md``, ``/ is the root``). ``kind`` is
    ``"command"``, ``"skill"``, ``"partial"`` (the start of a command or skill, still being typed) or ``"unknown"``.
    The same reading as ``parse_slash``: a plain name right after the slash, then a space or the end."""
    match = _SLASH_START.match(line or "")
    if match is None:
        return None
    end = match.end()
    rest = line[end:]
    if rest and not rest[0].isspace():
        return None                                   # "/tmp/build.log", "/README.md": a message
    name = match.group(2)
    if not name:
        return ("partial", end) if not rest else None
    slash = "/" + name.lower()
    known = {command.lower() for command in commands}
    offered = {skill.lower() for skill in (skills or {})}
    if slash in known:
        return "command", end
    if slash in offered:
        return "skill", end
    if not rest and any(item.startswith(slash) for item in known | offered):
        return "partial", end
    return "unknown", end


def make_lexer(commands: Sequence[str], extra: Optional[Callable[[], Mapping[str, str]]] = None):
    """Colours for the input line: the ``/name`` at the start gets the class of ``classify_command`` (a known
    command, a skill, an unknown name, a name still being typed); everything else is plain. ``extra`` returns the
    skills (``{"/name": description}``) and is asked each time, like the completer's."""
    from prompt_toolkit.lexers import Lexer

    class _Lexer(Lexer):
        def lex_document(self, document):
            lines = document.lines
            try:
                skills = dict(extra()) if extra is not None else {}
            except Exception:
                skills = {}
            first = classify_command(lines[0] if lines else "", commands, skills)

            def get_line(number: int):
                text = lines[number] if number < len(lines) else ""
                if number != 0 or first is None:
                    return [("", text)]
                kind, end = first
                start = len(text) - len(text.lstrip())
                parts = [("", text[:start])] if start else []
                parts.append((_STYLE_CLASS[kind], text[start:end]))
                if end < len(text):
                    parts.append(("", text[end:]))
                return parts

            return get_line

    return _Lexer()


def input_style():
    """The input line's colours: a command in cyan, a skill in magenta, an unknown name in red, a name still being
    typed in plain cyan; and a pop-up whose selected row stands out."""
    from prompt_toolkit.styles import Style
    return Style.from_dict({
        "command": "ansicyan bold",
        "skill": "ansimagenta bold",
        "unknown-command": "ansired",
        "partial-command": "ansicyan",
        "completion-menu": "bg:#262626 #d0d0d0",
        "completion-menu.completion": "bg:#262626 #d0d0d0",
        "completion-menu.completion.current": "bg:ansicyan #000000 bold",
        "bottom-toolbar": "noreverse",
        "bottom-toolbar.text": "noreverse",
        "rule": "#5a5a62",
        "status": "#8b8b93",
        "prompt": f"{ACCENT} bold",
        "completion-menu.meta.completion": "bg:#303030 #9a9a9a",
        "completion-menu.meta.completion.current": "bg:ansicyan #000000",
    })


def make_completer(commands: Sequence[str], descriptions: Optional[Mapping[str, str]] = None,
                   extra: Optional[Callable[[], Mapping[str, str]]] = None):
    """Completion: ``/commands`` at the start of a line (offered as you type, each with its one-line
    description), file paths for any other word (only when Tab is pressed, so typing a message stays quiet).

    ``extra`` returns more ``{"/name": description}`` entries (the skills); it is asked each time the list is
    drawn, so one added meanwhile shows at once. A command is never replaced by an extra of the same name, and
    an extra that fails is left out. An extra is completed with a space after it: you type what you want next."""
    from prompt_toolkit.completion import Completer, Completion, PathCompleter
    from prompt_toolkit.document import Document

    notes = dict(descriptions or {})

    class _Completer(Completer):
        def __init__(self) -> None:
            self._paths = PathCompleter(expanduser=True)

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if text.startswith("/") and " " not in text:
                for name in commands:
                    if name.startswith(text):
                        yield Completion(name, start_position=-len(text),
                                         display_meta=plain(notes.get(name, "")) or None)
                try:
                    more = dict(extra()) if extra is not None else {}
                except Exception:
                    more = {}
                for name in more:
                    if name.startswith(text) and name not in commands:
                        yield Completion(name + " ", start_position=-len(text),
                                         display_meta=plain(more.get(name, "")) or None)
                return
            if not complete_event.completion_requested:
                return
            word = document.get_word_before_cursor(WORD=True)
            if word:
                yield from self._paths.get_completions(Document(word), complete_event)

    return _Completer()


def safe_toolbar(toolbar: Callable[[], Any]) -> Callable[[], Any]:
    """The status bar text with control characters defused; an error building it gives an empty bar."""
    def render() -> Any:
        try:
            text = toolbar()
        except Exception:
            return ""
        return plain(text) if isinstance(text, str) else text
    return render
