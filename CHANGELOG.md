# Changelog

All notable changes to `agentx-dev` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semver](https://semver.org/).

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
