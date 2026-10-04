"""A long persistent run keeps its context bounded (sync and async runners)."""

import asyncio

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, Persistence, StandardTool
from agentx_dev.Runner.Persistence import estimate_tokens
from tests.conftest import MockModel, make_final, make_react_response

TURNS = 3000

BIG = StandardTool(func=lambda x: f"call {x}: " + "line of output " * 40, name="big", description="big output")
CFG = Persistence(max_minutes=5, compact_at_tokens=3000, keep_recent_turns=6, max_turns=TURNS + 10)


def _bound(cfg):
    """Compaction runs before a model call once the history passes the threshold,
    so a call never sees more than the threshold plus about one turn's growth
    (an assistant step and its tool result). Allow two turns of headroom."""
    one_turn = estimate_tokens([
        {"role": "assistant", "content": make_react_response("big", str(TURNS))},
        {"role": "user", "content": BIG.func(str(TURNS))},
    ])
    return cfg.compact_at_tokens + 2 * one_turn


class Scripted:
    """Calls ``big`` TURNS times, then answers; compaction summaries return short notes."""

    def __init__(self):
        self.sizes = []
        self.summaries = 0

    def __call__(self, messages):
        if len(messages) == 1 and "You are compacting" in str(messages[0]["content"]):
            self.summaries += 1
            return "NOTES"
        self.sizes.append(estimate_tokens(messages))
        if len(self.sizes) < TURNS:
            return make_react_response("big", str(len(self.sizes)))
        return make_final("finished")


def _check(result, script):
    assert result.outcome == "done" and result.content == "finished"
    assert len(script.sizes) == TURNS
    # Each call returns different text only to keep the history realistic; identical results are not a stuck signal.
    # Without compaction the history would reach hundreds of thousands of tokens.
    assert max(script.sizes) <= _bound(CFG), (max(script.sizes), _bound(CFG))
    assert script.summaries > 10


def test_context_stays_bounded_over_thousands_of_turns():
    script = Scripted()
    runner = AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[BIG],
                         verbose=False, persistence=CFG)
    _check(runner.invoke("go"), script)


def test_async_context_stays_bounded_over_thousands_of_turns():
    script = Scripted()
    runner = AsyncAgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[BIG],
                              verbose=False, persistence=CFG)
    _check(asyncio.run(runner.ainvoke("go")), script)
