"""HTTP + search tools for agents that need to look things up online.

Ships two StructuredTools that any AgentRunner can be given:

  - web_fetch(url, max_chars=50_000)  — GET a URL, return the response body
  - web_search(query, num_results=5)  — DuckDuckGo, Wikipedia fallback

Both use only urllib.request — no extra dependencies. All network / HTTP
failures surface as string 'ERROR: ...' responses (never raise), so the
agent loop can react like it does to any other tool failure. Ideal for a
'researcher' specialist that reads web content alongside file / code
specialists.

When ``web_fetch_tool(cache_dir=...)`` is given a directory, every
successful fetch also writes the FULL body to a cache file and returns
the file path alongside the preview. This is the fix for a specific
LLM failure mode: without the cache, a model that wanted to parse a
big HTML page had to paste it back into run_python code as a string
literal. That inevitably overflowed context, so the model would put
``'...'`` as a placeholder, get no output, and eventually fabricate
data to make the code "work". With the cache, the model just does
``open(path).read()`` inside run_python — no re-transmission, no
placeholders.

Neither tool is registered in DefaultTools — networking is a distinct
capability and users may want to gate it separately from file access.
Plug them in explicitly:

    from agentx_dev.WebTools import web_fetch_tool, web_search_tool

    researcher = AgentRunner(
        model=llm, agent=AgentType.ReAct,
        tools=[web_search_tool(),
               web_fetch_tool(cache_dir="./workspace/.cache")],
        max_iterations=15,
    )
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from agentx_dev.Tools import StructuredTool


_UA = "Mozilla/5.0 (compatible; agentx_dev/1.0; +https://github.com/shadrach098/Bruce_framework)"

# Max hops we will follow before giving up. Every hop is re-validated
# against _assert_public_url, so a chain of redirects can't smuggle us
# to a private address after passing the front-door check.
_MAX_REDIRECTS = 5


def _assert_public_url(url: str) -> None:
    """Reject non-http(s) schemes and any URL whose host resolves to a
    non-public IP (loopback, RFC1918, link-local, multicast, reserved).
    Called once for the user URL AND again for every redirect hop.

    Blocks the classic SSRF payloads: 169.254.169.254 (cloud metadata),
    127.0.0.1 / ::1 (loopback), 10.*/172.16-31.*/192.168.* (RFC1918),
    fc00::/7 (unique-local), and anything else Python's ipaddress
    module flags as non-global."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise urllib.error.URLError(
            f"scheme not allowed: {parsed.scheme!r} (only http/https)"
        )
    host = parsed.hostname
    if not host:
        raise urllib.error.URLError("URL has no host component")

    # Resolve every address the host maps to and reject if ANY is
    # non-public — DNS rebinding + happy-eyeballs both mean the address
    # actually used later could differ from the first one returned, so
    # be strict about all of them.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise urllib.error.URLError(f"DNS lookup failed for {host!r}: {e}") from e
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        # is_global excludes: private, loopback, link_local, multicast,
        # reserved, unspecified. Exactly the SSRF sink set.
        if not ip.is_global:
            raise urllib.error.URLError(
                f"blocked: {host} resolves to non-public address {addr} "
                f"(SSRF guard)"
            )


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib redirect handler that re-runs _assert_public_url on every
    hop. Also caps total hops so an infinite redirect loop can't spin
    forever inside a tool call."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reguard the target BEFORE building the next request.
        _assert_public_url(newurl)
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        # Track hop count on the request object; parent handler doesn't
        # expose max-redirects as a hook, so we enforce our own cap.
        hops = getattr(req, "_agentx_redirects", 0) + 1
        if hops > _MAX_REDIRECTS:
            raise urllib.error.HTTPError(
                newurl, code, f"too many redirects (>{_MAX_REDIRECTS})",
                headers, fp,
            )
        new_req._agentx_redirects = hops
        return new_req


_GUARDED_OPENER = urllib.request.build_opener(_GuardedRedirectHandler())


