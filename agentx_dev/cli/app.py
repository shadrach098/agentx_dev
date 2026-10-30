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
from rich.text import Text

from agentx_dev import AgentRunner, AgentType, Session
from agentx_dev.cli.approval import MODES, Gate
from agentx_dev.cli.commands import (AUTO_NOTICE, command_descriptions, command_names, hide_secrets, run_command,
                                     runs_while_working,
                                     shown)
from agentx_dev.cli.context import build_addendum
from agentx_dev.cli.mcp import (MCPManager, windows_system_env, windows_user_env)
from agentx_dev.cli.memory import append_entry, entry_text, read_recent, record_from_events
from agentx_dev.cli.recall import ProjectMemory, auto_block, choose_embeddings, make_recall_tool, worth_a_note
from agentx_dev.cli.models import (ANTHROPIC_KEY, OPENAI_KEY, ModelError, build_team_models, choose_model)
from agentx_dev.cli.models import model_label as label_of
from agentx_dev.cli.models import provider_for
from agentx_dev.cli.safepaths import safe_location
from agentx_dev.cli.sessions import (SessionInfo, find_session, latest_session, list_sessions, new_session_path,
                                     sessions_dir)
from agentx_dev.cli.settings import (DEFAULT_MAX_STEPS, DEFAULT_TOKEN_LIMIT, MAX_MAX_STEPS, PLACEHOLDER_MODEL, ensure_project_settings, ensure_user_settings,
                                     is_project_folder, load_settings, project_settings_path, user_settings_path,
                                     read_user_fullscreen, write_user_fullscreen)
from agentx_dev.cli.team import (MAX_TEAM_JOBS_PER_MESSAGE, counted_tokens, current_helper, make_team_tool, note_usage, on_main, run_team_job, skip_team_step, step_token_share, team_note)
from agentx_dev.cli.typeahead import TypeAhead
from agentx_dev.cli.ui_text import plain
from agentx_dev.cli.team_record import TeamRecord
from agentx_dev.cli.tools import build_tools, gate_tool
from agentx_dev.cli.trust import ensure_trusted, is_trusted
from agentx_dev.cli.ui import Progress, PromptInput, Screen, is_terminal, make_asker, status_line
from agentx_dev.cli.app_mcp import McpCommands
from agentx_dev.cli.app_models import ModelCommands
from agentx_dev.cli.app_usage import UsageCommands
from agentx_dev.cli.app_team import TeamCommands
from agentx_dev.cli.app_voice import LiveVoice, VoiceCommands
from agentx_dev.cli.app_team import TEAM_LIMIT_REACHED, TEAM_STALLED  # noqa: F401  (re-exported)
from agentx_dev.Skills import (SkillInstallError, Skills, discover, install, open_source, preview_text)

INIT_PROMPT = (
    "Look at this project: list the top-level files, read the README and the main config files. Then write a "
    "concise AGENTX.md in the working folder covering what the project is, how to run its tests and the app, "
    "the code layout, and the conventions to follow. Keep it under 60 lines. Use write_file. If an AGENTX.md "
    "already exists, tell me instead of overwriting it."
)

# After a turn that stopped at the step limit, "continue" re-ran checks whose results were already in the conversation
# (git status, the typecheck) before going on, at the price of a whole conversation's worth of tokens each time.
KEY_NAMES = (ANTHROPIC_KEY, OPENAI_KEY)


SKIP_DONE = " (a running step finishes)."


def _held(typeahead: TypeAhead, ask: Callable[..., Any]) -> Callable[..., Any]:
    """``ask`` with the type-ahead reader held off, so the prompt gets every key."""
    def held(*args: Any, **kwargs: Any) -> Any:
        with typeahead.hold():
            return ask(*args, **kwargs)
    return held


def saved_keys() -> Dict[str, str]:
    """The API keys saved in Windows: the system variables, then the user's own (which win). Empty elsewhere."""
    found: Dict[str, str] = {}
    for reader in (windows_system_env, windows_user_env):
        found.update({name: value for name, value in reader().items() if name in KEY_NAMES})
    return found


CARRY_ON_WORDS = ("continue", "go on", "carry on", "keep going", "resume", "proceed", "go ahead")
CARRY_ON_NOTE = ("\n\n[agentx] Your last turn stopped at the step limit. Everything it did is above in this "
                 "conversation. Carry on from its last step: do not re-run checks or re-read files whose results are "
                 "already above unless something changed them since; run checks again only once you have changed "
                 "something.")


