"""
This module contains classes and functions for working with agent prompts and JSON data.

It includes:
- Pydantic parser models for several agent prompt styles (StandardParser, React_,
  ChainOfThought, ZeroShot, FewShot, Instruction_Tuned_).
- ``convert_to_json`` for tolerant JSON extraction from LLM text.
- ``to_openai_tool`` / ``to_anthropic_tool`` helpers that turn any Pydantic
  ``BaseModel`` subclass into a function-calling tool specification, so the
  same parser models can be used either via JSON-in-text parsing or via the
  provider's native function-calling API.
- ``AgentFormattor`` which pairs a prompt template with its parser model and
  exposes the same function-calling helpers (``AgentType.ReAct.to_openai_tool()``
  works out of the box).
"""

import yaml, logging, json, uuid, time, os
from pydantic import BaseModel, Field, ValidationError, ConfigDict, AliasChoices
from typing import Dict, Any, Type, Optional, List, Union
from rich.console import Console

console = Console()
logger = logging.getLogger(__name__)


def _tool_description(model_cls: Type[BaseModel], description: Optional[str]) -> str:
    if description:
        return description
    if model_cls.__doc__:
        return model_cls.__doc__.strip()
    return f"Structured output for {model_cls.__name__}"


def to_openai_tool(model_cls: Type[BaseModel], description: Optional[str] = None) -> Dict[str, Any]:
    """Convert any Pydantic ``BaseModel`` subclass into an OpenAI tools-API spec."""
    return {
        "type": "function",
        "function": {
            "name": model_cls.__name__,
            "description": _tool_description(model_cls, description),
            "parameters": model_cls.model_json_schema(),
        },
    }


def to_anthropic_tool(model_cls: Type[BaseModel], description: Optional[str] = None) -> Dict[str, Any]:
    """Convert any Pydantic ``BaseModel`` subclass into an Anthropic tool spec."""
    return {
        "name": model_cls.__name__,
        "description": _tool_description(model_cls, description),
        "input_schema": model_cls.model_json_schema(),
    }


def to_tool_spec(model_cls: Type[BaseModel], description: Optional[str] = None) -> Dict[str, Any]:
    """Provider-agnostic tool spec. Chat models translate this into native format."""
    return {
        "name": model_cls.__name__,
        "description": _tool_description(model_cls, description),
        "parameters": model_cls.model_json_schema(),
    }


class _ParserMixin:
    """Adds function-calling helpers to parser BaseModels.

    Subclasses gain ``to_openai_tool``/``to_anthropic_tool``/``to_tool_spec``
    classmethods plus a ``from_function_call`` constructor that mirrors
    ``from_json`` but takes a pre-parsed args dict (the natural shape returned
    by a native tool call).
    """

    @classmethod
    def to_openai_tool(cls, description: Optional[str] = None) -> Dict[str, Any]:
        return to_openai_tool(cls, description)

    @classmethod
    def to_anthropic_tool(cls, description: Optional[str] = None) -> Dict[str, Any]:
        return to_anthropic_tool(cls, description)

    @classmethod
    def to_tool_spec(cls, description: Optional[str] = None) -> Dict[str, Any]:
        return to_tool_spec(cls, description)

    @classmethod
    def from_function_call(cls, args: Dict[str, Any]) -> "BaseModel":
        return cls(**args)


class StandardParser(_ParserMixin, BaseModel):
    """
    A Pydantic model representing the standard parser.

    Attributes:
    - action: str, the action to take.
    - action_input: str, Dict, or List, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    - from_function_call: Creates an instance from a tool-call args dict.
    """
    action: str = Field("The action to take", alias='action')
    action_input: str | Dict | List = Field("The input to the action", alias='action_input')

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_json(cls, json_str: str):
        obj = convert_to_json(json_str)
        if obj is None:
            return json_str
        return cls(**obj)



import importlib.resources as pkg_resources
from agentx_dev import resources

def load_prompt_templates():
    with pkg_resources.open_text(resources, "promptTemplate.yaml") as f:
        return yaml.safe_load(f)

system = load_prompt_templates()


