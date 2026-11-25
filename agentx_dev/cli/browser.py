"""A private Chromium agentx can drive: one per agentx process, one page per conversation.

``BrowserHost`` owns Playwright's sync API on a single worker thread (Playwright objects must stay on the thread that
made them); every public method hands a callable to that thread and waits for its answer. Chromium starts headless,
with an empty temporary profile, on the first call that needs a page, and ``close()`` (registered at exit for
``shared_browser()``) shuts it and the thread down. Playwright is optional: without it, or without its Chromium,
every call raises ``BrowserUnavailable(NOT_INSTALLED)``.

Each owner (a conversation's id) gets its own ``BrowserContext`` (no downloads, 1280x800) and page, so two
conversations never move each other's page or refs.

Which addresses the browser may reach: ``http``/``https`` pages whose host is public or loopback (the local dev
server), and ``about:blank``. Two layers enforce it:

1. ``url_refusal`` is checked before every ``open`` and every address-bar navigation, and a route handler on each
   context aborts any routed request (navigations, frames, fetches, images...) whose URL it refuses.
2. Playwright does not route the later hops of a redirect, so a page could redirect to ``http://169.254.169.254/``
   past the route handler. Chromium is therefore started with a small proxy on 127.0.0.1 (``_GuardProxy``) that
   every connection goes through (plain HTTP, HTTPS ``CONNECT`` tunnels and WebSockets alike); it resolves the host
   itself, refuses non-public, non-loopback hosts, and connects to the very addresses it checked, so a DNS answer
   that changes between the check and the connection is not used for anything that goes through it.

Host verdicts are cached for 30 s (``DNS_TTL_SEC``). Residual risks, accepted for v1: a loopback address Chromium
reaches without the proxy is allowed anyway; WebRTC is told to send UDP only through the proxy (which carries none),
so it cannot reach local addresses directly, but that rests on a Chromium switch; ``data:``/``blob:`` subresources
(inline images, fonts) never touch the network and are let through, while navigations to them are refused.
"""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import logging
import queue
import re
import socket
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

from agentx_dev.WebTools import _is_public
from agentx_dev.cli.tools import WEB_CAUTION, WEB_MAX_CHARS

log = logging.getLogger(__name__)

NOT_INSTALLED = ('The browser needs Playwright: pip install "agentx-dev[browser]" '
                 'then python -m playwright install chromium')

VIEWPORT = (1280, 800)
ACTION_TIMEOUT_SEC = 60.0           # how long a caller waits for the worker to answer one call ...
LOAD_CALL_TIMEOUT_SEC = 90.0        # ... or one that loads a page (open, back, the address bar)
NAV_TIMEOUT_MS = 30_000             # a page load
SETTLE_LOAD_SEC = 20.0              # after an action: wait at most this long for a page load it started to commit
ELEMENT_TIMEOUT_MS = 10_000         # waiting for an element to be clickable / fillable
SETTLE_IDLE_MS = 1_500              # after an action: wait at most this long for the network to go quiet
MAX_ELEMENTS = 300                  # interactive elements listed in a snapshot
NAME_MAX = 80                       # characters of an element's name or value shown
CONSOLE_MAX = 50                    # console lines kept per owner
FRAME_CHECK_SEC = 0.3               # frame(): look for a change at most this often (unless an action ran)
FRAME_QUALITY = 70                  # JPEG quality of the window's frames
FRAME_CALL_TIMEOUT_SEC = 2.0        # frame(): how long a window's poll waits for a new picture before the last one
TEXT_MAX = 64 * 1024                # user_event: the most text typed in one event
PUMP_SEC = 0.05                     # while idle, let Playwright deliver events (routes, console, dialogs) this often
DNS_TTL_SEC = 30.0                  # url_refusal caches a host's verdict this long

SCHEME_REFUSAL = "only http and https pages can be opened"


class BrowserUnavailable(RuntimeError):
    """The browser cannot run: ``NOT_INSTALLED`` when Playwright or its Chromium is missing, else
    ``The browser could not start: <reason>``."""


HELD_BY_USER = "The user is using the browser right now; wait or ask them."


class HeldByUser(ValueError):
    """An agent's action reached the worker after the user took the page over; its message is ``HELD_BY_USER``."""


class _Refused(Exception):
    """A host the browser may not reach; the message is the reason."""


# --- which addresses may be opened ------------------------------------------------------------------------------

_lookups: Dict[str, Tuple[float, List[str], Optional[str]]] = {}
_lookups_lock = threading.Lock()


