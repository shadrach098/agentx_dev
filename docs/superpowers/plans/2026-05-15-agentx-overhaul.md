# AgentX Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 13 confirmed bugs and architectural flaws in AgentX, then add true async LLM support and Anthropic/Claude model integration.

**Architecture:** The runner is refactored to be stateless-per-call (history lives in the call, not the instance), the tool-arg parsing bug is eliminated by validating before executing, and the async runner gets a real async LLM interface. A `Claude` chat model is added alongside `GPT`.

**Tech Stack:** Python 3.11+, Pydantic v2, OpenAI SDK, Anthropic SDK, asyncio, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `agentx_dev/Agents/Agent.py` | Modify | Remove duplicate `ToolCall`, make `BaseChatModel` abstract |
| `agentx_dev/ChatModel.py` | Modify | Add ABC interface, add async support to GPT, add `Claude` class |
| `agentx_dev/Runner/AgentRun.py` | Modify | Fix stateful prompt bug, fix tool-arg parsing, fix history duplication, return history |
| `agentx_dev/Runner/AsyncAgentRun.py` | Modify | Fix sync LLM call in async runner, fix tool-arg parsing |
| `agentx_dev/Tools.py` | Modify | Remove description mutation from registration |
| `agentx_dev/Runner/__init__.py` | Modify | Export Claude |
| `agentx_dev/__init__.py` | Modify | Export Claude |
| `promptTemplate.yaml` | Modify | Fix ChainOfThought invalid JSON |
| `tests/test_runner.py` | Create | Tests for stateless runner, tool parsing, history return |
| `tests/test_chatmodel.py` | Create | Tests for GPT/Claude interface contract |
| `tests/test_tools.py` | Create | Tests for StandardTool/StructuredTool parsing |

---

## Task 1: Remove Duplicate `ToolCall` and Add ABC to `BaseChatModel`

**Files:**
- Modify: `agentx_dev/Agents/Agent.py`
- Modify: `agentx_dev/ChatModel.py`
- Create: `tests/test_chatmodel.py`

- [ ] **Step 1: Write the failing test for BaseChatModel interface**

Create `tests/test_chatmodel.py`:

```python
import pytest
from agentx_dev.ChatModel import BaseChatModel


def test_base_chat_model_cannot_be_instantiated_directly():
    """BaseChatModel must be abstract — instantiating it directly should fail."""
    with pytest.raises(TypeError):
        BaseChatModel()


def test_subclass_without_initialize_cannot_be_instantiated():
    """A subclass that doesn't implement Initialize() should also fail."""
    class Incomplete(BaseChatModel):
        pass

    with pytest.raises(TypeError):
        Incomplete()


def test_subclass_with_initialize_can_be_instantiated():
    """A properly implemented subclass should work."""
    class MockModel(BaseChatModel):
        def Initialize(self, messages):
            return "mock response"

    m = MockModel()
    assert m.Initialize([]) == "mock response"
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_chatmodel.py -v
```

