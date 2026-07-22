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

## `output_schema=` — agent loop then validate

When you want the model to actually THINK (use tools, reason) before
producing the structured output:

```python
from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, Claude

class WeatherReport(BaseModel):
    city: str
    temperature_c: float
    conditions: str

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[weather_tool])
result = runner.invoke(
    "What's the weather in Paris? Return your final answer as JSON matching "
    'the schema: {"city": str, "temperature_c": float, "conditions": str}.',
    output_schema=WeatherReport,
)

print(result.content)   # str  — the model's JSON text
print(result.output)    # WeatherReport(city='Paris', ...)
```

The runner parses `result.content` as JSON (tolerant of ```json fences),
validates against `WeatherReport`, and puts the parsed instance on
`result.output`. If parsing or validation fails, a `ValueError` is
raised wrapping the underlying error — the framework never silently
returns malformed data.

## Difference between the two

| Feature | `with_structured_output` | `output_schema=` |
|---|---|---|
| Agent loop | No | Yes |
| Tools | No | Yes (any tools) |
| Method | Native function-calling | Prompt + JSON parse |
| Best for | Extraction from raw text | Multi-step reasoning that must return structured data |
| Failure mode | Model didn't call the tool | JSON parse or Pydantic validation error |

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
