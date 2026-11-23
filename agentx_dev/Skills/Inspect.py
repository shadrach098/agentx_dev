"""Looking at a skill before it is trusted: what files it has (with their digests), and what in them looks risky.

A skill is instructions the agent follows, so the danger is less in its scripts (nothing here or in the installer
runs them) than in its text: a line that tells the agent to ignore its rules, to hide what it does from the user,
to change agentx's own settings, to send keys somewhere, or to pipe a download into a shell. ``scan`` looks for
those with patterns. It is static and advisory: it reads the bytes and never runs, imports or opens anything, and
a clean result does not prove a skill is safe (text can be reworded past any pattern). Every finding quotes the
skill as data, cut to one line.

``snapshot`` reads a skill folder once, bounded, without following links, and ``Snapshot.digest`` names exactly
those bytes: the installer checks that what it copies is what was inspected, and the app keeps the digest to see
later whether a skill on disk was changed outside ``/skills``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple

from .Store import SIDECAR, SKILL_FILE, _is_link, one_line

MAX_FILES = 500
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FINDINGS = 60                       # per skill; the rest are counted
TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts",
                 ".rb", ".pl", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html", ".htm", ".xml", ".csv",
                 ".css", ".svg", ".tsx", ".jsx", ".go", ".rs", ".java", ".lua", ".r", ".sql", ""}
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl",
                   ".exe", ".dll", ".so", ".dylib", ".jar", ".msi", ".scr", ".vbs", ".psm1"}
BINARY_PROGRAMS = {".exe", ".dll", ".so", ".dylib", ".jar", ".msi", ".scr"}

DANGER, WARNING, NOTE = "danger", "warning", "note"


class SnapshotError(ValueError):
    """The folder cannot be read as one bounded, link-free skill; one short line says why."""


@dataclass(frozen=True)
class Snapshot:
    """A skill folder as read once: relative POSIX path -> bytes. ``digest`` covers every path and byte."""
    files: Dict[str, bytes]

    @property
    def manifest(self) -> Dict[str, str]:
        return {path: hashlib.sha256(data).hexdigest() for path, data in sorted(self.files.items())}

    @property
    def digest(self) -> str:
        return manifest_digest(self.manifest)


def manifest_digest(manifest: Dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(manifest.items())).encode("utf-8")).hexdigest()


def snapshot(folder: Path, *, include_sidecar: bool = True) -> Snapshot:
    """Every file under ``folder`` (hidden ones too: a hidden file is still on disk next to the skill). A link
    anywhere, a special file, or more than the limits raises ``SnapshotError``; nothing is followed."""
    folder = Path(folder)
    if _is_link(folder) or not folder.is_dir():
        raise SnapshotError("not a plain folder")
    files: Dict[str, bytes] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(dirpath)
        for name in sorted(dirnames):
            if _is_link(here / name):
                raise SnapshotError(f"{one_line((here / name).relative_to(folder).as_posix(), 80)} is a link")
        dirnames.sort()
        for name in sorted(filenames):
            path = here / name
            relative = path.relative_to(folder).as_posix()
            if not include_sidecar and relative == SIDECAR:
                continue
            if _is_link(path):
                raise SnapshotError(f"{one_line(relative, 80)} is a link")
            try:
                with open(path, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise SnapshotError(f"{one_line(relative, 80)} is not a regular file")
                    data = handle.read(MAX_FILE_BYTES + 1)
            except OSError as e:
                raise SnapshotError(f"cannot read {one_line(relative, 80)} ({type(e).__name__})") from None
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                raise SnapshotError("too big (over the file limits)")
            files[relative] = data
    if SKILL_FILE not in files:
        raise SnapshotError("no SKILL.md")
    return Snapshot(files)


# -- the scan ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    level: str                          # DANGER | WARNING | NOTE
    file: str
    line: int                           # 0: the file as a whole
    what: str                           # our words
    quote: str = ""                     # the skill's own text, one line, cut

    def shown(self) -> str:
        where = self.file + (f":{self.line}" if self.line else "")
        return f"{self.level}: {where}: {self.what}" + (f" - \"{self.quote}\"" if self.quote else "")


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    more: int = 0                       # findings over MAX_FINDINGS, counted but not kept

    def add(self, finding: Finding) -> None:
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append(finding)
        else:
            self.more += 1

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)


_I = re.IGNORECASE
# (level, what, pattern): each pattern is matched line by line against the text with zero-width characters removed
# and letters folded (NFKC), so "ign​ore" or full-width letters do not slip past.
TEXT_RULES: Tuple[Tuple[str, str, "re.Pattern[str]"], ...] = (
    (DANGER, "tells the agent to ignore its instructions",
     re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|system|all|any|"
                r"your)\b.{0,30}\b(instructions?|rules|prompts?|guidelines|directives)\b", _I)),
    (DANGER, "tries to take on a new identity or system role",
     re.compile(r"\b(you are now|from now on you are|act as (the )?system|new system prompt|<\s*/?\s*system\s*>)", _I)),
    (DANGER, "asks to hide what is done from the user",
     re.compile(r"\b(do not|don't|never)\b.{0,30}\b(tell|inform|mention|show|reveal)\b.{0,20}\b(the )?user\b|"
                r"\bwithout (asking|telling|informing|notifying) the user\b|\bsilently (run|install|delete|send|"
                r"upload|download|exfiltrate|execute)", _I)),
    (DANGER, "asks to get around the approval prompts or the mode",
     re.compile(r"\b(bypass|skip|avoid|disable|get around|circumvent|suppress)\b (the |any |all |agentx'?s? |your |"
                r"these )?(approval|permission|confirmation) (prompts?|requests?|questions?|dialogs?)\b|"
                r"(/mode|--mode)\s+auto\b", _I)),
    (DANGER, "touches agentx's own settings, skills or permissions",
     re.compile(r"\.agentx[\\/](settings|skills|skills-trust)|\bskills-trust\.json\b|\"(allow|deny)\"\s*:\s*\[", _I)),
    (DANGER, "reads out a key or a key file",
     re.compile(r"\b(echo|print|printf|cat|type|curl|wget|send|post|upload|write-output|write-host)\b[^\n]{0,80}"
                r"\$\{?(env:)?(?-i:[A-Z0-9_]*(API_KEY|SECRET|TOKEN|PASSWORD))\b|\b(cat|type|get-content|copy|cp|"
                r"curl|upload|send)\b[^\n]{0,60}(\.ssh[\\/]|\bid_(rsa|ed25519)\b|\.aws[\\/]credentials)", _I)),
    (WARNING, "mentions keys, secrets or credential files",
     re.compile(r"\b(?-i:[A-Z0-9_]*(API_KEY|SECRET_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD))\b|\.ssh[\\/]|"
                r"\bid_(rsa|ed25519)\b|\bkeychain\b|\bcredential (store|manager)\b|(^|[\s\"'/\\])\.env\b", _I)),
    (DANGER, "pipes a download straight into a shell",
     re.compile(r"\b(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^\n|]{0,200}\|\s*(sudo\s+)?"
                r"(ba|z|)sh\b|\b(iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n]{0,200}\|\s*(iex|invoke-"
                r"expression)\b|\bbase64\s+(-d|--decode)\b[^\n]{0,80}\|\s*(ba|z|)sh\b|\bpowershell\b[^\n]{0,40}"
                r"-enc(odedcommand)?\b", _I)),
    (DANGER, "deletes widely",
     re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\*)(\s|$)|\bremove-item\b[^\n]{0,60}-recurse[^\n]{0,60}"
                r"(\$home|~|[a-z]:\\\s*$)|\bformat\s+[a-z]:", _I)),
    (WARNING, "runs code it downloads or builds at run time",
     re.compile(r"\b(eval|exec)\s*\(|\bsubprocess\.|\bos\.system\s*\(|\binvoke-expression\b|\bchild_process\b", _I)),
    (WARNING, "sends data to an address",
     re.compile(r"\b(curl|wget)\b[^\n]{0,80}\s(-d|--data|-F|--form|-T|--upload-file|-X\s*POST)\b|"
                r"\brequests\.post\s*\(|\bfetch\s*\([^)]{0,80}method\s*:\s*['\"]POST", _I)),
    (WARNING, "installs packages",
     re.compile(r"\b(pip|pip3|npm|pnpm|yarn|gem|cargo|go)\s+(install|add|i)\b|\bnpx\s+\S", _I)),
)
_URL = re.compile(r"https?://([A-Za-z0-9.-]+)", _I)
_ZERO_WIDTH = {"​", "‌", "‍", "⁠", "﻿", "­"}
_BIDI = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩", "‎",
         "‏", "؜"}
_TAG_CHARS = range(0xE0000, 0xE0080)        # invisible "tag" letters that can spell out hidden text
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_BLOB = re.compile(r"[A-Za-z0-9+/=]{300,}")
_TRUSTED_HOSTS = {"github.com", "raw.githubusercontent.com", "docs.python.org", "pypi.org", "www.npmjs.com",
                  "developer.mozilla.org"}


def _folded(line: str) -> str:
    text = "".join(ch for ch in line if ch not in _ZERO_WIDTH and ord(ch) not in _TAG_CHARS)
    return unicodedata.normalize("NFKC", text)


def _hidden_characters(text: str) -> Optional[str]:
    for ch in text:
        if ch in _BIDI:
            return "direction-changing characters (text can read differently from how it runs)"
        if ord(ch) in _TAG_CHARS:
            return "invisible tag characters (they can spell out hidden instructions)"
        if ch in _ZERO_WIDTH and ch != "﻿":
            return "zero-width characters (they can hide words from patterns and from you)"
    return None


def _is_text(path: str, data: bytes) -> bool:
    return b"\x00" not in data[:8192] and PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES


def scan(snap: Snapshot) -> Report:
    """The findings for one skill, most serious first. Never raises, never runs anything."""
    report = Report()
    for path, data in sorted(snap.files.items()):
        if path == SIDECAR:
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in BINARY_PROGRAMS:
            report.add(Finding(DANGER, path, 0, "a compiled program; the agent cannot read what it does"))
            continue
        if not _is_text(path, data):
            report.add(Finding(NOTE, path, 0, "a binary file (not read)"))
            continue
        if suffix in SCRIPT_SUFFIXES:
            report.add(Finding(NOTE, path, 0, "a program file; installing does not run it, and anything the agent "
                                              "runs from it still asks you"))
        text = data.decode("utf-8", errors="replace")
        hidden = _hidden_characters(text)
        if hidden:
            report.add(Finding(DANGER, path, 0, f"has {hidden}"))
        for match in _HTML_COMMENT.finditer(text):
            inner = match.group(1).strip()
            if len(inner) > 20:
                line = text.count("\n", 0, match.start()) + 1
                report.add(Finding(WARNING, path, line, "hidden comment (not shown when the page is rendered)",
                                   one_line(inner, 120)))
        hosts = set()
        for number, line in enumerate(text.splitlines(), 1):
            folded = _folded(line)
            for level, what, pattern in TEXT_RULES:
                if pattern.search(folded):
                    report.add(Finding(level, path, number, what, one_line(line.strip(), 120)))
            if _BLOB.search(folded):
                report.add(Finding(WARNING, path, number, "a long encoded blob (it may hide content)"))
            for host in _URL.findall(folded):
                hosts.add(host.lower())
        others = sorted(h for h in hosts if h not in _TRUSTED_HOSTS)
        if others:
            report.add(Finding(NOTE, path, 0, "links to " + one_line(", ".join(others[:6]), 150)
                               + (f" and {len(others) - 6} more" if len(others) > 6 else "")))
    order = {DANGER: 0, WARNING: 1, NOTE: 2}
    report.findings.sort(key=lambda f: (order.get(f.level, 3), f.file, f.line))
    return report


def readable_text(snap: Snapshot, limit: int) -> Tuple[str, bool]:
    """The skill's text files, each under a ``=== file: <path> ===`` line, SKILL.md first, at most ``limit``
    characters in all. Binary and program files are listed by name only. ``(text, complete)``."""
    parts: List[str] = []
    used, complete = 0, True
    names = sorted(snap.files, key=lambda p: (p != SKILL_FILE, p))
    for path in names:
        if path == SIDECAR:
            continue
        data = snap.files[path]
        if PurePosixPath(path).suffix.lower() in BINARY_PROGRAMS or not _is_text(path, data):
            parts.append(f"=== file: {path} (binary, not shown) ===")
            continue
        body = data.decode("utf-8", errors="replace")
        room = limit - used
        if room <= 200:
            complete = False
            parts.append(f"=== file: {path} (left out: over the review's size limit) ===")
            continue
        if len(body) > room:
            body, complete = body[:room] + "\n[cut: over the review's size limit]", False
        parts.append(f"=== file: {path} ===\n{body}")
        used += len(body)
    return "\n".join(parts), complete


def snapshot_files(folder: Path, files: Tuple[str, ...]) -> Snapshot:
    """Only ``files`` (relative POSIX paths) under ``folder``: what the installer copies from a download. Any path
    with a link in it, a special file, or more than the limits raises ``SnapshotError``."""
    folder = Path(folder)
    found: Dict[str, bytes] = {}
    total = 0
    for relative in files:
        parts = PurePosixPath(relative).parts
        if not parts or any(part in ("", ".", "..") for part in parts) or PurePosixPath(relative).is_absolute():
            raise SnapshotError(f"{one_line(relative, 80)} is not a plain relative path")
        here = folder
        for part in parts:
            here = here / part
            if _is_link(here):
                raise SnapshotError(f"{one_line(relative, 80)} goes through a link")
        try:
            with open(here, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise SnapshotError(f"{one_line(relative, 80)} is not a regular file")
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError as e:
            raise SnapshotError(f"cannot read {one_line(relative, 80)} ({type(e).__name__})") from None
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(found) >= MAX_FILES:
            raise SnapshotError("too big (over the file limits)")
        found[relative] = data
    if SKILL_FILE not in found:
        raise SnapshotError("no SKILL.md")
    return Snapshot(found)
