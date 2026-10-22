# AGENTX.md

This repo is agentx_dev, a Python framework for tool-using agents, and `agentx`, its terminal app. Check the
Skills list before choosing tools and follow the skills that fit. This file is sent with every request: keep it short.

## Layout
- `agentx_dev/Runner/`: the agent loop (`AgentRun.py`, `AsyncAgentRun.py`), trimming (`Trim.py`).
- `agentx_dev/cli/`: the terminal app (`app.py`, `commands.py`, `team.py`, `tools.py`, `context.py`, `ui.py`).
- `agentx_dev/Skills/` and `agentx_dev/resources/skills/`: the skill store and the built-in skills.
- `agentx_dev/ChatModel.py`: providers and token usage. `DefaultTools.py`: the file and command tools.
- `tests/`: pytest; `tests/conftest.py` has `MockModel` (scripted replies, no API calls).
- `docs/`: user docs. The full agent guide (guardrails, tools, Supervisor rules) is `docs/reference/agent-guide.md`.

## Checks
- Run `python -m pytest -q` (about 2,700 tests, under two minutes) after a change and report the result.
- New behaviour gets a test with `MockModel`; never call a real API in tests.
- After editing `docs/`, run `python host/build_data.py`.
- Add a line to `CHANGELOG.md` under `[Unreleased]` for anything a user would notice.

## Conventions
- Plain, short English in messages, docs and comments. Keep the existing style.
- Read a file before editing it; read only the lines you need from big files (offset and limit).
- Print only what you need from `run_python` and `run_shell`; output is cut at 30,000 characters.
- For code structure (functions, classes, counts) use Python's `ast`, not string matching.
- If a tool warns about a repeated call, change approach instead of repeating it.
- Your final reply is what the user sees: give the facts found, with file names and line numbers.
- Commits: no `Co-Authored-By` lines.
