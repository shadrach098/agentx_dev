"""Real-browser regression for the window's Tasks / Browser / Terminal panel, over the real server: a task started
through the manager shows with Stop and moves to Finished; a terminal runs a command typed in its input line; the
page agentx opened for the shown conversation appears as a live frame and can be taken over; remote control makes
the Terminal tab read-only. python -m pytest -q tests/test_tasks_panel_browser.py (skipped without Playwright and
its browser). TASKS_PANEL_SHOTS=<folder> also keeps the screenshots (1280 and 600 wide, light and dark)."""
import http.server
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from tests.test_one_conversation_per_device import LAPTOP, Host, answers, ready

PY = sys.executable.replace("\\", "/")


def script(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f'"{PY}" "{str(f).replace(chr(92), "/")}"'


class Window:
    """agentx --ui with a real server, and a real Chromium showing its window as this computer's first device."""

    def __init__(self, tmp_path, playwright, scheme="dark", width=1280, height=800):
        self.tmp_path = tmp_path
        self.h = Host(tmp_path, answers("main"), [])
        self.errors, self.posts = [], []
        self.closed = False
        try:
            self.chromium = playwright.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
        except Exception as error:                      # playwright installed but no browser downloaded
            self.h.close()
            pytest.skip(f"no browser for playwright: {error}")
        # bypass_csp: the page's own policy forbids eval, which the test's wait_for_function uses (the page does not)
        self.context = self.chromium.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                                 bypass_csp=True)
        self.context.add_init_script(f"try {{ localStorage.setItem('agentx-device', '{LAPTOP}'); }} catch (_) {{}}")
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", self._note)
        self.page.goto(f"http://127.0.0.1:{self.h.port}/?t={self.h.front.token}")
        self.main = self.h.front_of(LAPTOP)
        ready(self.main)
        self.page.locator("#tasksBtn").wait_for(state="visible")

    def _note(self, request):
        if request.method == "POST" and "/api/" in request.url:
            try:
                body = json.loads(request.post_data or "{}")
            except ValueError:
                body = None
            self.posts.append((request.url.split(f":{self.h.port}", 1)[1], body))

    def posted(self, path):
        return [body for where, body in self.posts if where == path]

    @property
    def tasks(self):
        return self.main.app.tasks

    def open_tab(self, name):
        if self.page.locator("#tasksPanel").is_hidden():
            self.page.locator("#tasksBtn").click()
        self.page.locator(f'#tasksPanel [role="tab"][data-tab="{name}"]').click()
        self.page.locator(f'#tasksPanel [role="tabpanel"][data-tab="{name}"]').wait_for(state="visible")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.chromium.close()
        finally:
            tasks = getattr(self.h.app, "tasks", None)
            self.h.close()
            if tasks is not None:
                tasks.stop_all()                        # leave nothing running, whatever the test did


@pytest.fixture
def window(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    made = []
    with sync_api.sync_playwright() as p:
        def make(**kw):
            folder = tmp_path / f"w{len(made)}"
            folder.mkdir()
            made.append(Window(folder, p, **kw))
            return made[-1]
        try:
            yield make
        finally:
            for w in made:
                w.close()


@pytest.fixture
def site():
    from tests.test_browser_host import _Pages
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Pages)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def chromium_problem():
    """Why agentx's own Chromium cannot run here ("" when it can); asked before the window's Playwright starts, as
    the sync API cannot be started twice on one thread."""
    from tests.test_browser_host import _launch_problem
    return _launch_problem()


