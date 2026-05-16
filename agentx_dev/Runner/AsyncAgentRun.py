"""
Async version of AgentRunner for improved performance with concurrent tool execution.

This module provides AsyncAgentRunner which supports async tool execution,
streaming responses, and improved performance for I/O-bound operations.
"""

from agentx_dev.Agents import AgentFormattor, AgentCompletion, AgentPrompt
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Agents.Agent import StandardParser, ToolCall
from agentx_dev.Tools import StandardTool, StructuredTool, logger
from agentx_dev.AsyncTools import AsyncStandardTool, AsyncStructuredTool
from typing import Dict, Callable, List, Type, Optional, AsyncIterator
from pydantic import BaseModel, Field
import asyncio
import json


def _format_tool_for_prompt(tool) -> str:
    """Return a clean, LLM-readable tool description for the system prompt."""
    from agentx_dev.Tools import StructuredTool as ST
    if isinstance(tool, ST):
        param_names = list(tool.args_schema.model_fields.keys())
        return f"{tool.description} (required params: {', '.join(param_names)})"
    return tool.description


# Auto-initialize enhanced features
from agentx_dev.AutoSetup import get_auto_setup, ensure_initialized
from agentx_dev.Config import config
from agentx_dev.Observability import observability, EventType

# Ensure features are initialized
ensure_initialized()
_auto_setup = get_auto_setup()


