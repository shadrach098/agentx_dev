# Getting started

30 minutes from install to a working agent that can read your files, run
Python, and answer questions.

## Install

Requires Python 3.10+.

```bash
pip install agentx-dev            # OpenAI provider ships in the base install
pip install agentx-dev[anthropic] # adds Claude support
pip install agentx-dev[mcp]       # adds MCP client (stdio / sse / http)
pip install agentx-dev[otel]      # OpenTelemetry adapter
pip install agentx-dev[rag]       # Chroma, Qdrant and pgvector stores
pip install agentx-dev[excel]     # spreadsheets in Media (pandas, openpyxl)
pip install agentx-dev[all]       # everything above
```

The vector-store clients (Chroma, Qdrant, psycopg) come with `[rag]`, and pandas with `[excel]`; the in-memory
`VectorStore` needs nothing. Install into the same Python that runs your code (`python -m pip install
"agentx-dev[rag]"`); `pip` and `python` can point at different installs on one machine.

## Or use the terminal app: `agentx`

The same install gives you `agentx`, a coding assistant for your terminal. No code to write: run it in a project
folder and ask for changes. It reads files, edits them and runs commands there, and asks you before it changes
anything.

```bash
pip install "agentx-dev[anthropic]"     # the [anthropic] part is for Claude; GPT needs only agentx-dev
export ANTHROPIC_API_KEY=sk-ant-...      # or OPENAI_API_KEY (Windows: setx ANTHROPIC_API_KEY sk-ant-...)
cd my-project
agentx                                   # or: agentx --model claude-opus-5-5
```

What it can do: approval prompts and modes, team jobs with helpers working in parallel on the models you choose,
skills and MCP servers, project notes it remembers, pictures (it can look at screenshots), `/voice` to speak
instead of typing, and typing while it works. Start with [agentx: zero to hero](agentx/zero-to-hero.md), a step-by-step guide; every detail is in [the full reference](guides/cli.md).

## Providers — you get both

The framework is provider-agnostic. Two chat-model classes ship
in-tree: `GPT` (OpenAI) and `Claude` (Anthropic). Same agent code,
same tool code, either provider — pick per runner or per use case.

Set whichever key you have in the environment (or both):

```bash
# macOS / Linux
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...

# Windows PowerShell
$env:OPENAI_API_KEY = "sk-..."
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Everywhere the docs use `Claude()`, you can swap in `GPT()` and the
code still works. Any subclass of `BaseChatModel` drops in the same
way — see [chat models](concepts/models.md) for adding your own
provider (Ollama, Bedrock, Together, etc.).

## Your first agent — 5 lines

**With Claude:**

```python
from agentx_dev import AgentRunner, AgentType, Claude

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])
result = runner.invoke("What is 12 * 47? Reason step by step.")
print(result.content)
```

**With GPT:**

```python
from agentx_dev import AgentRunner, AgentType, GPT

runner = AgentRunner(model=GPT(), agent=AgentType.ReAct, tools=[])
result = runner.invoke("What is 12 * 47? Reason step by step.")
print(result.content)
```

Only line 1 and line 3 change — everything else about the framework
is identical. No tools yet; the model just thinks and answers. Let's
give it something to do.

## Add a tool

A tool is a Python function the agent can call. The simplest form is
`StandardTool` for a single-string argument:

```python
from agentx_dev import AgentRunner, AgentType, Claude, StandardTool
# Or: from agentx_dev import AgentRunner, AgentType, GPT, StandardTool

def get_weather(city: str) -> str:
    return f"It's 22C and sunny in {city}."

weather = StandardTool(
    func=get_weather,
    name="get_weather",
    description="Get the current weather for a city.",
)

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[weather])
# Swap Claude() for GPT() -- everything else stays the same.
result = runner.invoke("What's the weather in Paris?")
print(result.content)
```

For multiple typed arguments, use `StructuredTool` with a Pydantic schema —
covered in detail in [Tools](concepts/tools.md).

## Give it file access

Instead of hand-rolling read/write tools, the framework ships **DefaultTools**
under a permission system:

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions
# Or: from agentx_dev import AgentRunner, AgentType, GPT, Permissions

runner = AgentRunner(
    model=Claude(),                # or GPT() -- same everything else
    agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True,
        write_files=True,
        list_directories=True,
        execute_python=True,
        allowed_paths=["./workspace"],
    ),
)
result = runner.invoke(
    "Create ./workspace/notes.md with a bulleted list of the planets."
)
print(result.content)
```

Denied capabilities aren't even registered — the agent can't call them
because they don't appear as tools. See [Permissions & sandbox](concepts/permissions.md).

## Mental model

Everything the framework does is built on this loop:

```
┌────────────────────────────────────────────────────────────┐
│  1. Build system prompt from AgentType template + tools    │
│  2. Append user query                                      │
│  3. Ask the model for a response                           │
│  4. Parse the response                                     │
│     ├── If Final_Answer: return AgentCompletion            │
│     └── If tool call: dispatch tool, append result, GOTO 3 │
└────────────────────────────────────────────────────────────┘
```

Six things surround that loop:

- **[Models](concepts/models.md)** — Claude, GPT, or any subclass of `BaseChatModel`. Handles retries, rate limits, cost budgets, token counting.
- **[Tools](concepts/tools.md)** — Python callables the LLM can invoke, wrapped as `StandardTool` or `StructuredTool` (async variants too). The tool registry does dispatch, caching, circuit-breaking, and observability.
- **[Agent types](concepts/agents.md)** — Prompt templates (ReAct, Chain-of-Thought, Zero-Shot, Few-Shot, Instruction-Tuned) that shape how the model reasons and what its response looks like.
- **[Permissions](concepts/permissions.md)** — Capability flags (`read_files`, `execute_python`, etc.) that decide which `DefaultTools` get registered. Bounded by `allowed_paths` for filesystem sandboxing.
- **[Memory](advanced/memory.md)** — Where past conversation lives. Simple list, sliding window, token-limited, importance-based, summary-based, or semantic (embedding-based retrieval).
- **[Sessions](guides/sessions.md)** — Serialize an entire conversation to disk so the agent picks up where it left off after a restart.

## What to read next

- **If you want to build an application** — jump to a task guide:
  - [File-editing agent](guides/file-agent.md)
  - [Structured output](guides/structured-output.md)
  - [Streaming](guides/streaming.md)
  - [Sessions](guides/sessions.md)
- **If you want to understand internals** — read the concepts:
  - [Chat models](concepts/models.md)
  - [Tools](concepts/tools.md)
  - [Agents](concepts/agents.md)
- **If you want production features** — read advanced:
  - [Memory strategies](advanced/memory.md)
  - [RAG](advanced/rag.md)
  - [Multi-agent](advanced/supervisor.md)
  - [Evals](advanced/evals.md)
  - [Production controls](advanced/production-controls.md)

## Runnable examples

Every feature has a working demo under `examples/`:

| File | What it shows |
|---|---|
| `chatbot_example.py` | Multi-turn chat with memory + tool use |
| `file_agent_demo.py` | Read / write / run code |
| `function_calling_demo.py` | Structured output extraction |
| `mcp_demo.py` | End-to-end MCP server integration |
| `orchestration_demo.py` | Single agent / orchestrator / Supervisor side-by-side |
| `supervisor_codebase_analysis_demo.py` | Supervisor analyzing its own source |
| `v3_1_features_demo.py` | Prompt cache, parallel tools, RAG, handoffs, evals |
| `v3_1_comprehensive_demo.py` | All 3.1 features in one multi-agent pipeline |
