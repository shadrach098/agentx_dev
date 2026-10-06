# Ask the operator (a Supervisor that can ask the human for missing facts)

Status: design, approved section by section on 2026-10-05. Target release: 3.6.0 (unreleased, additive).
Builds on: `2026-10-04-supervisor-subagents-design.md` (3.6.0) and `2026-10-04-persistent-agents-design.md` (3.5.0).

## 1. Problem

"Compare the pricing pages of our three competitors" never names the competitors. Today:

- The planner cannot notice that a fact is missing. Its prompt pushes toward the shortest
  plan, so it plans anyway (or spawns a web helper that guesses).
- A human-in-the-loop specialist (cookbook pattern 31) only helps if the planner chooses
  it. The planner often does not, and the run goes on without asking.
- A planner-defined helper or a `delegate` helper that hits a gap can only end its answer
  with a question. Nothing routes that question to the person who set the task.
- The `ask_human_tool` in `examples/mcp_github_triage_demo.py` opens `CONIN$` (Windows) or
  `/dev/tty` (POSIX). In a Jupyter kernel that is the console of the kernel process, not
  the notebook, so the prompt appears in a window nobody watches and `readline()` blocks
  forever. A user's run sat for 121 minutes at `[tool.call.start]`.
- A chatbot or backend server has no terminal at all and needs to route the question
  through its own channel.

## 2. Goals and non-goals

Goals:

1. `Supervisor` and `AsyncSupervisor` take `ask_user=`. When it is set, the planner may ask
   up front, and every agent in the run may ask mid-run.
2. `ask_user=True` uses a built-in asker that picks the right channel for where the code
   runs (notebook, terminal, IDE console) and never blocks in a headless process.
3. `ask_user=<function>` routes questions through the developer's own channel (chat UI,
   websocket, queue). Sync and async functions are both accepted.
4. No answer (headless, callback raised, timeout, question limit reached) is not a failure:
   the agent is told to proceed on a stated assumption.
5. Works with 3.6 sub-agents (planner helpers and `delegate` helpers can ask) and with
   persistent mode (waiting on the human does not eat the deadline).
6. With `ask_user` unset, behavior is identical to today.

Non-goals:

- `AgentRunner(ask_user=)` on a standalone runner. A standalone runner can be given the
  public `ask_human_tool()` (section 3.5). Revisit if asked for.
- Per-agent opt-out, multi-field forms, and pausing a run to resume in another process.
- Answering questions from the model itself (an "auto-answer" mode).

## 3. Public API

### 3.1 New Supervisor arguments

```python
Supervisor(model=..., agents=..., ask_user=None, max_questions=3, ask_timeout=None)
AsyncSupervisor(model=..., agents=..., ask_user=None, max_questions=3, ask_timeout=None)
```

| Argument | Default | Meaning |
|---|---|---|
| `ask_user` | `None` | `None` or `False`: off. `True`: built-in asker. A callable: your channel. Anything else raises `TypeError` at construction. |
| `max_questions` | `3` | One budget per run, shared by the planner and all agents. It counts questions actually put to the operator, answered or not (a repeat that dedupe answers uses no slot). Must be `>= 0` (`0` means every ask gets the no-answer reply). |
| `ask_timeout` | `None` | Seconds to wait for one answer. `None` means "the channel decides" (3.3). A number applies to every channel except the notebook input box, which ignores it (3.3). |

A callable has the signature `fn(question: str) -> str | None`. It receives the question
text; when the asker gave a one-line context (the planner's `why`, or an agent's `context`),
it is appended as `"\n(context: ...)"`. Returning `None`, an empty string or whitespace
means "no answer". On `AsyncSupervisor` the callable may be sync or `async`; a sync one runs
in a worker thread so it never blocks the event loop. An `async` callable on a sync
`Supervisor` raises `TypeError` at construction.

