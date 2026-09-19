# Media with chat models

Call `GPT` or `Claude` directly with images, PDFs or audio — no agent
loop. Every example runs unchanged on either provider.

## One call

```python
from agentx_dev import Claude, Media

llm = Claude()                                   # or GPT(model="gpt-4o")

answer = llm.invoke("What does this chart show?", media=["q3_chart.png"])
```

`media=` takes a list, so you can send several files at once:

```python
llm.invoke(
    "Do these two receipts show the same total?",
    media=["receipt_a.jpg", Media.image("receipt_b.jpg", detail="high")],
)
```

## Typed output from a document

Pair media with `with_structured_output` to pull validated fields out of
a scan, a form, or a PDF:

```python
from pydantic import BaseModel, Field
from agentx_dev import GPT, Media

class DocumentVerificationResult(BaseModel):
    is_valid: bool = Field(description="True when the document passes every check")
    document_type: str = Field(description="passport, driver's licence, national ID, ...")
    issues: list[str] = Field(default_factory=list, description="Problems found")

verify = GPT(model="gpt-4o").with_structured_output(DocumentVerificationResult)

result = verify.invoke(
    "Check this ID: is it legible, unexpired, and are the photo and name present?",
    media=["uploads/id_card.jpg"],
)
print(result.is_valid, result.issues)            # a DocumentVerificationResult
```

This works on models that need OpenAI's Responses API for tool calls
(such as `gpt-6-astra` with a reasoning effort set): `GPT` switches that
model to `/v1/responses` automatically. See
[Tools on models that need the Responses API](../concepts/models.md#tools-on-models-that-need-the-responses-api).

## Media inside messages

Instead of `media=`, you can put `Media` straight into a message's
`content` list, next to text. Use this when the media belongs to a
specific turn, or when you build the messages yourself:

```python
llm.invoke([
    {"role": "system", "content": "You are a careful document reviewer."},
    {"role": "user", "content": [
        "Compare these two pages.",                 # a bare string is a text part
        Media.document("contract_v1.pdf"),
        Media.document("contract_v2.pdf"),
    ]},
])
```

Parts in either provider's own format work too, and are translated when
needed:

```python
llm.invoke([{"role": "user", "content": [
    {"type": "text", "text": "What breed is this?"},
    {"type": "image_url", "image_url": {"url": "https://example.com/dog.jpg"}},   # OpenAI shape
]}])                                                   # works on Claude() as well
```

`media=` and `content` combine: `media=` is appended to the **last**
user message.

## A conversation about an image

Earlier turns keep their images. The model sees the picture again on
every follow-up, not a description of it:

```python
history = [
    {"role": "user", "content": ["Here's the floor plan.", Media.image("plan.png")]},
    {"role": "assistant", "content": "Three bedrooms, one bathroom, open kitchen."},
    {"role": "user", "content": "Where could a second bathroom go?"},
]
llm.invoke(history)
```

## Async

Same arguments:

```python
answer = await llm.ainvoke("What's in this photo?", media=["photo.jpg"])
result = await verify.ainvoke("Check this ID.", media=["uploads/id_card.jpg"])
```

## Things to know

- **Build a `Media` once, reuse it.** Files are read and encoded when the
  `Media` is created, so one object can go to many calls and to both
  providers.
- **URLs aren't downloaded by the framework.** The provider fetches them,
  so they must be publicly reachable.
- **Audio** works on audio-capable GPT models only; Claude raises
  `ValueError`.
- **Streaming** (`llm.stream_text(...)`) takes messages, so put media in
  the `content` list; it has no `media=` argument.
