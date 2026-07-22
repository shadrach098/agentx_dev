"""
A lightweight framework for building LLM-powered agents that can use tools.

This module provides the necessary components to define tools, format them for
an agent prompt, and execute them within a loop controlled by an AgentRunner.
It leverages Pydantic for structured data validation and generating schemas
for function-calling APIs like OpenAI's.
"""

from typing import Dict, Callable, List, Type
from pydantic import BaseModel,Field

import logging

logger = logging.getLogger(__name__)


# --- Component 1: Pydantic Schema Generation for OpenAI Functions ---




class Model:
    """
    A wrapper to convert a Pydantic BaseModel into an OpenAI function specification.

    This class takes a Pydantic model and extracts its JSON schema to produce
    a `functions` list compatible with OpenAI's function-calling API.

    Attributes:
        model (Type[BaseModel]): The Pydantic model class provided during instantiation.
    """
    def __init__(self, model: Type[BaseModel]):
        """
        Initializes the Model wrapper.

        Args:
            model (Type[BaseModel]): The Pydantic model class to be wrapped.
        """
        self.model = model

    @classmethod
    def with_structured_output(cls, model: Type[BaseModel]) -> List[Dict]:
        """Legacy OpenAI `functions` shape — kept for backwards compatibility.

        Prefer ``to_openai_tool`` / ``to_anthropic_tool`` for the modern
        tool-calling APIs.
        """
        return [{
            "name": model.__name__,
            "description": (model.__doc__ or f"Structured output for {model.__name__}").strip(),
            "parameters": model.model_json_schema(),
        }]

    @classmethod
    def to_openai_tool(cls, model: Type[BaseModel], description: str | None = None) -> Dict:
        """Modern OpenAI tools-API spec for any Pydantic model."""
        from agentx_dev.Agents.Agent import to_openai_tool as _to
        return _to(model, description)

    @classmethod
    def to_anthropic_tool(cls, model: Type[BaseModel], description: str | None = None) -> Dict:
        """Anthropic tool spec for any Pydantic model."""
        from agentx_dev.Agents.Agent import to_anthropic_tool as _to
        return _to(model, description)


# --- Component 2: Tool Definitions ---

class StandardTool:
    """
    Represents a simple tool with a name, description, and a function to execute.
    This tool type does not have structured (Pydantic-based) arguments.
    """
    def __init__(self,  func: Callable, name: str |None = None, description: str | None = None):
        """
        Initializes a StandardTool.

        Args:
            name (str): The name of the tool, used by the LLM to identify it.
            func (Callable): The Python function to execute when the tool is called.
            description (str): A description of what the tool does, for the LLM to understand its purpose.
        """
        self.func = func
        if name:
            self.name = name
        elif getattr(func, "__name__", None) and func.__name__ != "<lambda>":
            self.name = func.__name__
        else:
            raise ValueError("Tool name is required (the supplied function has no usable __name__)")
        temp = description if description else self.func.__doc__
        if temp:
            self.description = temp
        else:
            raise ValueError("description needed cant be NONE")
        self.__name__ = 'StandardTool'

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the tool."""
        return (
            f"StandardTool(name={self.name!r}, description={self.description!r}, "
            f"func={self.func!r})")


class StructuredTool:
    """
    Represents a tool that requires structured arguments, defined by a Pydantic model.
    """
    def __init__(
        self,
        func: Callable ,
        args_schema: Type[BaseModel] ,
        name: str |None =None,
        description: str | None =None
    ):
        """
        Initializes a StructuredTool.

        Args:
            name (str): The name of the tool.
            description (str): A description of the tool's purpose.
            func (Callable): The Python function to execute. It should accept arguments
                             that match the fields in the `args_schema`.
            args_schema (Type[BaseModel]): A Pydantic model that defines the expected
                                           arguments for the tool.
        """
        self.func = func
        if name:
            self.name = name
        elif getattr(func, "__name__", None) and func.__name__ != "<lambda>":
            self.name = func.__name__
        else:
            raise ValueError("Tool name is required (the supplied function has no usable __name__)")
        temp = description if description else self.func.__doc__
        if temp:
            self.description = temp
        else:
            raise ValueError("description needed cant be NONE")
        self.args_schema = args_schema
        self.__name__ = 'StructuredTool'

    def to_openai_tool(self, description: str | None = None) -> Dict:
        """OpenAI tools-API spec built from this tool's args_schema."""
        from agentx_dev.Agents.Agent import to_openai_tool as _to
        spec = _to(self.args_schema, description or self.description)
        spec["function"]["name"] = self.name
        return spec

    def to_anthropic_tool(self, description: str | None = None) -> Dict:
        """Anthropic tool spec built from this tool's args_schema."""
        from agentx_dev.Agents.Agent import to_anthropic_tool as _to
        spec = _to(self.args_schema, description or self.description)
        spec["name"] = self.name
        return spec

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the tool."""
        return (
            f"StructuredTool(name={self.name!r}, description={self.description!r}, "
            f"args_schema={self.args_schema.__name__}, func={self.func!r})")


# --- Component 4: Prompt Management ---

from typing import List, Dict, Any

# # Suggestion: Placing local application imports after standard library imports is a common convention.


# ---------------------------------------------------------------------------
# Convenience re-exports so `agentx_dev.Tools` is a one-stop namespace
#
# Users can now import any tool from this module:
#
#     from agentx_dev.Tools import (
#         StandardTool, StructuredTool,          # wrappers (defined above)
#         web_search_tool, web_fetch_tool,       # web
#         vector_search_tool,                    # RAG
#         handoff_tool,                          # multi-agent
#         DefaultTools, Permissions,             # sandboxed filesystem tools
#     )
#
# The top-level imports (from agentx_dev import ...) still work; this is
# purely additive so callers who prefer the namespaced form get it too.
# Lazy-import-inside-getattr keeps import time low and avoids circular
# imports with modules that already import from Tools.py.
# ---------------------------------------------------------------------------

_TOOL_EXPORTS = {
    # Web tools
    "web_search_tool":  ("agentx_dev.WebTools",     "web_search_tool"),
    "web_fetch_tool":   ("agentx_dev.WebTools",     "web_fetch_tool"),
    # RAG
    "vector_search_tool": ("agentx_dev.Embeddings", "vector_search_tool"),
    # Multi-agent handoffs
    "handoff_tool":     ("agentx_dev.Handoffs",     "handoff_tool"),
    # Async tool wrappers (siblings to Standard/Structured)
    "AsyncStandardTool":   ("agentx_dev.AsyncTools", "AsyncStandardTool"),
    "AsyncStructuredTool": ("agentx_dev.AsyncTools", "AsyncStructuredTool"),
    # Filesystem / permissions
    "DefaultTools": ("agentx_dev.DefaultTools", "DefaultTools"),
    "Permissions":  ("agentx_dev.DefaultTools", "Permissions"),
}


def __getattr__(name):
    """PEP 562 module __getattr__: lazy-load re-exports on first access."""
    if name in _TOOL_EXPORTS:
        import importlib
        mod_name, attr_name = _TOOL_EXPORTS[name]
        mod = importlib.import_module(mod_name)
        obj = getattr(mod, attr_name)
        # Cache on the module so subsequent lookups skip the importlib call.
        globals()[name] = obj
        return obj
    raise AttributeError(f"module 'agentx_dev.Tools' has no attribute {name!r}")


def __dir__():
    """Expose the re-exports to `dir(agentx_dev.Tools)` and IDE autocomplete."""
    return sorted(list(globals().keys()) + list(_TOOL_EXPORTS.keys()))






