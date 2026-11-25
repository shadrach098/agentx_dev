---
name: running-code
description: When and how to run Python and shell commands - one-shot commands, checking a change, what not to run.
tools: [run_python, run_shell, start_background, background_output, stop_background, list_background,
  send_to_terminal]
version: 2
---
# Running code

- `run_shell` runs ONE command and returns its output and exit code: git, tests (pytest, npm test), installs,
  builds. Use its `cwd` argument to work in a sub-folder instead of chaining `cd`.
- `run_python` runs a short script: a calculation, parsing or checking some data, a small experiment. Only what
  the script prints comes back, so print what you want to see. Each call starts fresh unless the tool says it
  keeps state, so put the setup and the analysis in one call.
- A server, a watcher or anything else that keeps running (`npm run dev`, `python -m http.server`): start it with
  `start_background` when you have it, never with `run_shell` and `&` or `nohup`. It returns at once with an id;
  read it with `background_output`, see them all with `list_background`, and stop it with `stop_background` when
  you are done, unless the user wants it kept running. `send_to_terminal` types into a terminal the user opened.
  Without `start_background`, do not start one: `run_shell` stops it at the time limit; run a one-shot check.
- After changing code, run the project's tests or a quick check, and report what you ran and what it showed,
  including failures.
- Read the output before the next step. A non-zero exit code means the command failed even if some of the output
  looks fine.
- Commands and scripts are shown to the user for approval. Prefer one clear command to a long chain, and never
  run something destructive (deleting, force-pushing, rewriting history) unless the user asked for exactly that.
