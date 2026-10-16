---
name: running-code
description: When and how to run Python and shell commands - one-shot commands, checking a change, what not to run.
tools: [run_python, run_shell]
version: 1
---
# Running code

- `run_shell` runs ONE command and returns its output and exit code: git, tests (pytest, npm test), installs,
  builds. Use its `cwd` argument to work in a sub-folder instead of chaining `cd`.
- `run_python` runs a short script: a calculation, parsing or checking some data, a small experiment. Only what
  the script prints comes back, so print what you want to see. Each call starts fresh unless the tool says it
  keeps state, so put the setup and the analysis in one call.
- Do not start servers, watchers or anything that keeps running (`npm run dev`, `python -m http.server`): they
  are stopped at the time limit. Run a one-shot check instead.
- After changing code, run the project's tests or a quick check, and report what you ran and what it showed,
  including failures.
- Read the output before the next step. A non-zero exit code means the command failed even if some of the output
  looks fine.
- Commands and scripts are shown to the user for approval. Prefer one clear command to a long chain, and never
  run something destructive (deleting, force-pushing, rewriting history) unless the user asked for exactly that.
