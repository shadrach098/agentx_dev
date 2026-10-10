# Ask when done, and follow up (the Supervisor asks what to do next, and can keep going)

Status: design, approved section by section on 2026-10-09. Target release: 3.6.2 (unreleased, additive).
Builds on: `2026-10-05-ask-the-operator-design.md` (`OperatorChannel`, the asker) and
`2026-10-04-persistent-agents-design.md` (`RunBudget`, persistent mode).

## 1. Problem

A user ran a Supervisor with `ask_user=True` and the task "research this, and when done ask me what to
do next". The run ended and nobody was asked:

- The helper had the `ask_user` tool, but its instructions covered only a missing fact, so it wrote
  "What would you like me to do next?" into its answer (fixed separately in 3.6.2: the helper line and
  the planner note now cover "check with the user at some point").
- The Supervisor's own final summary then ended with the same sentence as plain text.
- Whether a question reaches the operator depends on a model choosing to call a tool. In the run that
  motivated this, the helper had no tools and its reply was a stray `{'code': ...}`, so no model step
  could have asked.
- A question written into an answer reaches no one, and `run()` ends. There is no loop in which the
  operator's reply could be acted on.

## 2. Goals and non-goals

Goals:

1. `Supervisor` and `AsyncSupervisor` take `ask_when_done=True`: when the final answer is ready, the
   framework itself asks the operator what to do next, through the same asker `ask_user` uses, and
   returns the reply in `result.next_step`.
2. They take `follow_up=True`: the reply starts another run (a round) with the earlier work as context,
   until the operator ends it or a cap is reached.
3. The question is asked whatever the outcome (`done`, `partial`, `stuck`, `out_of_time`,
   `out_of_budget`), and the prompt states the outcome.
4. Nothing changes when both are off, and the same behaviour holds in sync and async, in persistent mode,
   with memory, and with sub-agents.

Non-goals:

- Continuing a specialist's conversation. A runner starts fresh on every dispatch; a round is a new run
  that is told what happened before.
- Asking the model to classify the operator's reply. The stop words are a fixed list.
- Replacing the in-run `ask_user` questions. They stay as they are.
- A separate `Session` class (approach C below).

## 3. Approaches considered

- **A (chosen): a session layer around the existing run.** The current `stream()` / `astream()` body
  becomes one private round. The public `stream()` / `astream()` loops over rounds; `run()` is still a
  thin wrapper over it. The checkpoint is one new channel method. With both flags off the loop runs once.
- **B: checkpoint inside the round, loop only in `run()`.** Smaller, but a UI that uses `stream()` would
  get neither the checkpoint nor the loop.
- **C: a `Session(supervisor)` class.** Cleanest isolation, but the parameters were requested on
  `Supervisor`, and users would learn a second class.

## 4. API

```python
Supervisor(..., ask_user=True, ask_when_done=True)                  # ask once at the end
Supervisor(..., ask_user=True, follow_up=True, max_follow_ups=5)    # ask, then continue on the reply
```

New constructor parameters on `Supervisor` and `AsyncSupervisor`:

| Parameter | Default | Meaning |
|---|---|---|
| `ask_when_done` | `False` | After each round, ask the operator what to do next. |
| `follow_up` | `False` | A real instruction in the reply starts another round. Implies `ask_when_done`. |
| `max_follow_ups` | `5` | At most this many rounds after the first. Clamped to at least 0. |

Construction raises `ValueError` when `ask_when_done` or `follow_up` is set without `ask_user`
(`ask_user` is `None` or `False`). The message names the missing setting.

### 4.1 The flow

```
round 1: run as today                          -> result R1
checkpoint due?  (ask_when_done or follow_up, and a round is allowed to be followed)
  ask the operator            -> reply
  no answer / blank / stop word / not follow_up   -> end; next_step = reply or None
  instruction and follow_up and rounds left       -> round n+1 on the composed task -> loop
```

- **Who answers.** `next_step` is the reply exactly as typed, including "done" (a stop word is still a
  reply). It is `None` when nobody answered: no channel (headless), the read timed out, the asker
  raised, or the reply was empty.
- **Stop words.** A reply that, lowercased with surrounding whitespace and trailing `.!` removed, is one of
  `done`, `stop`, `quit`, `exit`, `no`, `nothing`, `that's all` ends the loop. Anything else is an
  instruction.
- **The cap.** After a follow-up round that is the last one allowed (round `1 + max_follow_ups`, with
  `max_follow_ups >= 1`) no question is asked, because a reply could not be acted on. The verbose log says
  "follow-up limit reached"; `next_step` is `None` and `len(result.rounds)` equals `1 + max_follow_ups`.
  Round 1 is always asked about, so `max_follow_ups=0` with `follow_up=True` behaves as `ask_when_done`.
- **`ask_when_done` without `follow_up`.** One round, then the question. The reply is returned in
  `next_step` and nothing runs on it.
- **After `out_of_budget`.** The checkpoint still asks and returns the reply, but no further round starts:
  the model's cost cap is cumulative, so another round would stop immediately.
- **Always ask.** The outcome does not decide whether to ask. The prompt states it.

## 5. The checkpoint prompt and the channel

`OperatorChannel` gets `checkpoint(...)` and `acheckpoint(...)`. They reuse `_ask_sync` / `_ask_async`, so
they inherit the asker, the timeout, the one-at-a-time lock, the headless handling (`no_channel`) and the
pause of the persistent deadline while the person thinks. They differ from `ask` / `aask` on purpose:

