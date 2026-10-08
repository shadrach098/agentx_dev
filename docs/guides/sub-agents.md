# Sub-agents

A Supervisor can create its own helpers, the way Claude Code launches a sub-agent for a side job. There are two ways, and both go through one set of limits that you set once.

- **The planner defines a helper inline.** When no specialist you registered fits a sub-task, the planner writes a new one into the plan: a name, its own instructions, and the tools it needs. The Supervisor builds it, runs the step on it, and discards it when the run ends.
- **A specialist hands off part of its own work.** While working, a specialist can call a `delegate` tool. A fresh agent does the side job in a clean context and a short summary comes back, so the specialist's own context stays small.

Nothing is on by default, except in persistent mode (below).

## Quick start

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Supervisor, SpawnConfig

model = GPT(model="gpt-4o-mini")     # or Claude(...); any chat model works

# A specialist you register yourself: it can read and list files under ./workspace.
explorer = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    tools=[],
    permissions=Permissions(
        read_files=True, list_directories=True,
        allowed_paths=["./workspace"], workspace="./workspace",
    ),
    system_addendum="You read files in the workspace and report what is in them. You cannot browse the web.",
    verbose=False,
)

supervisor = Supervisor(
    model=model,
    agents={"explorer": ("Reads and lists files in ./workspace", explorer)},   # your specialists
    spawn_config=SpawnConfig(
        enabled=True,
        capabilities={"web", "files_read"},   # the ceiling: what a spawned agent may use
        allowed_paths=["./workspace"],        # its file sandbox
        max_spawns=6,                         # sub-agents per run, plans and delegations together
    ),
)

result = supervisor.run("Compare the pricing pages of our three competitors")
for sub in result.spawned:
    print(sub["name"], sub["origin"], sub["tools"], sub["outcome"])
```

`AsyncSupervisor` takes the same `spawn_config=`. Spawned steps run in the same parallel batches as every other step.

The task above names no competitors. Add `ask_user=True` and the Supervisor asks you for them (see [Asking the operator](#asking-the-operator)).

The planner chooses between your registered agents and new helpers using only each agent's name and description, so describe what each one does and what it cannot do (see [pattern 31 in the cookbook](../cookbook/patterns.md) for `Specialist`, `when_to_use` and a human-in-the-loop example).

A runnable version of this is in the repo: `python examples/subagents_demo.py` (it creates `./workspace/competitors.md` on the first run, accepts your own task as an argument, and takes `--persistent` to add recovery rounds and a shared deadline).

## What the planner writes

A plan step can carry its own agent instead of naming one:

```json
{"id": "s2", "query": "Compare the three pricing pages",
 "new_agent": {"name": "pricing_analyst",
               "instructions": "You compare SaaS pricing. Return a table: plan, price, limits, source URL.",
               "tools": ["web_search", "web_fetch"]}}
