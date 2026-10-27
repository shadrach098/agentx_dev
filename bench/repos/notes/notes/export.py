"""Notes as a Markdown index."""

from notes.text import shorten, slugify


def index(store):
    lines = ["# Notes", ""]
    for note_id, title in sorted(store.titles().items()):
        lines.append(f"- [{shorten(title)}]({slugify(note_id)}.md)")
    return "\n".join(lines) + "\n"