- They do not consume `max_questions` and ignore the per-agent limit.
- They are not de-duplicated, and not looked up in memory or written to it (a next instruction is a
  command, not a fact).
- They record `{"source": "supervisor", "question": <the question line>, "answered", "reason",
  "deduped": False}` in `records` and emit `question` and `answer` events with source `supervisor`.

The text sent to the asker (one string, like any question):

```
<the final answer, at most 3,000 characters, then "(full text in result.content)" when cut>

Outcome: partial (1 of 2 steps failed).
What should I do next? (blank or "done" to finish)
```

The built-in asker labels it `[supervisor]`. A function asker receives the string and adds its own label.

The outcome line is `Outcome: <outcome>` plus, for `partial` and `stuck`, the count of failed steps
(`_result_failed`), and for `out_of_time` / `out_of_budget` the words "time limit reached" / "cost limit
reached".

## 6. What a follow-up round is

Each round is a complete, normal run: planning, helpers, asking, memory and persistent mode work as they
do today. The code reuses the existing per-run setup, which already rebuilds the spawn policy, the
operator channel, the run memory and `RunBudget` at the start of every `stream()`.

- **The task the planner sees** from round 2 is, in order: the original task; "WHAT HAS HAPPENED SO FAR"
  with each earlier round's operator instruction and answer (each answer capped at 3,000 characters); the
  operator's answers so far (the existing OPERATOR ANSWERS block); and "The operator now asks: <reply>. Do
  this now." The reply is capped at 4,000 characters.
- **Operator answers carry over.** The new round's channel is seeded with the earlier channel's answers
  and the questions already asked. A repeated question gets the first answer without a prompt, and the
  planner still sees the OPERATOR ANSWERS block and the "do the work" note.
- **Fresh per round:** the question budget, the per-agent limit, the spawned helpers, and the persistent
  time budget (`max_minutes`). The model's cost cap is cumulative and not reset.
- **Memory.** Only round 1's result is saved as a run result, under the original task, as today. A
  follow-up round's result is not saved (a composed task would be a poor lookup key). Operator answers
  are saved as today. Replies to the checkpoint are never saved. Recall into a follow-up round's planning
  prompt uses the composed task's text as it does for any task.
- **`result.query`** is the original task in round 1 and the operator's reply in later rounds.

## 7. What comes back

`SupervisorResult` gets two fields, both with defaults that leave existing code unchanged:

- `next_step: Optional[str] = None`.
- `rounds: List[Dict[str, Any]] = []`: one `{"round", "query", "content", "outcome"}` per round, filled
  only once a follow-up round has run. Empty for a single-round run.

The returned result is the last round's. `asked` lists every question of the whole session, in order,
including the `supervisor` checkpoints.

Stream events (both `stream` and `astream`):

- `{"type": "round", "round": n, "query": str}` at the start of each round from 2.
- `{"type": "checkpoint", "round": n, "outcome": str}` before each checkpoint question.
- The usual `question` / `answer` events, with source `supervisor` for the checkpoint.
- Each round's `final` event. A round's own `completion` event is not yielded; one `completion` ends the
  stream, carrying the session's result.

## 8. Structure of the change

- `agentx_dev/Operator.py`: `checkpoint` / `acheckpoint`, `seed_from(previous_channel)`, the stop-word
  test, and the prompt text builder. This file already owns the channel and the asker.
- `agentx_dev/Supervisor.py`: constructor parameters and validation on both classes; the existing
  `stream` / `astream` body is renamed to a private round generator (unchanged logic); the new public
  `stream` / `astream` run the rounds; `SupervisorResult` fields; the round-task builder as one pure
  function (unit-testable without a model).
- Docs: the guide's asking section, a FAQ entry, the CHANGELOG 3.6.2 entry, the README feature table.

## 9. Edge cases and errors

- No `ask_user`: construction error, as in section 4. With `ask_user` set and no way to ask (headless),
  the checkpoint is a no-answer: `next_step` is `None`, the loop ends, nothing raises.
- An asker that raises or times out is a no-answer, as in `ask`.
- `KeyboardInterrupt` and async cancellation propagate, as in `ask`.
- A round that raises (a bug, or a model error outside the budget handling) propagates; earlier rounds'
  results are lost with it. This matches `run()` today.
- `max_follow_ups=0` with `follow_up=True` behaves as `ask_when_done` (section 4.1, the cap).
- The previous round's operator channel stays on the instance until the next round starts, which is what
  lets the next round be seeded from it.
- Concurrent runs on one instance stay unsupported, as for the existing per-run state.

## 10. Testing

Scripted askers on both classes, using the existing helpers in `tests/subagent_helpers.py`:

- Rounds: 1 and 3; each stop word; a blank reply; no channel; the cap; no follow-up after `out_of_budget`.
- Round 2 content: the planner prompt holds the original task, the earlier answer and the reply; operator
  answers carry over; a repeated question is not prompted again.
- Memory: only round 1's result is saved.
- Budgets: a fresh time budget per round; checkpoint questions do not consume `max_questions`.
- Events: order and the single `completion`; `result.asked` includes the `supervisor` records.
- Construction errors, and the flags-off path unchanged (the existing suite stays green).

## 11. Open decisions recorded

- Always ask whatever the outcome (chosen by the user on 2026-10-09; the alternative was to ask only on
  `done`).
- Defaults: `max_follow_ups=5`; the answer shown is capped at 3,000 characters; the reply at 4,000.
