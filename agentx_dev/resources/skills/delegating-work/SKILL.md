---
name: delegating-work
description: When to hand work to helpers - start_team_job for big multi-part jobs, delegate for a self-contained side task.
tools: [start_team_job, delegate]
version: 1
---
# Delegating work

Do small and medium jobs yourself. Hand work off only when it would take over your own context.

- `start_team_job` is for LARGE, multi-part work: several files or areas, with research, changes and tests
  together. A team of helpers does it under a supervisor and you get the result back. It makes many model calls,
  and the user may be asked before it starts. If a job ends partial and the task is not finished, call it again
  for what remains, saying what is already done.
- `delegate` hands one self-contained piece of work to a fresh sub-agent that sees only the `task` text you give
  it. Put every fact it needs in `task`: file names, the goal, what a good answer looks like. Use it for side
  jobs that would clutter your context (reading many files, researching a subtopic), not for trivial steps.
- Whatever comes back is a summary. Check anything important yourself before telling the user it is done.
