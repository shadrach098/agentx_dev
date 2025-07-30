"""
This module contains classes and functions for working with agent prompts and JSON data.

It includes classes for different agent prompt templates, a function to convert strings to JSON,
and a container class for standard pre-defined agent prompt templates.
"""

import yaml,logging,json,uuid,time
from pydantic import BaseModel, Field, ValidationError
from typing import Dict, Any,Type,Optional,List
from rich.console import Console
console=Console()
# Set up logging
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

class StandardParser(BaseModel):
    """
    A Pydantic model representing the standard parser.

    Attributes:
    - action: str, the action to take.
    - action_input: str or Dict, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    """
    action: str = Field("The action to take", alias='action')
    action_input: str | Dict = Field("The input to the action", alias='action_input')

    class Config:
        populate_by_name = True

    @classmethod
    def from_json(cls, json_str: str):
        try:
            json_object = convert_to_json(json_str)
            if isinstance(json_object,dict):
                return cls(**json_object)
            elif isinstance(json_object,str):
                return json_object
        except (ValueError, ValidationError) as e:
            logger.error(f"Error creating StandardParser instance: {e}")
            raise






def load_prompt_templates(file_path: str) -> Dict:
    """
    Loads prompt templates from a YAML file.

    Args:
    - file_path: str, the path to the YAML file containing the prompt templates.

    Returns:
    - Dict: A dictionary containing the loaded prompt templates.
    """
    with open(file_path, 'r') as yaml_:
        return yaml.safe_load(yaml_)

system = load_prompt_templates('promptTemplate.yaml')


def convert_to_json(json_str: str) -> Dict[str, Any]:
    """
    Converts a given string into a JSON object.

    Args:
    - json_str: str, the string to be converted into JSON.

    Returns:
    - Dict[str, Any]: A dictionary representing the JSON object.

    Raises:
    - json.JSONDecodeError: If the input string is not valid JSON.
    """
    try:
        if json_str.startswith("```json") and json_str.endswith("```"):
            return json.loads(json_str.split("```json")[-1].split("```")[0])
        elif json_str.startswith('{') and json_str.endswith('}'):
            return json.loads(json_str)
        else :
            return json_str
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON: {e}")
        return {}  # or you can re-raise the exception, depending on your error handling strategy

# Consider adding a Config class to these models to allow population by field name
class Instruction_Tuned_(BaseModel):
    """
    A Pydantic model representing the Instruction-Tuned prompt template.

    Attributes:
    - action: str, the action to take or final answer.
    - action_input: str or Dict, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    """
    action: str = Field("The action to take OR Final Answer", alias="action")
    action_input: str | Dict = Field("The input to the action OR You should put what you want to return to use here and return to user immediately", alias="action_input")
    

    @classmethod
    def from_json(cls, json_str: str):
        try:
            json_object = convert_to_json(json_str)
            if isinstance(json_object,dict):
                return cls(**json_object)
            elif isinstance(json_object,str):
                return json_object
        except (ValueError, ValidationError) as e:
            # Handle the error, e.g., log it, raise a custom exception, or return a default value
            error = f"Error creating Instruction_Tuned_ instance: {e}"
            logger.error(error)
            raise 
        

class React_(BaseModel):
    """
    A Pydantic model representing the ReAct prompt template.

    Attributes:
    - Thought: str, the agent's thoughts.
    - action: str, the action to take or final answer.
    - action_input: str or Dict, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    """
    Thought: str = Field('The Agent Thoughts', alias='Thought')
    action: str = Field("The action to take OR Final Answer", alias="action")
    action_input: str | Dict = Field("The input to the action OR You should put what you want to return to use here and return to user immediately", alias="action_input")

    @classmethod
    def from_json(cls, json_str: str):
        try:
            json_object = convert_to_json(json_str)
            if isinstance(json_object,dict):
                console.print("[bold cyan]Thinking...[/bold cyan]")
                print(f"\n{json_object['Thought']}")
                return cls(**json_object)
            elif isinstance(json_object,str):
                return json_object
        except (ValueError, ValidationError) as e:
            # Handle the error, e.g., log it, raise a custom exception, or return a default value
            error = f"Error creating React_ instance: {e}"
            logger.error(error)
            raise

class ChainOfThought(BaseModel):
    """
    A Pydantic model representing the Chain-of-Thought prompt template.

    Attributes:
    - Thought: str, the step-by-step reasoning.
    - Action: str, the action to take or final answer.
    - Action_Input: str or Dict, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    """
    Thought: str = Field('The step-by-step reasoning', alias='Thought')
    Action: str = Field("The action to take or Final Answer", alias='Action')
    Action_Input: str | Dict = Field("The input to the action", alias='Action Input')

    @classmethod
    def from_json(cls, json_str: str):
        try:
            json_object = convert_to_json(json_str)
            if isinstance(json_object,dict):
                console.print("[bold cyan]Thinking...[/bold cyan]")
                print(f"\n{json_object['Thought']}")
                return cls(**json_object)
            elif isinstance(json_object,str):
                return json_object
        except (ValueError, ValidationError) as e:
            logger.error(f"Error creating ChainOfThought instance: {e}")
            raise

