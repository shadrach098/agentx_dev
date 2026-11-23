"""Real-browser regression: a question that waits for you while a team works must be readable without hunting.
The orb used to stay large at the top and the team panel grew above the message box, leaving the conversation (and
the question in it) a strip a few lines high that only scrolled when the pointer was inside it.

python -m pytest -q tests/test_waiting_card_room_browser.py (skipped without Playwright and its browser)."""
import json
import os
from pathlib import Path

import pytest

QUESTION = ("The audit needs a place for its reports. Should the team write them under "
            "C:/Users/you/security-audit-skill/AgentX/run-2, or into an ignored folder inside this repository? "
            "Say which, or give another folder.")


def events():
    rows = [{"id": f"deployment-{i}", "helper": f"helper-{i}", "label": label, "state": "running" if i == 0 else "waiting",
             "model": "fixture-model", "activity": "Read 3 files" if i == 0 else "", "stats": "", "action": "",
             "path": "", "summary": "", "inFlight": False}
            for i, label in enumerate(["initialize", "hunt approval", "hunt web", "hunt execution",
                                       "verify and critic", "reconcile validate"])]
    out = [{"type": "user", "text": "run the security audit with a team"}]
    out += [{"type": "print", "ansi": f"Step {n}: looked at part {n} of the project."} for n in range(1, 40)]
    out += [{"type": "status", "working": True, "label": "Team", "team": {"rows": rows, "done": 0, "total": 6}},
            {"type": "ask", "id": 7, "kind": "text", "prompt": "Team question: " + QUESTION}]
    return out


def test_a_waiting_question_has_room_and_the_wheel_works_everywhere(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    assets = Path(__file__).parents[1] / "agentx_dev/resources/webui"
    body = "".join(f"id: {n}\ndata: {json.dumps(e)}\n\n" for n, e in enumerate(events(), 1))
    errors = []
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
        except Exception as error:                      # playwright installed but no browser downloaded
            pytest.skip(f"no browser for playwright: {error}")
        try:
            page = browser.new_page(color_scheme="dark", viewport={"width": 1600, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                path = route.request.url.split("localhost:8765", 1)[1].split("?", 1)[0]
                if path == "/events":
                    route.fulfill(content_type="text/event-stream", body=body)
                elif path.startswith("/api/"):
                    route.fulfill(json=[] if path == "/api/sessions" else {"folderName": "Fixture"})
                else:
                    route.fulfill(path=str(assets / ("index.html" if path == "/" else path.lstrip("/"))))
            page.route("http://localhost:8765/**", serve)
            page.goto("http://localhost:8765/?t=fixture")
            title = page.locator(".card.question .card-title")
            title.wait_for(state="visible")
            page.wait_for_timeout(1200)                  # the stage folds and the card scrolls into view
            log = page.locator("#log").bounding_box()
            box = title.bounding_box()
            assert box["y"] >= log["y"] - 1 and box["y"] + box["height"] <= log["y"] + log["height"] + 1, (box, log)
            assert page.locator("#stage.compact").count() == 1                       # the orb made room
            assert page.locator("#stage").bounding_box()["height"] < 120
            team = page.locator("#team").bounding_box()
            assert team["height"] <= 0.34 * 1000 + 2                                   # a third of the window at most
            assert log["height"] > 300                                                 # the conversation has room
            page.screenshot(path=str(tmp_path / "waiting-card.png"))
            if os.environ.get("WAITING_CARD_SHOT"):
                page.screenshot(path=os.environ["WAITING_CARD_SHOT"])
            # the wheel over the orb scrolls the conversation
            page.locator("#log").evaluate("el => el.scrollTop = el.scrollHeight")
            before = page.locator("#log").evaluate("el => el.scrollTop")
            stage = page.locator("#stage").bounding_box()
            page.mouse.move(stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2)
            page.mouse.wheel(0, -400)
            page.wait_for_timeout(200)
            assert page.locator("#log").evaluate("el => el.scrollTop") < before
            # the team panel scrolls itself first, and only then hands the wheel on
            panel = page.locator("#team")
            assert panel.evaluate("el => el.scrollHeight > el.clientHeight")
            page.mouse.move(team["x"] + 40, team["y"] + team["height"] / 2)
            log_before = page.locator("#log").evaluate("el => el.scrollTop")
            page.mouse.wheel(0, 120)
            page.wait_for_timeout(200)
            assert panel.evaluate("el => el.scrollTop") > 0
            assert page.locator("#log").evaluate("el => el.scrollTop") == log_before
            assert not errors
        finally:
            browser.close()
