# Supervisor long-term memory (a vector store the planner and helpers read, and the Supervisor writes)

Status: design, approved section by section on 2026-10-08. Target release: 3.6.0 (unreleased, additive).
Builds on: `2026-10-05-ask-the-operator-design.md` (operator answers, `OperatorChannel`) and
`2026-10-04-supervisor-subagents-design.md` (helpers, `SpawnPolicy`).

## 1. Problem

Every Supervisor run starts from nothing:

- A fact you gave once ("our competitors are Notion, Obsidian, Coda") is asked for again on the next
  run, because `ask_user` answers live only for the run that got them.
- Earlier research is not available to the planner or the helpers: "compare our competitors" repeats
  work a previous run already finished.
- The memories the framework has (`ConversationMemory`, `SummaryMemory`, `SemanticMemory`) store chat
  turns and attach to one runner. The Supervisor takes no memory at all, so there is nowhere to hand
  it a store of facts.
- The vector stores (the in-memory `VectorStore`, Chroma, Qdrant, pgvector) already share one
  contract (`add`, `search`, `delete`, `clear`, `__len__`) and persist, so the storage part exists.

## 2. Goals and non-goals

Goals:

1. `Supervisor` and `AsyncSupervisor` take `memory=<any store with that contract>`.
2. The planner and every dispatched step automatically get the few memory items relevant to their
   own text, as a "FROM MEMORY" block. The model never has to remember to look.
3. The Supervisor writes by itself: each operator answer, and each completed run's final answer.
4. An exact repeat of a question you already answered is answered from memory: no prompt, no
   question slot.
5. Works with `ask_user` off, sync and async, persistent mode and the 3.6 sub-agents.
6. With `memory=None`, behavior is identical to today.

Non-goals (not in this release):

- Automatic expiry of old entries, summarizing or compacting old results, fuzzy reuse of stored
  answers, per-user namespaces (use a separate store), and a `recall` tool.
- Memory for `delegate` helpers. They see only the `task` their parent passes in.
- Replacing `ConversationMemory` and friends. They stay as they are.

## 3. Public API

### 3.1 New Supervisor arguments

```python
Supervisor(model=..., agents=..., memory=None, memory_top_k=4, memory_min_score=0.2, memory_write=True)
AsyncSupervisor(model=..., agents=..., memory=None, memory_top_k=4, memory_min_score=0.2, memory_write=True)
```

| Argument | Default | Meaning |
|---|---|---|
| `memory` | `None` | Any object with callable `add` and `search` (the vector store contract). `None` is off. Anything else without them raises `TypeError` at construction. Keeping it persistent is the store's job. |
| `memory_top_k` | `4` | Items per lookup. `0` turns recall off (writes still happen). Must be `>= 0`. |
| `memory_min_score` | `0.2` | Matches scoring below this are dropped. Tune per embedding: about 0.5 for OpenAI embeddings, about 0.1 for `HashEmbeddings`. |
| `memory_write` | `True` | `False` makes the store read-only (a shared or curated store). |

### 3.2 What is written (when `memory_write=True`)

1. **Each operator answer**, when it is received. Text `Q: <question>\nA: <answer>`, id
   `operator_answer:<first 16 hex of sha1 of the normalized question>` (normalized as the channel
   does: trimmed, spaces collapsed, case-folded), metadata
   `{"kind": "operator_answer", "qkey": <normalized question>, "question": ..., "answer": ..., "source": <agent>, "ts": <ISO UTC>}`.
   A newer answer to the same question replaces the old entry (the store overwrites an existing id).
   An answer that came from memory is not written again.
2. **Each run's final answer, only when the run's outcome is `done`.** Text
   `Task: <task>\nResult: <final answer cut at 2,000 characters>`, id
   `run_result:<first 16 hex of sha1 of the normalized task>` (the newest result for the same task
   replaces the older one), metadata `{"kind": "run_result", "task": <task>, "ts": <ISO UTC>}`.
   Stuck, partial, out-of-time, out-of-budget, no-plan and stopped-before-planning runs are never
   written, and neither is a run whose synthesis was cut off by the budget (the "Stopped: ..." stub)
   or came back empty.

Facts you add yourself with `store.add([...])` (no metadata, or metadata without a known `kind`)
are read like any other and shown as `[note]`.

### 3.3 What is read

The block (`FROM MEMORY`) is built from `store.search(query, top_k=memory_top_k,
min_score=memory_min_score)`:

