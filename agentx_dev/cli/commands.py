"""Slash commands: /help, /clear, /model, /mode, /resume, /sessions, /init, /exit.

Nothing here raises out of ``run_command`` (except an interruption, which belongs to the REPL): a bad
argument, a failing model switch or an unreadable file becomes a one-line message. Text that came from
elsewhere (a typed argument, a model label, an error message) is made printable with ``plain`` before it is
shown and is never read as markup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from agentx_dev.cli.approval import MODES
from agentx_dev.cli.models import ModelError
from agentx_dev.cli.ui import plain

_MAX_SHOWN = 160
_SECRET = re.compile(r"(?i)\b(?:sk|pk|key|tok)-[A-Za-z0-9_\-]{8,}|\bBearer\s+\S+")


def _shown(value: Any, limit: int = _MAX_SHOWN) -> str:
    """``value`` as one printable line: no control characters, no secrets, not too long."""
    text = _SECRET.sub("[hidden]", plain(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_slash(line: str) -> Optional[Tuple[str, str]]:
    """``("mode", "plan")`` for ``/mode plan``; None when the line is not a slash command.

    A first word that contains a path separator (``/tmp/build.log fails``) is a message, not a command."""
    text = line.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    name = parts[0] if parts else ""
    if "/" in name or "\\" in name:
        return None
    arg = parts[1].strip() if len(parts) > 1 else ""
    return name.lower(), arg


def _help(ctx: Any, arg: str) -> None:
    ctx.notice("\n".join(f"/{c.name:<9} {c.summary}" for c in COMMANDS))


def _clear(ctx: Any, arg: str) -> None:
    ctx.new_conversation()


def _model(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice(f"Model: {_shown(ctx.model_label)}. Switch with /model <name>, for example /model claude-sonnet-4-6.")
        return
    try:
        ctx.switch_model(arg)
    except ModelError as e:
        ctx.error(_shown(e))
        return
    ctx.notice(f"Model: {_shown(ctx.model_label)}")


def _mode(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice(f"Mode: {ctx.gate.mode}. Choose one of: {', '.join(MODES)}")
        return
    try:
        ctx.gate.set_mode(arg.lower())
    except ValueError as e:
        ctx.error(_shown(e))
        return
    ctx.notice(f"Mode: {ctx.gate.mode}")


def _resume(ctx: Any, arg: str) -> None:
    ctx.resume(arg or None)


def _sessions(ctx: Any, arg: str) -> None:
    ctx.show_sessions()


def _init(ctx: Any, arg: str) -> None:
    ctx.run_init()


def _exit(ctx: Any, arg: str) -> None:
    ctx.request_exit()


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[Any, str], None]


COMMANDS = (
    Command("help", "List the commands", _help),
    Command("clear", "Start a new conversation", _clear),
    Command("model", "Show or switch the model: /model [name]", _model),
    Command("mode", "Show or set the mode: /mode [default|accept-edits|plan]", _mode),
    Command("resume", "Load a saved conversation: /resume [id]", _resume),
    Command("sessions", "List this folder's saved conversations", _sessions),
    Command("init", "Draft an AGENTX.md for this project", _init),
    Command("exit", "Leave", _exit),
)
_BY_NAME = {command.name: command for command in COMMANDS}


def command_names() -> List[str]:
    return [f"/{command.name}" for command in COMMANDS]


def _report(ctx: Any, text: str) -> None:
    """Show an error line; a broken screen must not take the REPL down with it."""
    try:
        ctx.error(text)
    except Exception:
        pass


def run_command(ctx: Any, line: str) -> bool:
    """Run ``line`` if it is a slash command. Returns True when it was one (even an unknown one).

    Failures are shown, not raised. ``KeyboardInterrupt`` and ``SystemExit`` pass through to the caller."""
    parsed = parse_slash(line)
    if parsed is None:
        return False
    name, arg = parsed
    command = _BY_NAME.get(name)
    if command is None:
        _report(ctx, f"Unknown command /{_shown(name, 40)}. Commands: {', '.join(command_names())}")
        return True
    try:
        command.run(ctx, arg)
    except Exception as e:
        _report(ctx, f"/{command.name} failed ({type(e).__name__}): {_shown(e)}")
    return True