```

- `instructions` are free-form: the planner tells the helper who it is and what to return. The framework always adds its own rule: return the real data and never invent it.
- `tools` name tools the ceiling allows. Anything else is dropped and the planner is told what was dropped.
- Later steps reuse the helper by name (`{"agent": "pricing_analyst", ...}`) without defining it again. Repeating an identical definition also reuses it for free. A different definition under the same name gets a suffix (`pricing_analyst_2`).
- A name that matches a specialist you registered runs that specialist and ignores the definition.
- Names starting with `__` and names like `delegate_1` are reserved; a definition that uses one is dropped and the planner is asked to fix the plan.
- A helper exists for one run. When the run ends, your supervisor's registry is exactly as it was.

## The ceiling

The planner's plan is model output, which can be steered by the user's text. So you decide the most a helper may ever do, once, and nothing the planner or a helper writes can raise it.

| `SpawnConfig(...)` | Default | What it does |
|---|---|---|
| `enabled` | `False` | Master switch. |
| `capabilities` | `None` | Preset words a helper may use: `web` (search and fetch), `files_read` (read, list, find, grep), `files` (also write and edit), `code` (`run_python`), `delete`. Setting this or `tools` turns on ceiling mode. |
| `tools` | `None` | A pool of your own tool objects a helper may pick from by name. |
| `allowed_paths` | `["./workspace"]` | The file sandbox for `files_read`, `files`, `code` and `delete`. |
| `max_spawns` | 3 (6 in ceiling mode) | Sub-agents per run, planner spawns and delegations together. |
| `max_depth` | `1` | A helper can't spawn its own helpers. Raise it to allow deeper trees. |
| `max_iterations` | `15` | How many steps each helper the Supervisor creates may take. The helper is told its limit so it can finish before it runs out. |
| `approver` | `None` | Optional `(SpawnRequest) -> bool`. In ceiling mode it is an extra gate, not required. |
| `auto_spawn`, `auto_spawn_allowed_caps` | | The older approval flow, used only when neither `capabilities` nor `tools` is set. |

### Where the step limit is set

A step is one turn of an agent's loop (a model reply, usually followed by a tool call). The limit lives in two places, depending on who built the agent:

| Agent | Set the limit with |
|---|---|
| A specialist you registered with the Supervisor | `AgentRunner(..., max_iterations=N)` when you build it |
| A helper the Supervisor creates (a planner's `new_agent` or a `delegate` call) | `SpawnConfig(max_iterations=N)` |

The Supervisor itself has no step limit: it plans, dispatches and synthesizes. What bounds it is the number of steps in the plan, `max_subtask_retries` and, in persistent mode, the `Persistence` limits (`max_minutes`, `max_replans`, the cost budget).

When a helper runs out of steps its outcome is `iteration_limit`, and the Supervisor retries it (once by default, see `max_subtask_retries`) with a note saying it ran out, what it did, and to look things up less and answer from what it has. If helpers keep hitting the limit, raise `SpawnConfig(max_iterations=...)` or narrow what the step asks for.

Inside the ceiling, nobody is asked for approval, so a long unattended run never stalls. A request outside it is clipped, not fatal: the helper is built with what is allowed. If a helper asked for tools and none are allowed, the step fails with `spawn refused: no usable tools`.

Your own tools join the pool like this:

```python
from agentx_dev import SpawnConfig, StandardTool

def lookup(query: str) -> str:
    return f"results for {query}"

lookup_tool = StandardTool(func=lookup, name="lookup", description="Search our internal wiki")
config = SpawnConfig(enabled=True, tools=[lookup_tool], capabilities={"web"})
```

## Delegation

Every specialist of a Supervisor with a ceiling set (`capabilities=` or `tools=`, or the persistent default below) gets a `delegate` tool for the duration of the run (it is removed afterwards, so your runners are untouched). A legacy config with neither (the 3.5 `auto_spawn`/`approver` flow) gives specialists no `delegate`; the planner can still define helpers, through that approval flow.

```
delegate(task, instructions="", tools=[])  ->  the sub-agent's answer
```

- The helper sees only `task`, not the specialist's conversation. A thin `task` gives a thin result, so the specialist is told to put every needed fact in it.
- The answer comes back as the tool result, cut at 4,000 characters with a `[truncated]` marker. Only the outcome and size are kept on the run record (`result.spawned`); the answer is returned to the specialist, so it appears in that specialist's own output.
- When the ceiling clipped some of the requested tools, the tool result ends with `[note: these tools were not granted: ...]`, so the specialist knows what the helper could not use.
- A refusal comes back as plain text (`delegation refused: spawn limit reached; do this yourself`). It is information, not an error.
- A helper that gives up or crashes comes back as a tool error that starts `[delegate failed: stuck]` (or `error`, `out_of_time`, ...). The specialist's own stuck logic sees it and can retry with different instructions or do the work itself. A failed delegation never ends the specialist's run.

You can also give a standalone runner the same ability:

```python
from agentx_dev import AgentRunner, AgentType, SpawnConfig

