# Persistent agents: keep working through errors

**Status:** design approved in chat 2026-10-04; spec awaiting review.
**Target release:** 3.5.0 (new opt-in feature; one small default-mode fix, see 4.1).

## 1. Problem

A Supervisor run (and a plain `AgentRunner` run) ends as soon as something
goes wrong, instead of diagnosing the problem and trying another way, the
way Claude Code can work on one fix for hours. Reproduced with a specialist
whose only tool always raises: the specialist burned its turns, the run
ended, and the Supervisor saw `error=None`.

Four causes, all in the current code:

1. **Step caps, not progress.** `AgentRunner.max_iterations` defaults to 4
   (`AgentRun.py:1581`); specialists the Supervisor spawns get 15
   (`Supervisor.py:197`).
2. **Stuck means abort.** Three identical calls in a row prints
   "Terminated" and exits (`AgentRun.py:2382`); nothing tries a different
   approach first.
3. **Giving up looks like success.** "Terminated" and "Hit max_iterations"
   are returned as ordinary answers. `_dispatch_with_retry` only reacts to a
   raised exception or a user-supplied `subtask_success_check`, so a
   specialist that gave up is accepted and no retry or replan happens.
4. **The Supervisor is one-shot.** Plan once, dispatch, synthesize
   (`Supervisor.py:1006`, `:1300`). A failed step is recorded and skipped
   past; nothing replans around it. The runner also has no context
   management, so a long run would overflow the context window.

## 2. Goals and non-goals

**Goals**

- Opt-in persistent mode for `AgentRunner`, `AsyncAgentRunner`,
  `Supervisor`, `AsyncSupervisor`: keep going until the task is done or a
  time or cost limit is reached.
- When stuck, recover (diagnose, change approach) before giving up.
- Every run reports an honest outcome the caller can act on.
- Long runs stay inside the context window.
- Existing agents behave as they do today unless they opt in (one
  exception, 4.1).

**Non-goals (deferred, can be added without changing this design)**

- Saving progress to disk so a crashed process can resume.
- A `blocked` outcome / pause-and-ask-the-human flow.
- Changing the default `max_iterations`.
- `HandoffCoordinator` (it keeps its current behavior).

## 3. Public API

```python
from agentx_dev import AgentRunner, Persistence

model = GPT(model="gpt-5.4").configure_limits(           # the existing cost cap
    budget_usd=5.00, input_price_per_1k=0.0025, output_price_per_1k=0.01)   # your provider's prices

runner = AgentRunner(
    model=model,
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./workspace"]),
    persistence=Persistence(max_minutes=60),
)
result = runner.invoke("Get the failing tests passing")
result.outcome     # "done" | "stuck" | "out_of_time" | "out_of_budget" | "iteration_limit"
result.content     # the answer, or a report of where it stopped
result.progress    # dict: goal / done / failed / next
```

`Supervisor(..., persistence=Persistence(...))` takes the same object.
`persistence=None` (the default) keeps today's behavior.

### 3.1 `Persistence`

A frozen dataclass in `agentx_dev/Runner/Persistence.py`, exported from the
package root.

| Field | Default | Meaning |
|---|---|---|
| `max_minutes` | `30.0` | Wall-clock limit for the whole run (shared by a Supervisor and its specialists). |
| `reflect_after` | `3` | Consecutive failing or repeating turns before a reflection is forced. |
| `max_reflections` | `4` | Ladder length. If the stuck signal fires again after the last rung, the run ends `stuck`. |
| `compact_at_tokens` | `60_000` | Estimated history size that triggers compaction. |
| `keep_recent_turns` | `6` | Messages kept verbatim at the tail when compacting. |
| `max_replans` | `3` | Supervisor recovery rounds per stuck episode. |
| `max_turns` | `1000` | Backstop on total model turns (outcome `iteration_limit`). |
| `patient_retries` | `True` | Keep retrying transient provider errors with backoff until the deadline. |

The cost limit is not a field: it is the model's existing cap, set with
`model.configure_limits(budget_usd=..., input_price_per_1k=..., output_price_per_1k=...)`
(both prices are required). It is cumulative per model object: it counts
everything that object has spent, not just this run, so use a fresh model
object for a per-run cap. `CostBudgetExceeded` raised during a persistent
run is caught and ends the run as `out_of_budget`.

### 3.2 Outcomes

`AgentCompletion` gains `outcome: str = "done"` and
`progress: Optional[dict] = None`. `SubtaskResult` gains `outcome`.
`SupervisorResult` gains `outcome` (adds `"partial"`).

| Outcome | When |
|---|---|
| `done` | The agent returned a final answer. |
| `stuck` | The reflection ladder was exhausted (persistent), or the 3-identical-calls abort fired (default mode). |
| `out_of_time` | `max_minutes` elapsed. |
| `out_of_budget` | `CostBudgetExceeded`. |
| `iteration_limit` | `max_iterations` reached (default mode), or `max_turns` reached (persistent). |
| `partial` | Supervisor only: some steps `done`, some unresolved after recovery. |

