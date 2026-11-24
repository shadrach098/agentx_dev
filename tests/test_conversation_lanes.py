"""Another conversation opens while one works (a lane): the window shows it at once and the first goes on working.
Scripted models over the real server."""
import threading
import time

import pytest

from agentx_dev.cli import webui
from tests.conftest import MockModel, make_final
from tests.test_one_conversation_per_device import Host, LAPTOP, PHONE, answers, ready, said, until


@pytest.fixture
def host(tmp_path):
    made = []

    def make(main_model, *device_models):
        made.append(Host(tmp_path, main_model, device_models))
        return made[-1]
    yield make
    for h in made:
        h.close()


def slow_second_answer(release):
    """Answers "first" at once; the second message waits for ``release`` (the conversation is working)."""
    calls = {"n": 0}

    def script(messages):
        calls["n"] += 1
        if calls["n"] >= 2:
            release.wait(20)
            return make_final("second, after a while")
        return make_final("first")
    return MockModel(script=script)


def working_main(host):
    """The laptop's main conversation: one finished exchange (conversation A, then /clear), and now working."""
    release = threading.Event()
    h = host(slow_second_answer(release), answers("lane answer"), answers("spare"))
    main = h.front_of(LAPTOP)
    ready(main)
    h.request(LAPTOP, "POST", "/api/send", {"text": "hello"})
    said(main, "first")
    ready(main)
    first = main.app.session.session_id
    h.request(LAPTOP, "POST", "/api/send", {"text": "/clear"})        # A is saved; a new conversation starts
    until(lambda: main.conversation_id() not in ("", first))
    ready(main)
    h.request(LAPTOP, "POST", "/api/send", {"text": "now a long job"})
    until(lambda: main._working())
    return h, main, first, release


def test_a_conversation_opens_beside_a_working_one_and_both_go_on(host):
    h, main, first, release = working_main(host)
    opened = h.request(LAPTOP, "POST", "/api/lanes", {"session": ""})
    assert opened["ok"] and opened["lane"].startswith("l")
    lane = h.front.route(LAPTOP, opened["lane"])
    assert lane is not main and lane.lane == opened["lane"]
    ready(lane)                                                      # the new one takes a message at once
    lane.send("in the lane")
    said(lane, "lane answer")
    assert main._working()                                           # the first is still at it
    release.set()
    said(main, "second, after a while")


def test_a_saved_conversation_opens_in_a_lane_and_a_second_click_goes_to_it(host):
    h, main, first, release = working_main(host)
    opened = h.front.open_lane(LAPTOP, first)
    lane = h.front.route(LAPTOP, opened["lane"])
    ready(lane)
    assert lane.conversation_id() == first                           # it resumed that conversation
    assert h.front.open_lane(LAPTOP, first[:8]) == {"ok": True, "lane": opened["lane"]}   # not opened twice
    assert h.front.open_lane(LAPTOP, main.conversation_id())["lane"] == ""                # the shown one
    release.set()


def test_the_list_marks_what_runs_behind_the_shown_one(host):
    h, main, first, release = working_main(host)
    lane = h.front.open_lane(LAPTOP, first)["lane"]
    ready(h.front.route(LAPTOP, lane))
    items = {item["id"]: item for item in h.request(LAPTOP, "GET", f"/api/sessions?l={lane}")}
    assert items[main.conversation_id()]["lane"] == "" and items[main.conversation_id()]["working"] is True
    assert items[first]["lane"] == lane and items[first]["current"] is True
    release.set()


def test_a_conversation_open_on_another_device_is_not_opened_again(host):
    h, main, first, release = working_main(host)
    phone = h.front_of(PHONE)
    ready(phone)
    answer = h.front.open_lane(PHONE, main.conversation_id())
    assert answer["ok"] is False and "another window" in answer["error"]
    release.set()


def test_a_device_runs_at_most_a_few_conversations_beside_its_own(host, monkeypatch):
    monkeypatch.setattr(webui, "MAX_LANES", 1)
    h, main, first, release = working_main(host)
    assert h.front.open_lane(LAPTOP, "")["ok"]
    refused = h.front.open_lane(LAPTOP, "")
    assert refused["ok"] is False and "already run" in refused["error"]
    release.set()


def test_an_unknown_lane_falls_back_to_the_devices_own_conversation(host):
    h = host(answers("main"))
    main = h.front_of(LAPTOP)
    ready(main)
    assert h.front.route(LAPTOP, "l99") is main
    assert h.front.route(LAPTOP, "../x") is main                     # not a lane name at all
    assert h.request(LAPTOP, "GET", "/api/state?l=l99")["lane"] == ""


def test_the_window_counts_as_open_while_it_shows_any_of_its_conversations(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "CLOSE_GRACE_SEC", 0.1)
    front = webui.WebFront(cwd=tmp_path, home=tmp_path, open_it=True, opener=lambda url: "")
    lane = webui.WebFront(cwd=tmp_path, home=tmp_path, open_it=False)
    lane.host = front
    front.window_connected(True)
    lane.window_connected(True)                     # the window switched to a lane: it connects there...
    front.window_connected(False)                   # ...and leaves the main conversation
    time.sleep(0.2)
    assert front.window_closed() is False           # it is still open
    lane.window_connected(False)
    time.sleep(0.2)
    assert front.window_closed() is True
