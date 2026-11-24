"""Full control in the app window: a message waiting while agentx works can be sent now, edited or removed; a
conversation in Recent can be renamed or deleted (moved, never erased; never an open one); and a message you sent
can be edited and sent again (the conversation goes back to just before it; how it was is kept). Scripted models
over the real server."""
import threading
import time

import pytest

from agentx_dev.Pictures import content_text
from agentx_dev.cli.sessions import list_sessions, read_titles, sessions_dir
from tests.conftest import MockModel, make_final
from tests.test_one_conversation_per_device import Host, LAPTOP, ready, said, until


@pytest.fixture
def host(tmp_path):
    made = []

    def make(model):
        made.append(Host(tmp_path, model, ()))
        made[-1].front._worker = made[-1].thread          # Stop reaches the app's thread, as in a real run
        return made[-1]
    yield make
    for h in made:
        h.close()


def echo_model(slow_word="", stop=None, entered=None):
    """Answers each message with ``answered: <it>``; one with ``slow_word`` in it works until ``stop`` is set
    (in short waits, as a streamed reply does, so Stop gets in), setting ``entered`` once it is at it."""
    def script(messages):
        last = str(messages[-1]["content"])
        if "Answer with JSON only" in last:                 # the size question: never a team here
            return '{"team": false, "reason": "a test"}'
        typed = last.split("\n\n")[0]
        if slow_word and slow_word in typed and "Observation" not in last:
            if entered is not None:
                entered.set()
            for _ in range(300):
                if stop is not None and stop.is_set():
                    break
                time.sleep(0.05)
            return make_final("slow one done")
        return make_final(f"answered: {typed}")
    return MockModel(script=script)


def waiting(front):
    return front._status().get("waiting") or []


class TestAMessageWaitingWhileItWorks:
    def start_long_job(self, host):
        stop, entered = threading.Event(), threading.Event()
        h = host(echo_model("long job", stop, entered))
        main = h.front_of(LAPTOP)
        ready(main)
        h.request(LAPTOP, "POST", "/api/send", {"text": "a long job please"})
        assert entered.wait(15)                            # in its step: what is typed now waits for the next
        for text in ("a small note", "the urgent one"):
            h.request(LAPTOP, "POST", "/api/send", {"text": text})
        until(lambda: len(waiting(main)) == 2)
        return h, main, stop

    def test_each_has_its_kind_and_long_text_is_not_cut_short(self, host):
        h, main, stop = self.start_long_job(host)
        entries = waiting(main)
        assert [e["text"] for e in entries] == ["a small note", "the urgent one"]
        assert all(e["kind"] == "agent" for e in entries)
        stop.set()

    def test_send_now_stops_the_step_and_that_message_is_read_next(self, host):
        h, main, stop = self.start_long_job(host)
        result = h.request(LAPTOP, "POST", "/api/waiting", {"action": "now", "n": 2})
        assert result["ok"], result
        said(main, "a small note")                         # read at the first step of the one sent now
        ready(main)
        echoes = [e for e in main.bus.after(0) if e["type"] == "echo"]
        assert (echoes[0]["text"], echoes[0]["outcome"]) == ("the urgent one", "sent now")
        said_to_it = [content_text(m["content"]) for m in main.app.session.history if m["role"] == "user"]
        urgent = next(n for n, text in enumerate(said_to_it) if text.startswith("the urgent one"))
        note = next(n for n, text in enumerate(said_to_it) if "a small note" in text)
        assert said_to_it[0].startswith("a long job please") and urgent < note     # the one sent now went first
        assert "slow one done" not in [e.get("text") for e in main.bus.after(0) if e["type"] == "final"]

    def test_edit_takes_it_back_and_gives_its_text(self, host):
        h, main, stop = self.start_long_job(host)
        result = h.request(LAPTOP, "POST", "/api/waiting", {"action": "edit", "n": 1})
        assert result["ok"] and result["edit"] == "a small note"
        assert [e["text"] for e in waiting(main)] == ["the urgent one"]
        assert h.request(LAPTOP, "POST", "/api/waiting", {"action": "remove", "n": 1})["ok"]
        assert waiting(main) == []
        stop.set()
        said(main, "slow one done")

    def test_a_number_that_is_not_there_is_refused(self, host):
        h, main, stop = self.start_long_job(host)
        assert not h.request(LAPTOP, "POST", "/api/waiting", {"action": "now", "n": 5})["ok"]
        assert not h.request(LAPTOP, "POST", "/api/waiting", {"action": "now", "n": "x"})["ok"]
        assert not h.request(LAPTOP, "POST", "/api/waiting", {"action": "shout", "n": 1})["ok"]
        assert len(waiting(main)) == 2
        stop.set()


