"""Real-browser regression for the team job in the app window: one live block in the conversation, drawn like the
terminal's dark block, its rows updated in place (fixed height), a step that ends turning into its Done line, a big
team scrolling inside the block, and the final form coming back after a reload. The gray Team panel is gone.

python -m pytest -q tests/test_team_block_browser.py (skipped without Playwright and its browser)."""
import json
import os
from pathlib import Path

import pytest

ASSETS = Path(__file__).parents[1] / "agentx_dev/resources/webui"
LONG_PATH = "agentx_dev/resources/webui/some/very/deep/folder/with/a/long/name/" + "x" * 120 + ".py"


def row(i, state, **kw):
    out = {"id": f"deployment-{i + 1}", "helper": f"helper-{i}", "label": f"step {i}", "state": state,
           "model": "fixture-model", "fallback": "", "fallbackFrom": "", "fallbackReason": "", "termination": "",
           "inferred": False, "activity": "", "stats": "", "action": "", "path": "", "summary": "",
           "actionFailed": False, "inFlight": False, "toolUses": 0, "time": "0s", "actions": [],
           "mark": "", "word": "", "tone": "", "report": ""}
    out.update(kw)
    return out


def running(i, doing="read_path", path="a.py", tools=3, time="16s", **kw):
    return row(i, "running", action=doing, path=path, inFlight=True, toolUses=tools, time=time,
               activity=f"Read {tools} files", stats=f"{tools} tool uses · 12k tokens · {time}",
               actions=[{"name": "read_path", "subject": f"file-{n}.py", "summary": f"{n + 1} lines", "failed": False}
                        for n in range(tools)], **kw)


def done(i, report="Initial audience: Python developers building LLM agents who want one tool."):
    return row(i, "done", mark="✓", word="Done", tone="green", report=report, toolUses=4, time="16s",
               stats="4 tool uses · 58k tokens · 16s", activity="Read 4 files",
               actions=[{"name": "read_path", "subject": "notes.md", "summary": "80 lines", "failed": False}])


def status(job, rows, working=True):
    finished = sum(r["state"] in ("done", "failed", "skipped") for r in rows)
    return {"type": "status", "working": working, "label": "Team is working",
            "team": {"job": job, "rows": rows, "done": finished, "total": len(rows)}}


START = [{"type": "user", "text": "run the launch team"},
         {"type": "tool_call", "name": "start_team_job", "subject": "launch", "look": False},
         {"type": "team_plan", "steps": [{"agent": f"helper-{i}", "label": f"step {i}", "query": "q", "tools": None}
                                         for i in range(4)]}]


class Fixture:
    """A pretend agentx: each /events request takes the next batch of events (the page polls every 100 ms)."""

    def __init__(self, page):
        self.batches, self.history, self.next = [], None, 1
        page.route("http://localhost:8765/**", self.serve)

    def push(self, *events):
        self.batches.append(list(events))

    def serve(self, route):
        path = route.request.url.split("localhost:8765", 1)[1].split("?", 1)[0]
        if path == "/events":
            body = "retry: 100\n\n"
            for event in (self.batches.pop(0) if self.batches else []):
                body += f"id: {self.next}\ndata: {json.dumps(event)}\n\n"
                self.next += 1
            route.fulfill(content_type="text/event-stream", body=body)
        elif path == "/api/history":
            route.fulfill(json=self.history or {"seq": 0, "items": [], "pending": [], "status": {"working": False}})
        elif path.startswith("/api/"):
            route.fulfill(json=[] if path == "/api/sessions" else {"folderName": "Team block fixture"})
        else:
            route.fulfill(path=str(ASSETS / ("index.html" if path == "/" else path.lstrip("/"))))


def heights(page):
    return page.eval_on_selector_all(".team-block .tb-row", "rows => rows.map(r => r.getBoundingClientRect().height)")


@pytest.fixture
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            launched = p.chromium.launch(headless=os.environ.get("PW_HEADED") != "1")
        except Exception as error:                      # playwright installed but no browser downloaded
            pytest.skip(f"no browser for playwright: {error}")
        try:
            yield launched
        finally:
            launched.close()