STOPPED_CARRY_ON_NOTE = ("\n\n[agentx] Your last turn stopped before it finished; what it did is noted above. Carry on "
                         "from there: check the files it changed before redoing anything.")
KEPT_SO_FAR = "What it did so far is kept in the conversation, and changes already made stay on disk."
ERROR_NEXT = "Say continue to try again, or switch model with /model <name> and say continue."
OUT_OF_CREDIT_NEXT = ("Add credit in the provider's console (Anthropic: Plans & Billing; OpenAI: Billing), or switch "
                      "model with /model <name> and say continue.")
# The words providers use when an account has no credit left (Anthropic, then OpenAI's two forms).
_NO_CREDIT = ("credit balance is too low", "insufficient_quota", "exceeded your current quota")


def _out_of_credit(error: BaseException) -> bool:
    try:
        text = str(error).lower()
    except Exception:
        return False
    return any(words in text for words in _NO_CREDIT)


def provider_name(model: Any) -> str:
    return {"claude": "Anthropic", "gpt": "OpenAI"}.get(provider_for(str(model or "")) or "", "model provider's")


def _is_carry_on(text: str) -> bool:
    words = " ".join(str(text or "").lower().replace(",", " ").replace(".", " ").replace("!", " ").split())
    return bool(words) and len(words) <= 40 and any(words == w or words.startswith(w + " ") for w in CARRY_ON_WORDS)


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