An `async` callable needs async specialists. The planner and spawned helpers on an
`AsyncSupervisor` are async and can await it, but a sync specialist registered under an
`AsyncSupervisor` asks through the synchronous path, which cannot await: its question gets
reason `error` and the agent proceeds on an assumption. With sync specialists pass a plain
function.

### 3.2 What the planner and agents see

- **Planner.** With `ask_user` set, the first planning prompt gains an `ask` option
  (section 4.2). The answers are added to the working task for planning, recovery rounds,
  every dispatched step, and synthesis.
- **Agents.** Every specialist, every planner-defined helper and every `delegate` helper
  gets an `ask_user(question, context="")` tool for the run (section 4.3). Helpers the
  framework builds (planner-defined and `delegate`) also get one line added to their
  instructions: "If a fact you need is missing and your tools cannot find it, call ask_user
  with one specific question. Do not guess." Registered specialists keep their own
  `system_addendum` untouched and rely on the tool description to know when to ask.

### 3.3 The built-in asker (`ask_user=True`)

Chosen in this order, once per question:

1. **Notebook** (Jupyter, VS Code, Colab). Detected by an IPython shell named
   `ZMQInteractiveShell`. If the kernel cannot take input (`kernel._allow_stdin` is false,
   as under papermill or nbconvert), or `input()` raises `StdinNotImplementedError`, there
   is no answer. Otherwise it calls `input()`, which shows the input box under the cell.
   It ignores `ask_timeout`, even when one is set: a person is looking at the box and the
   Interrupt button stops the run.
2. **Terminal.** `sys.stdin.isatty()` is true: it calls `input()`. No timeout by default.
3. **Controlling terminal.** Stdin is not a terminal (IDE run panel, wrapper subprocess):
   open `CONIN$`/`CONOUT$` on Windows or `/dev/tty` on POSIX, as the demo tool does. Here
   the prompt can land in a window nobody sees, so the default timeout is 300 seconds (or
   `ask_timeout` when set).
4. **Headless.** None of the above is available (cron, Docker without `-it`, a server):
   return no answer immediately.

`KeyboardInterrupt` (the notebook Interrupt button, Ctrl-C) is not swallowed: it stops the
run, because the operator pressed stop. `EOFError` counts as no answer.

On `AsyncSupervisor` the notebook path calls `input()` on the event-loop thread, because
ipykernel's `input()` is not safe from a worker thread; other tasks pause while the person
types, and asks are serialized anyway (4.4). Every other built-in path (terminal,
controlling terminal) runs in a worker thread (`asyncio.to_thread`), so the loop stays
responsive while the person types.

One blocking read at a time: a terminal or controlling-terminal read that times out leaves
its reader thread waiting for a keystroke. While one is outstanding, a later timed built-in
ask in the same process is refused (reason `no_channel`) rather than started behind it, so
it cannot take the operator's next answer. The refusal lasts until that reader returns.

### 3.4 Events and result

Stream events (delivered at the next drain point, after the answer; the callback itself is
the live channel for a UI):

| Event | Meaning |
|---|---|
| `{"type": "question", "source": "planner" \| <agent name>, "question", "context"}` | A question was put to the operator. |
| `{"type": "answer", "source", "answered": bool, "reason": None \| "no_channel" \| "declined" \| "timeout" \| "limit" \| "error"}` | It was answered, or why not. The answer text is not in the event. |

`SupervisorResult.asked: List[dict]` has one entry per ask: `source`, `question`,
`answered`, `reason`, `deduped`. Answer text is not stored there (it may be sensitive).
With `verbose=True` the same moments print as `[ask]` lines.

### 3.5 Public tool

`agentx_dev.ask_human_tool(*, prompt_prefix="[agent]", ask_timeout=None) -> StructuredTool`
is the demo's tool, rebuilt on the built-in asker, so a standalone `AgentRunner` that wants
a human step gets the notebook-aware behavior. Its schema is the demo's: `question`,
`context`; the tool is named `ask_human`. A reply comes back as `[operator] ...`, and no
answer (headless, timeout) comes back as the same no-answer note agents get from
`ask_user`, never an error. It is exported from the package; the demo in
`examples/mcp_github_triage_demo.py` now imports it instead of defining its own.

