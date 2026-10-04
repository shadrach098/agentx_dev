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