def convert_to_json(json_str: Any) -> Optional[Dict[str, Any]]:
    """Tolerant JSON extractor.

    Returns the parsed object if ``json_str`` is a JSON object (optionally
    wrapped in ```json fences). Returns ``None`` when the input is clearly not
    JSON-shaped (so callers can treat it as free text). Raises
    ``json.JSONDecodeError`` when the input is JSON-shaped but malformed —
    silent failure used to mask real bugs.
    """
    if not isinstance(json_str, str):
        return None
    s = json_str.strip()
    if s.startswith("```json") and s.endswith("```"):
        s = s.split("```json", 1)[1].rsplit("```", 1)[0].strip()
    if s.startswith('{') and s.endswith('}'):
        return json.loads(s)
    return None


class Instruction_Tuned_(_ParserMixin, BaseModel):
    """
    A Pydantic model representing the Instruction-Tuned prompt template.

    Attributes:
    - action: str, the action to take or final answer.
    - action_input: str, Dict, or List, the input to the action.
    """
    action: str = Field("The action to take OR Final Answer", alias="action")
    action_input: str | Dict | List = Field("The input to the action OR You should put what you want to return to use here and return to user immediately", alias="action_input")

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_json(cls, json_str: str):
        obj = convert_to_json(json_str)
        if obj is None:
            return json_str
        return cls(**obj)


class React_(_ParserMixin, BaseModel):
    """
    A Pydantic model representing the ReAct prompt template.

    Attributes:
    - Thought: str, the agent's thoughts.
    - action: str, the action to take or final answer.
    - action_input: str, Dict, or List, the input to the action.
    """
    Thought: str = Field('The Agent Thoughts', alias='Thought')
    action: str = Field("The action to take OR Final Answer", alias="action")
    action_input: str | Dict | List = Field("The input to the action OR You should put what you want to return to use here and return to user immediately", alias="action_input")

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_json(cls, json_str: str):
        obj = convert_to_json(json_str)
        if obj is None:
            return json_str
        return cls(**obj)


class ChainOfThought(_ParserMixin, BaseModel):
    """
    A Pydantic model representing the Chain-of-Thought prompt template.

    Accepts BOTH naming styles via Pydantic ``AliasChoices``:
      - PascalCase ``Action`` / ``Action_Input`` (legacy CoT prompt)
      - lowercase ``action`` / ``action_input`` (matches the other AgentTypes)

    Storage attribute names stay lowercase so the runner can read
    ``instance.action`` / ``instance.action_input`` uniformly across every
    AgentType — no more parser-specific field lookups.
    """
    Thought: str = Field('The step-by-step reasoning', alias='Thought')
    action: str = Field(
        "The action to take or Final Answer",
        validation_alias=AliasChoices("action", "Action"),
        serialization_alias="action",
    )
    action_input: str | Dict | List = Field(
        "The input to the action",
        validation_alias=AliasChoices("action_input", "Action_Input"),
        serialization_alias="action_input",
    )

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_json(cls, json_str: str):
        obj = convert_to_json(json_str)
        if obj is None:
            return json_str
        return cls(**obj)


class ZeroShot(_ParserMixin, BaseModel):
    """
    A Pydantic model representing the Zero-Shot prompt template.
    """
    action: str = Field("The action to take", alias='action')
    action_input: str | Dict | List = Field("The input to the action", alias='action_input')

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_json(cls, json_str: str):
        obj = convert_to_json(json_str)
        if obj is None:
            return json_str
        return cls(**obj)


class FewShot(_ParserMixin, BaseModel):
    """
    A Pydantic model representing the Few-Shot prompt template.
    """
    action: str = Field("The action to take", alias='action')
    action_input: str | Dict | List = Field("The input to the action", alias='action_input')

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def from_json(cls, json_str: str):
        obj = convert_to_json(json_str)
        if obj is None:
            return json_str
        return cls(**obj)


def _parser_action_field(parser: Type[BaseModel]) -> Optional[str]:
    """Return whichever of {'action', 'Action'} the parser defines, or None."""
    fields = parser.model_fields
    if "action" in fields:
        return "action"
    if "Action" in fields:
        return "Action"
    return None


def _parser_action_input_field(parser: Type[BaseModel]) -> Optional[str]:
    fields = parser.model_fields
    if "action_input" in fields:
        return "action_input"
    if "Action_Input" in fields:
        return "Action_Input"
    return None


