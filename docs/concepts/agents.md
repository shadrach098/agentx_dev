# Agents

An **agent** in AgentX is a model + a prompt template + a set of tools +
a control loop. That's a solo agent (`AgentRunner`). But "agent" also
covers *how you compose them* — Supervisor decomposes tasks top-down,
HandoffCoordinator routes between peers, Compiled optimizes any of the
above. This doc covers all of them.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](models.md) for adding other providers.

1. **Agent types** — every prompt-template flavor for solo agents,
   with "when to use / when not to use."
2. **Agent architectures** — solo / Supervisor / Handoffs / Compiled,
   with when to reach for each.
3. **Parsers** — the Pydantic shapes each template produces.
4. **The runner loop** — step-by-step.
5. **Runner modes** — text, function-calling, native-binding.
6. **The `AgentCompletion` object** — what a run returns.
7. **Chat history + streaming**.
8. **Cheat sheet** — which agent shape for which situation.

---

## 1. Agent types

Every agent type ships as an `AgentFormatter` — a prompt template
string plus the Pydantic parser class that matches what the template
asks the model to emit. Pick one via the `AgentType` namespace:

```python
from agentx_dev import AgentRunner, AgentType, Claude

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])
```

### 1.1 `AgentType.ReAct` *(default)*

- **Style:** Thought → Action → Action_Input → Observation cycles.
- **Parser:** `React_` — fields `Thought`, `action`, `action_input`.
- **What the model produces per turn:**
  ```json
  {
    "Thought": "I need to check the file first.",
    "action": "read_path",
    "action_input": {"path": "./notes.md"}
  }
  ```
- **Use when:**
  - You want **visible reasoning** so you can debug why the model
    picked a tool.
  - The task requires multi-step planning (read, then compute, then
    write).
  - You're not sure which template to use — this is a safe default.
- **Do not use when:**
  - You need the smallest possible per-turn prompt (Zero_Shot is
    leaner).
  - You want the model to just call one tool without narration.

**Worked example:**

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    max_iterations=6,
)
result = runner.invoke("Read ./workspace/data.csv, summarize the columns.")
```

The agent will `Thought`-through what it needs, call `read_path`,
`Thought` again, produce a summary, and emit `Final_Answer`.

### 1.2 `AgentType.Chain_of_Thought`

- **Style:** Reason first (multi-sentence), then act.
- **Parser:** `ChainOfThought` — fields `Thought`, `Action`, `Action_Input`
  (note capitalized `Action`).
- **Difference from ReAct:** the template emphasizes **deeper
  reasoning** — the `Thought` is expected to be several sentences of
  analysis before choosing the action.
- **Use when:**
  - The task benefits from explicit multi-step decomposition
    (math, logic puzzles, code planning).
  - You're using a strong reasoning model (Opus, o1/o3) that will
    actually use the room.
- **Do not use when:**
  - The model is small / cheap (Haiku, gpt-4o-mini) — CoT often
    hurts them.
  - The task is a straightforward tool-call ("what's the weather").

**Worked example:**

```python
runner = AgentRunner(
    model=Claude(model="claude-opus-4-6"),
    agent=AgentType.Chain_of_Thought,
    tools=[calc_tool],
)
runner.invoke(
    "Alice has 3x more apples than Bob. Bob has half of Carol's 20. "
    "How many apples does Alice have?"
)
```

### 1.3 `AgentType.Zero_Shot`

- **Style:** Minimal scaffolding — no reasoning field, just action +
  input.
- **Parser:** `ZeroShot` — fields `action`, `action_input`.
- **Use when:**
  - The task is unambiguous and requires one obvious tool call.
  - You're deploying an agent that MUST NOT emit visible reasoning
    (product agents, terse APIs).
  - You want the lowest token overhead per turn.
- **Do not use when:**
  - The task needs multi-step planning.
  - Debugging — no `Thought` field means you can't see why the model
    picked a tool.

**Worked example:**

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.Zero_Shot, tools=[weather_tool],
)
runner.invoke("weather in Tokyo?")
```

### 1.4 `AgentType.Few_Shot`

- **Style:** Includes worked example turns in the prompt so the model
  learns the target format by demonstration.