def _forget_lookups() -> None:
    """Drop the cached host verdicts (tests)."""
    with _lookups_lock:
        _lookups.clear()


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _address_refusal(host: str, addr: str) -> Optional[str]:
    """None when ``addr`` is public or loopback, else why ``host`` (which resolved to it) is refused."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return f"{host} is not a public address"
    if ip.version == 6:                                 # an IPv4 address carried inside IPv6 is judged as IPv4:
        if ip.ipv4_mapped is not None:                  # ::ffff:127.0.0.1
            ip = ip.ipv4_mapped
        elif ip in _NAT64:                              # 64:ff9b::10.0.0.5 (NAT64 reaches the IPv4 address)
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip.sixtofour is not None:                  # 2002:0a00:0005:: (6to4)
            ip = ip.sixtofour
    if ip.is_loopback or _is_public(str(ip)):
        return None
    bare = addr.split("%", 1)[0]
    suffix = "" if host == bare else f" ({bare})"
    if ip.is_private and not ip.is_unspecified:         # 10/8, 172.16/12, 192.168/16, 169.254/16, fc00::/7, fe80::/10
        return f"{host} is a private address{suffix}"
    return f"{host} is not a public address{suffix}"     # 0.0.0.0, multicast, reserved ...


def _host_key(host: str) -> str:
    """A host as the refusal records compare it: lower case, no brackets, no trailing dot."""
    host = (host or "").strip().strip("[]").lower()
    return host[:-1] if host.endswith(".") and len(host) > 1 else host


def _checked_addresses(host: str, resolve: bool = True) -> List[str]:
    """The addresses ``host`` resolves to, all public or loopback; raises ``_Refused`` with the reason otherwise.
    With ``resolve=False`` a host name is not looked up (``[]`` comes back): only literal addresses are judged."""
    host = (host or "").strip().strip("[]").lower()
    if host.endswith(".") and len(host) > 1:
        host = host[:-1]
    if not host:
        raise _Refused("the address has no host")
    if host == "localhost" or host.endswith(".localhost"):   # Chromium answers these with loopback itself
        return ["127.0.0.1", "::1"]
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        reason = _address_refusal(host, host)
        if reason:
            raise _Refused(reason)
        return [host]
    if not resolve:
        return []
    now = time.monotonic()
    with _lookups_lock:
        cached = _lookups.get(host)
    if cached and cached[0] > now:
        _, addrs, reason = cached
    else:
        addrs, reason = [], None
        try:
            for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
                addr = info[4][0]
                if addr not in addrs:
                    addrs.append(addr)
        except (OSError, UnicodeError, ValueError):        # ValueError: a host with a NUL in it
            reason = f"{host!r} could not be found" if "\x00" in host else f"{host} could not be found"
        if not reason and not addrs:
            reason = f"{host} could not be found"
        for addr in addrs:                              # every address must pass: the connection may use any
            reason = reason or _address_refusal(host, addr)
        with _lookups_lock:
            if len(_lookups) > 2000:
                _lookups.clear()
            _lookups[host] = (now + DNS_TTL_SEC, addrs, reason)
    if reason:
        raise _Refused(reason)
    return list(addrs)


def url_refusal(url: str) -> Optional[str]:
    """None when the browser may open ``url``; else the reason it may not.

    Allowed: ``about:blank``, and ``http``/``https`` addresses whose host resolves only to public or loopback
    addresses. Everything else (``file:``, ``data:``, ``javascript:``, ``chrome:``, private and link-local hosts...)
    is refused."""
    return _url_refusal(url, resolve=True)


def _url_refusal(url: str, resolve: bool) -> Optional[str]:
    if not isinstance(url, str):
        return SCHEME_REFUSAL
    text = re.sub(r"[\t\n\r]", "", url).strip()         # a browser drops these from an address before reading it
    if text.lower() == "about:blank":
        return None
    scheme = text.split(":", 1)[0].lower() if ":" in text else ""
    if scheme not in ("http", "https"):
        return SCHEME_REFUSAL
    text = text.replace("\\", "/")                      # as Chromium reads http(s) addresses
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
        parts.port                                      # raises ValueError for a bad port
    except ValueError:
        return "the address is not valid"
    try:
        _checked_addresses(host or "", resolve=resolve)
    except _Refused as refused:
        return str(refused)
    return None


# --- the proxy every browser connection goes through -------------------------------------------------------------

_HOP_HEADERS = (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive")


def _read_head(conn: socket.socket, data: bytes = b"", limit: int = 64 * 1024) -> Tuple[Optional[bytes], bytes]:
    """Read up to the blank line that ends an HTTP head: ``(head, bytes read after it)``, or ``(None, b"")``."""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(8192)
        if not chunk:
            return None, b""
        data += chunk
        if len(data) > limit:
            return None, b""
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _kill(conn: Optional[socket.socket]) -> None:
    """Shut a socket both ways and close it (shutdown first: on Linux/macOS a close alone does not wake a thread
    blocked on it)."""
    if conn is None:
        return
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def _pipe(src: socket.socket, dst: socket.socket, last: Optional[List[float]] = None, idle: float = 0.0) -> None:
    """Copy ``src`` to ``dst`` until ``src`` ends or either socket fails. ``last`` (shared by both directions of a
    connection) holds the time anything last moved; when ``src``'s timeout fires and nothing moved either way for
    ``idle`` seconds, the connection counts as ended."""
    while True:
        try:
            data = src.recv(65536)
        except socket.timeout:
            if last is not None and time.monotonic() - last[0] < idle:
                continue                                # the other direction is busy (a download, a stream)
            return
        except OSError:
            return
        if not data:
            return
        if last is not None:
            last[0] = time.monotonic()
        try:
            dst.sendall(data)
        except OSError:
            return


def _connect(addrs: List[str], port: int) -> socket.socket:
    last: Optional[OSError] = None
    for addr in addrs:
        try:
            return socket.create_connection((addr, port), timeout=15)
        except OSError as error:
            last = error
    raise last or OSError("no address")


class _BadRequest(Exception):
    """A request the proxy does not read (answered 400)."""


def _port(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", text) or not 1 <= int(text) <= 65535:
        raise _BadRequest()
    return int(text)


_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _clean_header_lines(lines: List[bytes]) -> List[bytes]:
    """Header lines minus any ``X-Agentx-*`` header and any malformed line (no name, a space before the colon, a
    folded continuation line)."""
    kept = []
    for line in lines:
        name, sep, _ = line.partition(b":")
        if not sep or not _HEADER_NAME.fullmatch(name) or name.lower().startswith(b"x-agentx-"):
            continue
        kept.append(line)
    return kept


def _matching_refusal(refusals: List[Tuple[float, str, str]], navigations: List[Tuple[float, str]],
                      mark: float) -> str:
    """The reason of the newest proxy refusal since ``mark`` whose host one of the page's own navigations (or redirect
    hops) since ``mark`` asked for; "" when there is none. Only the proxy's own record decides: nothing a site sends
    can make a navigation count as refused, or choose the reason's text."""
    hosts = {host for at, host in navigations if at >= mark}
    for at, host, reason in reversed(refusals):
        if at >= mark and host in hosts:
            return reason
    return ""


def _request_line(head: bytes) -> Tuple[str, str, str]:
    parts = head.split(b"\n", 1)[0].rstrip(b"\r").decode("latin-1").split(" ")
    if len(parts) != 3 or not parts[0].isalpha() or not parts[1] or not parts[2].startswith("HTTP/"):
        raise _BadRequest()
    return parts[0], parts[1], parts[2]


PROXY_IDLE_SEC = 300.0              # a proxied connection silent both ways this long is closed
PROXY_MAX_CONNECTIONS = 64          # connections served at once; one more waits up to 5 s for a slot, then is closed


