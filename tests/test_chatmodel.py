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
