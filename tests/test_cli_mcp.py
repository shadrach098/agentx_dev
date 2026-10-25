"""MCP servers in the terminal app: ~/.agentx/mcp.json, /mcp add|remove|login|logout, the sign-in kept in the
credential store, the servers' tools offered (gated) to the agent and to team helpers. The agent cannot add a
server or sign in. The end-to-end tests run a real MCP server (tests/fixtures/mcp_echo_server.py) over stdio."""

import json
import sys
from pathlib import Path

import pytest

from agentx_dev.cli import mcp as mcp_module
from agentx_dev.cli.approval import Answer, Gate
from agentx_dev.cli.commands import _words, run_command
from agentx_dev.cli.mcp import (KeyringStorage, MCPManager, MissingVariable, ServerConfig, caution, config_path,
                                expand, load_servers, parse_server, save_servers, tool_name, valid_name)
from tests.conftest import MockModel
from tests.test_cli_app import Lines, make_app

ECHO_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False
needs_mcp = pytest.mark.skipif(not HAVE_MCP, reason="the mcp package is not installed")


def echo_config(name="echo"):
    return ServerConfig(name, "stdio", command=sys.executable, args=[str(ECHO_SERVER)])


class TestSettingsFile:
    def test_names(self):
        assert valid_name("flightclaw") and valid_name("my-server_2")
        for bad in ("", "Flight", "-x", "a b", "x" * 41, "../x"):
            assert not valid_name(bad)

    def test_save_and_load_round_trip(self, tmp_path):
        servers = {"web": ServerConfig("web", "http", url="https://a.example/mcp",
                                       headers={"Authorization": "Bearer ${A_KEY}"}),
                   "old": ServerConfig("old", "sse", url="https://b.example/sse"),
                   "local": ServerConfig("local", "stdio", command="npx", args=["-y", "pkg"], env={"K": "${K}"})}
        path = save_servers(tmp_path, servers)
        assert path == config_path(tmp_path)
        loaded, problems = load_servers(tmp_path)
        assert problems == [] and loaded == servers
        assert "Bearer ${A_KEY}" in path.read_text()            # the variable, never its value

    def test_bad_entries_are_skipped_with_a_reason(self, tmp_path):
        config_path(tmp_path).parent.mkdir(parents=True)
        config_path(tmp_path).write_text(json.dumps({"servers": {
            "ok": {"url": "https://a.example"}, "Bad Name": {"url": "https://x"}, "nourl": {"url": "ftp://x"},
            "badargs": {"command": "x", "args": "notalist"}}}))
        loaded, problems = load_servers(tmp_path)
        assert list(loaded) == ["ok"] and len(problems) == 3

    def test_a_broken_file_loads_nothing_and_says_why(self, tmp_path):
        config_path(tmp_path).parent.mkdir(parents=True)
        config_path(tmp_path).write_text("{nope")
        loaded, problems = load_servers(tmp_path)
        assert loaded == {} and "no MCP servers were loaded" in problems[0]

    def test_variables_come_from_the_environment(self):
        assert expand("Bearer ${A_KEY}", {"A_KEY": "s3cret"}) == "Bearer s3cret"
        with pytest.raises(MissingVariable):
            expand("${NOPE}", {})

    def test_parse_rejects_non_text_headers(self):
        config, why = parse_server("x", {"url": "https://a", "headers": {"A": 1}})
        assert config is None and "headers" in why


class FakeKeyring:
    def __init__(self):
        self.data = {}

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        assert len(value) <= 1_000 or value.startswith("chunks:")
        self.data[(service, key)] = value

    def delete_password(self, service, key):
        self.data.pop((service, key), None)


