# Week 3 · Friday — Support ticket triage

**When to post:** Friday, 11:00 AM–1:00 PM
**Format:** Text + code + Pydantic schema screenshot
**Goal:** Show structured output + a use case non-developers can
imagine paying for.

---

## Post text

Customer support teams are drowning. Every ticket looks the same
until it is not, and the "not" ones are the expensive ones.

Here is a triage agent that categorizes, drafts a reply, and knows
when to escalate:

```python
from typing import Literal
from pydantic import BaseModel, Field
from agentx_dev import AgentRunner, AgentType, Claude

class Triage(BaseModel):
    category: Literal["billing", "bug", "feature_request", "account", "other"]
    priority: Literal["low", "medium", "high", "critical"]
    sentiment: Literal["angry", "frustrated", "neutral", "happy"]
    draft_reply: str = Field(description="Reply in our brand voice. Max 4 sentences.")
    escalate_to_human: bool = Field(
        description="True if the issue involves data loss, refunds >$500, "
                    "legal threats, or a customer who has been escalated before."
    )
    reasoning: str = Field(description="One sentence explanation of the routing.")

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

ticket = """
Subject: Where is my $2,400??
I have been trying to get a refund since December. Nobody responds.
I am about to file a chargeback with my bank and post about this
publicly. This is unacceptable.
"""

result = runner.invoke(f"Triage this ticket:\n{ticket}", output_schema=Triage)
triage = result.output   # Typed Pydantic instance

print(triage.priority)              # "critical"
print(triage.escalate_to_human)     # True
print(triage.draft_reply)           # The draft
```

The critical part: `output_schema=Triage` tells the runner to
validate the final answer against the Pydantic model. If the model
returns malformed JSON, the framework raises a real error — you do
not ship broken data downstream.

Wire this to Zendesk / Front / Intercom webhooks, drop the drafts
in your queue, escalate the criticals, and your team's first-touch
time drops by whatever percent your incoming volume is.

For teams with more complex flows, add a `HandoffCoordinator` so
the "billing" tickets go to a specialist agent that knows your
refund policy, and "bug" tickets go to one that can search your
issue tracker.

`pip install agentx-dev`

#python #ai #llm #customersupport #agents

---

## Media

Screenshot of the terminal output showing the `Triage` object
printed with real values. Shows non-developers what "structured
output" actually looks like.

## Hashtags

`#python #ai #llm #customersupport #agents`

## First-comment reply

> The structured-output guide covers both patterns — one-shot
> extraction and full agent-loop-then-validate — and how to handle
> validation errors.
>
> Docs: (your Pages URL)/guides/structured-output.html

## Engagement plan

- Non-technical readers will react to this one more than the
  code-heavy posts. Reply to non-technical comments in plain
  language, not code.
- If a founder / support-team-lead reaches out, that is a lead. Take
  it to DM after 1–2 public replies.

## Success signal

At least one DM from someone whose team could use this. This is
the highest-conversion post of the campaign.
