"""A tiny MCP server for the tests: run as a program, it serves two tools over stdio."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(text: str) -> str:
    """Send the text back."""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two whole numbers."""
    return a + b


if __name__ == "__main__":
    server.run()