- **Parser:** `FewShot` — fields `action`, `action_input`.
- **Use when:**
  - You have a strict output format you want the model to hit
    reliably (e.g., specific JSON structure).
  - The task is niche enough that examples help more than
    instructions.
- **Do not use when:**
  - Token budget matters — examples inflate the system prompt.
  - The default ReAct examples already show the shape you want.

**Worked example:**

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.Few_Shot, tools=[extract_tool],
)
runner.invoke("Extract line items from: Joe's Diner, 2x Burger $10, 1x Fries $4")
```

### 1.5 `AgentType.Instruction_Tuned`

- **Style:** Direct commands, no reasoning field, tightly scoped
  instructions.
- **Parser:** `Instruction_Tuned_` — fields `action`, `action_input`.
- **Use when:**
  - You're using an instruction-tuned model (most GPT + Claude
    variants) and want them to just follow orders.
  - The task is procedural (call this, then that, then finish).
  - You're building deterministic pipelines (batch jobs, ETL).
- **Do not use when:**
  - The task is open-ended / creative.
  - You need visible reasoning for debugging.

**Worked example:**

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.Instruction_Tuned,
    tools=[calc_tool],
    system_addendum="Do not explain steps. Just compute and answer.",
)
runner.invoke("What is 13 * 47?")
```

### 1.6 Custom agent types

Subclass `AgentFormatter`:

```python
from pydantic import BaseModel
from agentx_dev import AgentFormatter, AgentRunner, Claude

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

---

## 2. Agent architectures

An `AgentRunner` with an `AgentType` is a **solo agent** — one model,
one prompt template, one control loop. But some tasks need more than
one persona. The framework ships four architectures:

| Architecture | Class | Best for |
|---|---|---|
| **Solo** | `AgentRunner` / `AsyncAgentRunner` | Single task, one persona, tool-using |
| **Supervisor** | `Supervisor` / `AsyncSupervisor` | Task decomposes into known sub-tasks |
| **Handoffs** | `HandoffCoordinator` | Routing depends on partial results |
| **Compiled** *(3.1)* | `Compiled` (wraps any runner) | Tune the system prompt against evals |

Decision guide:

```
Is the plan shape known upfront?
  ├─ Yes: decompose -> dispatch -> synthesize
  │      └─> Supervisor
  └─ No: routing depends on partial results
         ├─ Peers hand off mid-run (triage -> researcher -> writer)
         │  └─> HandoffCoordinator
         └─ One agent, one persona
            └─> AgentRunner
```

`Compiled` is orthogonal — it improves the prompt of *any* of the
above.

### 2.1 Solo agent — `AgentRunner`

The base case. One model, one prompt, one loop, one persona.

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
)
result = runner.invoke("Summarize ./workspace/notes.md.")
```

**Use when:**
- The task fits one persona (a researcher agent, a writer agent).
- The tool set is stable for the whole run.
- You don't need mid-task role switches.

**Async sibling:** `AsyncAgentRunner` — same shape, native async tool
dispatch, mixed sync/async tools transparent.

**Full docs:** you're already reading them — see the AgentType
section above.

### 2.2 Supervisor — top-down decomposition

`Supervisor` plans first, dispatches sub-tasks to named specialists,
then synthesizes their replies into one final answer. Three LLM calls
minimum: plan → N specialists → synthesis.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions, Supervisor,
)

llm = Claude()   # or GPT() -- same API
file_agent = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True, write_files=True, edit_files=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
)
python_agent = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    permissions=Permissions(
        read_files=True, execute_python=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
)

supervisor = Supervisor(
    model=llm,
    agents={
        "file_agent":   ("File management", file_agent),
        "python_agent": ("Python code execution", python_agent),
    },
    max_subtasks=5,
)

