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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from rich.console import Console

from agentx_dev import AgentRunner, AgentType, Session
from agentx_dev.cli.approval import MODES, Gate
from agentx_dev.cli.commands import (AUTO_NOTICE, command_descriptions, command_names, hide_secrets, run_command,
                                     shown, skill_request)
from agentx_dev.cli.compact import RETRY_AFTER_MESSAGES, compact, needs_compaction
from agentx_dev.cli.context import build_addendum
from agentx_dev.cli.memory import append_entry, entry_text, read_recent, record_from_events, summary_entry_text
from agentx_dev.cli.models import (ModelError, build_team_models, choose_model, describe_prompt, is_auto_note,
                                   parse_descriptions, parse_team_models, team_models_line, usable_team_models)
from agentx_dev.cli.models import model_label as label_of
from agentx_dev.cli.safepaths import safe_location
from agentx_dev.cli.sessions import SessionInfo, find_session, latest_session, list_sessions, new_session_path
from agentx_dev.cli.settings import (PLACEHOLDER_MODEL, ensure_project_settings, ensure_user_settings,
                                     is_project_folder, load_settings, project_settings_path, user_settings_path,
                                     write_project_model, write_project_team_models)
from agentx_dev.cli.team import MAX_TEAM_JOBS_PER_MESSAGE, make_team_tool, run_team_job, team_request
from agentx_dev.cli.tools import build_tools, gate_tool
from agentx_dev.cli.trust import ensure_trusted, is_trusted
from agentx_dev.cli.ui import Progress, PromptInput, Screen, is_terminal, make_asker
from agentx_dev.Skills import (SkillInstallError, Skills, discover, install, open_source, preview_text,
                               remove_skill)

INIT_PROMPT = (
    "Look at this project: list the top-level files, read the README and the main config files. Then write a "
    "concise AGENTX.md in the working folder covering what the project is, how to run its tests and the app, "
    "the code layout, and the conventions to follow. Keep it under 60 lines. Use write_file. If an AGENTX.md "
    "already exists, tell me instead of overwriting it."
)

TEAM_LIMIT_REACHED = (f"Refused: {MAX_TEAM_JOBS_PER_MESSAGE} team jobs already ran for this message. Report to the "
                      "user what is done and what remains, and let them say whether to continue.")
