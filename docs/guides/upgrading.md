# Upgrading

What changes when you move to a new `agentx-dev` release, and what —
if anything — you need to do. Upgrade with:

```bash
pip install -U agentx-dev
```

If you work in a notebook, restart the kernel afterwards. The old
module stays imported until you do, which shows up as errors like
"unexpected keyword argument" for parameters the new release added.

---

## Upgrading to the release after 3.7.0 (skills)

**One install change and one new default; no removed API.**

- **Changed: `Claude()` defaults to `claude-opus-5-5`** (was `claude-sonnet-4-6`) **with `max_tokens=16000`** (was
  4096: Opus 5.5 always thinks, and thinking counts toward the limit). Code that passes `model=` is unaffected. To
  keep the old behaviour, pass `Claude(model="claude-sonnet-4-6", max_tokens=4096)`. Opus 5.5 costs $4 / $20 per
  million tokens in / out (Sonnet 4.6: $3 / $15). `agentx` with only an Anthropic key and no model set now uses it.
- **New:** `AgentRunner(..., log=True)` (or a file path, a `logging.Logger`, a function) logs each run step by
  step; the default `log=None` changes nothing. The eval harness runs async runners and Supervisors, has new checks
  (`outcome_is`, `output_field`, `used_skill`, `used_agent`, `subtasks_ok`, `max_tool_errors`), `repeat=`, `tags=`
  and `compare_reports`. Existing eval code runs unchanged; `EvalReport.summary()` gains a `counted:` line, and
  `tool_error_count` now counts failed tool calls (it was always 0).
- **Fixed for the newest Claude models:** Opus 5.5, Sonnet 5.5 and Fable 5.1 refuse a forced tool choice, which
  structured output (`output_schema`, the answer parser) used. On those models the call is now sent with
  `tool_choice: auto` and a line asking for the tool; other models that refuse it are found from the error, once.

- **Changed: a smaller base install.** `chromadb`, `qdrant-client`, `psycopg`, `pandas` and `openpyxl` are no
  longer installed by `pip install agentx-dev` (chromadb alone is hundreds of megabytes). If you use
  `ChromaVectorStore`, `QdrantVectorStore` or `PgVectorStore`, install `pip install -U "agentx-dev[rag]"`; for
  spreadsheets in `Media`, `[excel]`; `[all]` has everything. An existing environment keeps the packages it already
  has, so nothing breaks until you build a fresh one. A class whose package is missing says which extra to install.

