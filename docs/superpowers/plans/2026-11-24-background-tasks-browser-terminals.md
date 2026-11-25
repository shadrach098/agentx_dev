# Background tasks, built-in browser and terminals: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** agentx can run long-running commands in the background, drive a private Chromium page, and share
terminals with the user, all visible and controllable from a right-hand panel in the app window.

**Architecture:** Two process-wide services, `TaskManager` (`agentx_dev/cli/background.py`) and `BrowserHost`
(`agentx_dev/cli/browser.py`), shared by every conversation of one agentx. Each conversation's `ChatApp` gets gated
tools bound to its own session id as the owner. `WebFront` exposes both services to the page over new token-checked
endpoints; the page gets a Tasks / Browser / Terminal panel.

**Tech Stack:** Python 3.9+ stdlib `subprocess` / `threading`; Playwright sync API (optional extra `browser`);
vanilla JS/CSS in `agentx_dev/resources/webui/`; pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-background-tasks-browser-terminals-design.md` (read it first, including
"Decisions made after the builder check", which overrides earlier lines).

## Global Constraints

- Never weaken the gate: `start_background` and `send_to_terminal` are judged exactly as `run_shell` with the same
  command text (deny rules, default skills-folder deny rules, allow rules, grants, compound check, modes).
- Browser: only `http`, `https`, `about:blank`; hosts public or loopback only; `accept_downloads=False`; every text
  result starts with `WEB_CAUTION` (from `agentx_dev/cli/tools.py`).
- Limits: 8 running tasks, 4 terminals per process; task log at most 5 MB (keep the first 64 KB, drop the oldest of
  the rest); `background_output` `lines` default 80, max 400; output capped at `OUTPUT_MAX_CHARS`.
- Logs live in `~/.agentx/tasks/<project_slug(cwd)>/<run-id>/<id>.log` (`project_slug` from
  `agentx_dev/cli/sessions.py`).
- Chat Stop/Esc never stops background tasks. agentx exit stops every task, terminal and the browser.
- While `WebFront.remote` is on: no terminals opened, typed into or closed from any window, and no browser take-over.
- Playwright is optional: `browser = ["playwright>=1.40"]` extra; missing → tools answer
  `The browser needs Playwright: pip install "agentx-dev[browser]" then python -m playwright install chromium`.
- Commit messages never carry a `Co-Authored-By` line. Do not push. Do not touch the main checkout.
- Tests: `python -m pytest -q -p no:cacheprovider` (no `-n`); the full suite must pass before the last commit.

## Review Focus

1. A server command that prints nothing and never exits: `start_background` must return within ~2 s with the id and
   "no output yet", not block. (Task 2 test `test_start_returns_while_it_runs`.)
2. A command whose process tree spawns children (`npm run dev` → node): Stop and agentx exit must end the children
   too. (Task 2 test `test_stop_ends_the_whole_tree`.)
3. Non-UTF-8 or escape-coded output (coloured logs, `\r` progress bars): output shown stripped of ANSI codes and
   decoded with `errors="replace"`, never crashing the reader. (Task 2 test `test_escape_codes_and_bad_bytes`.)
4. A page that redirects to `http://169.254.169.254/` or loads a `file://` frame: refused, and the page reports it.
   (Task 4 test `test_private_and_file_targets_are_refused`.)
5. Two conversations using the browser at once keep separate pages and refs. (Task 4 test
   `test_each_owner_has_its_own_page`.)

---

### Task 1: The gate knows the new tools

**Files:**
- Modify: `agentx_dev/cli/approval.py` (`TOOL_KINDS`, `subject_of`, `subjects_of`, `_is_compound`, `Gate.check`,
  `_granted`, `_grant_for`, `_remember`, `WEB_GRANT`)
- Modify: `agentx_dev/cli/preview.py`, `agentx_dev/cli/ui_text.py` (labels for the new tools in approval cards and
  step rows: "Start in the background", "Type in terminal", "Open page", "Click", "Type", "Press", "Go back",
  "Read page", "Screenshot", "Console", "Tasks", "Task output", "Stop task")
