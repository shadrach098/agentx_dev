"""Every run reports how it ended (default mode, sync runner)."""

from agentx_dev import AgentRunner, AgentType, StandardTool
from tests.conftest import MockModel


def calc():
    return StandardTool(func=lambda x: f"= {x}", name="calc", description="Compute.")


def run(model, **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[calc()], verbose=False, **kw)


def test_a_final_answer_is_done(make_final_response):
    result = run(MockModel(script=[make_final_response("hi")])).invoke("q")
    assert result.outcome == "done" and result.progress is None


def test_running_out_of_iterations_is_iteration_limit(make_react):
    model = MockModel(script=[make_react("calc", str(i)) for i in range(6)])
    result = run(model, max_iterations=3).invoke("q")
    assert result.outcome == "iteration_limit" and "max_iterations" in result.content


def test_three_identical_calls_is_stuck(make_react):
    model = MockModel(script=[make_react("calc", "5")] * 10)
    result = run(model, max_iterations=10).invoke("q")
    assert result.outcome == "stuck" and "Terminated" in result.content


def test_repeated_malformed_json_is_iteration_limit():
    model = MockModel(script=lambda messages: "{ this is not json }")   # JSON-shaped but invalid, and nothing to salvage
    result = run(model, max_iterations=2).invoke("q")
    assert result.outcome == "iteration_limit" and "exhausted max_iterations" in result.content
