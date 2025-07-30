from Bruce_Framework.Agents.Agent import AgentFormattor,StandardParser,ToolCall,AgentCompletion,AgentPrompt
from Bruce_Framework.ChatModel import BaseChatModel
from Bruce_Framework.Tools import StandardTool,StructuredTool,logger
from typing import Dict, Callable, List, Type, Optional
from pydantic import BaseModel,Field

import json,uuid




class AgentRunner:
    """
        The main engine that orchestrates the agent's execution loop.

        This class connects all the components of the framework: the LLM, the tools,
        and the prompt. It manages the agent's state, including its conversational
        history and internal scratchpad, and runs the primary "reason-act" cycle.
        
    """

    def __init__(
        self,
        model:BaseChatModel,
        Agent: AgentFormattor | AgentPrompt | str,
        tools: List[StandardTool|StructuredTool],  # A list of StandardTool and/or StructuredTool instances
        max_iterations: Optional[int] = 3,
    ):
        """
        Initializes the AgentRunner.

        Args:
            llm_model (BaseChatModel): The identifier for the LLM model to be used.
            Agent: The agent's prompt template or instances of (AgentFormattor, AgentPrompt or string ).
            tools (List): A list of tool instances available to the agent.
            max_iterations (Optional[int]): The maximum number of tool-calling cycles.

        Raises:
            TypeError: If any item in the `tools` list is not an instance of
                    StandardTool or StructuredTool.
        """
        self.Query = ""
        self.Agent = Agent
        self.tools = tools
        self.max_iterations = max_iterations
        self.model = model


        # This dictionary is useful for dispatching any tool type, so we'll build it first
        # Dictionaries for quick lookup
        self.func: Dict[str, Callable] = {}
        self.args: Dict[str, Dict] = {}


        # History and intermediate steps
        self.history: List[Dict[str, str]] = []
        self.agent_scratchpad = []
        self.Steps = ''

        # --- Tool Registration with Type Checking---
        # Iterate through the provided tools and register them for execution.
        for tool_instance in self.tools:
            # Use isinstance() for robust type checking.
            if isinstance(tool_instance, StandardTool):
                # It's a StandardTool, register its function.
                self.func[tool_instance.name] = tool_instance.func

            elif isinstance(tool_instance, StructuredTool):
                # It's a StructuredTool, register its function and schema.
                self.args[tool_instance.name] = {
                    "func": tool_instance.func,
                    "args_schema": tool_instance.args_schema
                }
                tool_instance.description=tool_instance.description + str(tool_instance.args_schema.__signature__.parameters)
            else:
                # If it's neither, raise an error with a helpful message.
                tool_type = type(tool_instance).__name__
                raise TypeError(
                    f"Unsupported tool type found in 'tools' list. "
                    f"Expected an instance of 'StandardTool' or 'StructuredTool', "
                    f"but got an object of type '{tool_type}'."
                )

            # --- Prompt Formatting ---
            
            # This block robust implementation would check the type of 'Agent'.
            self.holder=str(uuid.uuid4())
            
            self.tool_info = {
                    'tools': '\n'.join([f"- {t.name} : {t.description }" for t in self.tools]),
                    'tool_names': ', '.join([t.name for t in self.tools]),
                    'user_input' : self.holder
                }
            if isinstance(Agent, str) and '{tools}' in Agent and '{tool_names}' in Agent and '{user_input}' in Agent:

                # str.format_map requires a dictionary-like object.
                self.format_prompt: str = Agent.format_map(self.tool_info)
                self.Agent=AgentFormattor(prompt=Agent,Agent=StandardParser)
                self.parser=self.Agent.Agent
            # Handle the case where an AgentPrompt object is passed
            elif isinstance(Agent, AgentFormattor) and '{tools}' in Agent.prompt and '{tool_names}' in Agent.prompt and '{user_input}' in Agent.prompt:

                logger.debug(f"Formatting prompt from AgentFormattor: {self.Agent.prompt}")
                # str.format_map requires a dictionary-like object.
                self.format_prompt: str = Agent.prompt.format_map(self.tool_info)
                self.parser=self.Agent.Agent


            elif isinstance(Agent, AgentPrompt) and '{tools}' in Agent.prompt and '{tool_names}' in Agent.prompt and '{user_input}' in Agent.prompt:
               
                # str.format_map requires a dictionary-like object.
                self.format_prompt: str = Agent.format(self.tools,user_hold=self.holder)
                self.Agent=AgentFormattor(prompt=Agent.prompt,Agent=StandardParser)
                self.parser=self.Agent.Agent


            else:
                # This check is important for ensuring the prompt can be built correctly.
                raise ValueError("The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}, or an AgentFormattor instance.")
    def cleaning(words:str):
        import re
        text=""
        if isinstance(words,list):
            for serches in words:
                text+=re.sub(r"'^[a-zA-Z0-9_-]+$'.", '_',serches)
            words=text    
        return words
                    
    def Tool_Runner(self, tool_name: str, args_str: str) -> str:
        """
        Executes a specified tool with the given arguments.

        Args:
            tool_name (str): The name of the tool to run.
            args_str (str): A string of arguments for the tool. For structured tools,
                            this is expected to be a JSON string.

        Returns:
            str: The result of the tool execution or an error message.
        """
        # Check if it's a structured tool first.
        if tool_name in self.args:
            schema = self.args[tool_name].get('args_schema')
            func = self.args[tool_name].get('func')
            if schema and func:
                try:
                    # Parse the JSON string from the LLM into a dictionary.
                    parsed_args = args_str
                    result=func(**parsed_args)
                    if isinstance(args_str,str):
                        parsed_args = json.loads(args_str)
                    # Validate and structure the arguments using the Pydantic model.
                    
                        
                        validated_args = schema(**parsed_args)
                        result = func(**validated_args.model_dump())
                    # Use .model_dump() to get a dictionary to pass to the function.
                    # # Change: Replaced print with logger.info for tracking agent actions.
                    logger.info(f"ACTION: {tool_name}, INPUT: {parsed_args}")

                    # Execute the tool's function with the validated arguments.
                        

                    # # Change: Replaced print with logger.info for tracking tool results.
                    logger.info(f"RESULT: {result}")
                    return result
                except Exception as e:
                    # # Change: Use logger.error to explicitly log exceptions before returning an error message.
                    logger.error(f"Error executing structured tool '{tool_name}': {e}", exc_info=True)
                    return f"Error executing structured tool '{tool_name}': {e}"
            else:
                # # Change: Log a misconfiguration as an error.
                logger.error(f"Misconfigured structured tool '{tool_name}'.")
                return f"Error: Misconfigured structured tool '{tool_name}'."

        # Fallback to check for a standard tool.
        elif tool_name in self.func:
            try:
                # # Change: Replaced print with logger.info.
                logger.info(f"ACTION: {tool_name}, INPUT: {args_str}")
                function = self.func[tool_name]
                if not args_str:
                    result = function()
                    logger.info(f"RESULT: {result}")
                    return result
                result = function(args_str) # Standard tools take the raw string argument.
                # # Change: Replaced print with logger.info.
                logger.info(f"RESULT: {result}")
                return result
            except Exception as e:
                # # Change: Use logger.error to log exceptions.
                logger.error(f"Error executing standard tool '{tool_name}': {e}", exc_info=True)
                return f"Error executing standard tool '{tool_name}': {e}"
        else:
            # # Change: Use logger.warning for a non-critical but important issue.
            logger.warning(f"Tool '{tool_name}' not found. Available tools: {list(self.func.keys()) + list(self.args.keys())}")
            return f"Error: Tool '{tool_name}' not found. Please double-check the tool name."
        
    def Initialize(self, user_input: str):


        """
        The main agent execution loop. (Currently a placeholder)
        
        Args:
            user_input (str): Query to run the Agent with.

        Returns:
            str: The result of the Agent execution, Agent tool execution or an error message.
        """
        self.Query = user_input
        # Uses the highly efficient .replace() method to swap the prompt_holder with the actual user_input.
        if self.holder in self.format_prompt:
            self.format_prompt = self.format_prompt.replace(self.holder,self.Query)
        
        
        if not any(entry["role"] == "system" for entry in self.history):
            self.history.append({"role": "system", "content": self.format_prompt})
        
        self.history.append({"role":"user","content":self.Query})

        # logger.info to announce the start of the process.
        logger.info(">>>>> Entering AgentRunner Mode <<<<<")
        if not isinstance(self.model, BaseChatModel):
            raise TypeError(
                f"The 'model' object must be an instance of a class that inherits "
                f"from BaseChatModel, but got type {type(self.model).__name__}."
            )

        # --- AGENT LOOP LOGIC (PLACEHOLDER) ---
       
        count = 1
        tool_calls: List[ToolCall] = []
        steps: List[str] = []

        count = 1
        final_answer = None

        while count <= self.max_iterations:

            response = self.model.Initialize(messages=self.history)
            self.history.append({"role": "assistant", "content": response})
            
            model = self.Agent.Agent.from_json(response)

            if not isinstance(model, self.Agent.Agent):
                self.history.append({"role": "assistant", "content": model})
                
                final_answer = model
                break

            step_description = f"Step {count}: {model.action} with {model.action_input}"
            steps.append(step_description)

            self.history.append({
                "role": "assistant",
                "content": json.dumps({
                    "action": model.action,
                    "action_input": model.action_input
                })
            })
            
            if model.action == "Final_Answer":
                
                final_answer = model.action_input
                break
            print(f"Action : {model.action} \n Action_input : {model.action_input}")
            
            print(f"\x1B[3;33m🛠️ Invoking tool: '{model.action}' with args: {model.action_input}\x1B[0m")

            tool_response = self.Tool_Runner(model.action, model.action_input)
            print(f"\x1B[32m🛠️ Tool Response : {str(tool_response)}\x1B[0m")
            
            if str(tool_response).startswith("Error:"):
                self.history.append({
                    "role": "function",
                    "name": 'Tool_call_error',
                    "content": str(tool_response)
                    })
            else:
                    
                self.history.append({
                    "role": "function",
                    "name": model.action,
                    "content": str(tool_response)
                })

                tool_calls.append(ToolCall(
                    name=model.action,
                    args={"Input":model.action_input},
                    result=str(tool_response)
                ))

            count += 1
        print(f"\x1B[32m✅ Final Answer: {final_answer}\x1B[0m")
        return AgentCompletion.from_agent(
            model_name=self.model.__class__.__name__,
            query=self.Query,
            content=final_answer or "No final answer returned.",
            tool_calls=tool_calls,
            steps=steps,
            history= None
        )

            
            # 1. Build the prompt using self.Agent.format(tools=self.tools, user_input=self.Query)
            # 2. Call the LLM with the prompt.
            # 3. Parse the LLM output to get `tool_name` and `args`.
            # 4. Call self.Tool_Runner(tool_name, args)
            # 5. Update history/scratchpad and loop.

            
            # --- END OF PLACEHOLDER ---

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the runner's state."""
        return (
            f"<{self.__class__.__name__}("
            f"Agent={self.Agent.__class__.__name__}, "
            f"Tools={len(self.tools)}, "
            f"ToolNames={[tool.name for tool in self.tools]}, "
            f"MaxIterations={self.max_iterations}, "
            f"Model='{self.model}', "
            f"HistoryLen={len(self.history)})>"
        )

    def __str__(self) -> str:
        """Provides a user-friendly summary of the agent runner."""
        return (
            f"Agent Runner Summary:\n"
            f"  Agent Type     : {self.Agent.__class__.__name__}\n"
            f"  Tools Used     : {[tool.name for tool in self.tools]}\n"
            f"  Max Iterations : {self.max_iterations}\n"
            f"  Model          : {self.model}\n"
            f"  Query          : {self.Query}\n"
            f"  History Entries: {len(self.history)}"
        )