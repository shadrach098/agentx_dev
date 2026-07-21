<!--
Thanks for the PR. A few pointers so it lands quickly:
  * Keep the change focused. One PR per feature or fix.
  * Add or update tests in `tests/`.
  * If you touched a public API, update `docs/` and regenerate `host/data.js`.
  * Follow the commit + PR title style: `<type>: <short summary>`.
    Types: feat / fix / docs / test / refactor / chore.
-->

## What this changes

<!-- One or two sentences. What is the user-visible difference? -->

## Why

<!-- The problem this solves. Link an issue if there is one. -->

## How

<!-- Bullet the main mechanics. Note any tradeoffs. -->

## Testing

<!-- What did you run? `pytest tests/` output preferred. -->

## Checklist

- [ ] Tests added or updated
- [ ] Docs updated (if public API changed)
- [ ] `pytest tests/ -q` passes locally
- [ ] `python host/build_data.py` runs clean (if docs changed)
- [ ] Backwards-compatible OR breaking change explicitly flagged in the PR title
