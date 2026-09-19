# Media reference

## `Media`

```python
from agentx_dev import Media
```

| Constructor | Arguments | Notes |
|---|---|---|
| `Media.image(src)` | `media_type=`, `detail=` | `detail` is `"low"`, `"high"` or `"auto"`; GPT uses it, Claude ignores it |
| `Media.document(src)` | `media_type=`, `filename=` | a PDF; also routes text files and spreadsheets to the constructors below. Raw bytes default to `application/pdf`. Alias: `Media.pdf` |
| `Media.text(src)` | `media_type=`, `filename=`, `encoding=`, `max_chars=`, `truncate=` | CSV, TSV, TXT, Markdown, JSON, XML, YAML, HTML, sent as written. Raw bytes default to `text/plain` |
| `Media.spreadsheet(src)` | `sheets=`, `filename=`, `max_chars=`, `truncate=` | `.xlsx` / `.xls` / `.ods` → one CSV block per sheet. Needs `pip install agentx-dev[excel]` |
| `Media.audio(src)` | `media_type=` | wav or mp3; GPT audio models only |
| `Media.from_path(path)` | `media_type=` | kind taken from the file extension |
| `Media.from_url(url)` | `kind=`, `media_type=` | kind from the URL, or pass `kind="image"` etc. |
| `Media.from_bytes(data, media_type)` | `filename=` | kind from `media_type` |

`src` can be a file path (`str` or `Path`), an `http(s)` URL, a
`data:` URI, raw `bytes` (with `media_type=`), or another `Media`.
Only images and PDFs can be URLs.

Files are read when the `Media` is created. Images, PDFs and audio are
kept as base64; text files and spreadsheets are kept as text. URLs are
stored as-is and fetched by the provider.

### Text and spreadsheet options

| Option | Default | Meaning |
|---|---|---|
| `max_chars` | `100_000` | size cap in characters (about 25k tokens); `None` for no cap |
| `truncate` | `False` | over the cap: `False` raises; `True` keeps whole lines up to the cap and adds a `[... truncated ...]` note |
| `encoding` | UTF-8, then Windows-1252 | text encoding; Windows `\r\n` line endings become `\n` |
| `sheets` | all sheets | a sheet name or index, or a list of them |

The same options work on `Media.document()`, `Media.from_path()` and
`Media.from_bytes()`. Paths in `media=[...]` use the defaults.

Spreadsheets are read with `pandas.read_excel(..., dtype=object)`, so
cells keep the values they hold — a text-formatted `02134` stays
`02134` — and each sheet is written as CSV under a header like
`## Sheet: Sales (120 rows x 5 columns)`.

| Attribute | Meaning |
|---|---|
| `kind` | `"image"`, `"document"` or `"audio"` |
| `media_type` | e.g. `"image/png"`, `"application/pdf"`, `"audio/wav"` |
| `data` | base64 string, or `None` for a URL or text |
| `body` | the text of a text file or converted spreadsheet, else `None` |
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
{"type": "document", "source": {"type": "text", "media_type": "text/csv", "data": "zip,total\n02134,120"},
 "filename": "sales.csv"}
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
| text file / spreadsheet | `text` part: `<file name="..." type="...">…</file>` | `input_text`, same wrapper | `text` block, same wrapper |
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
| `ValueError: ... over max_chars=...` | a text file or spreadsheet over the size cap | `truncate=True`, a bigger `max_chars=`, or let an agent read it with pandas |
| `ImportError: ... pip install agentx-dev[excel]` | a spreadsheet without pandas / openpyxl | `pip install agentx-dev[excel]` (`.xls` also needs `xlrd`, `.ods` needs `odfpy`) |
| `ValueError: Word files aren't accepted by GPT or Claude` | `.docx` / `.doc` | save as PDF, or paste the text |
| `ValueError: PowerPoint files aren't accepted by GPT or Claude` | `.pptx` / `.ppt` | export as PDF |
| `ValueError: Archives can't be sent to a model` | `.zip` | extract and attach the files |
| `ValueError: Only PDFs can be passed by URL` | a CSV, text file or spreadsheet URL | download it and pass the local path |
| `ValueError: ... isn't UTF-8 or Windows-1252 text` | another text encoding | pass `encoding=` |
| `ValueError: Spreadsheets must be converted before sending` | a raw spreadsheet part in a message | use `Media.spreadsheet(path)` or pass the path |

Provider-side limits (image size, supported image formats, page counts)
come back as the provider's own error.
