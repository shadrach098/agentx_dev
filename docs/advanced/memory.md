# Memory strategies

Six memory implementations ship. Pick based on your conversation shape:


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

| Class | Strategy | Good for |
|---|---|---|
| `ConversationMemory` | Keep everything verbatim | Short conversations |
| `SlidingWindowMemory` | Keep last N messages | Long chat where only recency matters |
| `TokenLimitedMemory` | Drop oldest until under token cap | Long chat with hard cost budget |
| `ImportanceBasedMemory` | Keep by importance score | Head + tail + top-K middle |
| `SummaryMemory` | LLM-summarizes old turns | Very long conversations |
| `SemanticMemory` *(3.1)* | Embed + retrieve by similarity | Multi-topic long-tail recall |

All inherit from `BaseMemory` and expose:

```python
mem.add_message(role, content, **kwargs)
mem.get_messages() -> List[Dict[str, str]]
mem.clear()
```

## Attach to a runner

```python
from agentx_dev import (
    AgentRunner, AgentType, Claude,
    create_windowed_memory,
)

# The framework's auto_memory doesn't wire external memories directly --
# you thread them yourself via chat_history=.
mem = create_windowed_memory(window_size=10)

# On each turn:
mem.add_message("user", user_input)
result = runner.invoke(user_input, chat_history=mem.get_messages())
mem.add_message("assistant", result.content)
```

Or use `auto_memory=True` on the runner for a default in-process
`ConversationMemory` that populates automatically.

## SlidingWindowMemory

Drops the oldest non-system message when the window is exceeded.

```python
from agentx_dev import SlidingWindowMemory

mem = SlidingWindowMemory(max_messages=10, preserve_system=True)
```

Best when only the last N turns are relevant. Fast, no LLM cost.

## TokenLimitedMemory

Estimates tokens per message (~4 chars = 1 token) and drops oldest
until under the cap.

```python
from agentx_dev import TokenLimitedMemory

mem = TokenLimitedMemory(max_tokens=4000, preserve_system=True)
```

Best when you have a hard prompt budget.

## ImportanceBasedMemory

Keep head + tail always; middle is top-K by importance:

```python
from agentx_dev import ImportanceBasedMemory

mem = ImportanceBasedMemory(
    max_messages=20,
    importance_threshold=0.3,   # drop anything scored below this
    preserve_head=2,            # always keep first 2 (task framing)
    preserve_tail=4,            # always keep last 4 (recent context)
)

# Score messages when adding:
mem.add_message("user", "My favorite color is blue.", importance=0.9)
mem.add_message("user", "OK cool.", importance=0.2)   # low signal
```

Result: task framing + recent turns + top-scored middle messages
survive; chronological order preserved.

## SummaryMemory

When conversation crosses a threshold, older messages get compressed
into an LLM-generated summary:

```python
from agentx_dev import SummaryMemory, Claude

llm = Claude()   # or GPT() -- same API
def summarize(messages):
    text = "\n".join(f"[{m.role}] {m.content}" for m in messages)
    return llm.invoke(
        f"Summarize this conversation in 2-3 sentences:\n\n{text}"
    )

mem = SummaryMemory(
    summarizer=summarize,
    max_messages_before_summary=15,
    keep_recent=5,
)
```

Trades LLM calls for prompt-token savings. Best for open-ended chatbots
that run for hours.

## SemanticMemory *(3.1)*

Every message goes into a `VectorStore`; on retrieval, the top-K most
similar older messages are pulled back and injected as a synthetic
system note ahead of the recent tail. Combines "recent-tail wins" with
"long-tail recall by relevance".

```python
from agentx_dev import (
    SemanticMemory, HashEmbeddings, OpenAIEmbeddings,
    create_semantic_memory,
)

# Zero-dependency (deterministic, offline):
mem = SemanticMemory(embeddings=HashEmbeddings(), recent_tail=6, top_k=4)

# Or with real embeddings:
mem = SemanticMemory(embeddings=OpenAIEmbeddings(), recent_tail=6, top_k=4)

# Convenience factory (defaults to HashEmbeddings if none given):
mem = create_semantic_memory()

# Add messages as usual:
mem.add_message("user", "My dog Rex is a border collie.")
mem.add_message("assistant", "Noted -- Rex the border collie.")
# ...many turns later...

# BEFORE reading get_messages(), tell it what to score older turns against:
mem.set_query("What breed did I say my dog was?")
history = mem.get_messages()   # Rex message will be pulled back
```

**Constructor parameters:**

| Param | Default | What it does |
|---|---|---|
| `embeddings` | required | Any `Embeddings` subclass. Reuse the same instance across sessions. |
| `recent_tail` | 6 | Recent messages kept verbatim regardless of relevance. |
| `top_k` | 4 | How many older messages to pull back by similarity. |
| `min_score` | 0.15 | Cosine floor. Tune down for HashEmbeddings, up for OpenAI. |
| `preserve_system` | True | Always keep the system message at the top. |

## Choosing a memory

```
Is the conversation short (<10 turns)?
  → ConversationMemory

Is only recent context relevant?
  → SlidingWindowMemory

Do you have a hard token budget?
  → TokenLimitedMemory

Do some turns matter much more than others (task framing, key facts)?
  → ImportanceBasedMemory

Is it an open-ended chatbot running for hours?
  → SummaryMemory

Do users refer back to old facts across topics?
  → SemanticMemory  (best for the "remember what I told you" case)
```

## Combining memories

You can chain them — e.g. `SemanticMemory` for retrieval + a
`SummaryMemory` for the recent tail — but the ship-in strategies are
usually enough. Custom chains: subclass `BaseMemory` and delegate to
whichever backend fits.

## Persisting semantic memory across sessions

`SemanticMemory` doesn't have a built-in save/load, but its underlying
`VectorStore` does — persist it directly:

```python
mem.store.save("./data/mem-alice.json")

# On resume:
from agentx_dev import VectorStore, HashEmbeddings
store = VectorStore.load("./data/mem-alice.json", embeddings=HashEmbeddings())
mem = SemanticMemory(embeddings=store.embeddings)
mem._store = store  # rehydrate; keep the reference to the same instance
```

For full user-facing session state (chat history + tokens + tool
calls), use [Session](../guides/sessions.md) alongside memory.
