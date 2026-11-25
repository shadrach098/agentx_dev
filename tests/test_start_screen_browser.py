"""Real-browser check of the start screen: agentx --ui started in the home folder shows the folders trusted before
(newest use first) instead of a conversation; Chat here anyway brings the usual welcome, for this run (a reload keeps
it); choosing a folder says agentx opened it in a new window. python -m pytest -q tests/test_start_screen_browser.py
(skipped without Playwright and its browser). START_SCREEN_SHOTS=<folder> also keeps screenshots (1280 wide, light
and dark)."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agentx_dev.cli.app import ChatApp
from agentx_dev.cli.sessions import sessions_dir
from agentx_dev.cli.webui import WebFront
from tests.test_one_conversation_per_device import LAPTOP, answers, ready


class Window:
    """agentx --ui in a home folder, with a real server, and a real Chromium showing its window."""

    def __init__(self, tmp_path, playwright, scheme="dark", width=1280, height=800):
        self.home = tmp_path / "Home"
        self.home.mkdir()
        self.projects = [tmp_path / "Alpha", tmp_path / "Beta"]
        now = time.time()
        for folder, ago in zip(self.projects, (3 * 86400, 7200)):       # Alpha 3 days ago, Beta 2 hours ago
            folder.mkdir()
            used = sessions_dir(self.home, folder)
            used.mkdir(parents=True)
            os.utime(used, (now - ago, now - ago))
        trust = self.home / ".agentx" / "trusted.json"
        trust.write_text(json.dumps({"folders": [os.path.normcase(str(f)) for f in self.projects]}), encoding="utf-8")
        self.front = WebFront(cwd=self.home, home=self.home, open_it=False)
        self.front.start_screen = True
        self.front.start_server()
        self.port = self.front._server.server_address[1]
        self.app = ChatApp(cwd=self.home, home=self.home, console=self.front.console, read_line=self.front.read,
                           read_short=self.front.read_short, select=self.front.select, env={},
                           model_factory=lambda name, env: answers("hello"))
        self.front.wire(self.app)
        self.thread = threading.Thread(target=self._start, daemon=True)
        self.thread.start()
        self.errors = []
        self.closed = False
        try:
            self.chromium = playwright.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
        except Exception as error:                      # playwright installed but no browser downloaded
            self._close_host()
            pytest.skip(f"no browser for playwright: {error}")
        self.context = self.chromium.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                                 bypass_csp=True)
        self.context.add_init_script(f"try {{ localStorage.setItem('agentx-device', '{LAPTOP}'); }} catch (_) {{}}")
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.port}/?t={self.front.token}")

    def _start(self):
        if not self.front.wait_for_start():             # as agentx --ui: nothing starts before the choice
            return
        try:
            self.app.start()
        except EOFError:
            pass

    def _close_host(self):
        self.front.close()
        self.thread.join(5)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.chromium.close()
        finally:
            self._close_host()


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


def _shot(page, name):
    folder = os.environ.get("START_SCREEN_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / name))


def test_the_start_screen_lists_the_trusted_projects_and_chat_here_shows_the_welcome(window):
    w = window()
    page = w.page
    screen = page.locator(".start-screen")
    screen.wait_for(state="visible", timeout=10_000)
    assert screen.locator("h1").inner_text().strip() == "Choose a folder to work in"
    assert screen.locator(".start-why").inner_text().strip() == f"{w.home} is your home folder, not a project."
    rows = screen.locator(".start-row")
    assert rows.count() == 2
    assert [rows.nth(n).locator(".start-name").inner_text() for n in range(2)] == ["Beta", "Alpha"]
    assert rows.nth(0).locator(".start-ago").inner_text() == "2 hours ago"
    assert rows.nth(1).get_attribute("title") == str(w.projects[0].resolve())
    assert page.locator(".welcome").count() == 0
    assert page.locator("#composer").is_hidden()            # no conversation here by accident
    assert w.thread.is_alive() and not w.front._pending     # the app waits: no trust question, no message box
    # the rows are reached with the keyboard, and show where the focus is
    page.wait_for_function("document.activeElement && document.activeElement.classList.contains('start-row')",
                           timeout=5_000)
    assert page.evaluate("document.activeElement.title") == str(w.projects[1].resolve())     # the newest first
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.title") == str(w.projects[0].resolve())
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline != "none"                                # the focus shows
    page.reload()                                           # until a choice is made, a reload shows it again
    page.locator(".start-screen .start-row").first.wait_for(state="visible", timeout=10_000)
    assert page.locator(".welcome").count() == 0
    page.get_by_role("button", name="Chat here anyway").click()
    page.locator(".welcome h1").wait_for(state="visible", timeout=10_000)
    ready(w.front)                                          # agentx started here as usual
    assert page.locator(".start-screen").count() == 0
    assert page.locator("#composer").is_visible()
    assert w.front.start_view() == {"show": False}
    page.reload()                                           # the choice holds for this run
    page.locator(".welcome h1").wait_for(state="visible", timeout=10_000)
    assert page.locator(".start-screen").count() == 0
    assert not w.errors, w.errors


def test_choosing_a_folder_says_agentx_opened_it(window):
    w = window()
    page = w.page
    asked = []

    def opened(raw):                                        # the process start, stubbed
        asked.append(raw)
        return {"ok": True, "folder": str(raw)}
    w.front.open_folder = opened
    page.locator(".start-row").first.wait_for(state="visible", timeout=10_000)
    page.locator(".start-row").first.click()
    box = page.locator("#overlayBox")
    box.wait_for(state="visible", timeout=10_000)
    beta = str(w.projects[1].resolve())
    assert asked == [beta]
    assert box.locator("p").inner_text().strip() == f"agentx opened {beta} in a new window. You can close this one."
    assert w.front._hand_over is not None and w.front._hand_over[1] == beta
    assert not w.errors, w.errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_start_screen_in_both_themes(window, scheme):
    w = window(scheme=scheme)
    page = w.page
    page.locator(".start-row").first.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(400)                              # the fade-in
    for selector in (".start-screen h1", ".start-row .start-name", ".start-row .start-path"):
        color = page.eval_on_selector(selector, "n => getComputedStyle(n).color")
        assert color != page.eval_on_selector("body", "n => getComputedStyle(n).backgroundColor")
    _shot(page, f"start-1280-{scheme}.png")
    page.set_viewport_size({"width": 380, "height": 760})  # narrow: nothing runs off the side
    page.wait_for_timeout(200)
    assert page.evaluate("document.querySelector('.start-screen').scrollWidth <= "
                         "document.querySelector('.start-screen').clientWidth + 1")
    _shot(page, f"start-380-{scheme}.png")
    assert not w.errors, w.errors
