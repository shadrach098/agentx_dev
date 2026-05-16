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
