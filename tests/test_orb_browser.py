"""Real-browser checks of the orb's looks: each state draws something of its own (large and small), with no console
errors, and the app window picks "team" while helpers run and "working" for a single agent's step.

python -m pytest -q tests/test_orb_browser.py (skipped without Playwright and its browser)."""
import json
import os
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[1] / "agentx_dev/resources/webui"
STATES = ["thinking", "working", "team", "success", "idle", "stuck"]

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
:root { --brand: #b8ff3e; } body { background: #161616; margin: 0; display: flex; flex-wrap: wrap; gap: 40px; padding: 40px; }
</style></head><body><script src="/orb.js"></script></body></html>"""


def _browser(sync_api, p):
    try:
        return p.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
    except Exception as error:                          # playwright installed but no browser downloaded
        pytest.skip(f"no browser for playwright: {error}")


def test_each_state_draws_its_own_look(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(sync_api, p)
        try:
            page = browser.new_page(color_scheme="dark", viewport={"width": 1400, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

            def serve(route):
                path = route.request.url.split("localhost:8765", 1)[1].split("?", 1)[0]
                if path == "/orbs.html":
                    route.fulfill(content_type="text/html", body=PAGE)
                else:
                    route.fulfill(path=str(ASSETS / path.lstrip("/")))
            page.route("http://localhost:8765/**", serve)
            page.goto("http://localhost:8765/orbs.html")
            # each state at each size, stepped for 2.5 s; the share of the drawing that differs between two looks
            looks = page.evaluate("""(states) => {
              const grey = {}, out = {drawn: {}, diff: {}};
              for (const size of [220, 32, 16]) for (const state of states) {
                const c = document.createElement("canvas");
                c.className = "orb"; c.dataset.size = String(size);
                document.body.append(c);
                const orb = AgentxOrb.create(c, () => state, {manual: true, teamOf: () => ({running: 3, finished: 2})});
                for (let i = 0; i < 150; i++) AgentxOrb.step(orb, 1 / 60);
                const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
                const g = new Float32Array(d.length / 4);
                let sum = 0;
                for (let i = 0; i < g.length; i++) { g[i] = d[i * 4 + 1] * d[i * 4 + 3] / 255; sum += g[i]; }
                grey[size + ":" + state] = [g, sum];
                out.drawn[size + ":" + state] = sum;
              }
              for (const size of [220, 32, 16]) states.forEach((a, n) => states.slice(n + 1).forEach((b) => {
                const [ga, sa] = grey[size + ":" + a], [gb, sb] = grey[size + ":" + b];
                let diff = 0;
                for (let i = 0; i < ga.length; i++) diff += Math.abs(ga[i] - gb[i]);
                out.diff[size + ":" + a + ":" + b] = diff / (sa + sb);
              }));
              return out;
            }""", STATES)
            if os.environ.get("ORB_SHOT"):
                page.screenshot(path=os.environ["ORB_SHOT"], full_page=True)
            for key, drawn in looks["drawn"].items():
                assert drawn > 0, key                                          # it drew something
            for key, diff in looks["diff"].items():
                assert diff > 0.1, (key, diff)                               # and each look is its own
            assert not errors, errors
        finally:
            browser.close()


def _team_rows(running, done=0):
    return [{"id": f"deployment-{i}", "helper": f"helper-{i}", "label": f"part {i}",
             "state": "running" if i < running else "done" if i < running + done else "waiting",
             "model": "fixture-model", "activity": "", "stats": "", "action": "", "path": "", "summary": "",
             "inFlight": False} for i in range(running + done + 1)]


@pytest.mark.parametrize("status, expected", [
    ({"type": "status", "working": True, "label": "Team", "team": {"rows": _team_rows(2, 1), "done": 1, "total": 4}},
     "team"),
    ({"type": "status", "working": True, "label": "Reading files"}, "working"),
    ({"type": "status", "working": True, "label": "Thinking"}, "thinking"),
])
def test_the_window_shows_the_team_look_while_helpers_run(status, expected):
    sync_api = pytest.importorskip("playwright.sync_api")
    events = [{"type": "user", "text": "do the job"}, status]
    body = "".join(f"id: {n}\ndata: {json.dumps(e)}\n\n" for n, e in enumerate(events, 1))
    errors = []
    with sync_api.sync_playwright() as p:
        browser = _browser(sync_api, p)
        try:
            page = browser.new_page(color_scheme="dark", viewport={"width": 1400, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))

            def serve(route):
                path = route.request.url.split("localhost:8765", 1)[1].split("?", 1)[0]
                if path == "/events":
                    route.fulfill(content_type="text/event-stream", body=body)
                elif path.startswith("/api/"):
                    route.fulfill(json=[] if path == "/api/sessions" else {"folderName": "Fixture"})
                else:
                    route.fulfill(path=str(ASSETS / ("index.html" if path == "/" else path.lstrip("/"))))
            page.route("http://localhost:8765/**", serve)
            page.goto("http://localhost:8765/?t=fixture")
            page.locator(f'#stage[data-state="{expected}"]').wait_for(state="attached", timeout=5000)
            page.wait_for_timeout(600)
            assert page.locator("#stage").get_attribute("data-state") == expected
            if os.environ.get("ORB_SHOT"):
                page.screenshot(path=os.environ["ORB_SHOT"].replace(".png", f"-{expected}.png"))
            assert not errors, errors
        finally:
            browser.close()
