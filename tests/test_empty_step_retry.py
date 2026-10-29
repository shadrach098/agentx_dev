"""A step with no action and no answer (often a reply cut off by the output limit) is asked for again, a bounded
number of times, instead of ending the run with "unrecognized action"."""

import asyncio

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, StandardTool
from agentx_dev.Runner.AgentRun import EMPTY_STEP_NOTE, EMPTY_STEP_RETRIES
from tests.conftest import MockModel, make_final, make_react_response


STEADY = StandardTool(func=lambda x: f"ok {x}", name="steady", description="works")
EMPTY = make_react_response("", "")


def sync_runner(model):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[STEADY], verbose=False)


def async_runner(model):
    return AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[STEADY], verbose=False)


def text_of(history):
    return "\n".join(str(m.get("content")) for m in history)


class TestSync:
    def test_an_empty_step_is_asked_for_again(self):
        result = sync_runner(MockModel(script=[EMPTY, make_final("the report")])).invoke("go")
        assert result.content == "the report"
        assert EMPTY_STEP_NOTE in text_of(result.history)

    def test_retries_are_bounded(self):
        model = MockModel(script=lambda m: EMPTY)
        result = sync_runner(model).invoke("go")                 # ends: the same-call guard, or the placeholder
        assert result.content.startswith(("(agent emitted an unrecognized action", "(Terminated:"))
        assert len(model.calls) <= EMPTY_STEP_RETRIES + 1


class TestAsync:
    def test_an_empty_step_is_asked_for_again(self):
        result = asyncio.run(async_runner(MockModel(script=[EMPTY, make_final("the report")])).ainvoke("go"))
        assert result.content == "the report"

    def test_retries_are_bounded(self):
        model = MockModel(script=lambda m: EMPTY)
        result = asyncio.run(async_runner(model).ainvoke("go"))
        assert result.content.startswith("(agent emitted an unrecognized action")
        assert len(model.calls) == EMPTY_STEP_RETRIES + 1
