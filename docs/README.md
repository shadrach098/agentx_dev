# AgentX documentation

`agentx_dev` is two things in one install: a Python framework for building LLM agents (gated tools, skills, a
Supervisor that runs independent steps in parallel, sub-agents, RAG, memory, MCP, evals; GPT and Claude), and
`agentx`, a coding assistant for your terminal built on it, whose team jobs run helpers in parallel. Nothing sprawls.
For the terminal app, start with [the agentx guide](guides/cli.md).

This documentation walks you from your first agent to a production
multi-agent system. Read in order if you're new; jump to the section
you need if you already know the framework.

---

## Reading order

If you're new, work top-to-bottom in these three phases:

**Phase 1 — get running (30 minutes)**
1. [Getting started](getting-started.md) — install, first agent, mental model
2. [Use cases](use-cases.md) — see yourself in the framework: 12 concrete scenarios with runnable code

**Phase 2 — build things (1-2 hours)**
2. [Chat models](concepts/models.md) — GPT, Claude, retries, budgets, streaming
3. [Tools](concepts/tools.md) — StandardTool, StructuredTool, async variants
4. [Agents](concepts/agents.md) — agent types, parsers, the runner loop
5. [Permissions & sandbox](concepts/permissions.md) — DefaultTools, capability model
6. [Observability](concepts/observability.md) — event hooks, OTel

**Phase 3 — ship real applications (3+ hours)**

Task guides:
- [Build a file-editing agent](guides/file-agent.md)
- [Get typed output back](guides/structured-output.md)
- [Stream tokens as they arrive](guides/streaming.md)
- [Send images, PDFs and audio](media/overview.md) *(3.4)* — its own section:
  [chat models](media/chat-models.md), [agents](media/agents.md),
  [reference](media/reference.md)
- [Persist conversations](guides/sessions.md)
- [Connect an MCP server](guides/mcp.md)
- [Configure from YAML](guides/yaml-config.md)
- [Author your own tools](guides/custom-tools.md)
- [The `agentx` terminal assistant](guides/cli.md) *(3.7)* — approvals and
  modes, AGENTX.md, sessions, team jobs, helper models, project memory
- [Skills](guides/skills.md) *(next release)* — short guides the agent reads on
  demand; built-in skills, your own, and installing from a GitHub link
- [Upgrading between releases](guides/upgrading.md)

Advanced topics:
- [Memory strategies](advanced/memory.md) — 5 built-in memories + SemanticMemory
- [RAG with vector_search](advanced/rag.md) — embeddings, VectorStore, retrieval
- [Vector store adapters](advanced/vector-store-adapters.md) — Chroma / Qdrant / pgvector
- [Prompt caching](advanced/prompt-caching.md) — Anthropic ephemeral cache
- [Parallel tool calls](advanced/parallel-tools.md) — bind_tools_natively
- [Supervisor orchestration](advanced/supervisor.md) — decompose-then-dispatch (streaming events)
- [Agent-to-agent handoffs](advanced/handoffs.md) — HandoffCoordinator (streaming events)
- [Evals harness](advanced/evals.md) — EvalRunner, assertions, CI
- [Prompt optimization](advanced/prompt-optimization.md) — `Compiled` wrapper
- [Batch API](advanced/batch-api.md) — Anthropic 50%-off batch endpoint
- [Trace viewer](advanced/trace-viewer.md) — self-hosted timeline UI
- [Production controls](advanced/production-controls.md) — budgets, rate limits, circuit breakers