class ChatApp(McpCommands, ModelCommands, UsageCommands, TeamCommands, VoiceCommands, LiveVoice):
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
                 token_limit: int = DEFAULT_TOKEN_LIMIT, max_steps: int = DEFAULT_MAX_STEPS) -> None:
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
        self.fullscreen = False                    # set by main when agentx runs in full-screen mode
        self._user_env_reader: Callable[[], Mapping[str, str]] = windows_user_env     # for /mcp reload
        # Keys saved in Windows after this terminal opened; only read when the variables came from this process.
        self._saved_key_reader: Callable[[], Mapping[str, str]] = saved_keys if env is None else (lambda: {})
        self.progress.board = self._board
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
        self.max_steps = max(1, int(max_steps or DEFAULT_MAX_STEPS))   # steps the agent may take for one message
        self._stopped_at_step_limit = False        # the last turn ended at the step limit (see CARRY_ON_NOTE)
        self._stopped_turn = False                 # the last turn stopped early and was kept (_keep_stopped_turn)
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
        # What the user typed while the agent worked (``typed_while_working``), in order: a message is handed to the
        # agent at its next step (``_take_typed``); a command, and a message the agent did not get, run after.
        self._typed_lock = threading.Lock()
        self._typed: List[str] = []
        # The ones nobody has read yet, drawn under the spinner: ``{"text", "for": "agent" | "team" | "later"}``
        self._waiting: List[Dict[str, str]] = []
        self._team_plan: List[Dict[str, Any]] = []   # the running team job's plan, as shown (for /skip)
        try:
            self.progress.waiting = self._waiting_shown
        except Exception:
            pass

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
            f"Steps a message may take: {self.max_steps} (maxSteps in your settings file, or --max-steps)\n"
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
                             max_iterations=self.max_steps, auto_cache=False, verbose=False, parallel_tool_workers=1,
                             skills=self.skills, trim_results=True, strict_tool_dispatch=True)
        # The skills THIS conversation's agent can open (what /<skill> and the `/` pop-up offer): taken from the
        # runner itself, so it is exactly what use_skill can read, and it follows /clear, /resume and a new model.
        tool = next((t for t in runner.tools if t.name == "use_skill"), None)
        self._runner_skills = dict(getattr(tool, "skill_descriptions", None) or {})
        runner.interjections = self._take_typed
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
        self.pick_up_saved_keys()
        try:
            self.model = self.model_factory(self.requested_model, self.env)
        except ModelError as e:
            # no key for the saved model: when the other provider's key is set, offer its models instead
            self.model = self.model_from_the_other_key(e)
            if self.model is None:
                self._create_user_settings(None)
                self._show_settings_created()
                return 1
        except Exception as e:
            self.error(f"Could not set up the model ({type(e).__name__}).{self._model_source_note()}")
            self._create_user_settings(None)
            self._show_settings_created()
            return 1
        self._track_usage_model(self.model_label, self.model, "chat")
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
        self._start_typeahead()
        try:
            return self.loop()
        finally:
            if getattr(self.progress, "typeahead", None) is not None:
                self.progress.typeahead.close()
            if self.mcp is not None:
                self.mcp.close()

    # -- typing while the agent works ----------------------------------------------------------------------

    def _start_typeahead(self) -> None:
        """On the normal screen, read what is typed while the spinner runs (full-screen mode has its own input)."""
        if self.fullscreen or not getattr(self.progress, "enabled", False):
            return
        typeahead = TypeAhead(self.typed_while_working)
        if not typeahead.available:
            return
        self.progress.typeahead = typeahead
        for name in ("read_short", "select"):        # a question asked while it works gets every key
            ask = getattr(self, name)
            if callable(ask):
                setattr(self, name, _held(typeahead, ask))

    def typed_while_working(self, text: str) -> bool:
        """A line typed while the agent worked. Enter while ``/voice`` records stops it. Run at once: ``/skip``,
        ``/unsend``, ``/voice`` (it starts recording) and the commands that only show something (see
        ``runs_while_working``). Another command runs when the agent is done; a message goes to the running team
        job, else to the agent at its next step."""
        if self.recording() and not str(text or "").strip().lower().startswith("/unsend"):
            self.stop_live_voice(str(text or "").strip())   # a line typed first goes in front of the words
            return True
        text = str(text or "").strip()
        if not text:
            return True
        word, _, rest = text.partition(" ")
        word, rest = word.lower(), rest.strip()
        if word == "/skip":
            outcome = self._skip_step(rest)
            self._echo(text, outcome, done=outcome.endswith(SKIP_DONE))
            return True
        if word == "/unsend":
            outcome, done = self._unsend(rest)
            self._echo(text, outcome, done=done)
            return True
        if word == "/voice" and not rest:
            self.start_live_voice()
            return True
        if runs_while_working(text):
            self._echo(text, "ran now")
            try:
                run_command(self, text)
            except Exception as error:                # a thread of its own: show it, never raise
                self.error(self._failure(error))
            return True
        entry: Dict[str, Any] = {"text": text, "for": "later" if text.startswith("/") else "team"}
        with self._typed_lock:
            self._waiting.append(entry)
        if entry["for"] == "team":
            withdraw = team_note(text, on_read=lambda who: self._read_by(entry, who))
            if withdraw:
                entry["withdraw"] = withdraw
                return True
        entry["for"] = "later" if text.startswith("/") else "agent"
        with self._typed_lock:
            self._typed.append(text)
        return True

    def _unsend(self, which: str) -> Tuple[str, bool]:
        """``/unsend`` (the newest waiting line), ``/unsend <n>`` (the n-th listed) or ``/unsend all``: take back
        what nobody has read yet. ``(what happened, whether anything was taken back)``."""
        with self._typed_lock:
            waiting = list(self._waiting)
        if not waiting:
            return "nothing is waiting", False
        which = which.strip().lower()
        if which == "all":
            chosen = waiting
        elif not which:
            chosen = waiting[-1:]
        elif which.isdigit() and 1 <= int(which) <= len(waiting):
            chosen = [waiting[int(which) - 1]]
        elif which.isdigit():
            return f"there is no message {which}: {len(waiting)} waiting", False
        else:
            return "use /unsend, /unsend <number> or /unsend all", False
        taken = [entry["text"] for entry in chosen if self._take_back(entry)]
        if not taken:
            return "already read: it cannot be taken back", False
        if len(taken) == 1:
            return f"unsent: {self._safe(taken[0], 60)}", True
        return f"unsent {len(taken)} messages", True

    def _take_back(self, entry: Dict[str, Any]) -> bool:
        if entry.get("for") == "recording":
            return self.cancel_live_voice()
        with self._typed_lock:
            if not any(waiting is entry for waiting in self._waiting):
                return False                          # read (or run) meanwhile
            if entry.get("for") == "team":
                withdraw = entry.get("withdraw")
                if withdraw is None or not withdraw():
                    return False
            elif entry.get("for") in ("agent", "later"):
                if entry["text"] not in self._typed:
                    return False
                self._typed.remove(entry["text"])
            else:                                     # being written out (/voice): it lands in the line soon
                return False
            self._waiting = [waiting for waiting in self._waiting if waiting is not entry]
        return True

    WAITING_FOR = {"agent": "waiting for the agent's next step", "team": "waiting for a helper's next step",
                   "later": "runs when the agent is done",
                   "recording": "● recording… press Enter to stop", "transcribing": "writing out what you said"}

    def _waiting_shown(self) -> List[Tuple[str, str]]:
        """What the spinner draws under itself: each message typed while it works that nobody has read yet."""
        with self._typed_lock:
            return [(entry["text"], self.WAITING_FOR.get(entry["for"], "waiting")) for entry in self._waiting]

    def _read_by(self, entry: Dict[str, str], who: str) -> None:
        """``entry`` was read (by a helper, or by the agent): it leaves the waiting lines and is printed once, with
        who read it. Later reads of the same note (another helper) print nothing."""
        with self._typed_lock:
            if not any(waiting is entry for waiting in self._waiting):
                return
            self._waiting = [waiting for waiting in self._waiting if waiting is not entry]
        self._echo(entry["text"], f"read by {who}")

    def _echo(self, text: str, outcome: str, done: bool = True) -> None:
        """A typed line in the scrollback: ``❯ text  ✓ outcome`` (``✗`` when it could not be done)."""
        echo = Text("❯ ", style="bold")
        echo.append(plain(text, keep_newlines=True))
        echo.append(f"  {'✓' if done else '✗'} {plain(outcome)}", style="dim")
        self.console.print(echo)

    def _read_by_agent(self, text: str) -> None:
        """A message the agent just read (its ``interjection`` event)."""
        with self._typed_lock:
            entry = next((waiting for waiting in self._waiting
                          if waiting["for"] == "agent" and waiting["text"] == text), None)
        if entry is not None:
            self._read_by(entry, "the agent")

    def _team_notes_to_agent(self) -> None:
        """When a team job ends, the notes no helper read go to the main agent in the job's result."""
        with self._typed_lock:
            left = [waiting for waiting in self._waiting if waiting["for"] == "team"]
        for entry in left:
            self._read_by(entry, "the agent")

    def _skip_step(self, which: str) -> str:
        if not which:
            return "Usage: /skip <step number or helper name>, while a team job runs."
        steps = list(self._team_plan)
        number = int(which) if which.isdigit() else None
        if number is not None and not 1 <= number <= len(steps):
            return f"No step {number}: the plan has {len(steps)} steps." if steps else "No team job is running."
        if number is None:
            wanted = which.lower()
            if not any(wanted in (str(step.get("agent", "")).lower(), str(step.get("agent", "")).split("@")[0].lower())
                       for step in steps):
                return f"No step run by {self._safe(which, 60)!r} in the plan." if steps else "No team job is running."
        if not skip_team_step(which):
            return "No team job is running: nothing to skip."
        shown = f"Step {number}" if number is not None else f"The steps of {self._safe(which, 60)}"
        return f"{shown} will be skipped if not started yet{SKIP_DONE}"

    def _take_typed(self) -> List[str]:
        """The messages typed for the agent since its last step (the main runner's ``interjections``)."""
        with self._typed_lock:
            messages = [text for text in self._typed if not text.startswith("/")]
            self._typed = [text for text in self._typed if text.startswith("/")]
        return messages

    def _next_typed(self) -> Optional[str]:
        """The first line typed during the last turn that is still waiting (a command, or a message the agent
        finished before reading), or None."""
        with self._typed_lock:
            if not self._typed:
                return None
            line = self._typed.pop(0)
            for index, waiting in enumerate(self._waiting):
                if waiting["text"] == line and waiting["for"] in ("agent", "later"):
                    del self._waiting[index]
                    break
            return line

    def _team_record(self, task: str) -> Any:
        """Where this team job's plan and every helper's whole report are written as they come: the project's
        ``.agentx/team`` (the agent can read it later, after the terminal closed say), or, for the home folder or
        a drive root, ``~/.agentx/team/<folder>``. None when nothing can be written."""
        try:
            if self._memory_on:
                return TeamRecord.for_project(self.cwd, task, redact=self._hide)
            record = TeamRecord(self.home / ".agentx" / "team" / sessions_dir(self.home, self.cwd).name, task,
                                redact=self._hide)
            return record if record.folder is not None else None
        except Exception:                             # a record must never stop a team job
            return None

    def _show_unanswered(self, completion: Any, answered: bool) -> None:
        """A run that ended without a final answer (the step limit, say) still shows what it has: its summary, and
        how to carry on. Before, the turn just ended with nothing on the screen."""
        if completion is None:
            return
        outcome = str(getattr(completion, "outcome", "done") or "done")
        content = str(getattr(completion, "content", "") or "")
        if not answered and content.strip():
            self.screen.show_event({"type": "final", "content": content})
        if outcome == "iteration_limit":
            self._stopped_at_step_limit = True
            limit = getattr(getattr(self.session, "runner", None), "max_iterations", None)
            steps = f" ({limit} steps)" if isinstance(limit, int) else ""
            self.notice(f"Stopped at the step limit{steps} before finishing. Type \"continue\" to carry on from "
                        "here; what was done so far is kept.")
        elif outcome not in ("done", "") and not answered:
            self.notice(f"The run ended without finishing (outcome: {self._safe(outcome, 40)}).")

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
                line = self._next_typed()
                if line is None:
                    line = self._read_chat()
                else:                                  # typed while it worked: now the next message
                    self._echo(line, "running it now" if line.startswith("/") else "sent now")
            except EOFError:
                break
            except KeyboardInterrupt:
                if self.cancel_live_voice():
                    self.notice("Recording cancelled.")
                    continue
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
            if self.recording():                       # /voice started while it worked: Enter stops it
                self.stop_live_voice(str(line).strip())
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
        if self.session is not None and self.session.history and self.session_path is not None:
            self.notice("To pick up this conversation later: agentx --continue (add --model <name> to switch model).")
        return self._exit_code

    def _failure(self, error: BaseException) -> str:
        if _out_of_credit(error):
            return (f"Your {provider_name(self.model_label)} account is out of credit: the API refused the request "
                    f"({type(error).__name__}).")
        return f"Something went wrong ({type(error).__name__}): {self._safe(error)}"

    def _keep_stopped_turn(self, title: str, text: str, events: List[dict], reason: str, last_said: str) -> None:
        """A turn that stopped before it finished (an error, Ctrl+C, the token limit): your message and a short
        record of what it did go into the conversation and the work log, so "continue" (on this model or after
        /model) picks up from there instead of starting over."""
        if self.session is None:
            return
        record = record_from_events(title, events)
        calls = []
        for event in events:
            if event.get("type") == "tool_call":
                args = event.get("args")
                subject = (args.get("path") or args.get("command") or args.get("task") or "") \
                    if isinstance(args, dict) else ""
                calls.append(" ".join(f"{event.get('name')} {subject}".split())[:120])
        lines = [f"[agentx] This turn stopped before it finished: {reason}. What it did before it stopped:"]
        if record.changed:
            lines.append("- Files changed: " + ", ".join(record.changed[:20]))
        if record.ran:
            lines.append("- Commands run: " + "; ".join(command[:160] for command in record.ran[:10]))
        if calls:
            lines.append(f"- Tool calls ({len(calls)}, the last ones): " + "; ".join(calls[-15:]))
        if last_said.strip():
            lines.append("- The last thing it said: " + " ".join(last_said.split())[:500])
        if not (record.changed or record.ran or calls):
            lines.append("- Nothing yet.")
        lines.append("Changes already made are on disk. If the user says continue, pick up from here: check the files "
                     "above before redoing anything.")
        note = self._hide("\n".join(lines))
        self.session.history = [*(self.session.history or []), {"role": "user", "content": text},
                                {"role": "assistant", "content": note}]
        self._stopped_turn = True
        record.answer = f"Stopped before it finished ({reason})." + (
            f" Last: {' '.join(last_said.split())[:200]}" if last_said.strip() else "")
        if self._memory_on:
            now = datetime.now()
            self._append_log(entry_text(record, now, self._hide))
            if self.project_memory is not None and worth_a_note(record):
                self.project_memory.write(record, now, self._hide)

    def send(self, text: str, log_title: Optional[str] = None) -> None:
        """One turn: summarise a very long conversation first, stream the events to the screen, then autosave
        and, for a finished turn only, add an entry to the work log (titled ``log_title`` when given, else the
        message). Ctrl+C cancels the turn."""
        assert self.session is not None
        self._team_jobs = 0                            # a new message may start new team jobs
        self._team_stalled = False
        events: List[dict] = []
        finished = False
        stopped = ""                                   # why the turn stopped before it finished, if it did
        last_said = ""
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
            if self._stopped_at_step_limit and _is_carry_on(text):
                sent = text + CARRY_ON_NOTE
            elif self._stopped_turn and _is_carry_on(text):
                sent = text + STOPPED_CARRY_ON_NOTE
            self._stopped_at_step_limit = False
            self._stopped_turn = False
            if self._recall_next:                      # the first message of a fresh conversation: add close notes
                self._recall_next = False
                if self.project_memory is not None:
                    sent = text + auto_block(self.project_memory, text)
            answered = False
            for event in self.session.stream(sent):
                self.progress.stop()                       # erase the spinner, then print
                kind = event.get("type") if isinstance(event, dict) else None
                if kind == "interjection":                 # a message typed while it worked reached the agent
                    self.screen.flush()
                    self._read_by_agent(str(event.get("content") or ""))
                    self.progress.start("Thinking")
                    continue
                if kind == "completion":
                    self._show_unanswered(event.get("completion"), answered)
                self.screen.show_event(event)
                answered = answered or kind == "final"
                if kind in ("tool_call", "tool_result", "final"):
                    events.append(event)
                if kind == "thought" and str(event.get("content") or "").strip():
                    last_said = str(event.get("content"))
                if kind == "tool_call":                    # an open look-around flow is drawn above the spinner
                    self.progress.start(f"Running {self._safe(event.get('name', ''), 40)}")
                elif kind not in ("final", "completion"):
                    self.progress.start("Thinking")
            self.screen.flush()
            finished = True
        except TokenLimitReached as stop:
            self.notice(f"Stopped: this message used {stop.used:,} tokens, past your limit of {stop.limit:,} a "
                        f"message, and wrapping up took more. {KEPT_SO_FAR} /limit shows or changes the limit.")
            stopped = f"it used {stop.used:,} tokens, past the limit of {stop.limit:,}"
        except KeyboardInterrupt:
            self.notice(f"Turn interrupted (Ctrl+C). {KEPT_SO_FAR}")
            stopped = "the user pressed Ctrl+C"
        except Exception as e:
            self.error(self._failure(e))
            self.notice(KEPT_SO_FAR + " " + (OUT_OF_CREDIT_NEXT if _out_of_credit(e) else ERROR_NEXT))
            stopped = (f"the {provider_name(self.model_label)} account is out of credit" if _out_of_credit(e)
                       else f"an error ({type(e).__name__})")
        finally:
            self.progress.stop()
            self.screen.flush()                       # a look-around flow left open by an error or Ctrl+C
            with self._limit_lock:
                self._turn_start = None
                self._wrapping_up = False
                self.gate.closed = None
            self.progress.stop()
            if stopped:
                try:
                    self._keep_stopped_turn(text if log_title is None else log_title, text, events, stopped,
                                            last_said)
                except Exception:              # keeping it is a help: a failure here must not end the app
                    logging.getLogger(__name__).warning("could not keep the stopped turn", exc_info=True)
            self.save()
            after = self._usage_totals()
            self._last_turn_usage = tuple(b - a for a, b in zip(usage_before, after))      # type: ignore[assignment]
        if finished:
            try:
                self._log_turn(text if log_title is None else log_title, events)
            except Exception:                  # the turn itself went well: a log failure must not end the app
                self._warn_log_failed()

    # -- /mcp -------------------------------------------------------------------

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

    def set_fullscreen(self, on: bool) -> None:
        """``/fullscreen on|off``: saved in ~/.agentx/settings.json; it applies the next time agentx starts."""
        _, message = write_user_fullscreen(self.home, on)
        now = "on" if self.fullscreen else "off"
        self.notice(f"Full-screen mode {'on' if on else 'off'} from the next start (it is {now} now). "
                    f"{self._safe(message, 300)}")

    def show_fullscreen(self) -> None:
        self.notice(f"Full-screen mode is {'on' if self.fullscreen else 'off'}. /fullscreen on keeps the header and "
                    "the input fixed while the conversation scrolls (PgUp / PgDn or the mouse wheel; Shift+drag to "
                    "select text); /fullscreen off goes back to the normal screen. Either applies from the next "
                    "start; agentx --fullscreen or --no-fullscreen picks for one run.")

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

    def save(self) -> None:
        if self.session is None or self.session_path is None or not self.session.history:
            return                                # nothing to keep: no empty session files
        try:
            self.session.save(self.session_path)
        except OSError as e:
            self.error(f"Could not save the session: {self._safe(e)}")

    # -- commands that need the app ------------------------------------------

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

    # -- team jobs ----------------------------------------------------------

    # -- skills ---------------------------------------------------------------

    def install_skills(self, link: str) -> None:
        """``/skills install [name ...] <link>``. Only the user can start this (no tool does), and nothing is saved
        until they have seen what was found and said yes. Names before a github.com link pick those skills only."""
        wanted: List[str] = []
        words = link.split()
        if len(words) > 1 and "github.com" in words[-1].lower() and "github.com" not in " ".join(words[:-1]).lower():
            link, wanted = words[-1], [w.strip(",").lower() for w in words[:-1] if w.strip(",")]
        self.notice("Downloading...")
        try:
            with open_source(link) as fetched:
                found, problems = discover(fetched.root, fetched.subpath)
                if found and wanted:
                    picked = [c for c in found if c.name.lower() in wanted]
                    if not picked:
                        self.error(f"No skill named {', '.join(self._safe(w, 40) for w in wanted)} there. It has: "
                                   + ", ".join(c.name for c in found) + ".")
                        return
                    found = picked
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
        self.reload_skills()
        self.notice("Installed: " + ", ".join(names) + ". " + ("They are" if len(names) != 1 else "It is")
                    + " ready in this conversation: " + ", ".join(f"/{name}" for name in names)
                    + " or ask for it by name.")

    def reload_skills(self, say: bool = False) -> None:
        """Give this conversation the skills on disk now (after an install or a removal, or ``/skills reload``):
        the runner is built again with the conversation kept, as ``/model`` does. The skill list sits in the system
        prompt, so the next request starts the prompt cache again once."""
        if self.session is None:
            return
        before = set(self._runner_skills)
        self.session.attach(self._make_runner(self.model))
        if say:
            added = sorted(set(self._runner_skills) - before)
            gone = sorted(before - set(self._runner_skills))
            parts = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {', '.join(gone)}"] if gone else [])
            self.notice("Skills reloaded: " + ("; ".join(parts) if parts else "no change") + ".")

    def pick_up_saved_keys(self) -> List[str]:
        """Add the API keys this run does not have but Windows has saved (``setx``, the Environment Variables
        dialog): a terminal opened before a key was saved does not pass it on. Says which ones, never the values."""
        missing = [name for name in KEY_NAMES if not str(self.env.get(name) or "").strip()]
        if not missing:
            return []
        try:
            saved = self._saved_key_reader() or {}
        except Exception:
            return []
        added = [name for name in missing if str(saved.get(name) or "").strip()]
        for name in added:
            self.env[name] = str(saved[name]).strip()
        if added:
            self.notice(f"Using {' and '.join(added)} from your saved Windows variables (this terminal was opened "
                        "before it was set).")
        return added

    def _start_team_job(self, task: str, why: str) -> str:
        """The ``start_team_job`` tool: run the team and hand the result text back to the main agent."""
        refusal = self._team_refusal()
        if refusal:
            return refusal
        if self.model is None:
            return "Refused: no model is available for a team job."
        self._team_jobs += 1
        self.progress.stop()
        self.pick_up_saved_keys()                 # a key saved in Windows since agentx started
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
                                step_tokens=step_token_share(self.token_limit), closed=lambda: self.gate.closed,
                                record=self._team_record(task))
        finally:
            self._team_running = []
            self._team_rows = {}
            self.progress.stop()
            self.screen.flush_helpers()                # what a helper did before an error or Ctrl+C
            self._team_notes_to_agent()
            self.progress.start("Thinking")
        # A job in which no step got anywhere would most likely fail the same way again: stop the batches.
        # (A failed job, one that ended without a result, and outcome "stuck" - no step finished - all count.)
        self._team_stalled = (not text.startswith("Team job finished (outcome: ")
                              or text.startswith("Team job finished (outcome: stuck"))
        return f"{text}\n(team job {self._team_jobs} of {MAX_TEAM_JOBS_PER_MESSAGE} for this message)"