On any non-`done` outcome, `content` is a deterministic report built from
the ledger: why it stopped, what was completed, what failed, what was next.
No extra model call is used to write it.

## 4. Behavior

### 4.1 Default mode: honest outcomes (the one non-opt-in change)

`AgentCompletion.outcome` is set on every exit path in both runners:
final answer is `done`, the loop-level abort is `stuck`, hitting
`max_iterations` is `iteration_limit`. `content` is unchanged.

`Supervisor._dispatch_with_retry` (sync and async) treats a returned
`outcome != "done"` as a rejected attempt: it retries up to
`max_subtask_retries` (default 1) with the failure appended to the query,
and on exhaustion returns the last result with `error` set. This reuses the
existing "did not meet success criteria" path. A specialist that gave up
is therefore no longer reported as success. This is the only change that
affects agents which do not opt in.

### 4.2 The four hooks

All new logic lives in `agentx_dev/Runner/Persistence.py`. The sync and
async runners each own a copy of the loop, so both call the same four
hooks. Persistent mode replaces the `while count <= max_iterations` bound
with the deadline, `max_turns`, and the budget.

1. **Before each model call:** `RunBudget.check()` (deadline); compact if
   the history estimate is over `compact_at_tokens`.
2. **After each tool result:** feed the ledger and the stuck tracker.
3. **On a stuck signal:** reflect (4.3) or end `stuck`.
4. **At exit:** set `outcome`, build the report if non-`done`, attach
   `progress`.

A tool call already in flight when the deadline passes is allowed to finish
(tools carry their own timeouts); the run ends before the next model call.

### 4.3 Stuck detection and the reflection ladder

`StuckTracker` keeps independent consecutive counters:

- same `(action, args)` signature as the previous call;
- tool result is an error, or a circuit breaker is open;
- tool observation identical to the previous observation.

A signal fires when any counter reaches `reflect_after`. This replaces the
hard `LOOP_FORCE_STOP = 3` abort in persistent mode (the existing
tool-layer duplicate-call guard still refuses the repeated call itself).

**Progress** is a successful tool result whose signature and observation
both differ from the previous call. Progress resets all counters and the
ladder position.

On a signal the runner appends a recovery message and continues. The
message is delivered with the same role handling `_nudge_text_only_turn`
already uses (providers reject consecutive user turns). Rungs:

1. Name the root cause in one sentence; do not repeat the failed call.
2. These approaches were already tried (from the ledger); choose a
   different one.
3. Check your assumptions with a cheap read-only probe before another
   write.
4. If still blocked, state exactly what is blocking you and what you would
   need.

If the signal fires again after rung `max_reflections`, the run ends
`stuck` with the report.

### 4.4 Progress ledger

Kept by the framework, not the model (free, cannot drift). Fields:

- `goal`: the original task text.
- `done`: successful calls, each as name + short args + a one-line result.
- `failed`: failing calls, each as name + short args + error line + the
  ladder rung active at the time.
- `next`: the model's most recent stated plan (its last `Thought`).

Rendered as compact text. It is included in every reflection message, in
every compaction note, and in the non-`done` report, and exposed as
`result.progress`.

### 4.5 Compaction

When the estimated history exceeds `compact_at_tokens`
(`len(json)/4`, base64 media counted as a flat 1,500 tokens each; no
tokenizer dependency), the middle of the history is replaced by one notes
message:

- Preserved verbatim: system messages, the original task message
  (including its media), and the last `keep_recent_turns` messages.
- Summarized: everything between, by one `model.Initialize` call that
  records what has been learned, done, and which files or values matter.
  The ledger's `failed` list is appended to the notes verbatim. If the
  summary call fails, the notes are the ledger alone.
- **Invariants (tested for GPT-format and Claude-format histories):** the
  cut never separates a native tool call from its result; role alternation
  is valid for both providers (the notes are attached so no two user turns
  are adjacent); media in the original task is untouched.

### 4.6 Provider errors

With `patient_retries=True`, a transient error from the model call (HTTP
408, 429 or 5xx; timeouts; connection errors; rate-limit and overloaded
errors. Anything else, including programming errors, raises immediately)
is retried with exponential backoff
(capped at 60 s per wait) until the deadline. Non-retryable errors (auth,
invalid request) still raise immediately: retrying them would hide a real
problem. `CostBudgetExceeded` ends the run as `out_of_budget`. No
exception from budget or deadline reaches the caller.

### 4.7 Supervisor replanning

After each round of steps, in persistent mode, the unresolved set is
every step with `outcome != "done"` plus every step skipped because a
dependency failed. If it is non-empty, budget remains, and the replan
count for this episode is below `max_replans`, the Supervisor replans:

- The planner receives: the user task; completed results (kept, never
  re-run); each unresolved step with its error and its ledger; the
  instruction to produce a recovery plan that does not repeat the failed
  approach. It may choose a different specialist or spawn one (for example
  a code specialist to debug a failing step) under the existing
  `spawn_config`.
