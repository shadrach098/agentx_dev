# FAQ

## Which model should I use?

- **`claude-sonnet-4-6`** — best general-purpose. Strong tool use,
  reasonable cost, prompt caching cuts input costs 90% on repeated
  system prompts.
- **`claude-haiku-4-5`** — cheapest. Fine for simple tool-using agents
  and `llm_judge` evals.
- **`claude-opus-4-6`** — deepest reasoning. Use when quality matters
  and cost doesn't.
- **`gpt-4o`** — comparable to sonnet. Larger tool count support.
- **`gpt-4o-mini`** — cheap fallback. Prone to duplicate-call spirals —
  the framework's guards catch them.

## When should I use ReAct vs. function-calling vs. native binding?

| Mode | When |
|---|---|
| **ReAct (text mode, default)** | Most compatible; works with any model; portable across providers. Model reasons via visible Thought → Action → Observation cycles. |
| **Function-calling (`use_function_calling=True`)** | Parser output is structured — schema validation is stronger, tool-call correctness higher. Slight token overhead. |
| **Native binding (`bind_tools_natively=True`, 3.1)** | Best latency (parallel tool calls per turn), simplest prompt. Use when tools are well-described and you don't need the ReAct scaffold. |

## When should I use Supervisor vs. Handoffs?

- **Supervisor** — plan shape is knowable upfront. Fixed decompose →
  dispatch → synthesize. One planning call + N specialist runs + one
  synthesis call.
- **Handoffs** — routing depends on partial results, or specialists
  chain in loops. Peer-to-peer transfers via `handoff_tool`.

Both together: a top-level Supervisor whose specialists are themselves
handoff coordinators.

## How do I add a new provider?

Subclass `BaseChatModel` and implement `Initialize`. See the [Custom
providers section of models](../concepts/models.md#custom-providers).

## How do I mock a model for tests?

```python
from agentx_dev import BaseChatModel

class MockModel(BaseChatModel):
    def __init__(self, responses):
        self._responses = list(responses)
    def Initialize(self, messages):
        return self._responses.pop(0) if self._responses else ""

runner = AgentRunner(model=MockModel(["Paris"]), agent=AgentType.ReAct, tools=[])
```

## How do I limit total cost?

```python
llm = Claude().configure_limits(
    budget_usd=5.0,
    input_price_per_1k=0.003,
    output_price_per_1k=0.015,
)
```

Halts with `CostBudgetExceeded` when spend crosses.

## How do I persist a conversation?

`Session.start(runner) → session.invoke(...) → session.save(path)`.
See [Sessions](../guides/sessions.md).

## Can I stream tokens AND get the final `AgentCompletion`?

Yes. The last event from `runner.stream(...)` is always
`{"type": "completion", "completion": ...}`.

## Why does my agent loop forever?

Almost always: the model is calling the same tool with the same args.
The framework's duplicate-call guard warns at 3, refuses at 5. The
loop-level force-stop kicks in at 3 identical `(action, args)` pairs.
If it still loops:

- Lower `max_iterations`.
- Improve the prompt: tell the model when to stop.
- Add more tools so the model has escape routes.
- Add `strict_tool_dispatch=True` — feeds "unknown tool" errors back
  as observations instead of collapsing to implicit-final.

## Why doesn't `bind_tools_natively` work with GPT?

It does. Auto-adapts to the model. Just don't set
`parallel_tool_calls=True` on the `GPT` constructor at the model
level — it leaks into tool-less calls and OpenAI 400s. GPT defaults to
parallel tool calls when tools are present.

## How do I switch embedding models mid-project?

You can't in-place. Dim mismatch will raise. Options:

1. Reindex the whole `VectorStore` with the new embeddings.
2. Keep two stores in parallel and switch consumers over gradually.

## Is prompt caching worth it?

Yes on Claude, for any agent with:

- A system prompt over ~500 tokens.
- Tool schemas (each schema is ~50-200 tokens; multi-tool agents cache
  a lot).
- Long-running sessions where the same system + tools are reused.

Break-even after 1 cached call. Expect 70-90% cost cut on subsequent
calls.

## Can I use MCP servers with GPT?

Yes. `MCPClient` produces `AsyncStructuredTool` instances that any
runner can consume, regardless of provider.

## What happens if two agents want the same tool name?

- User tool + DefaultTool with the same name → `AgentRunner`
  constructor raises `TypeError` at build.
- Two custom tools with the same name in the registry → the second
  overwrites the first at register time. Rename one.

## How do I run multiple agents in parallel?

- `AsyncSupervisor(sequential=False)` for concurrent sub-tasks.
- Multiple `AsyncAgentRunner` instances via `asyncio.gather`.
- One `AgentRunner` with `bind_tools_natively=True` + multiple tools
  per turn — dispatches on a thread pool.

## How do I extend the AgentType templates?

Subclass `AgentFormatter`:

```python
from pydantic import BaseModel
from agentx_dev import AgentFormatter, AgentRunner

class MyParser(BaseModel):
    action: str
    action_input: str

MY_PROMPT = """You have these tools:
{tools}

Names: {tool_names}

User: {user_input}

Respond as JSON: {{"action": "<tool>", "action_input": "<args>"}}
"""

runner = AgentRunner(
    model=Claude(),
    agent=AgentFormatter(prompt=MY_PROMPT, Agent=MyParser),
)
```

The template MUST contain `{tools}`, `{tool_names}`, `{user_input}`.

## Where should I put `AGENTX.md`?

Repo root or workspace root. The framework auto-discovers it and adds
a pointer to the model's system prompt telling it to read via
`read_path` before acting. See the shipped `AGENTX.md` at the repo
root for a template.

## Is this LangChain-compatible?

Not directly, but conceptually familiar:

- `BaseChatModel.invoke` ≈ LangChain LCEL invoke.
- `StructuredOutputRunnable` ≈ LangChain `with_structured_output`.
- `AgentRunner.tools` ≈ LangChain agent tools.
- `Session` ≈ LangChain chat memory + history persistence.

The `|` composition operator on `StructuredOutputRunnable` matches
LCEL for pipeline construction.

## Where's the source of truth for each feature?

Every feature has its own module under `agentx_dev/`. The docs cross-
reference file paths where relevant. See [API summary](../reference/api-summary.md)
for the mapping.
