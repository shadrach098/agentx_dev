"""MCP (Model Context Protocol) integration for agentx_dev.

Plugs MCP servers into the agent as tool sources. Each tool a server exposes
becomes an ``AsyncStructuredTool`` the runner can dispatch through the
existing ``ToolRegistry.adispatch`` path — no special-casing in the loop.

Usage::

    from agentx_dev.MCP import MCPClient
    from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner

    async with MCPClient.connect_stdio(
        "npx", "-y", "@modelcontextprotocol/server-filesystem", "/some/path"
    ) as mcp:
        tools = await mcp.list_tools()
        runner = AsyncAgentRunner(model=llm, agent=AgentType.ReAct, tools=tools)
        await runner.ainvoke("List the files in /some/path")

Optional dependency. ``import agentx_dev.MCP`` only fails when you call a
method that needs the SDK — pure data helpers (the JSON-Schema-to-Pydantic
converter) work without it, so this module is testable without the SDK
installed.

Scope of this first slice (#21):
  - stdio transport only. HTTP/SSE deferred — same MCPClient surface,
    different constructor.
  - tools/list + tools/call. resources/* and prompts/* deferred —
    they're useful but a separate concern from agent tool dispatch.
  - JSON-Schema-to-Pydantic conversion covers top-level objects with
    typed primitive properties (string/integer/number/boolean/array/
    object) plus required-list handling. Nested object schemas degrade
    to ``Any`` — same restriction LangChain's MCP adapter has.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, Field, create_model

from agentx_dev.AsyncTools import AsyncStructuredTool


_JSON_SCHEMA_TYPE_MAP: Dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def json_schema_to_pydantic(schema: Dict[str, Any], model_name: str) -> Type[BaseModel]:
    """Build a Pydantic ``BaseModel`` from an MCP tool's inputSchema.

    Handles the common case — a top-level ``"type": "object"`` with
    ``properties`` and ``required`` — and degrades non-primitive types to
    ``Any``. Used both for agentx tool wrappers and tested directly so
    schema-mapping regressions show up immediately.
    """
    if not isinstance(schema, dict):
        return create_model(model_name, __base__=BaseModel)

    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])

    fields: Dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        prop_schema = prop_schema or {}
        json_type = prop_schema.get("type", "string")
        py_type = _JSON_SCHEMA_TYPE_MAP.get(json_type, Any)
        description = prop_schema.get("description")

        if prop_name in required:
            field_info = Field(..., description=description) if description else Field(...)
            fields[prop_name] = (py_type, field_info)
        else:
            default = prop_schema.get("default")
            field_info = Field(default, description=description) if description else Field(default)
            fields[prop_name] = (Optional[py_type], field_info)

    if not fields:
        return create_model(model_name, __base__=BaseModel)
    return create_model(model_name, **fields)


def _content_to_text(content_blocks) -> str:
    """Collapse an MCP CallToolResult.content list into a single string.

    MCP returns a list of typed content blocks (text / image / resource).
    For agent consumption we string-join the text bits and stringify the
    rest. Non-text content (images, embedded resources) is preserved as a
    fallback repr so the model sees *something* rather than empty output.
    """
    if not content_blocks:
        return ""
    parts: List[str] = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(repr(block))
    return "\n".join(parts)


def mcp_tool_to_async_structured_tool(
    mcp_tool: Any,
    session: Any,
) -> AsyncStructuredTool:
    """Wrap one MCP tool descriptor as an ``AsyncStructuredTool``.

    The closure binds ``session`` and the tool name so the call survives
    being passed around the registry. Errors from the server (``isError``
    in the CallToolResult) are re-raised as ``RuntimeError`` so the
    runner's dispatch path classifies them as ``ToolError`` via its normal
    exception path — no MCP-specific code in the runner.
    """
    name = mcp_tool.name
    description = mcp_tool.description or f"MCP tool: {name}"
    schema_dict = mcp_tool.inputSchema or {}
    args_schema = json_schema_to_pydantic(schema_dict, f"_MCPArgs_{name}")

    async def _call(**kwargs: Any) -> str:
        result = await session.call_tool(name, kwargs)
        if getattr(result, "isError", False):
            msg = _content_to_text(getattr(result, "content", []))
            raise RuntimeError(f"MCP tool '{name}' returned error: {msg}")
        return _content_to_text(getattr(result, "content", []))

    _call.__name__ = name
    return AsyncStructuredTool(
        func=_call,
        args_schema=args_schema,
        name=name,
        description=description,
    )


class MCPClient:
    """Async client for an MCP server.

    Use ``connect_stdio`` to spawn a subprocess server. Always await
    ``close()`` (or use as an async context manager) so the subprocess
    + pipes get cleaned up.

    This class is intentionally thin — the real work happens in
    ``mcp_tool_to_async_structured_tool`` and the MCP SDK's ClientSession.
    Future transports (HTTP, SSE) add alternate constructors but reuse
    the same instance methods.
    """

    def __init__(self, session: Any, exit_stack: AsyncExitStack):
        self._session = session
        self._exit_stack = exit_stack
        self._tools_cache: Optional[List[AsyncStructuredTool]] = None

    @classmethod
    async def _from_transport_streams(cls, exit_stack: AsyncExitStack, read, write) -> "MCPClient":
        """Shared finalization for every transport: wrap the (read, write)
        streams in a ClientSession, initialize, and return the client."""
        try:
            from mcp import ClientSession
        except ImportError as e:
            raise ImportError(
                "MCPClient requires the 'mcp' package. "
                "Install with: pip install mcp"
            ) from e

        try:
            session = await exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await exit_stack.aclose()
            raise
        return cls(session, exit_stack)

    @classmethod
    async def connect_stdio(
        cls,
        command: str,
        *args: str,
        env: Optional[Dict[str, str]] = None,
    ) -> "MCPClient":
        """Spawn ``command [args...]`` as a subprocess MCP server and
        return a connected client. Raises ``ImportError`` if the
        ``mcp`` package isn't installed."""
        try:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise ImportError(
                "MCPClient.connect_stdio requires the 'mcp' package. "
                "Install with: pip install mcp"
            ) from e

        params = StdioServerParameters(command=command, args=list(args), env=env)
        exit_stack = AsyncExitStack()
        try:
            read, write = await exit_stack.enter_async_context(stdio_client(params))
        except Exception:
            await exit_stack.aclose()
            raise
        return await cls._from_transport_streams(exit_stack, read, write)

    @classmethod
    async def connect_sse(
        cls,
        url: str,
        *,
        headers: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
        sse_read_timeout: float = 300.0,
    ) -> "MCPClient":
        """Connect to a remote MCP server over Server-Sent Events.

        Usage::

            mcp = await MCPClient.connect_sse(
                "https://my-mcp-server.example.com/sse",
                headers={"Authorization": "Bearer …"},
            )

        ``timeout`` is the HTTP connect timeout. ``sse_read_timeout`` is
        the SSE stream read timeout (default 5 minutes — long enough for
        a server that's idle between tool calls).
        """
        try:
            from mcp.client.sse import sse_client
        except ImportError as e:
            raise ImportError(
                "MCPClient.connect_sse requires the 'mcp' package. "
                "Install with: pip install mcp"
            ) from e

        exit_stack = AsyncExitStack()
        try:
            streams = await exit_stack.enter_async_context(
                sse_client(url, headers=headers, timeout=timeout, sse_read_timeout=sse_read_timeout)
            )
            read, write = streams[0], streams[1]
        except Exception:
            await exit_stack.aclose()
            raise
        return await cls._from_transport_streams(exit_stack, read, write)

    @classmethod
    async def connect_http(
        cls,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
    ) -> "MCPClient":
        """Connect to a remote MCP server over streamable HTTP.

        This is the modern MCP transport (replaces plain SSE for new
        servers). Same surface as ``connect_sse`` — just a different URL
        scheme and longer default timeout.
        """
        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as e:
            raise ImportError(
                "MCPClient.connect_http requires the 'mcp' package. "
                "Install with: pip install mcp"
            ) from e

        exit_stack = AsyncExitStack()
        try:
            # streamablehttp_client yields (read, write, get_session_id);
            # we only need read + write.
            streams = await exit_stack.enter_async_context(
                streamablehttp_client(url, headers=headers, timeout=timeout, sse_read_timeout=sse_read_timeout)
            )
            read, write = streams[0], streams[1]
        except Exception:
            await exit_stack.aclose()
            raise
        return await cls._from_transport_streams(exit_stack, read, write)

    async def __aenter__(self) -> "MCPClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Shut down the underlying subprocess + close pipes."""
        await self._exit_stack.aclose()

    async def list_tools(self, refresh: bool = False) -> List[AsyncStructuredTool]:
        """Fetch the server's tool list, wrapped as AsyncStructuredTool.

        Result is cached after the first call. Pass ``refresh=True`` to
        re-fetch (e.g. after a server restart).
        """
        if self._tools_cache is not None and not refresh:
            return list(self._tools_cache)

        result = await self._session.list_tools()
        tools = [mcp_tool_to_async_structured_tool(t, self._session) for t in result.tools]
        self._tools_cache = tools
        return list(tools)

    # ------------------------------------------------------------------
    # Resources — MCP servers can also expose readable resources (files,
    # database rows, API responses). Useful as context an agent can pull
    # mid-conversation rather than as actions it can take.
    # ------------------------------------------------------------------

    async def list_resources(self) -> List[Dict[str, Any]]:
        """Return every resource the server advertises.

        Each entry is a dict with ``uri``, ``name``, ``description``,
        ``mimeType`` (any may be missing — depends on the server).
        """
        result = await self._session.list_resources()
        out: List[Dict[str, Any]] = []
        for r in result.resources:
            out.append({
                "uri": str(getattr(r, "uri", "")),
                "name": getattr(r, "name", None),
                "description": getattr(r, "description", None),
                "mimeType": getattr(r, "mimeType", None),
            })
        return out

    async def read_resource(self, uri: str) -> str:
        """Fetch a resource by URI and return its text content.

        Binary resources (images, blobs) are stringified via ``repr`` as a
        fallback so callers always get *something*; treat the return as
        opaque text and decode separately if you need bytes.
        """
        result = await self._session.read_resource(uri)
        return _content_to_text(getattr(result, "contents", []))

    # ------------------------------------------------------------------
    # Prompts — MCP servers can expose pre-baked prompt templates with
    # named arguments. Useful as system-prompt seeds or as one-shot
    # user messages parameterized by the agent.
    # ------------------------------------------------------------------

    async def list_prompts(self) -> List[Dict[str, Any]]:
        """Return every prompt template the server advertises.

        Each entry has ``name``, ``description``, and ``arguments``
        (list of ``{name, description, required}`` dicts describing the
        prompt's parameters).
        """
        result = await self._session.list_prompts()
        out: List[Dict[str, Any]] = []
        for p in result.prompts:
            args = []
            for a in getattr(p, "arguments", []) or []:
                args.append({
                    "name": getattr(a, "name", None),
                    "description": getattr(a, "description", None),
                    "required": getattr(a, "required", False),
                })
            out.append({
                "name": getattr(p, "name", None),
                "description": getattr(p, "description", None),
                "arguments": args,
            })
        return out

    async def get_prompt(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """Render a prompt template into a list of messages.

        Returns an agentx-shaped message list (``[{role, content}, …]``)
        suitable to pass directly as ``chat_history`` to a runner or as
        the body of a one-shot ``model.invoke(...)`` call.
        """
        result = await self._session.get_prompt(name, arguments or {})
        messages: List[Dict[str, str]] = []
        for msg in getattr(result, "messages", []):
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", None)
            text = _content_to_text([content]) if content is not None else ""
            messages.append({"role": role, "content": text})
        return messages

    def resource_as_tool(
        self,
        uri: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> AsyncStructuredTool:
        """Expose a specific MCP resource as a no-arg StructuredTool the
        agent can decide to fetch on its own.

        Useful when you want the agent to be able to *pull* a resource at
        runtime (vs. eagerly reading every resource and dumping it into
        the prompt). The wrapped tool takes no arguments — calling it
        reads the URI and returns the contents.
        """
        from pydantic import BaseModel as _BM

        class _NoArgs(_BM):
            """No arguments — this resource is identified by its URI."""

        tool_name = name or f"read_{uri.replace(':', '_').replace('/', '_').replace('.', '_')}"
        tool_desc = description or f"Read the MCP resource at {uri}"
        session = self._session

        async def _read(**_kwargs) -> str:
            result = await session.read_resource(uri)
            return _content_to_text(getattr(result, "contents", []))

        _read.__name__ = tool_name
        return AsyncStructuredTool(
            func=_read,
            args_schema=_NoArgs,
            name=tool_name,
            description=tool_desc,
        )