class TestSignInStorage:
    @needs_mcp
    def test_tokens_go_to_the_credential_store_split_when_long_and_clear_removes_them(self, monkeypatch):
        import asyncio
        from mcp.shared.auth import OAuthToken
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        token = OAuthToken(access_token="a" * 2500, token_type="Bearer", refresh_token="r" * 900)
        asyncio.run(KeyringStorage("svc").set_tokens(token))
        assert ("agentx-mcp", "svc:tokens") in fake.data and fake.data[("agentx-mcp", "svc:tokens")].startswith("chunks:")
        again = asyncio.run(KeyringStorage("svc").get_tokens())                # a fresh object reads the store
        assert again.access_token == "a" * 2500 and KeyringStorage("svc").has_tokens()
        KeyringStorage("svc").clear()
        assert fake.data == {}

    def test_without_a_credential_store_it_keeps_the_sign_in_for_the_session(self, monkeypatch):
        monkeypatch.setattr(mcp_module, "_keyring", lambda: None)
        storage = KeyringStorage("svc")
        storage._set("tokens", "{}")
        assert storage.has_tokens() and not KeyringStorage("svc").has_tokens()


class TestToolNames:
    def test_names_are_safe_short_and_unique(self):
        taken = set()
        assert tool_name("flight", "search flights!", taken) == "flight__search_flights_"
        assert tool_name("flight", "search flights?", taken) == "flight__search_flights__2"
        assert len(tool_name("s" * 40, "t" * 60)) == 64

    def test_a_sign_in_request_at_startup_is_reported_not_opened(self, tmp_path):
        manager = MCPManager(tmp_path, {})
        manager._needs_login.add("flight")
        assert manager._reason("flight", RuntimeError("x")) == "needs sign-in: /mcp login flight"
        assert "no answer in time" in manager._reason("other", TimeoutError())
        assert "${K}" in manager._reason("other", MissingVariable("K"))


@needs_mcp
class TestARealServer:
    def test_connect_list_call_and_close(self, tmp_path):
        manager = MCPManager(tmp_path, {})
        manager.add(echo_config())
        try:
            assert manager.connect_all() == {"echo": "connected, 3 tools"}
            tools = {tool.name: tool for tool in manager.tools()}
            assert set(tools) == {"echo__echo", "echo__add", "echo__wipe"}
            assert tools["echo__echo"].func(text="hi") == caution("echo") + "echo: hi"
            assert tools["echo__add"].func(a=2, b=3).endswith("5")
            assert tools["echo__add"].description.startswith("[MCP server 'echo', added by the user]")
            assert (tmp_path / ".agentx" / "mcp-logs" / "echo.log").exists()      # its messages, off the screen
        finally:
            manager.close()
        assert tools["echo__echo"].func(text="x").startswith("ERROR: the MCP server 'echo' is not connected")

    def test_a_server_that_does_not_start_is_reported(self, tmp_path):
        manager = MCPManager(tmp_path, {})
        manager.add(ServerConfig("broken", "stdio", command=sys.executable, args=["-c", "import sys; sys.exit(3)"]))
        try:
            assert manager.connect_all()["broken"].startswith("not connected")
            assert manager.tools() == []
        finally:
            manager.close()


