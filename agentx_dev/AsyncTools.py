"""
Async-enabled tools for concurrent execution in agent workflows.

This module provides async versions of StandardTool and StructuredTool,
enabling concurrent tool execution for improved performance in I/O-bound operations.
"""

from typing import Dict, Callable, List, Type, Coroutine, Any
from pydantic import BaseModel, Field
import asyncio
import logging

logger = logging.getLogger(__name__)


class AsyncStandardTool:
    """
    Async version of StandardTool that supports concurrent execution.

    This tool type allows async functions to be used as tools, enabling
    concurrent I/O operations like API calls, database queries, etc.
    """

    def __init__(self, func: Callable[..., Coroutine], name: str | None = None, description: str | None = None):
        """
        Initializes an AsyncStandardTool.

        Args:
            func (Callable): Async function to execute when the tool is called.
            name (str, optional): The name of the tool. Defaults to function name.
            description (str, optional): Description of the tool. Defaults to function docstring.

        Raises:
            ValueError: If name or description cannot be determined.
        """
        self.func = func

        if not name:
            self.name = self.func.__name__
        elif not name and not self.func.__name__:
            raise ValueError("name description needed cant be NONE")
        else:
            self.name = name

        temp = description if description else self.func.__doc__
        if temp:
            self.description = temp
        else:
            raise ValueError("description needed cant be NONE")

        self.__name__ = 'AsyncStandardTool'

    async def execute(self, *args, **kwargs):
        """Execute the async tool function."""
        return await self.func(*args, **kwargs)

    def __repr__(self) -> str:
        return (
            f"AsyncStandardTool(name={self.name!r}, description={self.description!r}, "
            f"func={self.func!r})"
        )


class AsyncStructuredTool:
    """
    Async version of StructuredTool with Pydantic-based argument validation.

    Combines the benefits of structured arguments with async execution capabilities.
    """

    def __init__(
        self,
        func: Callable[..., Coroutine],
        args_schema: Type[BaseModel],
        name: str | None = None,
        description: str | None = None
    ):
        """
        Initializes an AsyncStructuredTool.

        Args:
            func (Callable): Async function to execute.
            args_schema (Type[BaseModel]): Pydantic model defining expected arguments.
            name (str, optional): Tool name. Defaults to function name.
            description (str, optional): Tool description. Defaults to function docstring.
        """
        self.func = func

        if not name:
            self.name = self.func.__name__
        elif not name and not self.func.__name__:
            raise ValueError("name description needed cant be NONE")
        else:
            self.name = name

        temp = description if description else self.func.__doc__
        if temp:
            self.description = temp
        else:
            raise ValueError("description needed cant be NONE")

        self.args_schema = args_schema
        self.__name__ = 'AsyncStructuredTool'

    async def execute(self, **kwargs):
        """Execute the async tool function with validated arguments."""
        validated_args = self.args_schema(**kwargs)
        return await self.func(**validated_args.model_dump())

    def __repr__(self) -> str:
        return (
            f"AsyncStructuredTool(name={self.name!r}, description={self.description!r}, "
            f"args_schema={self.args_schema.__name__}, func={self.func!r})"
        )


async def execute_tools_concurrently(tools_to_execute: List[Dict[str, Any]]) -> List[Any]:
    """
    Execute multiple async tools concurrently.

    Args:
        tools_to_execute: List of dicts with 'tool' (AsyncTool instance) and 'args' (dict).

    Returns:
        List of results from each tool execution in the same order.

    Example:
        >>> tools = [
        ...     {'tool': weather_tool, 'args': {'location': 'NYC'}},
        ...     {'tool': news_tool, 'args': {'topic': 'tech'}}
        ... ]
        >>> results = await execute_tools_concurrently(tools)
    """
    tasks = []
    for item in tools_to_execute:
        tool = item['tool']
        args = item.get('args', {})

        if isinstance(tool, AsyncStandardTool):
            tasks.append(tool.execute(args.get('input', '')))
        elif isinstance(tool, AsyncStructuredTool):
            tasks.append(tool.execute(**args))
        else:
            logger.warning(f"Skipping non-async tool: {tool}")

    return await asyncio.gather(*tasks, return_exceptions=True)
