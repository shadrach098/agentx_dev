# Week 1 · Monday — Announcement

**When to post:** Monday, 8:00–10:00 AM your local time
**Format:** Text-only (LinkedIn's algorithm favors native text on
launch posts)
**Goal:** Introduce yourself + the framework. Establish that you are
the sole builder.

---

## Post text

I spent the last several months building a Python framework for LLM
agents. It shipped this week. It is called `agentx-dev`.

I built it because every existing option asked me to choose sides.
LangChain wanted me all-in on their abstractions. Provider SDKs wanted
me all-in on their model. The rest wanted me to write the boring parts
myself again — the tool loop, the permission checks, the streaming,
the traces, the retries, the sessions.

So I wrote one framework that:

- treats Claude and GPT as first-class citizens (swap the constructor,
  keep the code)
- ships default tools (web search, filesystem, python exec) behind a
  proper permission system, not a "trust me" flag
- has real multi-agent orchestration — Supervisor for planning,
  Handoffs for handoffs, not fake ones
- streams both text tokens and structured step events, so you can
  debug what the model is actually thinking
- includes RAG, evals, prompt caching, parallel tool calls, the batch
  API, and MCP — because these should not be a separate library each

One person built this. It is on PyPI. It is on GitHub. The docs are
real. I would love your honest first impression.

```
pip install agentx-dev
```

If you build with LLMs, take 10 minutes on the Getting Started page
and tell me what breaks.

#python #ai #llm #opensource #agents

---

## Media

None. This is a text-only launch post. Text-only posts consistently
out-reach media posts on launch day because LinkedIn treats them as
"personal update" rather than "promotional".

## Hashtags

`#python #ai #llm #opensource #agents`

Put them on the last line, single space, lowercase.

## First-comment reply (post this within 60 seconds)

> Repo + docs here — comment with what you build, I read every one.
>
> GitHub: https://github.com/shadrach098/agentx_dev
> Docs: (your Pages URL)
> PyPI: https://pypi.org/project/agentx-dev/

## Engagement plan

- Reply to every comment in the first 90 minutes. Ask one specific
  follow-up ("what were you using before?" / "what are you building?").
- Comment thoughtfully on 5 other AI-builder posts today. LinkedIn
  rewards two-way engagement on launch day.
- Do NOT ask friends to like the post. LinkedIn detects coordinated
  early engagement and dampens reach.

## Success signal

3× your normal impression count. If you usually get 500 impressions
per post, this should get 1,500+. If it does not, do not repost —
just do Wednesday.
