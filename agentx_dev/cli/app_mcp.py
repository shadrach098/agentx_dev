"""MCP servers: connecting, listing, adding, allowing, signing in and out, reloading.

A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it."""

from __future__ import annotations

from typing import List, Optional

from agentx_dev.cli.mcp import INSTALL_HINT, KINDS, MCPManager, ServerConfig, config_path, valid_name
from agentx_dev.cli.tools import gate_tool


class McpCommands:
    """Mixed into ``ChatApp``, whose attributes these methods use."""

    def _mcp_manager(self) -> MCPManager:
        if self.mcp is None:
            self.mcp = MCPManager(self.home, self.env, show=self._mcp_show)
        return self.mcp

    def _mcp_show(self, text: str) -> None:
        with self.progress.paused():
            for line in str(text).splitlines():
                self.notice(self._safe(line, 2000))

    def _connect_mcp(self) -> None:
        """At startup: connect the servers in ~/.agentx/mcp.json (never opening a browser) and add their tools."""
        try:
            manager = self._mcp_manager()
        except Exception as error:                 # a broken file must not stop the app
            self.error(f"MCP servers were not loaded ({type(error).__name__}).")
            return
        for problem in manager.problems:
            self.error(self._safe(problem, 300))
        if not manager.servers:
            return
        self.progress.start("Connecting to MCP servers")
        try:
            statuses = manager.connect_all()
        finally:
            self.progress.stop()
        signin = [name for name, status in statuses.items() if status.startswith("needs sign-in")]
        down = [name for name, status in statuses.items()
                if not status.startswith("connected") and name not in signin]
        if signin:
            many = len(signin) != 1
            self.screen.warning(f"{len(signin)} MCP server{'s' if many else ''} need{'' if many else 's'} sign-in: "
                                f"{self._safe(', '.join(signin), 200)}",
                                f"run /mcp login {signin[0]}" if not many else "run /mcp login <name>")
        for name in down:
            self.screen.warning(f"MCP {self._safe(name, 40)}: {self._safe(statuses[name], 300)}", "run /mcp")
        for name, status in statuses.items():
            if status.startswith("connected"):
                self._note_new_mcp_tools(name, quiet_if_none=True)
        self._refresh_mcp_tools(attach=False)

    def _note_new_mcp_tools(self, name: str, quiet_if_none: bool = False) -> None:
        """Allow the read-only tools ``name`` shows for the first time, and say what runs without asking."""
        manager = self._mcp_manager()
        added = manager.note_new_tools(name)
        if quiet_if_none and not added:
            return
        kinds = manager.tool_kinds(name)
        allowed = [tool for tool in kinds if tool in manager.servers[name].allow]
        asking = len(kinds) - len(allowed)
        self.notice(f"MCP {self._safe(name, 40)}: {len(allowed)} of its {len(kinds)} tools run without asking (the "
                    f"server marks them read-only); {asking} ask first. /mcp tools {name} lists them; /mcp allow and "
                    "/mcp ask change one.")

    def _refresh_mcp_tools(self, attach: bool = True) -> None:
        """Put the connected servers' tools (gated) in ``self.tools``; with ``attach``, give the conversation a
        runner that has them, keeping its history."""
        old = {id(tool) for tool in self._mcp_tools}
        self.tools = [tool for tool in self.tools if id(tool) not in old]
        fresh = []
        for tool in (self.mcp.tools() if self.mcp is not None else []):
            wrapped = gate_tool(tool, self.gate, observer=self._observe_tool)
            wrapped.mcp_server, wrapped.mcp_remote = tool.mcp_server, tool.mcp_remote
            fresh.append(wrapped)
        self._mcp_tools = fresh
        self.tools.extend(fresh)
        # The servers' allowed tools run without asking: exact names, kept apart from the user's own rules.
        servers = self.mcp.servers if self.mcp is not None else {}
        self.gate.extra_allow = [tool.name for tool in fresh
                                 if tool.mcp_remote in getattr(servers.get(tool.mcp_server), "allow", [])]
        if attach and self.session is not None and self.model is not None:
            self.session.attach(self._make_runner(self.model))

    def show_mcp(self) -> None:
        manager = self._mcp_manager()
        path = self._safe(config_path(self.home), 300)
        if not manager.servers:
            self.notice(f"No MCP servers yet. Add one with /mcp add <name> <url> (or /mcp add <name> -- <command> "
                        f"[args]). They are kept in {path}.")
            return
        lines = ["MCP servers:"]
        for name, config in manager.servers.items():
            status = manager.status.get(name, "not connected")
            lines.append(f"  {self._safe(name, 40)}  {self._safe(config.describe(), 200)}  - {self._safe(status, 200)}")
        if self._mcp_tools:
            names = ", ".join(tool.name for tool in self._mcp_tools[:12])
            more = f" and {len(self._mcp_tools) - 12} more" if len(self._mcp_tools) > 12 else ""
            lines.append(f"Tools: {self._safe(names, 800)}{more}.")
        lines.append(f"Kept in {path}. Tools a server marks read-only run without asking; the rest ask, even in auto "
                     "mode. /mcp tools <name> lists a server's tools, /mcp allow|ask <name> <tool> changes one, "
                     "/mcp login|logout|remove <name>.")
        self.notice("\n".join(lines))

    def mcp_add(self, name: str, config: ServerConfig) -> None:
        manager = self._mcp_manager()
        if not valid_name(name):
            self.error("An MCP server name is lower-case letters, digits, - and _ (at most 40), starting with a "
                       "letter or digit.")
            return
        if name in manager.servers:
            status = manager.status.get(name, "not connected")
            self.error(f"There is already an MCP server called {self._safe(name, 40)} ({self._safe(status, 120)}). "
                       f"/mcp login {name} signs in again; /mcp remove {name} deletes it.")
            return
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.error(INSTALL_HINT)
            return
        if config.transport == "stdio" and not self._yes(
                f"This runs `{self._safe(config.describe(), 300)}` on your computer each time agentx starts, and "
                "its tools act for the agent. Add it? [y/N] "):
            self.notice("Nothing was added.")
            return
        try:
            path = manager.add(config)
        except (OSError, ValueError) as error:
            self.error(f"Could not save the server ({type(error).__name__}).")
            return
        self.notice(f"Added {self._safe(name, 40)} to {self._safe(path, 300)}. Connecting...")
        status = manager.connect(name)
        if status.startswith("needs sign-in") and self._yes(
                f"{name} wants you to sign in. Open the sign-in page in your browser now? [y/N] "):
            status = manager.login(name)
        self.notice(f"MCP {self._safe(name, 40)}: {self._safe(status, 300)}")
        if status.startswith("connected"):
            self._note_new_mcp_tools(name)
        self._refresh_mcp_tools()

    def _known_server(self, name: str) -> bool:
        if name in self._mcp_manager().servers:
            return True
        self.error(f"No MCP server called {self._safe(name, 40)}. /mcp lists them.")
        return False

    def show_mcp_tools(self, name: str) -> None:
        """``/mcp tools <name>``: every tool of a connected server, what the server says it does, and whether it
        runs without asking."""
        if not self._known_server(name):
            return
        manager = self._mcp_manager()
        kinds = manager.tool_kinds(name)
        if not kinds:
            self.notice(f"{name} is not connected ({self._safe(manager.status.get(name, 'not connected'), 200)}), so "
                        "its tools are not known yet.")
            return
        allow = manager.servers[name].allow
        free = [tool for tool in kinds if tool in allow]
        asking = [tool for tool in kinds if tool not in allow]
        lines = [f"{name}: {len(kinds)} tools. Run without asking ({len(free)}):"]
        lines += [f"  {self._safe(tool, 60)}  ({KINDS[kinds[tool]]})" for tool in free] or ["  (none)"]
        lines.append(f"Ask first ({len(asking)}):")
        lines += [f"  {self._safe(tool, 60)}  ({KINDS[kinds[tool]]})" for tool in asking] or ["  (none)"]
        lines.append(f"/mcp allow {name} <tool> [more...] lets tools run without asking (or: read-only, all); "
                     f"/mcp ask {name} <tool> [more...] makes them ask (or: all). Saved in "
                     f"{self._safe(config_path(self.home), 300)}. A deny rule in your settings still wins.")
        self.notice("\n".join(lines))

    def mcp_allow(self, name: str, words: List[str], allowed: bool) -> None:
        """``/mcp allow|ask <name> <tool...>`` (``all``; for allow also ``read-only``)."""
        if not self._known_server(name):
            return
        manager = self._mcp_manager()
        kinds = manager.tool_kinds(name)
        known = set(kinds) | set(manager.servers[name].seen)
        prefix = f"{name}__"
        picked: List[str] = []
        for word in words:
            if word == "all":
                picked += list(kinds) or list(known)
            elif word == "read-only" and allowed:
                picked += [tool for tool, kind in kinds.items() if kind == "read-only"]
            else:
                tool = word[len(prefix):] if word.startswith(prefix) else word
                if tool not in known:
                    self.error(f"{name} has no tool called {self._safe(word, 60)}. /mcp tools {name} lists them.")
                    return
                picked.append(tool)
        picked = list(dict.fromkeys(picked))
        if not picked:
            self.notice("Nothing changed.")
            return
        risky = [tool for tool in picked if kinds.get(tool) == "destructive"]
        if allowed and risky and not self._yes(
                f"{', '.join(self._safe(t, 40) for t in risky[:8])} {'deletes or cancels' if len(risky) == 1 else 'delete or cancel'} "
                "things (the server says so). Let them run without asking? [y/N] "):
            picked = [tool for tool in picked if tool not in risky]
            if not picked:
                self.notice("Nothing changed.")
                return
        try:
            manager.set_allowed(name, picked, allowed)
        except (OSError, ValueError) as error:
            self.error(f"Could not save the change ({type(error).__name__}).")
            return
        self._refresh_mcp_tools()
        what = "run without asking" if allowed else "ask first"
        self.notice(f"{len(picked)} {name} tool{'s' if len(picked) != 1 else ''} now {what}: "
                    f"{self._safe(', '.join(picked[:12]), 600)}{' ...' if len(picked) > 12 else ''}.")

    def mcp_remove(self, name: str) -> None:
        if not self._known_server(name):
            return
        if not self._yes(f"Remove the MCP server {name} and its saved sign-in? [y/N] "):
            self.notice("Nothing was removed.")
            return
        self._mcp_manager().remove(name)
        self._refresh_mcp_tools()
        self.notice(f"Removed {self._safe(name, 40)}.")

    def mcp_login(self, name: str) -> None:
        if not self._known_server(name):
            return
        manager = self._mcp_manager()
        if manager.servers[name].transport == "stdio":
            self.notice(f"{name} runs on your computer; there is nothing to sign in to. Reconnecting.")
        status = manager.login(name)
        self.notice(f"MCP {self._safe(name, 40)}: {self._safe(status, 300)}")
        if status.startswith("connected"):
            self._note_new_mcp_tools(name, quiet_if_none=True)
        self._refresh_mcp_tools()

    def mcp_reload(self, name: Optional[str] = None) -> None:
        """``/mcp reload [name]``: read ~/.agentx/mcp.json again and reconnect (with the saved sign-ins; no browser),
        then give the agent the tools. On Windows the variables saved for the user since agentx started (setx) are
        read too, so a ``${VAR}`` set after the start is found."""
        manager = self._mcp_manager()
        env = dict(self.env)
        try:
            saved = self._user_env_reader()
        except Exception:
            saved = {}
        for key, value in saved.items():
            env.setdefault(key, value)                 # what this run already has wins
        self.progress.start("Reconnecting MCP servers")
        try:
            statuses = manager.reload(name, env=env)
        finally:
            self.progress.stop()
        for problem in manager.problems:
            self.error(self._safe(problem, 300))
        if name is not None and not statuses:
            self.error(f"No MCP server named {self._safe(name, 40)!r}. /mcp lists them.")
            return
        if not statuses:
            self.notice(f"No MCP servers in {self._safe(config_path(self.home), 300)}. Add one with /mcp add.")
            self._refresh_mcp_tools()
            return
        lines = ["MCP servers reloaded:"]
        lines += [f"  {self._safe(server, 40)}  - {self._safe(status, 200)}" for server, status in statuses.items()]
        self.notice("\n".join(lines))
        for server, status in statuses.items():
            if status.startswith("connected"):
                self._note_new_mcp_tools(server, quiet_if_none=True)
        self._refresh_mcp_tools()

    def mcp_logout(self, name: str) -> None:
        if not self._known_server(name):
            return
        self._mcp_manager().logout(name)
        self._refresh_mcp_tools()
        self.notice(f"Signed out of {self._safe(name, 40)}: its saved sign-in was deleted. /mcp login {name} signs "
                    "in again.")
