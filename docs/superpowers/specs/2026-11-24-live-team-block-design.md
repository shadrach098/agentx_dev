# The team job, live in the conversation (app window)

## Why

In the agentx app window a team job shows twice: the dark terminal-style block in the conversation gets a line only
when a step ends (`✓ helper · label  Done (…)` and the start of its report), and a separate gray **Team** panel below
it lists the running and waiting steps live. The user finds the gray panel not nice and wants one view: the dark block
in the conversation, updated as the job goes.

## What the user approved

```
Team · 2 running · 1 of 4 done
✓ positioning-researcher · positioning    Done (4 tool uses · 58k tokens · 16s)
  └ Initial audience: Python developers building LLM agents who…
● launch-fact-checker · fact check        Reading tests/test_split_refresh.py · 18 tools · 1m 12s
● measurement-planner · measurement       Searching 4 patterns · 16s
○ announcement-editor · edit              waiting
```

- While a team job runs, the dark block in the conversation is the team's live view, updated in place.
- A running step is one line (its helper, label, what it is doing now, tool count, time), updated in place; rows have a
  fixed height, so nothing jumps while the text changes.
- When a step ends its line becomes today's ended line (`✓ … Done (…)`, `✗ … Failed`, `◐ … Partial`, `– … Skipped`,
  `↺ … on <fallback>`), with the start of its report under it, as today.
- Waiting steps show as `○ … waiting`.
- Clicking a row opens its details (what the gray panel shows today: the last actions, the counts, the fallback note),
  clicking again closes it.
- The gray Team panel goes away.
- The terminal (no `--ui`) is unchanged.

## Calls made (not asked)

- Big teams: the block stops growing at about 10 rows and scrolls inside, keeping the running steps in view; 20
  helpers never take the whole screen.
- The header line counts running and done like the panel's head does today.
- After the job ends the block stays in the conversation as the record of the job (every row in its ended form); a
  reloaded window or a reopened conversation shows that final form.
- The orb keeps getting its helper counts as today (`status.team` stays; only the panel's drawing moves).

## Non-goals

- No change to how team jobs run, the supervisor, recovery, budgets or the plan card (the numbered plan shown before
  the job starts).
- No change to the terminal's live Team group.

## Left to the builder

How the window learns of rows (today `status.team.rows` from `TeamPanel.snapshot()` plus the printed ended lines), how
the printed ended lines are replaced or merged so a step is not shown twice, the markup and styles (the dark block's
look), and the tests.