class AsyncAgentRunner:
    """
    Async version of AgentRunner with support for concurrent tool execution.

    This runner enables:
    - Concurrent execution of multiple async tools
    - Streaming responses from LLMs
    - Better performance for I/O-bound operations
    """

    def __init__(
        self,
        model: BaseChatModel,
        Agent: AgentFormattor | AgentPrompt | str,
        tools: List[StandardTool | StructuredTool | AsyncStandardTool | AsyncStructuredTool],
        max_iterations: int | None = None,
        auto_cache: bool = True,  # Automatically cache tool results
        auto_memory: bool = False,  # Automatically manage conversation memory
    ):
        """
        Initializes the AsyncAgentRunner.

        Args:
            model (BaseChatModel): The LLM model to use.
            Agent: The agent's prompt template.
            tools (List): List of tool instances (sync or async).
            max_iterations (Optional[int]): Maximum number of tool-calling cycles.
            auto_cache (bool): Enable automatic tool result caching (default: True).
            auto_memory (bool): Enable automatic memory management (default: False).
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

        # Tool registries
        self.func: Dict[str, Callable] = {}
        self.async_func: Dict[str, Callable] = {}
        self.args: Dict[str, Dict] = {}
        self.async_args: Dict[str, Dict] = {}

        # Auto-add batch concurrent tool if we have async tools
        self._auto_add_batch_concurrent_tool()

        # Register tools
        for tool_instance in self.tools:
            if isinstance(tool_instance, StandardTool):
                self.func[tool_instance.name] = tool_instance.func

            elif isinstance(tool_instance, StructuredTool):
                self.args[tool_instance.name] = {
                    "func": tool_instance.func,
                    "args_schema": tool_instance.args_schema
                }

            elif isinstance(tool_instance, AsyncStandardTool):
                self.async_func[tool_instance.name] = tool_instance.func

            elif isinstance(tool_instance, AsyncStructuredTool):
                self.async_args[tool_instance.name] = {
                    "func": tool_instance.func,
                    "args_schema": tool_instance.args_schema
                }
            else:
                tool_type = type(tool_instance).__name__
                raise TypeError(
                    f"Unsupported tool type found in 'tools' list. "
                    f"Expected StandardTool, StructuredTool, AsyncStandardTool, or AsyncStructuredTool, "
                    f"but got '{tool_type}'."
                )

        # Build tool prompt blocks (after batch_concurrent may have been auto-added)
        self._tool_prompt_block = '\n'.join([f"- {t.name} : {_format_tool_for_prompt(t)}" for t in self.tools])
        self._tool_names_block = ', '.join([t.name for t in self.tools])

        # Validate Agent prompt template
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
            raise ValueError(
                "The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}, "
                "or an AgentFormattor instance."
            )

    def _auto_add_batch_concurrent_tool(self):
        """
        Automatically create and add a batch concurrent tool if async tools exist.
        This allows the LLM to execute multiple async tools concurrently for better performance.
        """
        # Check if we have any async tools
        has_async_tools = any(
            isinstance(tool, (AsyncStandardTool, AsyncStructuredTool))
            for tool in self.tools
        )

        if not has_async_tools:
            return  # No async tools, skip batch tool creation

        # Create the batch concurrent tool
        from pydantic import BaseModel

        class BatchConcurrentRequest(BaseModel):
            """Schema for batch concurrent requests."""
            requests: List[Dict[str, str]]  # [{"tool": "tool_name", "input": "value"}]

        async def batch_concurrent_executor(requests: List[Dict[str, str]]) -> str:
            """
            AUTOMATICALLY ADDED: Execute multiple async tool calls concurrently.

            This tool is automatically available when you have async tools.
            Use it to call multiple tools at once for faster execution!

            Args:
                requests: List of dicts like [{"tool": "weather", "input": "NYC"}, ...]

            Returns:
                JSON string with all results
            """
            logger.info(f"BATCH CONCURRENT: Executing {len(requests)} calls in parallel")

            # Create tasks for all requests
            tasks = []
            valid_requests = []

            for req in requests:
                tool_name = req.get("tool")
                tool_input = req.get("input", "")

                # Check if tool exists in async functions
                if tool_name in self.async_func:
                    tasks.append(self.async_func[tool_name](tool_input))
                    valid_requests.append(req)
                elif tool_name in self.async_args:
                    # Structured async tool
                    func = self.async_args[tool_name]["func"]
                    schema = self.async_args[tool_name]["args_schema"]
                    try:
                        if isinstance(tool_input, str):
                            parsed = json.loads(tool_input)
                        else:
                            parsed = tool_input
                        validated = schema(**parsed)
                        tasks.append(func(**validated.model_dump()))
                        valid_requests.append(req)
                    except Exception as e:
                        logger.warning(f"Skipping invalid structured tool call: {e}")

            if not tasks:
                return json.dumps({"error": "No valid async tools found in requests"})

            # Execute all concurrently!
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Format output
            output = {}
            for req, result in zip(valid_requests, results):
                key = f"{req['tool']}_{req['input']}"
                if isinstance(result, Exception):
                    output[key] = f"Error: {str(result)}"
                else:
                    output[key] = str(result)

            logger.info(f"BATCH CONCURRENT: Completed {len(results)} calls")

            return json.dumps(output, indent=2)

        # Create the batch tool
        batch_tool = AsyncStructuredTool(
            func=batch_concurrent_executor,
            args_schema=BatchConcurrentRequest,
            name="batch_concurrent",
            description=(
                "AUTOMATIC CONCURRENT EXECUTION: Use this tool when you need to call "
                "multiple async tools at the same time for faster results. "
                f"Available async tools: {', '.join([t.name for t in self.tools if isinstance(t, (AsyncStandardTool, AsyncStructuredTool))])}. "
                "Input format: list of {\"tool\": \"tool_name\", \"input\": \"value\"}. "
                "Example: [{\"tool\": \"weather\", \"input\": \"NYC\"}, {\"tool\": \"weather\", \"input\": \"LA\"}]. "
                "This will execute all calls in parallel instead of sequentially!"
            )
        )

        # Add to tools list
        self.tools.append(batch_tool)
        logger.info("AUTO-ADDED: batch_concurrent tool for parallel execution of async tools")

    async def Tool_Runner(self, tool_name: str, args_str: str) -> str:
        """
        Executes a tool (async or sync) with the given arguments.
        Automatically caches results if auto_cache is enabled.

        Args:
            tool_name (str): The name of the tool to run.
            args_str (str): Arguments for the tool (JSON string for structured tools).

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

        # Check async structured tools first
        if tool_name in self.async_args:
            schema = self.async_args[tool_name].get('args_schema')
            func = self.async_args[tool_name].get('func')
            if schema and func:
                try:
                    parsed_args = args_str
                    if isinstance(args_str, str):
                        parsed_args = json.loads(args_str)

                    # Special handling for batch_concurrent when action_input is a List
                    if tool_name == "batch_concurrent" and isinstance(parsed_args, list):
                        # Wrap the list in the expected schema format
                        parsed_args = {"requests": parsed_args}

                    validated_args = schema(**parsed_args)
                    logger.info(f"ASYNC ACTION: {tool_name}, INPUT: {parsed_args}")

                    result = await func(**validated_args.model_dump())
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
                    logger.error(f"Error executing async structured tool '{tool_name}': {e}", exc_info=True)
                    return f"Error executing async structured tool '{tool_name}': {e}"

        # Check async standard tools
        elif tool_name in self.async_func:
            try:
                logger.info(f"ASYNC ACTION: {tool_name}, INPUT: {args_str}")
                function = self.async_func[tool_name]

                if not args_str:
                    result = await function()
                else:
                    result = await function(args_str)

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
                logger.error(f"Error executing async standard tool '{tool_name}': {e}", exc_info=True)
                return f"Error executing async standard tool '{tool_name}': {e}"

        # Check sync structured tools
        elif tool_name in self.args:
            schema = self.args[tool_name].get('args_schema')
            func = self.args[tool_name].get('func')
            if schema and func:
                try:
                    # Normalize: parse JSON string to dict first if needed
                    if isinstance(args_str, str):
                        parsed_args = json.loads(args_str)
                    else:
                        parsed_args = args_str  # already a dict

                    # Validate once with Pydantic, then call once
                    validated_args = schema(**parsed_args)
                    result = func(**validated_args.model_dump())

                    logger.info(f"ACTION: {tool_name}, INPUT: {parsed_args}")
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
                    logger.error(f"Error executing structured tool '{tool_name}': {e}", exc_info=True)
                    return f"Error executing structured tool '{tool_name}': {e}"

        # Check sync standard tools
        elif tool_name in self.func:
            try:
                logger.info(f"ACTION: {tool_name}, INPUT: {args_str}")
                function = self.func[tool_name]

                if not args_str:
                    result = function()
                else:
                    result = function(args_str)

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
                logger.error(f"Error executing standard tool '{tool_name}': {e}", exc_info=True)
                return f"Error executing standard tool '{tool_name}': {e}"

        else:
            logger.warning(
                f"Tool '{tool_name}' not found. Available tools: "
                f"{list(self.func.keys()) + list(self.args.keys()) + list(self.async_func.keys()) + list(self.async_args.keys())}"
            )
            return f"Error: Tool '{tool_name}' not found. Please double-check the tool name."

    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False
    ) -> AgentCompletion:
        """
        The main async agent execution loop with automatic enhancements.

        Args:
            user_input (str): Query to run the Agent with.
            ChatHistory (Optional[List[Dict[str, str]]]): Previous conversation history.
                        If auto_memory is enabled and this is None, automatic memory is used.
            stream (bool): Whether to stream responses.

        Returns:
            AgentCompletion: The result of the agent execution.
        """
        # Track agent execution if observability enabled
        agent_event = None
        if config.observability_enabled:
            agent_event = observability.start_event(
                EventType.AGENT_START,
                data={"query": user_input[:100]}
            )

        self.Query = user_input

        # Build fresh system prompt each call (stateless)
        tool_info = {
            'tools': self._tool_prompt_block,
            'tool_names': self._tool_names_block,
            'user_input': user_input
        }
        system_prompt = self.Agent.prompt.format_map(tool_info)

        working_history = [{"role": "system", "content": system_prompt}]

        # Use automatic memory if enabled and no manual history provided
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

        logger.info(">>>>> Entering AsyncAgentRunner Mode <<<<<")
        if not isinstance(self.model, BaseChatModel):
            raise TypeError(
                f"The 'model' object must be an instance of a class that inherits "
                f"from BaseChatModel, but got type {type(self.model).__name__}."
            )

        count = 1
        tool_calls: List[ToolCall] = []
        steps: List[str] = []
        final_answer = None

        while count <= self.max_iterations:
            response = await self.model.async_initialize(messages=working_history)
            working_history.append({"role": "assistant", "content": response})

            model = self.Agent.Agent.from_json(response)

            if not isinstance(model, self.Agent.Agent):
                final_answer = model
                break

            step_description = f"Step {count}: {model.action} with {model.action_input}"
            steps.append(step_description)

            working_history.append({
                "role": "assistant",
                "content": json.dumps({
                    "action": model.action,
                    "action_input": model.action_input
                })
            })

            if model.action == "Final_Answer":
                final_answer = model.action_input
                break

            print(f"\x1B[3;33m🛠️  Invoking tool: '{model.action}' with args: {model.action_input}\x1B[0m")

            # Use async tool runner
            tool_response = await self.Tool_Runner(model.action, model.action_input)
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

        # Update automatic memory if enabled
        if self.auto_memory and self._memory:
            self._memory.add_message("user", user_input)
            self._memory.add_message("assistant", final_answer or "No final answer returned.")

        # Complete agent execution tracking
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
        return (
            f"<{self.__class__.__name__}("
            f"Agent={self.Agent.__class__.__name__}, "
            f"Tools={len(self.tools)}, "
            f"ToolNames={[tool.name for tool in self.tools]}, "
            f"MaxIterations={self.max_iterations}, "
            f"Model='{self.model}')>"
        )