- **New:** `AgentRunner(..., skills=True)` and `AsyncAgentRunner(..., skills=True)` (or `skills=Skills("./folder")`) give the agent a `use_skill` tool and a short list of skills at the end of its system prompt. The default, `skills=None`, changes nothing: no new tool, no prompt change, no new files. See the [Skills guide](skills.md).
- **New in `agentx`:** skills are on. The first time you start it after upgrading, it creates five skill folders in `~/.agentx/skills/`, and the agent gets one more tool, `use_skill`, which only reads. Its system prompt gains a short list of skills, so the first request after the upgrade is not served from a prompt cache built before it. `/skills` lists them, `/skills install <github link or folder>` installs more after showing you what it found, `/skills remove <name>` deletes one. Edit the created skills freely: they are never overwritten, and one you delete is not recreated.
- **New folder:** `~/.agentx/skills/` (and `~/.agentx/skills/.state.json`, the names of the default skills created so far). A project may also keep skills in `<project>/.agentx/skills/`; the terminal app shows them to the agent in a separate block marked as coming from the project, after you have trusted the folder.
- **If you give an agent write access to your home folder** (`Permissions.full_access(["~"])` and `skills=True`), keep `~/.agentx/skills` outside what its write tools can reach: a skill is instructions the agent follows.
- **New in `agentx`, smaller requests:** old tool results are sent to the model as a one-line stub (the newest stay whole, 20,000 to 40,000 characters, cut in steps so the provider's prompt cache keeps working; older skill texts become a short note once the conversation is long; `run_python` and `run_shell` output is cut at 30,000 characters), `read_path` returns at most 30,000 characters at a time with a note on how to read on, a skill already read is answered with a pointer back, and summarising starts at about 60,000 characters of what is sent (was 150,000 of the saved history). Your saved sessions are unchanged. `/usage` shows the tokens used.
- **New in `agentx`, project notes:** a note per worthwhile turn is written to `.agentx/notes/` and indexed in `.agentx/memory/`. With `OPENAI_API_KEY` set, note text is sent to OpenAI's embeddings API to build the index. The agent gets a `recall` tool, and `/memory` shows the notes. Add `.agentx/notes/` and `.agentx/memory/` to your `.gitignore` if you do not want them in the repository.
- **New in `agentx`, a token limit:** once a message has used 200,000 tokens, `agentx` asks `Keep going for up to 200,000 more? [y/N]`. A no wraps up: tool calls are refused from then on and the agent answers with what it has found; only if that runs long too is the turn stopped like Ctrl+C. `/limit <tokens>` changes it and saves it in `~/.agentx/settings.json` as `"tokenLimit"`; `/limit 0` turns it off. `/usage` now also shows how much input came from the provider's cache.
- **New in `agentx`, web tools:** the agent has `web_search` and `web_fetch`, and the `web-research` skill is listed. In `default` and `plan` mode each lookup asks first (answer `a` to allow them for the session); `accept-edits` and `auto` do not ask. Add a deny rule such as `"web_fetch:*"` to turn fetching off.
- **New in `agentx`, model requests:** a request waits up to 5 minutes (was 60 seconds, retried by the client 3 times, then by agentx 3 times: about 12 minutes for one stuck request). A timeout is now retried once at most, and every retry is shown.
- **New in `agentx`, MCP servers:** `/mcp add`, `/mcp remove`, `/mcp login`, `/mcp logout`. Servers are kept in `~/.agentx/mcp.json` and connected at startup (a sign-in is never started by itself). Sign-ins go to the system's credential store. The `mcp` extra now needs `mcp>=1.10,<2` (the 2.x line changed its client API and is not supported yet) and adds `keyring`: `pip install -U "agentx-dev[mcp]"`.
- **Changed in code:** `BaseChatModel` retries a timed-out request once at most (the client has usually retried it already), waits for a 429's `Retry-After` (or a few seconds), and calls `model.on_retry(attempt, error, delay)` before each retry.
- **New in code, all off by default:** `AgentRunner(trim_results=True)` or `trim_results=TrimPolicy(...)` (`step_chars=`, `shrink_exempt_at=`), `Permissions.read_max_chars`, `Skills(remember_reads=True, observer=fn)`, and `model.on_usage = fn`, called after every reply that reports usage. OpenAI's cached input tokens are now recorded in `model.usage.total_cache_read_tokens` (Anthropic's already were).
- **Fixed:** an error raised while recording usage (`CostBudgetExceeded` from `configure_limits(budget_usd=...)`, or your `on_usage` hook) is no longer retried. Before, on the plain chat path the call was retried up to three times after the budget was spent, and each retry was another paid request.
- **If you used `trim_results=True` from a build of main:** the default `TrimPolicy` now keeps at least the newest result whole (was 4), moves its window in steps and stubs older skill texts in a long conversation. `TrimPolicy(keep_last=4, step_chars=0, shrink_exempt_at=0)` gives the earlier behaviour.
- **This repository's `AGENTX.md` is short now:** the full agent guide moved to [docs/reference/agent-guide.md](../reference/agent-guide.md).
- **No new dependency.**

## Upgrading to 3.7.0

**Nothing breaks.** The CHANGELOG lists no changed or removed API for 3.7.0. Two bug fixes change behavior for code that already used `Session`, `chat_history` or `edit_file`, in the direction you would want.

