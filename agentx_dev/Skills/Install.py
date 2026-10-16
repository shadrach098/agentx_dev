"""Installing skills from a link (or a folder) the USER gives: download, find the skills, show what was found,
and copy them into the skills folder once the user has said yes.

Nothing here is reachable from a tool: only the user's own command calls it. Everything downloaded is treated as
hostile: the archive is unpacked with size and path limits, links are never followed or copied, the sidecar file
that says where a skill came from is always written by us (never taken from the download), and the exact commit
is recorded so a later change in the repository changes nothing."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

from .Defaults import bundled_skills
from .Store import (
    NAME_RE, SIDECAR, SKILL_FILE, SkillFormatError, _is_link, load_skill, one_line,
)

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_FILES = 3000
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_SKILLS = 50
MAX_DEPTH = 5
PRUNE = {".git", "node_modules", "__pycache__", ".venv", "venv"}
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".js", ".mjs", ".ts", ".rb", ".pl",
                   ".exe", ".dll", ".so"}
ALLOWED_HOSTS = {"api.github.com", "codeload.github.com", "github.com"}
_GITHUB = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.+?))?)?/?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

HttpGet = Callable[[str, int, Optional[str]], bytes]


class SkillInstallError(Exception):
    """Something the user should be told, in one or two sentences. Never carries text from the download."""


@dataclass(frozen=True)
class Source:
    kind: str                              # "github" | "local"
    owner: str = ""
    repo: str = ""
    ref: str = ""                          # a branch, tag or commit; "" is the default branch
    subpath: str = ""                      # a folder inside the repository
    path: Optional[Path] = None            # for "local"


@dataclass(frozen=True)
class Fetched:
    root: Path                             # the unpacked repository (or the local folder)
    label: str                             # shown to the user and written to the sidecar
    commit: str = ""
    subpath: str = ""


@dataclass(frozen=True)
class Candidate:
    name: str
    description: str
    folder: Path
    files: Tuple[str, ...]                 # relative POSIX paths that will be copied
    scripts: Tuple[str, ...]               # the ones that look like programs


def parse_source(text: str) -> Source:
    """``https://github.com/<owner>/<repo>`` (optionally ``/tree/<branch>/<folder>``) or a folder on this computer.
    Branch names that contain ``/`` cannot be written in the link form: use a folder link on the default branch."""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        raise SkillInstallError("Give a github.com link or the path of a folder.")
    if text.lower().startswith(("http://", "https://", "git@", "ssh://")):
        match = _GITHUB.match(text)
        if match is None:
            raise SkillInstallError("Only https://github.com/<owner>/<repo> links (optionally followed by "
                                    "/tree/<branch>/<folder>) can be installed.")
        owner, repo, ref, subpath = match.groups()
        subpath = (subpath or "").strip("/")
        if subpath and any(part in ("", ".", "..") for part in subpath.split("/")):
            raise SkillInstallError("That folder path is not valid.")
        return Source("github", owner, repo, ref or "", subpath)
    path = Path(text).expanduser()
    if path.is_dir():
        return Source("local", path=path.resolve())
    raise SkillInstallError(f"{one_line(text, 80)!r} is neither a github.com link nor a folder.")


# -- downloading ----------------------------------------------------------------

class _SameHosts(urllib.request.HTTPRedirectHandler):
    """A redirect may only go to https on GitHub's own hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise SkillInstallError("The download was redirected somewhere other than GitHub, so it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, limit: int, accept: Optional[str] = None) -> bytes:
    """The body at ``url`` (a GitHub https address), at most ``limit`` bytes. Raises ``SkillInstallError``."""
    headers = {"User-Agent": "agentx-skills"}
    if accept:
        headers["Accept"] = accept
    try:
        with urllib.request.build_opener(_SameHosts()).open(urllib.request.Request(url, headers=headers),
                                                           timeout=30) as response:
            data = response.read(limit + 1)
    except SkillInstallError:
        raise
    except urllib.error.HTTPError as e:
        hint = " GitHub limits anonymous requests; try again later." if e.code in (403, 429) else ""
        raise SkillInstallError(f"GitHub answered HTTP {e.code}.{hint}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SkillInstallError(f"The download failed ({type(e).__name__}).") from None
    if len(data) > limit:
        raise SkillInstallError("The download is bigger than the limit, so it was stopped.")
    return data


def extract_zip(data: bytes, dest: Path) -> Path:
    """Unpack a GitHub archive into ``dest`` and return the repository root inside it. Refuses absolute paths,
    ``..``, drive letters, too many or too big files; skips links."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillInstallError("The download is not a valid archive.") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise SkillInstallError("The archive has too many files.")
        total = 0
        base = dest.resolve()
        for info in infos:
            if info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                continue                                   # links are never unpacked
            name = info.filename
            parts = PurePosixPath(name.replace("\\", "/")).parts
            if (not parts or name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]):
                raise SkillInstallError("The archive has an unsafe path, so nothing was installed.")
            if info.file_size > MAX_FILE_BYTES:
                continue                                   # one huge file is skipped, not unpacked
            target = dest.joinpath(*parts)
            try:
                target.resolve().relative_to(base)
            except ValueError:
                raise SkillInstallError("The archive has an unsafe path, so nothing was installed.") from None
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info) as source, open(target, "wb") as out:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                        raise SkillInstallError("The archive unpacks to more than the limit, so it was stopped.")
                    out.write(chunk)
    tops = [p for p in dest.iterdir()]
    return tops[0] if len(tops) == 1 and tops[0].is_dir() else dest


@contextmanager
def open_source(text: str, *, get: HttpGet = http_get) -> Iterator[Fetched]:
    """Resolve the user's link or folder to files on disk for the duration of the ``with`` block. For GitHub the
    commit is looked up first and the archive of that exact commit is downloaded, so what the user is shown is
    what is installed."""
    source = parse_source(text)
    if source.kind == "local":
        yield Fetched(source.path, str(source.path))
        return
    ref = urllib.parse.quote(source.ref or "HEAD", safe="")
    sha = get(f"https://api.github.com/repos/{source.owner}/{source.repo}/commits/{ref}", 200,
              "application/vnd.github.sha").decode("ascii", errors="replace").strip()
    if not _SHA.match(sha):
        raise SkillInstallError("GitHub did not return a commit for that link.")
    data = get(f"https://codeload.github.com/{source.owner}/{source.repo}/zip/{sha}", MAX_ARCHIVE_BYTES, None)
    with tempfile.TemporaryDirectory(prefix="agentx-skills-") as tmp:
        root = extract_zip(data, Path(tmp))
        yield Fetched(root, f"https://github.com/{source.owner}/{source.repo}", sha, source.subpath)


# -- finding and showing --------------------------------------------------------

def _files_of(folder: Path) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """``(files, scripts)`` in a skill folder, links and the sidecar left out; None when it is over the limits."""
    files: List[str] = []
    scripts: List[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and not _is_link(here / d))
        for name in sorted(filenames):
            path = here / name
            if name == SIDECAR or _is_link(path):
                continue
            size = path.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(files) >= 500:
                return None
            relative = path.relative_to(folder).as_posix()
            files.append(relative)
            executable = bool(path.stat().st_mode & 0o111) and os.name != "nt"
            if PurePosixPath(relative).suffix.lower() in SCRIPT_SUFFIXES or executable:
                scripts.append(relative)
    return tuple(files), tuple(scripts)


def discover(root: Path, subpath: str = "") -> Tuple[List[Candidate], List[str]]:
    """The skills under ``root/subpath``: every folder holding a ``SKILL.md`` (not looking inside a skill, nor
    deeper than ``MAX_DEPTH``). Returns ``(skills, problems)``; a problem is one line about a folder skipped."""
    base = root / subpath if subpath else root
    try:
        base.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        raise SkillInstallError("That folder is not inside the repository.") from None
    if not base.is_dir():
        raise SkillInstallError("That folder does not exist in the repository.")
    found: List[Candidate] = []
    problems: List[str] = []
    names = set()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE and not d.startswith(".") and not _is_link(here / d))
        shown = here.relative_to(base).as_posix() or "."
        if SKILL_FILE in filenames:
            dirnames[:] = []                               # a skill's sub-folders are its files, not more skills
            try:
                skill = load_skill(here)
            except SkillFormatError as e:
                problems.append(f"{one_line(shown, 60)}: {e}")
                continue
            listing = _files_of(here)
            if listing is None:
                problems.append(f"{one_line(shown, 60)}: too big (over the file limits)")
            elif skill.name in names:
                problems.append(f"{one_line(shown, 60)}: a second skill named {skill.name}; ignored")
            else:
                names.add(skill.name)
                found.append(Candidate(skill.name, skill.description, here, *listing))
                if len(found) >= MAX_SKILLS:
                    problems.append(f"Stopped after {MAX_SKILLS} skills.")
                    break
        elif len(here.relative_to(base).parts) >= MAX_DEPTH:
            dirnames[:] = []
    return found, problems


def preview_text(fetched: Fetched, found: Sequence[Candidate], problems: Sequence[str] = ()) -> str:
    """What the user is shown before saying yes. Every piece of downloaded text is cut and made one line."""
    where = fetched.label + (f" (commit {fetched.commit[:10]})" if fetched.commit else "")
    lines = [f"Found {len(found)} skill{'s' if len(found) != 1 else ''} in {one_line(where, 200)}:"]
    for c in found:
        lines.append(f"  {c.name} - {one_line(c.description, 150)} ({len(c.files)} file{'s' if len(c.files) != 1 else ''})")
    scripts = sorted({s for c in found for s in c.scripts})
    if scripts:
        lines.append("Some skills include program files: " + ", ".join(one_line(s, 40) for s in scripts[:8])
                     + (f" and {len(scripts) - 8} more" if len(scripts) > 8 else "")
                     + ". Installing does not run them; the agent can only read them, and anything it runs "
                       "still asks you first.")
    lines.append("Skills are instructions the agent will follow. Install only ones you trust.")
    for problem in list(problems)[:5]:
        lines.append("Skipped: " + one_line(problem, 120))
    return "\n".join(lines)


# -- writing --------------------------------------------------------------------

def install(found: Sequence[Candidate], target: Path, fetched: Fetched) -> List[str]:
    """Copy ``found`` into ``target/<name>/`` and write each one's sidecar. Checks every name first and installs
    nothing when one is taken or is the name of a built-in skill. Returns the names installed."""
    target = Path(target)
    built_in = {b.name for b in bundled_skills()}
    for c in found:
        if c.name in built_in:
            raise SkillInstallError(f"{c.name!r} is the name of a built-in skill, so nothing was installed.")
        if os.path.lexists(target / c.name):
            raise SkillInstallError(f"{c.name!r} is already in {target}. Remove it first with /skills remove "
                                    f"{c.name}. Nothing was installed.")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SkillInstallError(f"Could not create {target} ({type(e).__name__}).") from None
    done: List[str] = []
    for c in found:
        staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=str(target)))
        try:
            for relative in c.files:
                source, dest = c.folder / relative, staging / relative
                if _is_link(source):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(source, "rb") as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            sidecar = {"managed": False, "source": fetched.label, "commit": fetched.commit,
                       "installed": datetime.now(timezone.utc).date().isoformat()}
            (staging / SIDECAR).write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
            os.replace(staging, target / c.name)
            done.append(c.name)
        except OSError as e:
            shutil.rmtree(staging, ignore_errors=True)
            raise SkillInstallError(f"Could not write {c.name} ({type(e).__name__}). Installed so far: "
                                    f"{', '.join(done) or 'nothing'}.") from None
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return done


def remove_skill(name: str, target: Path) -> None:
    """Delete ``target/<name>``. Only a plain folder directly inside ``target``; a name that is not a skill name
    (``..``, a path) is refused."""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise SkillInstallError(f"{one_line(name, 40)!r} is not a skill name.")
    folder = Path(target) / name
    if not folder.is_dir() or _is_link(folder):
        raise SkillInstallError(f"There is no skill folder named {name} in {target}.")
    try:
        shutil.rmtree(folder)
    except OSError as e:
        raise SkillInstallError(f"Could not remove {name} ({type(e).__name__}).") from None
