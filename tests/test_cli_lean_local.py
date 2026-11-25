"""agentx sends a lean request to a local model (``localTools``): the core tools only and the short skill index, in a
local chat, at the switch to the fallback, and for team helpers; the "almost no room" estimate measures it. Also the
advice for a larger window names a variant Ollama accepts."""

import json

import pytest

from agentx_dev import ContextWindowTooSmall
from agentx_dev.cli import app as app_module
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.models import larger_window
from agentx_dev.cli.team import build_helpers
from agentx_dev.cli.tools import LEAN_TOOLS, build_tools, lean_tools
from agentx_dev.Skills import Skills
from agentx_dev.Tools import StructuredTool
from tests.conftest import MockModel, make_final
from tests.test_cli_fallback import LocalTools, OutOfCredit, paid, respond, started_on_claude
from tests.test_cli_local_app import Lines, local_mock, make_app
from tests.test_runner_fallback import KeyRejected, Paid

WEB = {"web_fetch", "web_search"}


@pytest.fixture(autouse=True)
def folder_is_trusted(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: True)


def mcp_tool():
    from pydantic import BaseModel

    class Args(BaseModel):
        q: str = ""

    tool = StructuredTool(func=lambda q="": "mcp says hi", args_schema=Args, name="mcp__srv__lookup",
                          description="A tool an MCP server added. " + "Long. " * 50)
    tool.mcp_server, tool.mcp_remote = "srv", "lookup"
    return tool


def names(runner):
    return set(runner.registry.names)