```
FROM MEMORY (saved from earlier runs; may be out of date, so check anything that matters with your tools):
- [operator answer, 2026-10-05] Q: Which three competitors should I compare? A: Notion, Obsidian, Coda
- [earlier result, 2026-10-04] Task: Compare pricing pages ... Result: ...
- [note] Our fiscal year starts in April.
```

- Each item is cut at 600 characters and the whole block at 3,000.
- An item whose `qkey` matches an operator answer already given in this run is left out (it is already
  in the OPERATOR ANSWERS block).
- No hits means no block and no event.

| Moment | Search query | Goes into |
|---|---|---|
| Before planning, and before each recovery round | the original task | the planning prompt, after the operator answers |
| Before each dispatched step (registered specialists and planner-defined helpers) | that step's own query | that step's context, after the operator answers |
| Synthesis | none | nothing |
| `delegate` helpers | none | nothing |

The planner prompt gains one line when its block is non-empty and the ask option is offered: "If the
FROM MEMORY block already answers a question, do not ask the operator."

### 3.4 Exact-answer reuse

Before a question reaches the operator, the channel looks in memory for an operator answer to the
exact same question (same normalization as dedupe). If found, the reply is that answer:

- the operator is not prompted and no question slot is used;
- the answer joins this run's OPERATOR ANSWERS block;
- the record has `from_memory: True` and the `answer` event has `"from_memory": True` (the key is
  present only when true, so existing record and event shapes do not change);
- it is not written back.

Only an exact match is reused. A similar but different question still goes to the operator. If the
lookup misses (a crowded store may not rank the entry in the top few), the operator is asked as
today. To be asked again about a fact, delete its id from the store.

### 3.5 Events and result

- Stream event `{"type": "memory", "stage": "plan" | "step", "hits": <int>}`, emitted the first
  time a distinct query injects something in a run (a repeat of the same query is served from the
  per-run cache and does not emit again), at the places the operator events are drained.
- `SupervisorResult.memory: List[dict]`: what this run wrote, `{"kind", "id", "text"}` with `text`
  cut at 80 characters. Empty when nothing was written.
- With `verbose=True`, `[memory]` lines print the same moments.

## 4. Design

### 4.1 `RunMemory` (new module `agentx_dev/SupervisorMemory.py`)

One per run, the only place that talks to the store. It mirrors `OperatorChannel`.

- `RunMemory.create(memory, *, top_k, min_score, write, verbose) -> Optional[RunMemory]`
  (validates; `None` when `memory` is `None`).
- `recall(query, stage) -> str` and `arecall(...)`: the block or `""`. Results are cached per
  normalized query for the run, so the same text is searched once. The store call runs in
  `asyncio.to_thread` in the async variant.
- `lookup_answer(question) -> Optional[str]` (and async twin): `search(question, top_k=5, min_score=0.0)`,
  then the first hit with `kind == "operator_answer"` and `qkey == normalized question`; its
  `answer` metadata is the reply.
- `remember_answer(...)`, `remember_result(...)`: build the id, text and metadata above and call
  `store.add([text], ids=[id], metadata=[meta])`; append to `written`.
- Every store call is wrapped: an exception is logged (`logger.warning`) and treated as "no hits" or
  "not written". A memory problem never fails a run.
- Constants: `MEMORY_RESULT_CHARS = 2000`, `MEMORY_ITEM_CHARS = 600`, `MEMORY_BLOCK_CHARS = 3000`,
  `KIND_ANSWER = "operator_answer"`, `KIND_RESULT = "run_result"`.
- It holds an event buffer with `drain()` guarded by its own lock (never held during a store call).

### 4.2 Wiring in the Supervisors

- `stream()` / `astream()` create the `RunMemory` next to the operator channel
  (`self._run_memory`, reset every run) and give it to the channel (`channel.memory`).
- Planning prompts use a new `_planning_task(user_task)` = `_task_for_model(user_task)` plus the
  planner memory block. `_synthesize` keeps `_task_for_model`, so memory never reaches synthesis.
  `result.query` stays the original task.
- The step context is built where the operator answers are added today (`_with_answers` in the sync
  `_run_plan`; `_run_subtask` in the async class), with the step's own query as the search text.
  Retries reuse the already-built context.
- `OperatorChannel._begin` runs the exact-answer lookup after the in-run dedupe check and before the
  per-agent and global limits, under the ask lock. `aask` does the store call with
  `asyncio.to_thread`.
