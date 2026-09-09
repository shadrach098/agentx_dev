# Week 2 · Friday — Streaming + traces

**When to post:** Friday, 11:00 AM–1:00 PM
**Format:** Text + short code snippet (or video if you can record it)
**Goal:** Show that observability is not an afterthought.

---

## Post text

If your agent framework only streams text tokens, you are debugging
blind.

An agent's real output is not just text. It is:

- the thought before the tool call
- the tool call itself (name + args)
- the tool result that came back
- the next thought
- the final answer

`agentx-dev` streams all of it, in one event loop, in the order it
happened:

```python
for event in runner.stream("What's the weather in Tokyo?"):
    if event["type"] == "thought":
        print("[thinking]", event["content"])
    elif event["type"] == "tool_call":
        print(f"[call] {event['name']}({event['args']})")
    elif event["type"] == "tool_result":
        print(f"[result] {event['result'][:80]}")
    elif event["type"] == "final":
        print(f"[answer] {event['content']}")
```

Want text tokens on top of that? Add `stream_tokens=True` and you
get `text_delta` events interleaved with the step events. Async
version mirrors sync one-for-one.

When you need to go beyond the terminal, there is a trace viewer.
It ingests the same events and lets you inspect a full agent run
step by step — tool calls, latencies, cost per call, tokens spent.

The version I ship uses this to catch my own regressions. If a bug
report says "the agent is doing X when it should not", the first
thing I do is load the trace.

I would rather ship 10% fewer features and 100% more observability
than the other way around.

`pip install agentx-dev`

#python #ai #llm #observability #agents

---

## Media

STRONG choice: a 15-second screen recording of the streaming
output in a terminal, with the thoughts / tool calls / results
flowing past.

Alternative: a screenshot of the trace viewer if it renders well.

## Hashtags

`#python #ai #llm #observability #agents`

## First-comment reply

> The full streaming guide is in the docs, including how to wire it
> into FastAPI + Server-Sent Events for a web frontend. Sub-100 lines
> to a real-time chat UI.
>
> Docs: (your Pages URL)/guides/streaming.html

## Engagement plan

- The "debugging blind" line will attract people telling their own
  debugging horror stories. Encourage them.
- Ask if anyone would want a trace viewer walkthrough as a next
  post. Let the audience tell you what to make.

## Success signal

Someone comments "I need this". That is your next post's opening
line.