class _GuardProxy:
    """An HTTP proxy on 127.0.0.1 for Chromium: it refuses hosts ``url_refusal`` would refuse, on every connection.

    Plain HTTP requests come in absolute form (``GET http://host/path``) and are sent on with ``Connection: close``
    (one request per connection, so a reused connection cannot reach a second host unchecked), with any
    ``X-Agentx-*`` header dropped from the answer; HTTPS and WebSockets come as ``CONNECT host:port`` and are
    tunnelled. A refusal answers a bare ``403`` ("Blocked by agentx": nothing about the host, the reason or the
    address, since page scripts can read it) and is written to ``refusals`` as ``(time, host, reason)``: agentx
    reports a refusal from that record only. Anything else it cannot read gets ``400``. When either side of a
    connection ends, or nothing moves either way for ``idle_timeout`` seconds, both sockets are closed."""

    def __init__(self) -> None:
        self.idle_timeout = PROXY_IDLE_SEC
        self._open: Set[socket.socket] = set()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(PROXY_MAX_CONNECTIONS)
        self._closed = False
        self.refusals: Deque[Tuple[float, str, str]] = deque(maxlen=100)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(64)
        except OSError:
            self._listener.close()
            raise
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, name="agentx-browser-proxy", daemon=True).start()

    def _track(self, conn: Optional[socket.socket]) -> None:
        if conn is not None:
            with self._lock:
                self._open.add(conn)

    def _untrack(self, conn: Optional[socket.socket]) -> None:
        if conn is None:
            return
        with self._lock:
            self._open.discard(conn)
        _kill(conn)

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break
            if not self._slots.acquire(timeout=5):      # too many at once: this one is dropped
                _kill(conn)
                continue
            self._track(conn)
            threading.Thread(target=self._serve_in_slot, args=(conn,), daemon=True).start()

    def _serve_in_slot(self, client: socket.socket) -> None:
        try:
            self._serve(client)
        finally:
            self._slots.release()

    def _answer(self, client: socket.socket, status: str, extra: str = "", body: bytes = b"") -> None:
        head = (f"HTTP/1.1 {status}\r\n{extra}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        try:
            client.sendall(head.encode("latin-1", "replace") + body)
        except OSError:
            pass

    def _refuse(self, client: socket.socket, host: str, reason: str) -> None:
        self.refusals.append((time.monotonic(), _host_key(host), reason))
        self._answer(client, "403 Forbidden", "Content-Type: text/html; charset=utf-8\r\n", _BLOCKED_PAGE)

    def _forward_head(self, upstream: socket.socket, client: socket.socket) -> Optional[bytes]:
        """Send the upstream answer's head(s) on without ``X-Agentx-*`` headers; returns the body bytes already
        read, or None when the upstream ended first. Interim ``1xx`` heads (not ``101``) are followed by another."""
        buffered = b""
        while True:
            head, buffered = _read_head(upstream, buffered)
            if head is None:
                return None
            lines = [line.rstrip(b"\r") for line in head.split(b"\n")]   # a bare LF ends a line too
            kept = [lines[0]] + _clean_header_lines(lines[1:])
            client.sendall(b"\r\n".join(kept) + b"\r\n\r\n")
            status = lines[0].split(b" ", 2)[1] if lines[0].count(b" ") else b""
            if not (status.startswith(b"1") and status != b"101"):
                return buffered

    def _serve(self, client: socket.socket) -> None:
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(30)
            head, rest = _read_head(client)
            if head is None:
                return
            plain = False
            asked = ""
            try:
                method, target, version = _request_line(head)
                if method.upper() == "CONNECT":
                    host, sep, port_text = target.rpartition(":")
                    if not sep or not host:
                        raise _BadRequest()
                    port = _port(port_text)
                    asked = host
                    upstream = _connect(_checked_addresses(host), port)
                    self._track(upstream)
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    try:
                        parts = urllib.parse.urlsplit(target)
                        hostname = parts.hostname
                        port = parts.port or 80
                    except ValueError:                  # "http://[x/", a bad port
                        raise _BadRequest() from None
                    if parts.scheme.lower() != "http" or not hostname:   # origin form is not for a proxy
                        raise _BadRequest()
                    asked = hostname
                    upstream = _connect(_checked_addresses(hostname), port)
                    self._track(upstream)
                    headers = [h for h in head.split(b"\r\n")[1:] if h]
                    upgrade = any(h.split(b":", 1)[0].strip().lower() == b"upgrade" for h in headers)
                    if not upgrade:
                        headers = [h for h in headers if h.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS]
                        headers.append(b"Connection: close")
                    path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
                    upstream.sendall(b"\r\n".join([f"{method} {path} {version}".encode("latin-1")] + headers)
                                     + b"\r\n\r\n")
                    plain = True
            except _BadRequest:
                self._answer(client, "400 Bad Request")
                return
            except _Refused as refused:
                self._refuse(client, asked, str(refused))
                return
            idle = self.idle_timeout
            last = [time.monotonic()]
            client.settimeout(idle)
            upstream.settimeout(idle)
            if rest:
                upstream.sendall(rest)

            def back() -> None:
                try:
                    if plain:
                        body = self._forward_head(upstream, client)
                        if body is None:
                            return
                        if body:
                            client.sendall(body)
                    _pipe(upstream, client, last, idle)
                except OSError:
                    pass
                finally:                                # either direction ending ends both
                    _kill(client)
                    _kill(upstream)

            answer = threading.Thread(target=back, daemon=True)
            answer.start()
            _pipe(client, upstream, last, idle)
            _kill(client)
            _kill(upstream)
            answer.join(timeout=5)
        except OSError:
            if upstream is None:
                self._answer(client, "502 Bad Gateway")
        finally:
            self._untrack(upstream)
            self._untrack(client)

    def open_connections(self) -> int:
        with self._lock:
            return len(self._open)

    def close(self) -> None:
        self._closed = True
        _kill(self._listener)
        with self._lock:
            conns = list(self._open)
            self._open.clear()
        for conn in conns:
            _kill(conn)


_BLOCKED_PAGE = b"<!doctype html><title>Blocked by agentx</title><h1>Blocked by agentx</h1>"


# --- what the agent reads ---------------------------------------------------------------------------------------

@dataclass
class Snapshot:
    """The page as text: its visible text and its numbered interactive elements (``[e3] button "Save"``)."""
    url: str
    title: str
    text: str
    elements: List[str]
    error: str = ""                                     # "ERROR: ..." when the last action did not go as asked

    def render(self) -> str:
        parts = [WEB_CAUTION + f"Page: {self.title}\nURL: {self.url}\n\n"]
        if self.error:
            parts.append(self.error + "\n\n")
        parts.append(f"{self.text}\n\nInteractive elements:\n")
        parts.append("\n".join(self.elements) if self.elements else "(none)")
        return "".join(parts)


# Tags the visible interactive elements of the main frame with data-agentx-ref="e<n>" (old tags cleared first) and
# returns them with the page's visible text. Password values never leave the page.
_SNAPSHOT_JS = r"""
(limits) => {
  const [maxElements, nameMax] = limits;
  for (const el of document.querySelectorAll('[data-agentx-ref]')) el.removeAttribute('data-agentx-ref');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = s => { s = clean(s); return s.length > nameMax ? s.slice(0, nameMax - 3) + '...' : s; };
  const selector = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [role=button], '
    + '[role=link], [role=checkbox], [role=tab], [role=menuitem], [contenteditable]:not([contenteditable=false])';
  const visible = el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (el.checkVisibility) return el.checkVisibility({visibilityProperty: true});
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const roleOf = el => {
    const role = el.getAttribute('role');
    if (role) return role;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') return type;
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      if (type === 'file') return 'file-input';
      return 'textbox';
    }
    return el.isContentEditable ? 'textbox' : tag;
  };
  const nameOf = el => {
    const tag = el.tagName.toLowerCase();
    let name = el.getAttribute('aria-label');
    const by = el.getAttribute('aria-labelledby');
    if (!clean(name) && by) name = by.split(/\s+/).map(id => {
      const t = document.getElementById(id); return t ? t.innerText : ''; }).join(' ');
    if (!clean(name) && el.labels && el.labels.length) name = Array.from(el.labels).map(l => l.innerText).join(' ');
    if (!clean(name) && tag === 'input' && ['submit', 'button', 'reset'].includes(el.type)) name = el.value;
    if (!clean(name) && tag === 'input' && el.type === 'image') name = el.alt;
    if (!clean(name) && !['input', 'textarea', 'select'].includes(tag) && !el.isContentEditable) name = el.innerText;
    if (!clean(name)) {
      const img = el.querySelector && el.querySelector('img[alt]');
      name = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt')
        || (img ? img.alt : '') || el.getAttribute('name') || '';
    }
    return cut(name);
  };
  const valueOf = el => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'password') return el.value ? '(hidden)' : '';
      if (['checkbox', 'radio', 'submit', 'button', 'reset', 'image', 'file'].includes(type)) return '';
      return cut(el.value);
    }
    if (tag === 'textarea') return cut(el.value);
    if (tag === 'select') return cut(Array.from(el.selectedOptions).map(o => o.text).join(', '));
    if (el.isContentEditable) return cut(el.innerText);
    return '';
  };
  const found = [];
  let total = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (!visible(el)) continue;
    total += 1;
    if (found.length >= maxElements) continue;
    const ref = 'e' + (found.length + 1);
    el.setAttribute('data-agentx-ref', ref);
    const checkable = el.tagName.toLowerCase() === 'input'
      && ['checkbox', 'radio'].includes((el.type || '').toLowerCase());
    const ariaChecked = el.getAttribute('aria-checked');
    found.push({ref, role: roleOf(el), name: nameOf(el), value: valueOf(el),
                checked: checkable ? el.checked : ariaChecked === 'true',
                disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true')});
  }
  return {elements: found, total, text: document.body ? document.body.innerText : ''};
}
"""


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _element_line(item: dict) -> str:
    line = f"[{item['ref']}] {item['role']} {_quote(item.get('name') or '')}"
    if item.get("value"):
        line += f" value={_quote(item['value'])}"
    if item.get("checked"):
        line += " checked"
    if item.get("disabled"):
        line += " disabled"
    return line


def _first_line(error: BaseException) -> str:
    text = str(getattr(error, "message", "") or error).strip()
    return text.splitlines()[0] if text else type(error).__name__


# --- the host ---------------------------------------------------------------------------------------------------

@dataclass
class _Owner:
    """One conversation's browser: its context, its pages (the newest is the current one) and what the window shows."""
    context: object
    pages: list = field(default_factory=list)
    console: Deque[str] = field(default_factory=lambda: deque(maxlen=CONSOLE_MAX))
    refusals: Deque[Tuple[float, str]] = field(default_factory=lambda: deque(maxlen=20))
    lock: threading.Lock = field(default_factory=threading.Lock)
    url: str = ""
    title: str = ""
    frame_no: int = 0
    frame_jpeg: Optional[bytes] = None
    frame_hash: bytes = b""
    frame_at: float = 0.0
    dirty: bool = True
    nav_at: float = 0.0                                 # when the current page's main frame last asked for a page
    nav_done_at: float = 0.0                            # ... and when such a request last committed or failed
    nav_finished_at: float = 0.0                        # ... or finished loading (a 204, a refused download)
    nav_hosts: Deque[Tuple[float, str]] = field(default_factory=lambda: deque(maxlen=50))   # hosts it asked for
    nav_status: Tuple[float, int] = (0.0, 0)            # (when, status) of the main frame's last page answer
    capture: Optional[Future] = None                    # the window's picture being taken (queued or running)

    def loading(self) -> bool:
        """A main-frame page load asked for and not yet committed. One that finished loading without committing
        (nothing to show) stops counting a second after it finished."""
        if self.nav_done_at >= self.nav_at:
            return False
        return not (self.nav_finished_at >= self.nav_at and time.monotonic() - self.nav_finished_at > 1.0)

    def note(self, line: str) -> None:
        with self.lock:
            self.console.append(line[:500])


class BrowserHost:
    """A private headless Chromium with one context and page per owner, driven from one worker thread.

    Methods that act on a page return a fresh ``Snapshot``. A refused address raises ``ValueError`` whose message is
    the reason; a target that matches nothing, a key Playwright does not know or a worker that does not answer in
    time raise ``ValueError`` whose message starts ``ERROR:``. Without Playwright or Chromium, ``BrowserUnavailable``.
    ``take``/``give``/``held_by_user``/``state`` never start the browser."""

    def __init__(self, viewport: Tuple[int, int] = VIEWPORT) -> None:
        self.viewport = (int(viewport[0]), int(viewport[1]))
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._queue: "queue.Queue" = queue.Queue()
        self._owners: Dict[str, _Owner] = {}
        self._held: Set[str] = set()
        self._pw = None
        self._browser = None
        self._proxy: Optional[_GuardProxy] = None
        self._Error: type = Exception
        self._Timeout: type = Exception

    # -- the worker ----------------------------------------------------------------------------------------------

    def _submit(self, fn: Callable, *args) -> Future:
        """Queue ``fn(*args)`` for the worker thread (run at once when called on it); its future."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._queue = queue.Queue()
                self._thread = threading.Thread(target=self._run, args=(self._queue,),
                                                name="agentx-browser", daemon=True)
                self._thread.start()
            work = self._queue
            worker = self._thread
        future: Future = Future()
        if threading.current_thread() is worker:
            try:
                future.set_result(fn(*args))
            except BaseException as error:
                future.set_exception(error)
            return future
        work.put((fn, args, future))
        return future

    def _call(self, fn: Callable, *args, timeout: float = ACTION_TIMEOUT_SEC):
        """Run ``fn(*args)`` on the worker thread and return its result (or raise its exception).

        A call still queued at ``timeout`` (another conversation's slow page load held the worker) is cancelled, so
        it never runs later: the caller was told it did not answer and may well try again. One already running
        cannot be stopped; the error says it may still finish."""
        future = self._submit(fn, *args)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            if future.cancel():
                raise ValueError(f"ERROR: the browser did not answer within {timeout:g} seconds; "
                                 "nothing was done") from None
            raise ValueError(f"ERROR: the browser did not answer within {timeout:g} seconds; what was asked "
                             "is still running and may still finish, so read the page before trying again") from None

    def _agent(self, owner: str, work: Callable) -> Callable:
        """``work`` for an agent's action on ``owner``'s page: on the worker it first checks the user has not taken
        the page over since the tool checked (the call may have waited behind another one). Not for the user's own
        events."""
        def checked():
            if self.held_by_user(owner):
                raise HeldByUser(HELD_BY_USER)
            return work()
        return checked

    def _run(self, work: "queue.Queue") -> None:
        while True:
            try:
                # With a page open, wake up often so Playwright can deliver its events (route handlers, console
                # lines, dialogs, file choosers) while no call is running; without one, sleep until a call comes.
                item = work.get(timeout=PUMP_SEC) if self._any_page() else work.get()
            except queue.Empty:
                self._pump()
                continue
            if item is None:
                break
            fn, args, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as error:              # handed to the caller
                future.set_exception(error)
        self._shutdown()
        while True:                                     # anything queued behind the stop
            try:
                item = work.get_nowait()
            except queue.Empty:
                break
            if item is not None and item[2].set_running_or_notify_cancel():
                item[2].set_exception(ValueError("ERROR: the browser was closed"))

    def _any_page(self) -> bool:
        return any(rec.pages for rec in list(self._owners.values()))

    def _pump(self) -> None:
        for rec in list(self._owners.values()):
            if rec.pages:
                try:
                    rec.pages[-1].wait_for_timeout(1)
                except Exception:
                    pass
                return

    def _ensure(self) -> None:
        """Start Playwright and Chromium (on the worker thread) unless they run already."""
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import Error, TimeoutError as PlaywrightTimeout, sync_playwright
        except ImportError:
            raise BrowserUnavailable(NOT_INSTALLED) from None
        proxy = pw = None
        try:
            proxy = _GuardProxy()
            pw = sync_playwright().start()
            self._browser = pw.chromium.launch(
                headless=True,
                proxy={"server": f"http://127.0.0.1:{proxy.port}"},
                args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--webrtc-ip-handling-policy=disable_non_proxied_udp"])
        except Exception as error:
            log.debug("the browser did not start: %s", error)
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
            if proxy is not None:
                proxy.close()
            self._browser = None
            text = str(error)
            if "Executable doesn't exist" in text or "playwright install" in text:   # Chromium was never downloaded
                raise BrowserUnavailable(NOT_INSTALLED) from None
            raise BrowserUnavailable(f"The browser could not start: {_first_line(error)}") from None
        self._pw, self._proxy = pw, proxy
        self._Error, self._Timeout = Error, PlaywrightTimeout

    def _shutdown(self) -> None:
        for owner in list(self._owners):
            self._drop(owner)
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        if self._proxy is not None:
            self._proxy.close()
        self._browser = self._pw = self._proxy = None

    def _drop(self, owner: str) -> None:
        rec = self._owners.pop(owner, None)
        if rec is None:
            return
        try:
            rec.context.unroute_all(behavior="ignoreErrors")
        except Exception:
            pass
        try:
            rec.context.close()
        except Exception:
            pass

    # -- owners and pages (worker thread) -------------------------------------------------------------------------

    def _owner(self, owner: str) -> _Owner:
        self._ensure()
        rec = self._owners.get(owner)
        if rec is None:
            context = self._browser.new_context(
                accept_downloads=False, service_workers="block",
                viewport={"width": self.viewport[0], "height": self.viewport[1]})
            context.set_default_timeout(ELEMENT_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            rec = _Owner(context=context)
            context.route("**/*", lambda route: self._route(rec, route))
            context.on("page", lambda page: self._adopt(rec, page))
            self._owners[owner] = rec
        return rec

    def _page(self, owner: str):
        """The owner's current page, made (about:blank) when it has none."""
        rec = self._owner(owner)
        if not rec.pages:
            self._adopt(rec, rec.context.new_page())
        return rec.pages[-1]

    def _adopt(self, rec: _Owner, page) -> None:
        """Track a page of the owner's context (its first page, or a popup, which becomes the current page)."""
        if page in rec.pages:
            return
        rec.pages.append(page)
        rec.dirty = True

        def closed(_page=None) -> None:
            if page in rec.pages:
                rec.pages.remove(page)
            rec.dirty = True

        def navigated(frame) -> None:
            if frame == page.main_frame:
                rec.nav_done_at = time.monotonic()
                if rec.pages and rec.pages[-1] is page:
                    rec.url = frame.url
                    rec.dirty = True

        def main_frame_request(request, stage: str) -> None:
            try:
                if request.is_navigation_request() and request.frame == page.main_frame:
                    now = time.monotonic()
                    setattr(rec, stage, now)
                    if stage == "nav_at":               # every hop of a redirect is asked for here too
                        rec.nav_hosts.append((now, _host_key(urllib.parse.urlsplit(request.url).hostname or "")))
            except Exception:
                pass

        def dialog(d) -> None:
            rec.note(f"[dialog] {d.type}: {d.message}")
            try:
                d.dismiss()
            except Exception:
                pass

        page.on("close", closed)
        page.on("framenavigated", navigated)
        page.on("request", lambda request: main_frame_request(request, "nav_at"))
        page.on("requestfailed", lambda request: main_frame_request(request, "nav_done_at"))
        page.on("requestfinished", lambda request: main_frame_request(request, "nav_finished_at"))

        def main_frame_response(response) -> None:
            try:
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    rec.nav_status = (time.monotonic(), response.status)
            except Exception:
                pass
        page.on("response", main_frame_response)
        page.on("console", lambda message: rec.note(f"[{message.type}] {message.text}"))
        page.on("pageerror", lambda error: rec.note(f"[pageerror] {error}"))
        page.on("dialog", dialog)
        # A listener keeps the system file dialog from opening; no file is ever set, so the choice is cancelled.
        page.on("filechooser", lambda chooser: rec.note("[filechooser] a file chooser opened and was cancelled"))
        page.on("download", lambda download: rec.note(f"[download] refused: {download.url}"))

    def _route(self, rec: _Owner, route) -> None:
        """Abort a request the browser may not make; let the rest through (the proxy checks again)."""
        try:
            request = route.request
            url = request.url
            scheme = url.split(":", 1)[0].lower()
            if scheme in ("http", "https"):
                # Scheme and literal addresses only: a name is resolved (and pinned) by the proxy, so no DNS
                # lookup ever runs on the worker thread.
                reason = _url_refusal(url, resolve=False)
            elif request.is_navigation_request() and url.lower() != "about:blank":
                reason = SCHEME_REFUSAL
            else:
                reason = None                           # data:/blob: images and fonts of a page
            if reason:
                # Only the main frame's own navigation is the page's refusal; a refused frame, image or fetch is a
                # console note and the page itself still loaded.
                if request.is_navigation_request() and request.frame.parent_frame is None:
                    rec.refusals.append((time.monotonic(), reason))
                rec.note(f"[refused] {url}: {reason}")
                route.abort("blockedbyclient")
            else:
                route.continue_()
        except Exception:
            try:
                route.abort("blockedbyclient")
            except Exception:
                pass

    def _ended_badly(self, rec: _Owner, page, mark: float) -> bool:
        """Whether the page an action or navigation since ``mark`` led to is not a page that loaded: Chromium's
        error page (a refused or failed load, a refused tunnel) or a 403 main-frame answer (the proxy refuses plain
        HTTP with one). A page that loaded is never reported refused, whatever its frames or fetches ran into."""
        if page.url.startswith("chrome-error:"):
            return True
        at, status = rec.nav_status
        return at >= mark and status == 403

    def _refusal_since(self, rec: _Owner, mark: float) -> str:
        """Why a main-frame navigation of this owner was refused after ``mark``: by its route handler, or by the
        proxy for a host the navigation (or one of its redirect hops) asked for. "" when nothing was refused. Only
        agentx's own records count, never what a response says; callers ask only when the page ended badly."""
        for at, reason in reversed(rec.refusals):
            if at >= mark:
                return reason
        if self._proxy is not None:
            return _matching_refusal(list(self._proxy.refusals), list(rec.nav_hosts), mark)
        return ""

    def _settle(self, rec: _Owner, page, mark: float) -> None:
        """Let what an action started finish: a page load it began (a key press or a script can start one that
        Playwright does not wait for), then the load event and a short quiet spell on the network."""
        try:
            page.wait_for_timeout(100)                  # a navigation the action started has asked by now
            deadline = time.monotonic() + SETTLE_LOAD_SEC
            while rec.loading() and time.monotonic() < deadline and not page.is_closed():
                page.wait_for_timeout(50)
        except Exception:
            pass
        try:
            page.wait_for_load_state("load", timeout=ELEMENT_TIMEOUT_MS)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_IDLE_MS)
        except Exception:
            pass

    def _snap(self, owner: str, error: str = "") -> Snapshot:
        rec = self._owner(owner)
        page = self._page(owner)
        rec.dirty = True
        deadline = time.monotonic() + SETTLE_LOAD_SEC
        try:                                            # a page load under way (the user pressed Enter): read it
            while rec.loading() and time.monotonic() < deadline and not page.is_closed():
                page.wait_for_timeout(50)
            page.wait_for_load_state("domcontentloaded", timeout=ELEMENT_TIMEOUT_MS)
        except Exception:
            pass
        try:
            data = page.evaluate(_SNAPSHOT_JS, [MAX_ELEMENTS, NAME_MAX])
        except Exception as failure:                    # a page navigating away mid-read: try once more
            try:
                page.wait_for_load_state("domcontentloaded", timeout=ELEMENT_TIMEOUT_MS)
                data = page.evaluate(_SNAPSHOT_JS, [MAX_ELEMENTS, NAME_MAX])
            except Exception:
                data = {"elements": [], "total": 0, "text": ""}
                error = error or f"ERROR: the page could not be read: {_first_line(failure)}"
        text = data.get("text") or ""
        if len(text) > WEB_MAX_CHARS:
            text = text[:WEB_MAX_CHARS] + f"\n[... the page's text was cut at {WEB_MAX_CHARS:,} characters]"
        elements = [_element_line(item) for item in data.get("elements") or []]
        hidden = int(data.get("total") or 0) - len(elements)
        if hidden > 0:
            elements.append(f"({hidden} more interactive elements not listed)")
        rec.url = page.url
        try:
            rec.title = page.title()
        except Exception:
            pass
        return Snapshot(url=page.url, title=rec.title, text=text, elements=elements, error=error)

    def _target(self, page, target: str):
        """A locator for a ref (``e12``), else a CSS selector that matches, else visible text."""
        target = (target or "").strip()
        missing = f"ERROR: nothing on the page matches '{target}'; read the page again for current refs"
        if not target:
            raise ValueError(missing)
        if re.fullmatch(r"e\d+", target):
            found = page.locator(f'[data-agentx-ref="{target}"]')
            if found.count():
                return found.first
            raise ValueError(missing)
        try:
            found = page.locator(f"css={target}")
            if found.count():
                return found.first
        except Exception:                               # not a CSS selector
            pass
        found = page.get_by_text(target, exact=False)
        try:
            if found.count():
                return found.first
        except Exception:
            pass
        raise ValueError(missing)

    def _navigate(self, owner: str, url: str, go: Callable) -> str:
        """Run a navigation (``go(page)``) and return "" or the ERROR line that says why it did not end well."""
        rec = self._owner(owner)
        page = self._page(owner)
        mark = time.monotonic()
        try:
            response = go(page)
        except self._Timeout:
            return f"ERROR: the page did not finish loading within {NAV_TIMEOUT_MS // 1000} seconds"
        except self._Error as error:
            reason = self._refusal_since(rec, mark)
            if reason:
                return f"ERROR: agentx refused to open {url}: {reason}"
            return f"ERROR: could not open {url}: {_first_line(error)}"
        ended_badly = (response is not None and response.status == 403) or page.url.startswith("chrome-error:")
        reason = self._refusal_since(rec, mark) if ended_badly else ""
        if reason:
            return f"ERROR: agentx refused to open {url}: {reason}"
        return ""

    def _after_action(self, owner: str, mark: float) -> Snapshot:
        rec = self._owner(owner)
        page = self._page(owner)
        self._settle(rec, page, mark)
        page = self._page(owner)
        reason = self._refusal_since(rec, mark) if self._ended_badly(rec, page, mark) else ""
        return self._snap(owner, f"ERROR: agentx refused to open a page: {reason}" if reason else "")

    # -- what the agent calls -------------------------------------------------------------------------------------

    def open(self, owner: str, url: str) -> Snapshot:
        reason = url_refusal(url)
        if reason:
            raise ValueError(reason)

        def work():
            mark = time.monotonic()
            error = self._navigate(owner, url, lambda page: page.goto(url, wait_until="load"))
            self._settle(self._owner(owner), self._page(owner), mark)
            return self._snap(owner, error)
        return self._call(self._agent(owner, work), timeout=LOAD_CALL_TIMEOUT_SEC)

    def click(self, owner: str, target: str) -> Snapshot:
        def work():
            page = self._page(owner)
            found = self._target(page, target)
            mark = time.monotonic()
            try:
                found.click()
            except self._Timeout:
                raise ValueError(f"ERROR: could not click '{target}': it did not become clickable within "
                                 f"{ELEMENT_TIMEOUT_MS // 1000} seconds") from None
            except self._Error as error:
                raise ValueError(f"ERROR: could not click '{target}': {_first_line(error)}") from None
            return self._after_action(owner, mark)
        return self._call(self._agent(owner, work))

    def type(self, owner: str, target: str, text: str, submit: bool = False) -> Snapshot:
        def work():
            page = self._page(owner)
            found = self._target(page, target)
            mark = time.monotonic()
            try:
                try:
                    found.fill(text)
                except self._Error:
                    if found.evaluate("el => el.tagName") == "SELECT":
                        try:
                            found.select_option(label=text)
                        except self._Error:
                            found.select_option(value=text)
                    else:
                        found.click()
                        page.keyboard.type(text)
                if submit:
                    found.press("Enter")
            except self._Error as error:
                raise ValueError(f"ERROR: could not type into '{target}': {_first_line(error)}") from None
            return self._after_action(owner, mark)
        return self._call(self._agent(owner, work))

    def press(self, owner: str, key: str) -> Snapshot:
        def work():
            page = self._page(owner)
            mark = time.monotonic()
            try:
                page.keyboard.press(key)
            except self._Error as error:
                raise ValueError(f"ERROR: could not press {key!r}: {_first_line(error)}") from None
            return self._after_action(owner, mark)
        return self._call(self._agent(owner, work))

    def back(self, owner: str) -> Snapshot:
        def work():
            page = self._page(owner)
            mark = time.monotonic()
            error = self._navigate(owner, page.url, lambda p: p.go_back(wait_until="load"))
            self._settle(self._owner(owner), self._page(owner), mark)
            return self._snap(owner, error)
        return self._call(self._agent(owner, work), timeout=LOAD_CALL_TIMEOUT_SEC)

    def snapshot(self, owner: str) -> Snapshot:
        return self._call(lambda: self._snap(owner))

    def screenshot(self, owner: str, full_page: bool = False) -> bytes:
        """A PNG of the owner's page (the viewport, or the whole page)."""
        return self._call(lambda: self._page(owner).screenshot(type="png", full_page=bool(full_page)))

    def console(self, owner: str) -> List[str]:
        """The newest console lines, page errors, dialogs and refusals of the owner's page (at most 50)."""
        rec = self._owners.get(owner)
        if rec is None:
            return []
        with rec.lock:
            return list(rec.console)

    def close_owner(self, owner: str) -> None:
        """Close the owner's context and page (a conversation that ended)."""
        self._held.discard(owner)
        if owner in self._owners:
            self._call(self._drop, owner)

    # -- what the window calls ------------------------------------------------------------------------------------

    def frame(self, owner: str) -> Tuple[int, Optional[bytes]]:
        """``(counter, JPEG of the viewport)``; the counter rises when the picture changed.

        A new picture is taken when an action or navigation ran since the last one, or else at most every
        ``FRAME_CHECK_SEC``; it counts as changed when its bytes differ. A window's poll never waits long: while the
        worker is busy (an agent's page load), the last picture is returned and at most one new one is queued per
        owner; one that is not ready within ``FRAME_CALL_TIMEOUT_SEC``, or fails, gives the last picture too. Never
        starts the browser."""
        rec = self._owners.get(owner)
        if rec is None or not rec.pages:
            return (rec.frame_no, rec.frame_jpeg) if rec is not None else (0, None)
        last = (rec.frame_no, rec.frame_jpeg)
        if rec.frame_jpeg is not None and not rec.dirty and time.monotonic() - rec.frame_at < FRAME_CHECK_SEC:
            return last

        def work():
            if not rec.pages:
                return rec.frame_no, rec.frame_jpeg
            now = time.monotonic()
            if rec.dirty or rec.frame_jpeg is None or now - rec.frame_at >= FRAME_CHECK_SEC:
                rec.dirty = False
                rec.frame_at = now
                page = rec.pages[-1]
                jpeg = page.screenshot(type="jpeg", quality=FRAME_QUALITY)
                digest = hashlib.sha1(jpeg).digest()
                if digest != rec.frame_hash:
                    rec.frame_hash, rec.frame_jpeg = digest, jpeg
                    rec.frame_no += 1
                rec.url = page.url
                try:
                    rec.title = page.title()
                except Exception:
                    pass
            return rec.frame_no, rec.frame_jpeg
        with rec.lock:
            pending = rec.capture
            if pending is None or pending.done():
                pending = rec.capture = self._submit(work)
            elif rec.frame_jpeg is not None:            # one is queued or being taken already
                return last
        try:
            return pending.result(timeout=FRAME_CALL_TIMEOUT_SEC)
        except Exception:                               # not ready in time (the worker is busy), or it failed
            return rec.frame_no, rec.frame_jpeg

    def user_event(self, owner: str, event: dict) -> None:
        """The user's own action on the page (they took it over, or used the address bar); not gated.

        ``{"type": "click", "x", "y"}`` (viewport pixels), ``wheel`` (``dy``, optional ``x``/``y``), ``key`` (a
        Playwright key name), ``text``, ``navigate`` (``url``, refused like ``open``: ``ValueError`` with the reason),
        ``back``, ``reload``."""
        if not isinstance(event, dict):
            raise ValueError("ERROR: the event is not an object")
        kind = str(event.get("type") or "")
        for name, limit in (("url", 8 * 1024), ("key", 64), ("text", TEXT_MAX)):
            value = event.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"ERROR: the event's {name} is not text")
            if value is not None and len(value) > limit:
                raise ValueError(f"ERROR: the event's {name} is longer than {limit} characters")
        if kind == "navigate":
            url = str(event.get("url") or "").strip()
            reason = url_refusal(url)
            if reason:
                raise ValueError(reason)
        if kind not in ("click", "wheel", "key", "text", "navigate", "back", "reload"):
            raise ValueError(f"ERROR: unknown browser event {kind!r}")

        def point(name: str, limit: int, default: Optional[float] = None) -> float:
            value = event.get(name, default)
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"ERROR: the event's {name} is not a number") from None
            return max(0.0, min(number, float(limit - 1)))

        def work():
            rec = self._owner(owner)
            page = self._page(owner)
            rec.dirty = True
            try:
                if kind == "click":
                    page.mouse.click(point("x", self.viewport[0]), point("y", self.viewport[1]))
                    page.wait_for_timeout(100)          # lets a page load the click started announce itself
                elif kind == "wheel":
                    if "x" in event and "y" in event:
                        page.mouse.move(point("x", self.viewport[0]), point("y", self.viewport[1]))
                    try:
                        dy = float(event.get("dy") or 0)
                    except (TypeError, ValueError):
                        raise ValueError("ERROR: the event's dy is not a number") from None
                    page.mouse.wheel(0, max(-10_000.0, min(dy, 10_000.0)))
                elif kind == "key":
                    page.keyboard.press(str(event.get("key") or ""))
                    page.wait_for_timeout(100)
                elif kind == "text":
                    page.keyboard.type(str(event.get("text") or ""))
                else:
                    if kind == "navigate":
                        url = str(event.get("url") or "").strip()
                        error = self._navigate(owner, url, lambda p: p.goto(url, wait_until="load"))
                    elif kind == "back":
                        error = self._navigate(owner, page.url, lambda p: p.go_back(wait_until="load"))
                    else:
                        error = self._navigate(owner, page.url, lambda p: p.reload(wait_until="load"))
                    if error:
                        raise ValueError(error)
            except self._Error as error:
                raise ValueError(f"ERROR: {_first_line(error)}") from None
            rec.url = page.url
            rec.dirty = True
        loads = kind in ("navigate", "back", "reload")
        self._call(work, timeout=LOAD_CALL_TIMEOUT_SEC if loads else ACTION_TIMEOUT_SEC)

    def take(self, owner: str) -> None:
        """The user takes the owner's page over: the agent's acting tools wait until ``give``."""
        self._held.add(owner)

    def give(self, owner: str) -> None:
        self._held.discard(owner)

    def held_by_user(self, owner: str) -> bool:
        return owner in self._held

    def state(self, owner: str) -> dict:
        """What the window shows about the owner's page; never starts the browser."""
        rec = self._owners.get(owner)
        if rec is None:
            return {"open": False, "url": "", "title": "", "user": self.held_by_user(owner), "frame": 0}
        return {"open": bool(rec.pages), "url": rec.url, "title": rec.title, "user": self.held_by_user(owner),
                "frame": rec.frame_no}

    def close(self) -> None:
        """Close every page, Chromium, Playwright, the proxy and the worker thread. A later call starts afresh."""
        with self._lock:                                # held while joining: a call made meanwhile waits for a
            worker, work = self._thread, self._queue    # fresh start instead of racing the shutdown
            self._thread = None
            if worker is None:
                return
            work.put(None)
            if threading.current_thread() is not worker:
                worker.join(timeout=15)


_shared: Optional[BrowserHost] = None
_shared_lock = threading.Lock()


def shared_browser() -> BrowserHost:
    """The process-wide ``BrowserHost`` (one Chromium for every conversation), closed at exit."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = BrowserHost()
            atexit.register(_shared.close)
        return _shared
