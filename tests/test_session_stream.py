"""Session.stream: a streamed turn updates the session the way invoke does."""

import pytest

from agentx_dev import AgentRunner, AgentType, Session
from tests.conftest import MockModel, make_final


def runner(*texts):
    return AgentRunner(model=MockModel(script=[make_final(t) for t in texts]), agent=AgentType.ReAct,
                       tools=[], verbose=False, auto_cache=False)


def test_it_yields_the_events_and_absorbs_the_completion():
    session = Session.start(runner("first"))
    events = list(session.stream("hello"))
    assert [e["type"] for e in events][-2:] == ["final", "completion"]
    assert session.completions_meta[-1]["content"] == "first"
    assert any(m["role"] == "user" and m["content"] == "hello" for m in session.history)


def test_a_second_turn_carries_the_history():
    session = Session.start(runner("first", "second"))
    list(session.stream("one"))
    list(session.stream("two"))
    users = [m["content"] for m in session.history if m["role"] == "user"]
    assert users == ["one", "two"]


def test_an_abandoned_turn_records_nothing():
    session = Session.start(runner("first"))
    gen = session.stream("hello")
    next(gen)
    gen.close()
    assert session.completions_meta == [] and session.history == []


def test_it_needs_a_runner():
    with pytest.raises(RuntimeError, match="no runner attached"):
        list(Session().stream("x"))