- **New:** `agentx`, a terminal coding assistant, is a new console script installed with the package. `python -m agentx_dev` starts the same program. You do not need to change any code; if you never run the command, nothing about your install is different except the new dependency below. See [The `agentx` command](cli.md).
- **New:** `Session.stream(user_input, **kw)` is like `Session.invoke` but yields the runner's events and absorbs the result on the final `completion` event. A turn abandoned before then is not recorded.
- **New dependency:** `prompt_toolkit>=3.0` (the input line of `agentx`) is now a base dependency. `pip install -U agentx-dev` installs it; `rich` was already one. Claude still needs `pip install "agentx-dev[anthropic]"`.
- **Fixed, behavior change:** a conversation can continue after a run that ended on a final answer given as a function call (`React_` or `respond`). Before, the run ended without a result for that call and the next turn was rejected by OpenAI ("An assistant message with 'tool_calls' must be followed by tool messages"). The history a run returns now closes the call, and a history saved by an older version is repaired when it is resumed. This affects `Session` and `chat_history` users; the closing entry is a `tool` message whose content is `(final answer delivered)`, so code that reads the last entries of `result.history` may now see it.
- **Fixed, behavior change:** `edit_file` no longer rewrites a file's line endings. It used to turn an LF file into CRLF on Windows and a CRLF file into LF on Linux and macOS, changing every line. It now keeps every byte outside the edit, and a model's bare newlines in `find` and `replace` are put in the file's own ending, so CRLF files can be edited across lines. If you had worked around the old behavior (for example by normalising endings after an edit), you can drop the workaround.

If you want to try `agentx`:

```bash
pip install -U agentx-dev
export ANTHROPIC_API_KEY=sk-ant-...      # or OPENAI_API_KEY
cd my-project
agentx
```

The first start in a folder asks whether you trust it. Nothing from the folder is read until you say yes. A cloned repository's `.agentx/settings.json` is untrusted: it cannot turn on `accept-edits` or `auto`, its extra folders outside the project are ignored, and its allow rules are listed and honoured only if you answer yes. Team helper models set by a project file (`teamModels`) are announced at startup. See [Project settings are untrusted](cli.md#project-settings-are-untrusted).

If you work in a notebook, restart the kernel after upgrading, as always.

---

## Upgrading to 3.6.0

**Nothing breaks unless you imported `SUPERVISOR_SPAWN_INSTRUCTION` or relied on spawned agents staying on the supervisor.** Sub-agents are opt-in, with five behavior changes (the **Changed** bullets below) for code that already used spawning or a persistent supervisor.

- **New:** a plan step can define a helper inline (`new_agent`), specialists get a `delegate` tool when a ceiling is set, and `SpawnConfig(capabilities=..., tools=...)` sets a ceiling that needs no per-spawn approval. `AsyncSupervisor(spawn_config=...)` and `AgentRunner(delegation=...)` are new. See [Sub-agents](sub-agents.md).
- **Changed:** a spawned agent no longer stays registered on the supervisor after the run. Previously `supervisor.agents` kept it, and a later run could see it.
- **Changed:** a spawn is no longer refused because an existing specialist has the same tools, so follow-up dispatches are not rerouted (`rerouted_from` in the `spawn` event is always `None`). The `spawn` event gained `origin`, `tools`, `dropped`, `reused`, `refused`.
- **Changed:** a `Supervisor` or `AsyncSupervisor` with `persistence=` and no `spawn_config` now allows helpers with web and read-only file access (`SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)`), and its specialists get the `delegate` tool for the run. Pass `spawn_config=SpawnConfig(enabled=False)` to keep the 3.5 behavior.
- **Changed:** with a 3.5-style config (`enabled=True` and neither `capabilities` nor `tools`), spawns keep their approval flow (`auto_spawn`, `auto_spawn_allowed_caps`, `approver`) and specialists do **not** get `delegate`; set `capabilities=` or `tools=` to turn delegation on. The planner prompt now teaches `new_agent` instead of `{"agent": "__spawn__", ...}`; old `__spawn__` steps still work.
- **Changed:** the `name` of a legacy `__spawn__` step must now match `[A-Za-z0-9_-]{1,40}` (and must not look like `delegate_1`); a malformed one is refused. `new_agent` names starting with `__` or of the form `delegate_<N>` are reserved.
- **Small:** `SpawnConfig.max_spawns` is now `Optional[int]` (unset means 3, or 6 in ceiling mode); `AsyncAgentRunner` accepts `system_addendum=` like `AgentRunner`; the old `SUPERVISOR_SPAWN_INSTRUCTION` constant is gone (the planner prompt is built by `spawn_instruction`).

---

## Upgrading to 3.5.0

