# Guide: images, PDFs, and audio

Pass media to a model or an agent with one type, `Media`, and the
framework renders it in whichever provider's wire format the model
needs. The same code works on `GPT()` and `Claude()`.

> **Both providers work.** Every example below runs unchanged with
> `GPT()` or `Claude()`. Where a provider can't accept a kind of media,
> you get a `ValueError` naming the fix before any request is sent.

## Attach media to an agent run

```python
from agentx_dev import AgentRunner, AgentType, Claude, Media

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

result = runner.invoke(
    "What does this chart show, and does the report agree?",
    media=["q3_chart.png", "q3_report.pdf"],       # paths, URLs, or Media
)
print(result.content)
```

`media=` is accepted by `AgentRunner.invoke`, `AgentRunner.stream`,
`AsyncAgentRunner.ainvoke`, and `AsyncAgentRunner.astream`. Each item can
be:

| Item | Example |
|---|---|
| a file path | `"scan.png"`, `Path("docs/r.pdf")` |
| a URL | `"https://example.com/cat.jpg"` |
| a `Media` | `Media.image("scan.png", detail="high")` |
| a content-part dict | an OpenAI `image_url` part or an Anthropic `image` block |

The turn's text stays the query; the media rides alongside it as real
image / document parts. Text-only calls are unchanged — the user turn is
still a plain string.

## Build `Media` explicitly

```python
Media.image("photo.jpg")                         # kind + type from the extension
Media.image("https://example.com/cat.jpg")       # URL, fetched by the provider
Media.image(png_bytes, media_type="image/png")   # raw bytes need a media_type
Media.image("photo.jpg", detail="low")           # GPT's detail hint (Claude ignores it)
Media.document("report.pdf")                     # PDF
Media.document(pdf_bytes, filename="r.pdf")      # bytes default to application/pdf
Media.audio("clip.wav")                          # wav / mp3, GPT audio models only
Media.from_path("anything.png")                  # infer the kind
```

Files are read and base64-encoded when the `Media` is created, so one
object can be reused across calls and across providers. URLs are **not**
fetched by the framework — the URL is handed to the provider.

## Straight to a chat model

Put media in a message's `content` list, in any of these shapes — mix
them freely:

```python
from agentx_dev import GPT, Media

llm = GPT(model="gpt-4o")
llm.invoke([{"role": "user", "content": [
    {"type": "text", "text": "Compare these two receipts."},
    Media.image("receipt_a.jpg"),
    {"type": "image_url", "image_url": {"url": "https://example.com/b.jpg"}},  # OpenAI-native
]}])
```

An OpenAI `image_url` part sent to `Claude()` is translated to an
Anthropic `image` block, and an Anthropic block sent to `GPT()` becomes an
`image_url` part. Keep writing whichever shape you already know.

## Media in a conversation

Images in `chat_history` are replayed as images, not stringified:

```python
history = [
    {"role": "user", "content": [{"type": "text", "text": "Here's the floor plan."},
                                 Media.image("plan.png")]},
    {"role": "assistant", "content": "Got it — three bedrooms, one bath."},
]
runner.invoke("Where would a second bathroom fit?", chat_history=history)
```

`completion.history` stores media as plain JSON dicts, so
`Session.save()` keeps working with media in the conversation.

## What each provider accepts

| Kind | GPT | Claude |
|---|---|---|
| image | inline + URL | inline + URL |
| PDF | inline (URL via the Responses API) | inline + URL |
| audio | inline wav / mp3 | not supported |

Two combinations need care:

- **Audio to Claude** fails fast with a `ValueError`: Claude has no audio input. Transcribe first, or
  use an audio-capable GPT model.
- **A document URL to GPT** — chat completions can't fetch documents,
  so since 3.4.1 `GPT` sends that request through the Responses API,
  which can. With `use_responses_api=False` it raises instead; use
  `Media.document(path_or_bytes)` to send the file inline.

Whether a specific *model* accepts a modality is still up to the
provider — audio, for example, needs an audio-capable GPT model.

## Current limits

- Media attaches to the **user's turn**. Tool results are still text:
  a tool that returns an image path doesn't make the model see the
  image. Attach it with `media=` instead.
- `Supervisor` and `HandoffCoordinator` don't forward media to
  specialists yet — pass it to the specialist runner directly.
- Provider size limits apply (roughly 5 MB per image for Claude, 20 MB
  for GPT). The framework doesn't resize.

## Files in a sandboxed workspace

If a file lives in the agent's workspace, point `media=` at it directly —
the model can't "see" an image by reading it with `read_path`, which is a
text reader:

```python
from agentx_dev import AgentRunner, AgentType, Claude, Permissions

runner = AgentRunner(model=Claude(), agent=AgentType.ReAct,
                     permissions=Permissions.full_access(["./my_workspace"]))
runner.invoke("Describe the photo", media=["./my_workspace/bruce.jpeg"])
```

Inside the workspace, the file tools treat a leading slash as the
workspace root — `read_path("/notes.txt")` reads
`./my_workspace/notes.txt` — and `run_python` / `run_shell` start in the
workspace. See [Permissions](../concepts/permissions.md#workspace-paths).
