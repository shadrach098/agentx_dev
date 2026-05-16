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
        def Initialize(self, messages) -> str:
            return "mock response"

    m = MockModel()
    assert m.Initialize([]) == "mock response"


def test_claude_is_a_valid_base_chat_model():
    """Claude must be an instance of BaseChatModel."""
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    from agentx_dev.ChatModel import Claude
    claude = Claude(model="claude-haiku-4-5-20251001")
    assert isinstance(claude, BaseChatModel)


def test_claude_initialize_raises_on_api_error():
    """Claude.Initialize() must raise on API errors, not silently return empty."""
    import os
    os.environ["ANTHROPIC_API_KEY"] = "invalid-key-that-will-fail"
    from agentx_dev.ChatModel import Claude
    claude = Claude(model="claude-haiku-4-5-20251001", max_retries=0)
    with pytest.raises(Exception):
        claude.Initialize([{"role": "user", "content": "hello"}])


import asyncio as _asyncio


def test_base_chat_model_has_async_initialize():
    """BaseChatModel subclasses must support async_initialize via the default wrapper."""
    class MockModel(BaseChatModel):
        def Initialize(self, messages) -> str:
            return "sync result"

    m = MockModel()
    result = _asyncio.run(m.async_initialize([]))
    assert result == "sync result"


def test_async_initialize_is_awaitable():
    """async_initialize must return a coroutine."""
    import inspect

    class MockModel(BaseChatModel):
        def Initialize(self, messages) -> str:
            return "response"

    m = MockModel()
    coro = m.async_initialize([])
    assert inspect.iscoroutine(coro), "async_initialize must return a coroutine"
    _asyncio.run(coro)