- The recovery plan uses the existing schema. New step ids are prefixed
  per round (`r2_step_1`). `depends_on` may reference completed step ids
  from earlier rounds; `_sanitize_plan` gains a `known_ids` argument for
  this. The 3.3 DAG semantics are otherwise unchanged.
- A round that produces at least one newly `done` step resets the replan
  count (progress). Otherwise the count increments.
- The loop ends when there are no unresolved steps, the budget is spent,
  the replan count is exhausted, or the planner returns an empty plan. A
  planner failure keeps all results so far and proceeds to synthesis.

**Shared budget.** The Supervisor creates one `RunBudget` (deadline) and
passes it to every specialist invocation through an internal
`Initialize(..., _budget=)` argument; a specialist configured with its
own `Persistence` uses the earlier of the two deadlines.
`Supervisor(persistence=P)` applies `P` to specialist runners that have
none for the duration of the run and restores them afterwards (try/finally,
as the scheduler cleanup already does).

**Honest synthesis.** The synthesis prompt lists unresolved steps and
reasons and forbids claiming success for them. `SupervisorResult.outcome`
is `done` (nothing unresolved), `partial` (some done, some unresolved),
`stuck` (none done, or no valid plan could be made), or `out_of_time` /
`out_of_budget` when the budget ended the run. It is computed this way in
default mode too (there is just no recovery round), so callers can rely on
it either way.

### 4.8 Events and logs

New stream events on runners and supervisors:
`{"type": "reflect", "rung": int, "reason": str}`,
`{"type": "compact", "before_tokens": int, "after_tokens": int}`,
`{"type": "budget", "reason": "time" | "cost"}`, and on supervisors
`{"type": "replan", "round": int, "unresolved": [step ids]}`. `verbose=True`
prints matching one-line logs. Existing event types are unchanged.

### 4.9 Tool-result cache (addendum, 2026-10-04)

The framework's default tool-result cache (`auto_cache=True`) is keyed on the tool function and its
arguments only. A cache hit returns before the tool runs, so a repeated call is not a retry, and a
cached "write succeeded" can be false. Persistent mode depends on retries and re-probing actually
executing, so a runner with `persistence` set suspends its registry's tool-result cache for as long
as persistence is set and restores it when cleared. This does not fix the cache's cross-runner and
side-effect hazards in default mode; those are tracked as a separate fix.

## 5. Files

| File | Change |
|---|---|
| `agentx_dev/Runner/Persistence.py` (new) | `Persistence`, outcome constants, `ProgressLedger`, `StuckTracker`, `RunBudget`, `compact_history`, reflection messages, report builder, patient-retry helper. |
| `agentx_dev/Agents/Agent.py` | `AgentCompletion.outcome`, `.progress`. |
| `agentx_dev/Runner/AgentRun.py`, `AsyncAgentRun.py` | `persistence=` parameter; the four hooks; `outcome` on every exit path; new events. |
| `agentx_dev/Supervisor.py` | `persistence=` on both supervisors; `SubtaskResult.outcome`; non-`done` is a rejected attempt; replan rounds; shared `RunBudget`; `SupervisorResult.outcome`; synthesis prompt; `_sanitize_plan(known_ids=)`. |
| `agentx_dev/__init__.py` | Export `Persistence`. |
| `docs/guides/long-running-agents.md` (new) + nav in `host/build_data.py` | The feature guide. |
| README, cookbook patterns, FAQ, troubleshooting, upgrading, CHANGELOG, version badge | 3.5.0 entries. |

## 6. Testing

Scripted models, every scenario on both sync and async runners.

- Stuck then recovers on a later rung; stuck forever ends `stuck` with the
  report; progress resets the ladder.
- Deadline ends `out_of_time`; `CostBudgetExceeded` ends `out_of_budget`;
  neither raises.
- Transient provider error is retried until success; auth error raises
  immediately.
- Compaction: media preserved; no split tool-call/result pair; valid role
  alternation for GPT-format and Claude-format histories; falls back to the
  ledger when the summary call fails.
- Supervisor: replans a failing step and succeeds in round 2; never
  re-runs completed steps; a recovery step may depend on a round-1 step;
  survives a planner failure; one deadline shared with specialists; runner
  config restored after the run.
- Long-run check: a fake flaky tool for several thousand turns; estimated
  context stays below the threshold plus one turn.
- Regression: the existing 309 tests pass unchanged apart from the
  deliberate 4.1 change, which gets its own tests (a specialist that hits
  `max_iterations` is retried once and then flagged, not reported as
  success).

## 7. Risks

- **Cost:** a persistent run can spend up to its limits. Defaults are 30
  minutes and the model's cost cap; the guide recommends setting
  `model.configure_limits(budget_usd=...)` whenever `Persistence` is used.
- **Compaction loses detail:** mitigated by the verbatim recent tail, the
  verbatim failed-attempts list, and the ledger fallback.
- **Reflection messages vs structured output:** reflections are ordinary
  turns and the final answer still goes through the existing
  `output_schema` parsing; covered by a test.
- **Behavior change in 4.1:** specialists that previously gave up silently
  are now retried once and flagged. Documented in the upgrading guide.
