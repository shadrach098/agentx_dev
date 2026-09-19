# Media with agents

Give an agent images, PDFs or audio alongside its task. The files ride on
the user's turn; the agent can still use its tools, remember the
conversation, and return typed output.

## Attach files to a run

```python
from agentx_dev import AgentRunner, AgentType, Claude, Media

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

result = runner.invoke(
    "What does this chart show, and does the report agree with it?",
    media=["q3_chart.png", "q3_report.pdf"],
)
print(result.content)
```

`media=` works the same on every runner entry point:

```python
runner.invoke(task, media=[...])
for event in runner.stream(task, media=[...]): ...
await async_runner.ainvoke(task, media=[...])
async for event in async_runner.astream(task, media=[...]): ...
```

## With tools

The model looks at the files and then uses its tools as normal. Here it
reads a receipt photo and writes its findings into the workspace:

```python
from agentx_dev import AgentRunner, AgentType, GPT, Media, Permissions

runner = AgentRunner(
    model=GPT(model="gpt-4o"),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./inbox"]),
)

runner.invoke(
    "Compare the invoice to the receipt photo and save any mismatched "
    "line items to mismatches.md.",
    media=["./inbox/invoice.pdf", Media.image("./inbox/receipt.jpg", detail="high")],
)
```

## Large data: let the agent use pandas

`media=` sends a file's full contents in the prompt. That's right for a
short CSV, but a 50,000-row export would go over the size limit, and
past the model's context window. For real datasets, give the agent the
file and Python, and let it run the analysis:

```python
from agentx_dev import AgentRunner, AgentType, GPT, Permissions

runner = AgentRunner(
    model=GPT(model="gpt-4o"),
    agent=AgentType.ReAct,
    permissions=Permissions.full_access(["./data"]),     # sales.xlsx lives here
)
runner.invoke("Load sales.xlsx with pandas and tell me which region grew fastest.")
```

`run_python` starts in the workspace and uses your program's Python
environment, so `pd.read_excel("sales.xlsx")` works once pandas and
openpyxl are installed there. The model only sees what its code
prints: a total, a grouped summary, a chart path — not every row.

| Data | Use |
|---|---|
| a short CSV or one small sheet | `media=["file.csv"]` — the model reads it all |
| a big CSV, many sheets, calculations | the agent + pandas, as above |

## Files in the agent's workspace

The model sees a file only when it's **attached**. Mentioning a path in
the task, or letting the agent call `read_path`, doesn't show it
anything — `read_path` is a text reader and returns
`file is not utf-8 text` for an image.

```python
# The model can see it:
runner.invoke("Describe the photo", media=["./my_workspace/bruce.jpeg"])

# The model can't — it only gets the file name:
runner.invoke("Describe the photo at /bruce.jpeg")
```

Media paths are relative to where your program runs, so include the
workspace folder (`./my_workspace/bruce.jpeg`). Inside the workspace, the
file tools treat a leading slash as the workspace root, and `run_python`
starts in the workspace — useful when the agent needs to *process* a file
(resize it, read its metadata) rather than look at it. See
[Permissions](../concepts/permissions.md#workspace-paths).

## Typed output

Combine media with `output_schema=` to get validated data back from an
agent run:

```python
from pydantic import BaseModel

class ExpenseClaim(BaseModel):
    merchant: str
    total: float
    currency: str

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[],
                     output_schema=ExpenseClaim)
claim = runner.invoke("Extract the expense claim.", media=["receipt.jpg"]).output
```

## Conversations

Images in earlier turns stay images. Pass `completion.history` back, or
keep your own history with `Media` in it:

```python
first = runner.invoke("Here's the floor plan.", media=["plan.png"])
runner.invoke("Where could a second bathroom go?", chat_history=first.history)
```

History stores media as plain JSON, so `Session.save()` works with media
in the conversation.

## Message lists

`runner.invoke` also accepts a chat-style message list. Media in the
last user message is attached, and its text becomes the task:

```python
runner.invoke([{"role": "user", "content": ["Read this receipt", Media.image("r.jpg")]}])
```

## Limits

- Tools return text. A tool can't hand the model an image to look at
  mid-run — attach what the model needs up front.
- `Supervisor` and `HandoffCoordinator` don't forward media to
  specialists yet. Call the specialist runner directly with `media=`.
- Memory (`auto_memory`) keeps the text of each turn, not the media.

See pattern [#26](../cookbook/patterns.md#26-vision-and-document-review-agent-34)
in the cookbook for a complete review agent.
