"""A tiny MCP server for the tests: run as a program, it serves three tools over stdio. ``echo`` is marked read-only,
``wipe`` destructive, and ``add`` carries no labels."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("echo")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def echo(text: str) -> str:
    """Send the text back."""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two whole numbers."""
    return a + b


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def wipe(what: str) -> str:
    """Pretend to delete something."""
    return f"wiped {what}"


if __name__ == "__main__":
    server.run()
