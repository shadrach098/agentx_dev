"""A tool can learn the id of the call it is running for (``current_tool_call_id``): the app keeps a team job's
record under the ``start_team_job`` call that ran it, so a reopened conversation draws the job under that call."""

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.Runner.AgentRun import current_tool_call_id
from tests.conftest import MockModel, make_final, make_react_response


def _probe(seen, timeout=None):
    def probe(q):
        seen.append(current_tool_call_id())
        return "ok"
    tool = StandardTool(func=probe, name="probe", description="Probe.")
    if timeout is not None:
        tool.timeout_sec = timeout
    return tool


def _format_call(call_id, action, action_input):
    return {"type": "tool_use", "name": "ReAct", "id": call_id,
            "input": {"Thought": "", "action": action, "action_input": action_input}}


def test_a_function_calling_run_names_the_call():
    seen = []
    model = MockModel(tool_script=[_format_call("c1", "probe", "x"), _format_call("c2", "Final_Answer", "done")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_probe(seen)], verbose=False,
                         use_function_calling=True, max_iterations=4)
    assert runner.invoke("go").content == "done"
    assert seen == ["c1"]
    assert current_tool_call_id() is None               # only while the tool runs


def test_a_native_run_names_each_call_also_under_a_timeout():
    seen = []
    model = MockModel(tool_script=[{"type": "tool_use", "name": "probe", "input": {"input": "native"}, "id": "n1"},
                                   {"type": "tool_use", "name": "respond", "input": {"answer": "done"}, "id": "n9"}])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_probe(seen, timeout=30)], verbose=False,
                         bind_tools_natively=True, max_iterations=4)
    assert runner.invoke("go").content == "done"
    assert seen == ["n1"]


def test_a_text_mode_run_has_no_call_id():
    seen = []
    model = MockModel(script=[make_react_response("probe", "text mode"), make_final("done")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[_probe(seen)], verbose=False, max_iterations=4)
    assert runner.invoke("go").content == "done"
    assert seen == [None]


def test_a_runner_run_inside_a_tool_call_restores_the_outer_id():
    seen = []

    def outer(q):
        seen.append(("before", current_tool_call_id()))
        inner_model = MockModel(tool_script=[_format_call("i1", "inner_probe", "x"),
                                             _format_call("i2", "Final_Answer", "inner done")])
        probe = StandardTool(func=lambda q: seen.append(current_tool_call_id()) or "ok", name="inner_probe",
                             description="Probe.")
        inner = AgentRunner(model=inner_model, agent=AgentType.ReAct, tools=[probe], verbose=False,
                            use_function_calling=True, max_iterations=4)
        assert inner.invoke("inner").content == "inner done"
        seen.append(("after", current_tool_call_id()))
        return "ok"

    model = MockModel(tool_script=[_format_call("o1", "outer", "x"), _format_call("o2", "Final_Answer", "done")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct,
                         tools=[StandardTool(func=outer, name="outer", description="Runs a runner.")],
                         verbose=False, use_function_calling=True, max_iterations=4)
    assert runner.invoke("go").content == "done"
    assert seen == [("before", "o1"), "i1", ("after", "o1")]
    assert current_tool_call_id() is None