class ZeroShot(BaseModel):
    """
    A Pydantic model representing the Zero-Shot prompt template.

    Attributes:
    - action: str, the action to take.
    - action_input: str or Dict, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    """
    action: str = Field("The action to take", alias='action')
    action_input: str | Dict = Field("The input to the action", alias='action_input')

    @classmethod
    def from_json(cls, json_str: str):
        try:
            json_object = convert_to_json(json_str)
            if isinstance(json_object,dict):
                return cls(**json_object)
            elif isinstance(json_object,str):
                return json_object
        except (ValueError, ValidationError) as e:
            logger.error(f"Error creating ZeroShot instance: {e}")
            raise

class FewShot(BaseModel):
    """
    A Pydantic model representing the Few-Shot prompt template.

    Attributes:
    - action: str, the action to take.
    - action_input: str or Dict, the input to the action.

    Methods:
    - from_json: Creates an instance from a JSON string.
    """
    action: str = Field("The action to take", alias='action')
    action_input: str | Dict = Field("The input to the action", alias='action_input')

    @classmethod
    def from_json(cls, json_str: str):
        try:
            json_object = convert_to_json(json_str)
            if isinstance(json_object,dict):
                return cls(**json_object)
            elif isinstance(json_object,str):
                return json_object
        except (ValueError, ValidationError) as e:
            logger.error(f"Error creating FewShot instance: {e}")
            raise


class AgentFormattor(BaseModel):
    prompt: str
    Agent: Type[BaseModel]=Field(description="The agent that will under the prompt and extract what's needed")
    
    @classmethod
    def Formattor(cls,prompt:str,Agent:Type[BaseModel]):
        return cls(prompt=prompt,Agent=Agent)


class AgentPrompt:
    """
    Manages and formats the prompt template for the agent, allowing for
    flexible placeholder values.
    """
    def __init__(self, prompt: str):
        """
        Initializes the prompt with a template string.
        """
        self.prompt = prompt

    def format(self, tools: List,user_hold, **kwargs: Any) -> str:
        """
        Formats the prompt template with dynamic values.

        Args:
            tools (List): A list of tool objects to populate {tools} and {tool_names}.
            **kwargs (Any): Optional. Any other key-value pairs corresponding to
                            placeholders in the template (e.g., user_input="...").
        Returns:
            str: The fully formatted prompt.
        """
        # Build the dictionary of values that are always available.
        format_values = {
            'tools': "\n".join([f"- {t.name}: {t.description}" for t in tools]),
            'tool_names': ", ".join([t.name for t in tools]),
            'user_input' : user_hold

        }

        # The update() method merges the optional kwargs.
        # If kwargs is empty, this does nothing.
        format_values.update(kwargs)

        # format_map uses the combined dictionary to fill in the template.
        return self.prompt.format_map(format_values)
    
    


class AgentType:
    """
    A container for standard, pre-defined agent prompt templates.

    This class holds various prompt styles as class attributes, making them
    easy to access and use to initialize an AgentPrompt.

    Example:
        react_prompt_template = AgentType.ReAct
        agent_prompt = AgentPrompt(template=react_prompt_template)
    """

    ReAct = AgentFormattor.Formattor(prompt=system['ReAct'],Agent=React_)
    Instruction_Tuned = AgentFormattor.Formattor(prompt=system['Instruction-Tuned'],Agent=Instruction_Tuned_)
    Chain_of_Thought = AgentFormattor.Formattor(prompt=system['Chain-of-Thought'],Agent=ChainOfThought)
    Zero_Shot = AgentFormattor.Formattor(prompt=system['Zero-Shot'],Agent=ZeroShot)
    Few_Shot = AgentFormattor.Formattor(prompt=system['Few-Shot'],Agent=FewShot)
    



class ToolCall(BaseModel):
    name: str
    args: Dict[str, Any]
    result: str


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
    tool_calls: List[ToolCall] = []
    steps: List[str] = []
    history: Optional[List[Dict[str, str]]] = None


    @classmethod
    def from_agent(cls, *, model_name: str, query: str, content: str, tool_calls: List[ToolCall], steps: List[str], history: List[Dict[str, str]]):
        return cls(
            id=str(uuid.uuid4()),
            created=int(time.time()),
            model=model_name,
            query=query,
            content=content,
            tool_calls=tool_calls,
            steps=steps,
            history=history
        )
    