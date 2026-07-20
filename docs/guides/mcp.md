# Guide: connect an MCP server

MCP (Model Context Protocol) lets you plug in tool servers without
writing tool code yourself. The framework's `MCPClient` supports stdio
(local subprocess), Server-Sent Events, and plain HTTP transports.

## Install

MCP is an optional extra:

```bash
pip install agentx-dev[mcp]
```

## Stdio (local subprocess — most common)

The official filesystem MCP server, for example:

```python
import asyncio
from agentx_dev import AsyncAgentRunner, AgentType, Claude, MCPClient

async def main():
    async with MCPClient.connect_stdio(
        "npx", "-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir",
    ) as mcp:
        tools = await mcp.list_tools()   # auto-discovers what the server offers

        runner = AsyncAgentRunner(
            model=Claude(),
            agent=AgentType.ReAct,
            tools=tools,
        )
        result = await runner.ainvoke("List the files in /path/to/dir")
        print(result.content)

asyncio.run(main())
```

Tools returned by `list_tools()` are already wrapped as
`AsyncStructuredTool` instances, ready to pass into a runner.

## HTTP / SSE (remote MCP server)

```python
mcp = await MCPClient.connect_http(
    "https://my-mcp-server.example.com/mcp",
    headers={"Authorization": "Bearer your-token"},
)

mcp = await MCPClient.connect_sse(
    "https://my-mcp-server.example.com/sse",
    headers={"Authorization": "Bearer your-token"},
)
```

Same context-manager pattern; same `.list_tools()` API.

## Resources

MCP servers can expose readable resources (files, URIs, database rows):

```python
async with MCPClient.connect_stdio(...) as mcp:
    resources = await mcp.list_resources()
    content = await mcp.read_resource("file:///readme.md")
```

To make a resource available as an on-demand tool:

```python
readme_tool = mcp.resource_as_tool("file:///readme.md")

runner = AsyncAgentRunner(
    model=Claude(), agent=AgentType.ReAct,
    tools=[*await mcp.list_tools(), readme_tool],
)
```

The model gets a tool it can call to fetch the resource; the framework
handles the read.

## Prompts

MCP servers can also expose pre-baked prompt templates:

```python
prompts = await mcp.list_prompts()
messages = await mcp.get_prompt("summarize", {"text": "..."})
# messages is a list of {"role": ..., "content": ...} dicts you can feed to a model.
```

## Mixing MCP with your own tools

```python
async with MCPClient.connect_stdio(
    "npx", "-y", "@modelcontextprotocol/server-github",
) as mcp:
    github_tools = await mcp.list_tools()

    runner = AsyncAgentRunner(
        model=Claude(),
        agent=AgentType.ReAct,
        tools=[
            *github_tools,       # from the MCP server
            weather_tool,        # your custom tool
            calc_tool,           # another custom
        ],
        permissions=Permissions.full_access(["./workspace"]),  # DefaultTools too
    )
```

## Long-lived MCP connections

For a server you want to keep open across many agent invocations:

```python
mcp = await MCPClient.connect_stdio("npx", "-y", "@modelcontextprotocol/server-filesystem", "/data")

try:
    tools = await mcp.list_tools()
    runner = AsyncAgentRunner(model=Claude(), agent=AgentType.ReAct, tools=tools)

    for question in questions:
        result = await runner.ainvoke(question)
        print(result.content)
finally:
    await mcp.close()
```

## Debugging MCP tools

Turn on framework observability — every MCP tool dispatch fires
`TOOL_CALL_START` / `TOOL_CALL_END` events:

```python
from agentx_dev import config, observability, ConsoleHook
config.observability_enabled = True
observability.add_hook(ConsoleHook(verbose=True))
```

## Common issues

- **`npx: command not found`** — install Node.js. `stdio` servers are
  usually npm packages.
- **Tools have empty descriptions** — the MCP server author didn't
  provide them; you can rewrite them before registering:
  `for t in tools: t.description = "..."`.
- **Server crashes on startup** — check the server's own stderr;
  `MCPClient` surfaces it.
- **Timeouts** — MCP tool calls default to no timeout. Set
  `tool.timeout_sec` on each tool if needed.

## Runnable example

See `examples/mcp_demo.py` for a full stdio server integration.
