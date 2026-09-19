"""Provider-neutral media input (images, PDFs, audio).

Build a ``Media`` once and hand it to any model or runner; the framework
translates it to the wire format of whichever provider receives it::

    from agentx_dev import Media

    llm.invoke([{"role": "user", "content": [
        {"type": "text", "text": "What is in this chart?"},
        Media.image("chart.png"),
    ]}])

    runner.invoke("Summarise the attached report", media=["report.pdf"])

Canonical form
--------------
Inside the framework a media part is a plain, JSON-serialisable dict, so
``completion.history`` and ``Session.save`` keep working::

    {"type": "image",    "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    {"type": "image",    "source": {"type": "url", "url": "https://..."}}
    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "..."},
     "filename": "report.pdf"}
    {"type": "audio",    "source": {"type": "base64", "media_type": "audio/wav", "data": "..."}}

That is Anthropic's content-block shape (plus an ``audio`` type and an
optional ``filename`` / ``detail``), chosen because it carries the most
information. :func:`content_for_openai` and :func:`content_for_anthropic`
translate to each provider, and both ALSO accept the other provider's
native part shapes -- an OpenAI-style ``image_url`` part sent to
``Claude()`` works, and an Anthropic ``image`` block sent to ``GPT()``
works. Callers keep writing whichever shape they already know.

Provider support
----------------
===========  ======================  ==========================
kind         GPT (chat completions)  Claude (messages)
===========  ======================  ==========================
image        base64 + URL            base64 + URL
document     base64 PDF              base64 + URL PDF
audio        base64 wav / mp3        not supported -> ValueError
===========  ======================  ==========================

Unsupported combinations raise ``ValueError`` naming the fix, rather than
sending a request the provider rejects with a less helpful message.
Whether a particular *model* accepts a modality (e.g. audio needs an
audio-capable GPT model) is still the provider's decision.
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
    ".txt": "text/plain",
}

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
    if media_type in ("application/pdf", "text/plain"):
        return "document"
    return None


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

    - ``Media.image(src)``    -- path, URL, or bytes
    - ``Media.document(src)`` -- a PDF (or plain text) path, URL, or bytes
    - ``Media.audio(src)``    -- a wav/mp3 path or bytes (GPT audio models)
    - ``Media.from_path(p)``  -- kind inferred from the file extension
    - ``Media.from_url(u)``   -- kind inferred from the URL, or pass ``kind=``
    - ``Media.from_bytes(b, media_type)``

    Files are read and base64-encoded at construction, so a ``Media`` is
    self-contained and safe to reuse across calls and providers. URLs are
    NOT fetched by the framework; the URL goes to the provider, which
    fetches it (where the provider supports that for the kind).
    """

    __slots__ = ("kind", "media_type", "data", "url", "filename", "detail")

    def __init__(
        self,
        kind: str,
        *,
        media_type: Optional[str] = None,
        data: Optional[str] = None,
        url: Optional[str] = None,
        filename: Optional[str] = None,
        detail: Optional[str] = None,
    ):
        if kind not in _MEDIA_TYPES:
            raise ValueError(f"Media kind must be one of {_MEDIA_TYPES}, got {kind!r}")
        if (data is None) == (url is None):
            raise ValueError("Media needs exactly one of data= (base64) or url=")
        if data is not None and not media_type:
            raise ValueError("Media with inline data needs a media_type (e.g. 'image/png')")
        self.kind = kind
        self.media_type = media_type
        self.data = data
        self.url = url
        self.filename = filename
        self.detail = detail

    # -- constructors ---------------------------------------------------

    @classmethod
    def _build(cls, kind: Optional[str], src: Any, media_type: Optional[str],
               filename: Optional[str], detail: Optional[str]) -> "Media":
        if isinstance(src, Media):
            return src
        if isinstance(src, (bytes, bytearray)):
            if not media_type:
                raise ValueError(
                    "Media from raw bytes needs media_type=, e.g. "
                    "Media.image(data, media_type='image/png')"
                )
            kind = kind or _kind_for_mime(media_type)
            if kind is None:
                raise ValueError(f"Can't infer a media kind from {media_type!r}; pass kind explicitly")
            return cls(kind, media_type=media_type,
                       data=base64.b64encode(bytes(src)).decode("ascii"),
                       filename=filename, detail=detail)
        if isinstance(src, os.PathLike):
            src = os.fspath(src)
        if not isinstance(src, str):
            raise TypeError(f"Media source must be a path, URL, bytes, or Media; got {type(src).__name__}")
        if src.startswith("data:"):
            mt, data = _parse_data_uri(src)
            mt = media_type or mt
            kind = kind or _kind_for_mime(mt)
            if kind is None:
                raise ValueError(f"Can't infer a media kind from data URI type {mt!r}")
            return cls(kind, media_type=mt, data=data, filename=filename, detail=detail)
        if _is_url(src):
            mt = media_type or _guess_mime(src)
            kind = kind or _kind_for_mime(mt)
            if kind is None:
                raise ValueError(
                    f"Can't infer whether {src!r} is an image, document, or audio; "
                    "use Media.image(url) / Media.document(url) / Media.audio(url)"
                )
            return cls(kind, media_type=mt, url=src, filename=filename, detail=detail)
        # A filesystem path.
        if not os.path.isfile(src):
            raise FileNotFoundError(f"Media file not found: {src}")
        mt = media_type or _guess_mime(src)
        kind = kind or _kind_for_mime(mt)
        if kind is None or not mt:
            raise ValueError(
                f"Can't infer a media type for {src!r}; pass media_type= "
                "(e.g. 'image/png', 'application/pdf', 'audio/wav')"
            )
        with open(src, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        return cls(kind, media_type=mt, data=data,
                   filename=filename or os.path.basename(src), detail=detail)

    @classmethod
    def image(cls, src: Any, *, media_type: Optional[str] = None,
              detail: Optional[str] = None) -> "Media":
        """An image. ``detail`` ('low' | 'high' | 'auto') is honoured by GPT
        and ignored by Claude."""
        return cls._build("image", src, media_type, None, detail)

    @classmethod
    def document(cls, src: Any, *, media_type: Optional[str] = None,
                 filename: Optional[str] = None) -> "Media":
        """A document -- normally a PDF. Raw bytes default to PDF."""
        if media_type is None and isinstance(src, (bytes, bytearray)):
            media_type = "application/pdf"
        return cls._build("document", src, media_type, filename, None)

    pdf = document

    @classmethod
    def audio(cls, src: Any, *, media_type: Optional[str] = None) -> "Media":
        """Audio (wav or mp3). Only audio-capable GPT models accept this;
        Claude has no audio input and raises ``ValueError``."""
        return cls._build("audio", src, media_type, None, None)

    @classmethod
    def from_path(cls, path: Any, *, media_type: Optional[str] = None) -> "Media":
        return cls._build(None, path, media_type, None, None)

    @classmethod
    def from_url(cls, url: str, *, kind: Optional[str] = None,
                 media_type: Optional[str] = None) -> "Media":
        if not _is_url(url):
            raise ValueError(f"Not an http(s) URL: {url!r}")
        return cls._build(kind, url, media_type, None, None)

    @classmethod
    def from_bytes(cls, data: bytes, media_type: str, *,
                   filename: Optional[str] = None) -> "Media":
        return cls._build(None, data, media_type, filename, None)

    # -- conversion -----------------------------------------------------

    def to_part(self) -> Dict[str, Any]:
        """The canonical JSON-serialisable content part."""
        if self.url is not None:
            source: Dict[str, Any] = {"type": "url", "url": self.url}
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
        where = self.url or f"<{len(self.data or '')} b64 chars>"
        return f"Media({self.kind}, {self.media_type or '?'}, {where})"


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