researcher = AgentRunner(
    model=model, agent=AgentType.ReAct, tools=my_tools,
    delegation=SpawnConfig(enabled=True, capabilities={"web"}),
)
result = researcher.invoke("Survey the market. Hand each competitor to a helper.")
print(researcher.spawned)      # name, origin, tools, dropped, outcome, chars for this run
```

In the async runner, several `delegate` calls in one model turn run at the same time when the runner dispatches a turn's tool calls together (`bind_tools_natively=True`).

## Persistent mode

With `persistence=Persistence(...)` on a Supervisor and no `spawn_config`, spawning is on with a safe ceiling: `capabilities={"web", "files_read"}`, `max_spawns=6`. Pass `spawn_config=SpawnConfig(enabled=False)` to turn it off, or your own config to change it.

- Helpers inherit `persistence`: they get the same stuck handling, ledger and compaction, and they share the one run deadline and cost cap.
- A recovery plan can define a replacement helper and mark the failed step as replaced (`"replaces": ["s1"]`), so a stuck specialist can be swapped for a differently-instructed one.
- Code execution, writes and deletes are not in the default ceiling. Allow them explicitly in your own `SpawnConfig`.

See [Long-running agents](long-running-agents.md).

## Asking the operator

"Compare the pricing pages of our three competitors" never names the competitors. With `ask_user` set, the Supervisor can ask you instead of guessing.

```python
supervisor = Supervisor(model=model, agents={"explorer": ("Reads files in ./workspace", explorer)},
                        spawn_config=SpawnConfig(enabled=True, capabilities={"web"}),
                        ask_user=True)
```

- **The planner asks first.** When the task leaves out a fact it cannot assume, it replies with questions instead of a plan. The framework asks them (up to `max_questions`, default 3), adds your answers to the task, and plans again. There is no extra model call when it plans straight away.
- **Every agent can ask mid-run.** Specialists, planner-defined helpers and `delegate` helpers get an `ask_user(question, context="")` tool for the run (removed afterwards, so your runners are untouched). The answer goes to the agent as `[operator] ...` and to every step dispatched later. Helpers also get a line in their instructions telling them to ask rather than guess; your registered specialists learn about the tool from its description.
- **After you answer, the planner is told to do the work.** Once you have answered something, every planning prompt (the replan, and any recovery round) adds: do not plan a step whose job is only to ask for more information; find what is still unknown with tools (give the helper the tools it needs, such as `web`) or state the assumption. A partial answer, such as one URL where three competitors were needed, should lead to research, not to another round of questions.
- **A helper can only look things up with the tools it is given.** The helper-creation instructions tell the planner that a helper with no tools can only reason and cannot fetch anything, and name `web` when your ceiling allows it. The `ceiling` only caps what a helper may use; the plan decides what it asks for.
- **An agent may ask once.** Each agent (by name) gets one question per run, and its tool description says to search with its tools first. The planner is exempt (it may ask several questions in its one round). The second question from the same agent gets "no more questions available; proceed on a stated assumption", so one helper cannot spend the whole shared budget clarifying.
- **One question budget per run**, shared by all of them. An identical question is asked once; a repeat gets the first answer and uses no slot. A question counts against the budget once it is put to you, whether or not you answer it. The one reply that is not remembered is `no_channel` (for example a terminal that is busy), so the same question can be asked again once the channel is free.
- **No answer is not a failure.** If nobody can answer (headless, your function raised, it timed out, you sent an empty reply, the budget is spent) the agent is told to proceed on a stated assumption and say what it assumed.

`ask_user` takes:

| Value | Meaning |
|---|---|
| `None` / `False` (default) | Off. Nothing changes. Use this for a chatbot or backend unless you wire a channel. |
| `True` | The built-in asker. In a notebook (Jupyter, VS Code, Colab) it shows the input box under the cell; in a terminal it uses `input()`; when stdin is not a terminal it opens the controlling terminal (`CONIN$` / `/dev/tty`, 300 s default timeout); headless it returns no answer at once. |
| a function `(question) -> str \| None` | Your own channel: a chat UI, a websocket, a queue. `AsyncSupervisor` also accepts an `async` function; a plain one runs in a worker thread. |

```python
def ask_via_chat(question: str) -> str | None:
    session.send(question)                       # your channel
    return session.wait_for_reply(timeout=120)   # None = no answer

