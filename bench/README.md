# The agentx benchmark

Ten fixed tasks on two small sample repos, each checked automatically, with the time and the tokens measured.
Run it before a release, and after any change that could make `agentx` better or cheaper, to see whether it did.

```bash
python -m bench list                                    # the tasks
python -m bench run --model gpt-6.1-sol                 # all ten (asks first: it uses your API key)
python -m bench run --model gpt-6.1-sol --task audit --task team-job --repeat 3
python -m bench compare bench/results/A.json bench/results/B.json
```

## The tasks

| Task | Repo | What it measures | Checked by |
|---|---|---|---|
| `find-code` | shop | find a line of code | the answer names the file and the line; no file changed |
| `explain-failure` | shop | explain a failing test | the answer names the cause; no file changed |
| `fix-failing-test` | shop | fix code so a test passes | the whole suite passes; the test file is unchanged |
| `fix-bug-add-test` | shop | fix a bug and add a test | a hidden test passes; a test was added |
| `add-validation` | shop | add input validation | a hidden test passes |
| `add-feature` | notes | add a function with tests | a hidden test passes |
| `rename-everywhere` | notes | rename across files | no old name left; a hidden test passes |
| `audit` | shop | audit with the `auditing-a-project` skill | the report names all three planted bugs; no file changed |
| `team-job` | notes | a team job: edit, then run | every function has type hints; the suite passes |
| `run-and-report` | shop | run the tests and report | the answer names the failing test and why; no file changed |

The `shop` repo has three planted bugs: discounts are truncated instead of rounded (one test fails), cancelling an
order twice restores the stock twice, and `add_item` accepts a quantity below 1. Hidden tests live in
`bench/hidden/` and are copied into the repo only for the check, so the agent cannot see them.

## How a task runs

Each task runs the real `agentx` app on a fresh copy of its repo, in `auto` mode, with a fresh home folder: only the
built-in skills, no MCP servers, no notes from earlier runs, so runs compare. Every question agentx asks is answered
no. Each task has a token budget (cached input counted as a tenth, as the token limit counts it); past it agentx
wraps up, so a runaway task costs at most its budget plus the wrap-up.

## What you get

A table on the screen, and in `bench/results/`:

- `<time>-<model>.json`: per task, whether it passed and why, seconds, model requests, tokens in and out, the
  cached input, the counted tokens, the chat's tool calls, every team helper's tool uses, and the team jobs started;
- `<time>-<model>/round-N/<task>.log`: what agentx showed, and a diff of every file it changed.

Models vary from run to run: use `--repeat 3` before trusting a difference of one task. `compare` shows each task's
pass rate, counted tokens and time side by side, with the change in tokens.

The JSON files are small and worth committing, so the history of runs stays with the code; the log folders are
ignored by git.