Expected: FAIL — `BaseChatModel()` succeeds (it's not abstract yet).

- [ ] **Step 3: Fix `BaseChatModel` in `agentx_dev/ChatModel.py`**

Replace the current `BaseChatModel` class (lines 12-21) with:

```python
from abc import ABC, abstractmethod

class BaseChatModel(ABC):
    """Abstract base class. All chat model integrations must implement Initialize()."""

    @abstractmethod
    def Initialize(self, messages) -> str:
        """Send messages to the LLM and return the response string."""
        ...
```

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/test_chatmodel.py -v
```

Expected: PASS (3/3).

- [ ] **Step 5: Remove the duplicate `ToolCall` definition in `agentx_dev/Agents/Agent.py`**

Lines 298-307 currently define `ToolCall` twice identically. Delete the second definition (lines 303-307) so only one remains:

```python
class ToolCall(BaseModel):
    name: str
    args: Dict[str, Any]
    result: str
```

(Just one copy — remove the second block entirely.)

- [ ] **Step 6: Run existing tests / import check**

```
python -c "from agentx_dev.Agents.Agent import ToolCall; print('OK')"
```

Expected: `OK`

- [ ] **Step 7: Commit**

```
git add agentx_dev/ChatModel.py agentx_dev/Agents/Agent.py tests/test_chatmodel.py
git commit -m "fix: make BaseChatModel abstract, remove duplicate ToolCall"
```

---

## Task 2: Fix the Stateful Prompt / Per-Call History Bug in `AgentRunner`

This is the most critical bug. The system prompt has the first user query burned into it forever after the first call.

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py`
- Create: `tests/test_runner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_runner.py`:

```python
import pytest
from unittest.mock import MagicMock
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Tools import StandardTool


def make_runner(mock_responses):
    """Helper: returns a runner with a mock model that returns responses in order."""
    model = MagicMock(spec=BaseChatModel)
    model.Initialize.side_effect = mock_responses

    def dummy_tool(input: str) -> str:
        """A dummy tool for testing."""
        return f"tool_result:{input}"

    tool = StandardTool(func=dummy_tool, name="dummy", description="A dummy tool.")
    return AgentRunner(model=model, Agent=AgentType.ReAct, tools=[tool])


def test_second_call_does_not_use_first_querys_system_prompt():
    """
    Calling Initialize() twice on the same runner must NOT embed the first
    query in the system prompt of the second call.
    """
    # Both calls produce a Final_Answer immediately
    final_answer_1 = '{"Thought": "t", "action": "Final_Answer", "action_input": "answer1"}'
    final_answer_2 = '{"Thought": "t", "action": "Final_Answer", "action_input": "answer2"}'

    runner = make_runner([final_answer_1, final_answer_2])

    result1 = runner.Initialize("first question")
    result2 = runner.Initialize("second question")

    # Extract system prompt seen during second call
    second_call_messages = runner.model.Initialize.call_args_list[1][1]["messages"]
    system_content = next(m["content"] for m in second_call_messages if m["role"] == "system")

    assert "first question" not in system_content, (
        "The system prompt for the second call must NOT contain the first query."
    )
    assert "second question" in system_content


def test_chat_history_not_duplicated_across_calls():
    """Passing the same ChatHistory to two calls must not duplicate messages."""
    final_answer = '{"Thought": "t", "action": "Final_Answer", "action_input": "done"}'
    runner = make_runner([final_answer, final_answer])

    history = [{"role": "assistant", "content": "hello"}]
    runner.Initialize("q1", ChatHistory=history)
    runner.Initialize("q2", ChatHistory=history)

    # Count how many times "hello" appears in the working history
    hello_count = sum(
        1 for m in runner.model.Initialize.call_args_list[1][1]["messages"]
        if m.get("content") == "hello"
    )
    assert hello_count == 1, f"Expected 1 copy of history message, got {hello_count}"


def test_history_returned_in_completion():
    """AgentCompletion.history must not be None."""
    final_answer = '{"Thought": "t", "action": "Final_Answer", "action_input": "done"}'
    runner = make_runner([final_answer])
    result = runner.Initialize("test")
    assert result.history is not None
    assert isinstance(result.history, list)
    assert len(result.history) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_runner.py -v
```

Expected: FAIL on all three.

- [ ] **Step 3: Refactor `AgentRunner.Initialize()` to be stateless-per-call**

In `agentx_dev/Runner/AgentRun.py`, replace the entire `Initialize()` method with the following. The key changes:
- Build a fresh `working_history` list on each call instead of mutating `self.history`
- Format the system prompt fresh each call using the current `user_input`
- Remove the UUID holder mechanism entirely (it was the root cause)
- Return history in `AgentCompletion`

```python
def Initialize(self, user_input: str, ChatHistory: Optional[List[Dict[str, str]]] = None) -> AgentCompletion:
    """
    The main agent execution loop.

    Args:
        user_input (str): The user's query.
        ChatHistory: Optional prior conversation messages.

    Returns:
        AgentCompletion: The result including history.
    """
    agent_event = None
    if config.observability_enabled:
        agent_event = observability.start_event(
            EventType.AGENT_START,
            data={"query": user_input[:100]}
        )

    self.Query = user_input

    # Build fresh system prompt for this call
    tool_info = {
        'tools': '\n'.join([f"- {t.name} : {t.description}" for t in self.tools]),
        'tool_names': ', '.join([t.name for t in self.tools]),
        'user_input': user_input
    }
    system_prompt = self.Agent.prompt.format_map(tool_info)

    # Build a fresh working history for this call — never mutate self.history
    working_history: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

    # Use automatic memory if enabled and no manual history provided
    effective_history = ChatHistory
    if self.auto_memory and self._memory and effective_history is None:
        effective_history = self._memory.get_messages()

    if effective_history and isinstance(effective_history, list):
        for r in effective_history:
            if r.get('role') and r.get('content'):
                content = r.get('content')
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
            "content": json.dumps({
                "action": model.action,
                "action_input": model.action_input
            })
        })

        if model.action == "Final_Answer":
            final_answer = model.action_input
            break

        print(f"\x1B[3;33m🛠️  Invoking tool: '{model.action}' with args: {model.action_input}\x1B[0m")
        tool_response = self.Tool_Runner(model.action, model.action_input)
        print(f"\x1B[32m🛠️  Tool Response : {str(tool_response)}\x1B[0m")

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
```

Also remove the `cleaning()` dead-code method (lines 141-148 of the original file) and remove `self.history`, `self.agent_scratchpad`, `self.Steps`, and `self.holder` from `__init__` since they are no longer needed.

- [ ] **Step 4: Run tests**

```
pytest tests/test_runner.py -v
```

Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```
git add agentx_dev/Runner/AgentRun.py tests/test_runner.py
git commit -m "fix: make AgentRunner stateless-per-call, fix prompt corruption bug, return history"
```

---

## Task 3: Fix Structured Tool Argument Parsing Bug

The bug: `func(**parsed_args)` executes before any parsing/validation, running the function twice (or with wrong args).

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py`
- Modify: `agentx_dev/Runner/AsyncAgentRun.py`
- Create: `tests/test_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools.py`:

```python
import pytest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel
from agentx_dev.Tools import StructuredTool, StandardTool


def test_structured_tool_executes_exactly_once_with_dict_args():
    """Tool function must be called exactly once, with validated args."""
    call_count = 0

    class AddArgs(BaseModel):
        a: int
        b: int

    def add(a: int, b: int) -> int:
        nonlocal call_count
        call_count += 1
        return a + b

    tool = StructuredTool(func=add, args_schema=AddArgs, name="add", description="Add two numbers.")

    # Simulate what Tool_Runner does: receive a dict from LLM
    from agentx_dev.Runner.AgentRun import AgentRunner
    import json

    model = MagicMock()
    model.Initialize.return_value = '{"Thought":"t","action":"Final_Answer","action_input":"done"}'

    from agentx_dev.Agents.Agent import AgentType
    from agentx_dev.ChatModel import BaseChatModel

    class MockModel(BaseChatModel):
        def Initialize(self, messages):
            return '{"Thought":"t","action":"Final_Answer","action_input":"done"}'

    runner = AgentRunner(model=MockModel(), Agent=AgentType.ReAct, tools=[tool])

    # Call Tool_Runner directly
    result = runner.Tool_Runner("add", {"a": 3, "b": 4})
    assert result == 7, f"Expected 7, got {result}"
    assert call_count == 1, f"Expected function to be called once, was called {call_count} times"


def test_structured_tool_executes_exactly_once_with_string_args():
    """Tool function must be called exactly once when args is a JSON string."""
    import json
    call_count = 0

    class AddArgs(BaseModel):
        a: int
        b: int

    def add(a: int, b: int) -> int:
        nonlocal call_count
        call_count += 1
        return a + b

    tool = StructuredTool(func=add, args_schema=AddArgs, name="add", description="Add two numbers.")

    from agentx_dev.Agents.Agent import AgentType
    from agentx_dev.ChatModel import BaseChatModel

    class MockModel(BaseChatModel):
        def Initialize(self, messages):
            return '{"Thought":"t","action":"Final_Answer","action_input":"done"}'

    runner = AgentRunner(model=MockModel(), Agent=AgentType.ReAct, tools=[tool])

    result = runner.Tool_Runner("add", json.dumps({"a": 3, "b": 4}))
    assert result == 7
    assert call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_tools.py -v
```

Expected: FAIL — call_count will be 2 or the function throws.

- [ ] **Step 3: Fix `Tool_Runner` for structured tools in `agentx_dev/Runner/AgentRun.py`**

Replace the structured tool execution block in `Tool_Runner` (the `if tool_name in self.args:` branch) with:

```python
if tool_name in self.args:
    schema = self.args[tool_name].get('args_schema')
    func = self.args[tool_name].get('func')
    if not schema or not func:
        logger.error(f"Misconfigured structured tool '{tool_name}'.")
        return f"Error: Misconfigured structured tool '{tool_name}'."
    try:
        # Normalize: if args is a JSON string, parse it first
        if isinstance(args_str, str):
            parsed_args = json.loads(args_str)
        else:
            parsed_args = args_str  # already a dict from the LLM

        # Validate with Pydantic, then call once
        validated_args = schema(**parsed_args)
        result = func(**validated_args.model_dump())
        logger.info(f"ACTION: {tool_name}, INPUT: {parsed_args}, RESULT: {result}")

        if self._cache:
            from agentx_dev.Cache import generate_cache_key
            cache_key = generate_cache_key(tool_name, str(args_str))
            self._cache.set(cache_key, result, ttl=config.cache_ttl)

        if tool_event:
            observability.end_event(tool_event, data={"result": str(result)[:100]})

        return result
    except Exception as e:
        logger.error(f"Error executing structured tool '{tool_name}': {e}", exc_info=True)
        return f"Error executing structured tool '{tool_name}': {e}"
```

- [ ] **Step 4: Apply the identical fix to `AsyncAgentRun.py`**

In `agentx_dev/Runner/AsyncAgentRun.py`, replace the `elif tool_name in self.args:` branch (sync structured tool path) with the same pattern:

```python
elif tool_name in self.args:
    schema = self.args[tool_name].get('args_schema')
    func = self.args[tool_name].get('func')
    if not schema or not func:
        logger.error(f"Misconfigured structured tool '{tool_name}'.")
        return f"Error: Misconfigured structured tool '{tool_name}'."
    try:
        if isinstance(args_str, str):
            parsed_args = json.loads(args_str)
        else:
            parsed_args = args_str

        validated_args = schema(**parsed_args)
        result = func(**validated_args.model_dump())
        logger.info(f"ACTION: {tool_name}, INPUT: {parsed_args}, RESULT: {result}")

        if self._cache:
            from agentx_dev.Cache import generate_cache_key
            cache_key = generate_cache_key(tool_name, str(args_str))
            self._cache.set(cache_key, result, ttl=config.cache_ttl)

        if tool_event:
            observability.end_event(tool_event, data={"result": str(result)[:100]})

        return result
    except Exception as e:
        logger.error(f"Error executing structured tool '{tool_name}': {e}", exc_info=True)
        return f"Error executing structured tool '{tool_name}': {e}"
```

Also apply the same normalize-then-validate pattern to the `if tool_name in self.async_args:` branch in `AsyncAgentRun.py` (lines 284-317), removing the special-case `batch_concurrent` hack and just letting the schema handle it cleanly.

- [ ] **Step 5: Run tests**

```
pytest tests/test_tools.py -v
```

Expected: PASS (2/2).

- [ ] **Step 6: Commit**

```
git add agentx_dev/Runner/AgentRun.py agentx_dev/Runner/AsyncAgentRun.py tests/test_tools.py
git commit -m "fix: structured tool args validated once before execution, not twice"
```

---

## Task 4: Fix Tool Description Mutation at Registration

**Files:**
- Modify: `agentx_dev/Runner/AgentRun.py`
- Modify: `agentx_dev/Runner/AsyncAgentRun.py`

The line `tool_instance.description = tool_instance.description + str(tool_instance.args_schema.__signature__.parameters)` mutates the shared tool object with ugly Python inspect output. Replace it with a local formatting function that generates a clean schema description for the prompt without touching the tool object.

- [ ] **Step 1: Add a helper function at the top of `AgentRun.py` (after imports)**

```python
def _format_tool_description(tool) -> str:
    """Return a prompt-ready description. For structured tools, appends param names."""
    from agentx_dev.Tools import StructuredTool
    if isinstance(tool, StructuredTool):
        params = list(tool.args_schema.model_fields.keys())
        return f"{tool.description} (params: {', '.join(params)})"
    return tool.description
```

- [ ] **Step 2: Replace the `tool_info` dict in `__init__` to use the helper**

In `AgentRunner.__init__`, find where `tool_info` is built:

```python
self.tool_info = {
    'tools': '\n'.join([f"- {t.name} : {t.description }" for t in self.tools]),
    'tool_names': ', '.join([t.name for t in self.tools]),
    'user_input' : self.holder
}
```

Replace with:

```python
self._tool_prompt_block = '\n'.join(
    [f"- {t.name} : {_format_tool_description(t)}" for t in self.tools]
)
self._tool_names_block = ', '.join([t.name for t in self.tools])
```

- [ ] **Step 3: Update `Initialize()` to use these blocks when building the system prompt**

In the refactored `Initialize()` from Task 2, replace the `tool_info` dict with:

```python
tool_info = {
    'tools': self._tool_prompt_block,
    'tool_names': self._tool_names_block,
    'user_input': user_input
}
```

- [ ] **Step 4: Remove the mutating lines from `__init__`**

Delete these lines from `AgentRunner.__init__` and `AsyncAgentRunner.__init__`:

```python
tool_instance.description = tool_instance.description + str(tool_instance.args_schema.__signature__.parameters)
```

Apply the same `_format_tool_description` helper approach in `AsyncAgentRun.py`.

- [ ] **Step 5: Run all tests**

```
pytest tests/ -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```
git add agentx_dev/Runner/AgentRun.py agentx_dev/Runner/AsyncAgentRun.py
git commit -m "fix: stop mutating tool description, use clean param formatting for prompt"
```

---

## Task 5: Fix ChainOfThought Prompt Template

**Files:**
- Modify: `promptTemplate.yaml`

The `Chain-of-Thought` template's example JSON uses unquoted keys and a `Action Input` key with a space, making it impossible to parse consistently.

- [ ] **Step 1: Replace the `Chain-of-Thought` section in `promptTemplate.yaml`**

Find and replace the `Chain-of-Thought` block with:

```yaml
Chain-of-Thought: |
  You are Nova, a smart assistant. Think through the problem step by step before choosing an action.

  You can use any of these tools:
  {tools}

  Tools names:
  {tool_names}

  Human: {user_input}

  IMPORTANT: Respond ONLY with valid JSON — no extra text, no code fences.

  {{
    "Thought": "<your step-by-step reasoning>",
    "Action": "<tool_name or Final_Answer>",
    "Action_Input": "<input value or final answer text>"
  }}

  Example:
  {{
    "Thought": "The user wants to know the weather in Barrie. I should use the weather tool.",
    "Action": "weather",
    "Action_Input": "Barrie"
  }}
```

- [ ] **Step 2: Update `ChainOfThought` Pydantic model in `Agent.py`** to match the new key `Action_Input` (no space):

Find in `agentx_dev/Agents/Agent.py`:

```python
Action_Input: str | Dict | List = Field("The input to the action", alias='Action Input')
```

Replace with:

```python
Action_Input: str | Dict | List = Field("The input to the action", alias='Action_Input')
```

- [ ] **Step 3: Verify import**

```
python -c "from agentx_dev.Agents.Agent import ChainOfThought; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```
git add promptTemplate.yaml agentx_dev/Agents/Agent.py
git commit -m "fix: ChainOfThought template now emits valid JSON with consistent key names"
```

---

## Task 6: True Async LLM Interface + Fix AsyncAgentRunner

The `AsyncAgentRunner` calls `self.model.Initialize()` synchronously, blocking the event loop.

**Files:**
- Modify: `agentx_dev/ChatModel.py`
- Modify: `agentx_dev/Runner/AsyncAgentRun.py`
- Modify: `tests/test_chatmodel.py`

- [ ] **Step 1: Add async test to `tests/test_chatmodel.py`**

Append to `tests/test_chatmodel.py`:

```python
import asyncio


def test_base_chat_model_has_async_initialize():
    """BaseChatModel subclasses must support async_initialize."""
    class MockModel(BaseChatModel):
        def Initialize(self, messages):
            return "sync result"

    m = MockModel()
    result = asyncio.run(m.async_initialize([]))
    assert result == "sync result"


def test_async_initialize_does_not_block_event_loop():
    """async_initialize must be awaitable (returns a coroutine)."""
    import inspect

    class MockModel(BaseChatModel):
        def Initialize(self, messages):
            return "response"

    m = MockModel()
    coro = m.async_initialize([])
    assert inspect.iscoroutine(coro)
    asyncio.run(coro)
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_chatmodel.py::test_base_chat_model_has_async_initialize -v
```

Expected: FAIL — `async_initialize` does not exist yet.

- [ ] **Step 3: Add `async_initialize` to `BaseChatModel` in `agentx_dev/ChatModel.py`**

Inside `BaseChatModel`, add a default async wrapper:

```python
class BaseChatModel(ABC):
    """Abstract base. All integrations must implement Initialize()."""

    @abstractmethod
    def Initialize(self, messages) -> str:
        """Synchronous LLM call. Returns the response string."""
        ...

    async def async_initialize(self, messages) -> str:
        """
        Async wrapper around Initialize(). Runs in a thread pool so it
        doesn't block the event loop. Override for a native async implementation.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.Initialize, messages)
```

Also add `import asyncio` at the top of `ChatModel.py`.

- [ ] **Step 4: Update `AsyncAgentRunner.Initialize()` to use `await self.model.async_initialize()`**

In `agentx_dev/Runner/AsyncAgentRun.py`, find both places where the model is called synchronously:

```python
response = self.model.Initialize(messages=self.history)
```

Replace with:

```python
response = await self.model.async_initialize(messages=working_history)
```

Also apply the same stateless-per-call refactor from Task 2 to `AsyncAgentRun.py`:
- Build `working_history` fresh each call
- Remove `self.history`, `self.holder`, `self.agent_scratchpad`, `self.Steps` from `__init__`
- Return `history=working_history` in `AgentCompletion`

- [ ] **Step 5: Run tests**

```
pytest tests/ -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```
git add agentx_dev/ChatModel.py agentx_dev/Runner/AsyncAgentRun.py tests/test_chatmodel.py
git commit -m "feat: BaseChatModel gets async_initialize, AsyncAgentRunner now truly async"
```

---

## Task 7: Add Anthropic / Claude Chat Model

**Files:**
- Modify: `agentx_dev/ChatModel.py`
- Modify: `agentx_dev/__init__.py`
- Modify: `agentx_dev/Runner/__init__.py`
- Modify: `tests/test_chatmodel.py`

- [ ] **Step 1: Install the Anthropic SDK**

```
pip install anthropic
```

Verify:

```
python -c "import anthropic; print(anthropic.__version__)"
```

Expected: a version string like `0.x.x`

- [ ] **Step 2: Add Claude test to `tests/test_chatmodel.py`**

Append to `tests/test_chatmodel.py`:

```python
def test_claude_is_a_valid_base_chat_model():
    """Claude must be an instance of BaseChatModel."""
    from agentx_dev.ChatModel import Claude
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    claude = Claude(model="claude-haiku-4-5-20251001")
    assert isinstance(claude, BaseChatModel)


def test_claude_initialize_raises_on_api_error(monkeypatch):
    """Claude.Initialize() must raise (not swallow) API errors."""
    from agentx_dev.ChatModel import Claude
    import os
    os.environ["ANTHROPIC_API_KEY"] = "invalid-key"

    claude = Claude(model="claude-haiku-4-5-20251001")

    # Should raise — not return empty string — on auth failure
    with pytest.raises(Exception):
        claude.Initialize([{"role": "user", "content": "hello"}])
```

- [ ] **Step 3: Implement the `Claude` class in `agentx_dev/ChatModel.py`**

After the `GPT` class, add:

```python
class Claude(BaseChatModel):
    """
    Chat model wrapper for Anthropic's Claude API.

    Usage:
        model = Claude(model="claude-sonnet-4-6")
        response = model.Initialize([{"role": "user", "content": "Hello"}])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        temperature: float = 1.0,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        import anthropic as _anthropic
        self._anthropic = _anthropic
        self.model_name = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = _anthropic.Anthropic(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY"),
            timeout=timeout,
            max_retries=max_retries,
        )

    def Initialize(self, messages) -> str:
        """
        Send messages to Claude and return the text response.

        Converts OpenAI-style message dicts to Anthropic format.
        System messages are extracted and passed as the `system` parameter.
        """
        try:
            # Separate system messages from the conversation
            system_parts = [m["content"] for m in messages if m.get("role") == "system"]
            conversation = [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]
            system_prompt = "\n\n".join(system_parts) if system_parts else self._anthropic.NOT_GIVEN

            response = self.client.messages.create(
                model=self.model_name,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt,
                messages=conversation,
            )
            return response.content[0].text if response.content else ""
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            raise

    async def async_initialize(self, messages) -> str:
        """Native async Claude call using the async Anthropic client."""
        try:
            import anthropic as _anthropic
            async_client = _anthropic.AsyncAnthropic(
                api_key=self.client.api_key,
                timeout=60.0,
            )
            system_parts = [m["content"] for m in messages if m.get("role") == "system"]
            conversation = [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]
            system_prompt = "\n\n".join(system_parts) if system_parts else _anthropic.NOT_GIVEN

            response = await async_client.messages.create(
                model=self.model_name,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt,
                messages=conversation,
            )
            return response.content[0].text if response.content else ""
        except Exception as e:
            logger.error(f"Claude async API error: {e}")
            raise
```

- [ ] **Step 4: Export `Claude` from `agentx_dev/__init__.py`**

Find the existing imports in `agentx_dev/__init__.py` and add:

```python
from agentx_dev.ChatModel import Claude
```

- [ ] **Step 5: Run tests**

```
pytest tests/test_chatmodel.py::test_claude_is_a_valid_base_chat_model -v
```

Expected: PASS (no real API call needed for this test).

- [ ] **Step 6: Commit**

```
git add agentx_dev/ChatModel.py agentx_dev/__init__.py tests/test_chatmodel.py
git commit -m "feat: add Claude (Anthropic) chat model with native async support"
```

---

## Task 8: Add Retry / Backoff to LLM Calls

**Files:**
- Modify: `agentx_dev/ChatModel.py`
- Modify: `tests/test_chatmodel.py`

- [ ] **Step 1: Add retry test**

Append to `tests/test_chatmodel.py`:

```python
def test_gpt_retries_on_transient_error(monkeypatch):
    """GPT.Initialize() must retry up to max_retries on transient errors."""
    from agentx_dev.ChatModel import GPT
    import os
    os.environ.setdefault("OPENAI_API_KEY", "test-key")

    call_count = 0

    class FakeCompletion:
        choices = [MagicMock(message=MagicMock(content="success"))]

    def fake_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient error")
        return FakeCompletion()

    from unittest.mock import patch, MagicMock
    gpt = GPT(model="gpt-4o", max_retries=3)
    monkeypatch.setattr(gpt.client.chat.completions, "create", fake_create)

    result = gpt.Initialize([{"role": "user", "content": "hi"}])
    assert result == "success"
    assert call_count == 3
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_chatmodel.py::test_gpt_retries_on_transient_error -v
```

Expected: FAIL — OpenAI SDK handles retries internally, but transient `ConnectionError` from our mock won't be caught.

- [ ] **Step 3: Add a `_with_retry` helper to `BaseChatModel`**

In `agentx_dev/ChatModel.py`, add inside `BaseChatModel`:

```python
def _with_retry(self, fn, max_retries: int = 3, base_delay: float = 1.0):
    """
    Call `fn()` with exponential backoff on transient errors.
    Raises on the final attempt.
    """
    import time
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Attempt {attempt + 1} failed ({e}), retrying in {delay:.1f}s...")
                time.sleep(delay)
    raise last_exc
```

- [ ] **Step 4: Wrap the OpenAI call in `GPT.Initialize()` with `_with_retry`**

In `GPT.Initialize()`, replace:

```python
return self.extract_content(self.client.chat.completions.create(
    messages=messages,
    **self.defaults,
    ...
))
```

With:

```python
return self._with_retry(
    lambda: self.extract_content(self.client.chat.completions.create(
        messages=messages,
        **self.defaults,
        extra_headers=extra_headers,
        extra_query=extra_query,
        extra_body=extra_body,
        timeout=timeout or self.timeout,
    )),
    max_retries=self.client.max_retries if hasattr(self.client, 'max_retries') else 3
)
```

- [ ] **Step 5: Run tests**

```
pytest tests/ -v
```

Expected: All pass.

- [ ] **Step 6: Commit**

```
git add agentx_dev/ChatModel.py tests/test_chatmodel.py
git commit -m "feat: add exponential backoff retry to BaseChatModel LLM calls"
```

---

## Task 9: Full Test Suite Run and Final Validation

- [ ] **Step 1: Run the complete test suite**

```
pytest tests/ -v --tb=short
```

Expected: All tests pass with no errors.

- [ ] **Step 2: Smoke test the sync runner end-to-end**

Create `tests/smoke_test.py`:

```python
"""Quick smoke test — no real API calls, uses a mock model."""
from unittest.mock import MagicMock
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Agents.Agent import AgentType
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Tools import StandardTool, StructuredTool
from pydantic import BaseModel
import json


class MockModel(BaseChatModel):
    def __init__(self, responses):
        self._responses = iter(responses)

    def Initialize(self, messages):
        return next(self._responses)


def test_tool_call_then_final_answer():
    def greet(name: str) -> str:
        """Greet someone."""
        return f"Hello, {name}!"

    tool = StandardTool(func=greet, name="greet", description="Greet a person by name.")
    responses = [
        json.dumps({"Thought": "I'll greet them.", "action": "greet", "action_input": "Alice"}),
        json.dumps({"Thought": "Done.", "action": "Final_Answer", "action_input": "Hello, Alice!"}),
    ]
    runner = AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])
    result = runner.Initialize("Say hi to Alice")
    assert result.content == "Hello, Alice!"
    assert result.history is not None
    assert any(m["content"] == "Hello, Alice!" for m in result.history)


