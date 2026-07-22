"""
Async version of AgentRunner for improved performance with concurrent tool execution.

This module provides AsyncAgentRunner which supports async tool execution,
streaming responses, and improved performance for I/O-bound operations.

Set ``use_function_calling=True`` to route the AgentType parser through
``model.async_call_with_tools`` instead of parsing JSON from text.
"""

from agentx_dev.Agents import AgentFormattor, AgentCompletion, AgentPrompt
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Agents.Agent import StandardParser, ToolCall, ToolError
from agentx_dev.Tools import StandardTool, StructuredTool, logger
from agentx_dev.AsyncTools import AsyncStandardTool, AsyncStructuredTool
from agentx_dev.Runner.AgentRun import ToolRegistry, _is_terminal_action, _coerce_runner_input
from typing import Dict, Callable, List, Type, Optional, Any, AsyncIterator
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


from agentx_dev.AutoSetup import get_auto_setup, ensure_initialized
from agentx_dev.Config import config
from agentx_dev.Observability import observability, EventType

_auto_setup = None  # populated lazily by _ensure_auto_setup()


def _ensure_auto_setup():
    """Idempotent: initialize auxiliary subsystems the first time a runner needs them."""
    global _auto_setup
    if _auto_setup is None:
        ensure_initialized()
        _auto_setup = get_auto_setup()
    return _auto_setup


def _read_action(parser_instance: BaseModel) -> str:
    if hasattr(parser_instance, "action"):
        return getattr(parser_instance, "action")
    if hasattr(parser_instance, "Action"):
        return getattr(parser_instance, "Action")
    raise AttributeError(
        f"Parser {type(parser_instance).__name__} has neither 'action' nor 'Action'"
    )


def _read_action_input(parser_instance: BaseModel):
    if hasattr(parser_instance, "action_input"):
        return getattr(parser_instance, "action_input")
    if hasattr(parser_instance, "Action_Input"):
        return getattr(parser_instance, "Action_Input")
    raise AttributeError(
        f"Parser {type(parser_instance).__name__} has neither 'action_input' nor 'Action_Input'"
    )


