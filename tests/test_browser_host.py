"""The private browser agentx drives (agentx_dev/cli/browser.py), against a small local http.server.

The tests that need a real Chromium are skipped when Playwright or its Chromium is missing (checked once by
launching it); the address checks, the take-over flags, the snapshot format and the missing-Playwright message are
tested everywhere.
"""
import functools
import http.server
import socket
import sys
import threading
import time
import urllib.parse

import pytest

from agentx_dev.cli import browser
from agentx_dev.cli.browser import NOT_INSTALLED, BrowserHost, BrowserUnavailable, Snapshot, url_refusal
from agentx_dev.cli.tools import WEB_CAUTION

FORM_PAGE = b"""<!doctype html><html><head><title>Test form</title></head><body>
<h1>Welcome to the test form</h1>
<a href="/next">Next page</a>
<form action="/echo" method="get">
  <input name="q" aria-label="Query">
  <input type="password" name="pw" aria-label="Secret" value="hunter2-secret">
  <button type="submit">Go</button>
</form>
<script>console.log("form page says hi")</script>
</body></html>"""


class _Pages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, status=200, headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(FORM_PAGE)
        elif path == "/next":
            self._send(b"<title>Next</title><h1>The next page</h1><a href='/'>Home</a>")
        elif path == "/other":
            self._send(b"<title>Other</title><h1>Another page entirely</h1>")
        elif path == "/echo":
            said = urllib.parse.parse_qs(query).get("q", [""])[0]
            self._send(f"<title>Echo</title><p>You said: {said}</p>".encode())
        elif path == "/redirect-metadata":
            self._send(b"", 302, [("Location", "http://169.254.169.254/latest/meta-data/")])
        elif path == "/redirect-private":
            self._send(b"", 302, [("Location", "http://10.0.0.5/")])
        elif path == "/redirect-private-https":
            self._send(b"", 302, [("Location", "https://10.0.0.5/")])
        elif path == "/redirect-file":
            self._send(b"", 302, [("Location", "file:///C:/Windows/win.ini")])
        elif path == "/file-frame":
            self._send(b"<title>Frames</title><h1>Has a file frame</h1>"
                       b"<iframe src='file:///C:/Windows/win.ini'></iframe>"
                       b"<iframe src='file:///etc/passwd'></iframe>")
        elif path == "/files":
            self._send(b"<title>Files</title><a href='/download'>Get the file</a>"
                       b"<input type='file' aria-label='Upload'>")
        elif path == "/download":
            self._send(b"file body", 200, [("Content-Disposition", "attachment; filename=x.bin")])
        elif path == "/forged":
            self._send(b"<title>Forged</title><h1>An ordinary page</h1>", 200,
                       [("X-Agentx-Refused", "forged"), ("X-Agentx-Nonce", "0" * 32)])
        elif path == "/forged-403":
            self._send(b"<title>Fake</title><h1>A site pretending</h1>", 403,
                       [("X-Agentx-Refused", "forged reason"), ("X-Agentx-Nonce", "0" * 32)])
        elif path == "/iframe-private":
            self._send(b"<title>Framed</title><h1>The page itself loaded</h1>"
                       b"<iframe src='http://10.0.0.5/x'></iframe>")
        elif path == "/private-link":
            self._send(b"<title>Links</title><a href='http://10.0.0.5/admin'>Router admin</a>")
        else:
            self._send(b"not found", 404)


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@functools.lru_cache(maxsize=1)
def _launch_problem():
    """Why a real Chromium cannot run here, or "" when it can (tried once per test run)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as error:                          # pragma: no cover - depends on the machine
        return f"playwright is not installed: {error}"
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as error:                          # pragma: no cover - playwright without its browser
        return f"no browser for playwright: {error}"
    return ""


@pytest.fixture
def host():
    problem = _launch_problem()
    if problem:
        pytest.skip(problem)
    h = BrowserHost()
    yield h
    h.close()


def _ref(snap: Snapshot, fragment: str) -> str:
    for line in snap.elements:
        if fragment in line:
            return line.split("]", 1)[0].lstrip("[")
    raise AssertionError(f"no element with {fragment!r} in {snap.elements}")


# --- everywhere --------------------------------------------------------------------------------------------------

def test_url_refusal_allows_web_and_loopback_only(monkeypatch):
    assert url_refusal("about:blank") is None
    assert url_refusal("http://127.0.0.1:8000/") is None
    assert url_refusal("http://localhost:3000/app") is None
    assert url_refusal("http://[::1]:8000/") is None
    assert url_refusal("http://[::ffff:127.0.0.1]/") is None
    assert url_refusal("http://93.184.215.14/") is None                       # a public literal needs no DNS

    assert url_refusal("file:///C:/x") == "only http and https pages can be opened"
    for url in ("data:text/html,hi", "javascript:alert(1)", "chrome://settings", "blob:http://a/b",
                "ws://127.0.0.1/", "ftp://example.com/", "view-source:http://127.0.0.1/"):
        assert url_refusal(url) == "only http and https pages can be opened", url
    assert url_refusal("http://10.0.0.5/") == "10.0.0.5 is a private address"
    assert url_refusal("http://169.254.169.254/") == "169.254.169.254 is a private address"
    assert url_refusal("http://192.168.1.1/") == "192.168.1.1 is a private address"
    assert url_refusal("http://[fe80::1]/") == "fe80::1 is a private address"
    assert url_refusal("http://0.0.0.0:8000/") == "0.0.0.0 is not a public address"
    assert url_refusal("http://[::ffff:10.0.0.5]/") is not None
    # IPv4 carried inside IPv6 (NAT64, 6to4) is judged as the IPv4 address.
    assert url_refusal("http://[64:ff9b::a00:5]/") == "64:ff9b::a00:5 is a private address"
    assert url_refusal("http://[64:ff9b::a9fe:a9fe]/") == "64:ff9b::a9fe:a9fe is a private address"
    assert url_refusal("http://[2002:a00:5::]/") == "2002:a00:5:: is a private address"
    assert url_refusal("http://[64:ff9b::5db8:d70e]/") is None                # 93.184.215.14
    # Chromium reads a backslash as a slash: the host here is 10.0.0.5, not example.com.
    assert url_refusal("http://10.0.0.5\\@example.com/") == "10.0.0.5 is a private address"
    assert url_refusal("http:///nohost") is not None

    # A name is judged by every address it resolves to.
    def fake_lookup(host, *args, **kwargs):
        if host == "mixed.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]
        if host == "loop.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        raise socket.gaierror(11001, "host not found")
    monkeypatch.setattr(browser.socket, "getaddrinfo", fake_lookup)
    browser._forget_lookups()
    try:
        assert url_refusal("https://mixed.test/") == "mixed.test is a private address (10.1.2.3)"
        assert url_refusal("http://loop.test:5173/") is None
        assert url_refusal("https://nowhere.test/") == "nowhere.test could not be found"
    finally:
        browser._forget_lookups()


def test_public_name_is_allowed_when_there_is_a_network():
    try:
        socket.getaddrinfo("example.com", 443)
    except OSError:
        pytest.skip("no network")
    assert url_refusal("https://example.com/") is None


def test_user_takes_over():
    h = BrowserHost()
    try:
        assert not h.held_by_user("a")
        h.take("a")
        assert h.held_by_user("a") and not h.held_by_user("b")
        assert h.state("a")["user"] is True
        h.give("a")
        assert not h.held_by_user("a")
        # Nothing here started a browser.
        assert h.state("a") == {"open": False, "url": "", "title": "", "user": False, "frame": 0}
        assert h.frame("a") == (0, None)
    finally:
        h.close()


def test_snapshot_render_format():
    snap = Snapshot(url="http://127.0.0.1/", title="Home", text="Hello",
                    elements=['[e1] link "Docs"', '[e2] button "Save"'])
    assert snap.render() == (WEB_CAUTION + 'Page: Home\nURL: http://127.0.0.1/\n\nHello\n\n'
                             'Interactive elements:\n[e1] link "Docs"\n[e2] button "Save"')
    empty = Snapshot(url="about:blank", title="", text="", elements=[])
    assert empty.render().startswith(WEB_CAUTION)
    assert empty.render().endswith("Interactive elements:\n(none)")
    failed = Snapshot(url="about:blank", title="", text="", elements=[], error="ERROR: boom")
    assert "ERROR: boom" in failed.render()


def test_missing_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    h = BrowserHost()
    try:
        with pytest.raises(BrowserUnavailable) as caught:
            h.open("a", "http://127.0.0.1:9/")
        assert str(caught.value) == NOT_INSTALLED
        with pytest.raises(BrowserUnavailable):
            h.snapshot("a")
    finally:
        h.close()
    assert NOT_INSTALLED == ('The browser needs Playwright: pip install "agentx-dev[browser]" '
                             'then python -m playwright install chromium')


def test_shared_browser_is_one_per_process():
    assert browser.shared_browser() is browser.shared_browser()


# --- with a real Chromium ----------------------------------------------------------------------------------------

def test_open_read_and_click(host, site):
    snap = host.open("a", site + "/")
    assert snap.title == "Test form"
    assert "Welcome to the test form" in snap.text
    assert snap.elements[0].startswith("[e1] ")
    assert '[e1] link "Next page"' in snap.elements
    secret = _ref(snap, 'textbox "Secret"')
    assert any(line.startswith(f"[{secret}]") and 'value="(hidden)"' in line for line in snap.elements)
    rendered = snap.render()
    assert rendered.startswith(WEB_CAUTION)
    assert "hunter2-secret" not in rendered
    assert f"URL: {site}/" in rendered and "Interactive elements:" in rendered

    after = host.click("a", _ref(snap, 'link "Next page"'))
    assert after.url == site + "/next"
    assert "The next page" in after.text

    back = host.back("a")
    assert back.url == site + "/"
    # By visible text, and by a CSS selector.
    assert host.click("a", "Next page").url == site + "/next"
    assert host.click("a", "a[href='/']").url == site + "/"
    with pytest.raises(ValueError) as caught:
        host.click("a", "e999")
    assert str(caught.value) == "ERROR: nothing on the page matches 'e999'; read the page again for current refs"
    with pytest.raises(ValueError):
        host.click("a", "No such words anywhere on the page")
    assert any("form page says hi" in line for line in host.console("a"))


def test_type_and_submit(host, site):
    snap = host.open("a", site + "/")
    result = host.type("a", _ref(snap, 'textbox "Query"'), "hello", submit=True)
    assert "You said: hello" in result.text
    assert result.url.startswith(site + "/echo")
    again = host.open("a", site + "/")
    host.type("a", _ref(again, 'textbox "Query"'), "typed")
    assert 'value="typed"' in " ".join(host.snapshot("a").elements)
    pressed = host.press("a", "Enter")
    assert "You said: typed" in pressed.text


def test_private_and_file_targets_are_refused(host, site):
    with pytest.raises(ValueError) as caught:
        host.open("a", "file:///C:/x")
    assert str(caught.value) == "only http and https pages can be opened"
    with pytest.raises(ValueError) as caught:
        host.open("a", "http://10.0.0.5/")
    assert str(caught.value) == "10.0.0.5 is a private address"

    # A redirect to the cloud metadata address ends on agentx's refusal, not on that page.
    snap = host.open("a", site + "/redirect-metadata")
    assert "169.254.169.254 is a private address" in snap.render()
    assert "meta-data" not in snap.text.replace("169.254.169.254/latest/meta-data/", "")
    snap = host.open("a", site + "/redirect-private")
    assert "10.0.0.5 is a private address" in snap.render()
    snap = host.open("a", site + "/redirect-private-https")      # the proxy refuses the tunnel
    assert snap.error == ("ERROR: agentx refused to open " + site
                          + "/redirect-private-https: 10.0.0.5 is a private address")

    # A redirect to file: and a file: frame never load the file.
    snap = host.open("a", site + "/redirect-file")
    assert not snap.url.startswith("file:")
    assert snap.error
    snap = host.open("a", site + "/file-frame")
    assert "Has a file frame" in snap.text
    frames = host._call(lambda: [f.url for f in host._page("a").frames])
    assert not any(url.startswith("file:") for url in frames), frames

    # A link to a private address: the click is refused and says why.
    snap = host.open("a", site + "/private-link")
    after = host.click("a", _ref(snap, 'link "Router admin"'))
    assert "10.0.0.5 is a private address" in after.render()
    assert not after.url.startswith("http://10.0.0.5")

    # The user's own address bar goes through the same check.
    with pytest.raises(ValueError) as caught:
        host.user_event("a", {"type": "navigate", "url": "http://192.168.0.1/"})
    assert str(caught.value) == "192.168.0.1 is a private address"


def test_each_owner_has_its_own_page(host, site):
    a = host.open("a", site + "/")
    b = host.open("b", site + "/other")
    assert "Another page entirely" in b.text
    again = host.snapshot("a")
    assert again.url == site + "/"
    assert "Welcome to the test form" in again.text
    # Refs are per page: a's link ref still works after b took a snapshot.
    assert host.click("a", _ref(a, 'link "Next page"')).url == site + "/next"
    assert host.snapshot("b").url == site + "/other"
    assert host.state("a")["url"] == site + "/next" and host.state("b")["url"] == site + "/other"
    host.close_owner("b")
    assert host.state("b")["open"] is False
    assert host.snapshot("a").url == site + "/next"


def test_frame_changes_after_navigation(host, site):
    assert host.frame("a") == (0, None)
    host.open("a", site + "/")
    first, jpeg = host.frame("a")
    assert first > 0 and jpeg[:2] == b"\xff\xd8"
    assert host.frame("a")[0] == first                       # nothing changed, the counter stays
    host.open("a", site + "/other")
    second, jpeg2 = host.frame("a")
    assert second > first and jpeg2 != jpeg
    state = host.state("a")
    assert state["open"] is True and state["url"] == site + "/other" and state["title"] == "Other"
    assert state["frame"] == second
    png = host.screenshot("a")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_user_events_drive_the_page(host, site):
    host.user_event("a", {"type": "navigate", "url": site + "/"})
    assert host.state("a")["url"] == site + "/"
    host.user_event("a", {"type": "navigate", "url": site + "/next"})
    host.user_event("a", {"type": "back"})
    assert host.snapshot("a").url == site + "/"
    host.user_event("a", {"type": "reload"})
    host.user_event("a", {"type": "wheel", "dy": 200})
    # Click into the query box by its position, type and press Enter.
    box = host._call(lambda: host._page("a").locator("input[name=q]").bounding_box())
    host.user_event("a", {"type": "click", "x": box["x"] + 5, "y": box["y"] + 5})
    host.user_event("a", {"type": "text", "text": "by hand"})
    host.user_event("a", {"type": "key", "key": "Enter"})
    assert "You said: by hand" in host.snapshot("a").text
    with pytest.raises(ValueError):
        host.user_event("a", {"type": "teleport"})


def test_downloads_and_file_choosers_are_refused(host, site):
    snap = host.open("a", site + "/files")
    started = time.monotonic()
    after = host.click("a", _ref(snap, 'link "Get the file"'))
    assert after.url == site + "/files"
    host.click("a", _ref(snap, 'file-input "Upload"'))
    host.snapshot("a")
    assert time.monotonic() - started < 15                  # nothing waits on a page load that never comes
    notes = " ".join(host.console("a"))
    assert "[filechooser]" in notes


def test_a_site_cannot_forge_a_refusal(host, site):
    snap = host.open("a", site + "/forged")
    assert snap.error == ""
    assert "An ordinary page" in snap.text and "forged" not in snap.render().replace("/forged", "")
    host.user_event("a", {"type": "navigate", "url": site + "/forged"})      # the address bar does not raise


# --- the guard proxy on its own (no Chromium) ---------------------------------------------------------------------

class _Upstream:
    """A raw TCP server on 127.0.0.1: answers each connection with ``reply`` (or, with ``reply=None``, accepts and
    never sends nor closes). Records what it received."""

    def __init__(self, reply=None):
        self.reply = reply
        self.received = []
        self.conns = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        data = b""
        try:
            conn.settimeout(5)
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        self.received.append(data)
        if self.reply is not None:
            try:
                conn.sendall(self.reply)
            finally:
                conn.close()

    def close(self):
        self.sock.close()
        for conn in self.conns:
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def proxy():
    p = browser._GuardProxy()
    yield p
    p.close()


def _ask(proxy, raw: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=10) as conn:
        conn.sendall(raw)
        data = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        return data


def test_proxy_reads_requests_strictly(proxy):
    for raw in (b"garbage\r\n\r\n", b"GET /\r\n\r\n", b"GET / HTTP/1.1\r\nHost: x\r\n\r\n",
                b"CONNECT 127.0.0.1:70000 HTTP/1.1\r\n\r\n", b"CONNECT 127.0.0.1:0 HTTP/1.1\r\n\r\n",
                b"CONNECT 127.0.0.1 HTTP/1.1\r\n\r\n", b"GET http://127.0.0.1:99999/ HTTP/1.1\r\n\r\n",
                b"GET ftp://127.0.0.1/ HTTP/1.1\r\n\r\n"):
        assert _ask(proxy, raw).startswith(b"HTTP/1.1 400 "), raw
    for raw in (b"CONNECT 127.0.0.1:\xb2 HTTP/1.1\r\n\r\n", b"GET http://[x/ HTTP/1.1\r\n\r\n"):
        assert _ask(proxy, raw).startswith(b"HTTP/1.1 400 "), raw          # not a traceback in the proxy thread
    # A refusal: a bare 403 that names nothing; the reason lives only in the proxy's own record.
    refused = _ask(proxy, b"CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n")
    assert refused.startswith(b"HTTP/1.1 403 ") and b"Blocked by agentx" in refused
    assert b"10.0.0.5" not in refused and b"private" not in refused and b"x-agentx" not in refused.lower()
    assert proxy.refusals[-1][1:] == ("10.0.0.5", "10.0.0.5 is a private address")
    refused = _ask(proxy, b"GET http://169.254.169.254/latest/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n")
    assert refused.startswith(b"HTTP/1.1 403 ") and b"169.254" not in refused
    assert proxy.refusals[-1][1:] == ("169.254.169.254", "169.254.169.254 is a private address")
    nul = _ask(proxy, b"CONNECT a\x00b:443 HTTP/1.1\r\n\r\n")       # getaddrinfo's ValueError is a refusal
    assert nul.startswith(b"HTTP/1.1 403 ")


def test_proxy_forwards_absolute_form_and_drops_forged_headers(proxy):
    up = _Upstream(b"HTTP/1.1 200 OK\r\nX-Agentx-Refused: forged\r\nx-agentx-nonce: guess\r\n"
                   b"Content-Length: 5\r\n\r\nhello")
    try:
        answer = _ask(proxy, f"GET http://127.0.0.1:{up.port}//a//b?x=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                             f"Proxy-Connection: keep-alive\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 200 OK") and answer.endswith(b"hello")
        assert b"agentx" not in answer.lower()
        sent = up.received[0]
        assert sent.startswith(b"GET //a//b?x=1 HTTP/1.1\r\n")
        assert b"Connection: close" in sent and b"Proxy-Connection" not in sent
    finally:
        up.close()


def test_proxy_tunnels_connect(proxy):
    up = _Upstream(b"tunnelled bytes")
    try:
        answer = _ask(proxy, f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\nping\r\n\r\n".encode())
        assert answer.startswith(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        assert answer.endswith(b"tunnelled bytes")
        assert up.received[0] == b"ping\r\n\r\n"
    finally:
        up.close()


def _wait_until(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_proxy_closes_silent_and_half_open_tunnels(proxy):
    proxy.idle_timeout = 0.5
    up = _Upstream(reply=None)                             # accepts, never answers, never closes
    try:
        before = threading.active_count()
        # The browser side goes away: the tunnel and its upstream socket go with it.
        conn = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        conn.sendall(f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert conn.recv(100).startswith(b"HTTP/1.1 200")
        assert _wait_until(lambda: proxy.open_connections() == 2)
        conn.close()
        assert _wait_until(lambda: proxy.open_connections() == 0)
        # Both sides stay silent: the idle timeout ends it, and the browser side is closed too.
        conn = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        conn.sendall(f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert conn.recv(100).startswith(b"HTTP/1.1 200")
        assert _wait_until(lambda: proxy.open_connections() == 0)
        assert conn.recv(100) == b""
        conn.close()
        # The proxy's threads for both are gone (the upstream's own handler threads may remain).
        assert _wait_until(lambda: threading.active_count() <= before + 2)
    finally:
        up.close()


def test_launch_failures_say_what_happened(monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")

    class FakePlaywright:
        def __init__(self, message):
            self.message = message
            self.chromium = self

        def start(self):
            return self

        def launch(self, **kwargs):
            raise RuntimeError(self.message)

        def stop(self):
            pass

    cases = [("BrowserType.launch: Executable doesn't exist at C:/x/chrome.exe", NOT_INSTALLED),
             ("BrowserType.launch: Target crashed\nmore detail",
              "The browser could not start: BrowserType.launch: Target crashed")]
    for message, expected in cases:
        monkeypatch.setattr(sync_api, "sync_playwright", lambda message=message: FakePlaywright(message))
        h = BrowserHost()
        try:
            with pytest.raises(BrowserUnavailable) as caught:
                h.snapshot("a")
            assert str(caught.value) == expected
        finally:
            h.close()

    def no_proxy():
        raise OSError("no port free")
    monkeypatch.setattr(browser, "_GuardProxy", no_proxy)
    h = BrowserHost()
    try:
        with pytest.raises(BrowserUnavailable) as caught:
            h.snapshot("a")
        assert str(caught.value) == "The browser could not start: no port free"
    finally:
        h.close()


def test_the_route_check_never_looks_names_up(monkeypatch):
    def no_lookup(*args, **kwargs):
        raise AssertionError("the route handler must not resolve names")
    monkeypatch.setattr(browser.socket, "getaddrinfo", no_lookup)
    browser._forget_lookups()
    assert browser._url_refusal("https://some-name.test/x", resolve=False) is None      # the proxy resolves it
    assert browser._url_refusal("http://10.0.0.5/", resolve=False) == "10.0.0.5 is a private address"
    assert browser._url_refusal("file:///C:/x", resolve=False) == "only http and https pages can be opened"


def test_proxy_close_stops_listening_and_ends_tunnels():
    p = browser._GuardProxy()
    up = _Upstream(reply=None)
    try:
        conn = socket.create_connection(("127.0.0.1", p.port), timeout=5)
        conn.sendall(f"CONNECT 127.0.0.1:{up.port} HTTP/1.1\r\n\r\n".encode())
        assert conn.recv(100).startswith(b"HTTP/1.1 200")
        p.close()
        assert conn.recv(100) == b""                       # the open tunnel ended
        conn.close()
        assert _wait_until(lambda: p.open_connections() == 0)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", p.port), timeout=2).close()
    finally:
        p.close()
        up.close()


def test_url_refusal_survives_a_nul_in_the_host():
    assert "could not be found" in url_refusal("http://a\x00b/")


def test_a_site_answering_403_with_agentx_headers_is_not_a_refusal(host, site):
    snap = host.open("a", site + "/forged-403")
    assert snap.error == ""
    assert "A site pretending" in snap.text and "forged reason" not in snap.render()


def test_page_scripts_learn_nothing_from_a_refusal(host, site, monkeypatch):
    """DNS rebinding stand-in: the page's own host starts resolving to a private address; the proxy refuses the
    page's same-origin fetch, and the page reads every header and the body of that answer."""
    real = browser._checked_addresses
    rebound = {"on": False}

    def fake(name, resolve=True):
        if rebound["on"] and resolve and threading.current_thread().name != "MainThread":
            raise browser._Refused(f"{name} is a private address (10.0.0.5)")
        return real(name, resolve=resolve)
    monkeypatch.setattr(browser, "_checked_addresses", fake)
    host.open("a", site + "/next")
    rebound["on"] = True
    seen = host._call(lambda: host._page("a").evaluate("""async () => {
        const r = await fetch('/next?again=1');
        return {status: r.status, headers: [...r.headers].map(h => h.join(': ')).join('\\n'), body: await r.text()};
    }"""))
    assert seen["status"] == 403
    leaked = (seen["headers"] + "\n" + seen["body"]).lower()
    for word in ("x-agentx", "nonce", "10.0.0.5", "private", "127.0.0.1"):
        assert word not in leaked, (word, seen)
    assert host._proxy.refusals                                  # agentx itself knows why


