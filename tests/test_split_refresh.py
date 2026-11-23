"""A browser refresh must reconnect to an opened folder, not lose its window."""
import pytest

from agentx_dev.cli.webui import WebFront


def test_opened_side_survives_refresh(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    parent = WebFront(cwd=tmp_path, home=tmp_path / "home", open_it=False)
    child = WebFront(cwd=tmp_path / "other", home=tmp_path / "home", open_it=False)
    parent_url = parent.start_server()
    child.start_server()
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except sync_api.Error as error:
                pytest.skip(f"Chromium unavailable: {error}")
            page = browser.new_page()
            page.goto(parent_url)
            # Exercise the folder picker without starting a model process.
            page.route("**/api/pane", lambda route: route.fulfill(json={
                "ok": True, "url": child.launch_address(), "title": "Other folder"}))
            page.locator("#folder").click()
            page.get_by_text("Open a folder beside…", exact=True).click()
            page.locator("#overlayBox input").fill("other")
            page.locator("#overlayBox").get_by_role("button", name="Open beside", exact=True).click()
            frame = page.frame_locator("#besideFrame")
            frame.locator("#input").wait_for()
            child_frame = next(f for f in page.frames if f != page.main_frame)
            child_frame.wait_for_function("!location.search.includes('c=')")
            device = child_frame.evaluate("localStorage.getItem('agentx-device')")
            child.bus.emit("user", text="Work before refresh")
            frame.locator("#column").get_by_text("Work before refresh", exact=True).wait_for()
            saved = page.evaluate("sessionStorage.getItem('agentx-beside')")
            assert child.token not in saved and "?c=" not in saved and "t=" not in saved
            page.reload()
            page.wait_for_function("() => document.querySelector('.app').classList.contains('split')", timeout=5000)
            frame.locator("#input").wait_for()
            child_frame = next(f for f in page.frames if f != page.main_frame)
            child_frame.wait_for_function("location.hash.includes('t=')")
            assert child_frame.url.startswith(child.address().split('?')[0])
            assert child_frame.evaluate("localStorage.getItem('agentx-device')") == device
            frame.locator("#column").get_by_text("Work before refresh", exact=True).wait_for()
            child.bus.emit("user", text="Work continues after refresh")
            frame.locator("#column").get_by_text("Work continues after refresh", exact=True).wait_for()
            # Closing a pane is intentional: it must not return on the next refresh.
            page.locator("#besideClose").click()
            page.reload()
            assert not page.locator(".app").evaluate("el => el.classList.contains('split')")
            browser.close()
    finally:
        parent.close()
        child.close()
