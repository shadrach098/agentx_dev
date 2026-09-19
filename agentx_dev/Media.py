"""Provider-neutral media input: images, PDFs, audio, text files, spreadsheets.

Build a ``Media`` once and hand it to any model or runner; the framework
translates it to the wire format of whichever provider receives it::

    from agentx_dev import Media

    llm.invoke("What is in this chart?", media=["chart.png"])
    runner.invoke("Which region grew fastest?", media=["sales.xlsx"])

Canonical form
--------------
Inside the framework a media part is a plain, JSON-serialisable dict, so
``completion.history`` and ``Session.save`` keep working::

    {"type": "image",    "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    {"type": "image",    "source": {"type": "url", "url": "https://..."}}
    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "..."},
     "filename": "report.pdf"}
    {"type": "document", "source": {"type": "text", "media_type": "text/csv", "data": "a,b
1,2"},
     "filename": "sales.csv"}
    {"type": "audio",    "source": {"type": "base64", "media_type": "audio/wav", "data": "..."}}

Text-like files (CSV, TSV, TXT, Markdown, JSON, XML, YAML, HTML) are kept
as text, exactly as written. Spreadsheets (.xlsx / .xls / .ods) are
converted with pandas to one CSV block per sheet. Both reach every
provider as a labelled text block (``<file name="sales.csv" ...>``),
which all GPT and Claude models accept.

:func:`content_for_openai`, :func:`content_for_openai_responses` and
:func:`content_for_anthropic` translate to each provider, and all of them
also accept the other provider's native part shapes.

Provider support
----------------
===========  =============================  ==========================
kind         GPT                            Claude
===========  =============================  ==========================
image        base64 + URL                   base64 + URL
PDF          base64 (URL via Responses)     base64 + URL
text files   text                           text
spreadsheet  text (CSV per sheet)           text (CSV per sheet)
audio        base64 wav / mp3               not supported -> ValueError
===========  =============================  ==========================

Unsupported inputs -- Word, PowerPoint, archives, audio to Claude, a text
file by URL -- raise ``ValueError`` naming the fix before any request is
sent. Whether a particular *model* accepts a modality is still the
provider's decision.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

__all__ = [
    "Media",
    "to_media_part",
    "user_content",
    "split_text_and_media",
    "content_for_openai",
    "content_for_openai_responses",
    "content_for_anthropic",
    "is_media_part",
]

_MEDIA_TYPES = ("image", "document", "audio")

# Extensions mimetypes gets wrong or misses on some platforms.
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    # Text-like files: sent to the model as text. Listed explicitly because
    # the OS table is unreliable -- on Windows mimetypes maps .csv to
    # application/vnd.ms-excel.
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".htm": "text/html",
    # Spreadsheets: converted to CSV text per sheet (pandas).
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    # Recognised only so they can be refused with a useful message.
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".zip": "application/zip",
}

_TEXT_TYPES = frozenset({
    "application/json", "application/jsonl", "application/xml",
    "application/yaml", "application/x-yaml",
})
_SPREADSHEET_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.ms-excel",
    "application/vnd.oasis.opendocument.spreadsheet",
})
_UNSUPPORTED_FIX = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        "Word files aren't accepted by GPT or Claude. Save it as PDF and use "
        "Media.document('file.pdf'), or paste its text into the prompt.",
    "application/msword":
        "Word files aren't accepted by GPT or Claude. Save it as PDF first.",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        "PowerPoint files aren't accepted by GPT or Claude. Export it as PDF "
        "and use Media.document('slides.pdf').",
    "application/vnd.ms-powerpoint":
        "PowerPoint files aren't accepted by GPT or Claude. Export it as PDF first.",
    "application/zip":
        "Archives can't be sent to a model. Extract the files and attach them "
        "individually.",
}

# Default cap on a text or spreadsheet attachment, in characters (roughly
# 25k tokens). Big enough for a real document, small enough that a stray
# 500k-row export fails loudly instead of overflowing the context window.
DEFAULT_MAX_CHARS = 100_000


def _is_text_type(media_type: Optional[str]) -> bool:
    mt = (media_type or "").lower()
    return mt.startswith("text/") or mt in _TEXT_TYPES


def _is_spreadsheet_type(media_type: Optional[str]) -> bool:
    return (media_type or "").lower() in _SPREADSHEET_TYPES

# OpenAI's input_audio only takes these two format names.
_AUDIO_FORMAT = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
}


def _guess_mime(name: str) -> Optional[str]:
    ext = os.path.splitext(name.split("?", 1)[0])[1].lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed, _ = mimetypes.guess_type(name)
    return guessed


def _kind_for_mime(media_type: Optional[str]) -> Optional[str]:
    if not media_type:
        return None
    if media_type.startswith("image/"):
        return "image"
    if media_type.startswith("audio/"):
        return "audio"
    if (media_type == "application/pdf" or _is_text_type(media_type)
            or _is_spreadsheet_type(media_type) or media_type.lower() in _UNSUPPORTED_FIX):
        return "document"
    return None


def _decode_text(raw: bytes, name: str, encoding: Optional[str]) -> str:
    """Bytes -> str. UTF-8 (BOM stripped) first, then cp1252, which is what
    Excel writes when it saves CSV on Windows. An explicit ``encoding``
    wins."""
    # Windows line endings are normalised: same content, fewer tokens,
    # and no stray carriage return at the end of every row.
    if encoding:
        return raw.decode(encoding).replace("\r\n", "\n")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc).replace("\r\n", "\n")
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{name} isn't UTF-8 or Windows-1252 text; pass encoding=")


def _limit_text(text: str, name: str, max_chars: Optional[int], truncate: bool) -> str:
    """Enforce the size cap on a text attachment."""
    if max_chars is None or len(text) <= max_chars:
        return text
    if not truncate:
        raise ValueError(
            f"{name} is {len(text):,} characters as text, over max_chars={max_chars:,} "
            f"(about {len(text) // 4:,} tokens). Pass truncate=True to send the first "
            f"{max_chars:,} characters, raise max_chars=, or for large data let an "
            "agent read the file with pandas via run_python instead."
        )
    cut = text.rfind("\n", 0, max_chars)
    if cut <= 0:
        cut = max_chars
    kept = text[:cut]
    total_lines = text.count("\n") + 1
    kept_lines = kept.count("\n") + 1
    return kept + (f"\n[... truncated: showing the first {kept_lines:,} of "
                   f"{total_lines:,} lines of {name}]")


def _spreadsheet_to_text(src: Any, name: str, sheets: Any) -> str:
    """Every requested sheet -> CSV text via pandas, one block per sheet.

    ``dtype=object`` keeps cell values as stored (a text-formatted "02134"
    stays "02134") instead of letting pandas re-infer column types."""
    try:
        import pandas as pd
    except ImportError as e:
        raise ImportError(
            "Reading spreadsheets needs pandas and openpyxl, which install "
            "with agentx-dev: pip install -U agentx-dev pandas openpyxl"
        ) from e
    try:
        frames = pd.read_excel(src, sheet_name=sheets, dtype=object)
    except ImportError as e:
        raise ImportError(
            f"Reading {name} needs another package ({e}). "
            "pip install -U pandas openpyxl "
            "(.xls also needs xlrd; .ods needs odfpy)"
        ) from e
    if not isinstance(frames, dict):
        frames = {sheets: frames}
    import datetime as _dt

    def _cell(v):
        # Excel stores a plain date as a midnight datetime; print it as a
        # date so every cell isn't padded with ' 00:00:00'. Real
        # timestamps keep their time.
        if isinstance(v, _dt.datetime) and v.time() == _dt.time(0):
            return v.date()
        return v

    blocks = []
    for sheet, df in frames.items():
        df = df.map(_cell) if hasattr(df, "map") else df.applymap(_cell)
        header = f"## Sheet: {sheet} ({len(df)} rows x {len(df.columns)} columns)"
        # lineterminator: pandas defaults to os.linesep, i.e. \r\n on Windows.
        csv_text = df.to_csv(index=False, lineterminator="\n")
        blocks.append(header + "\n" + csv_text.rstrip("\n"))
    return "\n\n".join(blocks)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _parse_data_uri(uri: str) -> Tuple[str, str]:
    """``data:image/png;base64,AAAA`` -> ("image/png", "AAAA")."""
    header, _, data = uri.partition(",")
    media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    return media_type, data


class Media:
    """One piece of non-text input: an image, a document, or audio.

    Construct with a classmethod rather than ``__init__``:

    - ``Media.image(src)``        -- path, URL, or bytes
    - ``Media.document(src)``     -- a PDF, a text-like file, or a spreadsheet
    - ``Media.text(src)``         -- CSV, TSV, TXT, Markdown, JSON, XML, YAML ...
    - ``Media.spreadsheet(src)``  -- .xlsx / .xls / .ods, one CSV block per sheet
    - ``Media.audio(src)``        -- a wav/mp3 path or bytes (GPT audio models)
    - ``Media.from_path(p)``      -- kind inferred from the file extension
    - ``Media.from_url(u)``       -- kind inferred from the URL, or pass ``kind=``
    - ``Media.from_bytes(b, media_type)``

    Files are read when the ``Media`` is created, so it is self-contained
    and safe to reuse across calls and providers. Images, PDFs and audio are
    kept as base64; text-like files and spreadsheets are kept as TEXT and
    reach the model as a labelled text block, which every model on both
    providers accepts. URLs are NOT fetched by the framework; the URL goes
    to the provider, which fetches it (images and PDFs only).
    """

    __slots__ = ("kind", "media_type", "data", "url", "body", "filename", "detail")

    def __init__(
        self,
        kind: str,
        *,
        media_type: Optional[str] = None,
        data: Optional[str] = None,
        url: Optional[str] = None,
        body: Optional[str] = None,
        filename: Optional[str] = None,
        detail: Optional[str] = None,
    ):
        if kind not in _MEDIA_TYPES:
            raise ValueError(f"Media kind must be one of {_MEDIA_TYPES}, got {kind!r}")
        if sum(v is not None for v in (data, url, body)) != 1:
            raise ValueError("Media needs exactly one of data= (base64), url=, or body= (text)")
        if data is not None and not media_type:
            raise ValueError("Media with inline data needs a media_type (e.g. 'image/png')")
        mt = (media_type or "").lower()
        if body is not None and kind != "document":
            raise ValueError("body= (text) is only valid for documents")
        if mt and kind == "image" and not mt.startswith("image/"):
            raise ValueError(f"{media_type!r} isn't an image type")
        if mt and kind == "audio" and not mt.startswith("audio/"):
            raise ValueError(f"{media_type!r} isn't an audio type")
        if kind == "document" and data is not None and mt != "application/pdf":
            # Neither provider accepts arbitrary binary documents inline.
            fix = _UNSUPPORTED_FIX.get(mt)
            raise ValueError(fix or (
                f"Inline documents must be PDF; got {media_type!r}. Text-like files "
                "use Media.text(), spreadsheets Media.spreadsheet()."
            ))
        self.kind = kind
        self.media_type = media_type
        self.data = data
        self.url = url
        self.body = body
        self.filename = filename
        self.detail = detail

    # -- constructors ---------------------------------------------------

    @classmethod
    def _build(cls, kind: Optional[str], src: Any, media_type: Optional[str],
               filename: Optional[str], detail: Optional[str], **opts: Any) -> "Media":
        """Shared loader. ``opts`` carries text/spreadsheet options:
        ``max_chars``, ``truncate``, ``sheets``, ``encoding``."""
        if isinstance(src, Media):
            return src
        if isinstance(src, os.PathLike):
            src = os.fspath(src)

        # ---- where the bytes come from
        raw: Optional[bytes] = None
        if isinstance(src, (bytes, bytearray)):
            if not media_type:
                raise ValueError(
                    "Media from raw bytes needs media_type=, e.g. "
                    "Media.image(data, media_type='image/png')"
                )
            raw, mt = bytes(src), media_type
        elif not isinstance(src, str):
            raise TypeError(f"Media source must be a path, URL, bytes, or Media; got {type(src).__name__}")
        elif src.startswith("data:"):
            mt, b64 = _parse_data_uri(src)
            mt = media_type or mt
            raw = base64.b64decode(b64) if (_is_text_type(mt) or _is_spreadsheet_type(mt)) else None
            if raw is None:
                kind = kind or _kind_for_mime(mt)
                if kind is None:
                    raise ValueError(f"Can't infer a media kind from data URI type {mt!r}")
                return cls(kind, media_type=mt, data=b64, filename=filename, detail=detail)
        elif _is_url(src):
            mt = media_type or _guess_mime(src)
            kind = kind or _kind_for_mime(mt)
            if kind is None:
                raise ValueError(
                    f"Can't infer whether {src!r} is an image, document, or audio; "
                    "use Media.image(url) / Media.document(url) / Media.audio(url)"
                )
            if kind == "document" and mt and mt.lower() != "application/pdf":
                raise ValueError(
                    f"Only PDFs can be passed by URL ({src!r} is {mt}). Download the "
                    "file and pass the local path -- text and spreadsheets are sent as text."
                )
            return cls(kind, media_type=mt, url=src, filename=filename, detail=detail)
        else:
            if not os.path.isfile(src):
                raise FileNotFoundError(f"Media file not found: {src}")
            mt = media_type or _guess_mime(src)
            filename = filename or os.path.basename(src)
            with open(src, "rb") as fh:
                raw = fh.read()

        name = filename or "attachment"
        kind = kind or _kind_for_mime(mt)
        if kind is None or not mt:
            raise ValueError(
                f"Can't infer a media type for {name!r}; pass media_type= "
                "(e.g. 'image/png', 'application/pdf', 'text/csv', 'audio/wav')"
            )

        # ---- text and spreadsheets become text documents
        if kind == "document" and _is_spreadsheet_type(mt):
            import io
            text = _spreadsheet_to_text(io.BytesIO(raw), name, opts.get("sheets"))
            text = _limit_text(text, name, opts.get("max_chars", DEFAULT_MAX_CHARS),
                               opts.get("truncate", False))
            return cls("document", media_type="text/csv", body=text, filename=name)
        if kind == "document" and _is_text_type(mt):
            text = _decode_text(raw, name, opts.get("encoding"))
            text = _limit_text(text, name, opts.get("max_chars", DEFAULT_MAX_CHARS),
                               opts.get("truncate", False))
            return cls("document", media_type=mt, body=text, filename=name)
        if kind == "document" and mt.lower() in _UNSUPPORTED_FIX:
            raise ValueError(_UNSUPPORTED_FIX[mt.lower()])

        return cls(kind, media_type=mt, data=base64.b64encode(raw).decode("ascii"),
                   filename=filename, detail=detail)

    @classmethod
    def image(cls, src: Any, *, media_type: Optional[str] = None,
              detail: Optional[str] = None) -> "Media":
        """An image. ``detail`` ('low' | 'high' | 'auto') is honoured by GPT
        and ignored by Claude."""
        return cls._build("image", src, media_type, None, detail)

    @classmethod
    def document(cls, src: Any, *, media_type: Optional[str] = None,
                 filename: Optional[str] = None, **opts: Any) -> "Media":
        """A document: a PDF, a text-like file (CSV, TXT, JSON ...), or a
        spreadsheet -- routed by type. Raw bytes default to PDF. ``opts``
        are the :meth:`text` / :meth:`spreadsheet` options."""
        if media_type is None and isinstance(src, (bytes, bytearray)):
            media_type = "application/pdf"
        return cls._build("document", src, media_type, filename, None, **opts)

    pdf = document

    @classmethod
    def text(cls, src: Any, *, media_type: Optional[str] = None,
             filename: Optional[str] = None, encoding: Optional[str] = None,
             max_chars: Optional[int] = DEFAULT_MAX_CHARS,
             truncate: bool = False) -> "Media":
        """A text-like file -- CSV, TSV, TXT, Markdown, JSON, XML, YAML, HTML --
        sent to the model exactly as written, labelled with its file name.

        ``max_chars`` caps the size (default 100,000 characters, about 25k
        tokens; ``None`` for no cap). Over the cap it raises, unless
        ``truncate=True``, which keeps the first whole lines up to the cap
        and notes how many were dropped. Raw bytes default to text/plain."""
        if media_type is None and isinstance(src, (bytes, bytearray)):
            media_type = "text/plain"
        if media_type is None and isinstance(src, (str, os.PathLike)):
            guessed = _guess_mime(os.fspath(src))
            media_type = guessed if _is_text_type(guessed) else "text/plain"
        return cls._build("document", src, media_type, filename, None,
                          encoding=encoding, max_chars=max_chars, truncate=truncate)

    @classmethod
    def spreadsheet(cls, src: Any, *, sheets: Any = None, filename: Optional[str] = None,
                    max_chars: Optional[int] = DEFAULT_MAX_CHARS,
                    truncate: bool = False) -> "Media":
        """An Excel or OpenDocument spreadsheet, converted with pandas to one
        CSV block per sheet (with its name and size). ``sheets`` picks sheets
        by name or index -- one, or a list; default all. pandas and
        openpyxl install with agentx-dev. Size options as :meth:`text`."""
        media_type = None
        if isinstance(src, (bytes, bytearray)):
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif isinstance(src, (str, os.PathLike)) and not _is_spreadsheet_type(_guess_mime(os.fspath(src))):
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return cls._build("document", src, media_type, filename, None,
                          sheets=sheets, max_chars=max_chars, truncate=truncate)

    @classmethod
    def audio(cls, src: Any, *, media_type: Optional[str] = None) -> "Media":
        """Audio (wav or mp3). Only audio-capable GPT models accept this;
        Claude has no audio input and raises ``ValueError``."""
        return cls._build("audio", src, media_type, None, None)

    @classmethod
    def from_path(cls, path: Any, *, media_type: Optional[str] = None, **opts: Any) -> "Media":
        return cls._build(None, path, media_type, None, None, **opts)

    @classmethod
    def from_url(cls, url: str, *, kind: Optional[str] = None,
                 media_type: Optional[str] = None) -> "Media":
        if not _is_url(url):
            raise ValueError(f"Not an http(s) URL: {url!r}")
        return cls._build(kind, url, media_type, None, None)

    @classmethod
    def from_bytes(cls, data: bytes, media_type: str, *,
                   filename: Optional[str] = None, **opts: Any) -> "Media":
        return cls._build(None, data, media_type, filename, None, **opts)

    # -- conversion -----------------------------------------------------

    def to_part(self) -> Dict[str, Any]:
        """The canonical JSON-serialisable content part."""
        if self.body is not None:
            source: Dict[str, Any] = {"type": "text", "media_type": self.media_type or "text/plain",
                                      "data": self.body}
        elif self.url is not None:
            source = {"type": "url", "url": self.url}
            if self.media_type:
                source["media_type"] = self.media_type
        else:
            source = {"type": "base64", "media_type": self.media_type, "data": self.data}
        part: Dict[str, Any] = {"type": self.kind, "source": source}
        if self.filename:
            part["filename"] = self.filename
        if self.detail:
            part["detail"] = self.detail
        return part

    def __repr__(self) -> str:
        if self.body is not None:
            where = f"<{len(self.body):,} chars of text>"
        else:
            where = self.url or f"<{len(self.data or ''):,} b64 chars>"
        name = f" {self.filename}" if self.filename else ""
        return f"Media({self.kind}, {self.media_type or '?'}{name}, {where})"


# ----------------------------------------------------------------------
# Normalisation helpers
# ----------------------------------------------------------------------

def to_media_part(item: Any) -> Dict[str, Any]:
    """Coerce anything a caller may put in ``media=[...]`` into a canonical
    part: a ``Media``, a path / URL / data-URI string, ``PathLike``, or an
    already-built part dict (canonical, OpenAI-native, or Anthropic-native)."""
    if isinstance(item, Media):
        return item.to_part()
    if isinstance(item, dict):
        return _canonicalise_part(item)
    return Media._build(None, item, None, None, None).to_part()


def is_media_part(part: Any) -> bool:
    if isinstance(part, Media):
        return True
    if not isinstance(part, dict):
        return False
    return part.get("type") in (*_MEDIA_TYPES, "image_url", "input_audio", "file")


def user_content(text: str, media: Optional[Iterable[Any]]) -> Union[str, List[Dict[str, Any]]]:
    """Build a user message ``content`` from text plus optional media.

    With no media it returns the plain string, so the message shape (and
    every provider's handling of it) is unchanged for text-only calls."""
    parts = [to_media_part(m) for m in (media or [])]
    if not parts:
        return text
    blocks: List[Dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(parts)
    return blocks


def split_text_and_media(content: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Split a message ``content`` into (joined text, media parts).

    Used where the framework needs the text of a turn -- e.g. the runner's
    query string -- without stringifying base64 payloads into it."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if isinstance(content, Media):
        return "", [content.to_part()]
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return str(content), []
    texts: List[str] = []
    media: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
            texts.append(str(part.get("text", "")))
        elif is_media_part(part):
            media.append(to_media_part(part))
    return "\n".join(t for t in texts if t), media


def _canonicalise_part(part: Dict[str, Any]) -> Dict[str, Any]:
    """Map a provider-native media part onto the canonical shape.

    Text parts and unknown parts pass through untouched."""
    ptype = part.get("type")
    if ptype in _MEDIA_TYPES:
        return part
    if ptype == "image_url":                               # OpenAI image
        spec = part.get("image_url")
        url = spec.get("url") if isinstance(spec, dict) else spec
        detail = spec.get("detail") if isinstance(spec, dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            mt, data = _parse_data_uri(url)
            out = {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
        else:
            out = {"type": "image", "source": {"type": "url", "url": url}}
        if detail:
            out["detail"] = detail
        return out
    if ptype == "file":                                    # OpenAI file
        spec = part.get("file") or {}
        file_data = spec.get("file_data")
        if isinstance(file_data, str) and file_data.startswith("data:"):
            mt, data = _parse_data_uri(file_data)
            out = {"type": "document", "source": {"type": "base64", "media_type": mt, "data": data}}
            if spec.get("filename"):
                out["filename"] = spec["filename"]
            return out
        return part            # file_id references can't be translated; leave as-is
    if ptype == "input_audio":                             # OpenAI audio
        spec = part.get("input_audio") or {}
        fmt = (spec.get("format") or "wav").lower()
        mt = "audio/mpeg" if fmt == "mp3" else f"audio/{fmt}"
        return {"type": "audio", "source": {"type": "base64", "media_type": mt,
                                            "data": spec.get("data", "")}}
    return part


# ----------------------------------------------------------------------
# Provider translators
# ----------------------------------------------------------------------

def _text_document(part: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """``(filename, media_type, text)`` for a document that should reach
    the model as text, else None.

    Covers text-sourced parts (CSV, spreadsheets, JSON ...) and older
    base64 parts with a text media type -- 3.4.0/3.4.1 stored ``.txt`` that
    way, which Claude rejects as a document."""
    if part.get("type") != "document":
        return None
    source = part.get("source") or {}
    mt = source.get("media_type") or "text/plain"
    name = part.get("filename") or part.get("title") or "attachment"
    if source.get("type") == "text":
        return name, mt, str(source.get("data", ""))
    if source.get("type") == "base64" and _is_text_type(mt):
        raw = base64.b64decode(source.get("data", "") or "")
        return name, mt, _decode_text(raw, name, None)
    return None


def _file_block(name: str, media_type: str, text: str) -> str:
    """A text attachment as the model sees it: delimited, with its name,
    so it's distinguishable from the user's own words."""
    safe = name.replace('"', "'")
    return f'<file name="{safe}" type="{media_type}">\n{text}\n</file>'


def _refuse_binary_document(part: Dict[str, Any]) -> None:
    """Raise for an inline non-PDF document neither provider accepts."""
    source = part.get("source") or {}
    mt = (source.get("media_type") or "").lower()
    if part.get("type") == "document" and source.get("type") == "base64" and mt != "application/pdf":
        if _is_spreadsheet_type(mt):
            raise ValueError("Spreadsheets must be converted before sending; use "
                             "Media.spreadsheet(path) (or pass the path in media=).")
        raise ValueError(_UNSUPPORTED_FIX.get(mt) or
                         f"Inline documents must be PDF; got {mt or 'an unknown type'!r}.")


def _data_uri(source: Dict[str, Any]) -> str:
    return f"data:{source.get('media_type') or 'application/octet-stream'};base64,{source.get('data', '')}"


def content_for_openai(content: Any) -> Any:
    """Translate message content to OpenAI chat-completions parts.

    Strings pass through. Lists have every media part (canonical,
    Anthropic-native, OpenAI-native, or ``Media``) rendered as OpenAI's
    ``image_url`` / ``file`` / ``input_audio`` parts."""
    if isinstance(content, Media):
        content = [content]
    if not isinstance(content, list):
        return content
    out: List[Any] = []
    for part in content:
        if isinstance(part, str):
            out.append({"type": "text", "text": part})
            continue
        if isinstance(part, Media):
            part = part.to_part()
        if not isinstance(part, dict):
            out.append(part)
            continue
        ptype = part.get("type")
        if ptype in ("image_url", "input_audio") or (ptype == "file" and "file" in part):
            out.append(part)                       # already OpenAI-native
            continue
        if ptype not in _MEDIA_TYPES:
            out.append(part)                       # text and anything else
            continue
        text_doc = _text_document(part)
        if text_doc is not None:
            out.append({"type": "text", "text": _file_block(*text_doc)})
            continue
        _refuse_binary_document(part)
        source = part.get("source") or {}
        if ptype == "image":
            url = source.get("url") if source.get("type") == "url" else _data_uri(source)
            spec: Dict[str, Any] = {"url": url}
            if part.get("detail"):
                spec["detail"] = part["detail"]
            out.append({"type": "image_url", "image_url": spec})
        elif ptype == "document":
            if source.get("type") == "url":
                raise ValueError(
                    "GPT (chat completions) cannot fetch a document from a URL. "
                    "Use Media.document(path_or_bytes) so it is sent inline."
                )
            out.append({"type": "file", "file": {
                "filename": part.get("filename") or "document.pdf",
                "file_data": _data_uri(source),
            }})
        elif ptype == "audio":
            if source.get("type") == "url":
                raise ValueError("GPT audio input must be inline; use Media.audio(path_or_bytes).")
            fmt = _AUDIO_FORMAT.get((source.get("media_type") or "").lower())
            if fmt is None:
                raise ValueError(
                    f"GPT audio input supports wav and mp3 only; got {source.get('media_type')!r}"
                )
            out.append({"type": "input_audio",
                        "input_audio": {"data": source.get("data", ""), "format": fmt}})
    return out


def content_for_openai_responses(content: Any) -> Any:
    """Translate message content to OpenAI **Responses API** input parts.

    Same inputs as :func:`content_for_openai`, different wire shapes:
    ``input_text`` / ``input_image`` / ``input_file`` / ``input_audio``.
    Unlike chat completions, the Responses API can fetch a document from
    a URL (``file_url``), so that combination is allowed here."""
    if isinstance(content, Media):
        content = [content]
    if not isinstance(content, list):
        return content
    out: List[Any] = []
    for part in content:
        if isinstance(part, str):
            out.append({"type": "input_text", "text": part})
            continue
        if isinstance(part, Media):
            part = part.to_part()
        if not isinstance(part, dict):
            out.append(part)
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text"):
            out.append({"type": "input_text", "text": str(part.get("text", ""))})
            continue
        if ptype in ("input_image", "input_file", "input_audio"):
            out.append(part)                       # already Responses-native
            continue
        part = _canonicalise_part(part)
        ptype = part.get("type")
        if ptype == "file":
            spec = part.get("file") or {}
            if spec.get("file_id"):
                out.append({"type": "input_file", "file_id": spec["file_id"]})
                continue
        if ptype not in _MEDIA_TYPES:
            out.append(part)
            continue
        text_doc = _text_document(part)
        if text_doc is not None:
            out.append({"type": "input_text", "text": _file_block(*text_doc)})
            continue
        _refuse_binary_document(part)
        source = part.get("source") or {}
        is_url = source.get("type") == "url"
        if ptype == "image":
            out.append({"type": "input_image",
                        "image_url": source.get("url") if is_url else _data_uri(source),
                        "detail": part.get("detail") or "auto"})
        elif ptype == "document":
            if is_url:
                out.append({"type": "input_file", "file_url": source.get("url")})
            else:
                out.append({"type": "input_file",
                            "filename": part.get("filename") or "document.pdf",
                            "file_data": _data_uri(source)})
        elif ptype == "audio":
            if is_url:
                raise ValueError("OpenAI audio input must be inline; use Media.audio(path_or_bytes).")
            fmt = _AUDIO_FORMAT.get((source.get("media_type") or "").lower())
            if fmt is None:
                raise ValueError(
                    f"OpenAI audio input supports wav and mp3 only; got {source.get('media_type')!r}"
                )
            out.append({"type": "input_audio",
                        "input_audio": {"data": source.get("data", ""), "format": fmt}})
    return out


def content_for_anthropic(content: Any) -> Any:
    """Translate message content to Anthropic content blocks.

    Strings pass through. Lists have every media part rendered as
    Anthropic ``image`` / ``document`` blocks, with framework-only keys
    (``filename``, ``detail``) mapped or dropped -- Anthropic rejects
    unknown block fields."""
    if isinstance(content, Media):
        content = [content]
    if not isinstance(content, list):
        return content
    out: List[Any] = []
    for part in content:
        if isinstance(part, str):
            out.append({"type": "text", "text": part})
            continue
        if isinstance(part, Media):
            part = part.to_part()
        if not isinstance(part, dict):
            out.append(part)
            continue
        part = _canonicalise_part(part)
        ptype = part.get("type")
        if ptype == "input_text":
            out.append({"type": "text", "text": part.get("text", "")})
            continue
        if ptype == "file":
            raise ValueError(
                "Claude can't use an OpenAI file_id reference; pass the file "
                "inline with Media.document(path_or_bytes)."
            )
        if ptype not in _MEDIA_TYPES:
            out.append(part)
            continue
        if ptype == "audio":
            raise ValueError(
                "Claude does not accept audio input. Transcribe it first, or "
                "send it to an audio-capable GPT model."
            )
        text_doc = _text_document(part)
        if text_doc is not None:
            # A plain text block: every Claude model accepts it, and a
            # base64 document block only takes PDFs.
            block_t: Dict[str, Any] = {"type": "text", "text": _file_block(*text_doc)}
            if part.get("cache_control"):
                block_t["cache_control"] = part["cache_control"]
            out.append(block_t)
            continue
        _refuse_binary_document(part)
        source = dict(part.get("source") or {})
        if source.get("type") == "url":
            source = {"type": "url", "url": source.get("url")}
        else:
            source = {"type": "base64", "media_type": source.get("media_type"),
                      "data": source.get("data", "")}
        block: Dict[str, Any] = {"type": ptype, "source": source}
        if ptype == "document" and part.get("filename"):
            block["title"] = part["filename"]
        if part.get("cache_control"):
            block["cache_control"] = part["cache_control"]
        out.append(block)
    return out
