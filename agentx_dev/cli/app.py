"""The agentx chat app: the loop, the turn, the session, and the command-line entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from rich.console import Console

from agentx_dev import AgentRunner, AgentType, Session
from agentx_dev.ChatModel import _is_timeout
from agentx_dev.cli.approval import MODES, Gate
from agentx_dev.cli.commands import (AUTO_NOTICE, command_descriptions, command_names, hide_secrets, run_command,
                                     shown, skill_request)
from agentx_dev.cli.compact import RETRY_AFTER_MESSAGES, compact, needs_compaction
from agentx_dev.Runner.Trim import TrimPolicy, trim_tool_results
from agentx_dev.cli.context import build_addendum
from agentx_dev.cli.mcp import INSTALL_HINT, KINDS, MCPManager, ServerConfig, config_path, valid_name
from agentx_dev.cli.memory import append_entry, entry_text, read_recent, record_from_events, summary_entry_text
from agentx_dev.cli.recall import ProjectMemory, auto_block, choose_embeddings, make_recall_tool, worth_a_note
from agentx_dev.cli.models import (ModelError, build_team_models, choose_model, describe_prompt, is_auto_note,
                                   parse_descriptions, parse_team_models, team_models_line, usable_team_models)
from agentx_dev.cli.models import model_label as label_of
from agentx_dev.cli.models import provider_for
from agentx_dev.cli.safepaths import safe_location
from agentx_dev.cli.sessions import SessionInfo, find_session, latest_session, list_sessions, new_session_path
from agentx_dev.cli.settings import (DEFAULT_TOKEN_LIMIT, PLACEHOLDER_MODEL, ensure_project_settings, ensure_user_settings,
                                     is_project_folder, load_settings, project_settings_path, user_settings_path,
                                     write_project_model, write_project_team_models, write_user_token_limit)
from agentx_dev.cli.team import (MAX_TEAM_JOBS_PER_MESSAGE, counted_tokens, current_helper, current_status,
                                 make_team_tool, note_usage, on_main, run_team_job, step_token_share, team_request)
from agentx_dev.cli.tools import build_tools, gate_tool
from agentx_dev.cli.trust import ensure_trusted, is_trusted
from agentx_dev.cli.ui import Progress, PromptInput, Screen, is_terminal, make_asker, status_line
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
# The "What's new" lines of the start screen: the newest changes a user would notice, kept to three.
WHATS_NEW = (
    "Team jobs: reading helpers run side by side, fewer re-reads",
    "An audit skill: /auditing-a-project",
    "MCP servers: /mcp add; read-only tools run without asking",
)


# What every tool call returns once the user has said no at the token limit question, so the agent (and any team
# helper) answers with what it has instead of starting more work.
WRAP_UP = ("Refused: the user's token limit for this message was reached and they asked you to stop. Do not call "
           "any more tools. Give your final answer now, using only what you have already found, and say what is "
           "unfinished.")
WRAP_UP_TOKENS = 50_000      # tokens a wrapping-up turn may still use (at least; half the limit when that is more)


class TokenLimitReached(KeyboardInterrupt):
    """Raised from a model's ``on_usage`` hook when the user declines to go past the token limit of a message. A
    ``KeyboardInterrupt`` so that every layer (tool dispatch, team helpers, the Supervisor) lets it through the
    way it lets Ctrl+C through, and the turn ends the same way."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"token limit: {used} of {limit}")
        self.used = used
        self.limit = limit

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
                 team_model_uses: Optional[Mapping[str, str]] = None,
                 token_limit: int = DEFAULT_TOKEN_LIMIT) -> None:
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
        self.progress.board = self._team_board
        self._team_running: List[str] = []   # the helpers whose steps are running (several when they only read)
        self._team_rows: Dict[int, Any] = {}  # slot -> StepStatus of each running step, drawn above the spinner
        self._team_steps = (0, 0)             # (steps ended, steps planned) in the running job
        self._team_log: List[Any] = []        # every step of the last team job, for /team log
        self._team_jobs = 0                # team jobs started for the current message
        self._team_stalled = False         # the last job in this message finished no step
        asker = make_asker(console, self.read_short, self.gate.cwd, select=select,
                           helper=current_helper, helper_models=self.helper_models_line)

        def ask_with_spinner_paused(request: Any) -> Any:           # a prompt must not fight the spinner
            with self.progress.paused():
                return asker(request)

        remembering = self._remembering(ask_with_spinner_paused)
        # helpers run in worker threads: every approval is asked on the main thread, one at a time
        self.gate.ask = lambda request: on_main(remembering, request)
        self.base_tools = build_tools(self.cwd, self.add_dirs, self.gate, observer=self._observe_tool)
        self.screen.cwd = str(self.cwd)
        self.tools = [*self.base_tools,
                      self._within_job_limit(gate_tool(make_team_tool(self._start_team_job), self.gate))]
        self._memory_on = is_project_folder(self.cwd, self.home)    # no log or folder settings for home / a drive root
        # The agent's skills: the user's own folder, and (read-only, untrusted) this project's, when it is a real
        # project and neither .agentx nor .agentx/skills is a link that leads out of it.
        project_skills = ([self.cwd / ".agentx" / "skills"]
                          if self._memory_on and safe_location(self.cwd, "skills") else [])
        # observer: a team helper opening a skill is drawn like its other tool calls (the chat's own are drawn from
        # the runner's events; ``_observe_tool`` ignores a call made outside a team job).
        # remember_reads: a skill already read in this conversation is not sent again in full.
        self.skills = Skills.default(self.home, project_dirs=project_skills, observer=self._observe_tool,
                                     remember_reads=True, guard=lambda: self.gate.closed)    # wrapping up: no reads
        self._runner_skills: Dict[str, str] = {}      # name -> description, for the current runner (_make_runner)
        # Project memory: a note per worthwhile turn in .agentx/notes, an index over them, the recall tool, and the
        # relevant notes added to the first message of a fresh conversation. Not for the home folder or a drive root.
        self.project_memory: Optional[ProjectMemory] = None
        if self._memory_on:
            embeddings, embedder_id = choose_embeddings(self.env)
            self.project_memory = ProjectMemory(self.cwd, embeddings, embedder_id)
            self.tools.append(make_recall_tool(self.project_memory))
        self._recall_next = False                     # the next message is the first of a fresh conversation
        self._newly_trusted = bool(newly_trusted)    # main: the user said yes to the trust question in this run
        self.project_settings_created: Optional[Path] = None
        self._log_warned = False
        self._compact_wait = 0             # no summarising until the history has this many messages
        self.addendum = build_addendum(self.cwd, self.home)
        self.model: Any = None
        self._usage_models: List[Tuple[str, str, Any]] = []     # (label, "chat" or "helper", model) used this session
        # (requests, input, output, cached input) of the last turn
        self._last_turn_usage: Optional[Tuple[int, int, int, int]] = None
        self.token_limit = max(0, int(token_limit or 0))   # tokens one message may use before asking; 0 is off
        self._limit_lock = threading.Lock()
        self._turn_start: Optional[int] = None    # tokens used before the running turn; None outside a turn
        self._turn_start_sent = 0                  # the same, every input token counted in full
        self._turn_allowed = 0                    # tokens the running turn may use before the next question
        self._turn_stopped = False                # past even the wrap-up allowance: every later reply stops
        self._wrapping_up = False                 # the user said no: tools refuse, the agent answers with what it has
        self.mcp: Optional[MCPManager] = None     # the user's MCP servers (``/mcp``), connected at startup
        self._mcp_tools: List[Any] = []           # their tools, gated, also in ``self.tools``
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
                             skills=self.skills, trim_results=True)
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
        self._recall_next = False                     # a resumed conversation already has its context
        return True

    def start(self, resume: Optional[str] = None) -> int:
        """Build the model and the session, then run the chat loop. Returns the exit code."""
        self._show_startup_warnings()
        try:
            self.model = self.model_factory(self.requested_model, self.env)
            self._track_usage_model(self.model_label, self.model, "chat")
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
        self._show_welcome()
        self._connect_mcp()                      # before the first runner, so it has the servers' tools
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
        if self.gate.mode == "auto":
            title, _, rest = AUTO_NOTICE.partition(": ")
            self.screen.callout(f"{title} is on.", rest[:1].upper() + rest[1:])
        self._show_settings_created()
        if self.project_settings_created is not None:
            self.notice(f"Created {self._safe(self.project_settings_created, 400)} with this folder's model. "
                        "/model <name> --folder changes it.")
        try:
            return self.loop()
        finally:
            if self.mcp is not None:
                self.mcp.close()

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
        usage_before = self._usage_totals()
        with self._limit_lock:
            self._turn_start = counted_tokens(usage_before[1], usage_before[2], usage_before[3])
            self._turn_start_sent = usage_before[1] + usage_before[2]
            self._turn_allowed = self.token_limit
            self._turn_stopped = False
            self._wrapping_up = False
            self.gate.closed = None
        try:
            self._maybe_compact()
            self.progress.start("Thinking")
            sent = text
            if self._recall_next:                      # the first message of a fresh conversation: add close notes
                self._recall_next = False
                if self.project_memory is not None:
                    sent = text + auto_block(self.project_memory, text)
            for event in self.session.stream(sent):
                self.progress.stop()                       # erase the spinner, then print
                self.screen.show_event(event)
                kind = event.get("type") if isinstance(event, dict) else None
                if kind in ("tool_call", "tool_result", "final"):
                    events.append(event)
                skills = self.screen.skills_line()
                if skills and kind not in ("final", "completion"):
                    self.progress.start(f"Skills: {self._safe(skills, 120)}")      # the open run, growing in place
                elif kind == "tool_call":
                    self.progress.start(f"Running {self._safe(event.get('name', ''), 40)}")
                elif kind not in ("final", "completion"):
                    self.progress.start("Thinking")
            self.screen.flush()
            finished = True
        except TokenLimitReached as stop:
            self.notice(f"Stopped: this message used {stop.used:,} tokens, past your limit of {stop.limit:,} a "
                        "message, and wrapping up took more. Your earlier turns are kept; this one was not recorded. "
                        "Changes already made in it remain on disk. /limit shows or changes the limit.")
        except KeyboardInterrupt:
            self.notice("Turn interrupted (Ctrl+C). Your earlier turns are kept; this one was not recorded. "
                        "Changes already made in that turn remain on disk.")
        except Exception as e:
            self.error(self._failure(e))
        finally:
            self.progress.stop()
            self.screen.flush()                       # a run of skill reads left open by an error or Ctrl+C
            with self._limit_lock:
                self._turn_start = None
                self._wrapping_up = False
                self.gate.closed = None
            self.progress.stop()
            self.save()
            after = self._usage_totals()
            self._last_turn_usage = tuple(b - a for a, b in zip(usage_before, after))      # type: ignore[assignment]
        if finished:
            try:
                self._log_turn(text if log_title is None else log_title, events)
            except Exception:                  # the turn itself went well: a log failure must not end the app
                self._warn_log_failed()

    # -- /mcp -------------------------------------------------------------------

    def _mcp_manager(self) -> MCPManager:
        if self.mcp is None:
            self.mcp = MCPManager(self.home, self.env, show=self._mcp_show)
        return self.mcp

    def _mcp_show(self, text: str) -> None:
        with self.progress.paused():
            for line in str(text).splitlines():
                self.notice(self._safe(line, 2000))

    def _show_welcome(self) -> None:
        """The box at the top: who, the mark, the model and folder; tips for this folder; what is new."""
        import getpass
        try:
            who = getpass.getuser()
        except Exception:
            who = ""
        greeting = "Welcome to agentx!" if self.settings_created is not None else (
            f"Welcome back {who}!" if who else "Welcome back!")
        provider = provider_for(self.model_label) or ""
        model = " · ".join(part for part in (self.model_label, {"gpt": "OpenAI", "claude": "Anthropic"}.get(
            provider, ""), f"{self.gate.mode} mode") if part)
        tips = []
        if self._memory_on and not (self.cwd / "AGENTX.md").exists():
            tips.append("Run /init to create an AGENTX.md with notes about this project")
        if _resolved(self.cwd) == _resolved(self.home):
            tips.append("Note: this is your home folder; start agentx in a project folder")
        tips.append("Type / to see the commands and skills; Alt+Enter adds a line")
        tips.append("Edits and commands ask first; Shift+Tab changes the mode")
        self.screen.welcome(version=_version(), greeting=self._safe(greeting, 60),
                            model=self._safe(model, 80), folder=self._safe(self._short_folder(), 200), tips=tips[:4],
                            news=WHATS_NEW)
        if self.add_dirs:                        # in full, under the box: a cut path would hide where it points
            self.notice(f"extra folders: {', '.join(self._safe(folder, 300) for folder in self.add_dirs)}")

    def _short_folder(self, limit: int = 42) -> str:
        """The working folder for the start screen: ``~`` for the home folder, cut at the front when long."""
        text = str(self.cwd)
        try:
            text = str(Path("~") / _resolved(self.cwd).relative_to(_resolved(self.home)))
        except ValueError:
            pass
        return text if len(text) <= limit else "…" + text[-(limit - 1):]

    def status_bar(self) -> Any:
        """The lines under the input: the mode and how to change it, the model and the folder's name."""
        return status_line(self.gate.mode, self.model_label, self.cwd.name or str(self.cwd))

    def _connect_mcp(self) -> None:
        """At startup: connect the servers in ~/.agentx/mcp.json (never opening a browser) and add their tools."""
        try:
            manager = self._mcp_manager()
        except Exception as error:                 # a broken file must not stop the app
            self.error(f"MCP servers were not loaded ({type(error).__name__}).")
            return
        for problem in manager.problems:
            self.error(self._safe(problem, 300))
        if not manager.servers:
            return
        self.progress.start("Connecting to MCP servers")
        try:
            statuses = manager.connect_all()
        finally:
            self.progress.stop()
        signin = [name for name, status in statuses.items() if status.startswith("needs sign-in")]
        down = [name for name, status in statuses.items()
                if not status.startswith("connected") and name not in signin]
        if signin:
            many = len(signin) != 1
            self.screen.warning(f"{len(signin)} MCP server{'s' if many else ''} need{'' if many else 's'} sign-in: "
                                f"{self._safe(', '.join(signin), 200)}",
                                f"run /mcp login {signin[0]}" if not many else "run /mcp login <name>")
        for name in down:
            self.screen.warning(f"MCP {self._safe(name, 40)}: {self._safe(statuses[name], 300)}", "run /mcp")
        for name, status in statuses.items():
            if status.startswith("connected"):
                self._note_new_mcp_tools(name, quiet_if_none=True)
        self._refresh_mcp_tools(attach=False)

    def _note_new_mcp_tools(self, name: str, quiet_if_none: bool = False) -> None:
        """Allow the read-only tools ``name`` shows for the first time, and say what runs without asking."""
        manager = self._mcp_manager()
        added = manager.note_new_tools(name)
        if quiet_if_none and not added:
            return
        kinds = manager.tool_kinds(name)
        allowed = [tool for tool in kinds if tool in manager.servers[name].allow]
        asking = len(kinds) - len(allowed)
        self.notice(f"MCP {self._safe(name, 40)}: {len(allowed)} of its {len(kinds)} tools run without asking (the "
                    f"server marks them read-only); {asking} ask first. /mcp tools {name} lists them; /mcp allow and "
                    "/mcp ask change one.")

    def _refresh_mcp_tools(self, attach: bool = True) -> None:
        """Put the connected servers' tools (gated) in ``self.tools``; with ``attach``, give the conversation a
        runner that has them, keeping its history."""
        old = {id(tool) for tool in self._mcp_tools}
        self.tools = [tool for tool in self.tools if id(tool) not in old]
        fresh = []
        for tool in (self.mcp.tools() if self.mcp is not None else []):
            wrapped = gate_tool(tool, self.gate, observer=self._observe_tool)
            wrapped.mcp_server, wrapped.mcp_remote = tool.mcp_server, tool.mcp_remote
            fresh.append(wrapped)
        self._mcp_tools = fresh
        self.tools.extend(fresh)
        # The servers' allowed tools run without asking: exact names, kept apart from the user's own rules.
        servers = self.mcp.servers if self.mcp is not None else {}
        self.gate.extra_allow = [tool.name for tool in fresh
                                 if tool.mcp_remote in getattr(servers.get(tool.mcp_server), "allow", [])]
        if attach and self.session is not None and self.model is not None:
            self.session.attach(self._make_runner(self.model))

    def show_mcp(self) -> None:
        manager = self._mcp_manager()
        path = self._safe(config_path(self.home), 300)
        if not manager.servers:
            self.notice(f"No MCP servers yet. Add one with /mcp add <name> <url> (or /mcp add <name> -- <command> "
                        f"[args]). They are kept in {path}.")
            return
        lines = ["MCP servers:"]
        for name, config in manager.servers.items():
            status = manager.status.get(name, "not connected")
            lines.append(f"  {self._safe(name, 40)}  {self._safe(config.describe(), 200)}  - {self._safe(status, 200)}")
        if self._mcp_tools:
            names = ", ".join(tool.name for tool in self._mcp_tools[:12])
            more = f" and {len(self._mcp_tools) - 12} more" if len(self._mcp_tools) > 12 else ""
            lines.append(f"Tools: {self._safe(names, 800)}{more}.")
        lines.append(f"Kept in {path}. Tools a server marks read-only run without asking; the rest ask, even in auto "
                     "mode. /mcp tools <name> lists a server's tools, /mcp allow|ask <name> <tool> changes one, "
                     "/mcp login|logout|remove <name>.")
        self.notice("\n".join(lines))

    def mcp_add(self, name: str, config: ServerConfig) -> None:
        manager = self._mcp_manager()
        if not valid_name(name):
            self.error("An MCP server name is lower-case letters, digits, - and _ (at most 40), starting with a "
                       "letter or digit.")
            return
        if name in manager.servers:
            status = manager.status.get(name, "not connected")
            self.error(f"There is already an MCP server called {self._safe(name, 40)} ({self._safe(status, 120)}). "
                       f"/mcp login {name} signs in again; /mcp remove {name} deletes it.")
            return
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.error(INSTALL_HINT)
            return
        if config.transport == "stdio" and not self._yes(
                f"This runs `{self._safe(config.describe(), 300)}` on your computer each time agentx starts, and "
                "its tools act for the agent. Add it? [y/N] "):
            self.notice("Nothing was added.")
            return
        try:
            path = manager.add(config)
        except (OSError, ValueError) as error:
            self.error(f"Could not save the server ({type(error).__name__}).")
            return
        self.notice(f"Added {self._safe(name, 40)} to {self._safe(path, 300)}. Connecting...")
        status = manager.connect(name)
        if status.startswith("needs sign-in") and self._yes(
                f"{name} wants you to sign in. Open the sign-in page in your browser now? [y/N] "):
            status = manager.login(name)
        self.notice(f"MCP {self._safe(name, 40)}: {self._safe(status, 300)}")
        if status.startswith("connected"):
            self._note_new_mcp_tools(name)
        self._refresh_mcp_tools()

    def _known_server(self, name: str) -> bool:
        if name in self._mcp_manager().servers:
            return True
        self.error(f"No MCP server called {self._safe(name, 40)}. /mcp lists them.")
        return False

    def show_mcp_tools(self, name: str) -> None:
        """``/mcp tools <name>``: every tool of a connected server, what the server says it does, and whether it
        runs without asking."""
        if not self._known_server(name):
            return
        manager = self._mcp_manager()
        kinds = manager.tool_kinds(name)
        if not kinds:
            self.notice(f"{name} is not connected ({self._safe(manager.status.get(name, 'not connected'), 200)}), so "
                        "its tools are not known yet.")
            return
        allow = manager.servers[name].allow
        free = [tool for tool in kinds if tool in allow]
        asking = [tool for tool in kinds if tool not in allow]
        lines = [f"{name}: {len(kinds)} tools. Run without asking ({len(free)}):"]
        lines += [f"  {self._safe(tool, 60)}  ({KINDS[kinds[tool]]})" for tool in free] or ["  (none)"]
        lines.append(f"Ask first ({len(asking)}):")
        lines += [f"  {self._safe(tool, 60)}  ({KINDS[kinds[tool]]})" for tool in asking] or ["  (none)"]
        lines.append(f"/mcp allow {name} <tool> [more...] lets tools run without asking (or: read-only, all); "
                     f"/mcp ask {name} <tool> [more...] makes them ask (or: all). Saved in "
                     f"{self._safe(config_path(self.home), 300)}. A deny rule in your settings still wins.")
        self.notice("\n".join(lines))

    def mcp_allow(self, name: str, words: List[str], allowed: bool) -> None:
        """``/mcp allow|ask <name> <tool...>`` (``all``; for allow also ``read-only``)."""
        if not self._known_server(name):
            return
        manager = self._mcp_manager()
        kinds = manager.tool_kinds(name)
        known = set(kinds) | set(manager.servers[name].seen)
        prefix = f"{name}__"
        picked: List[str] = []
        for word in words:
            if word == "all":
                picked += list(kinds) or list(known)
            elif word == "read-only" and allowed:
                picked += [tool for tool, kind in kinds.items() if kind == "read-only"]
            else:
                tool = word[len(prefix):] if word.startswith(prefix) else word
                if tool not in known:
                    self.error(f"{name} has no tool called {self._safe(word, 60)}. /mcp tools {name} lists them.")
                    return
                picked.append(tool)
        picked = list(dict.fromkeys(picked))
        if not picked:
            self.notice("Nothing changed.")
            return
        risky = [tool for tool in picked if kinds.get(tool) == "destructive"]
        if allowed and risky and not self._yes(
                f"{', '.join(self._safe(t, 40) for t in risky[:8])} {'deletes or cancels' if len(risky) == 1 else 'delete or cancel'} "
                "things (the server says so). Let them run without asking? [y/N] "):
            picked = [tool for tool in picked if tool not in risky]
            if not picked:
                self.notice("Nothing changed.")
                return
        try:
            manager.set_allowed(name, picked, allowed)
        except (OSError, ValueError) as error:
            self.error(f"Could not save the change ({type(error).__name__}).")
            return
        self._refresh_mcp_tools()
        what = "run without asking" if allowed else "ask first"
        self.notice(f"{len(picked)} {name} tool{'s' if len(picked) != 1 else ''} now {what}: "
                    f"{self._safe(', '.join(picked[:12]), 600)}{' ...' if len(picked) > 12 else ''}.")

    def mcp_remove(self, name: str) -> None:
        if not self._known_server(name):
            return
        if not self._yes(f"Remove the MCP server {name} and its saved sign-in? [y/N] "):
            self.notice("Nothing was removed.")
            return
        self._mcp_manager().remove(name)
        self._refresh_mcp_tools()
        self.notice(f"Removed {self._safe(name, 40)}.")

    def mcp_login(self, name: str) -> None:
        if not self._known_server(name):
            return
        manager = self._mcp_manager()
        if manager.servers[name].transport == "stdio":
            self.notice(f"{name} runs on your computer; there is nothing to sign in to. Reconnecting.")
        status = manager.login(name)
        self.notice(f"MCP {self._safe(name, 40)}: {self._safe(status, 300)}")
        if status.startswith("connected"):
            self._note_new_mcp_tools(name, quiet_if_none=True)
        self._refresh_mcp_tools()

    def mcp_logout(self, name: str) -> None:
        if not self._known_server(name):
            return
        self._mcp_manager().logout(name)
        self._refresh_mcp_tools()
        self.notice(f"Signed out of {self._safe(name, 40)}: its saved sign-in was deleted. /mcp login {name} signs "
                    "in again.")

    # -- /memory ----------------------------------------------------------------

    def show_memory(self, query: str = "") -> None:
        """``/memory``: what is kept and how it is searched, and the newest notes. ``/memory <words>``: the notes
        the recall tool would find for those words."""
        if self.project_memory is None:
            self.notice("Project memory is off here: no notes are kept for your home folder or a drive root.")
            return
        memory = self.project_memory
        query = query.strip()
        if query:
            hits = memory.search(query)
            if not hits:
                self.notice("No notes match that.")
                return
            lines = [f"Notes for {self._safe(query, 80)!r}:"]
            for hit in hits:
                lines.append(f"  {self._safe(hit.when, 20)}  {self._safe(hit.title, 80)}  ({self._safe(hit.path, 120)})")
            self.notice("\n".join(lines))
            return
        lines = [memory.describe()]
        newest = memory.notes()[-5:]
        if newest:
            lines.append("Newest notes:")
            for path in reversed(newest):
                lines.append(f"  {self._safe(path.name, 120)}")
        lines.append("A note is written after each turn that changes files, runs commands or gives a real answer. "
                     "The agent searches them with its recall tool, and a fresh conversation starts with the "
                     "closest ones. /memory <words> shows what it would find.")
        self.notice("\n".join(lines))

    # -- tokens used ----------------------------------------------------------

    def _track_usage_model(self, label: str, model: Any, role: str) -> None:
        """Remember a model whose token counters ``/usage`` should add up (once per object)."""
        if model is not None and not any(model is known for _, _, known in self._usage_models):
            self._usage_models.append((str(label), role, model))
            try:
                model.on_usage = self._check_token_limit
                model.on_retry = self._show_retry
            except (AttributeError, TypeError):          # an object that takes no attributes keeps no usage either
                pass

    def _show_retry(self, attempt: int, error: BaseException, delay: float) -> None:
        """A tracked model's ``on_retry`` hook: say that a model request failed and is being sent again, so a long
        wait does not look like a hang. (Said on the main thread: a team helper's request runs in a worker.)"""
        on_main(self._say_retry, attempt, error, wait=False)

    def _say_retry(self, attempt: int, error: BaseException) -> None:
        what = "got no answer in time" if _is_timeout(error) else f"failed ({type(error).__name__})"
        helper = current_helper()
        who = f" ({self._safe(helper, 60)})" if helper else ""
        with self.progress.paused():
            self.notice(f"A model request{who} {what}; trying again (attempt {attempt + 1}).")

    def _check_token_limit(self, _model: Any = None) -> None:
        """A tracked model's ``on_usage`` hook. Once the running message has used ``token_limit`` tokens (input
        and output, every model), ask whether to go on for another ``token_limit``. A no (or no answer) wraps the
        turn up: every tool call is refused with ``WRAP_UP`` from then on, so the agent and any team helpers answer
        with what they have, and the turn ends normally and is kept. Should wrapping up itself use more than its
        allowance (``WRAP_UP_TOKENS``, or half the limit), ``TokenLimitReached`` ends the turn like Ctrl+C. Helpers
        running at the same time wait for the answer; "a" goes on without asking again in this message. Outside a
        turn, or with no limit, nothing. A team helper's reply also counts against its step's share."""
        note_usage(_model)                   # a helper whose job is over stops here
        with self._limit_lock:
            if self._turn_start is None or not self.token_limit:
                return
            _, tokens_in, tokens_out, cached = self._usage_totals()
            used = counted_tokens(tokens_in, tokens_out, cached) - self._turn_start      # cached input as a tenth
            sent = tokens_in + tokens_out - self._turn_start_sent
            if self._turn_stopped:
                raise TokenLimitReached(used, self.token_limit)
            if used < self._turn_allowed:
                return
            if self._wrapping_up:
                self._turn_stopped = True
                raise TokenLimitReached(used, self.token_limit)
            answer = on_main(self._ask_to_go_on, used, sent)
            if answer == "a":
                self._turn_allowed = sys.maxsize
                return
            if answer == "y":
                self._turn_allowed = used + self.token_limit
                return
            self._wrapping_up = True
            self.gate.closed = WRAP_UP
            self._turn_allowed = used + max(WRAP_UP_TOKENS, self.token_limit // 2)
            on_main(self._say_paused, "Wrapping up: no more tool calls in this message; the agent answers with "
                                      "what it has found so far.")

    def _ask_to_go_on(self, used: int, sent: int = 0) -> str:
        """The limit question: "y" (another limit), "a" (go on, and do not ask again in this message) or "n". Enter,
        end of input, Ctrl+C and a reader that fails are all no; any other answer is asked once more."""
        cache = (f" ({sent:,} sent and received; input from the provider's cache counts as a tenth, about what it "
                 "costs)") if sent > used else ""
        prompt = (f"This message has used {used:,} tokens{cache}; your limit is {self.token_limit:,} a message "
                  f"(/limit changes it). Keep going for up to {self.token_limit:,} more? a = yes, and do not ask "
                  "again in this message. No wraps up with what was found so far. [y/a/N] ")
        with self.progress.paused():
            for attempt in range(2):
                try:
                    answer = str(self.read_short(prompt)).strip().lower()
                except (EOFError, KeyboardInterrupt, Exception):
                    return "n"
                if answer in ("y", "yes"):
                    return "y"
                if answer in ("a", "all", "always"):
                    return "a"
                if answer in ("", "n", "no"):
                    return "n"
                if attempt == 0:
                    self.notice("Please answer y, a or n.")
        return "n"

    def _say_paused(self, text: str) -> None:
        with self.progress.paused():
            self.notice(text)

    def set_token_limit(self, limit: int) -> None:
        """``/limit <tokens>``: use ``limit`` from now on (0 is no limit) and save it in ~/.agentx/settings.json."""
        self.token_limit = max(0, int(limit))
        _, message = write_user_token_limit(self.home, self.token_limit)
        shown = f"{self.token_limit:,} tokens a message" if self.token_limit else "off"
        self.notice(f"Token limit: {shown}. {self._safe(message, 300)}")

    def show_token_limit(self) -> None:
        if self.token_limit:
            self.notice(f"Token limit: {self.token_limit:,} tokens a message. When a message gets there, agentx asks "
                        "before it goes on. /limit <tokens> changes it (300k, 1m, and 0 for no limit also work).")
        else:
            self.notice("Token limit: off. /limit <tokens> sets one (for example /limit 200k).")

    @staticmethod
    def _counters(model: Any) -> Tuple[int, int, int, int]:
        """``(requests, input tokens, output tokens, cached input tokens)`` a model has recorded; zeros when it
        keeps none."""
        usage = getattr(model, "usage", None)
        try:
            return (int(getattr(usage, "total_calls", 0) or 0), int(getattr(usage, "total_input_tokens", 0) or 0),
                    int(getattr(usage, "total_output_tokens", 0) or 0),
                    int(getattr(usage, "total_cache_read_tokens", 0) or 0))
        except (TypeError, ValueError):
            return 0, 0, 0, 0

    def _usage_totals(self) -> Tuple[int, int, int, int]:
        """``(requests, input, output, cached input)`` summed over every model used so far."""
        rows = [self._counters(model) for _, _, model in self._usage_models]
        return tuple(sum(row[i] for row in rows) for i in range(4))              # type: ignore[return-value]

    @staticmethod
    def _cached_note(cached: int, tokens_in: int) -> str:
        if not cached or not tokens_in:
            return ""
        return f" ({cached:,} of the input, {cached * 100 // tokens_in}%, from the provider's cache)"

    def show_usage(self) -> None:
        """``/usage``: the tokens used this session, per model, and what the last turn used. Counts what the
        provider's replies report; a model that reports nothing shows zeros."""
        rows: Dict[Tuple[str, str], List[int]] = {}
        for label, role, model in self._usage_models:
            row = rows.setdefault((label, role), [0, 0, 0, 0])
            for i, value in enumerate(self._counters(model)):
                row[i] += value
        requests, tokens_in, tokens_out, cached_in = self._usage_totals()
        if requests == 0:
            self.notice("No model calls yet in this session.")
            return
        lines = ["Session (all models):",
                 f"  {requests:,} requests, {tokens_in:,} input tokens, {tokens_out:,} output tokens"
                 + self._cached_note(cached_in, tokens_in)]
        if len(rows) > 1 or any(role == "helper" for _, role in rows):
            for (label, role), (calls, inp, out, cached) in rows.items():
                if calls:
                    extra = f", {cached:,} of the input from the provider's cache" if cached else ""
                    lines.append(f"  {self._safe(label, 60)} ({role}): {calls:,} requests, {inp:,} in, {out:,} out{extra}")
        if self._last_turn_usage is not None and self._last_turn_usage[0] > 0:
            calls, inp, out, cached = self._last_turn_usage
            lines.append(f"Last turn: {calls:,} request{'s' if calls != 1 else ''}, {inp:,} input tokens, "
                         f"{out:,} output tokens" + self._cached_note(cached, inp))
        limit = f"{self.token_limit:,} tokens a message" if self.token_limit else "off"
        lines.append(f"Token limit: {limit} (/limit).")
        lines.append("Every request re-sends the conversation, so input grows with the number of steps in a turn. "
                     "Long results are trimmed, big files are read in pieces, and /clear starts fresh. Input the "
                     "provider served from its cache is billed at its cheaper cached rate.")
        self.notice("\n".join(lines))

    # -- project memory: the work log and summarising ------------------------

    def _hide(self, text: str) -> str:
        """``text`` without key-shaped tokens or the environment's actual key values (the same hiding as error
        messages, see ``_redacted``), but not flattened or cut: newlines are kept."""
        return hide_secrets(_without_env_keys(str(text), self.env))

    def _log_turn(self, title: str, events: List[dict]) -> None:
        if not self._memory_on:
            return
        record, now = record_from_events(title, events), datetime.now()
        self._append_log(entry_text(record, now, self._hide))
        if self.project_memory is not None and worth_a_note(record):
            self.project_memory.write(record, now, self._hide)        # never raises; a failure is logged

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
        # Measured on what the model is actually sent (old big tool results are stubs), not on the saved history.
        if len(history) < self._compact_wait or not needs_compaction(trim_tool_results(history, TrimPolicy())):
            return
        self.progress.start("Summarising the conversation")
        try:
            result = compact(history, self._summary_call)
        finally:
            self.progress.stop()
        if result.status == "done":
            self.session.history = result.history
            self._forget_skill_reads()          # the skill texts went into the summary: they may be read again
            self.notice(f"Summarised {result.replaced} earlier messages to keep the conversation short.")
            self._append_log(summary_entry_text(result.summary, datetime.now(), self._hide))
        elif result.status == "failed":
            self._compact_wait = len(history) + RETRY_AFTER_MESSAGES
            self.notice("The conversation is getting long, but it could not be summarised right now; "
                        "it will be tried again later.")

    def _forget_skill_reads(self) -> None:
        runner = getattr(self.session, "runner", None)
        for tool in getattr(runner, "tools", None) or []:
            forget = getattr(tool, "forget_reads", None)
            if callable(forget):
                forget()

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
        self._track_usage_model(self.model_label, model, "chat")
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
        self._recall_next = True

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
        """Called by the tool wrappers before the gate check, on the thread that runs the tool. A team helper's calls
        are not printed: they are counted on its step's line above the spinner (the step's own wrapper counts the
        other tools; the skills it opens are counted here) and listed by /team log."""
        status = current_status()
        if status is not None and name == "use_skill" and isinstance(args, dict):
            status.note_skill(str(args.get("name") or "?"))

    def _team_board(self) -> Any:
        rows = list(self._team_rows.values())
        return self.screen.team_board(rows, *self._team_steps) if rows else None

    def show_team_log(self) -> None:
        """``/team log``: every call of every step of the last team job in this session."""
        if not self._team_log:
            self.notice("No team job has run in this session yet.")
            return
        self.screen.team_log(self._team_log)

    def _show_helper_call(self, helper: str, name: str, args: dict) -> None:
        self.progress.stop()
        self.screen.helper_call(helper, name, args)
        self.progress.start(self._team_label())

    def _team_label(self) -> str:
        """The spinner's words during a team job: what the helpers are doing, or which are running."""
        activity = self.screen.helpers_line()
        if activity:
            return self._safe(activity, 160)
        running = self._team_running
        if not running:
            return "Team is working"
        if len(running) == 1:
            return f"Running {self._safe(running[0], 80)}"
        return f"Running {len(running)} helpers"

    def _team_notify(self, kind: str, data: dict) -> None:
        self.progress.stop()
        if kind == "plan":
            steps = data.get("steps") or []
            self._team_steps = (0, len(steps))
            self.screen.team_plan(steps)
            self.progress.start("Starting the team")
        elif kind == "dispatch":
            self._team_running.append(str(data.get("agent", "")))
            status = data.get("status")
            if status is not None:
                self._team_rows[status.slot] = status
                self._team_log.append(status)
            else:                                   # no status to draw: say that the step started
                self.screen.team_dispatch(data.get("agent"), data.get("query"))
            self.progress.start("Team is working")
        elif kind == "result":
            agent = str(data.get("agent", ""))
            if agent in self._team_running:
                self._team_running.remove(agent)
            status = data.get("status")
            if status is not None:
                self._team_rows.pop(status.slot, None)
            ended, planned = self._team_steps
            self._team_steps = (ended + 1, planned)
            self.screen.team_done(agent, data.get("label") or agent, bool(data.get("ok")), data.get("text"),
                                  partial=bool(data.get("partial")), skipped=bool(data.get("skipped")),
                                  status=status)
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
        for extra in extras:
            self._track_usage_model(extra.name, extra.model, "helper")
        self.progress.stop()                      # the notices below must not be drawn over the spinner
        for note in notes:
            self.notice(self._safe(note, 300))
        self._team_rows, self._team_log, self._team_steps = {}, [], (0, 0)
        self.progress.start("Planning the team job")
        try:
            text = run_team_job(task, model=self.model, tools=[*self.base_tools, *self._mcp_tools],
                                addendum=self.addendum,
                                notify=self._team_notify, ask_user=self._team_question,
                                describe_error=self._safe, extra_models=extras,
                                chat_use=self._use_of(self.model_label), skills=self.skills,
                                step_tokens=step_token_share(self.token_limit), closed=lambda: self.gate.closed)
        finally:
            self._team_running = []
            self._team_rows = {}
            self.progress.stop()
            self.screen.flush_helpers()                # what a helper did before an error or Ctrl+C
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
                             toolbar=lambda: holder[0].status_bar() if holder else "",
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
                      team_model_uses=settings.team_model_uses, token_limit=settings.token_limit)
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
