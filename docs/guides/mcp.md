# Guide: connect an MCP server

MCP (Model Context Protocol) lets you plug in tool servers without
writing tool code yourself. The framework's `MCPClient` supports stdio
(local subprocess), Server-Sent Events, and plain HTTP transports.


> **Both providers work.** Every `Claude()` in this page also works
> with `GPT()`. Same tools, same agent code, same runner APIs. Set
> whichever API key you have (`ANTHROPIC_API_KEY` for Claude,
> `OPENAI_API_KEY` for GPT) and swap the constructor. See
> [chat models](../concepts/models.md) for adding other providers.

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

## Runnable examples

Two end-to-end demos ship in `examples/`:

| File | What it shows |
|---|---|
| `examples/mcp_demo.py` | Minimal stdio server integration — the shortest path from `pip install` to a working MCP-backed agent. |
| `examples/mcp_github_triage_demo.py` | Production-shaped example — connects to the official MCP GitHub server, whitelists 3 of the ~26 advertised tools (`list_issues` / `get_issue` / `add_issue_labels`), scrubs the subprocess env down to the GitHub token, has the model classify open issues into a fixed taxonomy (bug/feature/docs/question/duplicate/needs-info + P0/P1/P2), then pauses at a HITL approval gate before writing labels back. |

### GitHub triage demo — key ideas worth stealing

Whether or not you copy the file, three patterns from it apply to any
MCP-backed agent that touches a real system:

1. **Whitelist tools before handing them to the model.** The MCP
   GitHub server exposes ~26 tools including `delete_file`,
   `create_pull_request`, `fork_repository`. Fewer tools = tighter
   ReAct planning AND smaller blast radius on a prompt injection
   that talked the model into reaching for something destructive:

   ```python
   allowed = {"list_issues", "get_issue", "add_issue_labels"}
   picked = [t for t in await mcp.list_tools() if t.name in allowed]
   ```

2. **Scrub the subprocess env.** Don't hand the child MCP server
   every `*_API_KEY` in your shell — pass only what it needs.
   Mirrors the framework's own 3.0.6 `_build_child_env` defence
   for `run_python` / `run_shell`:

   ```python
   subprocess_env = {"GITHUB_PERSONAL_ACCESS_TOKEN": os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"]}
   for k in ("PATH", "SystemRoot", "HOME"):
       if k in os.environ:
           subprocess_env[k] = os.environ[k]
   await MCPClient.connect_stdio("npx", "-y", "@modelcontextprotocol/server-github", env=subprocess_env)
   ```

3. **HITL gate the side-effectful step.** For anything that writes
   to a real system (labels, comments, PRs, files), have the model
   assemble the full plan first, then call an `ask_human` tool that
   returns the operator's approval. Reject → agent returns the plan
   as its final answer without writing anything. Fails closed when
   there's no TTY.

Prereqs for the triage demo: Node/`npx`, `GITHUB_PERSONAL_ACCESS_TOKEN`
with `repo` scope, `pip install agentx-dev[mcp]`, and either
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
