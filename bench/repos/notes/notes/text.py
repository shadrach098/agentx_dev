"""Text helpers for notes."""

import re


def slugify(title):
    """A note title as a file-friendly id: lower case, words joined by dashes."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return "-".join(words) or "untitled"


def first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def shorten(text, limit=40):
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
