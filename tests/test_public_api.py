"""The public API is written down in docs/reference/public-api.md. These tests fail when the package and that page
disagree, so adding, removing or renaming a public name is always a deliberate change to both."""

import importlib
import re
from pathlib import Path

import agentx_dev

PAGE = Path(__file__).resolve().parent.parent / "docs" / "reference" / "public-api.md"
OPTIONAL = {"MCPClient", "json_schema_to_pydantic", "mcp_tool_to_async_structured_tool"}   # need the mcp package


def block(marker: str) -> str:
    text = PAGE.read_text(encoding="utf-8")
    found = re.search(re.escape(marker) + r".*?\n```text\n(.*?)\n```", text, re.S)
    assert found, f"no block after {marker!r} in {PAGE}"
    return found.group(1)


def documented_names() -> set:
    return set(block("<!-- public-names").split())


def test_every_exported_name_is_documented_and_every_documented_name_is_exported():
    exported, documented = set(agentx_dev.__all__), documented_names()
    assert not exported - documented, f"exported but not on the public-API page: {sorted(exported - documented)}"
    assert not documented - exported - OPTIONAL, f"documented but not exported: {sorted(documented - exported)}"


def test_every_documented_name_imports():
    for name in documented_names() - OPTIONAL:
        assert hasattr(agentx_dev, name), name


def test_the_stable_paths_below_the_package_import():
    for line in block("<!-- public-paths").splitlines():
        module_name, _, names = line.partition(":")
        module = importlib.import_module(module_name.strip())
        for name in names.split():
            assert hasattr(module, name), f"{module_name}.{name}"


def test_the_list_is_sorted_so_a_diff_shows_what_changed():
    names = block("<!-- public-names").split()
    assert names == sorted(names)