def test_a_running_job_is_one_live_block_in_the_conversation(browser, tmp_path):
    shots = Path(os.environ.get("TEAM_BLOCK_ARTIFACTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(color_scheme="dark", viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fixture = Fixture(page)
    fixture.push(*START, status("job-1", [running(0), running(1, doing="grep", path=""), row(2, "waiting"),
                                          row(3, "waiting")]))
    page.goto("http://localhost:8765/?t=fixture")
    page.locator(".team-block .tb-row").nth(3).wait_for(state="visible")

    # one block, in the conversation, drawn as the dark terminal block; the gray panel is gone
    assert page.locator(".team-block").count() == 1
    assert page.locator("#log .team-block.term").count() == 1
    assert page.locator("#team").count() == 0 and page.locator(".team-row").count() == 0
    head = page.locator(".team-block .tb-head").inner_text()
    assert head == "Team · 2 running · 0 of 4 done", head
    lines = page.locator(".team-block .tb-line").all_inner_texts()
    assert lines[0].startswith("● helper-0 · step 0") and "Reading a.py · 3 tools · 16s" in lines[0], lines
    assert lines[2].startswith("○ helper-2 · step 2") and lines[2].rstrip().endswith("waiting"), lines
    before = heights(page)
    page.screenshot(path=str(shots / "team-block-running.png"))

    # the activity text changes (much longer): every row keeps its height, the block is updated in place
    first = page.locator(".team-block .tb-row").first.element_handle()
    fixture.push(status("job-1", [running(0, path=LONG_PATH, tools=18, time="1m 12s"),
                                  running(1, doing="grep", path="", tools=4), row(2, "waiting"), row(3, "waiting")]))
    page.wait_for_function("document.querySelector('.team-block .tb-line').textContent.includes('18 tools')")
    assert heights(page) == before
    assert page.evaluate("el => el === document.querySelector('.team-block .tb-row')", first)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    # a step ends: its row turns into the ended line with the start of its report, shown once
    fixture.push(status("job-1", [done(0), running(1, doing="grep", path="", tools=5), running(2), row(3, "waiting")]))
    page.wait_for_function("document.querySelector('.team-block .tb-line').textContent.includes('Done')")
    lines = page.locator(".team-block .tb-line").all_inner_texts()
    assert lines[0].startswith("✓ helper-0 · step 0") and "Done (4 tool uses · 58k tokens · 16s)" in lines[0], lines
    report = page.locator(".team-block .tb-row").first.locator(".tb-report").inner_text()
    assert report.strip().startswith("⎿ Initial audience")
    assert page.locator("#column").inner_text().count("Done (4 tool uses") == 1
    assert page.locator(".team-block .tb-head").inner_text() == "Team · 2 running · 1 of 4 done"
    assert page.locator(".team-block").count() == 1
    ended = heights(page)
    assert ended[1:] == before[1:]                    # the other rows did not move or grow

    # click a row: its details (last actions, counts); click again: closed
    target = page.locator(".team-block .tb-row").nth(1)
    target.click()
    detail = target.locator(".tb-detail")
    detail.wait_for(state="visible")
    assert "file-0.py" in detail.inner_text() and "5 tool uses" in detail.inner_text()
    fixture.push(status("job-1", [done(0), running(1, doing="grep", path="", tools=6), running(2), row(3, "waiting")]))
    page.wait_for_function("document.querySelectorAll('.team-block .tb-row')[1].textContent.includes('6 tools')")
    assert detail.is_visible()                         # an update keeps it open
    page.screenshot(path=str(shots / "team-block-details.png"))
    target.locator(".tb-line").click()
    assert detail.is_hidden()

    # the job ends: the block stays as the record, every row in its ended form
    fixture.push(status("job-1", [done(0), done(1), done(2), done(3)], working=False))
    page.wait_for_function("document.querySelector('.team-block .tb-head').textContent.includes('4 of 4')")
    assert page.locator(".team-block .tb-line").all_inner_texts()[3].startswith("✓ helper-3")
    assert page.locator(".team-block").count() == 1
    page.screenshot(path=str(shots / "team-block-ended.png"))
    assert not errors


def test_twenty_helpers_keep_the_block_within_its_height_with_the_running_steps_in_view(browser, tmp_path):
    for width, height in [(1440, 1000), (390, 844)]:
        page = browser.new_page(color_scheme="dark", viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        fixture = Fixture(page)
        rows = [done(i) for i in range(14)] + [running(14), running(15)] + [row(i, "waiting") for i in range(16, 20)]
        fixture.push(*START, status("job-20", rows))
        page.goto("http://localhost:8765/?t=fixture")
        page.locator(".team-block .tb-row").nth(19).wait_for(state="attached")
        page.wait_for_timeout(300)
        block = page.locator(".team-block").bounding_box()
        line = page.locator(".team-block .tb-line").first.bounding_box()["height"]
        assert block["height"] <= 16 * line + 60, (block, line)          # about ten rows, then it scrolls inside
        assert block["height"] < 0.5 * height
        box = page.locator(".team-block .tb-rows")
        assert box.evaluate("el => el.scrollHeight > el.clientHeight")
        inside = box.bounding_box()
        for n in (14, 15):                                                # the running steps are in view
            r = page.locator(".team-block .tb-row").nth(n).bounding_box()
            assert r["y"] >= inside["y"] - 1 and r["y"] + r["height"] <= inside["y"] + inside["height"] + 1, (n, r, inside)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"team-block-20-{width}.png"))
        assert not errors
        page.close()


def test_the_final_block_comes_back_after_a_reload(browser):
    page = browser.new_page(color_scheme="dark", viewport={"width": 1200, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fixture = Fixture(page)
    final = {"job": "job-9", "rows": [done(0), done(1, report="All checked.")], "done": 2, "total": 2}
    fixture.history = {"seq": 5, "pending": [], "status": {**status("job-9", final["rows"], working=False)},
                       "items": [{"type": "user", "text": "run the team"},
                                 {"type": "tool_call", "name": "start_team_job", "subject": "launch", "look": False},
                                 {"type": "team_block", **final},
                                 {"type": "tool_result", "name": "start_team_job", "summary": "", "failed": False},
                                 {"type": "final", "text": "The team finished."}]}
    for _ in range(2):                                 # opened, then reloaded
        page.goto("about:blank")                       # the page before must not take the next batch
        fixture.push({"type": "conversation", "path": "c.json", "base": 5})
        page.goto("http://localhost:8765/?t=fixture")
        page.locator(".team-block .tb-row").nth(1).wait_for(state="visible")
        page.wait_for_timeout(300)
        assert page.locator(".team-block").count() == 1
        lines = page.locator(".team-block .tb-line").all_inner_texts()
        assert [l[:1] for l in lines] == ["✓", "✓"]
        assert "All checked." in page.locator(".team-block").inner_text()
        assert page.locator(".team-block .tb-head").inner_text() == "Team · 2 of 2 done"
    assert not errors


def test_every_state_reads_as_its_line_a_replay_adds_nothing_and_reduced_motion_is_still(browser):
    page = browser.new_page(color_scheme="dark", viewport={"width": 390, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fixture = Fixture(page)
    rows = [running(0), row(1, "waiting"), done(2),
            row(3, "failed", mark="✗", word="Failed", tone="red", report="The provider refused."),
            row(4, "skipped", mark="–", word="Skipped", tone="dim"),
            row(5, "interrupted", mark="■", word="Stopped", tone="yellow"),
            row(6, "unknown", mark="?", word="Ended without word", tone="dim")]
    fixture.push(*START, status("job-s", rows))
    page.goto("http://localhost:8765/?t=fixture")
    page.locator(".team-block .tb-row").nth(6).wait_for(state="visible")
    marks = [line[:1] for line in page.locator(".team-block .tb-line").all_inner_texts()]
    assert marks == ["●", "○", "✓", "✗", "–", "■", "?"]
    assert page.locator(".team-block .tb-row.is-interrupted").count() == 1
    assert page.locator(".tb-row.tone-red .tb-report").inner_text().strip() == "⎿ The provider refused."
    assert page.locator(".is-running .tb-mark").evaluate("el => getComputedStyle(el).animationName") != "none"
    fixture.push(status("job-s", rows))               # a reconnect replays the status: the rows are not added again
    fixture.push(status("job-s", rows))
    page.wait_for_timeout(500)
    assert page.locator(".team-block").count() == 1 and page.locator(".team-block .tb-row").count() == 7
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.emulate_media(reduced_motion="reduce")
    assert page.locator(".is-running .tb-mark").evaluate("el => getComputedStyle(el).animationName") == "none"
    assert not errors