def test_structured_tool_call():
    class MulArgs(BaseModel):
        a: int
        b: int

    def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    tool = StructuredTool(func=multiply, args_schema=MulArgs, name="multiply", description="Multiply two numbers.")
    responses = [
        json.dumps({"Thought": "multiply", "action": "multiply", "action_input": {"a": 6, "b": 7}}),
        json.dumps({"Thought": "done", "action": "Final_Answer", "action_input": "42"}),
    ]
    runner = AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])
    result = runner.Initialize("What is 6 times 7?")
    assert result.content == "42"
    assert result.history is not None


def test_runner_reusable_across_calls():
    """Same runner instance must work correctly on the second call."""
    responses = [
        json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "first"}),
        json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "second"}),
    ]

    def dummy(x: str) -> str:
        """dummy"""
        return x

    tool = StandardTool(func=dummy, name="dummy", description="dummy tool.")
    runner = AgentRunner(model=MockModel(responses), Agent=AgentType.ReAct, tools=[tool])

    r1 = runner.Initialize("call one")
    r2 = runner.Initialize("call two")

    assert r1.content == "first"
    assert r2.content == "second"

    # System prompt of second call must NOT contain "call one"
    sys_msg = next(m for m in r2.history if m["role"] == "system")
    assert "call one" not in sys_msg["content"]
    assert "call two" in sys_msg["content"]
