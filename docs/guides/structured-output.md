# Guide: get typed output back

Two patterns:


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

1. **`with_structured_output`** — one-shot extraction from a model,
   no agent loop.
2. **`output_schema=`** — run the full agent loop, but validate the
   final answer against a Pydantic schema.

## `with_structured_output` — one-shot

Force the model to fill a Pydantic schema via tool-calling:

```python
from pydantic import BaseModel
from agentx_dev import Claude

class Receipt(BaseModel):
    merchant: str
    total: float
    currency: str = "USD"

extractor = Claude().with_structured_output(Receipt)
receipt = extractor.invoke("Joe's Diner, $12.50")
print(receipt)
# Receipt(merchant="Joe's Diner", total=12.5, currency='USD')

print(receipt.merchant)   # "Joe's Diner"
print(receipt.total)      # 12.5
```

Input accepts:
- a plain string
- a message list (`[{"role": "user", "content": "..."}]`)
- a dict with a `messages` key (for piped prompt templates)

Async sibling:

```python
receipt = await extractor.ainvoke("Joe's Diner, $12.50")
```

## Pipe composition

The runnable supports `|` so it can sit at the end of a pipeline:

```python
from langchain.prompts import ChatPromptTemplate

prompt = ChatPromptTemplate.from_messages([
    ("system", "Extract structured receipt data from the OCR text."),
    ("user", "{ocr_text}"),
])

pipeline = prompt | Claude().with_structured_output(Receipt)
receipt = pipeline.invoke({"ocr_text": "Joe's Diner, $12.50"})
```

## `output_schema=` — agent loop then coerce

When you want the model to actually THINK (use tools, reason) before
producing the structured output. Since 3.2 the schema can be declared
ONCE on the constructor, and the coercion uses native function calling
instead of parsing JSON out of the answer text:

```python
from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, Claude

class WeatherReport(BaseModel):
    city: str
    temperature_c: float
    conditions: str

runner = AgentRunner(
    model=Claude(), agent=AgentType.ReAct, tools=[weather_tool],
    output_schema=WeatherReport,           # declared once (3.2)
)
result = runner.invoke("What's the weather in Paris?")

print(result.content)   # str            — the human-readable answer
print(result.output)    # WeatherReport  — validated instance
```

No JSON instructions in the prompt, no schema on every call. Every
invoke on this runner produces both a readable `content` and a typed
`output`. A per-call `output_schema=` still works and wins over the
constructor value when both are set; passing neither keeps the old
behaviour exactly (`output` stays `None`).

### How the coercion works (3.2)

After the ReAct loop finishes, the runner makes ONE extra model call
that forces a native tool call against your schema — the provider's own
constrained decoding fills the fields. Nothing is regexed out of prose,
which removes the "model wrote a nice paragraph instead of JSON" and
"the JSON broke on an unescaped quote" failure classes.

Two properties worth knowing:

- **The agent loop is untouched.** Tool selection and intermediate
  reasoning run exactly as without a schema; the coercion is a
  post-processing step. Forcing a schema onto the loop itself degrades
  both the reasoning and the shape.
- **Fallback for custom models.** If your `BaseChatModel` subclass has
  no `call_with_tools`, the runner falls back to the pre-3.2 text-JSON
  parsing, so schema-less providers keep working.

If parsing or validation fails on the fallback path, a `ValueError` is
raised wrapping the underlying error — the framework never silently
returns malformed data.

## Difference between the two

| Feature | `with_structured_output` | `output_schema=` |
|---|---|---|
| Agent loop | No | Yes |
| Tools | No | Yes (any tools) |
| Method | Native function-calling | Native function-calling after the loop (3.2); JSON-parse fallback for non-FC models |
| Declared | Per extractor | Once on the constructor, or per call |
| Best for | Extraction from raw text | Multi-step reasoning that must return structured data |
| Failure mode | Model didn't call the tool | Coercion call failed AND JSON fallback failed |

## Typed specialists in a Supervisor (3.2)

The payoff of constructor-level schemas is multi-agent pipelines. When
a specialist declares an `output_schema`, the Supervisor preserves the
validated instance on `SubtaskResult.output` and serializes it into the
NEXT specialist's context as labelled JSON:

```
STRUCTURED OUTPUT (QueryIntent):
{
  "intent": "product_info",
  "search_query": "VelteHub features overview",
  "needs_rag": true
}
```

Downstream specialists parse fields, not sentences. See the
[Supervisor guide](../advanced/supervisor.md#structured-findings-threading-32)
and cookbook pattern 24 for a full intent → retrieve → rerank pipeline.

## Include raw response

`with_structured_output(..., include_raw=True)` returns both the raw
tool-use dict and the parsed instance:

```python
extractor = Claude().with_structured_output(Receipt, include_raw=True)
out = extractor.invoke("...")
print(out["parsed"])   # Receipt(...)
print(out["raw"])      # {"type": "tool_use", "name": "Receipt", "input": {...}, ...}
```

## Nested schemas

Pydantic v2 nesting works out of the box:

```python
class LineItem(BaseModel):
    name: str
    quantity: int
    price: float

class Receipt(BaseModel):
    merchant: str
    items: list[LineItem]
    total: float

extractor = Claude().with_structured_output(Receipt)
receipt = extractor.invoke(
    "Joe's Diner. 2x Burger $10 each, 1x Fries $4. Total $24."
)
for item in receipt.items:
    print(f"  {item.quantity}x {item.name} @ ${item.price}")
```

## Enums / Literals

```python
from typing import Literal

class Ticket(BaseModel):
    title: str
    priority: Literal["low", "med", "high", "critical"]
    category: Literal["bug", "feature", "chore"]

extractor = Claude().with_structured_output(Ticket)
```

Pydantic converts these to JSON Schema enums; the model reliably picks
one of the allowed values.

## Failure modes

- **`ValueError: model returned text instead of a Receipt tool call`** —
  the model refused to call the forced tool (usually because the schema
  is malformed or the input is empty). Print the raw string and adjust.
- **`ValidationError`** — the model called the tool but with bad data.
  Loosen the schema (add defaults, `Optional` fields) or improve field
  `description=` values so the model knows what to fill in.

## Runnable demo

See `examples/function_calling_demo.py` for both patterns end-to-end.
