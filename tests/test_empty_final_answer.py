"""An empty final answer is reported, never passed off as an answer."""

import asyncio

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, StandardTool
from agentx_dev.Runner.AgentRun import NO_ANSWER_TEXT
from tests.conftest import MockModel, make_final, make_react_response

READ = StandardTool(func=lambda path="": "contents", name="read_path", description="read")
WRITE = StandardTool(func=lambda path="", content="": f"wrote {path}", name="write_file", description="write")
TOOLS = [READ, WRITE]


def runs(model_factory, **kw):
    """The completion of the same scripted run, sync and async."""
    out = []
    sync = AgentRunner(model=model_factory(), agent=AgentType.ReAct, tools=TOOLS, verbose=False, **kw)
    out.append(sync.invoke("go"))
    asyn = AsyncAgentRunner(model=model_factory(), agent=AgentType.ReAct, tools=TOOLS, verbose=False, **kw)
    out.append(asyncio.run(asyn.ainvoke("go")))
    return out


def test_no_tools_empty_answer_is_no_answer():
    for c in runs(lambda: MockModel(script=[make_final("")])):
        assert c.outcome == "no_answer"
        assert c.content == NO_ANSWER_TEXT == "The model gave no answer."


def test_tool_work_then_empty_answer_is_done_with_note():
    script = lambda: MockModel(script=[
        make_react_response("read_path", {"path": "a.txt"}),
        make_react_response("write_file", {"path": "done.txt", "content": "x"}),
        make_react_response("read_path", {"path": "b.txt"}),
        make_final(""),
    ])
    for c in runs(script):
        assert c.outcome == "done"
        assert c.content.startswith("The model gave no final answer. It called: read_path, write_file")
        assert "files changed: done.txt" in c.content


def test_tool_work_without_writes_has_no_files_part():
    script = lambda: MockModel(script=[make_react_response("read_path", {"path": "a"}), make_final("  ")])
    for c in runs(script):
        assert c.outcome == "done"
        assert c.content == "The model gave no final answer. It called: read_path"


def test_native_respond_without_answer():
    def model():
        return MockModel(tool_script=[{"type": "tool_use", "name": "respond", "input": {}, "id": "r1"}])
    for c in runs(model, bind_tools_natively=True):
        assert c.outcome == "no_answer"
        assert c.content == NO_ANSWER_TEXT


def test_native_empty_text_turn_is_no_answer():
    def model():
        return MockModel(tool_script=[{"type": "text", "text": "  "}] * 4)
    for c in runs(model, bind_tools_natively=True):
        assert c.outcome == "no_answer"


def test_empty_final_answer_action():
    for c in runs(lambda: MockModel(script=[make_react_response("Final_Answer", "")])):
        assert c.outcome == "no_answer"


def test_normal_answer_untouched():
    for c in runs(lambda: MockModel(script=[make_final("all good")])):
        assert c.outcome == "done"
        assert c.content == "all good"