result = supervisor.run(
    "Scrape example.com for links via Python and save them to ./workspace/links.txt."
)
```

**Use when:**
- The task naturally splits into pieces (research + writing + format).
- Different sub-tasks need different tools / permissions.
- You want cost predictability — 1 planning call + N specialist runs
  + 1 synthesis call, no runaway routing loops.

**Do not use when:**
- Routing depends on partial results (Supervisor plans up-front).
- The task fits one specialist — use `AgentRunner` directly.

**Async variant:** `AsyncSupervisor` runs sub-tasks concurrently via
`asyncio.gather` when `sequential=False` (default). Set
`sequential=True` when later sub-tasks need earlier findings.

**Dynamic spawning:** `SpawnConfig(enabled=True)` lets the supervisor
create new specialists mid-plan when the initial catalog doesn't
cover a capability. Recognized capability keywords: `"web"`,
`"files"`, `"code"`, `"delete"`.

**Streaming:** `supervisor.stream(task)` yields `plan_start` / `plan` /
`dispatch` / `subtask_result` / `synthesize_start` / `final` /
`completion` events so UIs can render progress.

**Full docs:** [Supervisor](../advanced/supervisor.md).

### 2.3 Handoffs — peer routing

`HandoffCoordinator` lets one agent transfer control to a peer mid-run.
Modeled after OpenAI Swarm / Anthropic's routing SDK.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    HandoffCoordinator, handoff_tool,
)

llm = Claude()

triage = AgentRunner(
    model=llm, agent=AgentType.ReAct,
    tools=[
        handoff_tool("researcher", "for framework docs questions"),
        handoff_tool("writer",     "for final drafting"),
    ],
)
researcher = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[
    handoff_tool("writer", "when you have enough info"),
])
writer = AgentRunner(model=llm, agent=AgentType.ReAct, tools=[])

coord = HandoffCoordinator(
    agents={"triage": triage, "researcher": researcher, "writer": writer},
    entry="triage",
    max_hops=8,
)
result = coord.run("Draft a 200-word summary of MVCC.")
print(result.content)
print(result.hops)   # [{from, to, task, hop}, ...]
```

**Use when:**
- The routing decision depends on partial results (triage → researcher
  when it's a research question; → writer when it's a formatting task).
- Specialists loop (researcher ↔ reviewer critique cycle).
- You want the routing to be model-driven, not planner-driven.

**Do not use when:**
- The plan shape is knowable upfront (use `Supervisor` — one planning
  call is cheaper than iterative routing).
- The task fits one persona (use `AgentRunner`).

**Bounded:** `max_hops` (default 8) prevents infinite ping-pong.
Chat history sanitized between hops so tool_call_ids from one
agent's chain don't leak into another's.

**Streaming:** `coord.stream(query)` yields `invoke` / `completion` /
`handoff` / `final` / `result` events per hop.

**Full docs:** [Handoffs](../advanced/handoffs.md).

### 2.4 Compiled — optimized prompts *(3.1)*

`Compiled` wraps any runner factory and iteratively refines its
`system_addendum` against a metric scored by the eval harness. Half of
DSPy's power at a tenth of the surface area.

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    EvalCase, contains,
    Compiled,
)

def build(system_addendum=None):
    return AgentRunner(
        model=Claude(), agent=AgentType.ReAct, tools=[],
        system_addendum=system_addendum,
    )

trainset = [
    EvalCase("paris",  "Capital of France?",  [contains("Paris")]),
    EvalCase("tokyo",  "Capital of Japan?",   [contains("Tokyo")]),
    EvalCase("berlin", "Capital of Germany?", [contains("Berlin")]),
]

result = Compiled(
    runner_factory=build,
    trainset=trainset,
    iterations=3,
    candidates_per_iter=3,
).compile()

print(f"baseline: {result.baseline_score:.1%}")
print(f"best:     {result.best_score:.1%}")
print(f"addendum: {result.best_addendum}")

