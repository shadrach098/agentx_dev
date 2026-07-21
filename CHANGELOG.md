# Changelog

All notable changes to `agentx-dev` are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semver](https://semver.org/).

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
