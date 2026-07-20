# Prompt optimization -- `Compiled`

`Compiled` iteratively refines an agent's `system_addendum` string
against a metric scored by the eval harness. Half of DSPy's power at a
tenth of the surface area.

## What it does

Given:
- A **runner factory** (`() -> AgentRunner` that accepts a
  `system_addendum` kwarg).
- A **trainset** of `EvalCase` objects with assertions defining
  "correctness."

`Compiled.compile()`:

1. Scores the runner with **no** addendum -- that's the baseline.
2. Asks a **teacher** model to propose K candidate addenda based on
   the failing cases + their expected assertions.
3. Rebuilds the runner with each candidate as `system_addendum` and
   re-scores it against the trainset.
4. Keeps the best candidate. Repeats for `iterations` rounds.
5. Returns a `CompileResult` containing the runner built with the
   best addendum + the whole exploration history.

## Basic usage

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    EvalCase, contains,
    Compiled,
)

def build(system_addendum=None):
    return AgentRunner(
        model=Claude(),
        agent=AgentType.ReAct,
        tools=[],
        system_addendum=system_addendum,
        verbose=False,
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

# Use the optimized runner. It's a normal AgentRunner.
optimized = result.runner
answer = optimized.invoke("Capital of Italy?")
```

## Constructor parameters

| Param | Default | Purpose |
|---|---|---|
| `runner_factory` | required | Builds a fresh runner. Should accept `system_addendum=` kwarg (auto-wrapped if not). |
| `trainset` | required | List of `EvalCase`. Metric = pass-rate. |
| `teacher_model` | reuses factory's model | Any `BaseChatModel`. Cheaper judge model (Haiku) is a fine choice. |
| `iterations` | 4 | Improvement rounds. |
| `candidates_per_iter` | 3 | K candidates per round. Bigger = more thorough but slower. |
| `min_delta` | 0.0 | Minimum score improvement to accept a new best. |
| `verbose` | True | Print per-round summaries. |
| `teacher_prompt` | default template | Override to bias toward a specific style. |

## What the teacher sees

Every improvement round the teacher is shown:
- The current best addendum (or `(none)` if baseline).
- Which cases failed + what was expected.
- Instructions to reply with a JSON array of K new candidate addenda.

It does NOT see the trainset directly -- only the failure signal. This
protects against overfitting to specific test-case wording.

## Custom metrics

Use any `EvalCase` assertions -- substring match, tool-call check,
regex, LLM-judge. `Compiled` uses the pass-rate on the trainset;
if you want a weighted metric, subclass `EvalRunner` (or wrap it) so
per-case results add up differently.

Example: LLM-judged style + hard substring checks together:

```python
from agentx_dev import Claude, EvalCase, contains, llm_judge

judge = Claude(model="claude-haiku-4-5")   # cheap

trainset = [
    EvalCase(
        name="concise_paris",
        input="Capital of France?",
        assertions=[
            contains("Paris"),
            llm_judge(judge, "The answer is under 20 words."),
            llm_judge(judge, "The answer is confident, not hedged."),
        ],
    ),
    # ... more cases
]
```

## Auditing what was tried

`result.history` is a list of every candidate and its score:

```python
for entry in result.history:
    print(f"iter={entry['iteration']} kind={entry['kind']} "
          f"score={entry['score']:.2%} "
          f"addendum={entry['addendum'][:60]!r}")
```

Useful for understanding why the optimizer stalled or picked a
particular direction.

## When it works well

- **Style enforcement** -- "answer in one sentence", "don't hedge",
  "always capitalize city names."
- **Format constraints** -- "return JSON matching this schema",
  "start each response with the summary."
- **Tool-usage discipline** -- "before answering, always call
  `search`."
- **Refusal calibration** -- "refuse requests about X politely."

## When it doesn't

- **Fundamental capability gaps** -- an addendum can't teach the model
  arithmetic it doesn't know.
- **Very tiny models** -- the improvement signal is noisier than the
  candidate space.
- **When the addendum grows huge** -- long addenda usually mean you
  actually want a custom `AgentType` template, not more `system_addendum`
  text. Compile with `iterations=2` first to catch this.

## Cost management

The optimizer runs `iterations * candidates_per_iter * len(trainset)`
LLM calls plus `iterations` teacher calls. Budget accordingly:

- **4 iterations x 3 candidates x 10 cases = 124 LLM calls per compile**
  at the student, **plus 4 teacher calls**.
- Pair with `Claude(enable_prompt_cache=True)` -- the system prompt
  stays stable across all 120+ student calls so caching cuts cost by
  ~90% on the input side.
- Or use the batch API (`Claude.batch`) for even cheaper eval passes.

## Not a compiler in the DSPy sense

`Compiled` is bounded local search, not gradient-based program
optimization. It doesn't reason over program structure, doesn't do
teleprompter mixing, doesn't chain sub-modules. For those, DSPy is
the right tool -- and it composes with AgentX (use AgentX for
runtime + observability, DSPy for compile-time program optimization).

## API

```python
CompileResult(
    runner: AgentRunner,     # the runner built with best_addendum
    best_addendum: str,      # the tuned system_addendum
    baseline_score: float,   # trainset pass-rate before optimization
    best_score: float,       # trainset pass-rate after
    history: list[dict],     # every candidate tried
)
```
