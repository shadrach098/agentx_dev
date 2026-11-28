"""``localMcpServers``: the MCP servers whose tools a local model gets, on top of what ``localTools`` gives. Not set,
a local model gets what ``localTools`` says (lean: no MCP tools, all: every server's); Claude and GPT get every one."""

import json

import pytest

from agentx_dev.cli import app as app_module
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.settings import load_settings
from agentx_dev.cli.team import build_helpers
from agentx_dev.cli.tools import build_tools, left_out_note, local_tools
from agentx_dev.Skills import Skills
from agentx_dev.Tools import StructuredTool
from tests.conftest import MockModel
from tests.test_cli_fallback import OutOfCredit, paid, respond, started_on_claude
from tests.test_cli_lean_local import Recording
from tests.test_cli_local_app import Lines, local_mock, make_app
from tests.test_runner_fallback import KeyRejected, Paid

WEB = {"web_fetch", "web_search"}


@pytest.fixture(autouse=True)
def folder_is_trusted(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: True)


def server_tool(server, remote="lookup"):
    from pydantic import BaseModel

    class Args(BaseModel):
        q: str = ""

    tool = StructuredTool(func=lambda q="": "hi", args_schema=Args, name=f"mcp__{server}__{remote}",
                          description=f"A tool the {server} server added.")
    tool.mcp_server, tool.mcp_remote = server, remote
    return tool


def names(tools):
    return [tool.name for tool in tools]


# -- which tools -----------------------------------------------------------------------------------------------------

def test_with_all_and_a_list_only_the_listed_servers_tools_are_kept(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), server_tool("flights"), server_tool("github")]
    kept = names(local_tools(tools, lean=False, servers=["flights"]))
    assert WEB <= set(kept) and "read_path" in kept
    assert "mcp__flights__lookup" in kept and "mcp__github__lookup" not in kept
    assert kept == [name for name in names(tools) if name in kept]           # in their order


def test_with_lean_and_a_list_the_core_tools_and_the_listed_servers(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), server_tool("flights"), server_tool("github")]
    kept = names(local_tools(tools, lean=True, servers=["flights"]))
    assert "read_path" in kept and "mcp__flights__lookup" in kept
    assert not (WEB & set(kept)) and "mcp__github__lookup" not in kept


def test_an_empty_list_gives_no_mcp_tools_and_none_follows_local_tools(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), server_tool("flights")]
    assert "mcp__flights__lookup" not in names(local_tools(tools, lean=False, servers=[]))
    assert names(local_tools(tools, lean=False, servers=None)) == names(tools)
    assert "mcp__flights__lookup" not in names(local_tools(tools, lean=True, servers=None))


def test_the_note_names_the_servers_left_out_and_the_setting_that_adds_them(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), server_tool("flights"), server_tool("github")]
    note = left_out_note(tools, local_tools(tools, lean=False, servers=["flights"]))
    assert "the tools of the MCP server github" in note and "flights" not in note
    assert "web_search" not in note and '"localTools": "all"' not in note
    assert "MCP servers on a local model" in note and '"localMcpServers"' in note


# -- the setting -----------------------------------------------------------------------------------------------------

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_setting_is_read_from_your_own_file_only(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    cwd.mkdir()
    write(home / ".agentx" / "settings.json", {"localMcpServers": ["flights", "flights", " db "]})
    assert load_settings(cwd, home).local_mcp_servers == ["flights", "db"]
    write(cwd / ".agentx" / "settings.json", {"localMcpServers": []})
    settings = load_settings(cwd, home)
    assert settings.local_mcp_servers == ["flights", "db"]
    assert any("localMcpServers" in problem for problem in settings.problems)


@pytest.mark.parametrize("value", ["flights", [1], [""], {"a": 1}])
def test_a_setting_that_is_not_a_list_of_names_is_ignored(tmp_path, value):
    home = tmp_path / "home"
    write(home / ".agentx" / "settings.json", {"localMcpServers": value})
    settings = load_settings(tmp_path, home)
    assert settings.local_mcp_servers is None
    assert any("'localMcpServers' must be a list of MCP server names" in problem for problem in settings.problems)


# -- the chat, the fallback, team helpers ----------------------------------------------------------------------------

def local_app(tmp_path, **kw):
    model = local_mock(window=32768)
    model.prefers_native_tools = True
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="qwen3:8b", **kw)
    assert app.start() == 0
    app.tools.extend([server_tool("flights"), server_tool("github")])
    return app, model


