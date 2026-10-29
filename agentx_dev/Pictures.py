"""Pictures in tool results: a tool (``read_path``) can hand the model an image, not just text about it.

A picture result is a ``ToolContent``: a list of content parts (a text line saying what the picture is, then the
image itself) that prints as its text. Logs, the screen, team summaries and anything else that calls ``str()`` on a
tool result see one short line; the runner keeps the parts, and each provider's message converter sends the image
the way that provider accepts it (inside Claude's ``tool_result``; as a follow-up user message for GPT, whose tool
messages take text only).

A picture costs about 1,500 to 4,800 tokens every time the model is sent it, so ``stub_old_pictures`` leaves all but
the newest few out of each request (the history keeps them); the stub names the file so the model can open it again."""

from __future__ import annotations

import base64
import io
import os
import struct
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "PICTURE_EXTENSIONS", "MAX_PICTURE_BYTES", "MAX_PICTURE_SIDE", "KEEP_PICTURES", "ToolContent",
    "is_picture_path", "picture_result", "has_pictures", "content_text", "picture_parts", "stub_old_pictures",
]

PICTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# A picture file up to this size is sent as it is; a bigger one is made smaller first (with Pillow, when it is
# installed) or refused. 5 MB is what every Claude setup accepts.
MAX_PICTURE_BYTES = 5 * 1024 * 1024

# The longest side a provider accepts (Claude refuses more than 8000 pixels).
MAX_PICTURE_SIDE = 8000

# What a picture that is too big is made smaller to: the longest side Claude looks at in full detail.
SHRINK_TO_SIDE = 2576

# How many of the newest picture results each request carries whole.
KEEP_PICTURES = 3

_KINDS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WebP"}


class ToolContent(list):
    """A tool result made of content parts (``{"type": "text", ...}`` and ``{"type": "image", ...}``) that prints
    as its text, so a picture's data never ends up in a log line or on the screen."""

    def __str__(self) -> str:
        return content_text(self)

    __repr__ = __str__


def is_picture_path(path: Any) -> bool:
    return os.path.splitext(str(path))[1].lower() in PICTURE_EXTENSIONS