def two_conversations(host):
    """Conversation A (saved, not open) and B (open)."""
    h = host(echo_model())
    main = h.front_of(LAPTOP)
    ready(main)
    h.request(LAPTOP, "POST", "/api/send", {"text": "about apples"})
    said(main, "answered: about apples")
    ready(main)
    first = main.app.session.session_id
    h.request(LAPTOP, "POST", "/api/send", {"text": "/clear"})
    until(lambda: main.conversation_id() not in ("", first))
    ready(main)
    h.request(LAPTOP, "POST", "/api/send", {"text": "about pears"})
    said(main, "answered: about pears")
    ready(main)
    until(lambda: main.app.session_path is not None and main.app.session_path.exists())
    return h, main, first


class TestTheMenuOnAConversationInRecent:
    def test_rename(self, host):
        h, main, first = two_conversations(host)
        result = h.request(LAPTOP, "POST", "/api/session", {"action": "rename", "id": first, "title": "  Fruit\nplans "})
        assert result["ok"]
        info = next(i for i in list_sessions(h.home, h.cwd) if i.session_id == first)
        assert read_titles(h.home, h.cwd)[info.path.name] == "Fruit plans"
        assert not h.request(LAPTOP, "POST", "/api/session", {"action": "rename", "id": first, "title": "  "})["ok"]

    def test_delete_moves_it_aside_and_never_an_open_one(self, host):
        h, main, first = two_conversations(host)
        open_one = main.conversation_id()
        refused = h.request(LAPTOP, "POST", "/api/session", {"action": "delete", "id": open_one})
        assert not refused["ok"] and "open" in refused["error"]
        info = next(i for i in list_sessions(h.home, h.cwd) if i.session_id == first)
        assert h.request(LAPTOP, "POST", "/api/session", {"action": "delete", "id": first})["ok"]
        assert first not in [i.session_id for i in list_sessions(h.home, h.cwd)]
        assert (sessions_dir(h.home, h.cwd) / "deleted" / info.path.name).exists()     # it can be put back
        assert main.app.session_path.exists()

    def test_an_unknown_one_or_action_is_refused(self, host):
        h, main, first = two_conversations(host)
        assert not h.request(LAPTOP, "POST", "/api/session", {"action": "delete", "id": "../../x"})["ok"]
        assert not h.request(LAPTOP, "POST", "/api/session", {"action": "burn", "id": first})["ok"]


class TestEditingAMessageYouSent:
    def three_messages(self, host):
        h = host(echo_model("long job"))
        main = h.front_of(LAPTOP)
        ready(main)
        for text in ("one", "two", "three"):
            h.request(LAPTOP, "POST", "/api/send", {"text": text})
            said(main, f"answered: {text}")
            ready(main)
        return h, main

    def test_the_conversation_goes_back_to_before_it_and_how_it_was_is_kept(self, host):
        h, main = self.three_messages(host)
        shown = [i for i in main.history() if i["type"] == "user"]
        assert [i["text"] for i in shown] == ["one", "two", "three"] and all("at" in i for i in shown)
        result = h.request(LAPTOP, "POST", "/api/edit", {"n": 2, "text": "two"})
        assert result["ok"], result
        assert [i["text"] for i in main.history() if i["type"] == "user"] == ["one"]
        kept = next(i for i in list_sessions(h.home, h.cwd) if i.session_id == result["kept"])
        assert read_titles(h.home, h.cwd)[kept.path.name].endswith("(before edit)")
        assert "three" in kept.path.read_text(encoding="utf-8")                  # nothing lost
        assert any(e["type"] == "conversation" for e in main.bus.after(0))       # a reloaded window starts here
        h.request(LAPTOP, "POST", "/api/send", {"text": "two, better"})
        said(main, "answered: two, better")
        ready(main)
        assert [i["text"] for i in main.history() if i["type"] == "user"] == ["one", "two, better"]

    def test_the_text_must_match_and_it_must_be_waiting(self, host):
        h, main = self.three_messages(host)
        changed = h.request(LAPTOP, "POST", "/api/edit", {"n": 2, "text": "not what it said"})
        assert not changed["ok"] and "reload" in changed["error"]
        moved = h.request(LAPTOP, "POST", "/api/edit", {"n": 9, "text": "three"})   # found by its text
        assert moved["ok"]
        ready(main)
        h.request(LAPTOP, "POST", "/api/send", {"text": "a long job now"})
        until(lambda: main._working())
        busy = h.request(LAPTOP, "POST", "/api/edit", {"n": 1, "text": "one"})
        assert not busy["ok"] and "working" in busy["error"]
