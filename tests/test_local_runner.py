"""The runner binds tools natively for models that prefer it (small local models fail the ReAct format)."""

from agentx_dev import AgentRunner, AgentType, StandardTool
from tests.conftest import MockModel, _MockToolCallingModel


class PreferringModel(_MockToolCallingModel):
    prefers_native_tools = True


def a_tool():
    return StandardTool(func=lambda x: "ok", name="set_goal", description="Persist the goal.")


def _make(model, **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[a_tool()], verbose=False, **kw)


def test_preferring_model_binds_natively():
    runner = _make(PreferringModel(script=None, tool_script=[]))
    assert runner.bind_tools_natively is True
    assert not runner.use_function_calling


def test_explicit_false_wins():
    runner = _make(PreferringModel(script=None, tool_script=[]), bind_tools_natively=False)
    assert runner.bind_tools_natively is False


def test_other_models_unchanged():
    assert _make(MockModel()).bind_tools_natively is False


def test_explicit_function_calling_beats_preference():
    runner = _make(PreferringModel(script=None, tool_script=[]), use_function_calling=True)
    assert runner.bind_tools_natively is False
    assert runner.use_function_calling is True


def test_addendum_reaches_native_system_prompt():
    model = PreferringModel(script=None, tool_script=[
        {"type": "tool_use", "name": "respond", "input": {"answer": "done"}, "id": "c1"},
    ])
    runner = _make(model, system_addendum="ROLE-RULE-XYZ")
    runner.invoke("go")
    system = next(m["content"] for m in model.tool_calls_made[0] if m.get("role") == "system")
    assert "ROLE-RULE-XYZ" in system
