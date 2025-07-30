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

# # Change: Added a basic configuration for the logger.
# # This allows users of the framework to easily control log verbosity and format.
# # It sets a default to show INFO and higher-level messages.
logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')

# # Change: Created a logger instance for this specific module.
# # This is best practice for making logs traceable to their origin.
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
        """
        Creates an OpenAI-compatible function specification from a Pydantic model.

        Args:
            model (Type[BaseModel]): The Pydantic model class.

        Returns:
            List[Dict]: A list containing a single dictionary formatted as an
                        OpenAI function specification.
        """
        return [{
            "name": model.__name__,
            "description": model.__doc__ or f"Structured output for {model.__name__}",
            "parameters": model.model_json_schema(),
        }]


# --- Component 2: Tool Definitions ---

class StandardTool:
    """
    Represents a simple tool with a name, description, and a function to execute.
    This tool type does not have structured (Pydantic-based) arguments.
    """
    def __init__(self, name: str, func: Callable, description: str):
        """
        Initializes a StandardTool.

        Args:
            name (str): The name of the tool, used by the LLM to identify it.
            func (Callable): The Python function to execute when the tool is called.
            description (str): A description of what the tool does, for the LLM to understand its purpose.
        """
        self.name = name
        self.description = description
        self.func = func
        # Set __name__ for easier type checking in the AgentRunner.
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
        name: str = Field(..., description="Name of the tool."),
        description: str = Field(..., description="Description of what the tool does."),
        func: Callable = Field(..., description="Callable function the tool executes."),
        args_schema: Type[BaseModel] = Field(..., description="Pydantic model for input validation.")
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
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self.func = func
        # Set __name__ for easier type checking in the AgentRunner.
        self.__name__ = 'StructuredTool'

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the tool."""
        return (
            f"StructuredTool(name={self.name!r}, description={self.description!r}, "
            f"args_schema={self.args_schema.__name__}, func={self.func!r})")


# --- Component 4: Prompt Management ---

from typing import List, Dict, Any

# # Suggestion: Placing local application imports after standard library imports is a common convention.