# -- the command line -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentx", description="A terminal coding assistant built on agentx-dev.")
    parser.add_argument("--version", action="version", version=f"agentx {_version()}")
    parser.add_argument("--model", metavar="NAME", help="model to use, for example claude-opus-5-5 or gpt-6.1-sol")
    parser.add_argument("--max-steps", type=int, metavar="N", default=None,
                        help=f"steps the agent may take for one message (default {DEFAULT_MAX_STEPS}, or maxSteps "
                             "in the settings)")
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
    screen = parser.add_mutually_exclusive_group()
    screen.add_argument("--fullscreen", dest="fullscreen", action="store_true", default=None,
                        help="full-screen mode: the header and the input stay put while the conversation scrolls")
    screen.add_argument("--no-fullscreen", dest="fullscreen", action="store_false",
                        help="the normal screen, even when the settings turn full-screen mode on")
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
    home = Path.home()
    holder: List[Any] = []
    hooks = dict(extra_commands=lambda: holder[0].skill_entries() if holder else {},
                 toolbar=lambda: holder[0].status_bar() if holder else "",
                 cycle_mode=lambda: holder[0].cycle_mode() if holder else None)
    wanted = args.fullscreen if args.fullscreen is not None else read_user_fullscreen(home)
    if wanted:
        try:
            from agentx_dev.cli.fullscreen import FullScreen
            screen = FullScreen(history_file=home / ".agentx" / "history", commands=command_names(),
                                descriptions=command_descriptions(), **hooks)
        except Exception as e:
            print(f"agentx: full-screen mode could not start ({type(e).__name__}); using the normal screen.",
                  file=sys.stderr)
        else:
            def wire(app: "ChatApp") -> None:
                app.fullscreen = True
                app.screen.header = screen.set_header
                app.progress.drawn_by = screen.invalidate
                screen.progress = app.progress
                screen.on_typed = app.typed_while_working
                app.insert_input = screen.insert
                app.set_submit_empty = lambda on: setattr(screen, "submit_empty", on)

            return screen.run(lambda: _run(args, home, screen.console, screen.read, screen.read_short, screen.select,
                                           holder, wire))
    prompt = PromptInput(history_file=home / ".agentx" / "history", commands=command_names(),
                         descriptions=command_descriptions(), **hooks)
    def wire_prompt(app: "ChatApp") -> None:
        app.insert_input = getattr(prompt, "insert", None)     # /voice words that arrive after the turn

    return _run(args, home, Console(), prompt.read, prompt.read_short, prompt.select, holder, wire_prompt)