def _media_type(raw: bytes) -> Optional[str]:
    """The picture type from the file's first bytes (not its name: a .png that is really a JPEG is sent as one)."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _jpeg_size(raw: bytes) -> Optional[Tuple[int, int]]:
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            index += 1 if marker == 0xFF else 2
            continue
        length = struct.unpack(">H", raw[index + 2:index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", raw[index + 5:index + 9])
            return width, height
        index += 2 + length
    return None


def _size(raw: bytes, media_type: str) -> Optional[Tuple[int, int]]:
    """``(width, height)`` read from the picture's header, or None when it cannot be read."""
    try:
        if media_type == "image/png" and len(raw) >= 24:
            return struct.unpack(">II", raw[16:24])
        if media_type == "image/gif" and len(raw) >= 10:
            return struct.unpack("<HH", raw[6:10])
        if media_type == "image/jpeg":
            return _jpeg_size(raw)
        if media_type == "image/webp" and len(raw) >= 30:
            chunk = raw[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", raw[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(raw[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return int.from_bytes(raw[24:27], "little") + 1, int.from_bytes(raw[27:30], "little") + 1
    except (struct.error, IndexError):
        return None
    return None


def _shrink(raw: bytes, media_type: str) -> Optional[Tuple[bytes, str]]:
    """``raw`` made smaller (longest side ``SHRINK_TO_SIDE``, then JPEG if it is still too big), or None when
    Pillow is not installed or cannot open it."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            picture.load()
            picture = picture.copy()
        picture.thumbnail((SHRINK_TO_SIDE, SHRINK_TO_SIDE))
        if media_type == "image/png" or picture.mode in ("RGBA", "LA", "P"):
            out = io.BytesIO()
            picture.save(out, format="PNG", optimize=True)
            if out.tell() <= MAX_PICTURE_BYTES:
                return out.getvalue(), "image/png"
        out = io.BytesIO()
        picture.convert("RGB").save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return None


def _amount(count: int) -> str:
    if count < 1024 * 1024:
        return f"{max(1, round(count / 1024)):,} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def picture_result(path: Any, shown_as: Optional[str] = None) -> Any:
    """What ``read_path`` returns for a picture file: a ``ToolContent`` with a line saying what it is and the
    picture, or an ``ERROR: ...`` string when it is not a picture a model can take (unknown type, too big)."""
    shown = str(shown_as if shown_as is not None else path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_PICTURE_BYTES * 8 + 1)          # a far bigger file is refused without reading it all
    except OSError as error:
        return f"ERROR: cannot open {shown}: {error.strerror or error}"
    media_type = _media_type(raw)
    if media_type is None:
        return f"ERROR: {shown} is not a PNG, JPEG, GIF or WebP picture (its contents are some other kind of file)"
    size = _size(raw, media_type)
    too_big = len(raw) > MAX_PICTURE_BYTES or (size is not None and max(size) > MAX_PICTURE_SIDE)
    note = ""
    if too_big:
        smaller = _shrink(raw, media_type) if len(raw) <= MAX_PICTURE_BYTES * 8 else None
        if smaller is None or len(smaller[0]) > MAX_PICTURE_BYTES:
            dims = f", {size[0]}x{size[1]}" if size else ""
            return (f"ERROR: {shown} is too big to show ({_amount(len(raw))}{dims}); a picture must be at most "
                    f"{MAX_PICTURE_BYTES // (1024 * 1024)} MB and {MAX_PICTURE_SIDE:,} pixels on its longest side. "
                    "Take a smaller screenshot (a part of the page, or a smaller window) and open that.")
        before = f"{size[0]}x{size[1]}, " if size else ""
        note = f" Made smaller to send (it was {before}{_amount(len(raw))})."
        raw, media_type = smaller
        size = _size(raw, media_type)
    dims = f", {size[0]}x{size[1]}" if size else ""
    text = f"Picture {shown}: {_KINDS[media_type]}{dims}, {_amount(len(raw))}.{note}"
    image = {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(raw).decode("ascii")},
             "filename": os.path.basename(str(path))}
    return ToolContent([{"type": "text", "text": text}, image])


def _is_image_part(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in ("image", "image_url", "input_image")


def has_pictures(content: Any) -> bool:
    return isinstance(content, list) and any(_is_image_part(part) for part in content)


def picture_parts(content: Any) -> List[Dict[str, Any]]:
    return [part for part in content if _is_image_part(part)] if isinstance(content, list) else []


def content_text(content: Any) -> str:
    """The text of a message's content: a string as it is; for a list of parts, its text parts (a picture with
    no text about it reads as ``[picture]``)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts = [str(part.get("text", "")) for part in content
             if isinstance(part, dict) and part.get("type") in ("text", "input_text")]
    texts += [part for part in content if isinstance(part, str)]
    if not texts and has_pictures(content):
        return "[picture]"
    return "\n".join(text for text in texts if text)


def _stub(content: Sequence[Any]) -> str:
    text = content_text([part for part in content if not _is_image_part(part)]) or "A picture"
    return text + (" [The picture was shown earlier and is left out of this request to save tokens. Open it again "
                   "with read_path if you need to look at it.]")


def _is_tool_result(message: Dict[str, Any]) -> bool:
    if message.get("role") == "tool":
        return True
    content = message.get("content")
    return (message.get("role") == "user" and isinstance(content, list) and bool(content)
            and isinstance(content[0], dict) and str(content[0].get("text", "")).startswith("Observation: "))


def stub_old_pictures(messages: Iterable[Dict[str, Any]], keep: int = KEEP_PICTURES) -> List[Dict[str, Any]]:
    """A copy of ``messages`` in which every tool result with a picture except the newest ``keep`` has its
    picture replaced by a note. Pictures the user attached to their own message are left alone."""
    out = list(messages)
    found = [index for index, message in enumerate(out)
             if isinstance(message, dict) and _is_tool_result(message) and has_pictures(message.get("content"))]
    for index in found[:max(0, len(found) - max(0, keep))]:
        out[index] = {**out[index], "content": _stub(out[index]["content"])}    # a text-mode one keeps "Observation: "
    return out