# Use the optimized runner -- it's still an ordinary AgentRunner.
optimized = result.runner
```

**Use when:**
- You have a stable trainset (say 5-20 eval cases).
- You're tuning style / format / refusal calibration.
- You're shipping to prod and want the system prompt to survive
  regressions.

**Do not use when:**
- You need model-capability improvements (Compile can't teach a model
  it doesn't already know).
- The addendum grows huge (that's a signal you want a custom
  `AgentType` template, not more prompt).

**Cost math:** `iterations × candidates_per_iter × len(trainset)` LLM
calls per compile. Pair with `Claude(enable_prompt_cache=True)` to
cut input cost ~90%.

**Full docs:** [Prompt optimization](../advanced/prompt-optimization.md).

### 2.5 Composing architectures

The four architectures compose. Common shapes:

- **Supervisor of Handoffs** — a top-level `Supervisor` whose
  specialists are themselves `HandoffCoordinator` instances (each
  handling a family of peers).
- **Compiled Supervisor** — wrap `runner_factory=build_supervisor` in
  `Compiled` to tune the *planner's* system prompt against evals.
- **Handoff to Solo** — `HandoffCoordinator` routing between several
  plain `AgentRunner` specialists is the most common shape (that's
  what the comprehensive demo shows).

---

## 3. Parsers

Each agent type has a matching Pydantic parser. The runner calls
`parser.from_json(response)` — if the response parses into a parser
instance, the runner extracts `action` and `action_input`. If parsing
fails, the response is treated as a plain-text final answer.

| Parser class | Emitted fields | Used by |
|---|---|---|
| `StandardParser` | `Thought`, `action`, `action_input` | fallback for custom formatters |
| `React_` | `Thought`, `action`, `action_input` | `AgentType.ReAct` |
| `ChainOfThought` | `Thought`, `Action`, `Action_Input` | `AgentType.Chain_of_Thought` |
| `ZeroShot` | `action`, `action_input` | `AgentType.Zero_Shot` |
| `FewShot` | `action`, `action_input` | `AgentType.Few_Shot` |
| `Instruction_Tuned_` | `action`, `action_input` | `AgentType.Instruction_Tuned` |

`Final_Answer` is not a parser field — it's a **special value** for
`action` that terminates the loop. The runner accepts tolerant
variants: `Final_Answer`, `Final Answer`, `final_answer`,
`finalanswer`, `FINAL_ANSWER`, `Final-Answer`.

---

## 4. The runner loop

```
                       +----------------------+
                       | build system prompt  |
                       +----------+-----------+
                                  v
                       +----------+-----------+
                       | append user_input    |
                       +----------+-----------+
                                  v
        +-------------------------+-------------------------+
        |                                                   |
        v                                                   |
  +-----+------+   final answer                             |
  | LLM call   +--------------->  return AgentCompletion    |
  +-----+------+                                            |
        |                                                   |
        | parser returns (action, action_input)             |
        v                                                   |
  +-----+------+                                            |
  | dispatch   |                                            |
  | tool via   |                                            |
  | registry   |                                            |
  +-----+------+                                            |
        |                                                   |
        | append tool result                                |
        +---------------------------------------------------+
                                  (up to max_iterations)
```

Every iteration is one LLM call. `max_iterations` caps the loop
(default 4; bump for complex tasks).

Framework guards fire automatically:

- **Duplicate-call guard** (tool layer) — warns at 3 identical calls,
  refuses at 5.
- **Loop-level force-stop** — 3 consecutive identical
  `(action, args)` pairs and the loop bails with a synthesized final
  answer from the last successful tool result.
- **Implicit-final** — when `action` is neither a known tool nor a
  Final_Answer variant, the framework treats `action_input` as the
  final answer.

---

## 5. Three runner modes

**1. Text mode (default)** — parser JSON in the assistant message.

```python
runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[calc])
# Model emits: {"Thought": "...", "action": "calculator", "action_input": {...}}
```

Most compatible; works with any model; portable across providers.

**2. Function-calling mode** — parser routed through native FC.

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[calc],
    use_function_calling=True,
)
```

Parser output is structured, so schema validation is stronger.
Slightly higher accuracy on tool-call correctness.

**3. Native binding mode (3.1)** — YOUR tools are the native FC tools.

```python
runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[calc, weather],
    bind_tools_natively=True,
    parallel_tool_workers=4,   # concurrent dispatch when the model
                               # calls multiple tools per turn
)
```

Best latency (parallel tool calls per turn), simplest prompt for the
model. The framework auto-adds a `respond` tool the model calls to end
the loop.

`bind_tools_natively=True` and `use_function_calling=True` are
mutually exclusive.

**When to use each:**

| Mode | Use when |
|---|---|
| Text mode | Most compatible, cross-provider portability |
| Function-calling | Slightly stronger tool-call correctness; still uses AgentType parser |
| Native binding | Best latency (multi-tool per turn), simplest prompt |

---

## 6. The `AgentCompletion` object

