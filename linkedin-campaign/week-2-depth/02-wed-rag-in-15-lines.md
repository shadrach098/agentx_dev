# Week 2 · Wednesday — RAG in 15 lines

**When to post:** Wednesday, 8:00–10:00 AM
**Format:** Text + code
**Goal:** Prove that RAG in this framework is not a 40-line
boilerplate.

---

## Post text

I got tired of copy-pasting RAG boilerplate. So I put it in the
framework.

This is the whole retrieval-augmented-generation setup:

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    TextSplitter, VectorStore, retrieval_tool,
)

splitter = TextSplitter(chunk_size=800, chunk_overlap=100)
store = VectorStore()
store.add_documents(splitter.split_directory("./knowledge_base"))

runner = AgentRunner(
    model=Claude(),                   # or GPT() -- same API
    agent=AgentType.ReAct,
    tools=[retrieval_tool(store, top_k=5)],
)
answer = runner.invoke("What is our refund policy for enterprise plans?")
print(answer.content)
```

Fifteen lines. That includes the imports.

What is happening:

- `TextSplitter` chunks recursively — paragraph, then line, then
  sentence, then word, then character. It never breaks a code block
  in the middle.
- `VectorStore` is in-memory by default. When you need to go to
  production, swap in Chroma, Qdrant, or pgvector by changing one
  import — same interface.
- `retrieval_tool` is a real tool the agent decides when to call.
  Not a "stuff top 5 chunks into every prompt" hack. The agent asks
  when it needs to ask, and reads the sources back.

For a lot of teams this is the whole RAG stack. Not a separate
library, not a separate framework, not a service to spin up.

`pip install agentx-dev`

#python #ai #llm #rag #agents

---

## Media

Screenshot of the terminal output showing the agent (1) calling
`retrieval_tool`, (2) receiving the chunks, (3) writing the answer
with citations. Shows the flow, not just the result.

## Hashtags

`#python #ai #llm #rag #agents`

## First-comment reply

> Vector store adapters live in `agentx_dev` as separate installs
> — `pip install agentx-dev[chroma]`, `[qdrant]`, or `[pgvector]`.
> Same interface, real production stores when you need them.
>
> Docs: (your Pages URL)/guides/rag.html

## Engagement plan

- Ask commenters what they use for RAG today. Even a two-word
  reply ("just faiss") gives you a next-post idea.
- If someone says "our RAG stack is 40 pages of code" — that is a
  quote you can build a future post around ("here is what a 40-page
  RAG stack looks like in this framework").

## Success signal

Comments from people describing their current stack. That is your
research for future posts.
