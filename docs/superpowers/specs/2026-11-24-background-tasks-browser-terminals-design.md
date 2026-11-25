# agentx: background tasks, a built-in browser and terminals

Date: 2026-11-24. Status: approved by the user ("i approve the design you give"), who then left the work to run
unattended; the calls marked *(builder's call)* were made without them and are listed for review.

## Intent

The user watches Claude Code run background tasks (a dev server that keeps going while it carries on), drive a
browser inside its own window (open a page, click, type, look) and use terminals, with a panel that shows all of it
and lets the user take over. They want agentx to work the same way, in its app window (`agentx --ui`) first and, where
it makes sense, in the terminal app too.

Today agentx has none of it:

- `run_shell` waits for each command and stops it after 5 minutes (`SHELL_TIMEOUT_SEC` in `agentx_dev/cli/tools.py`),
  so a dev server or a watcher is killed; the docs tell the user to work around it with `nohup ... &`.
- The only web tools are `web_fetch` and `web_search` (`agentx_dev/WebTools.py`): nothing opens or drives a page.
- The app window has no terminal and no panel for running work.

## Three parts, built in this order

1. **Background tasks**: agentx starts long-running commands that keep running; it reads their output later and
   stops them; the window lists them.
2. **The built-in browser**: agentx opens and drives web pages (Playwright, Chromium); the window shows the page live
   and the user can take over.
3. **Terminals**: the user opens shells in the window and types in them; agentx can read them and, with approval,
   type in them. A background task is shown the same way, so the two share one panel.

Order: background tasks first (the browser is most useful once a dev server can stay up to point it at), then the
browser, then terminals (which reuse the task machinery).

## Goals

- The agent can start a dev server, keep working, read its log, open the page in a browser, click through it, see a
  screenshot, and stop the server, without the user leaving the window.
- The user sees all of it in one right-hand panel of the app window with three tabs, **Tasks**, **Browser** and
  **Terminal**, and can stop a task, look at a page and drive it, and type into a terminal.
- Everything the agent does through these tools goes through the existing approval gate with the same rules as
  `run_shell` and the web tools: the modes, allow and deny rules, "don't ask again" grants and plan mode work as they
  do today.

## Non-goals

- A full terminal emulator (cursor control, full-screen programs such as `vim` or `top`, colours beyond stripping
  escape codes). v1 terminals are pipe-based: line-oriented programs work; full-screen ones do not. A PTY needs
  `pywinpty` on Windows, which is not installed; *(builder's call)* not added now.
- Team helpers using these tools. Only the main agent gets them in v1.
- Using the user's own browser, profile, cookies or saved logins. The browser is a fresh, private profile.
- Installing Playwright or Chromium automatically.
- Persisting tasks, terminals or browser state across agentx restarts. Everything ends when agentx ends.
- Subagent rows in the Tasks panel (Claude Code shows those; agentx's team panel already covers team jobs).

## Anti-goals (what would make this a failure even if it works)

- A way around the gate: a command that `run_shell` would refuse or ask about must not run silently as a
  background task or as terminal input. Deny rules written for `run_shell` (including the default rules that protect
  the skills folders) must block the same command here.
- A way around the sandbox: the browser must not read local files (`file://`) or download files to disk.
- Orphaned processes: a dev server left running after agentx exits, or after the user presses Stop.
- A remote-control device (a phone opened with `/remote-control`) getting a raw shell it did not have before.
- Web text steering the agent: everything read from a page carries the same `WEB_CAUTION` marker as `web_fetch`.

## Constraints

- Python only on the server side; the page is the existing vanilla JS app (`agentx_dev/resources/webui/app.js`,
  `style.css`, `index.html`). No new frontend build step.
- Windows first (the user's machine), and macOS and Linux must keep working.
- Playwright is optional: a new extra `browser = ["playwright>=1.40"]` in `pyproject.toml`. Without it (or without its
  Chromium) the browser tools are still listed and answer with how to install
  (`pip install agentx-dev[browser]` then `python -m playwright install chromium`); nothing is installed for the user.
- Playwright's sync API is not thread-safe: every browser call, from the agent or from the window, runs on one
  dedicated thread.
- The existing test suite (about 4,000 tests) must stay green; new code gets tests, including real-process tests for
  tasks and a real-Chromium test for the browser that is skipped when Playwright or Chromium is missing.

## Approaches considered

**Background tasks**

- *A. A process manager inside agentx with its own tools* (chosen). New tools `start_background`,
  `background_output`, `stop_background`, `list_background`. Each task is a child process with its output written to
  a log file; agentx kills the process tree on Stop and at exit. The window and `/tasks` read the same manager.
- *B. Let `run_shell` take `background: true`.* One tool fewer, but it changes a library tool every agentx user
  depends on, mixes two very different result shapes, and the model would still need tools to read and stop.
- *C. Keep `nohup` and add only a viewer.* No control over the process (no reliable stop, orphans on exit), and the
  `nohup` / `&` forms are compound commands that always ask. Rejected.

**Browser**

- *A. Playwright driven from Python, a live screenshot stream to the window* (chosen). One headless Chromium per
  agentx, one page at a time (tabs left for later), the page shown in the window as frequently refreshed JPEG frames,
  and the user's clicks and keys on the frame sent back to the page. Works in the window, the terminal app (headless,
  the agent sees screenshots) and remote control.
- *B. An `<iframe>` of the page inside the window.* Most sites refuse to be framed, the agent could not drive it from
  Python, and a cross-origin frame cannot be read. Rejected.
- *C. Drive the user's own Chrome over CDP.* Uses their logins and cookies, which is the risk the anti-goals exclude.
  Rejected.

**Terminals**

- *A. Pipe-based shells sharing the task manager* (chosen). A terminal is a task whose command is the user's shell
  (PowerShell on Windows, `$SHELL` or `/bin/sh` elsewhere), with stdin open. The window shows its output and an input
  line. Line-oriented use works everywhere with no new dependency.
- *B. A PTY with xterm.js.* Real terminal behaviour, but needs `pywinpty` on Windows plus a vendored xterm.js; left for
  later.

## Design

### 1. Background tasks (`agentx_dev/cli/background.py`, new)

A `TaskManager` per agentx process (shared by the main conversation and its lanes; one per `WebFront` host),
holding `Task` records:

- `id`: short, readable, unique in the run (`t1`, `t2`, ...; terminals `term1`, ...).
- `kind`: `"task"` (an agent started it) or `"terminal"` (the user opened it; the agent cannot open one in v1).
- `owner`: the conversation that started a task (its session id), `""` for a terminal.
- `command`, `cwd`, `started_at`, `ended_at`, `exit_code`, `state` (`running`, `exited`, `stopped`, `failed`).
- `log`: a file at `~/.agentx/tasks/<project-slug>/<run-id>/<id>.log` (outside the project, so the repo stays
  clean). stdout and stderr are both written to it, in arrival order. *(builder's call)* At 5 MB the log keeps its first
  64 KB and drops the oldest of the rest, so a chatty server cannot fill the disk.

Starting: the command runs through the same shell `run_shell` uses (`DefaultTools`' `_build_child_env` for the
environment, with agentx's key-hiding), in the project folder or a `cwd` inside the sandbox, in its own process group
(`CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session` elsewhere) so the whole tree can be stopped. stdin is a
pipe for terminals and closed for tasks.

Stopping: kill the process tree (reuse `_kill_tree` from `DefaultTools`); state becomes `stopped`. At agentx exit
every running task and terminal is stopped (an `atexit` hook plus the app's own shutdown path). A task that exits on
its own records its exit code.

Limits *(builder's call)*: at most 8 running tasks and 4 terminals per agentx; starting one more is refused with a
message naming the running ones.

Tools (main agent only, all behind `gate_tool`):

| Tool | Arguments | Gate kind | Result |
|---|---|---|---|
| `start_background` | `command`, `cwd` (optional), `name` (optional, a label) | `command`, matched as `run_shell` | the id, and the first output after about 2 seconds (so a server that fails at once says so) |
| `background_output` | `id`, `lines` (default 80, at most 400) | `read` | state, exit code, and the newest lines (escape codes stripped, at most `OUTPUT_MAX_CHARS`) |
| `stop_background` | `id` | `read` *(it only ends agentx's own processes)* | what was stopped |
| `list_background` | none | `read` | every task and terminal: id, kind, state, command, age |
| `send_to_terminal` | `id`, `text` | `command`, matched as `run_shell` with `text` as the command | what was sent, then the new output after about 1 second |

"Matched as `run_shell`" means the gate uses `run_shell`'s subject, rules and grants for these calls: a deny rule
`run_shell:rm *` refuses `start_background rm -rf x`, an allow rule or a "don't ask again" grant for `npm run` covers
`start_background npm run dev`, a compound command always asks, auto mode runs it without asking as it does
`run_shell`, plan mode refuses it. The approval card shows "Start in the background: `npm run dev`".

The system prompt (`agentx_dev/cli/context.py`) gets two lines: use `start_background` for servers, watchers and
anything that does not end; never `run_shell` with `&` or `nohup` for that; stop what you started when you are done
with it, unless the user wants it kept running.

Terminal app: `/tasks` lists tasks and terminals; `/tasks show <id>` prints the newest lines; `/tasks stop <id>`
stops one (`/tasks stop all`). `/help` and the command menu list it.

### 2. The built-in browser (`agentx_dev/cli/browser.py`, new)

A `BrowserHost` per agentx process with one worker thread that owns Playwright. It starts Chromium lazily, on the
first browser call, headless, with a new temporary profile (`launch()` plus `new_context(accept_downloads=False,
viewport 1280x800)`), and closes it at agentx exit.

Safety:

- Only `http`, `https` and `about:blank` are opened. `file:`, `chrome:`, `data:`, `javascript:` and every other scheme
  are refused, and a route handler aborts any request (a link, a redirect, a frame) to a refused scheme.
- Downloads are refused (`accept_downloads=False`), and a page's file chooser is cancelled.
- Every text result starts with `WEB_CAUTION`.
- *(builder's call)* No credential or payment heuristics beyond that; the profile is empty, so the agent is never
  signed in anywhere unless the user signs in themselves while driving.

Tools (main agent only, behind `gate_tool`):

| Tool | Arguments | Gate kind | Result |
|---|---|---|---|
| `browser_open` | `url` | `web` (subject: the URL) | title, final URL and the page snapshot |
| `browser_click` | `target` | `web` (subject: the page URL and the target) | the snapshot after the click settles |
| `browser_type` | `target`, `text`, `submit` (optional, presses Enter) | `web` | the snapshot after |
| `browser_press` | `key` (Enter, Escape, Tab, ArrowDown...) | `web` | the snapshot after |
| `browser_back` | none | `web` | the snapshot after |
| `browser_read` | none | `read` | the snapshot of the current page |
| `browser_screenshot` | `full_page` (optional) | `read` | a picture of the page (agentx's `picture_result`), for models that see pictures |
| `browser_console` | none | `read` | the newest console messages and page errors (at most 50) |

The **snapshot** is the page's visible text (at most `WEB_MAX_CHARS`) plus a numbered list of its interactive elements
(links, buttons, inputs, selects) with their role, name and value, each with a ref (`e12`). A `target` is a ref from
the latest snapshot, or else a CSS selector, or else visible text; refs are tagged on the elements with a
`data-agentx-ref` attribute when the snapshot is taken.

Gate kinds: everything that can send data to a site (open, click, type, press, back) is `web`, so it asks in the
careful modes exactly like `web_fetch` (and the existing "all web searches and page fetches this session" grant
covers it, worded to include browser actions); reading what is already on screen is `read`.

**The user takes over.** The window's Browser tab shows the page as JPEG frames, refreshed while the tab is visible
(about 3 per second, only when the page changed) and after each action. A **Take over** button gives the user the
page: clicks on the frame become clicks at the same point on the page, the wheel scrolls it, keys typed while the
frame has focus are sent to it, and an address bar and Back/Reload buttons drive it. While the user has it, the
agent's browser tools answer "The user is using the browser right now; wait or ask them" (reads still work).
**Give back** returns it. The address bar's navigation goes through the same scheme check; the user's own
actions do not ask the gate (they are the user's). *(builder's call)* A remote-control device may watch the browser
but not take it over, matching the terminal rule below.

Without Playwright or Chromium: the tools answer with the install line; the Browser tab says the same.

### 3. Terminals

The user opens one with **New terminal** in the Terminal tab: a task of kind `terminal` running the user's shell
(PowerShell with `-NoLogo` on Windows, `$SHELL -i` or `/bin/sh` elsewhere) in the project folder, environment as for
tasks. The tab shows its output (escape codes stripped, the newest 2,000 lines, auto-scrolling unless the user
scrolled up) and an input line; Enter sends the line plus a newline to its stdin. Ctrl+C in the input stops the running
command (a Ctrl+Break / SIGINT to the process group) without closing the terminal; ✕ closes it.

The agent sees terminals in `list_background`, reads them with `background_output`, and types into one with
`send_to_terminal` (gated as a command, see the table). The user's own typing is not gated.

*(builder's call)* Terminals, and taking over the browser, are only for the window on the computer agentx runs on: a
remote-control device sees the Terminal tab read-only and cannot open, type into or close a terminal. Today a remote
device can only approve or refuse the agent's commands, not run its own, and this keeps it that way.

### The panel in the window

A **Tasks** button in the top bar (next to Details) opens a right-hand panel, the same kind of resizable side panel
as Details (they share the right edge: opening one closes the other). It has three tabs:

- **Tasks**: Running and Finished groups, like Claude Code's. A row shows the name or command, the time it has run,
  and Stop. A click opens its output (live, the newest lines). A finished one shows its exit code; Clear finished
  removes the finished rows.
- **Browser**: the address bar, Back, Reload, Take over / Give back, and the live frame. Empty state: "agentx has not
  opened a page" with an address bar to open one yourself.
- **Terminal**: one sub-tab per terminal, New terminal, the output and the input line.

The button shows a count of running tasks and terminals. The panel's tab and width are remembered per window
(localStorage). On a narrow window the panel covers the chat, as Details does.

Server side: new endpoints on the existing server (`agentx_dev/cli/webui.py`), token-checked like the others:
`GET /api/tasks`, `GET /api/tasks/<id>/output?after=<offset>`, `POST /api/tasks/<id>/stop`,
`POST /api/terminals` (open), `POST /api/terminals/<id>/input`, `POST /api/terminals/<id>/interrupt`,
`GET /api/browser/state`, `GET /api/browser/frame` (JPEG; `304`-style "unchanged" via a frame counter),
`POST /api/browser/take`, `POST /api/browser/give`, `POST /api/browser/input` (click, wheel, key, text, navigate,
back, reload). Status events carry the running count so the button updates without polling.

## Decisions made after the builder check *(all builder's calls, made while the user was away)*

1. **Which hosts the browser may reach.** `web_fetch` blocks every non-public address (its SSRF guard in
   `agentx_dev/WebTools.py`). The browser allows **public addresses and loopback** (`localhost`, `127.0.0.0/8`, `::1`),
   so it can open the local dev server; it refuses private ranges (10/8, 172.16/12, 192.168/16, fc00::/7), link-local
   (169.254/16, fe80::/10, which includes the cloud metadata address) and anything else non-public. The check runs on
   every request the page makes (a route handler on the context: navigations, redirects, frames, fetches), by host
   after DNS resolution, reusing `WebTools`' address classification, and the same check applies to the user's own
   address bar. A refused navigation says why.
2. **"The window on this computer".** The server listens on 127.0.0.1 only, so without remote control every window is
   on this computer or reached through a channel (an SSH tunnel) whose user already has a shell. So: **while remote
   control is on (`WebFront.remote`), terminals and browser take-over are off for every window**; the Terminal tab is
   read-only and says why; the agent's own tools still work. Without remote control they are on.
3. **What conversations share.** One `TaskManager` per agentx process (all conversations and lanes). Every agent can
   list and read every task and terminal. An agent may **stop a task its own conversation started without asking**;
   stopping another conversation's task or a user's terminal asks (gated as a command; auto mode runs it). The calling
   conversation's own gate decides every call. **The browser is one Chromium per process with one page (and context)
   per conversation**, so two working conversations never move each other's page or refs. The Browser tab shows the
   page of the conversation on screen; the Tasks tab shows every task, each with the title of the conversation that
   started it.
4. **Plan mode and rules for the browser.** `browser_open` is a fetch: kind `web`, allowed in plan mode as `web_fetch`
   is, and `web_fetch` allow and deny rules apply to it (matched as `web_fetch` with the URL as subject). Acting on a
   page (`browser_click`, `browser_type`, `browser_press`, `browser_back`) is a new kind, `browse`: it asks when `web`
   asks, the web grant covers it, and **plan mode refuses it** (a click can submit a form).
5. **Stop.** The chat's Stop / Esc interrupts the turn only; background tasks keep running (a dev server must survive an
   interrupted turn). A task ends with its own Stop (panel, `/tasks stop`, `stop_background`), when it exits, or when
   agentx exits.
6. **The agent cannot open a terminal** in v1; only the user can. It can read terminals and, gated, type into them.
7. **Stopping a user's terminal** asks (see 3).
8. **Local models.** The task tools (`start_background`, `background_output`, `stop_background`, `list_background`,
   `send_to_terminal`) join `LEAN_TOOLS`; the browser tools stay out with the web tools.

## Left to the builder

- Module layout within the files named above, class and helper names, and how the gate is told to treat the new tools
  as `run_shell` (a mapping in `approval.py` is expected).
- Exact frame rate, JPEG quality, and the snapshot format, within the limits above.
- How the panel's markup and styles fit the existing ones (reuse the Details panel's styles and the existing tokens).
- Documentation: `docs/guides/cli.md` (a section per part, the `/tasks` command, the new tools), the CHANGELOG
  `[Unreleased]` entry, `host/data.js` rebuilt with `python host/build_data.py`, and the troubleshooting entry for a
  missing Playwright.
