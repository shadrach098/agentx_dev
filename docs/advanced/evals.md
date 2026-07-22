# Evals harness *(3.1)*

Regression tests for agents. Define input + assertions, run them
through a runner factory, get a pass/fail report with per-case latency,
tokens, tool errors, and cost.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

## Minimum viable

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    EvalCase, EvalRunner, contains, called_tool,
)

def build_runner():
    return AgentRunner(
        model=Claude(), agent=AgentType.ReAct,
        tools=[calculator_tool],
        permissions=Permissions.read_only(["./"]),
    )

cases = [
    EvalCase(
        name="paris_capital",
        input="What's the capital of France?",
        assertions=[contains("Paris")],
    ),
    EvalCase(
        name="uses_calculator",
        input="What is 137 * 91?",
        assertions=[called_tool("calculator"), contains("12467")],
    ),
]

report = EvalRunner(build_runner).run(cases)
print(report.summary())
```

## What the runner does per case

1. Calls `build_runner()` (the factory) to get a **fresh** runner —
   state doesn't leak between cases (auto_cache, memory, session
   counters).
2. Snapshots `model.usage` before the run.
3. Calls `runner.invoke(case.input)`.
4. Runs each assertion callable; collects pass/fail + message.
5. Computes deltas: `input_tokens`, `output_tokens`, `cache_read_tokens`.
6. Counts tool errors from the completion's `tool_calls`.
7. Records duration.

## Assertion helpers

Every helper returns a callable `(AgentCompletion) -> (passed, message)`:

| Helper | What it checks |
|---|---|
| `contains(needle, case_sensitive=False)` | Final answer contains substring |
| `not_contains(needle, case_sensitive=False)` | Final answer does NOT contain substring |
| `matches_regex(pattern, flags=re.IGNORECASE)` | Final answer matches regex |
| `called_tool(name)` | Tool with that name was called |
| `tool_count(*, min=0, max=None)` | Tool call count within bounds |
| `max_iterations(limit)` | Total steps `<=` limit |
| `llm_judge(model, criterion)` | Judge model grades yes/no |

### `llm_judge` — open-ended quality checks

Substring matching is brittle for "is this helpful?" questions. Use a
judge model:

```python
from agentx_dev import Claude, llm_judge

judge = Claude(model="claude-haiku-4-5")   # cheap, fast

cases = [
    EvalCase(
        name="helpful_response",
        input="Explain MVCC.",
        assertions=[
            llm_judge(judge, "The answer accurately explains MVCC."),
            llm_judge(judge, "The answer is under 150 words."),
        ],
    ),
]
```

The judge is asked YES/NO + one short reason; the reason lands in the
report on failure.

### Custom assertions

Any callable with the right signature:

```python
def has_json_output(completion):
    try:
        import json
        json.loads(completion.content)
        return True, "output is valid JSON: PASS"
    except json.JSONDecodeError as e:
        return False, f"output is not JSON: FAIL ({e})"

EvalCase(name="json_output", input="...", assertions=[has_json_output])
```

## The report

```python
report = EvalRunner(build_runner).run(cases)

report.passed              # int
report.failed              # int
report.total               # int
report.pass_rate           # float 0-1
report.total_duration_sec
report.total_input_tokens
report.total_output_tokens
report.total_cache_read_tokens

print(report.summary())    # str
```

`summary()` output:

```
AgentX Evals -- 2/2 passed (100.0%) in 6.5s
  input tokens:  1247
  output tokens: 384
  cache reads:   120 (9.6% of input)

  [PASS] paris_capital  (2.3s, 380 tokens)
  [PASS] uses_calculator  (4.2s, 1251 tokens)
```

For CI dashboards:

```python
import json
with open("eval-report.json", "w") as f:
    json.dump(report.to_dict(), f, indent=2)
```

## Per-case metrics

```python
for result in report.results:
    print(result.case.name, result.passed, result.duration_sec)
    print(f"  tokens in/out: {result.input_tokens}/{result.output_tokens}")
    print(f"  cache reads: {result.cache_read_tokens}")
    print(f"  tool errors: {result.tool_error_count}")
    if not result.passed:
        for line in result.assertion_lines:
            print(f"    - {line}")
```

## JSON case files

Cases can live in disk files for version control:

```json
// tests/evals/paris.eval.json
{
  "name": "paris_capital",
  "input": "What's the capital of France?",
  "expected_substrings": ["Paris"],
  "forbidden_substrings": ["Berlin"],
  "must_call_tools": [],
  "max_iterations": 3
}
```

Multiple cases per file:

```json
// tests/evals/math.eval.json
{
  "cases": [
    {"name": "add", "input": "2+2?", "expected_substrings": ["4"]},
    {"name": "mult", "input": "6*7?", "expected_substrings": ["42"]}
  ]
}
```

Loaders:

```python
from agentx_dev import load_case_from_dict, load_cases_from_dir

cases = load_cases_from_dir("tests/evals/")   # walks recursively
```

## CLI

```bash
python -m agentx_dev.Evals run tests/evals/ --config agent.yaml
```

- Walks the directory for `*.eval.json` files.
- Loads a runner from `agent.yaml` (see [YAML config](../guides/yaml-config.md)).
- Runs every case, prints the summary.
- Exits `0` on all-pass, `1` on any failure.
- `--json-out report.json` writes structured output for CI.
- `--pattern '*.regression.json'` overrides the glob.

## Wire into CI

GitHub Actions:

```yaml
- name: Agent evals
  env:
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  run: |
    python -m agentx_dev.Evals run tests/evals/ \
      --config configs/agent.yaml \
      --json-out artifacts/eval-report.json

- name: Upload report
  if: always()
  uses: actions/upload-artifact@v3
  with:
    name: eval-report
    path: artifacts/eval-report.json
```

## Best practices

- **Fresh runner per case** — the factory pattern. Don't reuse a runner
  across cases; state leaks.
- **Cheap deterministic cases first** — substring / tool-count / regex
  assertions. Reserve `llm_judge` for the last mile.
- **Group by concern** — put related cases in one file so they share
  context in reviews.
- **Test both success AND failure paths** — include cases where you
  expect the agent to refuse / redirect / say "I don't know."
- **Track cost regression** — the report's `total_input_tokens` and
  `total_cache_read_tokens` make cost drift visible. Fail CI if input
  tokens grow > 10% between commits.

## Runnable demo

See `examples/v3_1_comprehensive_demo.py` — the `run_evals` function
regression-tests a full multi-agent pipeline through fresh factories.