class Recording(LocalTools):
    """A local model stand-in that keeps the tool specs of every call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tools_seen = []

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tools_seen.append([spec["name"] for spec in tools])
        return super().call_with_tools(messages, tools, force_tool=force_tool)


# -- which tools -----------------------------------------------------------------------------------------------------

def test_lean_tools_keeps_the_core_tools_and_drops_the_web_and_mcp_ones(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), mcp_tool()]
    kept = [tool.name for tool in lean_tools(tools)]
    assert kept == ["read_path", "list_directory", "find_files", "grep", "write_file", "edit_file", "delete_path",
                    "move_file", "run_python", "run_shell"]
    assert "start_team_job" in LEAN_TOOLS and "recall" in LEAN_TOOLS
    same_name = mcp_tool()
    same_name.name = "read_path"
    assert lean_tools([same_name]) == []                  # an MCP server's tool never counts as a core one


# -- a chat on a local model -----------------------------------------------------------------------------------------

def test_a_local_chat_sends_the_lean_request(tmp_path):
    model = local_mock(window=32768)
    model.prefers_native_tools = True
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="qwen3:8b")
    assert app.start() == 0
    app.tools.append(mcp_tool())
    runner = app._make_runner(model)
    assert names(runner) == {"read_path", "list_directory", "find_files", "grep", "write_file", "edit_file",
                             "delete_path", "move_file", "run_python", "run_shell", "start_team_job", "recall",
                             "start_background", "background_output", "stop_background", "list_background",
                             "send_to_terminal", "use_skill"}
    system = runner._system_prompt("")
    assert "read_path, list_directory" in system and "Search FILE CONTENTS" not in system
    assert "Skills: short guides for kinds of work. When one fits" in system


def test_local_tools_all_sends_the_whole_request(tmp_path):
    model = local_mock(window=32768)
    model.prefers_native_tools = True
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="qwen3:8b",
                            local_tools="all")
    assert app.start() == 0
    app.tools.append(mcp_tool())
    runner = app._make_runner(model)
    assert WEB <= names(runner) and "mcp__srv__lookup" in names(runner)
    assert "Search FILE CONTENTS" in runner._system_prompt("")


def test_a_cloud_chat_is_the_same_whatever_local_tools_says(tmp_path):
    def request(setting):
        model = MockModel(script=[])
        model.prefers_native_tools = True
        folder = tmp_path / setting
        folder.mkdir()
        app, _, _, _ = make_app(folder, Lines("/exit"), lambda name, env: model, model_name="claude-opus-5-5",
                                local_tools=setting)
        assert app.start() == 0
        runner = app._make_runner(model)
        prompt = runner._system_prompt("").replace(str(folder), "<folder>")
        return names(runner), prompt, json.dumps(runner._native_specs())

    lean, full = request("lean"), request("all")
    assert lean == full
    assert WEB <= lean[0] and "Search FILE CONTENTS" in lean[1]


def test_the_switch_to_the_fallback_brings_the_lean_tools(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = Recording("qwen3:8b", respond("Done on the fallback."))
    app, out, _, _ = started_on_claude(tmp_path, Lines("hello", "/exit"), primary, fallback)
    assert app.start() == 0
    assert "Done on the fallback." in out.getvalue()
    assert set(fallback.tools_seen[0]) == {*LEAN_TOOLS, "use_skill", "respond"}
    system = str(fallback.tool_calls_made[0][0]["content"])
    assert "Search FILE CONTENTS" not in system and "When one fits the task" in system
    assert app.session.runner.model is fallback and not (WEB & names(app.session.runner))   # the rebuilt runner


def test_the_switch_keeps_every_tool_with_local_tools_all(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = Recording("qwen3:8b", respond("Done on the fallback."))
    app, _, _, _ = started_on_claude(tmp_path, Lines("hello", "/exit"), primary, fallback, local_tools="all")
    assert app.start() == 0
    assert WEB <= set(fallback.tools_seen[0])


def test_the_no_room_estimate_measures_the_lean_request(tmp_path):
    app, _, _, _ = make_app(tmp_path, Lines(), lambda name, env: MockModel(script=[]), model_name="first")
    model = local_mock(window=16384)
    model.prefers_native_tools = True
    lean = app._fixed_chars(model)
    app.local_tools = "all"
    full = app._fixed_chars(model)
    assert lean < full - 3000


# -- team helpers ----------------------------------------------------------------------------------------------------

def _lean(model):
    return getattr(model, "is_local", False)


def test_a_helper_on_a_local_model_gets_the_lean_tools(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), mcp_tool()]
    model = local_mock()
    helpers = build_helpers(model, tools, "BASE", skills=Skills(tmp_path / "skills"), lean_for=_lean)
    explorer = helpers["explorer"][1]
    assert not (WEB & names(explorer)) and "mcp__srv__lookup" not in names(explorer)
    assert "use_skill" in names(explorer) and "grep" in names(explorer)
    assert "web_search and web_fetch are not available" in explorer.system_addendum
    cloud = build_helpers(MockModel(), tools, "BASE", skills=Skills(tmp_path / "skills"), lean_for=_lean)
    assert WEB <= names(cloud["explorer"][1]) and "mcp__srv__lookup" in names(cloud["explorer"][1])
    assert "not available" not in cloud["explorer"][1].system_addendum


def test_a_helper_moving_to_the_fallback_gets_the_lean_tools(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), mcp_tool()]
    fallback = Recording("qwen3:8b", respond("found it"))
    helpers = build_helpers(Paid([KeyRejected()]), tools, "BASE", skills=Skills(tmp_path / "skills"),
                            fallback_model=fallback, lean_for=_lean, addendum_for=lambda model: "FITTED")
    completion = helpers["explorer"][1].Initialize("look around")
    assert completion.content == "found it"
    assert not (WEB & set(fallback.tools_seen[0])) and "mcp__srv__lookup" not in fallback.tools_seen[0]
    assert "grep" in fallback.tools_seen[0] and "use_skill" in fallback.tools_seen[0]
    system = str(fallback.tool_calls_made[0][0]["content"])
    assert "FITTED" in system and "web_search and web_fetch are not available" in system


# -- the advice for a larger window ----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,window,target,variant", [
    ("qwen3-agent:latest", 16384, 32768, "qwen3-agent-32k"),
    ("qwen3:8b", 8192, 16384, "qwen3-16k"),
    ("llama3.1", 300, 16384, "llama3.1-16k"),
    ("qwen3-agent-32k", 32768, 65536, "qwen3-agent-32k-64k"),
    ("hf.co/someone/model:Q4_K_M", 4096, 16384, "hf.co/someone/model-16k"),
])
def test_larger_window_doubles_and_names_a_valid_variant(name, window, target, variant):
    assert larger_window(name, window) == (target, variant)


def test_too_small_says_double_the_window_and_a_valid_name(tmp_path, monkeypatch):
    def stream(self, *args, **kwargs):
        raise ContextWindowTooSmall(16384, 20000)
        yield

    monkeypatch.setattr(app_module.AgentRunner, "stream", stream)
    model = local_mock(name="qwen3-agent:latest", window=16384)
    app, out, _, _ = make_app(tmp_path, Lines("hello", "/exit"), lambda name, env: model,
                              model_name="qwen3-agent:latest")
    app.start()
    assert ("qwen3-agent:latest's 16,384-token window is too small for this conversation. A variant with a larger "
            "window fixes it: a Modelfile with FROM qwen3-agent:latest and PARAMETER num_ctx 32768, then ollama "
            "create qwen3-agent-32k -f Modelfile.") in out.getvalue()


def test_no_room_names_a_valid_variant(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines(), lambda name, env: MockModel(script=[]), model_name="first",
                              local_tools="all")
    model = local_mock(name="qwen3-agent:latest", window=4096)
    model.prefers_native_tools = True
    app._warn_if_no_room(model)
    assert ("a Modelfile with FROM qwen3-agent:latest and PARAMETER num_ctx 16384, then ollama create "
            "qwen3-agent-16k -f Modelfile.") in out.getvalue()
