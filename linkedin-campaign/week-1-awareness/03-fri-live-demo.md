# Week 1 · Friday — Live demo

**When to post:** Friday, 11:00 AM–1:00 PM
**Format:** Text + code snippet (LinkedIn's native code block)
**Goal:** Show that the framework is real in <30 seconds of reading.

---

## Post text

You can go from "empty file" to "agent that browses the web, reads
your files, and cites its sources" in 12 lines of Python.

Nothing hidden. Here is the whole thing.

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions
from agentx_dev import web_search_tool, web_fetch_tool

runner = AgentRunner(
    model=Claude(),                    # or GPT() -- same API
    agent=AgentType.ReAct,
    tools=[web_search_tool(), web_fetch_tool()],
    permissions=Permissions.full_access(["./workspace"]),
)

result = runner.invoke(
    "Research the current state of the SWE-bench leaderboard "
    "and write a 200-word briefing to ./workspace/briefing.md. "
    "Cite every claim with a URL."
)

print(result.content)
```

What happens when you run this:

- The agent calls `web_search` a few times, follows the top results
  with `web_fetch`, notices when a page is not useful, tries another.
- It writes the briefing to disk inside `./workspace/` — nowhere
  else, because `Permissions.full_access` only granted that path.
- It streams tool calls, tool results, and final answer as they
  happen if you use `runner.stream(...)` instead.
- Every citation is a real URL it actually opened, not one it
  hallucinated, because the tool returns the URL.

Swap `Claude()` for `GPT()` and the code does not change.

`pip install agentx-dev`

#python #ai #llm #opensource #agents

---

## Media

Optional but strong: a 20-second screen recording of running the
snippet in a terminal, with the streaming output visible. LinkedIn
weights native video heavily.

If no video, a single screenshot of the resulting `briefing.md` file
opened in an editor. Proves it actually worked.

## Hashtags

`#python #ai #llm #opensource #agents`

## First-comment reply

> Full runnable example is in the repo under `examples/`. If you try
> it, drop the topic you researched — I want to see the range.
>
> https://github.com/shadrach098/agentx_dev/tree/master/examples

## Engagement plan

- Encourage people to run it and comment their output. Every comment
  showing an actual result is worth 20 "cool" replies for the algo.
- If someone posts a bug in the comments, respond with the fix in a
  reply (not a DM). Public bug fixes look like a healthy project.

## Success signal

At least 3 people commenting "just tried it" or a screenshot of
their own run. Reach is nice; adoption is the goal.
