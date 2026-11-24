"""Real-browser regression for Recent as a tree, and for opening another conversation while one works.
python -m pytest -q tests/test_conversation_lanes_browser.py (skipped without Playwright and its browser)."""
import json
import os
from pathlib import Path

import pytest

SESSIONS = [
    {"id": "aaaa1111", "title": "Security audit", "updated": "2026-11-23T10:00", "current": True,
     "lane": "", "working": True, "asking": False},
    {"id": "bbbb2222", "title": "Opened while it worked", "updated": "2026-11-23T10:05", "current": False,
     "lane": "l1", "working": False, "asking": True, "from": "aaaa1111"},
    {"id": "cccc3333", "title": "An older conversation", "updated": "2026-11-22T09:00", "current": False},
]


def test_recent_is_a_tree_and_a_click_while_working_opens_a_lane(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    assets = Path(__file__).parents[1] / "agentx_dev/resources/webui"
    events = [{"type": "user", "text": "audit the repo"},
              {"type": "status", "working": True, "label": "Thinking"}]
    body = "".join(f"id: {n}\ndata: {json.dumps(e)}\n\n" for n, e in enumerate(events, 1))
    posts = []
    errors = []
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
        except Exception as error:
            pytest.skip(f"no browser for playwright: {error}")
        try:
            page = browser.new_page(color_scheme="dark", viewport={"width": 1280, "height": 800})
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                path = request.url.split("localhost:8765", 1)[1].split("?", 1)[0]
                if path == "/events":
                    route.fulfill(content_type="text/event-stream", body=body)
                elif path == "/api/sessions":
                    route.fulfill(json=SESSIONS)
                elif path == "/api/lanes":
                    posts.append((json.loads(request.post_data or "{}"), request.headers.get("x-agentx-lane")))
                    route.fulfill(json={"ok": True, "lane": "l2"})
                elif path == "/api/send":
                    posts.append((json.loads(request.post_data or "{}"), "send"))
                    route.fulfill(json={"result": "queued"})
                elif path == "/api/state":
                    route.fulfill(json={"folderName": "AgentX", "folder": "C:/x/AgentX",
                                        "lane": request.headers.get("x-agentx-lane") or ""})
                elif path.startswith("/api/"):
                    route.fulfill(json={})
                else:
                    route.fulfill(path=str(assets / ("index.html" if path == "/" else path.lstrip("/"))))
            page.route("http://localhost:8765/**", serve)
            page.goto("http://localhost:8765/?t=fixture")
            page.locator(".tree-folder").wait_for(state="visible")
            page.wait_for_timeout(500)
            assert "AgentX" in page.locator(".tree-folder").inner_text()
            top = page.locator("#sessions > ul.tree > li > .session-row .session-title")
            assert top.all_inner_texts() == ["Security audit", "An older conversation"]
            nested = page.locator("#sessions > ul.tree > li > ul.tree .session-title")
            assert nested.all_inner_texts() == ["Opened while it worked"]       # hangs from the one it came from
            assert page.locator(".session-run.asking").count() == 1             # waits for you behind this one
            # the L: a line down and an elbow across, drawn for each branch
            elbow = page.locator("#sessions ul.tree > li").first.evaluate(
                "el => getComputedStyle(el, '::after').borderTopStyle")
            assert elbow == "solid"
            page.screenshot(path=str(tmp_path / "tree.png"))
            if os.environ.get("TREE_SHOT"):
                page.screenshot(path=os.environ["TREE_SHOT"])
            # a click on a conversation that is not open, while this one works: it opens in a lane, not /resume
            page.locator(".session", has_text="An older conversation").click()
            page.wait_for_function("sessionStorage.getItem('agentx-lane') === 'l2'")
            assert posts[0][0] == {"session": "cccc3333"}
            assert not [p for p in posts if p[1] == "send"]
            # the folder row folds the list
            page.locator(".tree-folder").click()
            assert page.locator("#sessions ul.tree").count() == 0
            page.locator(".tree-folder").click()
            assert not errors
        finally:
            browser.close()