def test_a_task_shows_with_stop_and_moves_to_finished(window, tmp_path):
    w = window()
    page = w.page
    page.locator("#detailsBtn").click()
    page.locator("#details").wait_for(state="visible")
    owner = w.main.conversation_id()
    task = w.tasks.start(script(tmp_path, "srv.py", "import time\nprint('hello-task', flush=True)\ntime.sleep(60)\n"),
                         owner=owner, name="dev server")
    page.wait_for_function("document.getElementById('tasksBadge').textContent.trim() === '1'", timeout=10_000)
    assert page.locator("#tasksBadge").is_visible()
    page.locator("#tasksBtn").click()
    page.locator("#tasksPanel").wait_for(state="visible")
    assert page.locator("#details").is_hidden()                     # opening the panel closes Details
    assert page.locator("#tasksBtn").get_attribute("aria-expanded") == "true"
    row = page.locator(".tp-group.running .tp-row", has_text="dev server")
    row.wait_for(state="visible", timeout=10_000)
    assert row.locator(".tp-stop").is_visible() and row.locator(".tp-stop").inner_text().strip() == "Stop"
    # a click opens its output, live
    row.locator(".tp-open").click()
    page.locator("#taskOut").wait_for(state="visible")
    page.wait_for_function("document.getElementById('taskOutText').textContent.includes('hello-task')",
                           timeout=10_000)
    page.locator("#taskBack").click()
    page.locator(".tp-group.running .tp-row .tp-stop").first.click()
    finished = page.locator(".tp-group.finished .tp-row", has_text="dev server")
    finished.wait_for(state="visible", timeout=10_000)
    assert "stopped" in finished.inner_text().lower()
    assert page.locator(".tp-group.running .tp-row").count() == 0
    assert w.posted("/api/tasks/stop") == [{"id": task.id}]
    page.wait_for_function("document.getElementById('tasksBadge').hidden", timeout=10_000)
    # Clear finished removes it; the empty state says what to do
    page.locator("#tasksClear").click()
    page.locator(".tp-empty", has_text="No background tasks").wait_for(state="visible", timeout=10_000)
    # the tab is kept
    assert page.evaluate("localStorage.getItem('agentx-tasks-tab')") == "tasks"
    # and Details closes the panel again
    page.locator("#detailsBtn").click()
    page.locator("#details").wait_for(state="visible")
    assert page.locator("#tasksPanel").is_hidden()
    assert not w.errors


def test_a_terminal_runs_what_is_typed(window):
    w = window()
    page = w.page
    w.open_tab("terminal")
    assert page.evaluate("localStorage.getItem('agentx-tasks-tab')") == "terminal"
    page.locator(".tp-empty", has_text="No terminals").wait_for(state="visible", timeout=10_000)
    page.locator("#termNew").click()
    box = page.locator("#termInput")
    page.wait_for_function("!document.getElementById('termInput').disabled", timeout=15_000)
    box.fill("echo panel-ok")
    box.press("Enter")
    page.wait_for_function(r"/^panel-ok\s*$/m.test(document.getElementById('termOut').textContent)",
                           timeout=20_000)
    assert box.input_value() == ""
    box.press("Enter")                                               # an empty line is sent as "", never null
    page.wait_for_function("document.getElementById('termInput').value === ''")
    page.wait_for_timeout(300)
    sent = w.posted("/api/terminals/input")
    term = sent[0]["id"]
    assert sent[:2] == [{"id": term, "text": "echo panel-ok"}, {"id": term, "text": ""}]
    box.press("Control+c")                                           # nothing selected: Ctrl+C interrupts
    page.wait_for_timeout(300)
    assert w.posted("/api/terminals/interrupt") == [{"id": term}]
    assert page.locator(".term-tab", has_text="Terminal 1").count() == 1
    assert not w.errors


def test_the_browser_tab_shows_the_page_and_can_be_taken_over(chromium_problem, window, site):
    if chromium_problem:
        pytest.skip(chromium_problem)
    from agentx_dev.cli.browser import BrowserHost
    browser = BrowserHost()
    try:
        w = window()
        page = w.page
        w.main.app.browser = browser
        browser.open(w.main.conversation_id(), site + "/next")
        w.open_tab("browser")
        page.wait_for_function("(() => { const i = document.getElementById('bwFrame');"
                               " return i && !i.hidden && i.src.startsWith('blob:') && i.naturalWidth > 0; })()",
                               timeout=15_000)
        assert page.locator("#bwUrl").input_value() == site + "/next"
        # watching only: a click on the frame is not sent
        page.locator("#bwFrame").click()
        page.wait_for_timeout(300)
        assert not w.posted("/api/browser/input")
        page.locator("#bwTake").click()
        page.locator("#bwGive").wait_for(state="visible", timeout=10_000)
        assert browser.held_by_user(w.main.conversation_id())
        box = page.locator("#bwFrame").bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(400)
        clicks = [b["event"] for b in w.posted("/api/browser/input") if b["event"]["type"] == "click"]
        assert len(clicks) == 1
        assert abs(clicks[0]["x"] - 640) <= 4 and abs(clicks[0]["y"] - 400) <= 4      # scaled to 1280 x 800
        page.keyboard.type("ab")
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        events = [b["event"] for b in w.posted("/api/browser/input") if b["event"]["type"] in ("text", "key")]
        assert "".join(e.get("text", "") for e in events if e["type"] == "text") == "ab"
        assert {"type": "key", "key": "Enter"} in events
        page.locator("#bwGive").click()
        page.locator("#bwTake").wait_for(state="visible", timeout=10_000)
        assert not browser.held_by_user(w.main.conversation_id())
        # the address bar works without taking over
        page.locator("#bwUrl").fill(site + "/other")
        page.locator("#bwUrl").press("Enter")
        page.wait_for_function(f"document.getElementById('bwTitle').textContent.includes('Other')", timeout=15_000)
        assert not w.errors
    finally:
        browser.close()