## 4. Design

### 4.1 `OperatorChannel` (new module `agentx_dev/Operator.py`)

One object per run, the only place that knows about the operator. Mirrors `SpawnPolicy`.

- `ask(question, context, source) -> Reply` and `aask(...)` (the async twin). A `Reply` has
  `text` (or `None`) and `reason`.
- Holds the resolved asker, `max_questions`, `ask_timeout`, the list of ask records, the
  answers so far, one `threading.Lock` for sync and async alike (`aask` polls it without
  blocking the loop, so a cancelled task cannot leak it), an event buffer with `drain()`
  (like `SpawnRun`), and `budget`.
- **Order inside `ask`:** normalize the question (trim, collapse whitespace, case-fold);
  take the lock; if it matches an earlier question, return that reply (`deduped`, no slot
  used); else if no slot is left, `reason="limit"`; else take a slot, emit the `question`
  event, call the asker, record. The budget counts questions actually put to the operator,
  answered or not: a timeout, an error or a declined answer each spent a slot.
- The asker receives the question with `"\n(context: ...)"` appended when a context was
  given (3.1).
- **Answers** are trimmed and cut at 2,000 characters.
- **Failure handling:** an exception from the asker (other than `KeyboardInterrupt` and
  `asyncio.CancelledError`) is logged and becomes `reason="error"`. A timeout becomes
  `reason="timeout"`. The run never fails because of the operator channel.