Supervisor(model=model, agents=agents, ask_user=ask_via_chat, max_questions=3, ask_timeout=120)
```

Your function receives the question as a string. When the asker (the planner or an agent) gave a one-line context, it is appended as `(context: ...)` after the question. Returning `None`, an empty string or whitespace means no answer. A reply is cut at 2,000 characters.

`ask_timeout` bounds the wait for one answer (a function that does not return in time counts as no answer). It is not enforced on the notebook input box, where a person is looking at the box and the Interrupt button stops the run. In persistent mode, time spent waiting on you does not count against `max_minutes`.

**What the built-in prompt looks like.** Every prompt starts with who is asking, so you can see which agent needs you (the planner shows as `[planner]`, a helper by its name), then the question, and the agent's one-line context if it gave one:

```
[pricing_researcher] 🙋 needs your input
  question: Which three competitors should I compare?
  (context: the task names none)
> 
```

The same header appears in a notebook's input box, in a terminal, and on the controlling terminal. A console that cannot print the hand (some legacy Windows code pages) gets the same header without it. A function you pass as `ask_user` receives only the question text, so add your own label in your chat UI.

What you can see: `question` and `answer` stream events (after the answer; your function is the live channel for a UI), `[ask]` lines with `verbose=True`, and `result.asked` (source, question, answered, reason, deduped; the answer text is not kept). Treat answers as facts: if your function forwards raw end-user text, treat it as untrusted input to the run, like the task itself.

Things to know:

- **Interrupt stops the run.** `KeyboardInterrupt` (the notebook Interrupt button, Ctrl-C) is not treated as "no answer"; the operator pressed stop.
- **`AsyncSupervisor`: terminal asks do not block the loop.** Outside a notebook the built-in asker reads in a background daemon thread, so other tasks keep running while you type and a cancelled run does not keep the process waiting for Enter. In a notebook, which thread asks depends on the agent. The planner and async specialists (`AsyncAgentRunner`) under `AsyncSupervisor` ask on the event-loop thread, where ipykernel's `input()` works, and other tasks pause while you type. A sync `AgentRunner` specialist under `AsyncSupervisor` (or a sync runner that makes parallel native tool calls) asks from a worker thread, where ipykernel's `input()` may not show the box or may misbehave. In a notebook, prefer async specialists, or verify your setup. Asks are one at a time either way.
- **An `async def` function needs async specialists.** The planner and spawned helpers on an `AsyncSupervisor` can await it. A sync specialist (a plain `AgentRunner`) asks through the synchronous path, which cannot await an async function: its question gets reason `error` and the agent proceeds on an assumption. With sync specialists, pass a plain function.
- **A reader can outlive a timeout or a cancelled run.** If a built-in read on a terminal or the controlling terminal times out, or the run is cancelled while it waits, the reader thread keeps waiting for a keystroke. Later built-in asks in that process get no answer (`no_channel`) until it returns, so the next question cannot take the line you typed for the first.
- **Your own `ask_user` tool wins.** A runner (or a helper built from your `SpawnConfig` tool pool) that already has a tool named `ask_user` keeps it and is not given the framework's.
- **Never answer an agent's question with a password, key or token.** An agent can be steered by what it reads (a web page, a file) into asking you for something sensitive. Answer only what the task needs. Control characters (escape sequences, bell, carriage return) in a question are removed before the built-in asker prints it to a terminal.

For a standalone `AgentRunner` that needs a human step, give it `agentx_dev.ask_human_tool()`: the same asker as a tool called `ask_human` (arguments `question` and optional `context`), so it works in a notebook's input box as well as a terminal. `ask_human_tool(prompt_prefix="[agent]", ask_timeout=None)`.

## Long-term memory

Every run starts from nothing: a fact you gave once ("our competitors are Notion, Obsidian, Coda") is asked for again next time, and earlier research is not available to the planner. Give the Supervisor a vector store and it remembers.

```python
from agentx_dev import HashEmbeddings, Supervisor, VectorStore

