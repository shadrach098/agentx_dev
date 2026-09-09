# Week 4 · Monday — Lessons learned

**When to post:** Monday, 8:00–10:00 AM
**Format:** Text-only. Personal, honest, no code.
**Goal:** Deepen the personal connection. Personal-lesson posts
consistently out-reach every other format on LinkedIn.

---

## Post text

Six months of building `agentx-dev` alone. Here is what I got
wrong.

**1. I built the abstractions before I built the use cases.**

The first version had a beautiful `BaseAgent` class with hooks,
lifecycle events, pluggable middleware. It was elegant. It was also
almost impossible to use. I threw all of it away and started from
a single working script and refactored *toward* abstractions only
when I needed the second one.

Lesson: write the code you want to be able to write, then build
what makes it possible. Not the other way around.

**2. I under-documented the boring stuff.**

I wrote long docs for the impressive features — RAG, multi-agent,
prompt caching. I wrote almost nothing for the parts that made
those features work — Permissions, tool descriptions, how to
choose an AgentType. Guess which one people got stuck on?

The unglamorous docs are the ones people actually need. Every
week now I add a "why this exists" section to something that
already exists.

**3. I supported both providers from day one, and it was the best
technical decision I made.**

I almost went Claude-only because I use Claude every day. But I
forced myself to write every example twice — once with Claude,
once with GPT. The framework's whole abstraction layer got better
because I could not cheat.

If your framework only works with your favorite tool, it is not a
framework, it is a wrapper.

**4. I shipped too late.**

I sat on the code for weeks past the point where I should have
released. Every version I did not ship was a version that did not
get feedback. The bugs users found in the first 48 hours after
launch are bugs I would have found in month 7.

Release earlier than you are comfortable with.

If you are building something alone in a folder somewhere, the
worst version of your project that is actually shipped is worth
more than the best version that is not.

#python #ai #llm #buildinpublic #agents

---

## Media

None. Text-only lesson posts consistently out-reach media on
LinkedIn.

## Hashtags

`#python #ai #llm #buildinpublic #agents`

## First-comment reply

> If you are working on something in a folder somewhere — this is
> your reminder to ship. What are you sitting on?
>
> Repo: https://github.com/shadrach098/agentx_dev

## Engagement plan

- This is the reply-heavy post of the campaign. Set aside 90
  minutes to respond thoughtfully to every comment.
- People will share what THEY are building in the replies. Save
  those — they are your future guest posts / features.
- Do NOT try to sell the framework in this post's comments. Let it
  breathe. The post itself is the sell.

## Success signal

Comments from people telling you what they are building. This
post is graded on empathy, not clicks.
