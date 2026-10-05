# Changelog

All notable changes to `agentx-dev` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semver](https://semver.org/).

## [3.6.0] - 2026-10-04

Sub-agents: the Supervisor can create its own helpers. Opt-in, with the behavior changes listed below.

### Added

- **Inline helpers**: a plan step can carry `new_agent` (name, free-form
  instructions, tools). The helper runs the step, is reused by name, and is
  discarded when the run ends. Recovery plans can define a replacement for a
  stuck step.
- **`delegate` tool**: specialists hand a side job to a fresh sub-agent (clean
  context) and get a short summary back; refusals are plain text and a helper
  that fails comes back as a tool error the caller's stuck logic sees.
  `AgentRunner(..., delegation=SpawnConfig(...))` gives a standalone runner the
  same tool; `runner.spawned` lists what it created.
- **One ceiling**: `SpawnConfig(capabilities=..., tools=..., max_spawns=...,
  max_depth=...)` bounds every helper; spawns inside it need no approval and
  over-asks are clipped, not fatal. New read-only `files_read` preset.
- **`AsyncSupervisor(spawn_config=...)`**: async parity; spawned steps run in
  parallel. `AsyncAgentRunner` gained `system_addendum=`.
- `SupervisorResult.spawned`, `spawn` and `delegate_result` stream events,
  `AgentSpec`, `ToolRegistry.unregister`, `AgentRunner.add_tool` /
  `remove_tool`.
- New guide: Sub-agents.

### Changed

- A spawned agent no longer stays on the supervisor after the run.
- A spawn is no longer refused (or rerouted) because an existing specialist
  has the same tools. The `spawn` event gained `origin`, `tools`, `dropped`,
  `reused` and `refused`; `rerouted_from` is always `None`.
- A `Supervisor` / `AsyncSupervisor` with `persistence=` and no `spawn_config`
  now allows helpers with web and read-only file access (`max_spawns=6`) and
  gives its specialists the `delegate` tool. Pass
  `spawn_config=SpawnConfig(enabled=False)` to opt out.
- A legacy spawn config (`enabled=True` with neither `capabilities` nor
  `tools`) keeps the 3.5 approval flow and gives specialists no `delegate`
  tool; set a ceiling to turn delegation on.
- The planner prompt teaches `new_agent` instead of the `__spawn__` step; old
  `__spawn__` steps still work, but their `name` must now match
  `[A-Za-z0-9_-]{1,40}`.
- Agent names starting with `__` and names of the form `delegate_<N>` are
  reserved: a `new_agent` that uses one is dropped by plan sanitization.
- `SpawnConfig.max_spawns` is `Optional[int]` (unset: 3, or 6 in ceiling mode).
  `SUPERVISOR_SPAWN_INSTRUCTION` was removed.

## [3.5.0] - 2026-10-04

Agents that keep working through errors. Opt-in; one default change (below).

### Added

- **Persistent mode** (`persistence=Persistence(...)`) for `AgentRunner`,
  `AsyncAgentRunner`, `Supervisor` and `AsyncSupervisor`. When a run gets
  stuck (the same call repeated, or tool errors in a row) it is
  asked, in four escalating steps, to find the root cause, try a
  different approach, probe its assumptions, and say what blocks it,
  before it may stop; real progress resets the ladder. Runs stop on
  `max_minutes` or the model's cost cap, not a step count.
- **Context compaction** for long runs: old turns become notes, the task
  (with its attachments) and recent turns are untouched, and the failed
  attempts are kept word for word.
- **Patient retries**: transient provider errors (429, 5xx, timeouts,
  connection errors) are retried with backoff until the deadline; auth
  and invalid-request errors still raise at once.
- **Supervisors replan**: a step that does not finish is replanned
  around under one shared deadline; finished steps are kept and can be
  depended on. A recovery step names the steps it redoes
  (`"replaces": [...]`); a failed step counts as resolved only when its
  replacement finishes. New `replan`, `reflect`, `compact` and `budget`
  stream events.
- **Outcomes**: every completion has `.outcome` and (persistent runs)
  `.progress`; `SubtaskResult` and `SupervisorResult` have `.outcome`.
- New guide: Long-running agents.

### Changed

- A `Supervisor`/`AsyncSupervisor` now treats a specialist that finished
  with an outcome other than `done` (for example `iteration_limit`) as a
  failed attempt: it retries up to `max_subtask_retries` with the reason
  fed back, then flags the step. Previously such a result was accepted
  as a success. This applies without `persistence`. Consequences: each
  give-up costs one extra attempt by default (`max_subtask_retries=1`);
  the flagged step's recap is no longer passed to downstream
  specialists; steps that `depends_on` it are skipped; and
  `SupervisorResult.outcome` is `partial` or `stuck`.
- While `persistence` is set the tool-result cache is off, and the
  iteration cap is `persistence.max_turns`.

## [3.4.3] - 2026-09-26

Two path-resolution fixes for Windows sandboxes. Non-breaking.

### Fixed

- **`full_access(["/workspace"])` now means the project's workspace
  folder, not the drive root.** On Windows a rooted path without a drive
  letter resolves to the current drive, so the sandbox became
  `C:\workspace`; `auto_create_paths` then created it, and an agent
  asked for `workspace/spam.csv` searched an empty directory outside the
  project and reported the file missing. Slash-rooted sandbox roots are
  now re-rooted at the running project, matching what a leading slash
  already meant for tool arguments. POSIX absolute paths (`/workspace`
  as a Docker mount) and drive-qualified paths are untouched.
- **`workspace/report.md` no longer nests into
  `<workspace>/workspace/report.md`.** The `workspace` field documented
  this pass-through, but only the `./workspace/...` spelling had it.
  Models name the workspace folder constantly, so the missing case hit
  often; new files still resolve into the workspace as before.

## [3.4.2] - 2026-09-19

`media=` everywhere, CSV / text / Excel attachments, and structured output
on models that need the Responses API. Non-breaking.

### Added

- **`media=` on chat models and structured output.** `llm.invoke`,
  `llm.ainvoke`, and `with_structured_output(...).invoke` / `.ainvoke` take
  `media=[...]` (paths, URLs, `Media`, part dicts), appended to the last
  user turn, matching the agent runner. Media inside `content` lists
  already worked; this adds the argument.
- **CSV, text files, and Excel in `media=`.** Text-like files (CSV, TSV,
  TXT, Markdown, JSON, XML, YAML, HTML) are read as text -- exactly as
  written, UTF-8 or Windows-1252, CRLF normalised -- and spreadsheets
  (.xlsx / .xls / .ods) are converted with pandas (`dtype=object`, so
  cell values aren't re-inferred) to one CSV block per sheet. Both reach
  GPT (chat and Responses) and Claude as a labelled text block, which
  every model accepts. New `Media.text()` / `Media.spreadsheet()`; a
  100,000-character cap (`max_chars=`, `truncate=True`). pandas and
  openpyxl are now core dependencies, so spreadsheets work after a plain
  `pip install agentx-dev` (`[excel]` still works). Date cells without a
  time are written as dates, not `... 00:00:00`.
- **Media docs section.** Its own nav group: Overview & flow, With chat
  models, With agents, Reference (constructors, per-provider wire
  formats, errors). `docs/guides/media.md` is now a pointer to it.

### Fixed

- **`with_structured_output` on models that need the Responses API.**
  Structured output forces a single tool; if the Responses API rejects a
  forced tool choice for the model, `GPT` retries with
  `tool_choice="required"` (the same tool when there is only one), then
  `"auto"`, remembering it per model. `StructuredOutputRunnable` also
  accepts a JSON text answer that validates against the schema.
- **`.txt` media failed on Claude.** 3.4.0 stored text files as base64
  documents, which Anthropic only accepts for PDFs. Text files now go
  out as text; older base64 text parts in saved histories are decoded
  on the way out.
- **Unsupported files reached the provider.** `Media.document()`
  accepted any file type -- including CSV, which Windows' own type table
  labels `application/vnd.ms-excel` -- and sent it as an attachment
  neither provider accepts. Word, PowerPoint and archives now raise
  `ValueError` with the fix before any request; a text file or
  spreadsheet by URL raises too, since only PDFs can be fetched by URL.
- **Misleading WARNING on recovered calls.** The retry wrapper logged
  every non-retryable 400 at WARNING before the caller could recover, so a
  call that switched to the Responses API and succeeded still printed the
  original "Function tools with reasoning_effort are not supported" text.
  Now DEBUG; recoveries log their own WARNING and real failures log ERROR.

## [3.4.1] - 2026-09-18

Tool calls on OpenAI models that require the Responses API.

### Fixed

- **Tool calls failed on models that need the Responses API.** Some
  OpenAI models (e.g. `gpt-6-astra`) reject function tools combined with
  reasoning on `/v1/chat/completions`, and the error's suggested fix --
  `reasoning_effort='none'` -- is itself rejected by those models, so
  3.4.0's parameter adaptation had nowhere to go. `GPT` now switches a
  model's tool calls to `/v1/responses` when the provider says to,
  retries, and remembers it (WARNING-logged). History is translated only
  at the wire (`function_call` / `function_call_output` items paired by
  `call_id`), so agents, `tool_calls`, and completions are unchanged.
  Learned parameter fixes carry across endpoints (`reasoning.effort` maps
  to `reasoning_effort`). Requests are sent with `store=False`, matching
  chat completions. Plain text calls and streaming stay on chat
  completions.
- Requests chat completions can't express (a document URL) go to the
  Responses API, which accepts `file_url`, instead of raising.

### Added

- `GPT(use_responses_api=None | True | False)`: automatic (default),
  always, or never.

## [3.4.0] - 2026-09-18

Media input for GPT and Claude, models that adapt to each generation's
parameter support, and workspace-rooted paths. Non-breaking; two
defaults changed -- see docs/guides/upgrading.md.

### Added

- **Media input for GPT and Claude.** New `Media` type
  (`Media.image()`, `.document()`, `.audio()`, `.from_path()`,
  `.from_url()`, `.from_bytes()`) and a `media=[...]` argument on
  `AgentRunner.invoke` / `stream` and `AsyncAgentRunner.ainvoke` /
  `astream`. Items can be paths, URLs, `Media`, or content-part dicts
  in either provider's native shape; each model renders them in its
  own wire format (OpenAI `image_url` / `file` / `input_audio`,
  Anthropic `image` / `document`). Unsupported combinations -- audio to
  Claude, a document URL to GPT -- raise `ValueError` before any
  request. Media is stored as plain JSON dicts, so `completion.history`
  and `Session.save()` keep working. Guide: docs/guides/media.md.

- **Models adapt to per-generation parameter support.** `GPT` and
  `Claude` read a parameter-compatibility 400 (which names the
  parameter and, for enums, the allowed values), make the smallest
  change it asks for, retry, and remember it per model: an unsupported
  `reasoning_effort` moves to the nearest supported value, `max_tokens`
  becomes `max_completion_tokens` on reasoning models (up front for
  known families), parameters a model lacks are dropped, and Claude's
  `max_tokens` is clamped to the model's cap. Logged at WARNING; other
  400s raise unchanged. `adapt_params=False` opts out.

- **`Claude` gains `top_p`, `top_k`, `thinking`, `stop_sequences`.**

### Changed

- `Claude(temperature=)` defaults to `None` (not sent; the API default is
  the old 1.0). Always sending it conflicted with `top_p` on newer models
  and with extended thinking.
- `GPT(reasoning_effort=)` accepts any string, not just
  `none | low | medium | high`, so `minimal` and `xhigh` are expressible.

### Fixed

- **`reasoning_effort="none"` crashed on models that don't take it**
  ("Unsupported value: 'reasoning_effort' does not support 'none' with
  this model"). The models docs even recommended `"none"` as a fix for
  tool-calling conflicts; that advice is gone.
- **Workspace-rooted paths resolved to the drive root.** With a workspace
  set, `/bruce.jpeg` became `C:\bruce.jpeg` on Windows (a leading slash
  with no drive means the current drive's root) and the sandbox rejected
  it. A leading slash now means the workspace root; the re-rooted path
  is still sandbox-checked, so traversal is rejected as before.
- **`run_python` ran in the host process's directory, not the
  workspace.** It passed no `cwd`, so `open("bruce.jpeg")` looked
  wherever the program was launched from. It now starts in the workspace,
  matching `run_shell`.
- **Message-list input stringified media into the prompt.** A user turn
  whose content was a list (text + image) went through `str()`, so an
  attached image reached the model as base64 text. Text and media are
  now split, and earlier turns keep their list content.
- **Claude returned the wrong block with extended thinking.** It read
  `response.content[0].text`; with thinking the first block is a
  `thinking` block. Text blocks are now joined.
- `GPT.Initialize` / `stream_text` / `astream_text` now run messages
  through the same OpenAI translator as `call_with_tools`.

## [3.3.1] - 2026-09-09

Correctness fixes in the agent loop, found by driving it with a
scripted model rather than reading it. No API changes; every fix
replaces a silent wrong answer or a runaway cost with correct
behaviour. Nine regression tests ship with them
(`tests/test_agent_loop_flaws.py`).

### Fixed

- **Native binding had no loop-level spiral guard.** The repeat breaker
  sat after the native branch's `continue`, so `bind_tools_natively`
  runs never reached it: a model stuck re-issuing one call burned the
  full `max_iterations` (20 LLM turns where text mode stopped at 3) and
  returned the "hit max_iterations" recap quoting the dup-guard's own
  warning text instead of the data.

- **`respond` batched with real tool calls dropped them.** A model that
  emitted "do X, and here is my answer" in one turn had X silently
  discarded -- never dispatched, never in `completion.tool_calls`, no
  error. The batch now runs and the answer defers one turn.

- **`completion.history` could not be replayed as `chat_history`.** The
  copy filter kept only truthy `{role, content}`, which dropped every
  tool-calling assistant turn (`content=""`), stripped `tool_call_id`
  off `role="tool"` messages, and replayed a stored system prompt on top
  of the fresh one -- so a follow-up call sent two system messages, zero
  assistant turns, and an orphaned tool message providers reject. Fixed
  in both runners.

- **A sync `AgentRunner` silently ignored async tools.** `known_tools`
  unioned only the two sync registry tables while the registry accepted,
  listed and prompt-advertised async ones, so calling one fell through
  to implicit-final and returned `action_input` as the answer with an
  empty `tool_calls` list. Now unions all four, matching the async
  runner; the sync dispatcher already returns a clear `ToolError`, so
  the silent wrong answer became an actionable one.

- **`AsyncAgentRunner` regressed the function-calling message shape.**
  It appended its FC turn as raw JSON text with no `tool_calls` block
  and never set `_last_function_call_id`, so observations went back as
  `role="user"` -- text-mode shape while in FC mode, losing provider
  correlation and the cached prefix.

- **A `BaseException` from a specialist orphaned its siblings.**
  `CancelledError` and `KeyboardInterrupt` are not `Exception`, so
  `_run_subtask`'s handler never saw them; `t.result()` re-raised and
  left sibling tasks running unowned, with in-flight LLM calls still
  billing. The scheduler now owns its tasks in a `try`/`finally`, which
  also covers a consumer that stops iterating `astream` early.

- **The tool cache collided on tool name.** It is a process-wide
  singleton keyed on `(tool_name, args)` with no record of which
  implementation ran, so two runners whose tools merely share a name --
  `search`, `fetch`, `query`, routine across Supervisor specialists --
  served each other's results and the second function never ran. Keys
  now fold in the callable's `module.qualname`, so same-name /
  different-implementation misses while genuinely identical tools still
  share, including across processes for the disk-backed `FileCache`.

### Docs

- Streaming documentation described parameters the API rejects.
  `AsyncAgentRunner.astream()` and `HandoffCoordinator.stream()` take no
  `stream_tokens` (only the sync `AgentRunner.stream()` does), and every
  `text_delta` example was built on a default model where
  `use_function_calling` auto-detects to `True` and yields zero deltas.
  `simple_stream` was documented with the wrong signature entirely.
  Added a table naming exactly which stream methods accept what.

### Tests

- Repaired 20 stale text-mode tests. `MockModel` defined
  `call_with_tools` unconditionally, so 3.1.7's auto-detect routed every
  text script down the function-calling path and the runner returned
  `""`. The mock now advertises the capability only when scripted for
  it. Suite: 206 passed, 3 skipped.

## [3.3.0] - 2026-08-19

Dependency DAGs for the Supervisor. Plans declare which steps consume
which, and the executor derives ordering, parallelism, AND context
routing from those edges — unifying the old split where sequential
mode had threading but no parallelism and concurrent mode had
parallelism but no threading. Design: docs/design/3.3-depends-on-dag.md.

### Added

- **`depends_on` plan steps.** Every plan step now carries an `id`;
  a step that consumes an earlier step's output lists that id in
  `depends_on`. Sync `Supervisor` executes in stable topological
  order; `AsyncSupervisor` runs a completion-driven scheduler where a
  step starts the MOMENT its dependencies finish (not on wave
  barriers) and independent steps run concurrently. In DAG mode each
  step is threaded ONLY its direct dependencies' results — explicit
  routing instead of "everything prior", which also stops the
  per-entry context budget shrinking as plans grow.

- **Plan sanitization that never fails a run.** Missing/duplicate ids
  auto-assigned, unknown dependencies dropped (a planner typo degrades
  to a root step, not a dead run), self-deps dropped, cycles broken
  deterministically (back-edge in plan order), spawn steps cannot be
  depended on. If repairs were needed, the plan is re-requested once
  (`max_plan_retries`, default 1) with the repair warnings appended;
  the sanitized original is kept when the retry is no better.

- **Failure cascade + `skipped` flag.** A step whose dependency failed
  (after retries / success-check) is skipped, transitively, with
  `SubtaskResult.skipped=True` and an error naming the failed dep.
  Independent branches keep running; synthesis runs over what
  succeeded. Skipped steps never dispatch — no tokens burned
  downstream of garbage.

- **`skip_when` conditional execution.** A step may declare
  `{"step": <direct dep id>, "field": <typed output field>, "is": <value>}`;
  evaluated in Python (no LLM call) against the dependency's 3.2
  structured output, dotted paths supported, strictly FAIL-OPEN (any
  doubt → the step runs). Condition-skips do NOT cascade — dependents
  treat them as empty successes ("retrieval unnecessary" is not
  "answering impossible").

- **`Specialist` registry entries.** `agents={}` now also accepts
  `Specialist(description, runner, depends_on=[...names...],
  output_schema=..., when_to_use=...)`. The extras render into the
  planning catalog (`typically after:` / `returns: Schema(fields)` /
  `use when:`) so the planner can write real dependency graphs and
  `skip_when` conditions against actual field names. `depends_on`
  here is a planner HINT, never an execution constraint (the same
  specialist can appear twice in one plan; step-ids disambiguate).
  Classic `(description, runner)` tuples keep working — they are
  wrapped internally, and `Specialist` tuple-unpacks for older code.

- **`AsyncSupervisor(max_parallel=N)`.** Caps concurrent sub-tasks for
  rate-limited deployments. `sequential=True` is now sugar for
  `max_parallel=1` with all-prior threading.

- **`SubtaskResult.step_id` / `.depends_on` / `.skipped`** and a
  `step_id` field on `dispatch` / `subtask_result` stream events.

### Backward compatibility

A plan where NO step declares `depends_on` runs with byte-identical
legacy semantics: sync + async-sequential thread all prior results in
plan order; async-concurrent runs everything at once with no
threading. Verified by regression tests against the 3.2 behaviour.

## [3.2.0] - 2026-08-13

Typed multi-agent pipelines. Specialists can now declare a Pydantic
output schema once and pass validated instances to each other through
the Supervisor, instead of downstream agents re-parsing prose.

### Added

- **`output_schema` on the `AgentRunner` / `AsyncAgentRunner`
  constructor.** Declare the runner's output shape once
  (`AgentRunner(..., output_schema=QueryIntent)`) instead of passing it
  on every call or describing JSON in the prompt. A per-call
  `output_schema=` still wins when both are set. `None` keeps the exact
  pre-3.2 behaviour: no coercion, `completion.output` stays `None`.

- **Schema coercion via forced native function calling.** When a schema
  is in play, the final answer is converted by forcing a provider-native
  tool call against the schema (constrained decoding), not by regexing
  JSON out of prose. The ReAct loop itself is untouched: tool selection
  and intermediate reasoning run exactly as before, and the coercion
  happens once, after the loop finishes. Models without a
  `call_with_tools` implementation fall back to the previous text-JSON
  parsing, so custom `BaseChatModel` subclasses keep working.
  `completion.content` keeps the human-readable answer alongside
  `completion.output` in every case.

- **`SubtaskResult.output`.** The Supervisor now preserves each
  specialist's validated Pydantic instance next to its `content` text.
  Consumers that only read `content` are unaffected.

- **Structured specialist-to-specialist handoff.** When an earlier step
  produced typed output, `_build_augmented_query` serializes it into the
  next specialist's context as a labelled JSON block
  (`STRUCTURED OUTPUT (QueryIntent): {...}`) followed by the summary
  text, so downstream steps parse fields rather than interpreting
  sentences like `INTENT: ... SEARCH_QUERY: ...`.

- **`vector_search_tool` pipeline options.** New kwargs:
  `max_text_chars` (default 500; pass `0` for full untruncated passages,
  which a reranker judging evidence actually needs) and
  `structured_output` (default False; when True the tool returns a JSON
  array of `{id, text, vector_score, metadata}` instead of the
  human-formatted list). Defaults preserve existing behaviour byte-for-
  byte.

### Fixed

- **Supervisor planning prompt contradicted the execution engine.** The
  planner rule said sub-agents "do NOT see previous steps' output" and
  discouraged dependency chains, but the dispatcher has threaded prior
  findings into every step since `_build_augmented_query` shipped.
  The rule now tells the planner that sequential steps receive earlier
  results (structured when available) and that chains like
  intent -> retrieval -> reranking are a good plan shape, while still
  requiring same-specialist steps to merge and banning report-only steps.

## [3.1.7] — 2026-07-27

### Changed

- **`use_function_calling` default flipped to auto-detect** on
  `AgentRunner` / `AsyncAgentRunner`. The parameter's default type is
  now `Optional[bool] = None`; `None` resolves to `True` when the
  model class overrides `BaseChatModel.call_with_tools` (both `GPT`
  and `Claude` do) and to `False` when it doesn't (or when
  `bind_tools_natively=True`). Callers passing `True`/`False`
  explicitly are unaffected. Rationale: text-mode ReAct requires the
  model to emit strict JSON with any long `action_input` string
  properly escaped — a 1200-word markdown draft with unescaped
  newlines or quotes reliably breaks `json.loads` and killed the run.
  Function-calling mode routes the parser through the SDK's typed
  channel so escaping is handled automatically. The historical
  default (`False`) was the fragile option; the new default matches
  what most users actually want.

### Fixed

- **Malformed parser JSON no longer crashes the run.** When the
  text-mode assistant response failed `json.loads` (typically because
  a long `action_input` string had unescaped `"`, `\n`, or backticks),
  the framework used to raise `JSONDecodeError` and unwind the whole
  invocation. The runner now (1) tries a regex-based salvage that
  extracts `{Thought, action, action_input}` from the raw text
  covering the common "outer envelope valid, inner string broke
  escaping" failure, and (2) if salvage fails, feeds a targeted fix
  hint back to the model (`"your last response was not valid JSON;
  emit …, escape newlines as \n"`) and continues the loop bounded
  by `max_iterations`. Exhaustion returns a clear framework message
  rather than an uncaught exception. Applied to both sync and async
  runners via a shared `_salvage_react_json` helper.
  The salvager's action-name regex is intentionally strict
  (`[A-Za-z_][A-Za-z0-9_.\- ]{0,79}`) so it can't hallucinate an
  "action" out of an unrelated `"key":"value"` pair inside malformed
  JSON.

- **Verbose trace in `bind_tools_natively` mode now prints tool
  name + args + response.** Previously native runs showed blank
  `[tool.call.start]` / `[tool.call.complete]` pairs (the
  observability layer fires them without the trace context), so you
  couldn't tell which tool the model actually invoked or what came
  back. The runner now prints `[tool] Invoking '<name>' with args:
  <input>` and `[tool] Response: <preview>` (or `[tool] Error: ...`
  when the dispatch raised) in the post-dispatch loop, matching the
  format text-mode and function-calling mode use. Mirrored to the
  async runner.

- **`web_fetch_tool(vector_store=...)` auto-ingests fetched pages into
  a vector store** instead of dumping raw HTML into the model's
  context. Fixes the TPM-limit trap: when a research agent fetches
  four articles in parallel (via ``multi_tool_use.parallel`` or
  native binding), the combined bodies can easily exceed 40k tokens
  and blow past a 30k TPM ceiling on the very next model call.
  New parameters on ``web_fetch_tool``:

  | Kwarg | Default | Effect |
  |---|---|---|
  | ``vector_store`` | ``None`` | When set, each fetch is HTML-stripped, chunked with ``TextSplitter``, embedded via the store's embeddings, and added with ``{src: url, chunk_index, total_chunks}`` metadata. The tool response becomes a compact summary (URL, byte count, chunk count, 240-char preview) — NOT the raw body. The model then calls ``vector_search`` / ``Rag`` to pull only the passages it needs. |
  | ``chunk_size`` | ``1500`` | Characters per chunk when ``vector_store`` is set. Ignored otherwise. |
  | ``chunk_overlap`` | ``200`` | Overlap between adjacent chunks so a fact spanning a boundary is still retrievable. Ignored otherwise. |

  Backwards-compatible: the positional ``cache_dir`` signature keeps
  working; `web_fetch_tool()` with no ``vector_store`` returns raw
  body as before. Ingest and cache_dir compose — enable both and get
  disk-cached full bodies AND searchable chunks. HTML stripping is
  minimal and dependency-free (regex-based: script/style blocks
  dropped whole, then tags stripped, whitespace collapsed) so the
  ingest path adds no new install dependency. On JSON/plain-text
  responses the stripper is a near no-op.

  The observation returned to the model shows topical coverage --
  first, middle, and last chunk previews (up to 3 samples,
  deduplicated for short pages) -- so the model can tell what
  topics the page actually covers, not just the intro paragraph.
  Without this the model would only see the page's opening and
  wouldn't know to query for topics discussed later in the same
  page. Explicit instruction in the observation ("query with
  SPECIFIC keywords from the topics above; do NOT re-fetch; do
  NOT ask for the full body") steers the model toward the RAG path
  on follow-up turns.

- **`multi_tool_use.parallel` now reaches its dispatch path.**
  When GPT wanted to batch several tool calls into one turn (fetch N
  URLs concurrently, run M searches at once), it emitted OpenAI's
  synthetic `multi_tool_use.parallel` meta-tool. The registry's
  `_dispatch_multi_parallel` / `_adispatch_multi_parallel` handlers
  already knew how to unpack it, but the runner loop's known-tools
  guardrail rejected the name FIRST as unregistered — dumping the
  raw `{"tool_uses": [...]}` payload into the user-facing "final
  answer" and never invoking any of the nested calls. Added
  `multi_tool_use.parallel` to the recognized action set in both
  sync and async runners so the meta-tool flows through to dispatch
  and the existing unpackers run. Nested calls with the `functions.`
  prefix are normalized before dispatch (same as top-level FC
  calls), so the model can emit either shape.

- **`Permissions.full_access` / `read_only` auto-wrap a bare string.**
  Passing `full_access("./workspace")` used to iterate the string
  into 11 single-character "subtrees" (Python's `list("./workspace")`)
  — every path check silently rejected because no real path could
  ever match a `"."` or `"/"` "allowed subtree". The classmethod
  now detects a bare string and treats it as `[allowed_paths]`, so
  `full_access("./workspace")` does the intuitive thing (equivalent
  to `full_access(["./workspace"])` and auto-infers the workspace).
  Same fix on `read_only`. List inputs are unchanged.

- **`Permissions.full_access` now accepts (and auto-infers)
  `workspace`.** The classmethod set `allowed_paths` but not
  `workspace`, so short paths like `write_file(path="report.md")`
  resolved to CWD (outside the sandbox) and raised
  `PermissionError: access denied` — a landmine that every caller of
  `Permissions.full_access(["./workspace"])` hit sooner or later.
  New signature: `full_access(allowed_paths, *, workspace=None)`.
  When `workspace` isn't passed AND `allowed_paths` has exactly one
  entry, that path is auto-set as the workspace (the "project-scoped
  agent whose one allowed subtree IS its workspace" case, which is
  99% of use). Two or more paths stay ambiguous and require an
  explicit `workspace=` if short-path resolution is wanted. Pass an
  explicit `workspace=` string to override the auto-choice.
  Backwards-compatible on the positional signature; adds a keyword
  argument that existing callers didn't use.

## [3.1.5] — 2026-07-26

### Fixed

- **Text-mode tool results no longer use `role: "function"`.** In text
  mode (the default — no `use_function_calling`) the runner fed each tool
  observation back to the model as a `role: "function"` message. Newer
  OpenAI models reject that role outright (`400 … 'messages[N].role' does
  not support 'function' with this model`, e.g. gpt-5.x), and Anthropic
  never accepted it — text-mode multi-tool runs on Claude were latently
  broken too; older GPT models simply still tolerated the legacy role.
  Tool observations now go back as a plain `role: "user"` turn framed as
  `Observation: …`, which every provider and model generation accepts and
  which matches the ReAct template's own few-shot convention.
  Function-calling mode is unchanged (native `role: "tool"` +
  `tool_call_id`). The async runner was additionally emitting `function`
  unconditionally (even in FC mode); it now uses the same shared helper.

### Added

- **`subtask_success_check` on `Supervisor` / `AsyncSupervisor`.** Opt-in
  predicate `(SubtaskResult) -> bool | str` that decides whether a
  *returned* (non-raised) sub-task result is actually acceptable — the
  "ran fine but produced nothing useful" case a plain retry can't catch
  (a scraper that saved 0 links, an extractor that found nothing). Return
  `True` to accept, or `False`/a `str` reason to reject; a rejected
  result is retried like a raised error, with the reason fed back into the
  query, bounded by `max_subtask_retries`. After retries are exhausted the
  last result is returned with its `error` set (content preserved). A
  check that itself raises is treated as "accept" so a buggy predicate
  can't wedge the run. Default `None` keeps the exceptions-only behavior.
  New example `examples/robust_link_scraper.py` wires it together with a
  scraping `system_addendum` (parse relative+absolute hrefs, fall back to
  `sitemap.xml` on JS-rendered sites).

## [3.1.4] — 2026-07-26

### Fixed

- **A malformed-JSON tool argument no longer crashes the whole agent
  run.** When a model emitted a Python snippet or a Windows path as a
  tool-call argument — `re.findall(r'\d+')`, `C:\Users` — the `\d` / `\U`
  are illegal JSON escapes, and the OpenAI adapter's eager
  `json.loads(call.function.arguments)` raised `JSONDecodeError` and
  `raise`d it, unwinding the entire ReAct loop before the agent's own
  retry machinery could act. Under a Supervisor this surfaced as a bare
  `ERROR: Invalid \escape: line 1 column 598` and the sub-task was
  abandoned. Now:
    - `_parse_tool_arguments` repairs the common case (backslashes that
      don't begin a valid JSON escape are doubled), recovering `\d`,
      `\w`, `\s`, etc. with zero extra round-trips. A backslash before a
      valid-escape letter (`\b`, `\n`, …) remains ambiguous and is left
      as the escape — a documented limit.
    - When repair fails, `call_with_tools` returns a dedicated
      `invalid_tool_args` result and the loop feeds the error back as a
      retryable observation ("your arguments weren't valid JSON — escape
      backslashes and resend"), bounded by `max_iterations`, in all
      three modes across both `AgentRunner` and `AsyncAgentRunner`.
    `Claude` was already immune (its tool inputs arrive pre-parsed).

- **A tool-call preamble is no longer returned as the final answer.**
  Models routinely end a turn with an announcement instead of an action
  — "I'll look up your recent scores to get a clear view of your
  communication skills. Just a second!" — and every "no tool call
  found" branch in both runners was coded as *this text is the answer,
  break*. The loop terminated on iteration 1 and the caller got a
  promise instead of a result. Three sites per runner were affected:
  the native-binding path (`type != "tool_use"`), the
  `use_function_calling` path (parser unresolved), and the JSON-text
  path (response didn't parse). `max_iterations` never helped, because
  the break happened before any iteration was spent.

  The runner now feeds the model one corrective nudge — "your last turn
  had no action, so nothing happened; do it, don't announce it" — and
  continues the loop. Verified against both `AgentRunner` and
  `AsyncAgentRunner` in all three modes.

### Added

- **Proactive "act, don't announce" system-prompt clause.** The reactive
  `text_turn_nudges` fix corrects an agent *after* it narrates instead of
  acting; this clause heads it off. When (and only when) an agent has
  tools, its system prompt now tells it to call the tool rather than
  reply "I'll do X / just a second" and stop — and to report what it DID
  in past tense. Injected in all three modes across both runners; skipped
  for tool-less chat agents, where prose is the correct answer. Sits
  before any `system_addendum` so a caller's role instructions still win.

- **`max_subtask_retries` on `Supervisor` / `AsyncSupervisor`** (default
  `1`). A sub-task that raised used to be recorded as an error and the
  Supervisor moved straight to synthesis — no second attempt. Now a
  failed sub-task is re-dispatched up to this many times, with the prior
  error appended to the query so the specialist knows what to fix
  ("your previous attempt failed with X — diagnose and try again").
  Bounded and informed: only raised exceptions trigger a retry (a
  sub-task that returns content is accepted as-is, since the Supervisor
  can't tell "terse but correct" from "wrong"), and the error text is
  fed back rather than blindly re-running. Set to `0` for the old
  quit-on-first-failure behavior. Applies in both sequential and
  concurrent async modes.

- **`text_turn_nudges` on `AgentRunner` / `AsyncAgentRunner`** (default
  `1`). Caps the re-prompts described above at one extra LLM call per
  run; after the budget is spent the model's text stands as the answer.
  Set to `0` for the previous behavior. Automatically skipped when no
  tools are registered, since a runner with no tools is a plain chat
  call and prose genuinely is the answer there.

## [3.1.3] — 2026-07-22

Docs-only patch. No code changes since 3.1.2. Users on 3.1.2 don't
need to upgrade for functionality; upgrade to pick up the improved
onboarding docs bundled in the sdist.

### Documentation

- **Tools doc rewritten to answer "how do I actually use these?"**
  Added §0 `How each built-in tool is registered` as the entry
  section. Two registration paths — auto vs manual — laid out in a
  table on the first screen. Six runnable subsections covering every
  combination:
    - §0.1 DefaultTools via `Permissions(...)` (auto)
    - §0.2 WebTools via `tools=[web_search_tool(), web_fetch_tool()]`
    - §0.3 RAG via `TextSplitter` -> `VectorStore.add_documents` ->
      `vector_search_tool(store)`
    - §0.4 Handoffs via `handoff_tool` + `HandoffCoordinator`
    - §0.5 Custom `StructuredTool` from scratch
    - §0.6 Fully-loaded runner combining all of the above
    - §0.7 Rules on name collisions, invisible-denied-capabilities,
      async-tool behavior
  The existing inventory + wrapper / controls / cheat-sheet sections
  are unchanged; they now sit after the "how to use them" primer
  instead of before it.

## [3.1.2] — 2026-07-22

Patch release. Two independent fixes.

### Fixed

- **`llm_judge` correctly parses YES/NO across providers.** The judge
  parser was comparing the reply's first word to the literal string
  `"YES"`. GPT-4o answers `"YES,"` (comma-suffixed), which failed the
  equality check and marked every genuine PASS as FAIL. Claude replies
  `"YES"` without punctuation so the bug hid during local development.
  Fixed by matching `\b(YES|NO)\b` (word-boundary regex, case-
  insensitive) at the start of the reply. Handles every real shape:
  `YES`, `YES.`, `YES!`, `YES, exactly right`, `Yes.`, `yes -- reason`.
  Ambiguous replies (`Maybe`, empty string) still fail closed.
- Regression test `test_llm_judge_parses_various_verdict_shapes`
  covers 8 YES shapes, 5 NO shapes, and 4 ambiguous replies.

### Added

- **`agentx_dev.Tools` is a one-stop tools namespace.** Users no
  longer need to remember which module each tool lives in:

  ```python
  from agentx_dev.Tools import (
      StandardTool, StructuredTool,
      AsyncStandardTool, AsyncStructuredTool,
      web_search_tool, web_fetch_tool,
      vector_search_tool, handoff_tool,
      DefaultTools, Permissions,
  )
  ```

  Both this form and the pre-existing `from agentx_dev import X`
  form coexist. Implementation uses PEP 562 module `__getattr__`
  and `__dir__` so re-exports are lazy (no import cost for modules
  the caller doesn't touch) and show up in IDE autocomplete +
  `dir(agentx_dev.Tools)`.

## [3.1.1] — 2026-07-21

Second batch of 3.1 features + a full docs + brand pass.

### Added

**Streaming through orchestration**
- `Supervisor.stream()` / `AsyncSupervisor.astream()` emit
  `plan_start` / `plan` / `dispatch` / `subtask_result` /
  `synthesize_start` / `final` / `completion` events.
- `HandoffCoordinator.stream()` / `.astream()` emit `invoke` /
  `completion` / `handoff` / `final` / `result` events per hop.
- Legacy `.run()` / `.arun()` refactored to consume the streams (no
  code duplication).

**Prompt optimization — `Compiled`**
- New `agentx_dev.Compiler` module.
- `Compiled(runner_factory, trainset, ...)` iteratively refines a
  runner's `system_addendum` against the eval harness. Half of
  DSPy's power at a tenth of the surface.

**Anthropic Batch API**
- `Claude.batch(requests)` submits many prompts at Anthropic's 50%-off
  batch rate, polls to completion, returns results in submission order.
- Per-request error dicts on failure; token usage funneled into
  `TokenUsage` so cost tracking stays a single source of truth.

**Vector store adapters — `agentx_dev.VectorStores`**
- `ChromaVectorStore`, `QdrantVectorStore`, `PgVectorStore` — same
  public shape as the in-memory `VectorStore` (`add` / `search` /
  `delete` / `clear` / `__len__` / `embeddings`).
- `vector_search_tool()` and `SemanticMemory` accept any of them.
- SDK imports lazy; friendly `ImportError` when the underlying SDK
  is missing.

**Trace viewer (`viewer/`)**
- Self-hosted single-page app that reads `FileHook` JSONL and renders
  a timeline with type/text filters, summary sidebar, JSON drill-down.
- Works from `file://`, no server required.

**Docs site (`host/`)**
- Full editorial dark-first design system (JetBrains Mono headings,
  Inter body, `#B8FF3E` electric-lime accent).
- Command palette (`Cmd+K`) with keyboard navigation and live search.
- Hero code snippet with hand-tinted syntax highlighting.
- Reading progress bar, breadcrumbs, header anchor links.
- Sidebar sliding active marker, collapsible groups.
- Code copy buttons, language labels.
- Right-rail auto-TOC with `IntersectionObserver` scrollspy.
- Dark/light theme toggle, persisted.
- Cache-busted assets so edits land on refresh without hard-reload.

**Brand identity (`brand/`)**
- Full brand kit: 5 SVG assets (`mark`, `mono`, `wordmark`, `logo-full`,
  `app-icon`), `BRAND.md` strategy doc, rendered brand-kit HTML deck.
- Copy audit dropped "small" (weak) and "LangChain" references from
  all marketing surfaces.
- Favicon wired into docs + trace viewer.

**Test suite (`tests/`)**
- Restored + expanded pytest suite: 127 tests passing (3 skipped for
  absent optional SDKs).
- Coverage: parser + all `AgentType` variants, `ToolRegistry`
  (dispatch / dup-guard / circuit-breaker / timeout), Permissions
  (capability gating + sandbox + traversal), budgets (cost / rate /
  retry / non-retryable HTTP), runner loop (streaming + output_schema
  + chat history), embeddings + `VectorStore` + `SemanticMemory`,
  handoffs (bounded hops + history sanitization), evals harness
  (all assertion helpers + JSON case loaders), vector-store adapter
  shape conformance.

**Docs (`docs/`)**
- Full docs tree (34 pages), including new pages for:
  vector store adapters, prompt optimization, batch API, trace viewer,
  and a **use-cases** landing (13 concrete scenarios with runnable code).
- Rewrote **Tools** page to enumerate every built-in tool with args,
  return shape, capability flag, and use-case guidance.
- Rewrote **Agents** page to cover all four orchestration
  architectures (Solo / Supervisor / Handoffs / Compiled) with
  decision trees, worked examples, and cheat sheet.
- **Agentic RAG chatbot** as use case §13 — multi-query decomposition,
  parallel retrieval, self-critique, citations, user memory.

**Examples**
- `examples/agentic_rag_demo.py` — the runnable version of the
  agentic RAG use case. Auto-seeds a KB if none exists, `--demo` flag
  runs a 3-turn scripted session proving user-notes recall works.

**Package**
- `[chroma]`, `[qdrant]`, `[pgvector]`, `[dev]` extras added.
- `[anthropic]` bumped to `>=0.36` (Batch API + prompt cache).

### Fixed

- `AgentRunner._iter_run` in `bind_tools_natively=True` mode uses a
  minimal system prompt instead of the AgentType template so the
  ReAct `action/action_input` scaffold no longer fights the native
  tool interface. Previously produced JSON-blob answers under GPT.
- `HandoffCoordinator._sanitize_history_for_next_agent` strips tool
  and function role messages between hops so tool_call_ids from a
  previous agent don't leak into the next model's call (OpenAI 400).
- Docs site marker positioning uses double-`requestAnimationFrame` +
  `document.fonts.ready` so the sidebar accent bar lands on the
  correct row even on a cold font cache.
- Primary hero CTA color uses `#doc .hero-cta a.primary` selector to
  outrank `#doc a` link styling (previously rendered lime-on-lime
  and was invisible).

### Notes

- Package version bumped from `3.0.6` to `3.1.1`. The 3.1.0 release
  did not ship publicly — 3.1.1 is the first 3.1-tagged PyPI release
  and includes both batches of features.

## [3.1.0] — internal only (commits 52840e7)

First batch of 3.1 features. Committed but not released to PyPI.
Merged into 3.1.1 for the public release.

### Added
- Anthropic prompt caching (`Claude(enable_prompt_cache=True)`).
- Parallel per-turn tool dispatch in `AgentRunner`
  (`bind_tools_natively=True`, `parallel_tool_workers`).
- Semantic memory (`SemanticMemory`, embeddings-backed retrieval).
- RAG core (`Embeddings`, `HashEmbeddings`, `OpenAIEmbeddings`,
  `VectorStore`, `VectorHit`, `vector_search_tool()`).
- Agent-to-agent handoffs (`HandoffRequest`, `handoff_tool`,
  `HandoffCoordinator`, `HandoffResult`).
- Evals harness (`EvalCase`, `EvalRunner`, `EvalReport`, 7 assertion
  helpers, JSON case loader, `python -m agentx_dev.Evals run` CLI).
- `TokenUsage.cache_hit_ratio` property.

## [3.0.6] — 2026-03 (baseline)

Security hardening baseline (SSRF guard on `web_fetch`, HMAC-signed
persistent state, scrubbed subprocess env, path sanitizer,
`permissions.json` mode 0o600, ReDoS guard on `grep`,
`invoke`/`ainvoke` accept bare strings and message lists).