def test_a_local_chat_gets_only_the_chosen_servers(tmp_path):
    app, model = local_app(tmp_path, local_tools="all", local_mcp_servers=["flights"])
    runner = app._make_runner(model)
    assert "mcp__flights__lookup" in runner.registry.names and "mcp__github__lookup" not in runner.registry.names
    assert WEB <= set(runner.registry.names)
    system = runner._system_prompt("")
    assert "the tools of the MCP server github" in system and "MCP servers on a local model" in system


def test_lean_with_a_chosen_server_adds_it(tmp_path):
    app, model = local_app(tmp_path, local_mcp_servers=["flights"])
    runner = app._make_runner(model)
    assert "mcp__flights__lookup" in runner.registry.names
    assert not (WEB & set(runner.registry.names)) and "mcp__github__lookup" not in runner.registry.names


def test_a_cloud_chat_gets_every_server(tmp_path):
    model = MockModel(script=[])
    model.prefers_native_tools = True
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="claude-opus-5-5",
                            local_mcp_servers=[])
    assert app.start() == 0
    app.tools.extend([server_tool("flights"), server_tool("github")])
    runner = app._make_runner(model)
    assert {"mcp__flights__lookup", "mcp__github__lookup"} <= set(runner.registry.names)
    assert "MCP servers on a local model" not in runner._system_prompt("")


def test_the_switch_to_the_fallback_keeps_only_the_chosen_servers(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = Recording("qwen3:8b", respond("Done on the fallback."))
    app, _, _, _ = started_on_claude(tmp_path, Lines("hello", "/exit"), primary, fallback, local_tools="all",
                                     local_mcp_servers=["flights"])
    app.tools.extend([server_tool("flights"), server_tool("github")])
    assert app.start() == 0
    assert "mcp__flights__lookup" in fallback.tools_seen[0] and "mcp__github__lookup" not in fallback.tools_seen[0]
    assert WEB <= set(fallback.tools_seen[0])


def _lean(model):
    return getattr(model, "is_local", False)


def test_a_helper_on_a_local_model_gets_only_the_chosen_servers(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), server_tool("flights"), server_tool("github")]
    helpers = build_helpers(local_mock(), tools, "BASE", skills=Skills(tmp_path / "skills"), lean_for=_lean,
                            mcp_servers=["flights"])
    explorer = set(helpers["explorer"][1].registry.names)
    assert "mcp__flights__lookup" in explorer and "mcp__github__lookup" not in explorer
    cloud = build_helpers(MockModel(), tools, "BASE", skills=Skills(tmp_path / "skills"), lean_for=_lean,
                          mcp_servers=["flights"])
    assert {"mcp__flights__lookup", "mcp__github__lookup"} <= set(cloud["explorer"][1].registry.names)


def test_a_helper_moving_to_the_fallback_gets_only_the_chosen_servers(tmp_path):
    tools = [*build_tools(tmp_path, [], Gate(cwd=tmp_path)), server_tool("flights"), server_tool("github")]
    fallback = Recording("qwen3:8b", respond("found it"))
    helpers = build_helpers(Paid([KeyRejected()]), tools, "BASE", skills=Skills(tmp_path / "skills"),
                            fallback_model=fallback, lean_for=lambda model: False, mcp_servers=["flights"])
    assert helpers["explorer"][1].Initialize("look around").content == "found it"
    assert "mcp__flights__lookup" in fallback.tools_seen[0] and "mcp__github__lookup" not in fallback.tools_seen[0]