class _WebFetchArgs(BaseModel):
    url: str = Field(..., description="URL to fetch (http:// or https:// required).")
    max_chars: int = Field(
        50_000,
        description=(
            "Max characters of response body to return. Longer bodies are "
            "truncated with a marker. Keep small to save context tokens."
        ),
    )


class _WebSearchArgs(BaseModel):
    query: str = Field(..., description="Search query — plain words, no operators required.")
    num_results: int = Field(
        5,
        description="Number of results to return (1–10). Extra are ignored.",
    )


def _cache_path_for(cache_dir: Path, url: str) -> Path:
    """Derive a stable, filesystem-safe cache filename from a URL.
    Uses sha1 for the unique part and preserves the URL's file
    extension when present so downstream tools can guess the mime type."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    parsed = urllib.parse.urlparse(url)
    tail = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
    suffix = ""
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if len(ext) <= 5 and ext.isalnum():
            suffix = f".{ext}"
    if not suffix:
        suffix = ".html"   # sensible default; most fetches are pages
    return cache_dir / f"fetch_{digest}{suffix}"


def _do_fetch(url: str, cache_dir: Optional[Path], max_chars: int) -> str:
    """Fetch a URL and return its text body (plus a saved-to line when a
    cache_dir is configured). Errors return an 'ERROR:' string; never
    raises so the tool dispatch can log cleanly and the agent can decide
    whether to retry a different URL."""
    if not url.startswith(("http://", "https://")):
        return f"ERROR: URL must start with http:// or https:// — got {url!r}"
    # SSRF guard: reject non-public destinations (loopback, RFC1918,
    # link-local metadata IPs, etc.) before we ever open a socket, and
    # again on every redirect hop via _GuardedRedirectHandler.
    try:
        _assert_public_url(url)
    except urllib.error.URLError as e:
        return f"ERROR: refused — {e.reason if hasattr(e, 'reason') else e}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    })
    try:
        with _GUARDED_OPENER.open(req, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        return f"ERROR: HTTP {e.code} {e.reason} — {url}"
    except urllib.error.URLError as e:
        return f"ERROR: network — {e.reason} — {url}"
    except TimeoutError:
        return f"ERROR: request timed out after 15s — {url}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e} — {url}"

    body = raw.decode("utf-8", errors="replace")
    total_bytes = len(raw)

    # Save FULL content (not the truncated preview) so run_python can
    # read the real thing. This is the fix for the "model pastes '...'
    # placeholder into scraping code" failure mode.
    cache_note = ""
    if cache_dir is not None:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = _cache_path_for(cache_dir, url)
            path.write_bytes(raw)
            cache_note = (
                f"\n\n[cached full body ({total_bytes} bytes) to: {path}]\n"
                f"To parse this in run_python:\n"
                f"    html = open(r'{path}', encoding='utf-8', errors='replace').read()\n"
                f"    # then use BeautifulSoup, re, json, etc. — do NOT paste HTML into your code"
            )
        except (OSError, PermissionError) as e:
            cache_note = f"\n\n[cache write failed: {e}; preview only]"

    if len(body) > max_chars:
        body = body[:max_chars] + f"\n\n... (preview truncated at {max_chars} chars; total was {total_bytes} bytes)"
    return body + cache_note


def _search_duckduckgo(query: str, num_results: int) -> list[tuple[str, str]]:
    """POST the query to DDG's HTML endpoint (their current anti-bot
    setup rejects GET). Returns [(title, url), ...] or empty list."""
    data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(
        "https://html.duckduckgo.com/html/",
        data=data,
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
        },
        method="POST",
    )
    try:
        _assert_public_url("https://html.duckduckgo.com/html/")
        with _GUARDED_OPENER.open(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    pattern = re.compile(
        r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    out: list[tuple[str, str]] = []
    for href, title_html in pattern.findall(body):
        # Unwrap the DDG redirect wrapper: real URL sits in ?uddg=<enc>
        m = re.search(r"uddg=([^&]+)", href)
        real_url = urllib.parse.unquote(m.group(1)) if m else href
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        title = re.sub(r"\s+", " ", title)
        if title and real_url:
            out.append((title, real_url))
        if len(out) >= num_results:
            break
    return out


def _search_wikipedia(query: str, num_results: int) -> list[tuple[str, str]]:
    """Wikipedia OpenSearch API — reliable fallback when DDG is unhappy.
    Broader/current-events queries will get shallow results but
    encyclopedic queries work well."""
    q = urllib.parse.quote(query)
    url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={q}&limit={num_results}&format=json"
    # Never cache the wikipedia-search result — it's transient JSON we
    # use only to build the search list.
    body = _do_fetch(url, cache_dir=None, max_chars=20_000)
    if body.startswith("ERROR:"):
        return []
    try:
        data = json.loads(body)
        titles = data[1] if len(data) > 1 else []
        urls   = data[3] if len(data) > 3 else []
        return list(zip(titles, urls))[:num_results]
    except (json.JSONDecodeError, TypeError, IndexError):
        return []


def _web_search(query: str, num_results: int = 5) -> str:
    """Search the web. Tries DuckDuckGo first (real search results);
    falls back to Wikipedia OpenSearch if DDG rejects the request.
    Returns a numbered list of 'title\n   url' entries, or an ERROR
    string if both backends fail."""
    cap = max(1, min(int(num_results), 10))
    hits = _search_duckduckgo(query, cap)
    source = "DuckDuckGo"
    if not hits:
        hits = _search_wikipedia(query, cap)
        source = "Wikipedia (DuckDuckGo unavailable)"
    if not hits:
        return "(no results — both DuckDuckGo and Wikipedia search failed or returned nothing)"
    lines = [f"(via {source})"]
    for i, (title, real_url) in enumerate(hits, 1):
        lines.append(f"{i}. {title}\n   {real_url}")
    return "\n".join(lines)


def web_fetch_tool(cache_dir: Optional[str] = None) -> StructuredTool:
    """Build a web_fetch tool.

    Args:
        cache_dir: When set, every successful fetch also writes the FULL
            body to a file under this directory and the tool response
            includes a "[cached full body to: <path>]" line telling the
            model how to load it in run_python. Use this whenever the
            agent has run_python — it prevents the "paste HTML into
            code" failure mode. Leave None for a lightweight tool that
            only returns text to the model's context.
    """
    resolved_dir = Path(cache_dir).resolve() if cache_dir else None

    def _fetch(url: str, max_chars: int = 50_000) -> str:
        return _do_fetch(url, cache_dir=resolved_dir, max_chars=max_chars)

    if resolved_dir is not None:
        description = (
            "Fetch a URL over HTTP/HTTPS. Returns a preview of the body "
            f"AND saves the FULL body to a file under {resolved_dir}. "
            "The response includes the exact cache path plus a snippet "
            "showing how to open it. IMPORTANT: when you need to parse "
            "or scrape the fetched content in run_python, ALWAYS load "
            "it from the cache path — do NOT paste HTML into your "
            "Python code as a string literal. Pasting is what makes "
            "the model use '...' placeholders that produce empty "
            "output. Errors return an 'ERROR:' string; no exception."
        )
    else:
        description = (
            "Fetch a URL over HTTP/HTTPS and return the response body "
            "as text. Automatically truncated at max_chars (default "
            "50k). Errors return an 'ERROR:' string; no exception."
        )

    return StructuredTool(
        func=_fetch,
        args_schema=_WebFetchArgs,
        name="web_fetch",
        description=description,
    )


def web_search_tool() -> StructuredTool:
    return StructuredTool(
        func=_web_search,
        args_schema=_WebSearchArgs,
        name="web_search",
        description=(
            "Search the web via DuckDuckGo (with Wikipedia fallback if "
            "DDG blocks). Returns the top N results as 'title + URL'. "
            "Use this when you need to FIND URLs — then use web_fetch on "
            "the ones that look promising. Best for open-ended research "
            "questions; not a substitute for hitting a known URL directly. "
            "No API key needed."
        ),
    )
