from agentx_dev.Agents import AgentFormattor,AgentCompletion,AgentPrompt
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Agents.Agent import StandardParser,ToolCall
from agentx_dev.Tools import StandardTool,StructuredTool,logger
from typing import Dict, Callable, List, Type, Optional
from pydantic import BaseModel,Field

import json

# Auto-initialize enhanced features
from agentx_dev.AutoSetup import get_auto_setup, ensure_initialized
from agentx_dev.Config import config
from agentx_dev.Observability import observability, EventType

# Ensure features are initialized
ensure_initialized()
_auto_setup = get_auto_setup()




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
        max_iterations: int | None = None,
        auto_cache: bool = True,  # Automatically cache tool results
        auto_memory: bool = False,  # Automatically manage conversation memory
    ):
        """
        Initializes the AgentRunner.

        Args:
            llm_model (BaseChatModel): The identifier for the LLM model to be used.
            Agent: The agent's prompt template or instances of (AgentFormattor, AgentPrompt or string ).
            tools (List): A list of tool instances available to the agent.
            max_iterations (Optional[int]): The maximum number of tool-calling cycles.
            auto_cache (bool): Enable automatic tool result caching (default: True).
            auto_memory (bool): Enable automatic memory management (default: False).

        Raises:
            TypeError: If any item in the `tools` list is not an instance of
                    StandardTool or StructuredTool.
        """
        self.Query = ""
        self.Agent = Agent
        self.tools = tools
        self.max_iterations = max_iterations if max_iterations else 4
        self.model = model

        # Auto-setup features
        self.auto_cache = auto_cache and config.caching_enabled
        self.auto_memory = auto_memory and config.memory_enabled
        self._cache = _auto_setup.get_global_cache() if self.auto_cache else None
        self._memory = _auto_setup.create_memory() if self.auto_memory else None
        self._tool_cache: Dict[str, any] = {}  # Cache for tool results

        # This dictionary is useful for dispatching any tool type, so we'll build it first
        # Dictionaries for quick lookup
        self.func: Dict[str, Callable] = {}
        self.args: Dict[str, Dict] = {}

        # --- Tool Registration with Type Checking ---
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
                tool_instance.description = tool_instance.description + str(tool_instance.args_schema.__signature__.parameters)
            else:
                # If it's neither, raise an error with a helpful message.
                tool_type = type(tool_instance).__name__
                raise TypeError(
                    f"Unsupported tool type found in 'tools' list. "
                    f"Expected an instance of 'StandardTool' or 'StructuredTool', "
                    f"but got an object of type '{tool_type}'."
                )

        # Pre-build the tool blocks used for prompt formatting on every call
        self._tool_prompt_block = '\n'.join([f"- {t.name} : {t.description}" for t in self.tools])
        self._tool_names_block = ', '.join([t.name for t in self.tools])

        # --- Validate Agent and store the prompt template ---
        if isinstance(Agent, str) and '{tools}' in Agent and '{tool_names}' in Agent and '{user_input}' in Agent:
            self.Agent = AgentFormattor(prompt=Agent, Agent=StandardParser)
            self.parser = self.Agent.Agent
        elif isinstance(Agent, AgentFormattor) and '{tools}' in Agent.prompt and '{tool_names}' in Agent.prompt and '{user_input}' in Agent.prompt:
            logger.debug(f"Formatting prompt from AgentFormattor: {self.Agent.prompt}")
            self.parser = self.Agent.Agent
        elif isinstance(Agent, AgentPrompt) and '{tools}' in Agent.prompt and '{tool_names}' in Agent.prompt and '{user_input}' in Agent.prompt:
            self.Agent = AgentFormattor(prompt=Agent.prompt, Agent=StandardParser)
            self.parser = self.Agent.Agent
        else:
            raise ValueError("The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}, or an AgentFormattor instance.")
                    
    def Tool_Runner(self, tool_name: str, args_str: str) -> str:
        """
        Executes a specified tool with the given arguments.
        Automatically caches results if auto_cache is enabled.

        Args:
            tool_name (str): The name of the tool to run.
            args_str (str): A string of arguments for the tool. For structured tools,
                            this is expected to be a JSON string.

        Returns:
            str: The result of the tool execution or an error message.
        """
        # Check cache first if enabled
        if self._cache:
            from agentx_dev.Cache import generate_cache_key
            cache_key = generate_cache_key(tool_name, args_str)
            cached_result = self._cache.get(cache_key)
            if cached_result is not None:
                logger.info(f"CACHE HIT: {tool_name}")
                if config.observability_enabled:
                    observability.emit(observability.start_event(
                        EventType.CUSTOM,
                        data={"cache": "hit", "tool": tool_name}
                    ))
                return cached_result

        # Track tool execution if observability enabled
        tool_event = None
        if config.observability_enabled:
            tool_event = observability.start_event(
                EventType.TOOL_CALL_START,
                data={"tool_name": tool_name, "args": str(args_str)[:100]}
            )

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

                    # Cache the result if caching is enabled
                    if self._cache:
                        from agentx_dev.Cache import generate_cache_key
                        cache_key = generate_cache_key(tool_name, args_str)
                        self._cache.set(cache_key, result, ttl=config.cache_ttl)

                    # Complete observability event
                    if tool_event:
                        observability.end_event(tool_event, data={"result": str(result)[:100]})

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

                # Cache the result if caching is enabled
                if self._cache:
                    from agentx_dev.Cache import generate_cache_key
                    cache_key = generate_cache_key(tool_name, args_str)
                    self._cache.set(cache_key, result, ttl=config.cache_ttl)

                # Complete observability event
                if tool_event:
                    observability.end_event(tool_event, data={"result": str(result)[:100]})

                return result
            except Exception as e:
                # # Change: Use logger.error to log exceptions.
                logger.error(f"Error executing standard tool '{tool_name}': {e}", exc_info=True)
                return f"Error executing standard tool '{tool_name}': {e}"
        else:
            # # Change: Use logger.warning for a non-critical but important issue.
            logger.warning(f"Tool '{tool_name}' not found. Available tools: {list(self.func.keys()) + list(self.args.keys())}")
            return f"Error: Tool '{tool_name}' not found. Please double-check the tool name."
        
    def Initialize(self, user_input: str, ChatHistory: Optional[List[Dict[str, str]]] = None) -> AgentCompletion:
        """
        The main agent execution loop.

        Args:
            user_input: The user's query for this turn.
            ChatHistory: Optional prior conversation messages to inject.

        Returns:
            AgentCompletion: Result including full working history.
        """
        agent_event = None
        if config.observability_enabled:
            agent_event = observability.start_event(
                EventType.AGENT_START,
                data={"query": user_input[:100]}
            )

        self.Query = user_input

        # Build a fresh system prompt for this specific call
        tool_info = {
            'tools': self._tool_prompt_block,
            'tool_names': self._tool_names_block,
            'user_input': user_input
        }
        system_prompt = self.Agent.prompt.format_map(tool_info)

        # Fresh working history — never mutate instance state
        working_history: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

        # Resolve chat history source
        effective_history = ChatHistory
        if self.auto_memory and self._memory and effective_history is None:
            effective_history = self._memory.get_messages()

        if effective_history and isinstance(effective_history, list):
            for r in effective_history:
                if r.get('role') and r.get('content'):
                    content = r['content']
                    if r.get('timestamp'):
                        content += r['timestamp']
                    working_history.append({'role': r['role'], 'content': content})

        working_history.append({"role": "user", "content": user_input})

        if not isinstance(self.model, BaseChatModel):
            raise TypeError(
                f"The 'model' object must inherit from BaseChatModel, "
                f"but got type {type(self.model).__name__}."
            )

        count = 1
        tool_calls: List[ToolCall] = []
        steps: List[str] = []
        final_answer = None

        while count <= self.max_iterations:
            response = self.model.Initialize(messages=working_history)
            working_history.append({"role": "assistant", "content": response})

            model = self.Agent.Agent.from_json(response)

            if not isinstance(model, self.Agent.Agent):
                final_answer = model
                break

            step_description = f"Step {count}: {model.action} with {model.action_input}"
            steps.append(step_description)

            working_history.append({
                "role": "assistant",
                "content": json.dumps({"action": model.action, "action_input": model.action_input})
            })

            if model.action == "Final_Answer":
                final_answer = model.action_input
                break

            print(f"\x1B[3;33m🛠️  Invoking tool: '{model.action}' with args: {model.action_input}\x1B[0m")
            tool_response = self.Tool_Runner(model.action, model.action_input)
            print(f"\x1B[32m🛠️  Tool Response: {str(tool_response)}\x1B[0m")

            if str(tool_response).startswith("Error:"):
                working_history.append({
                    "role": "function",
                    "name": "tool_call_error",
                    "content": str(tool_response)
                })
            else:
                working_history.append({
                    "role": "function",
                    "name": model.action,
                    "content": str(tool_response)
                })
                tool_calls.append(ToolCall(
                    name=model.action,
                    args={"Input": model.action_input},
                    result=str(tool_response)
                ))

            count += 1

        print(f"\x1B[32m✅ Final Answer: {final_answer}\x1B[0m")

        if self.auto_memory and self._memory:
            self._memory.add_message("user", user_input)
            self._memory.add_message("assistant", final_answer or "No final answer returned.")

        if agent_event:
            observability.end_event(agent_event, data={
                "final_answer": str(final_answer)[:100],
                "iterations": count,
                "tool_calls": len(tool_calls)
            })

        return AgentCompletion.from_agent(
            model_name=self.model.__class__.__name__,
            query=user_input,
            content=final_answer or "No final answer returned.",
            tool_calls=tool_calls,
            steps=steps,
            history=working_history
        )

    def __repr__(self) -> str:
        """Provides a developer-friendly representation of the runner's state."""
        return (
            f"<{self.__class__.__name__}("
            f"Agent={self.Agent.__class__.__name__}, "
            f"Tools={len(self.tools)}, "
            f"ToolNames={[tool.name for tool in self.tools]}, "
            f"MaxIterations={self.max_iterations}, "
            f"Model='{self.model}')>"
        )

    def __str__(self) -> str:
        """Provides a user-friendly summary of the agent runner."""
        return (
            f"Agent Runner Summary:\n"
            f"  Agent Type     : {self.Agent.__class__.__name__}\n"
            f"  Tools Used     : {[tool.name for tool in self.tools]}\n"
            f"  Max Iterations : {self.max_iterations}\n"
            f"  Model          : {self.model}\n"
            f"  Query          : {self.Query}"
        )