def test_remote_control_makes_the_terminal_read_only(window):
    w = window()
    page = w.page
    w.h.front.remote = True
    w.open_tab("terminal")
    note = page.locator("#termNote")
    note.wait_for(state="visible", timeout=10_000)
    assert "remote control" in note.inner_text()
    assert page.locator("#termNew").is_hidden()
    w.open_tab("browser")
    page.wait_for_timeout(1500)
    assert page.locator("#bwTake").is_hidden()
    assert not w.errors


def test_the_browser_tab_without_a_page_says_what_to_do(window):
    w = window()
    w.open_tab("browser")
    w.page.locator(".tp-empty", has_text="agentx has not opened a page yet").wait_for(state="visible",
                                                                                      timeout=10_000)
    assert w.page.locator("#bwUrl").is_visible()
    assert not w.errors


SHOTS = os.environ.get("TASKS_PANEL_SHOTS")


@pytest.mark.skipif(not SHOTS, reason="set TASKS_PANEL_SHOTS=<folder> to take the screenshots")
def test_screenshots(chromium_problem, window, tmp_path, site):
    from agentx_dev.cli.browser import BrowserHost
    out = Path(SHOTS)
    out.mkdir(parents=True, exist_ok=True)
    browser = None if chromium_problem else BrowserHost()
    try:
        for scheme in ("light", "dark"):
            for width in (1280, 600):
                w = window(scheme=scheme, width=width, height=800)
                page = w.page
                owner = w.main.conversation_id()
                w.tasks.start(script(tmp_path, "srv.py", "import time\nprint('Listening on http://localhost:5173',"
                                                         " flush=True)\ntime.sleep(60)\n"),
                              owner=owner, name="npm run dev -- --port 5173 --host with a long name that wraps")
                w.tasks.start(script(tmp_path, "watch.py", "import time\nprint('watching', flush=True)\n"
                                                           "time.sleep(60)\n"), owner=owner, name="pytest-watch")
                done = w.tasks.start(script(tmp_path, "ok.py", "print('built')\n"), owner=owner, name="build")
                failed = w.tasks.start(script(tmp_path, "bad.py", "raise SystemExit(2)\n"), owner=owner,
                                       name="lint")
                for t in (done, failed):
                    for _ in range(100):
                        if w.tasks.get(t.id).state != "running":
                            break
                        page.wait_for_timeout(50)
                w.open_tab("tasks")
                page.locator(".tp-group.finished .tp-row").nth(1).wait_for(state="visible", timeout=10_000)
                page.wait_for_timeout(300)
                page.screenshot(path=str(out / f"tasks-{width}-{scheme}.png"))
                page.locator(".tp-group.running .tp-open").first.click()
                page.wait_for_function("document.getElementById('taskOutText').textContent.includes('Listening')",
                                       timeout=10_000)
                page.screenshot(path=str(out / f"task-output-{width}-{scheme}.png"))
                page.locator("#taskBack").click()
                w.open_tab("terminal")
                page.locator("#termNew").click()
                page.wait_for_function("!document.getElementById('termInput').disabled", timeout=15_000)
                page.locator("#termInput").fill("echo panel-ok")
                page.locator("#termInput").press("Enter")
                page.wait_for_function(r"/^panel-ok\s*$/m.test(document.getElementById('termOut').textContent)",
                                       timeout=20_000)
                page.screenshot(path=str(out / f"terminal-{width}-{scheme}.png"))
                w.open_tab("browser")
                page.wait_for_timeout(500)
                page.screenshot(path=str(out / f"browser-empty-{width}-{scheme}.png"))
                if browser is not None:
                    w.main.app.browser = browser
                    browser.open(owner, site + "/next")
                    page.wait_for_function("(() => { const i = document.getElementById('bwFrame');"
                                           " return i && !i.hidden && i.naturalWidth > 0; })()", timeout=15_000)
                    page.locator("#bwTake").click()
                    page.locator("#bwGive").wait_for(state="visible", timeout=10_000)
                    page.wait_for_timeout(400)
                    page.screenshot(path=str(out / f"browser-{width}-{scheme}.png"))
                    page.locator("#bwGive").click()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not w.errors
                w.close()
    finally:
        if browser is not None:
            browser.close()
