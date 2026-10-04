"""A long persistent run keeps its context bounded."""

from agentx_dev import AgentRunner, AgentType, Persistence, StandardTool
from agentx_dev.Runner.Persistence import estimate_tokens
from tests.conftest import MockModel, make_final, make_react_response

TURNS = 3000


def test_context_stays_bounded_over_thousands_of_turns():
    sizes = []
    summaries = []
    turn = []

    def script(messages):
        if len(messages) == 1 and "You are compacting" in str(messages[0]["content"]):
            summaries.append(1)
            return "NOTES"
        turn.append(1)
        sizes.append(estimate_tokens(messages))
        if len(turn) < TURNS:
            return make_react_response("big", str(len(turn)))
        return make_final("finished")

    big = StandardTool(func=lambda x: f"call {x}: " + "line of output " * 40, name="big", description="big output")
    cfg = Persistence(max_minutes=5, compact_at_tokens=3000, keep_recent_turns=6, max_turns=TURNS + 10)
    runner = AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, tools=[big],
                         verbose=False, persistence=cfg)
    result = runner.invoke("go")

    assert result.outcome == "done" and result.content == "finished"
    assert len(sizes) == TURNS
    # Each call returns different text only to keep the history realistic; identical results are not a stuck signal.
    # Without compaction the history would reach hundreds of thousands of tokens.
    assert max(sizes) < 20_000, max(sizes)
    assert len(summaries) > 10
