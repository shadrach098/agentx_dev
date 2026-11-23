"""Optional real-browser regression: python -m pytest -q tests/test_team_panel_browser.py."""
import json
import os
from pathlib import Path

import pytest


def test_team_roster_browser(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    assets = Path(__file__).parents[1] / "agentx_dev/resources/webui"
    states = ["running", "running", "waiting", "done", "failed", "skipped", "interrupted", "unknown"]
    rows = [{"id": f"deployment-{i}", "helper": f"helper-{i}",
             "label": label, "state": state, "model": "fixture-model",
             "activity": "1 tool call" if i < 2 else "", "stats": "",
             "action": ["read_path", "edit_file"][i] if i < 2 else "",
             "path": ["agentx_dev/cli/team_panel.py", "app.js"][i] if i < 2 else "",
             "summary": "64 lines" if i == 0 else "", "inFlight": i == 1}
            for i, (state, label) in enumerate(zip(states, [
                "Backend lifecycle", "Responsive roster", "Browser regression",
                "Runtime review", "Provider failure", "Superseded deployment",
                "Stopped with Esc", "Ended without word"]))]
    count = len(rows)
    errors = []
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
        except Exception as error:                      # playwright installed but no browser downloaded
            pytest.skip(f"no browser for playwright: {error}")
        try:
            page = browser.new_page(color_scheme="dark")
            page.on("pageerror", lambda error: errors.append(str(error)))
            def serve(route):
                path = route.request.url.split("localhost:8765", 1)[1].split("?", 1)[0]
                if path == "/events":
                    event = {"type": "status", "working": True, "label": "Team", "team": {"rows": rows, "done": 3, "total": count}}
                    route.fulfill(content_type="text/event-stream", body="id: 1\ndata: " + json.dumps(event) + "\n\n")
                elif path.startswith("/api/"):
                    route.fulfill(json=[] if path == "/api/sessions" else {"folderName": "Team panel fixture"})
                else:
                    target = assets / ("index.html" if path == "/" else path.lstrip("/"))
                    route.fulfill(path=str(target))
            page.route("http://localhost:8765/**", serve)
            artifact_dir = Path(os.environ.get("TEAM_PANEL_ARTIFACTS") or tmp_path)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            for name, width, height in [("desktop", 1440, 1000), ("mobile", 390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                page.goto("http://localhost:8765/?t=fixture")
                page.locator(".team-row").nth(count - 1).wait_for(state="visible")
                assert page.locator(".team-row").count() == count
                assert page.locator('.team-row.is-running').count() == 2
                acts = page.locator(".act")
                assert acts.count() == 2
                assert "Last · Read" in acts.nth(0).inner_text() and "team_panel.py" in acts.nth(0).inner_text()
                assert "Now · Editing" in acts.nth(1).inner_text()           # in flight: what it is doing now
                assert page.locator(".team-state").all_text_contents() == states
                assert page.locator(".team-row.is-interrupted").count() == 1
                assert page.locator(".team-row.is-unknown").count() == 1
                assert page.locator(".is-running .team-marker").first.evaluate("el => getComputedStyle(el).animationName") != "none"
                assert acts.first.evaluate("el => getComputedStyle(el, '::before').borderLeftStyle") == "solid"
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                for row in page.locator(".team-row").all():
                    box = row.bounding_box()
                    assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
                page.screenshot(path=str(artifact_dir / f"team-panel-{name}.png"), full_page=True)
                # Reconnect/full replay must replace, not append, deployment rows.
                page.reload()
                page.locator(".team-row").nth(count - 1).wait_for(state="visible")
                assert page.locator(".team-row").count() == count
            page.emulate_media(reduced_motion="reduce")
            assert page.locator(".is-running .team-marker").first.evaluate("el => getComputedStyle(el).animationName") == "none"
            assert not errors
        finally:
            browser.close()