@needs_mcp
class TestInTheApp:
    def app(self, tmp_path, *answers, mode="default"):
        app, out, cwd, home = make_app(tmp_path, Lines(*answers), MockModel(), width=400, mode=mode)
        app.model = app.model_factory(None, {})
        app._start_fresh()
        return app, out, home

    def test_no_servers_says_how_to_add_one(self, tmp_path):
        app, out, home = self.app(tmp_path)
        run_command(app, "/mcp")
        assert "No MCP servers yet. Add one with /mcp add" in out.getvalue()

    def test_add_a_local_server_after_a_yes_and_its_tools_reach_the_agent_gated(self, tmp_path):
        app, out, home = self.app(tmp_path, "y", mode="auto")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            text = out.getvalue()
            assert "MCP echo: connected, 3 tools" in text
            assert "1 of its 3 tools run without asking" in text
            assert load_servers(home)[0]["echo"].args == [str(ECHO_SERVER)]
            assert "echo__add" in [t.name for t in app.session.runner.tools]      # the conversation's agent has it
            tool = next(t for t in app.tools if t.name == "echo__add")
            asked = []
            app.gate.ask = lambda request: asked.append(request) or Answer("n")
            assert "declined" in tool.func(a=1, b=2).lower()
            assert asked and asked[0].tool == "echo__add"             # unlabelled: asks, even in auto mode
            app.gate.allow.append("echo__*")
            assert tool.func(a=1, b=2).endswith("3")                  # the user's own allow rule lets it through
            run_command(app, "/mcp")
            assert "echo__add" in out.getvalue() and "/mcp tools <name>" in out.getvalue()
        finally:
            app.mcp.close()

    def test_a_no_adds_nothing(self, tmp_path):
        app, out, home = self.app(tmp_path, "n")
        run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
        assert "Nothing was added." in out.getvalue() and not config_path(home).exists()

    def test_remove_after_a_yes(self, tmp_path):
        app, out, home = self.app(tmp_path, "y", "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            run_command(app, "/mcp remove echo")
            assert "Removed echo." in out.getvalue() and load_servers(home)[0] == {}
            assert not any(t.name.startswith("echo__") for t in app.tools)
        finally:
            app.mcp.close()

    def test_team_helpers_get_the_tools(self, tmp_path):
        from agentx_dev.cli.team import build_helpers
        app, out, home = self.app(tmp_path, "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            helpers = build_helpers(MockModel(), [*app.base_tools, *app._mcp_tools], "BASE")
            for _, runner in helpers.values():
                assert "echo__echo" in [t.name for t in runner.tools]
        finally:
            app.mcp.close()


class TestTheAgentCannotAddServers:
    def test_no_tool_touches_mcp_settings(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        names = {tool.name for tool in app.tools}
        assert not any("mcp" in name for name in names)

    def test_the_settings_file_is_protected_from_the_agents_file_tools(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), mode="auto")
        import os
        write = next(t for t in app.tools if t.name == "write_file")
        target = home / ".agentx" / "mcp.json"
        # Written as an absolute path (refused on Windows; on Linux a rooted path outside the project is read as
        # one inside it) and as a relative path that climbs out of the project (refused): never the real file.
        for path in (str(target), os.path.relpath(target, cwd)):
            write.func(path=path, content="{}", if_exists="overwrite")
        assert not target.exists()


class TestCommandWords:
    def test_backslashes_stay_and_quotes_group(self):
        assert _words(r'add x -- "C:\Program Files\node.exe" C:\a\b.js') == \
            ["add", "x", "--", r"C:\Program Files\node.exe", r"C:\a\b.js"]

    @pytest.mark.parametrize("line", ["/mcp add", "/mcp add x", "/mcp add x ftp://a", "/mcp add x https://a --y",
                                      "/mcp remove", "/mcp frob x", '/mcp add x "unclosed'])
    def test_bad_lines_show_the_usage(self, tmp_path, line):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel(), width=400)
        run_command(app, line)
        assert "Usage: /mcp" in out.getvalue()


@needs_mcp
class TestClientSecret:
    """Supabase issues a client secret to a client that asked for none, and does not say how to send it; the sign-in
    then failed with 'Required parameter: client_secret'. Such a secret is sent in the token request."""

    def info(self, **kw):
        from mcp.shared.auth import OAuthClientInformationFull
        return OAuthClientInformationFull(client_id="c", redirect_uris=["http://localhost:33418/callback"], **kw)

    def test_a_secret_without_a_method_is_posted(self, monkeypatch):
        import asyncio
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        asyncio.run(KeyringStorage("supa").set_client_info(self.info(client_secret="s3cret")))
        stored = asyncio.run(KeyringStorage("supa").get_client_info())
        assert stored.token_endpoint_auth_method == "client_secret_post" and stored.client_secret == "s3cret"

    def test_one_saved_before_the_fix_is_mended_when_read(self, monkeypatch):
        import asyncio
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        fake.set_password("agentx-mcp", "supa:client", self.info(client_secret="s3cret").model_dump_json())
        assert asyncio.run(KeyringStorage("supa").get_client_info()).token_endpoint_auth_method == "client_secret_post"

    def test_a_public_client_and_a_stated_method_are_left_alone(self):
        from agentx_dev.cli.mcp import _secret_sent
        assert _secret_sent(self.info(token_endpoint_auth_method="none")).token_endpoint_auth_method == "none"
        assert _secret_sent(self.info(client_secret="s", token_endpoint_auth_method="client_secret_basic")) \
            .token_endpoint_auth_method == "client_secret_basic"


@needs_mcp
class TestAnExpiredSignInIsRenewed:
    """A saved token only says how long it lasted from when it was issued, and the SDK loaded it as valid forever: an
    expired token was sent, the server said 401 and agentx reported 'needs sign-in' although a renewal token was
    saved. The moment it runs out is now kept, and the SDK is told, so it renews instead."""

    def token(self, expires_in=3600):
        from mcp.shared.auth import OAuthToken
        return OAuthToken(access_token="a", token_type="Bearer", expires_in=expires_in, refresh_token="r")

    def test_saving_records_when_it_runs_out(self, monkeypatch):
        import asyncio
        import time
        monkeypatch.setattr(mcp_module, "_keyring", lambda: FakeKeyring())
        storage = KeyringStorage("svc")
        asyncio.run(storage.set_tokens(self.token(3600)))
        assert 3500 < storage.expires_at() - time.time() < 3600              # a minute early

    def test_a_token_saved_before_the_time_was_kept_has_no_time(self, monkeypatch):
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        fake.set_password("agentx-mcp", "svc:tokens", self.token().model_dump_json())
        assert KeyringStorage("svc").expires_at() is None            # sent as it is until the server says no

    def test_a_token_that_never_runs_out_has_no_time(self, monkeypatch):
        import asyncio
        monkeypatch.setattr(mcp_module, "_keyring", lambda: FakeKeyring())
        storage = KeyringStorage("svc")
        asyncio.run(storage.set_tokens(self.token(None)))
        assert storage.expires_at() is None

    def metadata(self):
        from mcp.shared.auth import OAuthMetadata
        return OAuthMetadata(issuer="https://api.example", authorization_endpoint="https://api.example/authorize",
                             token_endpoint="https://api.example/v1/oauth/token")

    def test_a_run_out_token_is_renewed_at_the_saved_address(self, monkeypatch):
        """Supabase signs in on api.supabase.com; after a restart the SDK guessed mcp.supabase.com/token (404)."""
        import asyncio
        import time
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        storage = KeyringStorage("svc")
        storage.set_metadata(self.metadata().model_dump_json())
        fake.set_password("agentx-mcp", "svc:tokens", self.token().model_dump_json())
        fake.set_password("agentx-mcp", "svc:expires_at", str(time.time() - 10))       # ran out
        provider = mcp_module.oauth_provider(ServerConfig("svc", url="https://mcp.example/mcp"), storage, False)
        asyncio.run(provider._initialize())
        assert str(provider.context.oauth_metadata.token_endpoint) == "https://api.example/v1/oauth/token"
        assert not provider.context.is_token_valid()                    # so with client info and a renewal token,
        provider.context.client_info = object()                         # the SDK renews, at the saved address
        assert provider.context.can_refresh_token()

    def test_without_the_address_a_run_out_token_is_sent_rather_than_renewed_at_a_guess(self, monkeypatch):
        import asyncio
        import time
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        fake.set_password("agentx-mcp", "svc:tokens", self.token().model_dump_json())
        fake.set_password("agentx-mcp", "svc:expires_at", str(time.time() - 10))
        provider = mcp_module.oauth_provider(ServerConfig("svc", url="https://mcp.example/mcp"),
                                             KeyringStorage("svc"), False)
        asyncio.run(provider._initialize())
        assert provider.context.token_expiry_time is None and provider.context.is_token_valid()

    def test_the_address_is_saved_when_the_sdk_finds_it(self, monkeypatch):
        import asyncio
        import httpx
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        storage = KeyringStorage("svc")
        provider = mcp_module.oauth_provider(ServerConfig("svc", url="https://mcp.example/mcp"), storage, False)
        response = httpx.Response(200, content=self.metadata().model_dump_json().encode())
        asyncio.run(provider._handle_oauth_metadata_response(response))
        assert "api.example/v1/oauth/token" in storage.metadata()

    def test_clear_forgets_the_time_too(self, monkeypatch):
        import asyncio
        fake = FakeKeyring()
        monkeypatch.setattr(mcp_module, "_keyring", lambda: fake)
        asyncio.run(KeyringStorage("svc").set_tokens(self.token()))
        KeyringStorage("svc").clear()
        assert fake.data == {}


class TestToolKinds:
    def tool(self, **hints):
        from types import SimpleNamespace
        return SimpleNamespace(name="t", annotations=SimpleNamespace(**{"readOnlyHint": None, "destructiveHint": None,
                                                                       **hints}) if hints else None)

    def test_the_servers_labels_decide(self):
        from agentx_dev.cli.mcp import tool_kind
        assert tool_kind(self.tool(readOnlyHint=True, destructiveHint=False)) == "read-only"
        assert tool_kind(self.tool(readOnlyHint=False, destructiveHint=True)) == "destructive"
        assert tool_kind(self.tool(readOnlyHint=True, destructiveHint=True)) == "destructive"   # the stricter word
        assert tool_kind(self.tool(readOnlyHint=False, destructiveHint=False)) == "changes"
        assert tool_kind(self.tool()) == "unlabelled"

    def test_the_lists_are_saved_and_read_back(self, tmp_path):
        save_servers(tmp_path, {"f": ServerConfig("f", url="https://a.example", allow=["search"],
                                                  seen=["search", "cancel"])})
        config = load_servers(tmp_path)[0]["f"]
        assert config.allow == ["search"] and config.seen == ["cancel", "search"]


@needs_mcp
class TestTheAllowList:
    """The first time agentx sees a server's tools it lets the ones the server marks read-only run without asking,
    saves that in mcp.json, and never adds back one the user took out."""

    def app(self, tmp_path, *answers):
        app, out, cwd, home = make_app(tmp_path, Lines(*answers), MockModel(), width=400, mode="auto")
        app.model = app.model_factory(None, {})
        app._start_fresh()
        return app, out, home

    def test_read_only_tools_run_without_asking_the_rest_ask(self, tmp_path):
        app, out, home = self.app(tmp_path, "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            config = load_servers(home)[0]["echo"]
            assert config.allow == ["echo"] and set(config.seen) == {"echo", "add", "wipe"}
            asked = []
            app.gate.ask = lambda request: asked.append(request.tool) or Answer("n")
            tools = {t.name: t for t in app.tools}
            assert tools["echo__echo"].func(text="hi").endswith("echo: hi") and asked == []
            tools["echo__wipe"].func(what="x")
            assert asked == ["echo__wipe"]
        finally:
            app.mcp.close()

    def test_tools_lists_them_and_ask_takes_one_out_for_good(self, tmp_path):
        app, out, home = self.app(tmp_path, "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            run_command(app, "/mcp tools echo")
            text = out.getvalue()
            assert "Run without asking (1):\n  echo  (only reads)" in text
            assert "wipe  (deletes or cancels)" in text and "add  (not labelled by the server)" in text
            run_command(app, "/mcp ask echo echo")
            assert load_servers(home)[0]["echo"].allow == []
            assert "echo__echo" not in app.gate.extra_allow
            app.mcp.connect("echo")                                   # seen again: not added back
            assert app.mcp.note_new_tools("echo") == [] and load_servers(home)[0]["echo"].allow == []
        finally:
            app.mcp.close()

    def test_allowing_a_destructive_tool_asks_first(self, tmp_path):
        app, out, home = self.app(tmp_path, "y", "n", "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            run_command(app, "/mcp allow echo all")                   # "n" to wipe: the others still go in
            assert set(load_servers(home)[0]["echo"].allow) == {"echo", "add"}
            run_command(app, "/mcp allow echo echo__wipe")            # "y"; the full name works too
            assert "wipe" in load_servers(home)[0]["echo"].allow and "echo__wipe" in app.gate.extra_allow
        finally:
            app.mcp.close()

    def test_a_deny_rule_still_wins(self, tmp_path):
        app, out, home = self.app(tmp_path, "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            app.gate.deny.append("echo__echo")
            assert next(t for t in app.tools if t.name == "echo__echo").func(text="hi").startswith("Refused")
        finally:
            app.mcp.close()

    def test_an_unknown_tool_name_changes_nothing(self, tmp_path):
        app, out, home = self.app(tmp_path, "y")
        try:
            run_command(app, f'/mcp add echo -- "{sys.executable}" "{ECHO_SERVER}"')
            run_command(app, "/mcp allow echo nope")
            assert "has no tool called nope" in out.getvalue() and load_servers(home)[0]["echo"].allow == ["echo"]
        finally:
            app.mcp.close()
