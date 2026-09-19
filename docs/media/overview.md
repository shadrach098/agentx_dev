# Media: overview and flow

Send images, PDFs, audio, CSV and other text files, and Excel
spreadsheets to GPT or Claude, directly or through an agent, with one
type: `Media`. You describe *what* the file is once, and
the framework turns it into the exact format each provider expects.

```python
from pydantic import BaseModel
from agentx_dev import GPT, Claude, AgentRunner, AgentType, Media

class Receipt(BaseModel):
    merchant: str
    total: float

llm = Claude()                                   # or GPT(model="gpt-4o")

llm.invoke("What's in this photo?", media=["photo.jpg"])                  # a chat model
llm.with_structured_output(Receipt).invoke("Read it", media=["r.jpg"])    # typed output
AgentRunner(model=llm, agent=AgentType.ReAct, tools=[]).invoke(
    "Check this invoice", media=["invoice.pdf"])                          # an agent
```

The same `media=[...]` argument works on every entry point, and the same
code runs on both providers.

## Where you can pass media

| Entry point | How |
|---|---|
| `llm.invoke(...)` / `await llm.ainvoke(...)` | `media=[...]` *(3.4.2)* or `Media` in a message's `content` list |
| `llm.with_structured_output(S).invoke(...)` / `.ainvoke(...)` | `media=[...]` *(3.4.2)* or in `content` |
| `runner.invoke(...)` / `runner.stream(...)` | `media=[...]` or in the last user message |
| `await runner.ainvoke(...)` / `runner.astream(...)` | `media=[...]` or in the last user message |
| `chat_history=[...]` | `Media` inside earlier turns' `content` lists |

Details for each: [chat models](chat-models.md) · [agents](agents.md).

## What goes in `media=[...]`

| Item | Example | What happens |
|---|---|---|
| a file path | `"scan.png"`, `Path("docs/r.pdf")`, `"sales.csv"`, `"q3.xlsx"` | read; type from the extension |
| a URL | `"https://example.com/cat.jpg"` | passed to the provider, which downloads it |
| a `Media` | `Media.image("scan.png", detail="high")` | used as-is |
| raw bytes | `Media.image(data, media_type="image/png")` | bytes need a type, so wrap them in `Media` |
| a part dict | `{"type": "image_url", ...}` or `{"type": "image", ...}` | either provider's native shape |

Paths are relative to **where your program runs**, not to an agent's
workspace.

## The flow

```text
 what you pass                      stored in history                     sent on the wire
 ────────────────                   ──────────────────                    ────────────────
 "photo.jpg"          ─┐                                          ┌─► GPT  chat completions
 Media.image(...)      │   ┌────────────────────────────────────┐ │     image_url / file / input_audio
 b"..." + media_type   ├──►│ canonical part (plain JSON dict)   ├─┼─► GPT  Responses API
 "https://…/x.jpg"     │   │ {"type": "image", "source": {...}} │ │     input_image / input_file / input_audio
 OpenAI / Anthropic ───┘   └────────────────────────────────────┘ └─► Claude messages
 part dicts                                                             image / document
```

1. **You pass** a path, URL, bytes, `Media`, or a part dict in either
   provider's format.
2. **It's stored** as a canonical part — a plain JSON dict. That's what
   lands in `completion.history`, so `Session.save()` and
   `chat_history=` keep working with media in the conversation.
3. **The model translates** it right before sending, for whichever API
   it's calling. The same history can go to GPT and then to Claude.

The turn's text stays the text. The query string never contains base64,
and a text-only call is still a plain string on the wire.

## What each provider accepts

| Kind | Files | GPT | Claude |
|---|---|---|---|
| image | `.png` `.jpg` `.gif` `.webp` | file or URL | file or URL |
| PDF | `.pdf` | file (URL via the Responses API) | file or URL |
| text | `.csv` `.tsv` `.txt` `.md` `.json` `.xml` `.yaml` `.html` | file, sent as text | file, sent as text |
| spreadsheet | `.xlsx` `.xls` `.ods` | converted to CSV text per sheet | converted to CSV text per sheet |
| audio | `.wav` `.mp3` | file, on audio-capable models | not supported |

Word, PowerPoint and zip files aren't accepted by either provider, so
they raise `ValueError` with the fix (usually: save as PDF).

Two combinations need care:

- **Audio to Claude** raises `ValueError` before any request is sent.
  Claude has no audio input.
- **A PDF URL to GPT.** Chat completions can't fetch documents, so `GPT`
  sends that request through the Responses API, which can.

Whether a particular *model* accepts a kind of media is still up to the
provider. Audio, for example, needs an audio-capable GPT model.

## Data files: CSV and Excel

Text-like files and spreadsheets don't go to the model as attachments
— no provider reads an `.xlsx` directly. The framework reads them and
sends the contents as a labelled text block:

```text
<file name="sales.csv" type="text/csv">
zip,region,total
02134,North,120
</file>
```

- **CSV and other text files are sent exactly as written.** They're not
  parsed, so a ZIP code like `02134` keeps its leading zero.
- **Spreadsheets are converted with pandas**, one CSV block per sheet,
  headed with the sheet name and size. Install the extra first:
  `pip install agentx-dev[excel]`.
- **There's a size limit:** 100,000 characters (about 25k tokens) by
  default. Above that you get a `ValueError`, unless you pass
  `truncate=True` to send the first part, or raise `max_chars=`.

For big data — thousands of rows, many sheets — don't paste it into
the prompt. Let an agent load it with pandas instead. See
[Large data](agents.md#large-data-let-the-agent-use-pandas).

## Limits

- Media attaches to the **user's turn**. A tool's result is text: a tool
  that returns an image path doesn't make the model see the image.
- `Supervisor` and `HandoffCoordinator` don't forward media to
  specialists yet. Pass it to the specialist runner directly.
- Provider size limits apply: roughly 5 MB per image for Claude and 20 MB
  for GPT. The framework doesn't resize.

## Next

- [Media with chat models](chat-models.md): `invoke`, structured output,
  conversations, async
- [Media with agents](agents.md): runners, streaming, workspace files
- [Media reference](reference.md): constructors, wire formats, errors