- Answer writes are queued by the channel and flushed right after the ask lock is released
  (`ask` synchronously; `aask` via `asyncio.to_thread`), so a slow store never holds the lock.
- The run-result write happens at the end of `stream()` / `astream()`, after synthesis, when
  `_supervisor_outcome(...) == "done"`. `SupervisorResult.memory` is filled from `written`.
- Memory works with `ask_user` unset: recall, step injection and the run-result write still run; the
  operator-answer features simply have nothing to do.

### 4.3 Persistent mode and sub-agents

- Recovery planning gets the planner block (it goes through `_plan_once`). Recovery rounds and
  replacement helpers do not write; the run-result write is once, at the end.
- Planner-defined helpers and registered specialists get a step block like any step. `delegate`
  helpers do not (non-goal).

### 4.4 Errors and edge cases

| Situation | Result |
|---|---|
| `memory=None` | No change anywhere. |
| `memory` lacks `add` or `search` | `TypeError` at construction. |
| Store raises on search | Logged; no block; the run continues. |
| Store raises on add | Logged; nothing recorded in `result.memory`; the run continues. |
| `memory_top_k=0` | No recall (and no `memory` events); writes and exact-answer reuse still run. |
| `memory_write=False` | Nothing is written; recall and exact-answer reuse still run. |
| Hit with unknown or missing `kind` | Shown as `[note]`. |
| Run ends `stuck`, `partial`, `out_of_time` or `out_of_budget` | The run result is not written; operator answers already written stay. |
| Two parallel async steps with the same query | One search (per-run cache). |
| Operator answers a question already in memory but the lookup missed | Stored under the same id, so it replaces the old entry. |

## 5. Testing

Use the real in-memory `VectorStore` with `HashEmbeddings` (offline), plus a small fake store that
raises, and the scripted-model helpers in `tests/subagent_helpers.py`.

- `RunMemory`: block format and labels (`[operator answer, date]`, `[earlier result, date]`,
  `[note]`); `memory_top_k`, `memory_min_score`, the 600-character item cut and the 3,000-character
  block cut; no duplicates of this run's operator answers; per-run query cache; validation
  `TypeError`; every store failure is swallowed.
- Writes: operator answer id and metadata, overwrite on a repeat; run result only on `done`; the
  2,000-character cut; `memory_write=False`; `result.memory` contents.
- Reads in the Supervisors: block in the first planning prompt, recovery prompts and dispatched
  steps (both DAG and legacy branches, retries), never in synthesis or `delegate` helpers; the planner
  "do not ask" line only when the block is non-empty; `result.query` unchanged; `memory` events.
- Exact-answer reuse: no prompt, no slot, `from_memory` on record and event, joins the OPERATOR
  ANSWERS block, not re-written; a similar-but-different question still asks; a lookup miss asks.
- The same for `AsyncSupervisor` (store calls off the loop; writes flushed after the lock is
  released; parallel steps).
- `memory=None`: prompts, events and results identical (the existing suite stays green).

## 6. Documentation

- `docs/guides/sub-agents.md`: a "Long-term memory" section (what is read and written, the arguments,
  exact-answer reuse, how to refresh a fact, the safety notes).
- `docs/cookbook/patterns.md`: pattern 33, a file-backed store (`VectorStore.save` / `load` with
  `HashEmbeddings` or `OpenAIEmbeddings`) and a Chroma example, plus `memory_write=False` for a
  curated read-only store.
- `docs/cookbook/faq.md`, `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `README.md`,
  `CHANGELOG.md` (3.6.0 entry), `examples/subagents_demo.py` gains `--memory <file>`, and
  `host/data.js` is regenerated.

## 7. Risks and parked items

- **Prompt injection through stored results.** A result built from a web page can carry text that is
  replayed into later prompts. The block is labelled reference data, results are capped, and
  `memory_write=False` is the answer for untrusted input. The guide says so.
- **Stale results.** Run results and answers are dated and labelled "may be out of date"; there is no
  automatic expiry (parked).
- **Cost.** Each lookup is one embedding request plus a search (planning, each step, each exact-answer
  check). With `OpenAIEmbeddings` that adds up on long plans; `memory_top_k=0` skips recall.
- **Noisy matches.** `HashEmbeddings` is noisy; the 0.2 default is conservative and tunable.
- **Exact-match misses in a crowded store** fall back to asking the operator (safe, a little more
  interruption).
- **Privacy.** Answers are stored in your file or database; never answer with a password, key or
  token.