- Test: `tests/test_gate_new_tools.py`

**Interfaces:**
- Produces: `TOOL_KINDS` entries: `start_background`, `send_to_terminal`, `stop_other_task` → `"command"`;
  `background_output`, `list_background`, `stop_background`, `browser_read`, `browser_screenshot`,
  `browser_console` → `"read"`; `browser_open` → `"web"`; `browser_click`, `browser_type`, `browser_press`,
  `browser_back` → `"browse"`.
- Produces: `RULE_AS = {"start_background": "run_shell", "send_to_terminal": "run_shell", "browser_open": "web_fetch"}`
  and `rule_view(tool, args) -> Tuple[str, Dict]` returning the tool name and args the rules/grants see
  (`send_to_terminal`'s `text` becomes `command`; `stop_other_task` keeps `command`). `Gate.check` uses it for
  subjects, deny/allow matching, compound check and grants; the `Request` still names the real tool.
- Produces: `WEB_GRANT = "all web searches, page fetches and browser actions this session"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_gate_new_tools.py` (build a `Gate` as the existing gate
  tests do, with a recording `ask`):
  - `test_shell_deny_rule_blocks_background_and_terminal_input`: deny `run_shell:rm *` →
    `check("start_background", {"command": "rm -rf x"})` and `check("send_to_terminal", {"id": "term1", "text": "rm x"})`
    both return the `Refused: the settings deny rule 'run_shell:rm *' blocks this.` text, no ask.
  - `test_default_skills_rules_cover_background`: with `DEFAULT_DENY` from `agentx_dev/cli/app.py`,
    `start_background` of `cat ~/.agentx/skills/x` is refused.
  - `test_allow_rule_and_grant_for_run_shell_cover_background`: allow `run_shell:npm run *` → no ask for
    `start_background npm run dev`; an "a" answer to `start_background npm test` then covers `run_shell npm test`.
  - `test_compound_background_always_asks`: allow `run_shell:npm *`, `start_background "npm run dev & rm x"` asks.
  - `test_plan_mode`: plan refuses `start_background`, `send_to_terminal`, `browser_click`; allows `browser_open`,
    `browser_read`, `background_output`.
  - `test_auto_mode_runs_commands_and_browse`: auto → no ask for `start_background`, `browser_click`,
    `stop_other_task`.
  - `test_browse_asks_like_web_and_web_grant_covers_it`: default mode asks for `browser_click`; after an "a" on a
    `web_fetch`, `browser_type` does not ask; `web_fetch:*evil*` deny refuses `browser_open https://evil.com`.
- [ ] **Step 2: Run them**: `python -m pytest tests/test_gate_new_tools.py -q -p no:cacheprovider` → FAIL.
- [ ] **Step 3: Implement** the mapping in `approval.py`: `kind == "browse"` behaves as `"web"` everywhere except
  plan mode (refused); `_remember` for `browse` sets `web_allowed`; `subject_of` for `browser_*` acting tools is
  `"<url> :: <target or key or text>"` passed in by the tool via an `url` argument the tool layer fills (see Task 5:
  the gated function receives `page_url` injected before the gate; keep `subject_of` reading `args.get("page_url")`).
  Add the labels to `preview.py` / `ui_text.py`.
- [ ] **Step 4: Run** the new tests and `tests/test_cli_approval*.py tests/test_cli_gate*.py` (whatever the existing
  gate test files are named: `grep -l "Gate(" tests/`) → PASS.
- [ ] **Step 5: Commit** `feat(agentx): the gate judges background commands as run_shell and browser actions as web`.

### Task 2: The task manager

**Files:**
- Create: `agentx_dev/cli/background.py`
- Test: `tests/test_background_tasks.py`

**Interfaces:**
- Produces:
  - `@dataclass Task`: `id: str, kind: str ("task"|"terminal"), command: str, cwd: str, owner: str, name: str,
    started_at: float, ended_at: Optional[float], exit_code: Optional[int], state: str
    ("running"|"exited"|"stopped"|"failed"), log: Path`.
  - `class TaskManager(home: Path, cwd: Path, env: Optional[dict] = None, max_tasks: int = 8, max_terminals: int = 4)`
    with `start(command: str, *, cwd: Optional[Path] = None, owner: str = "", name: str = "", kind: str = "task") ->
    Task` (raises `TaskLimit(str)` with the running ids named), `open_terminal() -> Task`,
    `output(id: str, lines: int = 80) -> str` (stripped, decoded), `read_from(id: str, offset: int) -> Tuple[str, int]`
    (for the window: text after a byte offset, and the new offset), `send(id: str, text: str) -> None` (terminal
    only; writes `text + "\n"`, and appends `$ text` to the log), `interrupt(id: str) -> None`,
    `stop(id: str) -> Task`, `stop_all() -> None`, `get(id) -> Optional[Task]`, `list() -> List[Task]`,
    `clear_finished() -> int`, `running_count() -> int`.
  - `def shared(home: Path, cwd: Path) -> TaskManager`: the process-wide manager for that project (created once,
    `atexit.register(stop_all)`).
  - `def strip_codes(text: str) -> str`: ANSI/OSC escapes removed, `\r`-overwritten progress lines collapsed to the
    last state.
  - `TERMINAL_ARGV`: Windows `["powershell", "-NoLogo", "-NoProfile", "-Command", "-"]`; elsewhere
    `[os.environ.get("SHELL") or "/bin/sh"]`.
- Consumes: `_resolve_shell_interpreter`, `_build_child_env`, `_kill_tree` from `agentx_dev/DefaultTools.py`;
  `project_slug` from `agentx_dev/cli/sessions.py`. Environment: `_build_child_env(Permissions(...))` as `run_shell`
  gets it.

- [ ] **Step 1: Write the failing tests** (real processes; use `sys.executable -c` commands so they run everywhere):
  - `test_start_returns_while_it_runs`: start `python -c "import time; time.sleep(60)"` → returns in < 3 s, state
    `running`; `stop` → `stopped` within 5 s.
  - `test_output_is_kept_and_read_back`: a command printing `hello` then exiting 3 → after it ends, `output` holds
    `hello`, state `exited`, `exit_code == 3`.
  - `test_stop_ends_the_whole_tree`: the command starts a child `python` that writes its pid to a file and sleeps;
    after `stop`, that pid is gone (`psutil` is not available: on Windows check with `tasklist /FI "PID eq n"`,
    elsewhere `os.kill(pid, 0)` raising).
  - `test_escape_codes_and_bad_bytes`: output with `\x1b[31mred\x1b[0m`, `50%\r100%` and byte `\xff` → `output`
    contains `red` and `100%`, not `50%`, and a replacement character.
  - `test_limits`: `max_tasks=2` → third start raises `TaskLimit` naming `t1` and `t2`.
  - `test_terminal_takes_input`: `open_terminal()`, `send(id, "echo hi-there")` → within 5 s `output` contains
    `hi-there` and the echoed `$ echo hi-there`.
  - `test_log_is_capped`: monkeypatch the cap to 10 KB / head 1 KB; a command printing 100 KB → log file under
    ~12 KB, starts with the first line printed, ends with the last.
  - `test_stop_all_at_exit`: two running tasks, `stop_all()` → both `stopped`.
- [ ] **Step 2: Run** → FAIL (module missing).
- [ ] **Step 3: Implement** `background.py`. One reader thread per task copies stdout+stderr (merged with
  `stderr=STDOUT`) into the log in binary; `creationflags=CREATE_NEW_PROCESS_GROUP` on Windows,
  `start_new_session=True` elsewhere; `interrupt` sends `CTRL_BREAK_EVENT` / `SIGINT` to the group. Decoding happens
  on read.
- [ ] **Step 4: Run** → PASS, three times in a row (process tests flake easily; fix any flake by waiting on state,
  never by sleeping longer).
- [ ] **Step 5: Commit** `feat(agentx): a task manager for commands that keep running`.

### Task 3: Task tools in the conversation, `/tasks`, and cleanup

**Files:**
- Create: `agentx_dev/cli/task_tools.py`
- Modify: `agentx_dev/cli/tools.py` (export `make_permissions(cwd, add_dirs) -> Permissions` used by `build_tools`;
  add the five task tool names to `LEAN_TOOLS`)
- Modify: `agentx_dev/cli/app.py` (create `self.tasks = shared(self.home, self.cwd)`; add the tools after
  `build_tools`; stop all at the app's final exit path; `device_app` lanes share the same manager)
- Modify: `agentx_dev/cli/commands.py` (`/tasks`, `/tasks show <id>`, `/tasks stop <id|all>` in `COMMANDS` and help)
- Modify: `agentx_dev/cli/context.py` (two instruction lines, wording in the spec, Design §1)
- Test: `tests/test_task_tools.py`

**Interfaces:**
- Consumes: Task 1's tool names and kinds; Task 2's `TaskManager`, `Task`, `TaskLimit`.
- Produces: `build_task_tools(manager: TaskManager, owner: Callable[[], str], permissions: Permissions, gate: Gate,
  titles: Callable[[str], str]) -> List[StructuredTool]` returning gated `start_background`, `background_output`,
  `stop_background`, `list_background`, `send_to_terminal`. `stop_background` of a task whose `owner` is not
  `owner()` (another conversation's task, or a terminal) first calls `gate.check("stop_other_task", {"command":
  f"stop {id}: {task.command}"})` under `approval_lock(gate)` and returns its refusal if any.
- Tool results (exact first lines):
  - start: `Started t1 in the background: <command>` then `Output so far:` and up to 40 lines, or
    `(no output yet)`; if it already exited: `It exited at once with code N:` and the output.
  - output: `t1 (running for 2m 10s): <command>` or `t1 (exited with code 0 after 12s): ...`, then the lines.
  - list: one line per task `t1  running  2m 10s  npm run dev  (started by: <title>)`; `No background tasks.` when
    none.
  - stop: `Stopped t1 (npm run dev).`; `t1 had already ended (exit code 0).`
  - send: `Sent to term1: <text>` then `Output since:` and the new lines after ~1 s.

- [ ] **Step 1: Write the failing tests** with a real `ChatApp` and `MockModel` (see `tests/conftest.py` and how
  `tests/test_typing_while_working.py` builds one), `mode="auto"` unless the test is about asking:
  - `test_the_agent_starts_reads_and_stops_a_task`: scripted model calls `start_background` with a sleeping python
    command, then `background_output`, then `stop_background` → the results contain `Started t1`, `running`,
    `Stopped t1`.
  - `test_chat_stop_leaves_tasks_running`: start a task, then interrupt the turn (raise `KeyboardInterrupt` in the
    model script) → the task is still `running`.
  - `test_stopping_another_conversations_task_asks`: two apps sharing one manager (`device_app`), default mode; app
    B's `stop_background t1` asks (recorded request `tool == "stop_other_task"`); answering "n" leaves it running.
  - `test_tasks_command`: `/tasks` lists `t1`; `/tasks stop t1` stops it; `/tasks stop all`.
  - `test_lean_tools_include_task_tools`: `lean_tools(app.tools)` names include all five task tools and no
    `browser_*`.
  - `test_exit_stops_everything`: after the app's exit path, `manager.running_count() == 0`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `task_tools.py` and the wiring.
- [ ] **Step 4: Run** the new tests plus `tests/test_typing_while_working.py tests/test_cli_*.py` → PASS.
- [ ] **Step 5: Commit** `feat(agentx): background tasks the agent starts, reads and stops; /tasks`.

### Task 4: The browser host

**Files:**
- Create: `agentx_dev/cli/browser.py`
- Modify: `pyproject.toml` (extra `browser = ["playwright>=1.40"]`; add `playwright` to the `dev` extra only if the
  test suite already depends on it elsewhere — check `grep -l playwright tests/`)
- Test: `tests/test_browser_host.py` (module-level skip when Playwright or Chromium cannot launch)

**Interfaces:**
- Produces:
  - `NOT_INSTALLED` (the install line from Global Constraints).
  - `def url_refusal(url: str) -> Optional[str]`: `None` when allowed; else the reason
    (`only http and https pages can be opened`, `<host> is a private address`...). Allowed: `about:blank`, http(s)
    whose host resolves only to public addresses (`WebTools._is_public`) or loopback.
  - `class BrowserHost(viewport=(1280, 800))`: one worker thread; every public method runs on it and returns its
    result (raises `BrowserUnavailable(NOT_INSTALLED)` when Playwright is missing).
    - `open(owner: str, url: str) -> Snapshot`, `click(owner, target: str) -> Snapshot`,
      `type(owner, target: str, text: str, submit: bool = False) -> Snapshot`, `press(owner, key: str) -> Snapshot`,
      `back(owner) -> Snapshot`, `snapshot(owner) -> Snapshot`, `screenshot(owner, full_page: bool = False) -> bytes`
      (PNG), `console(owner) -> List[str]`, `frame(owner) -> Tuple[int, Optional[bytes]]` (counter, JPEG of the
      viewport; counter rises when the page changed), `user_event(owner, event: dict) -> None` (`{"type": "click",
      "x", "y"}`, `wheel` with `dy`, `key` with `key`, `text` with `text`, `navigate` with `url`, `back`, `reload`),
      `take(owner)`, `give(owner)`, `held_by_user(owner) -> bool`, `state(owner) -> dict` (`{"open", "url",
      "title", "user": bool, "frame": int}`), `close()`.
  - `@dataclass Snapshot: url: str, title: str, text: str, elements: List[str]` with `def render() -> str` →
    `WEB_CAUTION` + `Page: <title>\nURL: <url>\n\n<text>\n\nInteractive elements:\n[e1] link "Docs" ...`.
  - `def shared_browser() -> BrowserHost` (process-wide, closed at exit).
- Owners get their own `BrowserContext` + page (`accept_downloads=False`, `context.route("**/*", ...)` aborting any
  request whose URL `url_refusal` refuses, `page.on("filechooser", lambda fc: ...)` cancelling).
- Target resolution in `click`/`type`: `e<n>` → `[data-agentx-ref="e<n>"]`; else, if it parses as a CSS selector and
  matches, that; else `page.get_by_text(target, exact=False).first`. A target matching nothing:
  `ERROR: nothing on the page matches '<target>'; read the page again for current refs`.

- [ ] **Step 1: Write the failing tests** against a local `http.server` on 127.0.0.1 serving a small form page:
  - `test_open_read_and_click`: open the page → snapshot text holds the heading, elements list holds `[e1]`…;
    `click("e<n of the link>")` → URL changed.
  - `test_type_and_submit`: `type(ref of input, "hello", submit=True)` → the result page shows `hello`.
  - `test_private_and_file_targets_are_refused`: `url_refusal("file:///C:/x")`, `("http://10.0.0.5/")`,
    `("http://169.254.169.254/")` are refusals; `("http://127.0.0.1:8000/")` and `("https://example.com/")` are not
    (example.com check skipped when there is no network); a page that redirects to `http://169.254.169.254/` ends
    on an error, not that page.
  - `test_each_owner_has_its_own_page`: owners `a` and `b` open different pages; `snapshot("a")` still shows a's.
  - `test_user_takes_over`: `take("a")` → `held_by_user("a")`; `give("a")` → not.
  - `test_frame_changes_after_navigation`: frame counter rises after `open`.
  - `test_missing_playwright`: monkeypatch the import to fail → `BrowserHost().open(...)` raises
    `BrowserUnavailable` whose message is `NOT_INSTALLED`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `browser.py` (sync Playwright on the worker thread; a `queue.Queue` of
  `(callable, future)`).
- [ ] **Step 4: Run** → PASS three times.
- [ ] **Step 5: Commit** `feat(agentx): a private browser agentx can drive (Playwright)`.

### Task 5: Browser tools in the conversation

**Files:**
- Create: `agentx_dev/cli/browser_tools.py`
- Modify: `agentx_dev/cli/app.py` (add the tools; close the browser at exit)
- Modify: `agentx_dev/cli/context.py` (one line: use the browser tools to check web pages and UIs you build; open the
  dev server's localhost address; read the page before clicking; never type passwords or payment details)
- Test: `tests/test_browser_tools.py` (skip like Task 4)

**Interfaces:**
- Consumes: Task 4's `BrowserHost`, `Snapshot`, `BrowserUnavailable`; Task 1's kinds and the `page_url` subject rule.
- Produces: `build_browser_tools(host: BrowserHost, owner: Callable[[], str], gate: Gate) -> List[StructuredTool]`:
  the eight tools of the spec's table. Each acting tool's gated function receives the current page URL as
  `page_url` (filled from `host.state(owner())["url"]` before the gate check, not a model argument: add it to the
  args dict passed to `gate.check` only). A tool called while `held_by_user` returns
  `The user is using the browser right now; wait or ask them.` (reads still work). `browser_screenshot` returns
  `picture_result` of a temporary PNG (deleted after reading).

- [ ] **Step 1: Write the failing tests**: scripted `MockModel` (auto mode) opens the local test page, reads it,
  clicks, types → results contain the snapshot text with `WEB_CAUTION`; a `file://` open returns the refusal; in
  default mode `browser_click` asks with `kind == "browse"`; while the user holds the page, `browser_click` returns
  the "user is using the browser" text and `browser_read` works.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(agentx): the agent opens, reads and drives pages in its own browser`.

### Task 6: Window endpoints

**Files:**
- Modify: `agentx_dev/cli/webui.py` (`WebFront`: `tasks_payload()`, `task_output(id, offset)`, `task_stop(id)`,
  `terminal_open()`, `terminal_input(id, text)`, `terminal_interrupt(id)`, `terminal_close(id)`,
  `browser_state()`, `browser_frame()`, `browser_take()`, `browser_give()`, `browser_input(event)`; routes as in the
  spec's "The panel in the window"; `_status()` gains `"tasks": running_count`)
- Test: `tests/test_window_tasks_endpoints.py`

**Interfaces:**
- Consumes: Tasks 2–5 (`shared(home, cwd)`, `shared_browser()`; the owner for the browser is the routed front's
  `conversation_id()`).
- Produces JSON shapes used by Task 7:
  - `GET /api/tasks` → `{"tasks": [{"id", "kind", "name", "command", "state", "exitCode", "startedAt", "endedAt",
    "owner", "ownerTitle"}], "terminalsAllowed": bool}`.
  - `GET /api/tasks/output?id=t1&after=0` → `{"text", "offset", "state"}`.
  - `POST /api/tasks/stop {"id"}`, `POST /api/tasks/clear` → `{"ok"}`.
  - `POST /api/terminals {}` → `{"ok", "id"}`; `POST /api/terminals/input {"id", "text"}`;
    `POST /api/terminals/interrupt {"id"}`; `POST /api/terminals/close {"id"}` → `{"ok"}` or `{"ok": false,
    "error"}`. All four refuse with `"terminals are off while remote control is on"` when `remote`.
  - `GET /api/browser/state` → `{"available", "open", "url", "title", "user", "frame", "takeAllowed", "error"}`.
  - `GET /api/browser/frame?since=<n>` → JPEG bytes with header `X-Frame: <n>`, or 204 when unchanged.
  - `POST /api/browser/take`, `/give`, `/input {event}` → `{"ok"}`; take/input refused while `remote`.
- [ ] **Step 1: Write the failing tests** with the `Host` helper from `tests/test_one_conversation_per_device.py`:
  list/stop/output round trip for a task started through the manager; terminal open/input/output; remote mode
  refuses terminals and take-over; `_status()["tasks"]` counts running ones; browser state reports `available:
  false` with the install line when Playwright import is monkeypatched away; with Playwright, open a page through
  the host and fetch a frame (JPEG magic bytes `\xff\xd8`).
- [ ] **Step 2–4:** run → fail → implement → pass.
- [ ] **Step 5: Commit** `feat(webui): endpoints for background tasks, terminals and the browser`.

### Task 7: The panel in the window

**Files:**
- Modify: `agentx_dev/resources/webui/index.html` (a Tasks button with a count badge in `header.top` next to
  `#detailsBtn`; an `<aside class="tasks-panel" id="tasksPanel" hidden>` beside `#details` with a tab list
  Tasks / Browser / Terminal and three bodies)
- Modify: `agentx_dev/resources/webui/app.js`, `agentx_dev/resources/webui/style.css`
- Test: `tests/test_tasks_panel_browser.py` (Playwright drives the real window, like the existing
  `tests/test_*_browser.py`; skipped without Playwright)

**Behaviour (from the spec, "The panel in the window"):** opening the panel closes Details and the reverse; the tab
and width are kept in `localStorage` (`agentx-tasks-tab`, `agentx-tasks-width`, wrapped in try/catch); Tasks tab
polls `/api/tasks` every 2 s while open and shows Running / Finished groups with Stop, a click opens a live output
view polling `/api/tasks/output` every second; Browser tab polls `/api/browser/state` every second and
`/api/browser/frame?since=` about 3 times a second while visible, shows the address bar, Back, Reload, Take over /
Give back; while taken, clicks/wheel/keys on the frame image are sent to `/api/browser/input` with coordinates scaled
from the displayed image to the 1280×800 viewport; Terminal tab lists terminals as sub-tabs with New terminal, output
(newest 2,000 lines, stays scrolled to the bottom unless scrolled up) and an input line (Enter sends, Ctrl+C
interrupts); read-only with a note when `terminalsAllowed` is false. The top button's badge shows
`status.tasks` when > 0. Narrow windows: the panel covers the chat, like Details.

- [ ] **Step 1: Write the failing browser test**: open the window, start a task through the manager, open the panel →
  the task row shows with Stop; Stop → it moves to Finished; Terminal tab → New terminal, type `echo panel-ok` →
  output shows `panel-ok`; Browser tab with a page opened through the host → an `<img>` frame appears.
- [ ] **Step 2–4:** fail → implement → pass. Then look at it in a real window (screenshots at 1280 and 600 px wide,
  light and dark) and fix overlap or overflow.
- [ ] **Step 5: Commit** `feat(webui): a Tasks / Browser / Terminal panel`.

### Task 8: Docs and release notes

**Files:** `docs/guides/cli.md` (a "Background tasks, the browser and terminals" section: the tools, `/tasks`, the
panel, the safety rules, the remote-control rule, the Playwright install), the `run_shell` limits paragraph (replace
the `nohup` advice with `start_background`), `docs/cookbook/troubleshooting.md` (the missing Playwright message),
`CHANGELOG.md` `[Unreleased]` `### Added`, `README.md` feature table row, `host/data.js` via
`python host/build_data.py`.

- [ ] **Step 1:** write the docs. **Step 2:** `python host/build_data.py`. **Step 3:** full suite
  `python -m pytest -q -p no:cacheprovider` → all pass. **Step 4: Commit** `docs: background tasks, the browser and
  terminals`.