```

Run it:

```
pytest tests/smoke_test.py -v
```

Expected: PASS (3/3).

- [ ] **Step 3: Commit smoke tests**

```
git add tests/smoke_test.py
git commit -m "test: add end-to-end smoke tests for AgentRunner"
```

- [ ] **Step 4: Final commit tagging the overhaul**

```
git tag v3.0.0-overhaul
```

---

## Self-Review

**Spec coverage check:**
- [x] Duplicate `ToolCall` — Task 1
- [x] `BaseChatModel` not abstract — Task 1
- [x] Stateful prompt corruption — Task 2
- [x] History duplication — Task 2
- [x] `AgentCompletion.history` always None — Task 2
- [x] Dead `cleaning()` method — Task 2 (removed during init cleanup)
- [x] Tool arg double-execution bug — Task 3 (sync + async)
- [x] Tool description mutation — Task 4
- [x] ChainOfThought invalid JSON — Task 5
- [x] AsyncAgentRunner sync LLM call — Task 6
- [x] Claude/Anthropic model — Task 7
- [x] Retry/backoff — Task 8
- [x] FewShot/ZeroShot identical models — noted; not changed (behavioral difference is in the prompt template, not the Pydantic model — acceptable)

**Placeholder scan:** None found. All steps include exact code.

**Type consistency:** `ToolCall`, `AgentCompletion`, `BaseChatModel`, `working_history` — consistent across all tasks.