def _run(args: argparse.Namespace, home: Path, console: Console, read: Callable[..., str],
         read_short: Callable[..., str], select: Callable[..., Any], holder: List[Any],
         wire: Optional[Callable[["ChatApp"], None]] = None) -> int:
    """Everything after the screen is chosen: the trust question, the settings, the app, the loop."""
    try:
        cwd = Path.cwd().resolve()
        setup_logging(home)
        # Nothing in this folder (its settings, its AGENTX.md) is read until the user has said they trust it.
        # A folder trusted for the first time in this run gets its own settings file (``ChatApp.start``).
        was_trusted = is_trusted(cwd, home)
        if not ensure_trusted(cwd, home, console, select=select, read_line=read_short):
            print("agentx: this folder is not trusted, so nothing was started. Run agentx in a folder you trust.",
                  file=sys.stderr)
            return 1
        settings = load_settings(cwd, home)
        add_dirs = []
        for entry in [*settings.add_dirs, *args.add_dir]:
            path = Path(entry).expanduser()
            add_dirs.append(_resolved(path if path.is_absolute() else cwd / path))
        app = ChatApp(cwd=cwd, home=home, console=console, read_line=read, read_short=read_short,
                      select=select,
                      model_name=args.model or settings.model, mode=args.mode or settings.mode or "default",
                      allow=settings.allow, deny=settings.deny, add_dirs=add_dirs, problems=settings.problems,
                      project_allow=settings.project_allow, newly_trusted=not was_trusted,
                      model_source=None if args.model else settings.model_source,
                      team_models=settings.team_models, team_models_source=settings.team_models_source,
                      team_model_uses=settings.team_model_uses, token_limit=settings.token_limit,
                      max_steps=min(max(1, args.max_steps), MAX_MAX_STEPS) if args.max_steps else settings.max_steps)
        if wire is not None:
            wire(app)
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
