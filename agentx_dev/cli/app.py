"""The agentx chat app: the loop, the turn, the session, and the command-line entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence

from rich.console import Console

from agentx_dev import AgentRunner, AgentType, Session
from agentx_dev.cli.approval import MODES, Gate
from agentx_dev.cli.commands import (AUTO_NOTICE, command_descriptions, command_names, hide_secrets, run_command,
                                     shown)
from agentx_dev.cli.compact import RETRY_AFTER_MESSAGES, compact, needs_compaction
from agentx_dev.cli.context import build_addendum
from agentx_dev.cli.memory import append_entry, entry_text, read_recent, record_from_events, summary_entry_text
from agentx_dev.cli.models import ModelError, choose_model
from agentx_dev.cli.models import model_label as label_of
from agentx_dev.cli.sessions import SessionInfo, find_session, latest_session, list_sessions, new_session_path
from agentx_dev.cli.settings import (ensure_project_settings, ensure_user_settings, is_project_folder, load_settings,
                                     project_settings_path, user_settings_path, write_project_model)
from agentx_dev.cli.team import make_team_tool, run_team_job, team_request
from agentx_dev.cli.tools import build_tools, gate_tool
from agentx_dev.cli.trust import ensure_trusted, is_trusted
from agentx_dev.cli.ui import Progress, PromptInput, Screen, is_terminal, make_asker

INIT_PROMPT = (
    "Look at this project: list the top-level files, read the README and the main config files. Then write a "
    "concise AGENTX.md in the working folder covering what the project is, how to run its tests and the app, "
    "the code layout, and the conventions to follow. Keep it under 60 lines. Use write_file. If an AGENTX.md "
    "already exists, tell me instead of overwriting it."
)

TEAM_ALREADY_RAN = ("Refused: a team job already ran in this turn. Use its result, or wait for the user's "
                    "next message before starting another.")

NOT_A_TERMINAL = "agentx needs an interactive terminal (headless mode is not available yet)."
MAX_RULES_SHOWN = 20
MAX_RULE_CHARS = 200
MAX_ERROR_CHARS = 300


HOME_PROTECTED = (".agentx", ".gitconfig", ".config/git", ".bashrc", ".bash_profile", ".bash_login", ".profile",
                  ".zshrc", ".zprofile", ".config/fish/config.fish")


def _literal(path: str) -> str:
    """``path`` as a glob that matches only itself (a folder name may hold ``[``, ``*`` or ``?``)."""
    return path.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def default_deny(home: Optional[Path] = None) -> List[str]:
    """Deny rules that are always present, in addition to the user's and the project's.

    * ``.git`` and everything under it, at the top of the project and in any sub-folder: git follows a
      ``.git`` FILE (``gitdir:``), ``.git/commondir``, ``.git/config`` (``core.hooksPath``, aliases),
      ``.git/hooks/*`` and ``.git/modules/<sub>/...``, and what runs from there runs later, outside the
      approval prompts. A change under ``.git/`` is also invisible to ``git status`` and ``git diff``, unlike an
      edit to ``conftest.py``, a ``Makefile`` or ``package.json``, so the user would never see it in review.
    * ``.agentx`` and everything under it at the top of the project (the only place the project settings are
      read from), and ``~/.agentx`` (``home``) when the tools can reach it: a model must not rewrite its own
      permissions;
    * in the home folder, for the same reason as ``.git``: ``.gitconfig``, ``.config/git`` and the common shell
      profiles (``HOME_PROTECTED``). Other files outside the project that run later are not protected.

    They refuse ``write_file``, ``edit_file``, ``delete_path`` and ``move_file`` (as source and as
    destination). The patterns are matched against every spelling of the path the gate computes (relative,
    absolute, ``./``, ``..``, backslashes, links followed, case folded on Windows)."""
    places = [".git", ".git/*", "*/.git", "*/.git/*", ".agentx", ".agentx/*"]
    if home is not None:
        try:
            base = Path(home).resolve().as_posix().rstrip("/")
        except (OSError, RuntimeError, ValueError):
            base = Path(home).as_posix().rstrip("/")
        # Files in the home folder that run later (git config, shell profiles) and the user's own settings.
        # An absolute pattern also matches the relative spelling when the project is the home folder.
        for name in HOME_PROTECTED:
            places.append(f"{_literal(base)}/{name}")
            if name in (".agentx", ".config/git"):
                places.append(f"{_literal(base)}/{name}/*")
    rules: List[str] = []
    for place in places:
        for tool in ("write_file", "edit_file", "delete_path"):
            rules.append(f"{tool}:{place}")
        rules.append(f"move_file:{place} -> *")
        rules.append(f"move_file:* -> {place}")
    return rules


DEFAULT_DENY = tuple(default_deny())


def _without_env_keys(text: str, env: Mapping[str, str]) -> str:
    """``text`` with the actual values of the key-like variables in ``env`` replaced by ``[hidden]``."""
    for name, secret in env.items():
        if isinstance(secret, str) and len(secret) >= 8 and any(
                word in str(name).upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(secret, "[hidden]")
    return text


def _redacted(value: Any, env: Mapping[str, str], limit: int = MAX_ERROR_CHARS) -> str:
    """``value`` as one printable line without control characters, secrets (patterns that look like one, and
    the actual key values in ``env``) or excess length."""
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return "(cannot be shown)"
    return shown(_without_env_keys(text, env), limit)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(path))


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
                 project_allow: Iterable[str] = (),
                 select: Optional[Callable[..., Optional[int]]] = None, newly_trusted: bool = False) -> None:
        self.cwd = Path(cwd).resolve()
        self.select = select                  # an arrow-key menu, or None (then answers are typed)
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
        self.settings_created: Optional[Path] = None      # set by main when it wrote a first settings file
        self.add_dirs = [_resolved(Path(d).expanduser()) for d in add_dirs]
        self.gate = Gate(mode=mode, allow=allow, deny=[*default_deny(self.home), *deny], cwd=self.cwd)
        # The mode in force when "a" was answered at an edit prompt (None: accept-edits, if on, was chosen)
        self._mode_before_grant: Optional[str] = None
        self._watch_mode_changes()
        self.progress = Progress(console)
        self._team_helper = ""             # the helper whose step is running; "" outside a team job
        self._team_used = False            # a turn may start one team job
        asker = make_asker(console, self.read_short, self.gate.cwd, select=select,
                           helper=lambda: self._team_helper)

        def ask_with_spinner_paused(request: Any) -> Any:           # a prompt must not fight the spinner
            with self.progress.paused():
                return asker(request)

        self.gate.ask = self._remembering(ask_with_spinner_paused)
        self.base_tools = build_tools(self.cwd, self.add_dirs, self.gate, observer=self._observe_tool)
        self.tools = [*self.base_tools,
                      self._once_per_turn(gate_tool(make_team_tool(self._start_team_job), self.gate))]
        self._memory_on = is_project_folder(self.cwd, self.home)    # no log or folder settings for home / a drive root
        self._newly_trusted = bool(newly_trusted)    # main: the user said yes to the trust question in this run
        self.project_settings_created: Optional[Path] = None
        self._log_warned = False
        self._compact_wait = 0             # no summarising until the history has this many messages
        self.addendum = build_addendum(self.cwd, self.home)
        self.model: Any = None
        self.session: Optional[Session] = None
        self.session_path: Optional[Path] = None
        self._exit = False
        self._exit_code = 0
        self._plain_input = False          # True once the input line failed and the plain prompt took over

    def _remembering(self, ask: Callable[..., Any]) -> Callable[..., Any]:
        """``ask`` that notes when the user answers "a" at an edit prompt: the gate then switches itself to
        accept-edits, and that is a grant for this conversation, not a mode the user chose."""
        def asking(request: Any) -> Any:
            answer = ask(request)
            grants_edits = (getattr(request, "kind", "") == "edit" and getattr(request, "remember", False)
                            and getattr(answer, "choice", "") == "a")
            # Relies on Gate.check calling ask BEFORE _remember switches the mode: here the mode is still the
            # one the user had when they answered, and that is what a new conversation goes back to.
            if grants_edits and self.gate.mode != "accept-edits":
                self._mode_before_grant = self.gate.mode
            return answer
        return asking

    def _watch_mode_changes(self) -> None:
        """A mode the user sets (``/mode``) is theirs: it ends any edit grant, so a new conversation keeps it."""
        set_mode = self.gate.set_mode

        def watched(mode: str) -> None:
            set_mode(mode)
            self._mode_before_grant = None

        self.gate.set_mode = watched

    # -- what is printed -----------------------------------------------------

    def notice(self, text: str) -> None:
        self.screen.notice(text)

    def error(self, text: str) -> None:
        self.screen.error(text)

    def _safe(self, value: Any, limit: int = MAX_ERROR_CHARS) -> str:
        """``value`` as one printable line: no control characters, no secrets (neither a pattern that looks
        like one nor the actual key values in the environment), not too long."""
        return _redacted(value, self.env, limit)

    def _show_startup_warnings(self) -> None:
        for problem in self.problems:
            self.notice("Warning: " + self._safe(problem, 400))
        self._confirm_project_rules()
        self._check_extra_folders()

    def _confirm_project_rules(self) -> None:
        """A repository's own allow rules can switch off the prompts (``"write_file"`` is accept-edits under
        another name, ``"run_shell"`` runs every plain command), so they are listed and the user is asked once.
        Anything but a clear yes drops them for this session; the user's own allow rules and every deny rule,
        the project's included, are kept (a deny rule only restricts)."""
        if not self.project_allow:
            return
        shown = [self._safe(rule, MAX_RULE_CHARS) for rule in self.project_allow[:MAX_RULES_SHOWN]]
        lines = ["This project's settings (.agentx/settings.json) allow these actions without asking you:"]
        lines += [f"  {rule}" for rule in shown]
        if len(self.project_allow) > MAX_RULES_SHOWN:
            lines.append(f"  ... and {len(self.project_allow) - MAX_RULES_SHOWN} more")
        self.notice("\n".join(lines))
        if self._yes("Honour these project rules? [y/N] "):
            return
        for rule in self.project_allow:                     # one occurrence each: the user may have written it too
            for index in range(len(self.gate.allow) - 1, -1, -1):
                if self.gate.allow[index] == rule:
                    del self.gate.allow[index]
                    break
        self.notice("The project's allow rules are ignored for this session; its deny rules still apply.")

    def _yes(self, prompt: str) -> bool:
        """A yes/no question through the short-answer reader. Enter, end of input, Ctrl+C and a reader that
        fails are all no; an answer that is neither is asked once more, and then means no."""
        for attempt in range(2):
            try:
                answer = str(self.read_short(prompt)).strip().lower()
            except (EOFError, KeyboardInterrupt, Exception):
                return False
            if answer in ("y", "yes"):
                return True
            if answer in ("", "n", "no"):
                return False
            if attempt == 0:
                self.notice("Please answer y or n.")
        return False

    def _check_extra_folders(self) -> None:
        for folder in self.add_dirs:
            shown = self._safe(folder, 400)
            if not folder.exists():
                self.notice(f"Warning: the extra folder {shown} does not exist (it was not created).")
            elif folder == _resolved(Path(folder.anchor or "/")):
                self.error(f"Warning: the extra folder {shown} is the filesystem root: the tools can reach "
                           "every file on this drive.")
            elif folder == _resolved(self.home):
                self.error(f"Warning: the extra folder {shown} is your home folder: the tools can reach "
                           "everything in it. Files outside the project that run later are not protected "
                           "beyond a short list (git config, common shell profiles, ~/.agentx).")

    # -- the context the slash commands use ----------------------------------

    @property
    def model_label(self) -> str:
        return label_of(self.model) if self.model is not None else "(no model)"

    def request_exit(self) -> None:
        self._exit = True

    def cycle_mode(self) -> None:
        self.gate.cycle_mode()
        self._mode_before_grant = None            # the user chose this mode

    @property
    def user_settings_file(self) -> Path:
        return user_settings_path(self.home)

    def show_settings(self) -> None:
        """What is in force now and which files to edit to change it (the /settings command)."""
        user, project = self.user_settings_file, project_settings_path(self.cwd)
        state = lambda path: "found" if path.is_file() else "not created"           # noqa: E731
        allow = f"{len(self.gate.allow)} allow, {len(self.gate.deny)} deny rules (deny includes built-in protections)"
        self.notice(
            f"Model: {self.model_label}\n"
            f"Mode: {self.gate.mode}\n"
            f"Rules: {allow}\n"
            f"Extra folders: {', '.join(str(d) for d in self.add_dirs) or 'none'}\n"
            f"Your settings file ({state(user)}): {user}\n"
            f"This project's settings file ({state(project)}): {project}\n"
            "Edit your file to change the default model, mode, extra folders and permission rules, then restart. "
            "Use /model <name> or /mode <name> to change them for this session only. "
            "The project file cannot turn on accept-edits or add folders outside the project.")

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
        """A new conversation is a clean slate for "don't ask again": the session-wide command grants are
        emptied, and accept-edits that came from answering "a" at an edit prompt goes back to the mode that was
        in force when the user answered. A mode the user chose since (``/mode``, Shift+Tab) is kept."""
        self._compact_wait = 0
        self.gate.grants.clear()
        if self._mode_before_grant is not None and self.gate.mode == "accept-edits":
            self.gate.set_mode(self._mode_before_grant)
        self._mode_before_grant = None

    def _fresh(self, runner: AgentRunner) -> None:
        self._new_conversation_state()
        self.session = Session.start(runner)
        self.session_path = new_session_path(self.home, self.cwd, self.session.session_id)

    def _load(self, info: SessionInfo, runner: AgentRunner) -> bool:
        """Load ``info`` into the session. False when the file could not be read: a new conversation was
        started instead, and the caller must not say it resumed anything."""
        try:
            session = Session.load(info.path).attach(runner)
        except Exception as e:
            # one line, and never the file's content (a validation error quotes it)
            self.error(f"Could not read {self._safe(info.path.name, 120)} ({type(e).__name__}). "
                       "Starting a new conversation.")
            self._fresh(runner)
            return False
        self._new_conversation_state()
        self.session, self.session_path = session, info.path
        return True

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
        if self._newly_trusted and self._memory_on:         # never overwrites a file that is already there
            self.project_settings_created = ensure_project_settings(self.cwd, self.home, self.model_label)
        if resume is None:
            self._start_fresh()
        else:
            info = self._find_for_start(resume)
            if info is None:
                if resume not in ("latest", "pick"):
                    self.error(f"No saved session '{self._safe(resume, 60)}' for this folder.")
                    return 1
                if resume == "latest":
                    self.notice("No saved session found; starting a new conversation.")
                self._start_fresh()
            else:
                self._load(info, self._make_runner(self.model))
        extra = f"\nextra folders: {', '.join(str(d) for d in self.add_dirs)}" if self.add_dirs else ""
        self.notice(f"agentx {_version()} - {self.model_label} - {self.cwd}{extra}\n"
                    f"mode: {self.gate.mode}.\n"
                    "Ask about this project, or tell me what to change. I ask before I edit files or run commands.\n"
                    "Type / to see the commands. Enter sends, Alt+Enter adds a line, Tab completes, "
                    "Shift+Tab changes the mode, Ctrl+D leaves.")
        if self.gate.mode == "auto":
            self.notice(AUTO_NOTICE)
        if self.settings_created is not None:
            self.notice(f"Created your settings file: {self.settings_created}\n"
                        "Edit it to change the default model, mode and permission rules. /settings shows what is in force.")
        if self.project_settings_created is not None:
            self.notice(f"Created {self._safe(self.project_settings_created, 400)} with this folder's model. "
                        "/model <name> --folder changes it.")
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
            except Exception as e:
                # The input line itself failed (the history file cannot be written, say): one clean line,
                # then the plain prompt without history; if that fails too, leave.
                self.error(f"Could not read your input ({type(e).__name__}): {self._safe(e)}")
                if self._plain_input:
                    self._exit_code = 1
                    break
                self._plain_input = True
                self.notice("Using a plain prompt without history or completion.")
                self.read_line = lambda prompt="": input(prompt)
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
                if e.code is None:
                    self._exit_code = 0
                elif isinstance(e.code, int):
                    self._exit_code = e.code
                else:                                      # SystemExit("message"): show it, exit 1
                    self.error(self._safe(e.code))
                    self._exit_code = 1
                break
            except Exception as e:
                self.error(self._failure(e))
                continue
            self.send(line)
        self.save()
        return self._exit_code

    def _failure(self, error: BaseException) -> str:
        return f"Something went wrong ({type(error).__name__}): {self._safe(error)}"

    def send(self, text: str, log_title: Optional[str] = None) -> None:
        """One turn: summarise a very long conversation first, stream the events to the screen, then autosave
        and, for a finished turn only, add an entry to the work log (titled ``log_title`` when given, else the
        message). Ctrl+C cancels the turn."""
        assert self.session is not None
        self._team_used = False                        # a new message may start a new team job
        events: List[dict] = []
        finished = False
        try:
            self._maybe_compact()
            self.progress.start("Thinking")
            for event in self.session.stream(text):
                self.progress.stop()                       # erase the spinner, then print
                self.screen.show_event(event)
                kind = event.get("type") if isinstance(event, dict) else None
                if kind in ("tool_call", "tool_result", "final"):
                    events.append(event)
                if kind == "tool_call":
                    self.progress.start(f"Running {self._safe(event.get('name', ''), 40)}")
                elif kind not in ("final", "completion"):
                    self.progress.start("Thinking")
            finished = True
        except KeyboardInterrupt:
            self.notice("Turn interrupted (Ctrl+C). Your earlier turns are kept; this one was not recorded. "
                        "Changes already made in that turn remain on disk.")
        except Exception as e:
            self.error(self._failure(e))
        finally:
            self.progress.stop()
            self.save()
        if finished:
            self._log_turn(text if log_title is None else log_title, events)

    # -- project memory: the work log and summarising ------------------------

    def _hide(self, text: str) -> str:
        """``text`` without key-shaped tokens or the environment's actual key values (the same hiding as error
        messages, see ``_redacted``), but not flattened or cut: newlines are kept."""
        return hide_secrets(_without_env_keys(str(text), self.env))

    def _log_turn(self, title: str, events: List[dict]) -> None:
        if not self._memory_on:
            return
        self._append_log(entry_text(record_from_events(title, events), datetime.now(), self._hide))

    def _append_log(self, text: str) -> None:
        """Add ``text`` to ``.agentx/log.md``. A failure is noticed once per session and never stops the chat."""
        if not self._memory_on or append_entry(self.cwd, text):
            return
        if not self._log_warned:
            self._log_warned = True
            self.notice("Could not write the work log in .agentx/log.md; carrying on without it.")

    def _summary_call(self, prompt: str) -> str:
        """One plain call to the current model (no tools, no agent loop): every model's ``Initialize`` takes the
        message list and returns the reply text."""
        reply = self.model.Initialize(messages=[{"role": "user", "content": prompt}])
        return reply if isinstance(reply, str) else ""

    def _maybe_compact(self) -> None:
        """Summarise a very long conversation before the turn. Nothing is removed unless a summary replaces it;
        after a failure it is not tried again until ``RETRY_AFTER_MESSAGES`` more messages have been added.
        The runner rebuilds its messages from ``session.history`` on every turn, so replacing it is enough."""
        assert self.session is not None
        history = self.session.history
        if len(history) < self._compact_wait or not needs_compaction(history):
            return
        self.progress.start("Summarising the conversation")
        try:
            result = compact(history, self._summary_call)
        finally:
            self.progress.stop()
        if result.status == "done":
            self.session.history = result.history
            self.notice(f"Summarised {result.replaced} earlier messages to keep the conversation short.")
            self._append_log(summary_entry_text(result.summary, datetime.now(), self._hide))
        elif result.status == "failed":
            self._compact_wait = len(history) + RETRY_AFTER_MESSAGES
            self.notice("The conversation is getting long, but it could not be summarised right now; "
                        "it will be tried again later.")

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

    def save_folder_model(self, name: str) -> None:
        """``/model <name> --folder``: keep the model for this folder in its settings file."""
        if not self._memory_on:
            self.notice("No folder settings file is kept for your home folder or a drive root.")
            return
        path, message = write_project_model(self.cwd, name)
        (self.notice if path is not None else self.error)(self._safe(message, 600))

    def _refresh_instructions(self, with_log: bool = False) -> None:
        """Read AGENTX.md again (``/init`` or the user may have written or changed it). ``with_log`` adds the
        newest work-log entries: only for a fresh conversation, never for a resumed one."""
        log_text = read_recent(self.cwd) if (with_log and self._memory_on) else ""
        self.addendum = build_addendum(self.cwd, self.home, log_text=log_text)

    def _start_fresh(self) -> None:
        """A new conversation (startup without --continue/--resume, or /clear): it starts from the work log."""
        self._refresh_instructions(with_log=True)
        self._fresh(self._make_runner(self.model))

    def new_conversation(self) -> None:
        self.save()
        self._start_fresh()
        self.notice(f"Started a new conversation (mode: {self.gate.mode}).")

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
        if self.select is not None:                           # arrow keys; None when there is no console
            labels = [f"{info.updated_at[:19]}  {info.title}  ({info.session_id[:8]})" for info in infos[:9]]
            self.notice("Which conversation?")
            picked = self.select([self._safe(label, 120) for label in labels] + ["Cancel"], 0)
            if picked is not None:
                return infos[picked] if 0 <= picked < len(labels) else None
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
        self._refresh_instructions()
        if not self._load(info, self._make_runner(self.model)):
            return
        self.notice(f"Resumed {self._safe(info.session_id[:8], 20)}: {self._safe(info.title, 80)} "
                    f"(mode: {self.gate.mode})")

    def run_init(self) -> None:
        self.send(INIT_PROMPT, log_title="/init")

    def run_team_command(self, task: str) -> None:
        """``/team <task>``: ask the main agent to call the team tool for ``task`` (same prompt, gate and history)."""
        self.send(team_request(task), log_title=f"/team {task}")

    # -- team jobs ----------------------------------------------------------

    def _once_per_turn(self, gated: Any) -> Any:
        """``gated`` (the team tool behind the gate) with the one-job-per-turn rule checked first, so a second
        call is refused without asking the user again. The first call goes through the gate as before; a job
        the user declined does not count (``_team_used`` is set only when a job starts)."""
        inner = gated.func

        def once(**kwargs):
            if self._team_used:
                return TEAM_ALREADY_RAN
            return inner(**kwargs)

        once.__name__, once.__qualname__, once.__module__ = gated.name, f"once_per_turn.{gated.name}", __name__
        wrapped = type(gated)(func=once, args_schema=gated.args_schema, name=gated.name,
                              description=gated.description)
        wrapped.cacheable = False
        return wrapped

    def _observe_tool(self, name: str, args: dict) -> None:
        """Called by the tool wrappers before the gate check. During a team job it shows the helper's call."""
        if not self._team_helper:
            return
        self.progress.stop()
        self.screen.helper_call(self._team_helper, name, args)
        self.progress.start(f"Running {self._team_helper}")

    def _team_notify(self, kind: str, data: dict) -> None:
        self.progress.stop()
        if kind == "plan":
            self.screen.team_plan(data.get("steps"))
            self.progress.start("Starting the team")
        elif kind == "dispatch":
            self._team_helper = str(data.get("agent", ""))
            self.screen.team_dispatch(data.get("agent"), data.get("query"))
            self.progress.start(f"Running {self._team_helper}")
        elif kind == "result":
            self._team_helper = ""
            self.screen.team_result(data.get("agent"), bool(data.get("ok")), data.get("text"))
            self.progress.start("Team is working")

    def _team_question(self, question: str) -> Optional[str]:
        """The planner or a helper needs a fact: ask in the terminal. Enter, Ctrl+D and Ctrl+C skip it."""
        with self.progress.paused():
            self.screen.team_question(question)
            try:
                answer = self.read_short("Answer (Enter to skip): ")
            except (EOFError, KeyboardInterrupt):
                return None
        return str(answer).strip() or None

    def _start_team_job(self, task: str, why: str) -> str:
        """The ``start_team_job`` tool: run the team and hand the result text back to the main agent."""
        if self._team_used:
            return TEAM_ALREADY_RAN
        if self.model is None:
            return "Refused: no model is available for a team job."
        self._team_used = True
        self.progress.start("Planning the team job")
        try:
            return run_team_job(task, model=self.model, tools=self.base_tools, addendum=self.addendum,
                                notify=self._team_notify, ask_user=self._team_question,
                                describe_error=self._safe)
        finally:
            self._team_helper = ""
            self.progress.start("Thinking")


# -- the command line -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentx", description="A terminal coding assistant built on agentx-dev.")
    parser.add_argument("--version", action="version", version=f"agentx {_version()}")
    parser.add_argument("--model", metavar="NAME", help="model to use, for example claude-sonnet-4-6 or gpt-4o")
    parser.add_argument("--mode", choices=MODES, help="default, accept-edits (edits run without asking), plan "
                                                       "(read-only) or auto (edits, commands and Python run "
                                                       "without asking; delete and move still ask)")
    parser.add_argument("--add-dir", action="append", default=[], metavar="PATH",
                        help="let the agent work in this extra folder (repeatable)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--continue", dest="continue_", action="store_true",
                       help="resume the latest conversation in this folder")
    group.add_argument("--resume", nargs="?", const="pick", default=None, metavar="ID",
                       help="resume a saved conversation (pick from a list, or give an id)")
    return parser


LOG_FILE = "agentx.log"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class _LogFormatter(logging.Formatter):
    """The standard format, with control characters (the framework logs model-chosen text, such as a tool path
    with escape sequences in it) written out as ``\\x1b`` so that ``cat agentx.log`` cannot drive a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        text = _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", super().format(record))
        for name, value in os.environ.items():              # provider errors can echo the key that was sent
            if len(value) >= 8 and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                text = text.replace(value, "[hidden]")
        return text


class _QuietFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating log file whose failures are silent: the default ``handleError`` prints a traceback."""

    def handleError(self, record: logging.LogRecord) -> None:
        pass

    def _open(self):                      # also used when the file is rotated: the log is private to the user
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def setup_logging(home: Path) -> None:
    """Keep the framework's logger away from the terminal. It logs a full traceback (and the model's own text)
    when a tool raises, and with no handler configured Python prints those lines to stderr, in the middle of the
    chat screen. A handler on the root logger swallows everything from every library; warnings and errors from
    ``agentx_dev`` go to ``~/.agentx/agentx.log`` instead (best effort: no folder, no log, no message).
    Calling it again replaces the handlers it added before."""
    root, framework = logging.getLogger(), logging.getLogger("agentx_dev")
    for logger in (root, framework):
        for handler in list(logger.handlers):
            if getattr(handler, "_agentx_cli", False):
                logger.removeHandler(handler)
                handler.close()
    quiet = logging.NullHandler()
    quiet._agentx_cli = True                                   # type: ignore[attr-defined]
    root.addHandler(quiet)
    try:
        folder = Path(home) / ".agentx"
        folder.mkdir(parents=True, exist_ok=True)
        handler = _QuietFileHandler(folder / LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                    encoding="utf-8", delay=True)
    except (OSError, ValueError):
        return
    handler._agentx_cli = True                                 # type: ignore[attr-defined]
    handler.setLevel(logging.WARNING)
    handler.setFormatter(_LogFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    framework.addHandler(handler)


def _startup_failure(error: BaseException) -> int:
    print(f"agentx could not start ({type(error).__name__}): {_redacted(error, os.environ)}", file=sys.stderr)
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not is_terminal():
        print(NOT_A_TERMINAL, file=sys.stderr)
        return 1
    try:
        cwd, home = Path.cwd().resolve(), Path.home()
        setup_logging(home)
        console = Console()
        holder: List[Any] = []
        prompt = PromptInput(history_file=home / ".agentx" / "history", commands=command_names(),
                             descriptions=command_descriptions(),
                             toolbar=lambda: holder[0].toolbar() if holder else "",
                             cycle_mode=lambda: holder[0].cycle_mode() if holder else None)
        # Nothing in this folder (its settings, its AGENTX.md) is read until the user has said they trust it.
        # A folder trusted for the first time in this run gets its own settings file (``ChatApp.start``).
        was_trusted = is_trusted(cwd, home)
        if not ensure_trusted(cwd, home, console, select=prompt.select, read_line=prompt.read_short):
            print("agentx: this folder is not trusted, so nothing was started. Run agentx in a folder you trust.",
                  file=sys.stderr)
            return 1
        settings = load_settings(cwd, home)
        add_dirs = []
        for entry in [*settings.add_dirs, *args.add_dir]:
            path = Path(entry).expanduser()
            add_dirs.append(_resolved(path if path.is_absolute() else cwd / path))
        app = ChatApp(cwd=cwd, home=home, console=console, read_line=prompt.read, read_short=prompt.read_short,
                      select=prompt.select,
                      model_name=args.model or settings.model, mode=args.mode or settings.mode or "default",
                      allow=settings.allow, deny=settings.deny, add_dirs=add_dirs, problems=settings.problems,
                      project_allow=settings.project_allow, newly_trusted=not was_trusted)
        holder.append(app)
        app.settings_created = ensure_user_settings(home, getattr(app, "model_label", None))
    except KeyboardInterrupt:
        print("agentx: interrupted while starting.", file=sys.stderr)
        return 130
    except Exception as e:
        return _startup_failure(e)
    try:
        return app.start("latest" if args.continue_ else args.resume)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except Exception as e:
        print(f"agentx stopped ({type(e).__name__}): {_redacted(e, os.environ)}", file=sys.stderr)
        return 1
