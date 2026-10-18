"""Slash commands: /help, /clear, /model, /mode, /resume, /sessions, /init, /exit.

Nothing here raises out of ``run_command`` (except an interruption, which belongs to the REPL): a bad
argument, a failing model switch or an unreadable file becomes a one-line message. Text that came from
elsewhere (a typed argument, a model label, an error message) is made printable with ``plain`` before it is
shown and is never read as markup."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from agentx_dev.cli.approval import MODES
from agentx_dev.cli.models import ModelError
from agentx_dev.cli.ui import plain

_MAX_SHOWN = 160
_NAME = re.compile(r"/([A-Za-z0-9_-]+)(?:\s+(.*))?", re.DOTALL)
_TAIL = r"[A-Za-z0-9_\-]"
_SECRET = re.compile(
    "|".join((
        r"\b[\w.-]*(?:api[_-]?key|password|secret|token)\s*=\s*\S+",   # name=value
        rf"\b(?:sk|pk)[-_]{_TAIL}{{8,}}",                              # sk-..., sk_live_..., pk_test_...
        rf"\b(?:key|tok)-(?={_TAIL}*\d){_TAIL}{{8,}}",                 # key-/tok- with a digit in it
        rf"\b(?:key|tok)-{_TAIL}{{20,}}",                              # ... or long enough
        r"\bgh[pousr]_[A-Za-z0-9]{8,}",                                # GitHub tokens
        r"\bxox[bap]-[A-Za-z0-9-]{8,}",                                # Slack tokens
        r"\bAKIA[0-9A-Z]{12,}",                                        # AWS access key id
        r"\bAIza[\w-]{20,}",                                           # Google API key
        r"\beyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?",                           # JWT
        r"\b[0-9a-fA-F]{40,}\b",                                       # long hex
        r"\bBearer\s+[\w.~+/=-]{16,}",                                 # bearer token
    )),
    re.IGNORECASE,
)


def hide_secrets(text: str) -> str:
    """``text`` with key-shaped tokens replaced by ``[hidden]``; newlines are kept."""
    return _SECRET.sub("[hidden]", text)


def shown(value: Any, limit: int = _MAX_SHOWN) -> str:
    """``value`` as one printable line: no control characters, no secrets, not too long."""
    text = hide_secrets(plain(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_slash(line: str) -> Optional[Tuple[str, str]]:
    """``("mode", "plan")`` for ``/mode plan``; None when the line is not a slash command.

    A first word that is not a plain name (``/tmp/build.log fails``, ``/README.md``) or a space right after
    the slash (``/ is the root``) makes the line a message, not a command."""
    match = _NAME.fullmatch(line.strip())
    if match is None:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


AUTO_NOTICE = ("Auto mode: file edits, shell commands and Python run without asking. Delete and move still ask, "
               "and .git, .agentx and your git and shell config files stay protected. Shell and Python can do "
               "anything your account can, so use it in folders you trust. Shift+Tab leaves auto mode.")


def _help(ctx: Any, arg: str) -> None:
    ctx.notice("\n".join(f"/{c.name:<9} {c.summary}" for c in COMMANDS))


def _clear(ctx: Any, arg: str) -> None:
    # The app starts the new conversation: it clears the session-wide command grants and undoes an edit grant
    # ("a" at an edit prompt); rules from settings and a mode the user chose explicitly stay.
    ctx.new_conversation()


def _model(ctx: Any, arg: str) -> None:
    words = arg.split()
    folder = "--folder" in words
    name = " ".join(word for word in words if word != "--folder")
    where = getattr(ctx, "user_settings_file", None)
    default_hint = (f' Add --folder to keep it for this folder. "model" in {shown(where)} is the default for folders '
                    "without their own .agentx/settings.json; a folder's file (written when you first trust the "
                    "folder) wins." if where else " Add --folder to keep it for this folder.")
    if not name:
        if folder:
            ctx.error("Usage: /model <name> --folder")
            return
        ctx.notice(f"Model: {shown(ctx.model_label)}. Switch for this session with /model <name>, for example "
                   f"/model gpt-4o-mini or /model claude-sonnet-4-6.{default_hint}")
        return
    try:
        ctx.switch_model(name)
    except ModelError as e:
        ctx.error(shown(e))
        return
    if folder:
        ctx.notice(f"Model: {shown(ctx.model_label)}")
        ctx.save_folder_model(name)
        return
    ctx.notice(f"Model: {shown(ctx.model_label)} (this session only).{default_hint}")


def _settings(ctx: Any, arg: str) -> None:
    ctx.show_settings()


def _mode(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice(f"Mode: {ctx.gate.mode}. Choose one of: {', '.join(MODES)}")
        return
    try:
        ctx.gate.set_mode(arg.lower())
    except ValueError as e:
        ctx.error(shown(e))
        return
    ctx.notice(f"Mode: {ctx.gate.mode}")
    if ctx.gate.mode == "auto":
        ctx.notice(AUTO_NOTICE)


def _resume(ctx: Any, arg: str) -> None:
    ctx.resume(arg or None)


def _sessions(ctx: Any, arg: str) -> None:
    ctx.show_sessions()


def _init(ctx: Any, arg: str) -> None:
    ctx.run_init()


def _team(ctx: Any, arg: str) -> None:
    if not arg:
        ctx.notice("Usage: /team <task>. Hands a big job to a team of helpers (explorer, coder, runner). "
                   "You are asked before it starts. Small jobs are better done in the normal chat.")
        return
    ctx.run_team_command(arg)


TEAM_MODELS_USAGE = ('Usage: /team-models <name>[="what it is for"] [<name> ...] [--folder], '
                     "/team-models describe [--folder], or /team-models clear [--folder]")


def _team_models(ctx: Any, arg: str) -> None:
    try:
        words = shlex.split(arg, posix=True)           # name="hard coding" stays one word, without the quotes
    except ValueError:                                 # an unclosed quote
        ctx.error("A quote is not closed (put a note with an apostrophe in double quotes). " + TEAM_MODELS_USAGE)
        return
    folder = "--folder" in words
    names = [word for word in words if word != "--folder"]
    if not names:
        if folder:
            ctx.error(TEAM_MODELS_USAGE)
            return
        ctx.show_team_models()
        return
    if [name.lower() for name in names] == ["describe"]:
        ctx.describe_team_models(folder=folder)
        return
    if [name.lower() for name in names] == ["clear"]:
        names = []
    ctx.set_team_models(names, folder=folder)


SKILLS_USAGE = ("Usage: /skills | /skills install <github link or folder> | /skills remove <name> | "
                "/skills use <name> [what you want]")


def skill_request(name: str, text: str = "") -> str:
    """The message ``/<skill> [text]`` sends the main agent: read the skill first, then do what was asked."""
    if text:
        return (f'Use the "{name}" skill for this: call the use_skill tool with name "{name}" first, follow what it '
                f"says, then do the following.\n\n{text}")
    return (f'I want to use the "{name}" skill. Call the use_skill tool with name "{name}" and read it, then ask me '
            "what I want to apply it to (if the conversation already makes that clear, go ahead).")


def _skills(ctx: Any, arg: str) -> None:
    word, _, rest = arg.strip().partition(" ")
    rest = rest.strip()
    if not word:
        ctx.show_skills()
    elif word.lower() == "install" and rest:
        ctx.install_skills(rest)
    elif word.lower() == "remove" and rest:
        ctx.remove_skill(rest)
    elif word.lower() == "use" and rest:
        name, _, what = rest.partition(" ")
        if not ctx.run_skill(name.strip().lower(), what.strip()):
            ctx.error(f"No skill named {shown(name, 60)!r} in this conversation. /skills lists them; "
                      "ones added since it began load after /clear.")
    else:
        ctx.error(SKILLS_USAGE)


def _memory(ctx: Any, arg: str) -> None:
    ctx.show_memory(arg)


def _usage(ctx: Any, arg: str) -> None:
    ctx.show_usage()


def _exit(ctx: Any, arg: str) -> None:
    ctx.request_exit()


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[Any, str], None]


COMMANDS = (
    Command("help", "List the commands", _help),
    Command("clear", "Start a new conversation.", _clear),
    Command("model", "Show or switch the model: /model [name] [--folder]", _model),
    Command("mode", "Show or set the mode: /mode [default|accept-edits|plan|auto]", _mode),
    Command("settings", "Show the model, mode and rules in force, and the files that change them", _settings),
    Command("resume", "Load a saved conversation: /resume [id]", _resume),
    Command("sessions", "List this folder's saved conversations", _sessions),
    Command("init", "Draft an AGENTX.md for this project", _init),
    Command("team", "Hand a big job to a team of helpers: /team <task>", _team),
    Command("team-models", "Models team helpers may also use: /team-models [name[=use] ...|clear] [--folder]",
            _team_models),
    Command("skills", "List the skills, or add one: /skills [install <link>|remove <name>]", _skills),
    Command("memory", "Show the project notes kept for later, or search them: /memory [words]", _memory),
    Command("usage","Show the tokens used this session, per model, and by the last turn", _usage),
    Command("exit", "Leave", _exit),
)
_BY_NAME = {command.name: command for command in COMMANDS}
_BY_NAME["skill"] = _BY_NAME["skills"]          # an alias, not listed twice in the pop-up


def command_names() -> List[str]:
    return [f"/{command.name}" for command in COMMANDS]


def command_descriptions() -> dict:
    """``{"/name": one-line summary}`` for the pop-up list shown when the user types ``/``."""
    return {f"/{command.name}": command.summary for command in COMMANDS}


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
        try:
            if getattr(ctx, "run_skill", None) is not None and ctx.run_skill(name, arg):
                return True               # /<skill> [what you want]: the agent reads the skill and does the task
        except Exception as e:
            _report(ctx, f"/{shown(name, 40)} failed ({type(e).__name__}): {shown(e)}")
            return True
        _report(ctx, f"Unknown command /{shown(name, 40)}. Commands: {', '.join(command_names())}")
        return True
    try:
        command.run(ctx, arg)
    except Exception as e:
        _report(ctx, f"/{command.name} failed ({type(e).__name__}): {shown(e)}")
    return True
