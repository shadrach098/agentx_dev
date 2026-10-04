# Long-running agents *(3.5)*

By default an agent stops when its step budget runs out (`max_iterations`,
4 unless you raise it) or when it repeats the same call, and a
`Supervisor` plans once and moves past a failed step. That's the right
default for short tasks.

For work that can take a while — getting a failing build to pass,
migrating a folder of files, researching a question with dead ends —
turn on **persistent mode**. The agent notices when it's stuck, changes
approach, and stops only when it has finished or a time or cost limit
is reached.

## Turn it on

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Persistence

model = GPT(model="gpt-5.4").configure_limits(
    budget_usd=5.00,
    input_price_per_1k=0.0025,       # your provider's prices
    output_price_per_1k=0.01,
)

runner = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    persistence=Persistence(max_minutes=60),
)

result = runner.invoke("Get the failing tests in ./workspace passing")
print(result.outcome)       # "done", or why it stopped
print(result.content)       # the answer, or a report of where it stopped
```

`persistence=None` (the default) keeps the ordinary loop. Set a cost
cap whenever you use persistence: the time limit alone doesn't stop a
fast, expensive loop.

## What comes back

Every run — persistent or not — reports how it ended:

| `result.outcome` | Meaning |
|---|---|
| `done` | The agent returned a final answer. |
| `stuck` | It tried the recovery steps below and was still stuck. |
| `out_of_time` | `max_minutes` passed. |
| `out_of_budget` | The model's cost cap was reached. |
| `iteration_limit` | A step cap was reached (`max_iterations`, or `max_turns` in persistent mode). |

When a run doesn't finish, `result.content` is a report, not an answer:
why it stopped, what it completed, what failed, and what it planned
next. `result.progress` has the same facts as data
(`goal` / `done` / `failed` / `next`). Nothing is raised for a time or
cost limit.

## How it recovers

The loop watches for two kinds of trouble, each counted over
`reflect_after` turns in a row (default 3): the same call repeated,
or tool errors. When one fires it doesn't give up.
It adds a note to the last tool result and carries on, a little firmer
each time:

1. Name the root cause in one sentence, and don't repeat the call that
   failed.
2. Choose an approach you haven't tried. The failed attempts are listed.
3. Check your assumptions with one cheap read-only probe before another
   write or retry.
4. If you're still blocked, say exactly what's blocking you.

If the trouble comes back after the last step, the run ends `stuck` with
the report above. Any real progress — a successful call that isn't a
repeat — resets the ladder, so a run can work for hours: each stuck
patch escalates, then starts over once the agent is moving again.

## Long runs and the context window

Hours of tool output would overflow any context window. Once the
history passes `compact_at_tokens` (default 60,000, estimated), the
turns in the middle are replaced by a short set of notes: one extra
model call summarizes what was learned and what's left, and the list of
failed attempts is kept word for word. The task itself (including any
images or files you attached) and the most recent turns
(`keep_recent_turns`, default 6) are never touched. If the summary call
fails, the failed-attempts list is used on its own.

## Time, cost, and provider errors

- **Time:** `max_minutes` (default 30) for the whole run. A Supervisor
  and all its specialists share one deadline.
- **Cost:** the model's own cap, set with
  `model.configure_limits(budget_usd=..., input_price_per_1k=...,
  output_price_per_1k=...)`. It counts everything that model object has
  spent, not just this run: use a fresh model object for a per-run cap.
- **Transient provider errors** (HTTP 429 and 5xx, timeouts,
  connection errors) are retried with growing waits until the deadline.
  Errors that won't fix themselves — a bad API key, an invalid request,
  a bug in your tool — still raise straight away. A run with
  `stream_tokens=True` isn't retried mid-response.

## Supervisors

```python
from agentx_dev import Supervisor, Persistence

sup = Supervisor(
    model=planner_model,
    agents={"coder": ("writes and runs code", coder), "writer": ("writes docs", writer)},
    persistence=Persistence(max_minutes=45),
)
result = sup.run("Fix the failing test, then update the changelog")
print(result.outcome)       # done | partial | stuck | out_of_time | out_of_budget
```

When a step doesn't finish, the Supervisor asks the planner for a
recovery plan: steps that did finish are kept (never re-run) and can be
used as inputs; the failed approach is described so it isn't repeated;
the planner may pick a different specialist or, if you enabled
spawning, create one. A recovery step names the failed steps it redoes
(`"replaces": [...]`); a failed step counts as resolved only once a step
that replaces it finishes, so a recovery plan can't hide failed work by
leaving it out. The Supervisor keeps going while rounds resolve failed
steps and stops after `max_replans` (default 3) rounds in a row that
don't. The final answer says plainly which parts weren't completed.

`Persistence` is applied to specialist runners that don't have their
own, for the duration of the run. `SupervisorResult.outcome` is
computed in default mode too (`done`, `partial` or `stuck`).

## Watch it work

`runner.stream(...)` and `supervisor.stream(...)` add events:

| Event | When |
|---|---|
| `{"type": "reflect", "rung": 2, "reason": "..."}` | A recovery step was added. |
| `{"type": "compact", "before_tokens": ..., "after_tokens": ...}` | History was compacted. |
| `{"type": "replan", "round": 2, "unresolved": [...], "plan": [...]}` | A Supervisor started a recovery round. |

With `verbose=True` the same moments print as `[persist]` lines. The
`AsyncAgentRunner.astream` replays its events after the run and does not
emit `reflect` or `compact`, but `result.progress` and the log lines are
the same.

## Settings

| `Persistence(...)` | Default | What it does |
|---|---|---|
| `max_minutes` | `30` | Wall-clock limit for the run. |
| `reflect_after` | `3` | Bad turns in a row before a recovery step. |
| `max_reflections` | `4` | Recovery steps before the run ends `stuck`. |
| `compact_at_tokens` | `60000` | History size that triggers compaction. |
| `keep_recent_turns` | `6` | Messages kept word for word when compacting. |
| `max_replans` | `3` | Supervisor recovery rounds without progress. |
| `max_turns` | `1000` | Backstop on total model turns. |
| `patient_retries` | `True` | Wait out transient provider errors. |

## Things to know

- **The tool-result cache is off while persistence is set.** A retry
  answered from the cache isn't a retry, and a cached "write succeeded"
  can be false.
- **Progress isn't saved to disk.** If the process dies, the run is
  lost; resuming isn't supported yet.
- **A run can still be wrong.** Persistence keeps an agent working; it
  doesn't check the work. Give the task a way to verify itself (run the
  tests, read the file back).
- **Upgrading:** with or without persistence, a Supervisor now retries
  a specialist that gave up and flags it if it still doesn't finish.
  See [Upgrading](upgrading.md).