Every `runner.invoke(...)` returns an `AgentCompletion`:

```python
result.content         # str  -- final answer
result.tool_calls      # List[ToolCall] -- every dispatched call
result.steps           # List[str] -- step descriptions like "Step 1: get_weather with Paris"
result.history         # List[Dict] -- the full working_history
result.query           # str  -- the original user input
result.model           # str  -- the model class name
result.id              # str  -- uuid
result.created         # int  -- unix timestamp
result.output          # Any  -- set when output_schema= was passed
```

`ToolCall` fields:
```python
tc.name    # tool name
tc.args    # {"Input": <what the model passed>}
tc.result  # str -- the tool's return value (stringified)
```

---

## 7. Streaming events

`runner.stream(query)` yields structured step events:

```python
for event in runner.stream("What's the weather in NYC?"):
    if event["type"] == "thought":       print("Thought:", event["content"])
    elif event["type"] == "tool_call":   print("Calling", event["name"])
    elif event["type"] == "tool_result": print("Got:", event["result"])
    elif event["type"] == "final":       print("Final:", event["content"])
    elif event["type"] == "completion":  full = event["completion"]  # AgentCompletion
```

Add `stream_tokens=True` for `text_delta` events with per-token
chunks. See [Streaming](../guides/streaming.md).

---

## 8. Chat history

Pass prior turns as `chat_history`:

```python
result = runner.invoke("What about tomorrow?", chat_history=[
    {"role": "user", "content": "What's the weather in Paris?"},
    {"role": "assistant", "content": "22C and sunny."},
])
```

Or pass a message list directly — the framework pulls the last user
turn as the query and threads earlier turns as history:

```python
result = runner.invoke([
    {"role": "user", "content": "What's the weather in Paris?"},
    {"role": "assistant", "content": "22C and sunny."},
    {"role": "user", "content": "And tomorrow?"},
])
```

---

## 9. Async

`AsyncAgentRunner` has the same API but every method is async:

```python
from agentx_dev import AsyncAgentRunner, AgentType, Claude

runner = AsyncAgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[calc])
result = await runner.ainvoke("What is 12 * 47?")

async for event in runner.astream("Do X"):
    ...
```

Async supports mixed sync + async tools transparently.

---

## 10. Common gotchas

- **`use_function_calling=True` + `bind_tools_natively=True`** — raises
  at construction. Pick one.
- **Passing a list-of-messages to `invoke`** — the framework normalizes
  it. The last user turn becomes the query; earlier turns become chat
  history. System messages in the list are dropped (the runner builds
  its own).
- **`max_iterations` default is 4** — bump for complex tasks.
- **Empty `tool_calls` on completion** — the model produced a direct
  text answer (either no tools were needed, or it went straight to
  Final_Answer).

---

## 11. Cheat sheet

### Which AgentType for a solo agent?

| Situation | AgentType |
|---|---|
| First agent, unsure which type | `ReAct` |
| Deep multi-step reasoning (Opus, o1/o3) | `Chain_of_Thought` |
| Terse product agent, don't leak reasoning | `Zero_Shot` |
| Strict output format, examples help | `Few_Shot` |
| Deterministic pipeline, follow orders exactly | `Instruction_Tuned` |
| Weakly-typed model prone to spirals (gpt-4o-mini) | `ReAct` with `strict_tool_dispatch=True` |
| High-throughput multi-tool per turn | `ReAct` + `bind_tools_natively=True` |
| Custom output shape / niche format | subclass `AgentFormatter` |

### Which architecture for the whole system?

| Situation | Architecture |
|---|---|
| Single task, one persona | `AgentRunner` |
| Task decomposes into known steps (research + write + format) | `Supervisor` |
| Same as above but sub-tasks are independent | `AsyncSupervisor(sequential=False)` |
| Later sub-tasks need earlier findings | `AsyncSupervisor(sequential=True)` or `Supervisor` |
| Routing depends on partial results (triage → specialist) | `HandoffCoordinator` |
| Two agents in a critique loop | `HandoffCoordinator` with `max_hops=4` |
| Deep hierarchy | `Supervisor` whose specialists are `HandoffCoordinator`s |
| Shipping to prod, want prompt tuned to evals | wrap any of the above in `Compiled` |