**Nothing breaks, with one behavior change.** Persistent mode is opt-in.

- **New:** `Persistence(...)` on `AgentRunner`, `AsyncAgentRunner`,
  `Supervisor` and `AsyncSupervisor`. See
  [Long-running agents](long-running-agents.md).
- **New:** every completion has `.outcome` (`done`, `stuck`,
  `out_of_time`, `out_of_budget`, `iteration_limit`) and `.progress`
  (set only by persistent runs). `SubtaskResult` and `SupervisorResult`
  have `.outcome` too.
- **Changed, even without `persistence`:** a `Supervisor` (and
  `AsyncSupervisor`) used to accept a specialist that gave up ("Hit
  max_iterations ...", "Terminated: ...") as a normal answer. It now
  treats any outcome other than `done` as a failed attempt: it retries up
  to `max_subtask_retries` (default 1) with the reason fed back, then
  returns the step with `error` set. In practice:
  - **Each give-up costs an extra attempt.** With the default
    `max_subtask_retries=1` a specialist that gives up runs twice, so
    expect more model calls (and cost) on runs that hit it. Pass
    `max_subtask_retries=0` to keep a single attempt.
  - **The flagged step's recap no longer reaches later specialists.**
    Steps with `error` set are left out of the "prior sub-task findings"
    handed to downstream specialists, as errored steps always were.
  - **Its dependents are skipped.** In a plan that uses `depends_on`,
    steps that depend on the flagged step are not run; they come back
    with `skipped=True` and `error` "skipped: dependency '...' failed".
  - `SupervisorResult.outcome` says `partial` or `stuck` instead of
    `done` when this happens.

  If a test of yours expected the old behavior, that is why.
- **While persistence is set**, the runner's tool-result cache is off
  and the iteration cap is `max_turns` (default 1000); clearing
  `runner.persistence` restores both.

---

## Upgrading to 3.4.3

**Nothing breaks.** Two Windows path fixes:

- `Permissions.full_access(["/workspace"])` now means the `workspace`
  folder of the running project. It used to mean `C:\workspace` (a
  rooted path with no drive letter is the current drive's root), and
  since sandbox directories are auto-created, agents searched an empty
  folder outside the project. `./workspace` behaved correctly all
  along, and still does; on Linux and macOS `/workspace` stays
  absolute. Write `C:/workspace` if you really mean the drive root.
- `workspace/report.md` no longer nests into
  `<workspace>/workspace/report.md`. See
  [Workspace paths](../concepts/permissions.md#workspace-paths).

---

## Upgrading to 3.4.2

**Nothing breaks.**

- `llm.invoke`, `llm.ainvoke`, and `with_structured_output(...).invoke`
  / `.ainvoke` take `media=[...]`, like the agent runner. See
  [Media with chat models](../media/chat-models.md).
- `with_structured_output` works on models that need the Responses API.
  If the model rejects a forced tool choice there, `GPT` retries with
  `tool_choice="required"` and remembers it.
- CSV, TSV, TXT, Markdown, JSON, XML, YAML and Excel files can go in
  `media=`. Text is sent as written; spreadsheets are converted with
  pandas (pandas and openpyxl are now installed with agentx-dev). There's a 100,000-character
  cap; pass `truncate=True` or `max_chars=` to change it. See
  [Data files](../media/overview.md#data-files-csv-and-excel).
- **Fixed:** `.txt` files sent to Claude used to fail — they went out as
  base64 documents, which Claude only accepts for PDFs. They're sent as
  text now.
- Word, PowerPoint and zip files now fail with a `ValueError` before any
  request is sent, instead of a provider `400`.
- Quieter logs. The original chat-completions rejection is no longer
  printed at WARNING when a call recovers, so a successful switch no
  longer looks like the old error coming back.

## Upgrading to 3.4.1

**Nothing breaks.** Tool calls on models that refuse tools plus
reasoning on `/v1/chat/completions` (for example `gpt-6-astra`) now
switch to the Responses API automatically, with a WARNING, instead of
raising. Document URLs sent to `GPT` go the same way. To keep the old
behaviour, pass `GPT(use_responses_api=False)`. See
[Tools on models that need the Responses API](../concepts/models.md#tools-on-models-that-need-the-responses-api).

## Upgrading to 3.4

**Nothing breaks.** No public signature was removed or renamed, and
every existing call works unchanged. Two defaults changed and are worth
knowing about.

### New in 3.4

| Feature | Use it |
|---|---|
| Images, PDFs and audio for GPT and Claude | `runner.invoke("Describe this", media=["photo.jpg"])` — see [Media](../media/overview.md) |
| Models adjust to what each model generation accepts | on by default; `adapt_params=False` turns it off — see [Old and new models](../concepts/models.md#old-and-new-models) |
| Extra Claude settings | `Claude(top_p=, top_k=, thinking=, stop_sequences=)` |
| A leading `/` means the workspace | `read_path("/notes.txt")` reads `<workspace>/notes.txt` |

### Changed behaviour

**`Claude()` no longer sends `temperature` unless you set it.** The
default went from `1.0` to `None` (not sent). The API's own default is
`1.0`, so results are the same. The change stops conflicts with `top_p`
on newer models and with extended thinking. If your code *reads*
`llm.temperature`, it now sees `None` unless you passed a value.

**Rejected parameters are adjusted instead of raising.** Before 3.4, a
parameter the model didn't support raised a `400` — for example
`reasoning_effort="none"` on a model that only takes
`low`/`medium`/`high`. Now the model makes the smallest change the error
asks for, retries, and logs a `WARNING` like:

```
OpenAI model 'gpt-5.4' rejected a request parameter; changed
reasoning_effort 'none' -> 'low' and retried. Remembered for this model.
```

If you relied on that error — say, to fail fast in CI when a
configuration is wrong — opt out per model:

```python
GPT(model="gpt-5.4", reasoning_effort="none", adapt_params=False)
Claude(temperature=0.3, top_p=0.9, adapt_params=False)
```

Only parameter-compatibility errors are handled this way. Context-length
overflows, bad API keys and every other `400` raise exactly as before.

**`run_python` starts in the workspace.** It used to inherit the
directory the host program was launched from. If your agent's Python
code used paths relative to *that* directory, rewrite them relative to
the workspace, or use absolute paths. `run_shell` already behaved this
way.

**Leading-slash paths resolve inside the workspace** when a workspace is
set. `/notes.txt` used to mean the drive root (`C:\notes.txt` on
Windows), which the sandbox rejected anyway — so this only turns an
error into the result you meant. The re-rooted path is still
sandbox-checked. Without a workspace, nothing changes.

### Recommended follow-ups

- **Remove `reasoning_effort="none"` workarounds.** Earlier docs
  suggested it to avoid a tool-calling conflict. It's now safe to set
  the effort you want and let the model settle on the nearest value it
  supports.
- **Stop base64-encoding images yourself.** Hand the file path to
  `media=` or `Media.image()`, and the same code works on both providers.
- **Send images with `media=`, not `read_path`.** `read_path` reads
  text. Attaching the file is how the model actually sees it.

---

## Upgrading to 3.3

**Nothing breaks.** `Supervisor` and `AsyncSupervisor` plans can now
declare `depends_on` edges. Plans without edges run exactly as before.
`sequential=True` still works; it now behaves like `max_parallel=1`.
See [Supervisor](../advanced/supervisor.md).

## Upgrading to 3.2

**Nothing breaks.** `AgentRunner(output_schema=...)` is new. If you see
`TypeError: AgentRunner.__init__() got an unexpected keyword argument
'output_schema'`, an older build is still installed or still imported —
upgrade and restart the kernel.

## Upgrading to 3.1.7

**`use_function_calling` detects itself.** It defaults to `True` for any
model that implements native function calling (both `GPT` and `Claude`).
Two things follow from that:

- **Streaming tokens:** `stream_tokens=True` yields no `text_delta`
  events in function-calling mode. Pass `use_function_calling=False` when
  you want token streaming. See [Streaming](streaming.md).
- **Test mocks:** a mock model that defines `call_with_tools` is treated
  as function-calling-capable. If your mock only scripts *text*
  responses, don't define `call_with_tools` on it — otherwise the runner
  takes the function-calling path and returns `""`.