store = VectorStore(embeddings=HashEmbeddings())          # any store with add() and search()
supervisor = Supervisor(model=model, agents=agents, ask_user=True, memory=store)
```

`memory` takes anything with callable `add` and `search`: `VectorStore`, `ChromaVectorStore`, `QdrantVectorStore`, `PgVectorStore`. Anything else raises `TypeError` at construction. Keeping the store persistent is yours to do: the in-memory `VectorStore` has `save(path)` and `VectorStore.load(path, embeddings)` (call `save` after the run); Chroma (with a `persist_directory`), Qdrant and pgvector persist themselves. With `memory=None` (the default) nothing changes. `AsyncSupervisor` takes the same arguments.

| Argument | Default | Meaning |
|---|---|---|
| `memory` | `None` | The store. `None` is off. |
| `memory_top_k` | `4` | Items per lookup. `0` turns recall off (writes and exact-answer reuse still run). Must be `>= 0`. |
| `memory_min_score` | `0.2` | Matches scoring below this are dropped. Tune per embedding: about `0.5` for `OpenAIEmbeddings`, about `0.1` for `HashEmbeddings`. |
| `memory_write` | `True` | `False` makes the store read-only: nothing is saved, recall and exact-answer reuse still run. |

**What it saves** (when `memory_write=True`):

- **Each operator answer**, as `Q: <question>` / `A: <answer>` under the id `operator_answer:<16 hex of sha1 of the normalized question>` (trimmed, spaces collapsed, case-folded). A newer answer to the same question replaces the old one. An answer that came from memory is not saved again.
- **Each run's final answer, only when the run's outcome is `done`**, as `Task: <task>` / `Result: <answer>` under `run_result:<16 hex of sha1 of the normalized task>`. The result is cut at 2,000 characters, and the newest result for the same task replaces the older one. A run that ends in any outcome other than `done` saves nothing (and neither does a run whose synthesis was cut off by the budget).

**What it reads.** Before planning (and before each recovery round) and before each dispatched step (specialists and planner-defined helpers), the Supervisor searches the store with that text and adds a block to the prompt:

```
FROM MEMORY (saved from earlier runs; may be out of date, so check anything that matters with your tools):
- [operator answer, 2026-10-05] Q: Which three competitors should I compare? A: Notion, Obsidian, Coda
- [earlier result, 2026-10-04] Task: Compare pricing pages ... Result: ...
- [note] Our fiscal year starts in April.
```

At most `memory_top_k` items, each cut at 600 characters, the whole block at 3,000. Synthesis and `delegate` helpers get no block (a `delegate` helper sees only the task its parent passes). An operator answer already given in this run is left out (it is in the OPERATOR ANSWERS block). When the block is not empty and `ask_user` is set, the planner is also told not to ask what the block already answers. Facts you add yourself with `store.add([...])` are read like any other and shown as `[note]`, as is any item without a known `kind`.

**Exact-answer reuse.** Before a question reaches you, the Supervisor looks for a stored operator answer to the exact same question (same normalization as the in-run dedupe). If it finds one, you are not prompted, no question slot is used, and the answer joins the run's OPERATOR ANSWERS. The record in `result.asked` and the `answer` event carry `"from_memory": True` (the key is present only when true). A repeat of a question already answered from memory in the same run also shows `deduped: True` and `from_memory: True` on its record. Only an exact match is reused; a similar question still asks you. This needs `ask_user`; without it recall and run results still work.

**To be asked again about a fact, delete its id** from the store: `store.delete([entry["id"]])` for an `entry` in `result.memory` (below), or `store.delete([answer_id(question)])` with `from agentx_dev.SupervisorMemory import answer_id`.

What you can see:

| Where | What |
|---|---|
| `{"type": "memory", "stage": "plan" \| "step", "hits": int}` stream event | Emitted the first time a distinct query injects something in a run (a repeat of the same query is served from the per-run cache and does not emit again); not emitted when nothing matched or `memory_top_k=0`. |
| `result.memory` | What this run saved: `[{"kind": "operator_answer" \| "run_result", "id", "text"}]`, `text` cut at 80 characters. Empty when nothing was saved. |
| `[memory]` lines with `verbose=True` | The same moments. |

Things to know:

- **Stored results are replayed into later prompts.** A result built from a web page or a file can carry text the page's author wrote. Use `memory_write=False` (a curated, read-only store) for supervisors that handle untrusted input.
- **Never answer an agent with a password, key or token.** Answers are stored in your file or database and shown to later runs.
- **Cost.** Each lookup is one embedding request plus a search: planning, each dispatched step, and each exact-answer check. With `OpenAIEmbeddings` that adds up on long plans; `memory_top_k=0` skips recall.
- **A memory problem never fails a run.** A store that raises on `search` or `add` is logged and skipped.
- **No expiry.** Items are dated and labelled "may be out of date"; delete an id, or use a fresh store, to forget.
- **Noisy matches.** `HashEmbeddings` is noisy; raise `memory_min_score` if irrelevant items appear.
- See [pattern 33](../cookbook/patterns.md) for file-backed, Chroma and read-only examples.

## Watching it

`supervisor.stream(task)` includes:

| Event | Meaning |
|---|---|
| `{"type": "spawn", "name", "origin": "plan"\|"delegate", "tools", "dropped", "reused", "refused", ...}` | A helper was built, reused, or refused. |
| `{"type": "delegate_result", "name", "outcome", "chars"}` | A delegation returned. |
| `{"type": "question", "source": "planner"\|<agent name>, "question", "context"}` | A question was put to the operator (`ask_user` set). |
| `{"type": "answer", "source", "answered", "reason": None\|"no_channel"\|"declined"\|"timeout"\|"limit"\|"error"}` | The reply, or why there was none. The answer text is not in the event. `"from_memory": True` is added when the answer came from long-term memory. |
| `{"type": "memory", "stage": "plan"\|"step", "hits": int}` | Items from long-term memory were added to a prompt (`memory=` set). |

With `verbose=True` the same moments print as `[spawn]`, `[ask]` and `[memory]` lines.

## Things to know

- **The ceiling bounds tools, not behavior.** Instructions can steer a helper within what you allowed (for example to fetch a particular URL with `web`). Keep `code`, `files` and `delete` out of the ceiling for supervisors that handle untrusted input.
- **A helper without a code tool can still write code as text.** A model may answer with a script it cannot run (the ceiling stopped it from running anything; `result.spawned[i]["tools"]` shows what it had). Helpers are told never to answer with a script unless they have a code tool, and to say what they tried and what was missing; the Supervisor then reports that no data came back instead of treating the code as data.
- **Helpers fetch readable text.** The `web` helper's `web_fetch` returns pages as readable text (scripts, styles and tags removed), so prices and copy are not buried under markup. Your own `web_fetch_tool()` keeps returning the raw body unless you pass `text_only=True`.
- **Cost.** Each helper is a full agent with its own turns. `max_spawns` and `max_depth` bound the count; for unattended runs also use `Persistence` or a model cost cap (`model.configure_limits(budget_usd=..., input_price_per_1k=..., output_price_per_1k=...)`).
- **Fresh context is the point, and the catch.** A helper knows nothing you don't put in its task.
- **Tool cache.** Helpers in persistent mode run with the tool-result cache off, like any persistent agent.
- **One run at a time per instance.** A Supervisor or AsyncSupervisor keeps its run's state (the helper registry and spawn count) on the instance, so don't run two tasks on one instance at the same time; use one instance per concurrent run. The same goes for a runner built with `delegation=`.
- **The ceiling, not the parent's permissions.** A specialist's `delegate` grants tools from the `SpawnConfig` ceiling, regardless of the specialist's own `permissions=`.
- **Helpers a planner defines can't delegate** at the default `max_depth=1`; only the specialists you registered can. Raise `max_depth` to allow it.
