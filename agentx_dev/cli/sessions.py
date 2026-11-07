"""Where agentx sessions are saved and how they are listed and found."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

TITLE_CHARS = 60
MIN_PREFIX = 4
MAX_SESSION_BYTES = 50 * 1024 * 1024      # a larger file is not listed
MAX_SESSIONS_LISTED = 1000                # only the newest this many files (by name) are looked at
MAX_SLUG_CHARS = 120


def _resolve(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(path))


_TIMESTAMPED = re.compile(r"^\d{8}T\d{6}Z-")


def _printable(text: object) -> str:
    """One line of printable text: every kind of whitespace (including \r, \x85, tabs) becomes a single space and
    other control characters are dropped, so a file cannot rewrite the line it is shown on."""
    return " ".join("".join(c for c in str(text) if c.isprintable() or c.isspace()).split())


def project_slug(cwd: Path) -> str:
    """The working folder as one folder name: separators, colons and other characters a file name cannot hold become
    ``-``, then ``-`` and a short hash of the whole resolved path, so that ``C:\\a-b`` and ``C:\\a\\b`` (the same
    readable part) keep separate sessions. A very long path keeps only the end of its readable part."""
    full = str(_resolve(cwd))
    slug = re.sub(r'[\\/:<>"|?*\x00-\x1f]+', "-", full).strip("-") or "root"
    digest = hashlib.sha1(full.encode("utf-8", errors="replace")).hexdigest()[:8]
    room = MAX_SLUG_CHARS - len(digest) - 1
    if len(slug) > room:
        slug = slug[-room:].lstrip("-")
    return f"{slug}-{digest}"


def sessions_dir(home: Path, cwd: Path) -> Path:
    return Path(home) / ".agentx" / "sessions" / project_slug(cwd)


def new_session_path(home: Path, cwd: Path, session_id: str, now: Optional[datetime] = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    short = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))[:8] or "session"
    return sessions_dir(home, cwd) / f"{stamp}-{short}.json"


@dataclass(frozen=True)
class SessionInfo:
    path: Path
    session_id: str
    title: str
    updated_at: str


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):                               # content split into parts: use the text parts
        return " ".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


_ADDED_TO_MESSAGES = ("\n\n<project-notes>", "\n\n[agentx]", "\n\n[framework]")


def user_messages(history: Any) -> List[str]:
    """What the person typed, in order: not agentx's own notes, a summary that replaced older messages, a tool
    result sent as text, or what agentx added to a message."""
    said: List[str] = []
    if not isinstance(history, list):
        return said
    for message in history:
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        text = _text_of(message.get("content"))
        for marker in _ADDED_TO_MESSAGES:
            text = text.split(marker, 1)[0]
        text = text.removeprefix("[The user wrote this while you were working; take it into account from now on]\n")
        text = " ".join(_printable(text).split())
        if not text or text.startswith(("[", "Observation: ")):
            continue
        said.append(text)
    return said


def _title(history: Any) -> str:
    said = user_messages(history)
    return said[0][:TITLE_CHARS] if said else "(no messages yet)"


TITLES_NAME = "titles.json"                 # short titles the model wrote, by session file name
MAX_TITLES = 2000


def titles_path(home: Path, cwd: Path) -> Path:
    return sessions_dir(home, cwd) / TITLES_NAME


def read_titles(home: Path, cwd: Path) -> dict:
    data = _load(titles_path(home, cwd))
    return {str(k): _printable(v)[:TITLE_CHARS] for k, v in (data or {}).items() if isinstance(v, str) and v.strip()}


def save_title(home: Path, cwd: Path, session_file: str, title: str) -> bool:
    """Keep ``title`` for the session saved as ``session_file`` (its name). Never raises; False when not saved."""
    title = _printable(title)[:TITLE_CHARS].strip()
    if not title:
        return False
    path = titles_path(home, cwd)
    try:
        titles = read_titles(home, cwd)
        titles[str(session_file)] = title
        if len(titles) > MAX_TITLES:                       # the oldest names (by their timestamp) go first
            titles = dict(sorted(titles.items())[-MAX_TITLES:])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".titles-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(titles, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _load(path: Path) -> Optional[dict]:
    """The JSON object in a session file, or None when it is not a readable, reasonably sized regular file."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SESSION_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    return data if isinstance(data, dict) else None


def list_sessions(home: Path, cwd: Path) -> List[SessionInfo]:
    """This folder's saved sessions, newest first. Files that cannot be read are skipped."""
    folder = sessions_dir(home, cwd)
    try:
        # newest first by name; names that are not "<timestamp>-<id>.json" go last, so they never push real sessions out
        paths = sorted(folder.glob("*.json"), key=lambda p: (bool(_TIMESTAMPED.match(p.name)), p.name),
                       reverse=True)[:MAX_SESSIONS_LISTED]
    except (OSError, ValueError):
        return []
    found: List[SessionInfo] = []
    titles = read_titles(home, cwd)
    for path in paths:
        if path.name == TITLES_NAME:
            continue
        data = _load(path)
        if data is None:
            continue
        raw_id = data.get("session_id")
        session_id = (_printable(raw_id) if isinstance(raw_id, (str, int)) else "") or path.stem
        updated = data.get("updated_at")
        found.append(SessionInfo(path=path, session_id=session_id,
                                 title=titles.get(path.name) or _title(data.get("history")),
                                 updated_at=_printable(updated) if isinstance(updated, str) else ""))
    found.sort(key=lambda info: (info.updated_at, info.path.name), reverse=True)
    return found


def latest_session(home: Path, cwd: Path) -> Optional[SessionInfo]:
    infos = list_sessions(home, cwd)
    return infos[0] if infos else None


def find_session(home: Path, cwd: Path, ident: str) -> Optional[SessionInfo]:
    """A session by file name or by a unique id prefix of at least ``MIN_PREFIX`` characters."""
    ident = str(ident).strip()
    infos = list_sessions(home, cwd)
    for info in infos:
        if info.path.name == ident:
            return info
    if len(ident) < MIN_PREFIX:
        return None
    matches = [info for info in infos if info.session_id.startswith(ident)]
    return matches[0] if len(matches) == 1 else None
