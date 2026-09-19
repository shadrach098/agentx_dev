# Media reference

## `Media`

```python
from agentx_dev import Media
```

| Constructor | Arguments | Notes |
|---|---|---|
| `Media.image(src)` | `media_type=`, `detail=` | `detail` is `"low"`, `"high"` or `"auto"`; GPT uses it, Claude ignores it |
| `Media.document(src)` | `media_type=`, `filename=` | normally a PDF; raw bytes default to `application/pdf`. Alias: `Media.pdf` |
| `Media.audio(src)` | `media_type=` | wav or mp3; GPT audio models only |
| `Media.from_path(path)` | `media_type=` | kind taken from the file extension |
| `Media.from_url(url)` | `kind=`, `media_type=` | kind from the URL, or pass `kind="image"` etc. |
| `Media.from_bytes(data, media_type)` | `filename=` | kind from `media_type` |

`src` can be a file path (`str` or `Path`), an `http(s)` URL, a
`data:` URI, raw `bytes` (with `media_type=`), or another `Media`.

Files are read and base64-encoded when the `Media` is created. URLs are
stored as-is and fetched by the provider.

| Attribute | Meaning |
|---|---|
| `kind` | `"image"`, `"document"` or `"audio"` |
| `media_type` | e.g. `"image/png"`, `"application/pdf"`, `"audio/wav"` |
| `data` | base64 string, or `None` for a URL |
| `url` | the URL, or `None` for inline data |
| `filename` | original file name, if known |
| `detail` | image detail hint, if set |
| `to_part()` | the canonical part dict |

## The canonical part

What the framework stores in history — plain JSON:

```python
{"type": "image",    "source": {"type": "base64", "media_type": "image/png", "data": "iVBOR..."}}
{"type": "image",    "source": {"type": "url", "url": "https://example.com/cat.jpg"}}
{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBE..."},
 "filename": "report.pdf"}
{"type": "audio",    "source": {"type": "base64", "media_type": "audio/wav", "data": "UklG..."}}
```

`detail` and `filename` are optional keys.

## Wire formats

What each part becomes when a model sends it:

| Part | GPT chat completions | GPT Responses API | Claude messages |
|---|---|---|---|
| image, inline | `image_url` with a `data:` URI, plus `detail` | `input_image` with a `data:` URI, `detail` (default `auto`) | `image`, `base64` source |
| image, URL | `image_url` with the URL | `input_image` with the URL | `image`, `url` source |
| PDF, inline | `file` with `filename` + `file_data` | `input_file` with `filename` + `file_data` | `document`, `base64` source; `filename` becomes `title` |
| PDF, URL | not possible → sent via the Responses API | `input_file` with `file_url` | `document`, `url` source |
| audio | `input_audio` (`wav` / `mp3`) | `input_audio` | not possible → `ValueError` |

Parts written in either provider's own format are accepted everywhere.
An OpenAI `image_url` part sent to Claude becomes an `image` block, and
an Anthropic `image` block sent to GPT becomes `image_url`.

## Helper functions

In `agentx_dev.Media`, for building or inspecting messages yourself:

| Function | Does |
|---|---|
| `to_media_part(item)` | any accepted item → canonical part |
| `user_content(text, media)` | text + media → a message `content` value (a plain string when there's no media) |
| `split_text_and_media(content)` | a `content` value → `(text, [media parts])` |
| `content_for_openai(content)` | → OpenAI chat-completions parts |
| `content_for_openai_responses(content)` | → OpenAI Responses API parts |
| `content_for_anthropic(content)` | → Anthropic content blocks |

## Errors

All raised before any request is sent:

| Error | Cause | Fix |
|---|---|---|
| `ValueError: Claude does not accept audio input` | audio sent to `Claude` | transcribe first, or use an audio-capable GPT model |
| `ValueError: ... audio input supports wav and mp3 only` | another audio format sent to GPT | convert to wav or mp3 |
| `ValueError: GPT (chat completions) cannot fetch a document from a URL` | a PDF URL with `use_responses_api=False`, or from `content_for_openai` directly | allow the Responses API, or send the file inline |
| `ValueError: Claude can't use an OpenAI file_id reference` | an OpenAI `file_id` part sent to Claude | send the file inline with `Media.document(...)` |
| `ValueError: Media from raw bytes needs media_type=` | bytes without a type | `Media.image(data, media_type="image/png")` |
| `ValueError: Can't infer whether ... is an image, document, or audio` | a URL with no telling extension | `Media.image(url)` / `Media.document(url)` |
| `ValueError: Can't infer a media type for ...` | a file with an unknown extension | pass `media_type=` |
| `FileNotFoundError: Media file not found` | wrong path — paths are relative to your program, not the workspace | fix the path |

Provider-side limits (image size, supported image formats, page counts)
come back as the provider's own error.