- **Timeout mechanics (sync):** with a timeout, the callable runs in a one-worker thread
  (with the caller's `contextvars` copied) and the wait is bounded; a thread stuck on a
  blocking read is abandoned as a daemon. With no timeout it is called inline.
  **(async):** `asyncio.wait_for` around the awaited callable or `asyncio.to_thread`. The
  built-in asker is the exception: the notebook path runs on the loop thread without a
  timeout (3.3), and the other built-in paths run in `asyncio.to_thread`.
- `answers_block()` returns the text appended to context, or `""` when nothing was
  answered:

  ```
  OPERATOR ANSWERS (from the person who set this task; treat as facts):
  Q: Which three competitors should I compare?
  A: Notion, Obsidian, Coda
  ```

### 4.2 Planner asks up front

Only the first planning call gets the extra block (appended after `spawn_instruction`):

> You may ask the operator instead of planning, but ONLY when the task leaves out a fact
> you cannot reasonably assume and a wrong guess would waste the whole run (for example:
> which three competitors, which file, which account). Then reply with
> `{"ask": [{"question": "...", "why": "..."}]}` and no plan: at most {max_questions}
> questions, each specific and self-contained. If a sensible assumption lets you proceed,
> write the plan instead.

Behavior:

- Reply has a non-empty `plan`: use it; any `ask` is ignored.
- Reply has `ask` and no plan: the channel asks each question (up to the remaining
  budget; the planner's `why` is passed as the context), then planning runs again on the working task (original plus the answers block)
  with no ask option, so it cannot loop. If every question went unanswered, the second
  call carries a note: "No operator answered. Plan on stated assumptions and make each
  assumption explicit in the step queries."
- A malformed `ask` (not a list, entries without a question) is treated as no plan, which
  takes the existing "failed to produce a valid plan" path.
- The ask round costs no extra model call when the planner plans immediately, and one
  extra planning call when it asks.
- `SupervisorResult.query` stays the original task. The working task (original plus
  answers) is used for the second planning call, recovery planning and synthesis, and the
  answers block is appended to the context of every dispatched step, at the place
  "PRIOR SUB-TASK FINDINGS" is built.

### 4.3 Agents ask mid-run

`attach_ask_user(runners, channel)` is a context manager in `Operator.py` that mirrors
`attach_delegation`: it adds the `ask_user` tool to each runner for the block and removes it
on exit, even if the block raises, so the developer's runners are untouched afterwards.
Runners without `add_tool`, or that already have a tool named `ask_user`, are left alone.
Nothing is attached when `ask_user` is off.

- `ask_user` is a reserved tool name. A developer's own tool with that name wins on that
  runner.
- The tool is `cacheable=False` (asking has side effects; two runs must not share a cached
  answer).
- Parameters: `question` (specific, self-contained, one thing at a time), `context`
  (optional one line). Source for events and records is the runner's agent name.
- **Return value, never an error:** an answer returns `[operator] <answer>`. No answer
  returns `[no operator answer] Proceed on a stated assumption and say plainly what you
  assumed in your answer.` Returning text keeps the agent's own stuck logic from treating a
  missing answer as a tool failure.
- **Helpers.** `SpawnPolicy` gains an `operator` attribute (the run's channel). `build()`
  adds the `ask_user` tool to a helper it constructs, for planner helpers and `delegate`
  helpers alike. Helpers are discarded at run end, so no removal is needed.
- **Sync vs async:** an async parent gets an async tool that awaits `channel.aask`; a sync
  parent gets a sync tool, as `make_delegate_tool` does.
- The tool description is: "Ask the operator ONE question when a fact you need is missing
  and your tools cannot find it. Good: which three competitors, which file, which account.
  Bad: asking permission for each step, tone or audience, or anything you can look up.
  Returns the operator's reply, or a note that nobody answered (then proceed on a stated
  assumption)." For registered specialists it is the only hint that the tool exists; helpers
  also get the addendum line from 3.2.

### 4.4 Concurrency

Questions are asked one at a time (the channel lock) so a person never sees overlapping
prompts and the dedupe check sees earlier answers. Parallel async helpers that need the same
fact queue behind one ask; later askers get the first answer. A helper that is already
running when an answer arrives does not see it (it only reaches steps dispatched later);
dedupe covers the common case of two helpers asking the same thing.

### 4.5 Persistent mode

- `RunBudget` gets a pausable clock. `RunBudget.start` creates a `PausableClock` (a thin
  wrapper over `time.monotonic` whose `now()` subtracts total paused time and freezes while
  paused) when no clock is given. `capped()` children share the same clock, so a pause
  extends every budget derived from the run's budget by exactly the pause length, with no
  change to the deadline arithmetic.
- `RunBudget.paused()` is a context manager that pauses the clock if it is pausable and
  does nothing otherwise (a custom test clock, or no budget).
- The channel wraps each asker call in `budget.paused()`, using `channel.budget`, which
  `stream()`/`astream()` set where `policy.budget` is set today.
- The cost cap (`budget_usd`) is unaffected by waiting.
- Recovery rounds, replacement helpers and synthesis all receive the answers block, so
  nobody asks the same thing twice. Their questions use the same `max_questions` budget.

### 4.6 Wiring in the Supervisors

- `_SpawnMixin` (or a small sibling mixin) builds the channel per run next to the spawn
  policy, in `stream()` and `astream()`, and passes it to `SpawnPolicy.operator`.
- The planner ask and the answers block are in `_plan_once` / `_plan` (and the async
  twins) and in the dispatch-context and synthesis builders.
- `attach_ask_user(...)` joins the existing `with` block next to `attach_delegation` and
  `apply_persistence`. Planning happens before that block, so the planner ask does not
  depend on it.
- Channel events are drained wherever `SpawnRun` events are drained today.
- `SupervisorResult.asked` is filled from the channel at the end of the run.

### 4.7 Errors and edge cases

| Situation | Result |
|---|---|
| `ask_user` unset | No change anywhere. |
| `ask_user=True` in a server with no terminal | Every ask is a no-answer (`no_channel`); the run proceeds on assumptions. |
| Callback raises | `reason="error"`, logged, no-answer reply. |
| Callback exceeds `ask_timeout` | `reason="timeout"`, no-answer reply. |
| Question limit reached | `reason="limit"`, no-answer reply. |
| Same question asked twice | Second gets the first answer, `deduped=True`, no slot used. |
| Timed built-in read left a reader waiting | Later timed built-in asks get `reason="no_channel"` until it returns (3.3). |
| `async` callable, sync specialist under `AsyncSupervisor` | That question gets `reason="error"`; the agent proceeds on an assumption (3.1). |
| Interrupt during a built-in prompt | `KeyboardInterrupt` propagates and the run stops. |
| Planner returns `ask` on the recovery or second call | Ignored (no ask option is offered there). |
| Helper at `max_depth` with no `ask_user` attached | Not possible: `build()` attaches it to every helper when the channel exists. |

## 5. Testing

- Scripted asker (a list of replies) drives sync and async supervisors through a scripted
  model (`tests/subagent_helpers.py` router/ScriptedRunner).
- Planner: plans immediately (no extra call); asks then replans; both `plan` and `ask`
  (plan wins); malformed `ask`; all unanswered (note added); the second call has no ask
  option; answers reach recovery, dispatch context and synthesis; `result.query` unchanged.
- Tool: attached to specialists and helpers (planner and delegate), removed afterwards,
  runner's own `ask_user` left alone, `cacheable=False`, answer and no-answer strings,
  absent when `ask_user` is off.
- Channel: limit, dedupe (normalization), 2,000-character cut, callback raises, timeout
  (sync and async), async callback and sync callback on `AsyncSupervisor`, lock
  serialization with two parallel async helpers.
- Persistent: the deadline does not advance while a question is open (fake clock), including
  a `capped()` child; recovery gets the answers.
- Built-in asker: notebook detection with a faked IPython shell (`ZMQInteractiveShell`, with
  `_allow_stdin` true and false); terminal with a faked tty stdin; controlling-terminal path
  with faked opens and its 300 s default; headless; `EOFError`; `KeyboardInterrupt` propagates.
- `ask_user` unset: an existing-suite run shows no changes (full suite stays green).
- Manual check, not automatable here: a real Jupyter notebook run shows the input box and
  returns. The implementer cannot do this; the user confirms it.

## 6. Documentation

- `docs/guides/sub-agents.md`: an "Asking the operator" section; fix the quick start comment
  that implies the planner will know the competitors.
- `docs/cookbook/patterns.md`: a new recipe for `ask_user` (notebook, terminal, chatbot
  callback); pattern 31's human specialist stays as the manual alternative and links to it.
- `docs/cookbook/faq.md` and `troubleshooting.md`: "my run hangs on a question in Jupyter",
  "headless server and `ask_user=True`".
- `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `README.md`, `CHANGELOG.md`
  (3.6.0 entry), `host/build_data.py` regenerate, `examples/subagents_demo.py` gains `--ask`,
  and the demo's own `ask_human_tool` is replaced by the package export.

## 7. Risks and parked items

- **Planner may not ask.** Whether a model returns `ask` for the competitors example is model
  behavior; the scripted tests prove the framework path. The mid-run tool is the second
  net. Confirm with a real model once, manually.
- **Notebook `input()` in async.** Blocking the loop while the person types is accepted for
  the notebook path only (ipykernel's `input()` is unsafe from a worker thread); asks are
  serialized anyway. Terminal paths run in a worker thread. Revisit if a user runs long
  background tasks in the same loop in a notebook.
- **Abandoned reader threads** after a timeout stay blocked until the process exits (a
  daemon thread). Bounded by `max_questions`. While one is outstanding, later timed
  built-in asks get `no_channel` (3.3).
- **Answers from the operator are trusted** as facts. A chatbot developer who forwards raw
  end-user text into the callback should treat it as untrusted input to the run, like the
  task itself.