def test_only_agentx_records_decide_a_refusal():
    match = browser._matching_refusal
    navs = [(10.0, "site.test"), (11.0, "10.0.0.5")]
    assert match([], navs, 9.0) == ""
    assert match([(12.0, "other.test", "other.test is a private address")], navs, 9.0) == ""
    assert match([(12.0, "10.0.0.5", "10.0.0.5 is a private address")], navs, 9.0) == "10.0.0.5 is a private address"
    assert match([(8.0, "10.0.0.5", "old")], navs, 9.0) == ""                   # before this navigation began
    assert match([(12.0, "10.0.0.5", "late")], [(8.0, "10.0.0.5")], 9.0) == ""    # asked for by an earlier one


def test_proxy_strips_smuggled_and_malformed_headers(proxy):
    up = _Upstream(b"HTTP/1.1 200 OK\nX-Agentx-Refused: smuggled\r\n X-Agentx-Folded: x\r\n"
                   b"Bad Header: y\r\nnocolon\r\nContent-Length: 2\r\n\r\nok")
    try:
        answer = _ask(proxy, f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        head, _, body = answer.partition(b"\r\n\r\n")
        assert head.split(b"\r\n") == [b"HTTP/1.1 200 OK", b"Content-Length: 2", b"Connection: close",
                                       b"Proxy-Connection: close"]
        assert body == b"ok"
    finally:
        up.close()


def test_a_refused_frame_does_not_make_the_page_refused(host, site):
    snap = host.open("a", site + "/iframe-private")
    assert snap.error == ""
    assert "The page itself loaded" in snap.text
    assert any("[refused] http://10.0.0.5/x" in line for line in host.console("a"))
    host.user_event("a", {"type": "navigate", "url": site + "/iframe-private"})     # does not raise


def test_a_page_that_loaded_is_never_reported_refused(host, site):
    """A proxy refusal record for the very host and window of a load that answered 200 is ignored."""
    host.open("a", site + "/")
    later = time.monotonic() + 120                             # counts as "since the action began" for any mark
    host._proxy.refusals.append((later, "127.0.0.1", "127.0.0.1 is a private address (injected)"))
    try:
        snap = host.open("a", site + "/next")
        assert snap.error == "" and "injected" not in snap.render()
        after = host.click("a", _ref(snap, 'link "Home"'))
        assert after.error == "" and after.url == site + "/"
        host.user_event("a", {"type": "navigate", "url": site + "/next"})          # does not raise
    finally:
        host._proxy.refusals.clear()


def test_user_event_fields_must_be_text_and_short():
    h = BrowserHost()
    for event in ({"type": "text", "text": 5}, {"type": "text", "text": "y" * (64 * 1024 + 1)},
                  {"type": "key", "key": 3}, {"type": "navigate", "url": ["http://a/"]}, "click"):
        with pytest.raises(ValueError) as caught:
            h.user_event("o", event)
        assert str(caught.value).startswith("ERROR:"), event
    assert h._thread is None                            # refused before the worker was started


# -- final review fixes -------------------------------------------------------------------------------------------

def _busy_worker(h):
    """Hold ``h``'s worker on another conversation's slow call; returns the event that lets it go."""
    started, release = threading.Event(), threading.Event()
    h._submit(lambda: (started.set(), release.wait(20)))
    assert started.wait(5)
    return release


def test_a_call_that_timed_out_never_runs_later():
    """A click that "did not answer" because another conversation held the worker must not fire afterwards (the
    model would retry it: a form sent twice)."""
    h = BrowserHost()
    release = _busy_worker(h)
    ran = []
    try:
        with pytest.raises(ValueError) as caught:
            h._call(lambda: ran.append("late click"), timeout=0.3)
        assert "did not answer" in str(caught.value) and "may still" not in str(caught.value)
        release.set()
        assert h._submit(lambda: "next").result(timeout=5) == "next"   # the worker went past the cancelled call
        assert ran == []
    finally:
        release.set()
        h.close()


def test_a_call_still_running_at_its_timeout_says_it_may_still_finish():
    h = BrowserHost()
    release = threading.Event()
    try:
        with pytest.raises(ValueError) as caught:
            h._call(lambda: release.wait(20), timeout=0.3)
        assert "may still finish" in str(caught.value)
    finally:
        release.set()
        h.close()


def test_a_user_event_that_timed_out_never_runs_later(monkeypatch):
    h = BrowserHost()
    monkeypatch.setattr(browser, "ACTION_TIMEOUT_SEC", 0.3)
    release = _busy_worker(h)
    owned = []
    monkeypatch.setattr(h, "_owner", lambda owner: owned.append(owner))
    try:
        with pytest.raises(ValueError):
            h.user_event("a", {"type": "click", "x": 1, "y": 1})
        release.set()
        assert h._submit(lambda: "next").result(timeout=5) == "next"
        assert owned == []
    finally:
        release.set()
        h.close()


@pytest.mark.parametrize("act", [
    lambda h: h.open("a", "http://127.0.0.1:9/"),
    lambda h: h.click("a", "e1"),
    lambda h: h.type("a", "e1", "x"),
    lambda h: h.press("a", "Enter"),
    lambda h: h.back("a"),
])
def test_an_agent_action_queued_before_a_take_over_is_refused_when_it_runs(act):
    """The tools check the take-over before queueing; an action already queued behind a slow call is checked again
    on the worker, so it never lands on a page the user has taken since."""
    h = BrowserHost()
    release = _busy_worker(h)
    box = {}

    def agent():
        try:
            box["result"] = act(h)
        except Exception as error:                      # noqa: BLE001 - examined below
            box["error"] = error
    worker = threading.Thread(target=agent, daemon=True)
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while h._queue.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert h._queue.qsize() == 1
        h.take("a")
        release.set()
        worker.join(10)
        assert isinstance(box.get("error"), browser.HeldByUser), box
        assert str(box["error"]) == browser.HELD_BY_USER
        assert h._browser is None                        # refused before Chromium was even started
    finally:
        release.set()
        h.close()


def test_the_users_own_event_is_not_refused_by_their_take_over(monkeypatch):
    h = BrowserHost()
    h.take("a")
    seen = []

    class Page:
        mouse = type("M", (), {"click": staticmethod(lambda x, y: seen.append((x, y)))})()
        url = "http://127.0.0.1:1/"

        def wait_for_timeout(self, ms):
            pass
    rec = browser._Owner(context=None, pages=[Page()])
    monkeypatch.setattr(h, "_owner", lambda owner: rec)
    monkeypatch.setattr(h, "_page", lambda owner: rec.pages[-1])
    try:
        h.user_event("a", {"type": "click", "x": 3, "y": 4})
        assert seen == [(3.0, 4.0)]
    finally:
        h.close()


def test_proxy_answers_close_whatever_the_upstream_says(proxy):
    """The rest of a plain-HTTP connection is piped to its upstream: a site that answers keep-alive must not get
    Chromium to reuse that connection for a request to another host."""
    up = _Upstream(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nKeep-Alive: timeout=60\r\n"
                   b"Proxy-Connection: keep-alive\r\nContent-Length: 2\r\n\r\nok")
    try:
        answer = _ask(proxy, f"GET http://127.0.0.1:{up.port}/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        head, _, body = answer.partition(b"\r\n\r\n")
        assert head.split(b"\r\n") == [b"HTTP/1.1 200 OK", b"Content-Length: 2", b"Connection: close",
                                       b"Proxy-Connection: close"]
        assert body == b"ok"
    finally:
        up.close()


def test_proxy_keeps_the_upgrade_of_a_websocket_answer(proxy):
    up = _Upstream(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    try:
        answer = _ask(proxy, f"GET http://127.0.0.1:{up.port}/ws HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                             f"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n".encode())
        head = answer.partition(b"\r\n\r\n")[0].split(b"\r\n")
        assert head == [b"HTTP/1.1 101 Switching Protocols", b"Upgrade: websocket", b"Connection: Upgrade"]
    finally:
        up.close()