Reference & recipes:
- [API summary](reference/api-summary.md) — every exported symbol at a glance
- [Patterns cookbook](cookbook/patterns.md) — reusable multi-agent shapes
  (`agentx` patterns 34-37 *(3.7)*: [using the assistant](cookbook/patterns.md#34-use-the-agentx-assistant-on-a-project-37),
  [team jobs](cookbook/patterns.md#35-hand-a-big-job-to-a-team-of-helpers-37),
  [helper models](cookbook/patterns.md#36-run-team-helpers-on-several-models-37),
  [project memory](cookbook/patterns.md#37-keep-a-model-a-log-and-a-short-history-per-folder-37))
- [Troubleshooting](cookbook/troubleshooting.md) — common errors + fixes
  (see [The `agentx` command](cookbook/troubleshooting.md#the-agentx-command-37))
- [FAQ](cookbook/faq.md)

---

## Mental model in one paragraph

An **AgentRunner** owns a **model** (Claude/GPT/anything that implements
`BaseChatModel`), a set of **tools** (Python functions the LLM can call),
a **permissions** object (what the tools are allowed to touch on disk),
and an **agent type** (which prompt template shapes how the LLM reasons).
You call `runner.invoke(question)` and get back an `AgentCompletion`
that carries the final answer, every tool call the model made, and the
full working history. Everything else in the framework is either an
extension of that loop (multi-agent, streaming, sessions) or an
operational lever (memory, caching, budgets, rate limits).

## Framework at a glance

```
                    +-----------------+
                    | your query      |
                    +--------+--------+
                             v
                    +--------+--------+
                    |  AgentRunner    |<---- Permissions (sandbox)
                    |  (loop)         |<---- AgentType     (prompt template)
                    +---+----------+--+<---- Session       (persistence)
                        |          |
              +---------+          +----------+
              v                               v
        BaseChatModel                    ToolRegistry
        (GPT / Claude)                   (StandardTool / StructuredTool /
              |                          AsyncStandardTool / AsyncStructuredTool)
              |                                    |
              +---> TokenUsage                     +---> DefaultTools (file / grep / py / shell)
              +---> retry / rate-limit             +---> WebTools (search / fetch)
              +---> cost budget                    +---> vector_search (RAG)
              +---> prompt cache (3.1, Claude)     +---> handoff_tool (multi-agent)
                                                   +---> your own StructuredTool
```

## Version signposts

- **3.0.6** — security hardening (SSRF, HMAC-signed pickles, env scrubbing).
- **3.1** — prompt caching, parallel tool calls (sync), semantic memory,
  RAG (`vector_search_tool`), agent-to-agent handoffs, evals harness.
  See the [What's new in 3.1](../README.md#whats-new-in-31--power-features)
  section of the top-level README for a summary table.
- **3.2** — typed multi-agent pipelines: constructor `output_schema` on
  the runners, `SubtaskResult.output` carrying validated Pydantic
  instances through the Supervisor, structured findings threading.
  See [structured output](guides/structured-output.md).
- **3.3** — `depends_on` execution DAG for the Supervisor: topological
  ordering, a completion-driven async scheduler with `max_parallel`,
  transitive failure cascade, and `skip_when` conditional steps.
  See [supervisor](advanced/supervisor.md) and the
  [design note](design/3.3-depends-on-dag.md).
- **3.4** — media input (images, PDFs, audio) for GPT and Claude via
  `Media` and `media=`; models that adapt to each generation's
  parameter support (`adapt_params`); new Claude settings; workspace-
  rooted paths and `run_python` starting in the workspace. See
  [media](media/overview.md) and [upgrading](guides/upgrading.md).
- **Next release** — skills: `AgentRunner(skills=True)` gives an agent a
  `use_skill` tool and a short skill list; five skills ship for the default
  tools; `/skills install <github link>` in `agentx`. See the
  [guide](guides/skills.md) and [upgrading](guides/upgrading.md).
- **3.7** — the `agentx` terminal assistant: approvals and
  modes, trusted folders, `AGENTX.md`, saved sessions, team jobs with helpers
  on several models, and a work log and model per folder; plus
  `Session.stream()` and two fixes (conversations after a function-call final
  answer, `edit_file` line endings). See the [guide](guides/cli.md) and
  [upgrading](guides/upgrading.md).

## Where things live in the source

Every module in `agentx_dev/` is documented; the mapping is:

| Module | Docs |
|---|---|
| `ChatModel.py` | [Chat models](concepts/models.md) |
| `Tools.py`, `AsyncTools.py` | [Tools](concepts/tools.md) |
| `Agents/Agent.py` | [Agents](concepts/agents.md) |
| `Runner/AgentRun.py`, `AsyncAgentRun.py` | [The runner loop](concepts/agents.md#4-the-runner-loop) |
| `DefaultTools.py` | [Permissions & sandbox](concepts/permissions.md) |
| `WebTools.py` | [Web tools](guides/custom-tools.md#web-tools) |
| `Observability.py` | [Observability](concepts/observability.md) |
| `Memory.py`, `Embeddings.py` | [Memory](advanced/memory.md), [RAG](advanced/rag.md) |
| `Session.py` | [Sessions](guides/sessions.md) |
| `Skills/` (`Store.py`, `Defaults.py`, `Attach.py`, `Install.py`), `resources/skills/` | [Skills](guides/skills.md) |
| `Cache.py` | [Production controls](advanced/production-controls.md#tool-result-cache) |
| `Supervisor.py` | [Supervisor](advanced/supervisor.md) |
| `Handoffs.py` | [Handoffs](advanced/handoffs.md) |
| `Evals.py` | [Evals](advanced/evals.md) |
| `MCP.py` | [MCP](guides/mcp.md) |
| `Loader.py` | [YAML config](guides/yaml-config.md) |
| `Streaming.py` | [Streaming](guides/streaming.md) |
