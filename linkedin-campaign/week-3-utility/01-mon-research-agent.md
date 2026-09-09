# Week 3 · Monday — Build a research agent

**When to post:** Monday, 8:00–10:00 AM
**Format:** Text + code + before/after screenshot
**Goal:** Get people to actually try building something concrete.

---

## Post text

I built a research agent this weekend. Total code: 35 lines. Total
time: two hours, most of it tuning the prompt.

What it does:

- Takes a topic
- Searches the web, follows the strongest sources
- Cross-checks facts across sources
- Writes a briefing to disk with inline citations
- Refuses to make claims it cannot cite

Here is the whole thing:

```python
from pathlib import Path
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    web_search_tool, web_fetch_tool,
)

WORKSPACE = Path("./research")
WORKSPACE.mkdir(exist_ok=True)

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=[web_search_tool(), web_fetch_tool(cache_dir=str(WORKSPACE))],
    permissions=Permissions.full_access([str(WORKSPACE)]),
    max_iterations=15,
)

BRIEF = (
    "Research the topic: {topic}. Write a 400-word briefing to "
    "./research/briefing.md. Every factual claim needs a citation "
    "in the form [Source Title](URL). If you cannot cite a claim, "
    "leave it out. Prefer primary sources."
)

result = runner.invoke(BRIEF.format(topic="the state of LLM inference in 2026"))
print(result.content)
```

Where I would take this next:

- Add a `sources.json` output so downstream tools can consume the
  citation list programmatically
- Swap `AgentType.ReAct` for a `Supervisor` if I want a
  planner-worker split (planner picks the sub-topics, workers do the
  research in parallel)
- Add an eval that grades citations — every claim must map to a
  URL that actually contains the claim

If you build one, drop the topic + the briefing in the comments.
I want to see the range.

`pip install agentx-dev`

#python #ai #llm #agents #buildinpublic

---

## Media

Two screenshots side by side:

1. The terminal running the agent
2. The resulting `briefing.md` opened in an editor, with citations
   visible

Alternative: a 30-second video that shows the whole loop end to end.

## Hashtags

`#python #ai #llm #agents #buildinpublic`

## First-comment reply

> Full runnable example is in the repo. If you build a variant,
> screenshot it here — I will share the best ones next week.
>
> https://github.com/shadrach098/agentx_dev/tree/master/examples

## Engagement plan

- Ask for topics people would want researched. Even if they never
  build the agent, the comment thread turns into a mini-brainstorm
  that reaches their networks.
- Feature 2–3 of the best variants in next Monday's post if you get
  submissions. Turns your audience into your content.

## Success signal

At least one person shares a screenshot of their own working
version.