class AsyncAgentRunner:
    """Async agent runner. Same reason-act loop as :class:`AgentRunner`
    but every I/O path is awaitable.

    Typical use::

        import asyncio
        from agentx_dev import AsyncAgentRunner, AgentType, Claude

        runner = AsyncAgentRunner(
            model=Claude(),
            agent=AgentType.ReAct,
            tools=[my_async_tool],
        )
        result = asyncio.run(runner.ainvoke("..."))

    Accepts a mix of sync and async tools transparently. When at least
    one async tool is registered, a ``batch_concurrent`` meta-tool is
    auto-added so the LLM can dispatch several async calls in one turn
    via ``asyncio.gather``. Combine with ``bind_tools_natively=True``
    for the modern "parallel tool calls per turn" pattern where every
    tool_use block in one assistant response is dispatched concurrently.
    """

    def __init__(
        self,
        model: BaseChatModel,
        Agent: AgentFormattor | AgentPrompt | str | None = None,
        tools: List[StandardTool | StructuredTool | AsyncStandardTool | AsyncStructuredTool] | None = None,
        max_iterations: int | None = None,
        auto_cache: bool = True,
        auto_memory: bool = False,
        use_function_calling: bool = False,
        verbose: bool = True,
        bind_tools_natively: bool = False,
        *,
        agent: AgentFormattor | AgentPrompt | str | None = None,
        permissions: Any = None,
        include_denied_tools: bool = False,
        strict_tool_dispatch: bool = False,
    ):
        """Construct an ``AsyncAgentRunner``. Parameters mirror
        :class:`AgentRunner` -- see that docstring for the full details;
        the differences are noted here.

        Args:
            model: A ``BaseChatModel`` subclass. Both providers ship
                sync + async paths, so ``Claude()`` and ``GPT()``
                work here without changes.
            Agent: The prompt template + parser pair. Same accepted
                shapes as ``AgentRunner`` (``AgentType`` member,
                custom ``AgentFormatter``, or raw string).
            tools: Any mix of ``StandardTool`` / ``StructuredTool``
                (sync) and ``AsyncStandardTool`` /
                ``AsyncStructuredTool`` (async). Async tools use
                ``asyncio.wait_for`` for cancellable timeouts; sync
                tools run on the thread pool. When at least one async
                tool is present, ``batch_concurrent`` is auto-added
                so the LLM can batch calls via ``asyncio.gather``.
            max_iterations: Loop cap per ``ainvoke``. Default 4.
            auto_cache: Same as ``AgentRunner``: cache tool results
                by ``(name, args)``. Default True.
            auto_memory: Same as ``AgentRunner``. Default False.
            use_function_calling: Route the AgentType parser through
                native ``async_call_with_tools``. Mutually exclusive
                with ``bind_tools_natively``.
            verbose: Print step trace to stdout during ``ainvoke``.
            bind_tools_natively: Bind user tools directly as native
                FC tools; skip the AgentType parser. Multiple
                ``tool_use`` blocks per turn dispatch concurrently
                via ``asyncio.gather`` (no thread pool needed because
                async tools are cancellable natively).
            agent: PEP-8 lowercase alias for ``Agent``.
            permissions: A ``Permissions`` instance -- auto-registers
                the sandboxed ``DefaultTools`` and injects the
                filesystem hint into the system prompt.
            include_denied_tools: Surface denied capabilities as no-op
                tools. Default False.
            strict_tool_dispatch: Feed unknown-tool errors back into
                the loop so the model can retry with a valid name.
                Default False.

        Raises:
            TypeError: On missing / duplicate ``Agent``/``agent`` or
                on a bad ``permissions`` type.
            ValueError: On the mutually-exclusive combination of
                ``use_function_calling`` and ``bind_tools_natively``.
        """
        if Agent is not None and agent is not None:
            raise TypeError("AsyncAgentRunner received both 'Agent' and 'agent'; pass only one.")
        if agent is not None:
            Agent = agent
        if Agent is None:
            raise TypeError("AsyncAgentRunner missing required argument: 'agent'")
        if tools is None:
            tools = []

        # Auto-wire DefaultTools when permissions= is passed. Same shape
        # as sync AgentRunner — see that docstring + collision-detection
        # rationale.
        if permissions is not None:
            from agentx_dev.DefaultTools import DefaultTools, Permissions
            if not isinstance(permissions, Permissions):
                raise TypeError(
                    f"permissions= must be a Permissions instance, "
                    f"got {type(permissions).__name__}"
                )
            default_tools = DefaultTools.build(
                permissions, include_denied=include_denied_tools,
            )
            user_tool_names = {t.name for t in tools}
            default_tool_names = {t.name for t in default_tools}
            collision = user_tool_names & default_tool_names
            if collision:
                raise TypeError(
                    f"Tool name collision between user tools and DefaultTools: "
                    f"{sorted(collision)}. Rename your tool or drop the "
                    f"capability from permissions to avoid the conflict."
                )
            tools = list(default_tools) + list(tools)
        self.Query = ""
        self.Agent = Agent
        self.tools = tools
        self.max_iterations = max_iterations if max_iterations else 4
        self.model = model
        self.use_function_calling = use_function_calling
        self.verbose = verbose
        self.bind_tools_natively = bind_tools_natively
        if bind_tools_natively and use_function_calling:
            raise ValueError(
                "AsyncAgentRunner: use_function_calling and bind_tools_natively "
                "are mutually exclusive. Function-calling routes through the "
                "AgentType parser; native binding skips it. Pick one."
            )
        # See AgentRunner.__init__ for the strict_tool_dispatch contract.
        self.strict_tool_dispatch = strict_tool_dispatch

        self.auto_cache = auto_cache and config.caching_enabled
        self.auto_memory = auto_memory and config.memory_enabled
        _setup = _ensure_auto_setup() if (self.auto_cache or self.auto_memory) else None
        self._cache = _setup.get_global_cache() if (_setup and self.auto_cache) else None
        self._memory = _setup.create_memory() if (_setup and self.auto_memory) else None
        self._tool_cache: Dict[str, Any] = {}

        # Batch-concurrent tool is auto-added BEFORE the registry snapshot so
        # the prompt block sees it as a real tool the LLM can call.
        self._auto_add_batch_concurrent_tool()

        # ToolRegistry owns storage + prompt-block formatting + dispatch. The
        # runner keeps thin dict views of each bucket so older user code that
        # reaches into runner.func / runner.args / runner.async_func /
        # runner.async_args keeps working. New code should go through
        # self.registry.
        self.registry = ToolRegistry(self.tools)
        self.registry.configure_cache(self._cache, cache_ttl=config.cache_ttl)
        self.func: Dict[str, Callable] = self.registry.sync_std
        self.args: Dict[str, Dict] = self.registry.sync_struct
        self.async_func: Dict[str, Callable] = self.registry.async_std
        self.async_args: Dict[str, Dict] = self.registry.async_struct
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

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
        has_async_tools = any(
            isinstance(tool, (AsyncStandardTool, AsyncStructuredTool))
            for tool in self.tools
        )

        if not has_async_tools:
            return

        from pydantic import BaseModel as _BM

        class BatchConcurrentRequest(_BM):
            """Schema for batch concurrent requests."""
            requests: List[Dict[str, str]]

        async def batch_concurrent_executor(requests: List[Dict[str, str]]) -> str:
            """
            AUTOMATICALLY ADDED: Execute multiple async tool calls concurrently.
            """
            logger.info(f"BATCH CONCURRENT: Executing {len(requests)} calls in parallel")

            tasks = []
            valid_requests = []

            for req in requests:
                tool_name = req.get("tool")
                tool_input = req.get("input", "")

                if tool_name in self.async_func:
                    tasks.append(self.async_func[tool_name](tool_input))
                    valid_requests.append(req)
                elif tool_name in self.async_args:
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

            results = await asyncio.gather(*tasks, return_exceptions=True)

            output = {}
            for req, result in zip(valid_requests, results):
                key = f"{req['tool']}_{req['input']}"
                if isinstance(result, Exception):
                    output[key] = f"Error: {str(result)}"
                else:
                    output[key] = str(result)

            logger.info(f"BATCH CONCURRENT: Completed {len(results)} calls")
            return json.dumps(output, indent=2)

        batch_tool = AsyncStructuredTool(
            func=batch_concurrent_executor,
            args_schema=BatchConcurrentRequest,
            name="batch_concurrent",
            description=(
                "AUTOMATIC CONCURRENT EXECUTION: Use this tool when you need to call "
                "multiple async tools at the same time for faster results. "
                f"Available async tools: {', '.join([t.name for t in self.tools if isinstance(t, (AsyncStandardTool, AsyncStructuredTool))])}. "
                "Input format: list of {\"tool\": \"tool_name\", \"input\": \"value\"}. "
            ),
        )

        self.tools.append(batch_tool)
        logger.info("AUTO-ADDED: batch_concurrent tool for parallel execution of async tools")

    async def Tool_Runner(self, tool_name: str, args_str) -> Any:
        """Execute a tool. Delegates to ``self.registry.adispatch``.

        Kept as an instance method for backward compatibility — older user
        code may call ``runner.Tool_Runner(...)`` directly. New callers
        should prefer ``await runner.registry.adispatch(...)``.
        """
        return await self.registry.adispatch(tool_name, args_str)

    def _resolve_parser_step(self, response_or_call) -> Optional[BaseModel]:
        if isinstance(response_or_call, dict) and "type" in response_or_call:
            if response_or_call["type"] == "tool_use":
                return self.parser.from_function_call(response_or_call["input"])
            return None
        parsed = self.parser.from_json(response_or_call)
        if isinstance(parsed, self.parser):
            return parsed
        return None

    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentCompletion:
        if ChatHistory is not None and chat_history is not None:
            raise TypeError("Pass either 'ChatHistory' or 'chat_history', not both.")
        if chat_history is not None:
            ChatHistory = chat_history
        agent_event = None
        if config.observability_enabled:
            agent_event = observability.start_event(
                EventType.AGENT_START,
                data={"query": user_input[:100]}
            )

        self.Query = user_input

        tool_info = {
            'tools': self._tool_prompt_block,
            'tool_names': self._tool_names_block,
            'user_input': user_input,
        }
        system_prompt = self.Agent.prompt.format_map(tool_info)

        working_history = [{"role": "system", "content": system_prompt}]

        effective_history = ChatHistory
        if self.auto_memory and self._memory and effective_history is None:
            effective_history = self._memory.get_messages()

        if effective_history and isinstance(effective_history, list):
            for r in effective_history:
                if r.get('role') and r.get('content'):
                    working_history.append({'role': r['role'], 'content': r['content']})

        working_history.append({"role": "user", "content": user_input})

        logger.info(">>>>> Entering AsyncAgentRunner Mode <<<<<")
        if not isinstance(self.model, BaseChatModel):
            raise TypeError(
                f"The 'model' object must be an instance of a class that inherits "
                f"from BaseChatModel, but got type {type(self.model).__name__}."
            )

        parser_tool_spec = self.Agent.to_tool_spec() if self.use_function_calling else None
        parser_tool_name = self.parser.__name__ if self.use_function_calling else None

        # Native binding mode (#16 follow-up): pre-build the per-user-tool
        # specs once + a synthetic "respond" tool the LLM calls to signal
        # completion. Doing this once outside the loop avoids re-computing
        # JSON schemas every iteration.
        native_tool_specs = None
        if self.bind_tools_natively:
            native_tool_specs = list(self.registry.to_tool_specs())
            native_tool_specs.append({
                "name": "respond",
                "description": (
                    "Return the final answer to the user. Call this when you have "
                    "all the information you need."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "description": "The final answer."},
                    },
                    "required": ["answer"],
                },
            })

        count = 1
        tool_calls: List[ToolCall] = []
        steps: List[str] = []
        final_answer: Optional[str] = None

        while count <= self.max_iterations:
            if self.bind_tools_natively:
                # Native mode: LLM picks from user tools directly. Multiple
                # tool calls in one turn → dispatched concurrently via
                # asyncio.gather. The synthetic "respond" tool signals done.
                call_result = await self.model.async_call_with_tools(
                    messages=working_history,
                    tools=native_tool_specs,
                    force_tool=None,
                )

                if call_result.get("type") != "tool_use":
                    # Model emitted text instead of a tool call — treat as final.
                    final_answer = call_result.get("text", "")
                    working_history.append({"role": "assistant", "content": final_answer})
                    break

                turn_calls = call_result.get("tool_calls") or [{
                    "name": call_result["name"],
                    "input": call_result["input"],
                    "id": call_result.get("id") or f"call_{count}_0",
                }]

                # Did the LLM call "respond"? That's the loop terminator.
                respond_call = next((c for c in turn_calls if c["name"] == "respond"), None)
                if respond_call is not None:
                    final_answer = str(respond_call["input"].get("answer", ""))
                    # Still record the assistant turn so history reflects what happened.
                    working_history.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": respond_call.get("id") or f"call_{count}_respond",
                            "type": "function",
                            "function": {
                                "name": "respond",
                                "arguments": json.dumps(respond_call["input"]),
                            },
                        }],
                    })
                    break

                # Otherwise: dispatch every non-respond call concurrently.
                non_respond = [c for c in turn_calls if c["name"] != "respond"]
                for i, c in enumerate(non_respond):
                    if not c.get("id"):
                        c["id"] = f"call_{count}_{i}"

                working_history.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["input"]),
                        },
                    } for c in non_respond],
                })

                async def _run_one(call):
                    # StandardTool/AsyncStandardTool got synthesized {input: str}
                    # specs — unwrap so dispatch sees the raw string.
                    args = call["input"]
                    if not self.registry.is_structured(call["name"]) and isinstance(args, dict) and "input" in args:
                        args = args["input"]
                    return await self.registry.adispatch(call["name"], args)

                # return_exceptions=True: if one dispatch raises (e.g. a
                # bug inside ToolRegistry itself, not a tool author's bug
                # which is already wrapped as ToolError), the other parallel
                # dispatches still complete and we record the failure as a
                # ToolError. Previously a single raise killed the whole
                # agent loop mid-iteration.
                results = await asyncio.gather(
                    *(_run_one(c) for c in non_respond), return_exceptions=True
                )

                # Record each tool result with its tool_call_id correlation.
                for call, result in zip(non_respond, results):
                    if isinstance(result, BaseException):
                        # Promote the raised exception to a ToolError so the
                        # downstream history-recording branch is unchanged.
                        result = ToolError(
                            f"unhandled exception in dispatch for '{call['name']}': {result}",
                            tool=call["name"],
                            cause=result,
                        )
                    is_error = isinstance(result, ToolError)
                    step_description = f"Step {count}: {call['name']} with {call['input']}"
                    steps.append(step_description)
                    if not is_error:
                        tool_calls.append(ToolCall(
                            name=call["name"],
                            args={"Input": call["input"]},
                            result=str(result),
                        ))
                    working_history.append({
                        "role": "tool",
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "content": f"Error: {result}" if is_error else str(result),
                    })

                count += 1
                continue  # Skip the legacy single-action handling below.

            if self.use_function_calling:
                call_result = await self.model.async_call_with_tools(
                    messages=working_history,
                    tools=[parser_tool_spec],
                    force_tool=parser_tool_name,
                )

                # The parser path forces a single tool (the AgentType), so
                # tool_calls always has length 1 here. The runner-level
                # concurrency on parallel user-tool calls happens BELOW, in
                # the Tool_Runner dispatch, after the parser has chosen
                # which user-facing action(s) to run.
                parser_instance = self._resolve_parser_step(call_result)

                if parser_instance is None:
                    final_answer = call_result.get("text", "")
                    working_history.append({"role": "assistant", "content": final_answer})
                    break

                working_history.append({
                    "role": "assistant",
                    "content": json.dumps(call_result["input"]),
                })
            else:
                response = await self.model.async_initialize(messages=working_history)
                working_history.append({"role": "assistant", "content": response})
                parser_instance = self._resolve_parser_step(response)

                if parser_instance is None:
                    final_answer = response
                    break

            action = _read_action(parser_instance)
            action_input = _read_action_input(parser_instance)

            step_description = f"Step {count}: {action} with {action_input}"
            steps.append(step_description)

            # Terminal-action check — tolerant of format variants that
            # LLMs emit under use_function_calling=True where the schema
            # doesn't constrain `action` to a specific spelling.
            if _is_terminal_action(action):
                final_answer = action_input
                break

            thought = getattr(parser_instance, "Thought", None)
            if thought and self.verbose:
                print(f"\x1B[36m[thought] {thought}\x1B[0m")
            elif thought:
                logger.info(f"thought: {thought}")

            # Strip OpenAI's "functions." prefix before the known-tools
            # check — otherwise a valid tool call falls through to the
            # implicit-final guardrail and leaks Thought as user text.
            normalized_action = self.registry._normalize_tool_name(action)
            if normalized_action != action:
                if self.verbose:
                    print(
                        f"\x1B[3;33m[loop] normalized action "
                        f"'{action}' -> '{normalized_action}'\x1B[0m"
                    )
                action = normalized_action

            # Implicit-final guardrail — see the sync AgentRunner for
            # the full rationale. If strict_tool_dispatch=True AND the
            # unknown action looks like a tool identifier (no spaces)
            # AND we still have iteration budget, feed an error
            # observation back to the model and let it retry.
            known_tools = set(self.registry.sync_std) | set(self.registry.sync_struct) \
                        | set(self.registry.async_std) | set(self.registry.async_struct)
            if not action or action not in known_tools:
                looks_like_tool_id = (
                    bool(action) and " " not in action and len(action) < 80
                )
                if (
                    self.strict_tool_dispatch
                    and looks_like_tool_id
                    and count < self.max_iterations
                ):
                    available = sorted(known_tools)
                    error_content = (
                        f"[framework] error: '{action}' is not a "
                        f"registered tool. Available tools: {available}. "
                        f"Options: (1) call one of them with the correct "
                        f"argument shape; (2) emit action=\"Final_Answer\" "
                        f"with your final response text in action_input "
                        f"if you're done."
                    )
                    tc_id = getattr(self, "_last_function_call_id", None) if self.use_function_calling else None
                    err_msg = {
                        "role": "tool" if tc_id else "function",
                        "name": "tool_call_error",
                        "content": error_content,
                    }
                    if tc_id:
                        err_msg["tool_call_id"] = tc_id
                    working_history.append(err_msg)
                    if self.verbose:
                        print(
                            f"\x1B[1;33m[loop] strict-retry: action "
                            f"'{action[:60]}' unknown; feeding error "
                            f"back so the model can retry\x1B[0m"
                        )
                    self._last_function_call_id = None
                    count += 1
                    continue

                # Non-strict OR strict-can't-help: implicit-final.
                if action_input and str(action_input).strip():
                    final_answer = str(action_input)
                elif action and action.strip() and " " in action:
                    final_answer = str(action)
                else:
                    final_answer = (
                        "(agent emitted an unrecognized action and no "
                        "answer text — try rephrasing or re-run)"
                    )
                if self.verbose:
                    preview = (action or "<empty>")[:60]
                    print(
                        f"\x1B[1;33m[loop] implicit-final: action "
                        f"'{preview}' is not a registered tool and not "
                        f"a Final_Answer variant; returning action_input "
                        f"as final (Thought NOT leaked)\x1B[0m"
                    )
                break

            if self.verbose:
                print(f"\x1B[3;33m[tool] Invoking '{action}' with args: {action_input}\x1B[0m")
            else:
                logger.info(f"tool invoke: {action}({action_input})")

            tool_response = await self.Tool_Runner(action, action_input)
            if self.verbose:
                print(f"\x1B[32m[tool] Response: {str(tool_response)[:300]}\x1B[0m")
            else:
                logger.info(f"tool response: {str(tool_response)[:300]}")

            if isinstance(tool_response, ToolError):
                working_history.append({
                    "role": "function",
                    "name": "tool_call_error",
                    "content": f"Error: {tool_response}",
                })
            else:
                working_history.append({
                    "role": "function",
                    "name": action,
                    "content": str(tool_response),
                })

                tool_calls.append(ToolCall(
                    name=action,
                    args={"Input": action_input},
                    result=str(tool_response),
                ))

            count += 1

        if self.verbose:
            print(f"\x1B[32m[final] {final_answer}\x1B[0m")
        else:
            logger.info(f"final_answer: {final_answer}")

        if self.auto_memory and self._memory:
            self._memory.add_message("user", user_input)
            self._memory.add_message("assistant", final_answer or "No final answer returned.")

        if agent_event:
            observability.end_event(agent_event, data={
                "final_answer": str(final_answer)[:100],
                "iterations": count,
                "tool_calls": len(tool_calls),
            })

        return AgentCompletion.from_agent(
            model_name=self.model.__class__.__name__,
            query=user_input,
            content=final_answer or "No final answer returned.",
            tool_calls=tool_calls,
            steps=steps,
            history=working_history,
        )

    async def ainvoke(
        self,
        user_input: Any,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
        *,
        chat_history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentCompletion:
        """Canonical async entry point. Alias for ``Initialize`` with the
        same shape normalization as ``AgentRunner.invoke`` — accepts a
        bare query string, a message list, or ``{"messages": [...]}``.
        See ``_coerce_runner_input`` for the exact rules."""
        if ChatHistory is not None and chat_history is not None:
            raise TypeError("Pass either 'ChatHistory' or 'chat_history', not both.")
        base_history = chat_history if chat_history is not None else ChatHistory
        query, hist = _coerce_runner_input(user_input, base_history)
        return await self.Initialize(query, None, stream, chat_history=hist)

    async def astream(
        self,
        user_input: Any,
        chat_history: Optional[List[Dict[str, str]]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Async streaming. Yields the same step-event dicts as
        ``AgentRunner.stream`` — ``thought`` / ``tool_call`` / ``tool_result``
        / ``final`` / ``completion`` — but driven by an async loop so it
        composes with async tools and ``async_call_with_tools``.

        For now the loop runs to completion internally and replays its
        events; full async-iterator-with-await-points integration belongs
        in a follow-up. Callers consume the result identically::

            async for event in runner.astream("greet Alice"):
                print(event)
        """
        # Captures every event so we can yield them after the loop runs.
        # Wrapping Initialize this way keeps behavior identical to ainvoke;
        # the wins from a true async generator (mid-loop yield to the
        # caller) require restructuring Initialize itself, deferred until
        # there's a concrete user need for it.
        events: List[Dict[str, Any]] = []

        # Hook every step by reaching into the same primitives Initialize
        # uses. Easiest: capture the completion + tool_calls/steps and
        # synthesize the event stream from them so callers see the same
        # shape as sync stream().
        query, hist = _coerce_runner_input(user_input, chat_history)
        completion = await self.Initialize(query, chat_history=hist)
        for step_desc in completion.steps:
            # Best-effort decode of "Step N: action with input"
            try:
                _, payload = step_desc.split(":", 1)
                action_part, _, args_part = payload.strip().partition(" with ")
                events.append({"type": "tool_call", "name": action_part.strip(), "args": args_part.strip()})
            except Exception:
                events.append({"type": "step", "content": step_desc})
        for tc in completion.tool_calls:
            events.append({
                "type": "tool_result",
                "name": tc.name,
                "result": tc.result,
                "is_error": False,
            })
        events.append({"type": "final", "content": completion.content})
        events.append({"type": "completion", "completion": completion})
        for event in events:
            yield event

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__}("
            f"Agent={self.Agent.__class__.__name__}, "
            f"Tools={len(self.tools)}, "
            f"ToolNames={[tool.name for tool in self.tools]}, "
            f"MaxIterations={self.max_iterations}, "
            f"FunctionCalling={self.use_function_calling}, "
            f"Model='{self.model}')>"
        )
