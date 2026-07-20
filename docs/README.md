# AgentX documentation

`agentx_dev` is a small, production-ready Python framework for building
LLM agents. Think LangChain, minus the sprawl.

This documentation walks you from your first agent to a production
multi-agent system. Read in order if you're new; jump to the section
you need if you already know the framework.

---

## Reading order

If you're new, work top-to-bottom in these three phases:

**Phase 1 — get running (30 minutes)**
1. [Getting started](getting-started.md) — install, first agent, mental model

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
- [Persist conversations](guides/sessions.md)
- [Connect an MCP server](guides/mcp.md)
- [Configure from YAML](guides/yaml-config.md)
- [Author your own tools](guides/custom-tools.md)

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
- [Troubleshooting](cookbook/troubleshooting.md) — common errors + fixes
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

## Where things live in the source

Every module in `agentx_dev/` is documented; the mapping is:

| Module | Docs |
|---|---|
| `ChatModel.py` | [Chat models](concepts/models.md) |
| `Tools.py`, `AsyncTools.py` | [Tools](concepts/tools.md) |
| `Agents/Agent.py` | [Agents](concepts/agents.md) |
| `Runner/AgentRun.py`, `AsyncAgentRun.py` | [The runner loop](concepts/agents.md#the-runner-loop) |
| `DefaultTools.py` | [Permissions & sandbox](concepts/permissions.md) |
| `WebTools.py` | [Web tools](guides/custom-tools.md#web-tools) |
| `Observability.py` | [Observability](concepts/observability.md) |
| `Memory.py`, `Embeddings.py` | [Memory](advanced/memory.md), [RAG](advanced/rag.md) |
| `Session.py` | [Sessions](guides/sessions.md) |
| `Cache.py` | [Production controls](advanced/production-controls.md#tool-result-cache) |
| `Supervisor.py` | [Supervisor](advanced/supervisor.md) |
| `Handoffs.py` | [Handoffs](advanced/handoffs.md) |
| `Evals.py` | [Evals](advanced/evals.md) |
| `MCP.py` | [MCP](guides/mcp.md) |
| `Loader.py` | [YAML config](guides/yaml-config.md) |
| `Streaming.py` | [Streaming](guides/streaming.md) |