class AgentFormattor(BaseModel):
    prompt: str
    Agent: Type[BaseModel] = Field(description="The agent that will under the prompt and extract what's needed")

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def Formattor(cls, prompt: str, Agent: Type[BaseModel]):
        return cls(prompt=prompt, Agent=Agent)

    def to_openai_tool(self, description: Optional[str] = None) -> Dict[str, Any]:
        return to_openai_tool(self.Agent, description)

    def to_anthropic_tool(self, description: Optional[str] = None) -> Dict[str, Any]:
        return to_anthropic_tool(self.Agent, description)

    def to_tool_spec(self, description: Optional[str] = None) -> Dict[str, Any]:
        return to_tool_spec(self.Agent, description)

    @property
    def action_field(self) -> Optional[str]:
        return _parser_action_field(self.Agent)

    @property
    def action_input_field(self) -> Optional[str]:
        return _parser_action_input_field(self.Agent)


class AgentPrompt:
    """
    Manages and formats the prompt template for the agent, allowing for
    flexible placeholder values.
    """
    def __init__(self, prompt: str):
        self.prompt = prompt

    def format(self, tools: List, user_hold, **kwargs: Any) -> str:
        format_values = {
            'tools': "\n".join([f"- {t.name}: {t.description}" for t in tools]),
            'tool_names': ", ".join([t.name for t in tools]),
            'user_input': user_hold,
        }
        format_values.update(kwargs)
        return self.prompt.format_map(format_values)


class AgentType:
    """
    A container for standard, pre-defined agent prompt templates.

    Example:
        react_prompt_template = AgentType.ReAct
        agent_prompt = AgentPrompt(template=react_prompt_template)
    """

    ReAct = AgentFormattor.Formattor(prompt=system['ReAct'], Agent=React_)
    Instruction_Tuned = AgentFormattor.Formattor(prompt=system['Instruction-Tuned'], Agent=Instruction_Tuned_)
    Chain_of_Thought = AgentFormattor.Formattor(prompt=system['Chain-of-Thought'], Agent=ChainOfThought)
    Zero_Shot = AgentFormattor.Formattor(prompt=system['Zero-Shot'], Agent=ZeroShot)
    Few_Shot = AgentFormattor.Formattor(prompt=system['Few-Shot'], Agent=FewShot)


# Public alias for the typo-bearing legacy name. New code should prefer
# AgentFormatter; AgentFormattor stays exported for backward compatibility.
AgentFormatter = AgentFormattor


class ToolError(str):
    """A tool-execution failure carried inline through the runner loop.

    Subclasses ``str`` so it survives existing ``str(tool_response)`` calls
    and downstream history serialization unchanged, while also being
    distinguishable via ``isinstance(x, ToolError)``. Replaces the previous
    fragile pattern of returning ``"Error: ..."`` strings and checking with
    ``startswith("Error:")``.
    """

    def __new__(cls, message: str, *, tool: str | None = None, cause: BaseException | None = None):
        instance = super().__new__(cls, message)
        instance.tool = tool
        instance.cause = cause
        return instance


class ToolCall(BaseModel):
    name: str
    args: Dict[str, Any]
    result: str


class AgentCompletion(BaseModel):
    id: str
    object: str = "agent.completion"
    created: int
    model: str
    query: str
    content: str
    tool_calls: List[ToolCall] = Field(default_factory=list)
    steps: List[str] = Field(default_factory=list)
    # History values can be strings (text content) OR lists/dicts (when the
    # message carries provider-native tool_use / tool_call blocks per #15).
    history: Optional[List[Dict[str, Any]]] = None
    # When the caller passes ``output_schema=`` to runner.invoke, the final
    # answer text is parsed as JSON and validated against that schema; the
    # resulting Pydantic instance lands here. Stays None if no schema was
    # passed or if parsing failed (the parse error surfaces as an exception
    # at invoke time, not silently).
    output: Optional[Any] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def from_agent(cls, *, model_name: str, query: str, content: Union[str, Dict[str, Any], List[Dict[str, Any]]], tool_calls: Optional[List[ToolCall]] = None, steps: Optional[List[str]] = None, history: List[Dict[str, str]]):
        if isinstance(content, (dict, list)):
            content_str = json.dumps(content, ensure_ascii=False)
        else:
            content_str = content
        return cls(
            id=str(uuid.uuid4()),
            created=int(time.time()),
            model=model_name,
            query=query,
            content=content_str,
            tool_calls=tool_calls,
            steps=steps,
            history=history
        )
