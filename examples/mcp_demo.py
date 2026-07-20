"""End-to-end MCP demo.

Connects to an MCP server, lists its tools/resources/prompts, and wires
the tools into an AsyncAgentRunner so the agent can actually call them.

Three scenarios:

  1. ``list_tools`` / ``list_resources`` / ``list_prompts`` — pure
     introspection of what a server exposes.
  2. Wire MCP tools into ``AsyncAgentRunner`` and run a one-shot task
     that requires a tool call.
  3. Use ``resource_as_tool`` to let the agent fetch a specific MCP
     resource on demand.

If ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY`` is set, a real LLM drives
the scenarios. Otherwise a ``StubModel`` scripts the conversation so the
demo still runs with no credentials.

MCP server: spawns the reference filesystem server via npx if it's
available; otherwise uses an in-process ``FakeMCPSession`` so the demo
works on any machine.

Run:
    python examples/mcp_demo.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import AsyncExitStack

from agentx_dev.Agents.Agent import AgentType
from agentx_dev.AsyncTools import AsyncStructuredTool
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.MCP import MCPClient, mcp_tool_to_async_structured_tool
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner


# ---------------------------------------------------------------
# In-process fake MCP session — used when no npx server is available.
# ---------------------------------------------------------------

class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [_FakeContent(text)]
        self.isError = is_error


class _FakeMCPTool:
    def __init__(self, name, description, schema):
        self.name = name
        self.description = description
        self.inputSchema = schema


class _FakeReadResult:
    def __init__(self, text: str):
        self.contents = [_FakeContent(text)]


class _FakeResource:
    def __init__(self, uri, name, description, mime_type):
        self.uri = uri
        self.name = name
        self.description = description
        self.mimeType = mime_type


class FakeMCPSession:
    """Pretends to be a connected ClientSession. Two tools, one resource."""

    def __init__(self):
        self._tools = [
            _FakeMCPTool(
                name="list_allowed_directories",
                description="Returns the list of directories that this server is allowed to access.",
                schema={"type": "object", "properties": {}},
            ),
            _FakeMCPTool(
                name="list_files",
                description="List files in a directory.",
                schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            ),
            _FakeMCPTool(
                name="word_count",
                description="Return the word count of a text string.",
                schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            ),
        ]
        self._resources = [
            _FakeResource("memo://onboarding", "Onboarding notes",
                          "Internal onboarding doc", "text/plain"),
        ]
        self.tool_calls: List = []

    async def list_tools(self):
        class _R:
            pass
        r = _R()
        r.tools = self._tools
        return r

    async def list_resources(self):
        class _R:
            pass
        r = _R()
        r.resources = self._resources
        return r

    async def read_resource(self, uri: str):
        return _FakeReadResult(
            "Welcome aboard! Day 1 is orientation. Day 2 is environment setup."
        )

    async def call_tool(self, name: str, args: Dict[str, Any]):
        self.tool_calls.append((name, args))
        if name == "list_allowed_directories":
            return _FakeCallResult("Allowed directories:\n  - /fake/sandbox")
        if name == "list_files":
            path = args.get("path", ".")
            return _FakeCallResult(f"[{path}]\n  - greeting.py\n  - readme.md\n  - data.json")
        if name == "word_count":
            text = args.get("text", "")
            return _FakeCallResult(str(len(text.split())))
        return _FakeCallResult(f"(no fake implementation for {name})", is_error=True)


# ---------------------------------------------------------------
# Stub LLM — scripts a sensible tool-use sequence without an API key.
# ---------------------------------------------------------------

class StubModel(BaseChatModel):
    def __init__(self, scripted: List[str]):
        self._scripted = iter(scripted)

    def Initialize(self, messages) -> str:
        raise NotImplementedError("Stub only supports async_initialize")

    async def async_initialize(self, messages) -> str:
        return next(self._scripted)


def pick_llm():
    if os.getenv("ANTHROPIC_API_KEY"):
        from agentx_dev.ChatModel import Claude
        return Claude(model="claude-haiku-4-5-20251001", max_tokens=512), "Claude (live API)"
    if os.getenv("OPENAI_API_KEY"):
        from agentx_dev.ChatModel import GPT
        return GPT(model="gpt-4o-mini", temperature=0), "GPT (live API)"
    # The script uses tool names that exist BOTH on the npx
    # @modelcontextprotocol/server-filesystem (when npx is available) AND on
    # the in-process FakeMCPSession further down. ``list_allowed_directories``
    # is the safest filesystem-server tool to call (no required args).
    scripted = [
        json.dumps({
            "Thought": "Let me see which directories the server lets me touch.",
            "action": "list_allowed_directories",
            "action_input": {},
        }),
        json.dumps({
            "Thought": "Got it. I have what I need to answer.",
            "action": "Final_Answer",
            "action_input": "The MCP server gave me the allowed-directories list "
                            "via list_allowed_directories. Demo successful.",
        }),
    ]
    return StubModel(scripted), "StubModel (no API key — scripted responses)"


# ---------------------------------------------------------------
# Server selection — real npx server if available, else fake.
# ---------------------------------------------------------------

async def build_client() -> tuple[MCPClient, str]:
    sandbox = Path(__file__).parent / "_sandbox_mcp"
    sandbox.mkdir(exist_ok=True)
    (sandbox / "greeting.py").write_text("print('hi')\n", encoding="utf-8")
    (sandbox / "readme.md").write_text("Welcome to the project.\n", encoding="utf-8")

    if shutil.which("npx"):
        try:
            client = await MCPClient.connect_stdio(
                "npx", "-y", "@modelcontextprotocol/server-filesystem", str(sandbox)
            )
            return client, f"npx @modelcontextprotocol/server-filesystem {sandbox}"
        except Exception as e:
            print(f"  (could not start real MCP server: {e}; falling back to fake)")

    fake = FakeMCPSession()
    return MCPClient(session=fake, exit_stack=AsyncExitStack()), "FakeMCPSession (in-process)"


# ---------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------

async def scenario_introspection(mcp: MCPClient):
    print("\n" + "=" * 70)
    print("Scenario 1: list what the MCP server exposes")
    print("=" * 70)

    tools = await mcp.list_tools()
    print(f"\n  Tools ({len(tools)}):")
    for t in tools:
        print(f"    - {t.name} :: {t.description}")

    try:
        resources = await mcp.list_resources()
        print(f"\n  Resources ({len(resources)}):")
        for r in resources:
            print(f"    - {r['uri']} ({r.get('mimeType', '?')}) — {r.get('description', '')}")
    except Exception as e:
        print(f"\n  Resources: server does not support resources/list ({e})")

    try:
        prompts = await mcp.list_prompts()
        print(f"\n  Prompts ({len(prompts)}):")
        for p in prompts:
            args_brief = ", ".join(a["name"] for a in p["arguments"])
            print(f"    - {p['name']}({args_brief}) — {p.get('description', '')}")
    except Exception as e:
        print(f"\n  Prompts: server does not support prompts/list ({e})")


async def scenario_agent_uses_mcp_tools(mcp: MCPClient, llm, label: str):
    print("\n" + "=" * 70)
    print("Scenario 2: AsyncAgentRunner consumes MCP-backed tools")
    print("=" * 70)

    tools = await mcp.list_tools()
    runner = AsyncAgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=tools,
        max_iterations=5,
        auto_cache=False,
        verbose=False,
    )

    task = (
        "Ask the MCP server which directories it lets you touch by calling "
        "list_allowed_directories, then summarize the result."
    )
    print(f"\n  Task : {task}")
    print(f"  LLM  : {label}\n")

    result = await runner.ainvoke(task)

    print(f"  Final answer: {result.content}")
    print(f"  Steps taken : {len(result.steps)}")
    for i, step in enumerate(result.steps, 1):
        print(f"    {i}. {step}")
    print(f"  Tool calls  : {len(result.tool_calls)}")
    for tc in result.tool_calls:
        result_preview = tc.result if len(tc.result) < 120 else tc.result[:120] + "…"
        print(f"    - {tc.name} → {result_preview}")


async def scenario_resource_as_tool(mcp: MCPClient):
    print("\n" + "=" * 70)
    print("Scenario 3: expose an MCP resource as an on-demand tool")
    print("=" * 70)
    try:
        resources = await mcp.list_resources()
    except Exception as e:
        print(f"\n  Skipping — server has no resources support: {e}")
        return
    if not resources:
        print("\n  Skipping — server has no resources.")
        return

    uri = resources[0]["uri"]
    print(f"\n  Wrapping resource: {uri}")
    tool = mcp.resource_as_tool(uri, name="read_onboarding", description="Read onboarding notes.")
    print(f"  Tool name: {tool.name}")
    print(f"  Tool description: {tool.description}")
    # Call it directly to show the round trip.
    content = await tool.func()
    print(f"\n  Tool returned (first 120 chars):\n    {content[:120]}")


async def main():
    print("Building MCP client…")
    mcp, server_label = await build_client()
    llm, llm_label = pick_llm()
    try:
        print(f"Server: {server_label}")
        print(f"LLM   : {llm_label}")

        await scenario_introspection(mcp)
        await scenario_agent_uses_mcp_tools(mcp, llm, llm_label)
        await scenario_resource_as_tool(mcp)
    finally:
        await mcp.close()


if __name__ == "__main__":
    asyncio.run(main())
