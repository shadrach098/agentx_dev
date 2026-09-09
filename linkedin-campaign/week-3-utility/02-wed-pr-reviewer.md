# Week 3 · Wednesday — Agent that reviews your PRs

**When to post:** Wednesday, 8:00–10:00 AM
**Format:** Text + code + concrete before/after
**Goal:** Show a use case people would actually pay for, then give
it away.

---

## Post text

Last week I got my own agent to review my pull requests before I
open them. It caught three things I would have missed.

The setup is not complicated:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude, Permissions,
    default_tools_from_permissions,
)

runner = AgentRunner(
    model=Claude(),
    agent=AgentType.ReAct,
    tools=default_tools_from_permissions(
        Permissions.readonly(["./"]).with_shell(["git"]),
    ),
)

review = runner.invoke("""
Review the diff on my current branch against main.

Look for:
1. Off-by-one errors, null checks I forgot, typos in strings that
   the type checker cannot catch.
2. Tests that assert on implementation details instead of behavior.
3. Public API changes that are not reflected in the docs.
4. Anything I renamed that I forgot to rename everywhere.

Reply as three sections: MUST FIX, WORTH DISCUSSING, NIT.
""")

print(review.content)
```

The critical part is the `Permissions.readonly` line. The agent can
read every file. It can run `git diff`, `git log`, `git status`. It
cannot write anything, cannot push, cannot rm. If it tried, the
framework blocks it before the syscall lands.

That is the same pattern for any agent that touches your code: give
it exactly the capabilities it needs, no more.

Three things it caught this week that I missed:

- A rename I did in one file that broke an import in another
- An error path that returned None instead of raising
- A test that would pass even if the function returned the wrong
  answer, because it only asserted on the return type

You can literally paste this into a project today and use it.

`pip install agentx-dev`

#python #ai #llm #devtools #agents

---

## Media

Screenshot of the terminal output showing the agent's review — the
"MUST FIX / WORTH DISCUSSING / NIT" structure visible. If you can
show a real finding without leaking anything sensitive, it becomes
much more credible.

## Hashtags

`#python #ai #llm #devtools #agents`

## First-comment reply

> The permission system is the reason I trust myself to run this.
> Full guide: (docs URL)/concepts/permissions.html
>
> Repo: https://github.com/shadrach098/agentx_dev

## Engagement plan

- Ask developers what they would want their PR-review agent to
  catch. Very common answer: "our team's specific style rules". Note
  those; they are future post material.
- If a security-minded commenter asks about the permission model,
  answer in depth. That reply thread can carry the post.

## Success signal

At least one comment that says "I am doing this at work tomorrow".
That is intent — the highest-value engagement.
