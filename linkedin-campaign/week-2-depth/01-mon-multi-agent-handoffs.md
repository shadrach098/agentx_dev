# Week 2 · Monday — Multi-agent handoffs

**When to post:** Monday, 8:00–10:00 AM
**Format:** Text + short code snippet
**Goal:** Establish technical credibility. Show you understand the
hard parts of multi-agent, not just the demo parts.

---

## Post text

Everyone talks about "multi-agent". Almost nobody explains what
actually breaks when you try it.

Here is what breaks:

You have a Researcher agent. It calls three tools, gets three tool
results, writes a summary. Now you want to hand off to a Writer
agent that turns the summary into a briefing.

Naive handoff: append the Researcher's whole history to the Writer's
context and hit go. This fails for a specific reason: the second
model sees `tool_use` blocks with IDs from the first model's tool
calls, and no matching `tool_result` blocks — because in a handoff,
you do not want the new agent to think it made those calls itself.

Anthropic and OpenAI both reject the request. Or worse — they
accept it and the new model gets confused about who called what.

The fix is not glamorous. You have to walk the message list and
either drop or convert the assistant's tool_use blocks into plain
text before the next agent sees them. `agentx-dev` calls this
`_sanitize_history_for_next_agent`. It runs on every handoff so
you never think about it.

```python
from agentx_dev import HandoffCoordinator, AgentRunner, AgentType, Claude

researcher = AgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[web_search_tool(), web_fetch_tool()],
)
writer = AgentRunner(
    model=Claude(), agent=AgentType.Instruction_Tuned,
    tools=[],
)

coordinator = HandoffCoordinator(agents={
    "researcher": researcher,
    "writer": writer,
})
result = coordinator.invoke(
    "Research SWE-bench and write a briefing.",
    start_with="researcher",
)
```

The Researcher's tool history is sanitized before the Writer sees
it. You do not touch the wiring. It just works.

If your multi-agent system feels flaky and you cannot tell why —
this is where I would look first.

#python #ai #llm #multiagent #agents

---

## Media

Optional carousel (3 slides):

1. "Multi-agent handoffs: the bug nobody tells you about"
2. Diagram: Agent A's messages → sanitizer → Agent B's context.
   Show the tool_use blocks getting converted to text.
3. Screenshot of the working code + result.

Alternative: no media. The technical hook is strong enough on its own.

## Hashtags

`#python #ai #llm #multiagent #agents`

## First-comment reply

> This bug cost me a full weekend of debugging when I hit it the
> first time. Curious how other frameworks handle it — reply with
> what you use and I will read the source.
>
> https://github.com/shadrach098/agentx_dev

## Engagement plan

- Technical posts attract technical replies. Have your laptop open
  so you can look at specific issues people paste in the comments.
- If a competitor's user shows up defending their tool, thank them
  for the perspective and move on. Never argue.

## Success signal

At least 2 comments from people who have hit this exact bug. Those
comments are worth more than any number of "great post" replies.