TEAM_STALLED = ("Refused: no step of the last team job finished, so another job would likely fail the same way. "
                "Report to the user what happened and what remains, and let them decide.")

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
                 select: Optional[Callable[..., Optional[int]]] = None, newly_trusted: bool = False,
                 model_source: Optional[Path] = None, team_models: Optional[Sequence[str]] = None,
                 team_models_source: Optional[Path] = None,
                 team_model_uses: Optional[Mapping[str, str]] = None) -> None:
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
        self.model_source = model_source       # the settings file ``model_name`` came from (None: a flag, or none)
        self.team_models: List[str] = list(team_models or [])
        self.team_model_uses: Dict[str, str] = dict(team_model_uses or {})    # what a listed model is for
        # What the chat model said each model (lower case) is good for this session: an ``auto:`` note, or ""
        # when it did not know or could not be asked. A model in here is not asked about again unless forced.
        self._auto_notes: Dict[str, str] = {}
        # The file the list came from. An empty list keeps its file: that file turned the extra models off.
        self.team_models_source = team_models_source
        self.problems = list(problems)
        self.project_allow = list(project_allow)
        self.settings_created: Optional[Path] = None      # set by start when it wrote a first settings file
        self.add_dirs = [_resolved(Path(d).expanduser()) for d in add_dirs]
        self.gate = Gate(mode=mode, allow=allow, deny=[*default_deny(self.home), *deny], cwd=self.cwd)
        # The mode in force when "a" was answered at an edit prompt (None: accept-edits, if on, was chosen)
        self._mode_before_grant: Optional[str] = None
        self._watch_mode_changes()
        self.progress = Progress(console)
        self._team_helper = ""             # the helper whose step is running; "" outside a team job
        self._team_jobs = 0                # team jobs started for the current message
        self._team_stalled = False         # the last job in this message finished no step
        asker = make_asker(console, self.read_short, self.gate.cwd, select=select,
                           helper=lambda: self._team_helper, helper_models=self.helper_models_line)

        def ask_with_spinner_paused(request: Any) -> Any:           # a prompt must not fight the spinner
            with self.progress.paused():
                return asker(request)

        self.gate.ask = self._remembering(ask_with_spinner_paused)
        self.base_tools = build_tools(self.cwd, self.add_dirs, self.gate, observer=self._observe_tool)
        self.tools = [*self.base_tools,
                      self._within_job_limit(gate_tool(make_team_tool(self._start_team_job), self.gate))]
        self._memory_on = is_project_folder(self.cwd, self.home)    # no log or folder settings for home / a drive root
        # The agent's skills: the user's own folder, and (read-only, untrusted) this project's, when it is a real
        # project and neither .agentx nor .agentx/skills is a link that leads out of it.
        project_skills = ([self.cwd / ".agentx" / "skills"]
                          if self._memory_on and safe_location(self.cwd, "skills") else [])
        # observer: a team helper opening a skill is drawn like its other tool calls (the chat's own are drawn from
        # the runner's events; ``_observe_tool`` ignores a call made outside a team job).
        self.skills = Skills.default(self.home, project_dirs=project_skills, observer=self._observe_tool)
        self._runner_skills: Dict[str, str] = {}      # name -> description, for the current runner (_make_runner)
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
        self._announce_project_team_models()
        self._check_extra_folders()

    def _announce_project_team_models(self) -> None:
        """A repository's own ``teamModels`` can send a team job's work to another provider or a dearer model, and
        in auto mode (or with an allow rule for the team tool) nothing else would say so: one notice, no prompt.
        A list from the user's own settings, or one set in this session, is not announced."""
        source = self.team_models_source
        if not self.team_models or source is None:
            return
        try:
            from_project = Path(source).resolve() == project_settings_path(self.cwd).resolve()
        except OSError:
            from_project = False
        if not from_project:
            return
        self.notice(f"This project's settings let team helpers also use: "
                    f"{self._safe(self._listed_with_uses(), 800)} (from {self._safe(source, 300)}). "
                    "/team-models clear turns that off for this session.")

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
        return label_of(self.model) if self.model is not None else PLACEHOLDER_MODEL

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
            f"Helper models: {self._safe(self._listed_with_uses(), 800) or 'none (helpers use the chat model)'}\n"
            f"Your settings file ({state(user)}): {user}\n"
            f"This project's settings file ({state(project)}): {project}\n"
            "Edit your file to change the default model, mode, extra folders and permission rules, then restart; "
            "a model in this project's file wins over yours. "
            "Use /model <name> or /mode <name> to change them for this session only, "
            "or /model <name> --folder to also save the model in this project's file. "
            "/team-models sets the models team helpers may also use. "
            "The project file cannot turn on accept-edits or add folders outside the project.")

    def toolbar(self) -> str:
        session_id = self.session.session_id[:8] if self.session else "-"
        return f" {self.model_label} | mode: {self.gate.mode} | {self.cwd} | {session_id} | Shift+Tab: mode "

    # -- starting up ---------------------------------------------------------

    def _make_runner(self, model: Any) -> AgentRunner:
        # auto_cache=False: a cached result must never stand in for a call that needs approval or has side
        # effects. No timeout is set anywhere: an approval prompt waits for the user as long as it takes.
        # One tool call at a time: one approval prompt open, on the main thread, and edits made in order.
        runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=self.tools, system_addendum=self.addendum,
                             max_iterations=50, auto_cache=False, verbose=False, parallel_tool_workers=1,
                             skills=self.skills)
        # The skills THIS conversation's agent can open (what /<skill> and the `/` pop-up offer): taken from the
        # runner itself, so it is exactly what use_skill can read, and it follows /clear, /resume and a new model.
        tool = next((t for t in runner.tools if t.name == "use_skill"), None)
        self._runner_skills = dict(getattr(tool, "skill_descriptions", None) or {})
        return runner

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
            self.error(self._safe(e) + self._model_source_note())
            self._create_user_settings(None)
            self._show_settings_created()
            return 1
        except Exception as e:
            self.error(f"Could not set up the model ({type(e).__name__}).{self._model_source_note()}")
            self._create_user_settings(None)
            self._show_settings_created()
            return 1
        # Only now is the model's real name known: the first settings file is written with it (never overwritten).
        self._create_user_settings(self.model_label)
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
        self._show_settings_created()
        if self.project_settings_created is not None:
            self.notice(f"Created {self._safe(self.project_settings_created, 400)} with this folder's model. "
                        "/model <name> --folder changes it.")
        return self.loop()

    def _create_user_settings(self, model: Optional[str]) -> None:
        """Write ``~/.agentx/settings.json`` if there is none yet (``model`` only when it is a real name)."""
        created = ensure_user_settings(self.home, model)
        if created is not None:
            self.settings_created = created

    def _show_settings_created(self) -> None:
        if self.settings_created is not None:
            self.notice(f"Created your settings file: {self._safe(self.settings_created, 400)}\n"
                        "Edit it to change the default model, mode and permission rules. /settings shows what is in force.")

    def _model_source_note(self) -> str:
        """For a model that cannot be built: which settings file named it, so the user knows what to edit."""
        if self.model_source is None:
            return ""
        name, source = self._safe(self.requested_model, 60), self._safe(self.model_source, 300)
        return f" The model name {name!r} comes from {source}."

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
        self._team_jobs = 0                            # a new message may start new team jobs
        self._team_stalled = False
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
            try:
                self._log_turn(text if log_title is None else log_title, events)
            except Exception:                  # the turn itself went well: a log failure must not end the app
                self._warn_log_failed()

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
        if not self._memory_on:
            return
        try:
            written = append_entry(self.cwd, text)
        except Exception:                      # append_entry returns False for the expected failures
            written = False
        if not written:
            self._warn_log_failed()

    def _warn_log_failed(self) -> None:
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

    def helper_models_line(self) -> str:
        """The approval panel's ``Helper models:`` line; "" when no extra models are set or there is no model."""
        if not self.team_models or self.model is None:
            return ""
        return team_models_line(self.model_label, self.team_models, self.env)

    def show_team_models(self) -> None:
        """``/team-models``: the list, where it came from, and which entries a job would use now."""
        if not self.team_models and self.team_models_source is not None:
            self.notice(f"Helper models: none - turned off by {self._safe(self.team_models_source, 300)}, so team "
                        f"helpers use the chat's model ({self._safe(self.model_label, 80)}). Set some with "
                        "/team-models <name> [<name> ...]; add --folder to keep them for this folder.")
            return
        if not self.team_models:
            self.notice("Helper models: none set, so team helpers use the chat's model "
                        f"({self._safe(self.model_label, 80)}). Set some with /team-models <name> [<name> ...]; "
                        "add --folder to keep them for this folder.")
            return
        usable, notes = usable_team_models(self.model_label, self.team_models, self.env)
        source = (f" (from {self._safe(self.team_models_source, 300)})" if self.team_models_source
                  else " (this session)")
        lines = [f"Helper models{source}: {self._safe(self._listed_with_uses(), 800)}",
                 f"A team job may use: {self._safe(self.model_label, 110)} (chat"
                 + (f"; {self._safe(self._use_of(self.model_label), 90)})" if self._use_of(self.model_label) else ")")
                 + "".join(f", {self._safe(name, 110)}" for name in usable)]
        lines += [self._safe(note.replace('Not used:', 'Not used now:', 1), 200) for note in notes]
        lines.append('Change them with /team-models <name>[="what it is for"] ..., /team-models clear, or add --folder '
                     "to save.")
        self.notice("\n".join(lines))

    def _use_of(self, name: str) -> str:
        """The user's note on what model ``name`` is for ("" when none); names match in any case."""
        wanted = str(name).strip().lower()
        return next((use for listed, use in self.team_model_uses.items() if listed.lower() == wanted), "")

    def _with_use(self, name: str) -> str:
        use = self._use_of(name)
        return f"{name} ({use})" if use else name

    def _listed_with_uses(self) -> str:
        """The list as shown: ``gpt-6.1-sol (hard coding), gpt-5.5``."""
        return ", ".join(self._with_use(name) for name in self.team_models)

    def set_team_models(self, names: Sequence[str], folder: bool = False) -> None:
        """``/team-models <name>[=<what it is for>] ...`` (session) or ``... --folder`` (also saved in this
        folder's settings file)."""
        cleaned, uses, problems = parse_team_models(list(names))
        for problem in problems:
            self.error(self._safe(problem, 300))
        if names and not cleaned:
            self.error("No usable model name was given, so the list was left unchanged.")
            return
        self.team_models = cleaned
        self.team_model_uses = uses
        self.team_models_source = None
        if cleaned:
            self._apply_known_notes()             # what the chat model already said this session is kept
            self._describe_models()
        report = self._save_team_models_in_folder() if folder else None
        self.show_team_models()               # after the write, so it names the file as the source
        if report is not None:
            report[0](report[1])

    def describe_team_models(self, folder: bool = False) -> None:
        """``/team-models describe``: ask the chat model again what each model is good for, replacing only its own
        (``auto:``) notes, then show the list; with ``folder`` the list and its notes are saved in this folder's
        settings file. The list itself and where it came from are not changed."""
        if not self.team_models:
            self.notice("No helper models are set, so there is nothing to describe. Set some with "
                        "/team-models <name> ...")
            return
        self._describe_models(force=True)
        report = self._save_team_models_in_folder() if folder else None
        self.show_team_models()
        if report is not None:
            report[0](report[1])

    def _save_team_models_in_folder(self) -> Tuple[Callable[[str], None], str]:
        """Write the list and its notes to this folder's settings file; what to print about it."""
        if not self._memory_on:
            return self.notice, "No folder settings file is kept for your home folder or a drive root."
        path, message = write_project_team_models(self.cwd, self.team_models, self.team_model_uses)
        if path is not None:
            self.team_models_source = path
        return (self.notice if path is not None else self.error), self._safe(message, 600)

    def _apply_known_notes(self) -> None:
        """Give each model on the list (and the chat's own) the note the chat model already wrote for it this
        session, unless it has a note."""
        for name in [self.model_label, *self.team_models]:
            known = self._auto_notes.get(name.lower(), "")
            if known and not self._use_of(name):
                self.team_model_uses[name] = known

    def _describe_models(self, force: bool = False) -> None:
        """Ask the chat model, in one call, what each model a team may use (the list and the chat's own) is good
        for, and keep the answers as ``auto:`` notes. A note the user wrote is never asked about or replaced.
        Each model is asked about once per session (its answer, or the lack of one, is remembered and reused
        when the list is set again); ``force`` asks again about every model without a note of the user's. A
        model the chat model does not know gets no note. A failure or an unreadable reply gives one notice;
        Ctrl+C skips it. The reply is model output: only short, printable notes for the names asked about are
        kept."""
        if self.model is None:
            return
        wanted: List[str] = []
        for name in [self.model_label, *self.team_models]:
            note = self._use_of(name)
            if name.lower() in (w.lower() for w in wanted) or (note and not is_auto_note(note)):
                continue
            if force or (not note and name.lower() not in self._auto_notes):
                wanted.append(name)
        if not wanted:
            self._apply_known_notes()
            return
        for name in wanted:                      # asked once, whatever happens below
            self._auto_notes.setdefault(name.lower(), "")
        self.progress.start("Asking the model about the helper models")
        try:
            reply = self._summary_call(describe_prompt(wanted))
        except KeyboardInterrupt:
            self.progress.stop()
            self.notice("Skipped describing the helper models.")
            return
        except Exception as error:
            self.progress.stop()
            self.notice(f"Could not describe the models ({type(error).__name__}); add notes yourself with "
                        '/team-models <name>="what it is for".')
            return
        self.progress.stop()
        found = parse_descriptions(reply, wanted)
        if found is None:
            self.notice("The model's answer about the helper models could not be read; add notes yourself with "
                        '/team-models <name>="what it is for", or try /team-models describe.')
            return
        for name in wanted:
            self._auto_notes[name.lower()] = found.get(name, "")
            if name not in found:                 # now unknown: an older auto note must not linger
                for key in [key for key, use in self.team_model_uses.items()
                            if key.lower() == name.lower() and is_auto_note(use)]:
                    del self.team_model_uses[key]
        for name, note in found.items():
            for key in [key for key in self.team_model_uses if key.lower() == name.lower()]:
                del self.team_model_uses[key]
            self.team_model_uses[name] = note
        self._apply_known_notes()
        unknown = [name for name in wanted if name not in found]
        if unknown:
            self.notice("No note for " + self._safe(", ".join(unknown), 400) + ": the chat model does not know "
                        'what they are good at. Add one with /team-models <name>="what it is for" if you know.')

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

    def _within_job_limit(self, gated: Any) -> Any:
        """``gated`` (the team tool behind the gate) with the jobs-per-message limit checked first, so a call past
        ``MAX_TEAM_JOBS_PER_MESSAGE`` is refused without asking the user again. Other calls go through the gate
        as before; a job the user declined does not count (``_team_jobs`` grows only when a job starts)."""
        inner = gated.func

        def once(**kwargs):
            refusal = self._team_refusal()
            return refusal if refusal else inner(**kwargs)

        once.__name__, once.__qualname__, once.__module__ = gated.name, f"within_job_limit.{gated.name}", __name__
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
            self.screen.team_result(data.get("agent"), bool(data.get("ok")), data.get("text"),
                                    partial=bool(data.get("partial")))
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

    # -- skills ---------------------------------------------------------------

    def show_skills(self) -> None:
        """``/skills``: every skill found on disk (the ones this conversation's agent has first-class, shown as the
        ``/name`` that runs them), and how to use, add or remove one."""
        store = self.skills.store()
        taken = set(command_names())

        def label(skill: Any) -> str:
            if skill.name not in self._runner_skills:
                return skill.name
            return f"/{skill.name}" if f"/{skill.name}" not in taken else f"/skills use {skill.name}"

        lines = [f"  {label(s)} ({s.source}) - {self._safe(s.description, 100)}"
                 + ("" if s.name in self._runner_skills else "  [not active in this conversation]")
                 for s in store.all()]
        lines += [f"  Skipped: {self._safe(problem, 160)}" for problem in store.problems[:5]]
        shown = "\n".join(lines) if lines else "  none yet; the built-in ones are created when a conversation starts"
        self.notice(f"Skills (the agent reads one when its work matches):\n{shown}\n"
                    "Use one with /<skill> [what you want] (type / to pick from the list). "
                    f"They live in {self._safe(self.skills.dir, 300)}. /skills install <github link or folder> adds "
                    "more; /skills remove <name> deletes one. A new conversation (/clear) picks up changes, and "
                    "skills for tools this agent does not have stay inactive.")

    def skill_entries(self) -> Dict[str, str]:
        """``{"/name": "(skill) description"}`` for the `/` pop-up: the skills this conversation's agent has, except
        ones named like a built-in command (those are reached with ``/skills use <name>``)."""
        taken = set(command_names())
        return {f"/{name}": f"(skill) {self._safe(description, 120)}"
                for name, description in sorted(self._runner_skills.items()) if f"/{name}" not in taken}

    def run_skill(self, name: str, text: str = "") -> bool:
        """``/<skill> [what you want]`` and ``/skills use <name> [what you want]``: ask the agent to read the skill
        and do the task. False (nothing sent) when this conversation's agent has no skill of that name."""
        if name not in self._runner_skills:
            return False
        self.send(skill_request(name, text), log_title=f"/{name} {text}".strip())
        return True

    def install_skills(self, link: str) -> None:
        """``/skills install <link>``. Only the user can start this (no tool does), and nothing is saved until
        they have seen what was found and said yes."""
        self.notice("Downloading...")
        try:
            with open_source(link) as fetched:
                found, problems = discover(fetched.root, fetched.subpath)
                if not found:
                    skipped = "".join(f"\n  {self._safe(p, 160)}" for p in problems[:5])
                    self.error("No skills found there (a skill is a folder with a SKILL.md file)." + skipped)
                    return
                self.notice(preview_text(fetched, found, problems))
                if not self._yes(f"Install {len(found)} skill{'s' if len(found) != 1 else ''} into "
                                 f"{self._safe(self.skills.dir, 200)}? [y/N] "):
                    self.notice("Nothing was installed.")
                    return
                names = install(found, self.skills.dir, fetched)
        except SkillInstallError as e:
            self.error(self._safe(e, 400))
            return
        self.notice("Installed: " + ", ".join(names) + ". Start a new conversation (/clear) to use "
                    + ("them." if len(names) != 1 else "it."))

    def remove_skill(self, name: str) -> None:
        """``/skills remove <name>``: deletes a skill folder from the user's skills folder after a yes."""
        skill = self.skills.store().skills.get(name.strip())
        if skill is None or skill.folder is None:
            self.error(f"No skill named {self._safe(name, 60)!r}. /skills lists them.")
            return
        if skill.source == "project":
            self.error("That skill belongs to this project's folder. Delete .agentx/skills/<name> yourself if you "
                       "want it gone.")
            return
        if not self._yes(f"Remove the skill {skill.name} from {self._safe(self.skills.dir, 200)}? [y/N] "):
            self.notice("Nothing was removed.")
            return
        try:
            remove_skill(skill.folder.name, self.skills.dir)
        except SkillInstallError as e:
            self.error(self._safe(e, 400))
            return
        self.notice(f"Removed {skill.name}.")

    def _start_team_job(self, task: str, why: str) -> str:
        """The ``start_team_job`` tool: run the team and hand the result text back to the main agent."""
        refusal = self._team_refusal()
        if refusal:
            return refusal
        if self.model is None:
            return "Refused: no model is available for a team job."
        self._team_jobs += 1
        if self.team_models:
            self._describe_models()               # a list loaded from settings gets its notes at the first job
        extras, notes = build_team_models(self.team_models, self.env, chat_label=self.model_label,
                                          factory=self.model_factory,
                                          uses={name: self._use_of(name) for name in self.team_models})
        self.progress.stop()                      # the notices below must not be drawn over the spinner
        for note in notes:
            self.notice(self._safe(note, 300))
        self.progress.start("Planning the team job")
        try:
            text = run_team_job(task, model=self.model, tools=self.base_tools, addendum=self.addendum,
                                notify=self._team_notify, ask_user=self._team_question,
                                describe_error=self._safe, extra_models=extras,
                                chat_use=self._use_of(self.model_label), skills=self.skills)
        finally:
            self._team_helper = ""
            self.progress.start("Thinking")
        # A job in which no step got anywhere would most likely fail the same way again: stop the batches.
        # (A failed job, one that ended without a result, and outcome "stuck" - no step finished - all count.)
        self._team_stalled = (not text.startswith("Team job finished (outcome: ")
                              or text.startswith("Team job finished (outcome: stuck"))
        return f"{text}\n(team job {self._team_jobs} of {MAX_TEAM_JOBS_PER_MESSAGE} for this message)"

    def _team_refusal(self) -> str:
        """Why another team job may not start in this message ("" when it may)."""
        if self._team_jobs >= MAX_TEAM_JOBS_PER_MESSAGE:
            return TEAM_LIMIT_REACHED
        if self._team_stalled:
            return TEAM_STALLED
        return ""


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
                             extra_commands=lambda: holder[0].skill_entries() if holder else {},
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
                      project_allow=settings.project_allow, newly_trusted=not was_trusted,
                      model_source=None if args.model else settings.model_source,
                      team_models=settings.team_models, team_models_source=settings.team_models_source,
                      team_model_uses=settings.team_model_uses)
        holder.append(app)
        # ~/.agentx/settings.json is written by ChatApp.start once the model is built, so it holds its real name.
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
