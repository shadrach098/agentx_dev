# Contributing to agentx-dev

Thanks for your interest. This is a solo-authored framework, so a few
process choices are opinionated -- they exist to keep the surface area
small and the codebase readable.

## Getting set up

```bash
git clone https://github.com/shadrach098/Bruce_framework.git
cd Bruce_framework
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev,anthropic]"
```

Sanity check:

```bash
pytest tests/            # should show 127 passing, 3 skipped
python -c "import agentx_dev; print(agentx_dev.__all__)"
```

## Before you open a PR

1. **Run the tests.** `pytest tests/ -q` from the repo root.
2. **Add tests** for whatever you changed. Every existing subsystem
   has a `tests/test_<module>.py`; add cases there.
3. **Keep the change focused.** One PR per feature or fix. If your
   change touches five modules and adds three exports, split it.
4. **Preserve existing behavior** unless the change is explicitly a
   breaking one. Backwards-compat is a real value.
5. **Update docs** if you added or changed a public API. `docs/`
   holds the source; run `python host/build_data.py` to regenerate
   the browsable site's `data.js`.

## Commit + PR style

- **Commit messages:** `<type>: <short summary>` where type is one of
  `feat` / `fix` / `docs` / `test` / `refactor` / `chore`. Follow
  with a blank line and the body if the summary isn't enough. Look
  at recent commits (`git log --oneline -20`) for the shape.
- **PR titles:** same shape as the commit message. If the PR has
  multiple commits, the title summarizes the whole PR.
- **PR descriptions:** what changed, why, and one before/after
  example if the change is user-visible.

## What lands and what doesn't

Things that land easily:

- Bug fixes with a regression test.
- New tools that follow the existing patterns.
- Docs improvements (more examples, clearer explanations, fixing typos).
- New vector-store adapters or provider adapters that follow the
  existing interface.
- Test coverage for uncovered paths.

Things that need discussion first (open an issue):

- New public classes or modules.
- Changes to public method signatures.
- Anything that grows the dependency footprint of the base install.
- Anything that reshapes the runner loop, `Supervisor`, or
  `HandoffCoordinator`.

Things that don't land:

- Style-only refactors that reshape existing files.
- Adding a "generic" abstraction over one specific thing.
- Wrapping existing SDKs "for consistency."
- LangChain-compatibility shims.
- Auto-generated boilerplate.

## Code style

- **Docstrings are the source of truth.** Every public class + method
  gets one. Say *why* the choice was made, not just what it does.
- **No trailing summary comments** at the end of functions.
- **Type hints on public APIs.** Not needed on internal helpers if
  the shape is obvious from three lines up.
- **Small files > large files**, but one-module features > splitting
  a single concept across three files for tidiness.

## Security-sensitive changes

The framework has explicit security surfaces:

- `Permissions` + sandbox enforcement in `DefaultTools.py`
- `run_python` HMAC-signed persistent state in `AutoSetup.py`
- SSRF guard in `WebTools.py`
- Session-id sanitizer in `DefaultTools.py`

If your change touches any of these, please:

1. Add a test that exercises the security check.
2. Add a note in the PR describing the threat model you considered.

See `AGENTX.md` at the repo root for the guardrails the framework
imposes on itself.

## Questions?

Open an issue with the "question" label. Solo maintainer, so response
time varies -- a good repro / minimal example is the single biggest
help.
