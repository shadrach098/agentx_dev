# Week 1 · Wednesday — Origin story

**When to post:** Wednesday, 8:00–10:00 AM
**Format:** Text-only, personal
**Goal:** Humanize the project. People follow builders, not repos.

---

## Post text

Two years ago I was pasting the same 40 lines of tool-loop plumbing
into every agent project I started.

Six months ago I got tired.

I opened a new folder and started `agentx-dev`. I did not tell anyone
I was working on it. I did not tweet about it. I built it in the
open on GitHub but I did not promote it once — I wanted to see if I
could get it to a state where I would actually use it in production
before I asked anyone else to.

Three things I decided on day one, that I refused to compromise on:

1. **Both providers, always.** Every example works with Claude and
   GPT. Not "supports both" — actually equal citizens. If a feature
   only works on one, it is a bug.

2. **No trust-me tools.** The framework ships filesystem access,
   shell exec, and python evaluation. They are gated behind explicit
   Permissions objects. If you want an agent to write to disk, you
   pass `Permissions.full_access(["./workspace"])` and it is
   sandboxed there.

3. **Docs are code.** Every code snippet in the docs runs. If it
   drifts, the CI catches it. I read too many "getting started"
   pages that had not compiled since 2023.

The 3.1.3 version went out this week. Real multi-agent orchestration,
RAG, evals, streaming, sessions, MCP, prompt caching, batch API.

It is not perfect. There are bugs I know about and probably bugs I
do not. But it is the framework I wish had existed when I started.

If you have ever pasted the same tool loop into three projects in a
row, take a look.

`pip install agentx-dev`

#python #ai #llm #buildinpublic #agents

---

## Media

Optional: a single screenshot of the CHANGELOG.md file. Shows the
work receipts without being promotional.

## Hashtags

`#python #ai #llm #buildinpublic #agents`

## First-comment reply

> The `Permissions` decision was the one I was most nervous about.
> Curious what you think — is opt-in capability sandboxing the right
> default, or too paternalistic for a dev tool?
>
> Repo: https://github.com/shadrach098/agentx_dev

## Engagement plan

- The `#buildinpublic` tag pulls in an audience that loves reply
  threads. Engage.
- If anyone asks "why not just use LangChain / LlamaIndex / etc.",
  do NOT trash them. Say: "great for X, this is better for Y."
- Save 20 minutes to answer comments. Personal posts get more of
  them than technical ones.

## Success signal

Comments > 10 in the first 3 hours. This post is graded on
conversation, not reach.
