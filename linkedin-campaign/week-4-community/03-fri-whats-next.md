# Week 4 · Friday — What's next + contributor call

**When to post:** Friday, 11:00 AM–1:00 PM
**Format:** Text + roadmap bullets
**Goal:** Convert readers into contributors. Turn the launch
campaign into a durable community.

---

## Post text

Four weeks ago I posted about `agentx-dev` for the first time.

Since then:

- (fill in) new GitHub stars
- (fill in) PyPI downloads this month
- (fill in) issues opened by real users
- (fill in) contributors who have sent a PR

I did not have any of that a month ago. Thank you.

Here is what is next.

**3.2 is in progress.** The theme is *making agents feel like
software*, not like a science project.

- **First-class agent testing.** The evals harness gets fixtures,
  snapshot testing, and a diff-friendly output. So you can wire it
  into CI and treat regressions like broken unit tests.
- **Cost accounting.** Every agent run produces a machine-readable
  cost breakdown by tool, model, and step. You can budget your
  runs the same way you budget your cloud bill.
- **A proper agent debugger.** Set a breakpoint on a tool call.
  Inspect the message history. Step forward, step back. If you
  have used PyCharm's debugger for Python, that experience for
  agents.
- **More MCP servers ready to plug in.** GitHub, Postgres, Slack,
  each behind a Permissions object so you get the security model
  for free.

**Where I need help.**

- If you write technical docs, a lot of the framework's advanced
  features have shallow docs. This is the single highest-impact
  thing anyone can contribute.
- If you use a vector store I do not support yet (Weaviate,
  Milvus, Turbopuffer), a fresh adapter is 100–150 lines. I can
  review your PR the same week.
- If you are building something interesting with the framework,
  send me a screenshot for the README's "built with" section. I
  will credit you.

Good open source is not one person. I have been a one-person
project because that is what a launch looks like. The next chapter
is not.

`pip install agentx-dev`

Repo (star to follow the roadmap):
https://github.com/shadrach098/agentx_dev

#python #ai #llm #opensource #agents

---

## Media

Optional: a screenshot of the GitHub Insights page showing the
star curve from launch to today. Only include if the curve is
compelling — a flat curve on a "here is what's next" post is bad
signal.

## Hashtags

`#python #ai #llm #opensource #agents`

## First-comment reply

> Contributor guide is here: (your CONTRIBUTING.md URL). If you
> pick up an issue, comment on it first so we do not duplicate
> work.
>
> Good-first-issue label:
> https://github.com/shadrach098/agentx_dev/labels/good%20first%20issue

## Engagement plan

- Any comment offering help is a real lead. Reply within an hour.
  Move to DM if it turns into a real conversation.
- Tag the "good first issue" label in the repo — visitors from
  this post should have somewhere obvious to land.
- End of week: send a personal thank-you note (public or DM) to
  every commenter who pledged something. Community starts with
  memory.

## Success signal

At least one person opens a PR or an issue that they mention was
prompted by this post. That is the whole point of the campaign.
