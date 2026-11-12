"""Security fixes for MCP servers and project instructions:

- a stdio server's bare command (npx, uvx, node) is found on PATH only, never in the project folder;
- a tool allowed because the server marked it read-only asks again once the server stops saying so, while the
  user's own /mcp allow choices stay;
- a saved sign-in is tied to the address it was made for, and deleted when a name is pointed elsewhere, dropped
  from mcp.json or used again;
- an AGENTX.md in a parent folder is read only when the user trusts that folder.

Nothing here touches the real credential store, ~/.agentx or the network: homes are temporary folders and the
keyring helpers are replaced by a dict."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from agentx_dev.cli import mcp as mcp_module
from agentx_dev.cli.approval import Answer, Gate
from agentx_dev.cli.context import CLOSE_MARK, OPEN_MARK, build_addendum, find_context_files
from agentx_dev.cli.mcp import (CommandNotFound, KeyringStorage, MCPManager, ServerConfig, _Live, load_servers,
                                save_servers, stdio_command)
from agentx_dev.cli.trust import is_trusted, remember_trust
from tests.conftest import MockModel
from tests.test_cli_app import Lines, make_app

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False
needs_mcp = pytest.mark.skipif(not HAVE_MCP, reason="the mcp package is not installed")


@pytest.fixture
def store(monkeypatch):
    """The credential store as a dict (key -> text)."""
    data = {}
    monkeypatch.setattr(mcp_module, "_kr_get", lambda key: data.get(key))
    monkeypatch.setattr(mcp_module, "_kr_set", lambda key, value: data.__setitem__(key, value) or True)
    monkeypatch.setattr(mcp_module, "_kr_delete", lambda key: data.pop(key, None) and None)
    return data


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- #3: a stdio server's program is never taken from the project folder --------------------------------------

def _program(folder, name):
    """A harmless file that counts as a program called ``name`` in ``folder`` (never run)."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + (".cmd" if sys.platform == "win32" else ""))
    path.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n", encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o755)
    return path


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A project folder (the current folder) holding its own npx, and a real-looking npx in a folder on PATH.
    PATH also lists '.', an empty entry and the project folder itself, as a careless PATH might."""
    project, real_bin = tmp_path / "project", tmp_path / "bin"
    fake = _program(project, "npx")
    real = _program(real_bin, "npx")
    monkeypatch.chdir(project)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(project), str(real_bin)]))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return SimpleNamespace(project=project, fake=fake, real=real, home=tmp_path / "home")


def test_a_bare_command_is_found_on_path_not_in_the_project_folder(planted):
    found = stdio_command("npx")
    assert os.path.samefile(found, planted.real)
    assert not os.path.samefile(found, planted.fake)


def test_a_bare_command_only_in_the_project_folder_is_not_found(planted, monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(planted.project)]))
    with pytest.raises(CommandNotFound) as caught:
        stdio_command("npx")
    assert str(caught.value) == "npx not found on PATH"


def test_a_command_with_a_folder_is_used_as_written(planted):
    assert stdio_command(str(planted.real)) == str(planted.real)
    assert stdio_command(sys.executable) == sys.executable


@needs_mcp
def test_the_sdk_is_handed_the_program_found_on_path_and_the_child_keeps_the_current_folder(planted, monkeypatch):
    import mcp.client.stdio as stdio
    handed = []

    def fake_stdio_client(params, errlog=None):
        handed.append(params)
        raise RuntimeError("not started in this test")

    monkeypatch.setattr(stdio, "stdio_client", fake_stdio_client)
    save_servers(planted.home, {"files": ServerConfig("files", "stdio", command="npx", args=["-y", "pkg"])})
    manager = MCPManager(planted.home, {})
    try:
        manager.connect("files", timeout=10)
    finally:
        manager.close()
    assert len(handed) == 1
    assert os.path.isabs(handed[0].command) and os.path.samefile(handed[0].command, planted.real)
    assert handed[0].cwd is None                                  # the child still starts in the current folder


@needs_mcp
def test_a_missing_program_fails_that_server_with_a_clear_status(planted, monkeypatch):
    import mcp.client.stdio as stdio
    started = []

    def fake_stdio_client(params, errlog=None):
        started.append(params)
        raise RuntimeError("must not start")

    monkeypatch.setattr(stdio, "stdio_client", fake_stdio_client)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(planted.project)]))
    save_servers(planted.home, {"files": ServerConfig("files", "stdio", command="npx")})
    manager = MCPManager(planted.home, {})
    try:
        assert manager.connect("files", timeout=10) == "not connected: npx not found on PATH"
    finally:
        manager.close()
    assert started == []


# -- #17: an automatic read-only allow is checked against the server's label on every connect ------------------

def _tool(name, read_only, destructive):
    return SimpleNamespace(name=name, description="d", inputSchema={"type": "object", "properties": {
        "sql": {"type": "string"}}}, annotations=SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive))


def _manager(home, entry, *tools):
    save_servers(home, {"s": ServerConfig("s", url="http://127.0.0.1:9/mcp", **entry)})
    manager = MCPManager(home, {})
    manager._live["s"] = _Live(session=None, tools=list(tools), stop=None, task=None)
    return manager


def test_a_tool_first_seen_read_only_goes_in_the_automatic_list(tmp_path):
    manager = _manager(tmp_path, {}, _tool("q", True, False), _tool("w", False, True))
    assert manager.review_tools("s") == (["q"], [])
    config = load_servers(tmp_path)[0]["s"]
    assert config.auto == ["q"] and config.allow == [] and sorted(config.seen) == ["q", "w"]
    assert manager.allowed_tools("s") == ["q"]


def test_an_automatic_allow_is_dropped_when_the_server_relabels_the_tool(tmp_path):
    manager = _manager(tmp_path, {"auto": ["q"], "seen": ["q"]}, _tool("q", False, True))
    assert manager.allowed_tools("s") == []                      # even before the review: the label decides
    assert manager.review_tools("s") == ([], ["q"])
    config = load_servers(tmp_path)[0]["s"]
    assert config.auto == [] and config.seen == ["q"]
    manager._live["s"].tools = [_tool("q", True, False)]           # read-only again: still asks (it is in seen)
    assert manager.review_tools("s") == ([], []) and manager.allowed_tools("s") == []


def test_the_users_own_allow_stays_when_the_label_changes(tmp_path):
    manager = _manager(tmp_path, {"allow": ["q"], "seen": ["q"]}, _tool("q", False, True))
    assert manager.review_tools("s") == ([], [])
    assert manager.allowed_tools("s") == ["q"] and load_servers(tmp_path)[0]["s"].allow == ["q"]


def test_an_old_files_read_only_allows_move_to_the_automatic_list_once(tmp_path):
    path = write(tmp_path / ".agentx" / "mcp.json", json.dumps({"servers": {"s": {
        "url": "http://127.0.0.1:9/mcp", "allow": ["q", "w"], "seen": ["q", "w"]}}}))   # before "auto" existed
    manager = MCPManager(tmp_path, {})
    tools = [_tool("q", True, False), _tool("w", False, False)]
    manager._live["s"] = _Live(session=None, tools=tools, stop=None, task=None)
    assert manager.allowed_tools("s") == ["q", "w"]                  # nothing changes for the user today
    assert manager.review_tools("s") == ([], [])
    config = load_servers(tmp_path)[0]["s"]
    assert config.auto == ["q"] and config.allow == ["w"]
    assert json.loads(path.read_text(encoding="utf-8"))["servers"]["s"]["auto"] == ["q"]
    assert manager.allowed_tools("s") == ["q", "w"]
    # Later the user allows the read-only tool themselves; the next connect does not move anything again.
    manager.set_allowed("s", ["q"], True)
    again = MCPManager(tmp_path, {})
    again._live["s"] = _Live(session=None, tools=tools, stop=None, task=None)
    assert again.review_tools("s") == ([], [])
    config = load_servers(tmp_path)[0]["s"]
    assert config.allow == ["q", "w"] and config.auto == []
    # And from now on the label is checked: relabelled, the moved tool would ask (see the tests above).


def test_an_old_file_is_only_moved_after_a_connect(tmp_path):
    write(tmp_path / ".agentx" / "mcp.json", json.dumps({"servers": {
        "s": {"url": "https://a.example", "allow": ["q"], "seen": ["q"]},
        "t": {"url": "https://b.example", "auto": ["x"], "seen": ["x"]}}}))
    manager = MCPManager(tmp_path, {})
    manager._live["t"] = _Live(session=None, tools=[_tool("x", False, True)], stop=None, task=None)
    manager.review_tools("t")                                         # saves the whole file
    data = json.loads((tmp_path / ".agentx" / "mcp.json").read_text(encoding="utf-8"))["servers"]
    assert "auto" not in data["s"] and data["s"]["allow"] == ["q"]   # still to be moved when s connects


def test_ask_takes_a_tool_out_of_both_lists_and_allow_makes_it_the_users_own(tmp_path):
    manager = _manager(tmp_path, {"auto": ["q"], "seen": ["q"]}, _tool("q", True, False))
    manager.set_allowed("s", ["q"], False)
    assert load_servers(tmp_path)[0]["s"].auto == [] and manager.allowed_tools("s") == []
    manager.set_allowed("s", ["q"], True)
    config = load_servers(tmp_path)[0]["s"]
    assert config.allow == ["q"] and config.auto == []


def test_a_relabelled_tool_asks_again_in_auto_mode_and_the_user_is_told(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=400, mode="auto")
    manager = _manager(home, {"auto": ["q"], "seen": ["q"]}, _tool("q", False, True))
    app.mcp = manager
    try:
        app._note_new_mcp_tools("s", quiet_if_none=True)             # what every connect and reload does
        app._refresh_mcp_tools(attach=False)
        assert "s__q" in [t.name for t in app.tools] and "s__q" not in app.gate.extra_allow
        assert "now asks first: the server no longer marks it read-only" in out.getvalue()
        asked = []
        gate = Gate(mode="auto", ask=lambda request: asked.append(request.tool) or Answer("n"))
        gate.extra_allow = list(app.gate.extra_allow)
        assert gate.check("s__q", {"sql": "delete"}) is not None and asked == ["s__q"]
    finally:
        manager.close()


def test_the_gate_follows_the_current_label_even_without_a_review(tmp_path):
    app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=400, mode="auto")
    manager = _manager(home, {"auto": ["q"], "seen": ["q"]}, _tool("q", True, False))
    app.mcp = manager
    try:
        app._refresh_mcp_tools(attach=False)
        assert app.gate.extra_allow == ["s__q"]
        manager._live["s"].tools = [_tool("q", False, True)]
        app._refresh_mcp_tools(attach=False)
        assert app.gate.extra_allow == []
    finally:
        manager.close()


# -- #19: a saved sign-in belongs to the address it was made for -----------------------------------------------

def _token(text="dummy-A"):
    from mcp.shared.auth import OAuthToken
    return OAuthToken(access_token=text, token_type="Bearer")


@needs_mcp
def test_a_sign_in_saved_before_the_address_was_kept_is_taken_for_the_current_one(store):
    asyncio.run(KeyringStorage("svc").set_tokens(_token()))           # an old sign-in: no address with it
    storage = KeyringStorage("svc")
    assert storage.bind("https://a.example/mcp") is False and storage.has_tokens()
    assert store["svc:url"] == "https://a.example/mcp"
    assert KeyringStorage("svc").bind("https://a.example/mcp") is False


@needs_mcp
def test_a_sign_in_for_another_address_is_deleted_not_sent(store):
    import httpx
    storage = KeyringStorage("svc")
    storage.bind("https://a.example/mcp")
    asyncio.run(storage.set_tokens(_token()))
    assert store["svc:url"] == "https://a.example/mcp"
    again = KeyringStorage("svc")
    assert again.bind("http://127.0.0.1:9/mcp-B") is True
    assert not again.has_tokens() and not any(key.startswith("svc:") for key in store)
    provider = mcp_module.oauth_provider(ServerConfig("svc", url="http://127.0.0.1:9/mcp-B"), again, False)

    async def first_request():
        flow = provider.async_auth_flow(httpx.Request("POST", "http://127.0.0.1:9/mcp-B"))
        request = await flow.__anext__()
        await flow.aclose()
        return request

    assert "authorization" not in {k.lower() for k in asyncio.run(first_request()).headers}


@needs_mcp
def test_connecting_after_the_url_changed_clears_the_sign_in_and_says_so(tmp_path, store, monkeypatch):
    import mcp.client.streamable_http as http
    save_servers(tmp_path, {"svc": ServerConfig("svc", url="https://a.example/mcp")})
    manager = MCPManager(tmp_path, {})
    storage = manager.storage("svc")
    storage.bind("https://a.example/mcp")
    asyncio.run(storage.set_tokens(_token()))
    save_servers(tmp_path, {"svc": ServerConfig("svc", url="http://127.0.0.1:9/mcp-B")})   # edited by hand
    handed = []

    def fake_provider(config, storage, interactive, **kw):
        handed.append((config.url, storage.has_tokens()))
        return None

    def offline(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(mcp_module, "oauth_provider", fake_provider)
    monkeypatch.setattr(http, "streamablehttp_client", offline)
    try:
        manager.reload("svc")
    finally:
        manager.close()
    assert handed == [("http://127.0.0.1:9/mcp-B", False)]
    notes = manager.take_notes()
    assert len(notes) == 1 and "/mcp login svc" in notes[0] and manager.take_notes() == []


@needs_mcp
def test_a_server_dropped_from_the_file_loses_its_sign_in(tmp_path, store):
    save_servers(tmp_path, {"svc": ServerConfig("svc", url="https://a.example/mcp"),
                            "other": ServerConfig("other", url="https://b.example/mcp")})
    manager = MCPManager(tmp_path, {})
    asyncio.run(manager.storage("svc").set_tokens(_token()))
    asyncio.run(manager.storage("other").set_tokens(_token("B")))
    write(tmp_path / ".agentx" / "mcp.json", "{ not json")              # a typo is not a removal
    manager.reload()
    assert KeyringStorage("svc").has_tokens() and KeyringStorage("other").has_tokens()
    save_servers(tmp_path, {"other": ServerConfig("other", url="https://b.example/mcp", headers={
        "Authorization": "Bearer ${NOT_SET_IN_THIS_TEST}"})})
    manager.servers["svc"] = ServerConfig("svc", url="https://a.example/mcp")
    try:
        manager.reload()
    finally:
        manager.close()
    assert not KeyringStorage("svc").has_tokens() and KeyringStorage("other").has_tokens()
    assert any("svc is no longer in mcp.json" in note for note in manager.take_notes())


@needs_mcp
def test_reloading_one_dropped_name_loses_its_sign_in(tmp_path, store):
    save_servers(tmp_path, {"svc": ServerConfig("svc", url="https://a.example/mcp")})
    manager = MCPManager(tmp_path, {})
    asyncio.run(manager.storage("svc").set_tokens(_token()))
    save_servers(tmp_path, {})
    try:
        assert manager.reload("svc") == {}
    finally:
        manager.close()
    assert "svc" not in manager.servers and not KeyringStorage("svc").has_tokens()


@needs_mcp
def test_adding_a_server_under_a_used_name_starts_without_a_sign_in(tmp_path, store):
    asyncio.run(KeyringStorage("svc").set_tokens(_token()))           # left from a server removed by hand
    manager = MCPManager(tmp_path, {})
    manager.add(ServerConfig("svc", url="https://c.example/mcp"))
    assert not KeyringStorage("svc").has_tokens() and not manager.storage("svc").has_tokens()


@needs_mcp
def test_the_address_is_kept_with_a_new_sign_in_and_cleared_on_logout(tmp_path, store):
    save_servers(tmp_path, {"svc": ServerConfig("svc", url="https://a.example/mcp")})
    manager = MCPManager(tmp_path, {})
    storage = manager.storage("svc")
    storage.bind("https://a.example/mcp")
    assert "svc:url" not in store                                     # nothing saved for a server never signed in
    asyncio.run(storage.set_tokens(_token()))
    assert store["svc:url"] == "https://a.example/mcp"
    try:
        manager.logout("svc")
    finally:
        manager.close()
    assert store == {}


# -- #18: a parent folder's AGENTX.md needs that folder to be trusted ------------------------------------------

def _found(cwd, home):
    return [p.resolve() for p in find_context_files(cwd, home)]


def test_an_untrusted_parent_folders_file_is_not_read(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    shared = tmp_path / "shared"
    (shared / "proj").mkdir(parents=True)
    write(shared / "AGENTX.md", "PLANTED-BY-OTHER")
    write(shared / "proj" / "AGENTX.md", "PROJECT-RULE")
    assert remember_trust(shared / "proj", home)
    assert is_trusted(shared / "proj", home) and not is_trusted(shared, home)
    assert (shared / "AGENTX.md").resolve() not in _found(shared / "proj", home)
    text = build_addendum(shared / "proj", home, system="X")
    assert "PLANTED-BY-OTHER" not in text
    assert text.index(OPEN_MARK) < text.index("PROJECT-RULE") < text.index(CLOSE_MARK)


def test_a_trusted_parent_folders_file_is_read(tmp_path):
    home = tmp_path / "home"
    write(tmp_path / "work" / "AGENTX.md", "WORK-RULE")
    cwd = tmp_path / "work" / "a" / "b"
    cwd.mkdir(parents=True)
    write(tmp_path / "work" / "a" / "AGENTX.md", "A-RULE")
    remember_trust(cwd, home)
    assert "WORK-RULE" not in build_addendum(cwd, home, system="X")
    remember_trust(tmp_path / "work", home)                         # the folders inside it are trusted too
    text = build_addendum(cwd, home, system="X")
    assert text.index("WORK-RULE") < text.index("A-RULE")


def test_the_users_own_files_are_still_read(tmp_path):
    home = tmp_path / "home"
    write(home / ".agentx" / "AGENTX.md", "USER-OWN")
    write(home / "AGENTX.md", "HOME-LEVEL")
    write(home / "Downloads" / "AGENTX.md", "DOWNLOADED")
    cwd = home / "Downloads" / "repo"
    write(cwd / "AGENTX.md", "REPO")
    remember_trust(cwd, home)
    text = build_addendum(cwd, home, system="X")
    assert "USER-OWN" in text and "HOME-LEVEL" in text and "REPO" in text
    assert "DOWNLOADED" not in text


def test_the_climb_still_stops_at_dot_git_even_when_the_parent_is_trusted(tmp_path):
    home = tmp_path / "home"
    write(tmp_path / "shared" / "AGENTX.md", "OUTER")
    write(tmp_path / "shared" / "proj" / ".git" / "HEAD", "ref")
    cwd = tmp_path / "shared" / "proj"
    remember_trust(tmp_path / "shared", home)
    assert "OUTER" not in build_addendum(cwd, home, system="X")
