"""The agentx chat app: the loop, the turn, the session, and the command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

from rich.console import Console

from agentx_dev import AgentRunner, AgentType, Session
from agentx_dev.cli.approval import MODES, Gate
from agentx_dev.cli.commands import _shown, command_names, run_command
from agentx_dev.cli.context import build_addendum
from agentx_dev.cli.models import ModelError, choose_model
from agentx_dev.cli.models import model_label as label_of
from agentx_dev.cli.sessions import SessionInfo, find_session, latest_session, list_sessions, new_session_path
from agentx_dev.cli.settings import load_settings
from agentx_dev.cli.tools import build_tools
from agentx_dev.cli.ui import PromptInput, Screen, make_asker, plain

INIT_PROMPT = (
    "Look at this project: list the top-level files, read the README and the main config files. Then write a "
    "concise AGENTX.md in the working folder covering what the project is, how to run its tests and the app, "
    "the code layout, and the conventions to follow. Keep it under 60 lines. Use write_file. If an AGENTX.md "
    "already exists, tell me instead of overwriting it."
)

NOT_A_TERMINAL = "agentx needs an interactive terminal (headless mode is not available yet)."
MAX_RULES_SHOWN = 20
MAX_RULE_CHARS = 200
MAX_ERROR_CHARS = 300


def _default_deny() -> List[str]:
    """Deny rules that are always present, in addition to the user's and the project's. A model must not be
    able to rewrite its own permissions (``.agentx/``: the settings files), install a git hook or point git at
    one (``.git/hooks/`` and ``.git/config``): those run later, outside the approval prompts. They cover
    writing, editing, deleting and moving (either end of a move), the folder itself included, at the top of
    the project and in any sub-folder (or any other folder the tools can reach, such as ``~/.agentx``)."""
    protected = (".agentx", ".git/hooks", ".git/config")
    rules: List[str] = []
    for base in protected:
        for prefix in ("", "*/"):
            places = [f"{prefix}{base}"] if base == ".git/config" else [f"{prefix}{base}", f"{prefix}{base}/*"]
            for place in places:
                for tool in ("write_file", "edit_file", "delete_path"):
                    rules.append(f"{tool}:{place}")
                rules.append(f"move_file:{place} -> *")
                rules.append(f"move_file:* -> {place}")
    return rules


DEFAULT_DENY = tuple(_default_deny())


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("agentx-dev")
    except Exception:
        return "unknown"


class ChatApp:
    """One interactive session. All terminal I/O comes in through ``console``, ``read_line`` (the chat prompt)
    and ``read_short`` (approvals and choices), so it runs the same in tests.

    ``cwd`` is resolved once and is the one folder the gate, the tools, the approval previews and the
    instructions all use. ``problems`` and ``project_allow`` (from ``Settings``) are shown as warnings before
    the first prompt."""

    def __init__(self, *, cwd: Path, home: Path, console: Console, read_line: Callable[[str], str],
                 read_short: Optional[Callable[[str], str]] = None, model_name: Optional[str] = None,
                 mode: str = "default", allow: Iterable[str] = (), deny: Iterable[str] = (),
                 add_dirs: Iterable[Path] = (), model_factory: Callable[..., Any] = choose_model,
                 env: Optional[Mapping[str, str]] = None, problems: Iterable[str] = (),
                 project_allow: Iterable[str] = ()) -> None:
        self.cwd = Path(cwd).resolve()
        self.home = Path(home)
        self.console = console
        self.screen = Screen(console)
        self.read_line = read_line
        self.read_short = read_short or read_line
        self.model_factory = model_factory
        self.env = dict(os.environ if env is None else env)
        self.requested_model = model_name
        self.problems = list(problems)
        self.project_allow = list(project_allow)
        self.gate = Gate(mode=mode, allow=allow, deny=[*DEFAULT_DENY, *deny], cwd=self.cwd)
        self.gate.ask = make_asker(console, self.read_short, self.gate.cwd)
        self.tools = build_tools(self.cwd, [Path(d) for d in add_dirs], self.gate)
        self.addendum = build_addendum(self.cwd, self.home)
        self.model: Any = None
        self.session: Optional[Session] = None
        self.session_path: Optional[Path] = None
        self._exit = False
        self._exit_code = 0

    # -- what is printed -----------------------------------------------------

    def notice(self, text: str) -> None:
        self.screen.notice(text)

    def error(self, text: str) -> None:
        self.screen.error(text)

    def _safe(self, value: Any, limit: int = MAX_ERROR_CHARS) -> str:
        """``value`` as one printable line: no control characters, no secrets (neither a pattern that looks
        like one nor the actual key values in the environment), not too long."""
        try:
            text = value if isinstance(value, str) else str(value)
        except Exception:
            return "(cannot be shown)"
        for name, secret in self.env.items():
            if isinstance(secret, str) and len(secret) >= 8 and any(
                    word in str(name).upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                text = text.replace(secret, "[hidden]")
        return _shown(text, limit)

    def _show_startup_warnings(self) -> None:
        for problem in self.problems:
            self.notice("Warning: " + self._safe(problem, 400))
        if self.project_allow:
            shown = [self._safe(rule, MAX_RULE_CHARS) for rule in self.project_allow[:MAX_RULES_SHOWN]]
            lines = ["Warning: this project's settings (.agentx/settings.json) allow these actions without "
                     "asking you:"]
            lines += [f"  {rule}" for rule in shown]
            if len(self.project_allow) > MAX_RULES_SHOWN:
                lines.append(f"  ... and {len(self.project_allow) - MAX_RULES_SHOWN} more")
            lines.append("If you did not expect this, check that file before you go on (or leave with /exit).")
            self.notice("\n".join(lines))

    # -- the context the slash commands use ----------------------------------

    @property
    def model_label(self) -> str:
        return label_of(self.model) if self.model is not None else "(no model)"

    def request_exit(self) -> None:
        self._exit = True

    def cycle_mode(self) -> None:
        self.gate.cycle_mode()

    def toolbar(self) -> str:
        session_id = self.session.session_id[:8] if self.session else "-"
        return f" {self.model_label} | mode: {self.gate.mode} | {self.cwd} | {session_id} | Shift+Tab: mode "

    # -- starting up ---------------------------------------------------------

    def _make_runner(self, model: Any) -> AgentRunner:
        # auto_cache=False: a cached result must never stand in for a call that needs approval or has side
        # effects. No timeout is set anywhere: an approval prompt waits for the user as long as it takes.
        # One tool call at a time: one approval prompt open, on the main thread, and edits made in order.
        return AgentRunner(model=model, agent=AgentType.ReAct, tools=self.tools, system_addendum=self.addendum,
                           max_iterations=50, auto_cache=False, verbose=False, parallel_tool_workers=1)

    def _new_conversation_state(self) -> None:
        """A new conversation is a clean slate for "don't ask again" (session-wide command grants)."""
        self.gate.grants.clear()

    def _fresh(self, runner: AgentRunner) -> None:
        self._new_conversation_state()
        self.session = Session.start(runner)
        self.session_path = new_session_path(self.home, self.cwd, self.session.session_id)

    def _load(self, info: SessionInfo, runner: AgentRunner) -> None:
        try:
            session = Session.load(info.path).attach(runner)
        except Exception as e:
            # one line, and never the file's content (a validation error quotes it)
            self.error(f"Could not read {self._safe(info.path.name, 120)} ({type(e).__name__}). "
                       "Starting a new conversation.")
            self._fresh(runner)
            return
        self._new_conversation_state()
        self.session, self.session_path = session, info.path

    def start(self, resume: Optional[str] = None) -> int:
        """Build the model and the session, then run the chat loop. Returns the exit code."""
        self._show_startup_warnings()
        try:
            self.model = self.model_factory(self.requested_model, self.env)
        except ModelError as e:
            self.error(self._safe(e))
            return 1
        except Exception as e:
            self.error(f"Could not set up the model ({type(e).__name__}).")
            return 1
        runner = self._make_runner(self.model)
        if resume is None:
            self._fresh(runner)
        else:
            info = self._find_for_start(resume)
            if info is None:
                if resume not in ("latest", "pick"):
                    self.error(f"No saved session '{self._safe(resume, 60)}' for this folder.")
                    return 1
                if resume == "latest":
                    self.notice("No saved session found; starting a new conversation.")
                self._fresh(runner)
            else:
                self._load(info, runner)
        self.notice(f"agentx {_version()} - {self.model_label} - {self.cwd}\n"
                    f"mode: {self.gate.mode}. Type /help for commands, /exit or Ctrl+D to leave.")
        return self.loop()

    def _find_for_start(self, resume: str) -> Optional[SessionInfo]:
        if resume == "latest":
            return latest_session(self.home, self.cwd)
        if resume == "pick":
            return self._pick()
        return find_session(self.home, self.cwd, resume)

    # -- the loop and one turn -----------------------------------------------

    def loop(self) -> int:
        while not self._exit:
            try:
                line = self.read_line("> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                self.notice("(Ctrl+C) Type /exit or press Ctrl+D to leave.")
                continue
            line = str(line).strip()
            if not line:
                continue
            try:
                if run_command(self, line):
                    continue          # a command, or an unknown one: never a message for the model
            except KeyboardInterrupt:
                self.notice("Command interrupted (Ctrl+C).")
                continue
            except SystemExit as e:
                self._exit_code = e.code if isinstance(e.code, int) else 0
                break
            except Exception as e:
                self.error(self._failure(e))
                continue
            self.send(line)
        self.save()
        return self._exit_code

    def _failure(self, error: BaseException) -> str:
        return f"Something went wrong ({type(error).__name__}): {self._safe(error)}"

    def send(self, text: str) -> None:
        """One turn: stream the events to the screen, then autosave. Ctrl+C cancels the turn."""
        assert self.session is not None
        try:
            for event in self.session.stream(text):
                self.screen.show_event(event)
        except KeyboardInterrupt:
            self.notice("Turn interrupted (Ctrl+C). Your earlier turns are kept; this one was not recorded.")
        except Exception as e:
            self.error(self._failure(e))
        finally:
            self.save()

    def save(self) -> None:
        if self.session is None or self.session_path is None or not self.session.history:
            return                                # nothing to keep: no empty session files
        try:
            self.session.save(self.session_path)
        except OSError as e:
            self.error(f"Could not save the session: {self._safe(e)}")

    # -- commands that need the app ------------------------------------------

    def switch_model(self, name: str) -> None:
        """Switch the model, keeping the conversation. A model that cannot be built raises (``ModelError``)
        before anything changes, so the old model stays."""
        model = self.model_factory(name, self.env)
        self.model = model
        self.requested_model = name
        assert self.session is not None
        self.session.attach(self._make_runner(model))

    def new_conversation(self) -> None:
        self.save()
        self._fresh(self._make_runner(self.model))
        self.notice("Started a new conversation.")

    def _lines_for(self, infos: Sequence[SessionInfo]) -> List[str]:
        return [f"{n}. {info.updated_at[:19]}  {info.title}  ({info.session_id[:8]})"
                for n, info in enumerate(infos, 1)]

    def show_sessions(self) -> None:
        infos = list_sessions(self.home, self.cwd)
        self.notice("\n".join(self._lines_for(infos)) if infos else "No saved sessions for this folder.")

    def _pick(self) -> Optional[SessionInfo]:
        infos = list_sessions(self.home, self.cwd)
        if not infos:
            self.notice("No saved sessions for this folder.")
            return None
        self.notice("\n".join(self._lines_for(infos)))
        try:
            answer = self.read_short("Resume which? (number, blank to cancel) ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(infos):
            return infos[int(answer) - 1]
        return None

    def resume(self, ident: Optional[str]) -> None:
        info = self._pick() if ident is None else find_session(self.home, self.cwd, ident)
        if info is None:
            if ident is not None:
                self.error(f"No saved session '{self._safe(ident, 60)}' for this folder.")
            return
        self.save()
        self._load(info, self._make_runner(self.model))
        self.notice(f"Resumed {self._safe(info.session_id[:8], 20)}: {self._safe(info.title, 80)}")

    def run_init(self) -> None:
        self.send(INIT_PROMPT)


# -- the command line -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentx", description="A terminal coding assistant built on agentx-dev.")
    parser.add_argument("--version", action="version", version=f"agentx {_version()}")
    parser.add_argument("--model", metavar="NAME", help="model to use, for example claude-sonnet-4-6 or gpt-4o")
    parser.add_argument("--mode", choices=MODES, help="default, accept-edits (edits run without asking) or plan "
                                                       "(read-only)")
    parser.add_argument("--add-dir", action="append", default=[], metavar="PATH",
                        help="let the agent work in this extra folder (repeatable)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--continue", dest="continue_", action="store_true",
                       help="resume the latest conversation in this folder")
    group.add_argument("--resume", nargs="?", const="pick", default=None, metavar="ID",
                       help="resume a saved conversation (pick from a list, or give an id)")
    return parser


def _is_terminal() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:
        return False


def _startup_failure(error: BaseException) -> int:
    print(f"agentx could not start ({type(error).__name__}): {plain(error)}", file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not _is_terminal():
        print(NOT_A_TERMINAL, file=sys.stderr)
        return 1
    try:
        cwd, home = Path.cwd().resolve(), Path.home()
        settings = load_settings(cwd, home)
        add_dirs = []
        for entry in [*settings.add_dirs, *args.add_dir]:
            path = Path(entry).expanduser()
            add_dirs.append(path if path.is_absolute() else (cwd / path).resolve())
        console = Console()
        holder: List[Any] = []
        prompt = PromptInput(history_file=home / ".agentx" / "history", commands=command_names(),
                             toolbar=lambda: holder[0].toolbar() if holder else "",
                             cycle_mode=lambda: holder[0].cycle_mode() if holder else None)
        app = ChatApp(cwd=cwd, home=home, console=console, read_line=prompt.read, read_short=prompt.read_short,
                      model_name=args.model or settings.model, mode=args.mode or settings.mode or "default",
                      allow=settings.allow, deny=settings.deny, add_dirs=add_dirs, problems=settings.problems,
                      project_allow=settings.project_allow)
        holder.append(app)
    except Exception as e:
        return _startup_failure(e)
    try:
        return app.start("latest" if args.continue_ else args.resume)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
