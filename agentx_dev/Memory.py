"""
Memory and context management for long conversations in AgentX framework.

This module provides:
- Conversation history management with token limits
- Summary-based compression for long contexts
- Sliding window memory
- Semantic memory with importance scoring
"""

from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from abc import ABC, abstractmethod
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """Represents a single message in conversation history."""
    role: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    importance: float = 1.0  # 0.0 to 1.0, higher = more important

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format for LLM APIs."""
        return {
            "role": self.role,
            "content": self.content
        }

    def token_estimate(self) -> int:
        """Rough estimate of token count (4 chars ≈ 1 token)."""
        return len(self.content) // 4 + len(self.role) // 4


class BaseMemory(ABC):
    """Abstract base class for memory implementations."""

    @abstractmethod
    def add_message(self, role: str, content: str, **kwargs):
        """Add a message to memory."""
        pass

    @abstractmethod
    def get_messages(self) -> List[Dict[str, str]]:
        """Get messages in LLM API format."""
        pass

    @abstractmethod
    def clear(self):
        """Clear all messages."""
        pass


class ConversationMemory(BaseMemory):
    """
    Simple conversation memory that stores all messages.

    Best for short conversations where token limits aren't a concern.
    """

    def __init__(self):
        self.messages: List[Message] = []

    def add_message(self, role: str, content: str, **kwargs):
        """Add a message to the conversation history."""
        message = Message(
            role=role,
            content=content,
            metadata=kwargs.get('metadata', {}),
            importance=kwargs.get('importance', 1.0)
        )
        self.messages.append(message)

    def get_messages(self) -> List[Dict[str, str]]:
        """Get all messages in LLM format."""
        return [msg.to_dict() for msg in self.messages]

    def get_total_tokens(self) -> int:
        """Estimate total tokens in conversation."""
        return sum(msg.token_estimate() for msg in self.messages)

    def clear(self):
        """Clear all messages."""
        self.messages.clear()


class SlidingWindowMemory(BaseMemory):
    """
    Maintains a sliding window of recent messages.

    Automatically removes oldest messages when window size is exceeded.
    Always preserves the system message if present.
    """

    def __init__(self, max_messages: int = 10, preserve_system: bool = True):
        """
        Initialize sliding window memory.

        Args:
            max_messages: Maximum number of messages to retain.
            preserve_system: If True, always keep the system message.
        """
        self.max_messages = max_messages
        self.preserve_system = preserve_system
        self.messages: List[Message] = []
        self.system_message: Optional[Message] = None

    def add_message(self, role: str, content: str, **kwargs):
        """Add a message and maintain window size."""
        message = Message(
            role=role,
            content=content,
            metadata=kwargs.get('metadata', {}),
            importance=kwargs.get('importance', 1.0)
        )

        if role == "system" and self.preserve_system:
            self.system_message = message
        else:
            self.messages.append(message)

            # Remove oldest non-system messages if exceeding limit
            while len(self.messages) > self.max_messages:
                self.messages.pop(0)

    def get_messages(self) -> List[Dict[str, str]]:
        """Get messages with system message first if preserved."""
        result = []
        if self.system_message and self.preserve_system:
            result.append(self.system_message.to_dict())
        result.extend([msg.to_dict() for msg in self.messages])
        return result

    def clear(self):
        """Clear all messages including system message."""
        self.messages.clear()
        self.system_message = None


class TokenLimitedMemory(BaseMemory):
    """
    Memory that enforces a token limit by removing oldest messages.

    Automatically manages conversation history to stay within token budget.
    """

    def __init__(self, max_tokens: int = 4000, preserve_system: bool = True):
        """
        Initialize token-limited memory.

        Args:
            max_tokens: Maximum total tokens to maintain.
            preserve_system: If True, always keep the system message.
        """
        self.max_tokens = max_tokens
        self.preserve_system = preserve_system
        self.messages: List[Message] = []
        self.system_message: Optional[Message] = None

    def add_message(self, role: str, content: str, **kwargs):
        """Add a message and maintain token limit."""
        message = Message(
            role=role,
            content=content,
            metadata=kwargs.get('metadata', {}),
            importance=kwargs.get('importance', 1.0)
        )

        if role == "system" and self.preserve_system:
            self.system_message = message
        else:
            self.messages.append(message)
            self._trim_to_limit()

    def _trim_to_limit(self):
        """Remove oldest messages until under token limit."""
        system_tokens = self.system_message.token_estimate() if self.system_message else 0

        while self.messages:
            current_tokens = sum(msg.token_estimate() for msg in self.messages) + system_tokens

            if current_tokens <= self.max_tokens:
                break

            # Remove oldest message
            removed = self.messages.pop(0)
            logger.debug(f"Removed message to maintain token limit: {removed.role} ({removed.token_estimate()} tokens)")

    def get_messages(self) -> List[Dict[str, str]]:
        """Get messages with system message first if preserved."""
        result = []
        if self.system_message and self.preserve_system:
            result.append(self.system_message.to_dict())
        result.extend([msg.to_dict() for msg in self.messages])
        return result

    def get_total_tokens(self) -> int:
        """Get current total token count."""
        system_tokens = self.system_message.token_estimate() if self.system_message else 0
        return sum(msg.token_estimate() for msg in self.messages) + system_tokens

    def clear(self):
        """Clear all messages."""
        self.messages.clear()
        self.system_message = None


class ImportanceBasedMemory(BaseMemory):
    """
    Memory that retains most important messages based on importance scores.

    Messages can be scored for importance, and low-importance messages
    are removed first when memory limits are reached.
    """

    def __init__(
        self,
        max_messages: int = 20,
        importance_threshold: float = 0.3,
        preserve_system: bool = True,
        preserve_head: int = 2,
        preserve_tail: int = 4,
    ):
        """
        Initialize importance-based memory.

        Args:
            max_messages: Maximum number of messages to retain.
            importance_threshold: Minimum importance score to keep (0.0 to 1.0).
            preserve_system: If True, always keep the system message.
            preserve_head: Always keep the first N non-system messages (the
                task framing). Default 2.
            preserve_tail: Always keep the last M non-system messages (recent
                context the model needs to continue). Default 4.
        """
        self.max_messages = max_messages
        self.importance_threshold = importance_threshold
        self.preserve_system = preserve_system
        self.preserve_head = max(0, preserve_head)
        self.preserve_tail = max(0, preserve_tail)
        self.messages: List[Message] = []
        self.system_message: Optional[Message] = None

    def add_message(self, role: str, content: str, importance: float = 1.0, **kwargs):
        """
        Add a message with importance score.

        Args:
            role: Message role (system, user, assistant, function).
            content: Message content.
            importance: Importance score from 0.0 to 1.0 (default 1.0).
        """
        message = Message(
            role=role,
            content=content,
            metadata=kwargs.get('metadata', {}),
            importance=importance
        )

        if role == "system" and self.preserve_system:
            self.system_message = message
        else:
            self.messages.append(message)
            self._trim_by_importance()

    def _trim_by_importance(self):
        """Trim while preserving conversation continuity.

        Policy (in order):
          1. Drop anything below ``importance_threshold`` (preserves order).
          2. If still over ``max_messages``: keep the first ``preserve_head``
             messages (task framing), the last ``preserve_tail`` messages
             (recent context), plus top-K-by-importance from the middle.
             K is whatever budget remains. Result stays in chronological
             order — the LLM never sees a discontinuity.

        Previous implementation sorted the whole list by importance and
        truncated, which destroyed chronological order and broke
        conversation continuity (a later ``sort by timestamp`` in
        ``get_messages`` couldn't recover messages that had been dropped).
        """
        # Step 1: hard threshold prune.
        self.messages = [m for m in self.messages if m.importance >= self.importance_threshold]

        n = len(self.messages)
        if n <= self.max_messages:
            return

        head_n = min(self.preserve_head, n)
        tail_n = min(self.preserve_tail, n - head_n)
        middle = self.messages[head_n: n - tail_n] if tail_n else self.messages[head_n:]

        middle_budget = self.max_messages - head_n - tail_n
        if middle_budget < 0:
            middle_budget = 0

        if len(middle) > middle_budget:
            # Pick top-K by importance, but reassemble in original chronological order.
            ranked = sorted(enumerate(middle), key=lambda kv: kv[1].importance, reverse=True)
            kept_indices = sorted(idx for idx, _ in ranked[:middle_budget])
            middle = [middle[i] for i in kept_indices]

        head = self.messages[:head_n]
        tail = self.messages[n - tail_n:] if tail_n else []
        removed = n - (len(head) + len(middle) + len(tail))
        self.messages = head + middle + tail

        if removed:
            logger.debug(
                f"Trimmed {removed} middle messages (kept head={len(head)}, "
                f"middle={len(middle)}, tail={len(tail)})"
            )

    def get_messages(self) -> List[Dict[str, str]]:
        """Get messages in chronological order. Trim is order-preserving so no
        re-sort is necessary; the explicit sort stays as a defensive guarantee."""
        result = []
        if self.system_message and self.preserve_system:
            result.append(self.system_message.to_dict())
        sorted_messages = sorted(self.messages, key=lambda msg: msg.timestamp)
        result.extend([msg.to_dict() for msg in sorted_messages])
        return result

    def clear(self):
        """Clear all messages."""
        self.messages.clear()
        self.system_message = None


class SummaryMemory(BaseMemory):
    """
    Memory that compresses old messages into summaries.

    When conversation gets long, older messages are summarized using
    a provided summarization function (typically an LLM call).
    """

    def __init__(
        self,
        summarizer: Callable[[List[Message]], str],
        max_messages_before_summary: int = 10,
        keep_recent: int = 5
    ):
        """
        Initialize summary-based memory.

        Args:
            summarizer: Function that takes a list of Messages and returns a summary string.
            max_messages_before_summary: Trigger summarization after this many messages.
            keep_recent: Number of recent messages to keep unsummarized.
        """
        self.summarizer = summarizer
        self.max_messages_before_summary = max_messages_before_summary
        self.keep_recent = keep_recent
        self.messages: List[Message] = []
        self.summary: Optional[str] = None
        self.system_message: Optional[Message] = None

    def add_message(self, role: str, content: str, **kwargs):
        """Add a message and trigger summarization if needed."""
        message = Message(
            role=role,
            content=content,
            metadata=kwargs.get('metadata', {}),
            importance=kwargs.get('importance', 1.0)
        )

        if role == "system":
            self.system_message = message
        else:
            self.messages.append(message)

            # Trigger summarization if threshold exceeded
            if len(self.messages) > self.max_messages_before_summary:
                self._summarize_old_messages()

    def _summarize_old_messages(self):
        """Summarize old messages and keep only recent ones."""
        # Split messages into old and recent
        messages_to_summarize = self.messages[:-self.keep_recent]
        recent_messages = self.messages[-self.keep_recent:]

        if messages_to_summarize:
            try:
                # Generate summary
                new_summary = self.summarizer(messages_to_summarize)

                # Combine with existing summary if present
                if self.summary:
                    self.summary = f"{self.summary}\n\n{new_summary}"
                else:
                    self.summary = new_summary

                # Keep only recent messages
                self.messages = recent_messages

                logger.info(f"Summarized {len(messages_to_summarize)} messages")
            except Exception as e:
                logger.error(f"Failed to summarize messages: {e}", exc_info=True)

    def get_messages(self) -> List[Dict[str, str]]:
        """Get messages with summary prepended."""
        result = []

        # Add system message
        if self.system_message:
            result.append(self.system_message.to_dict())

        # Add summary as system message if available
        if self.summary:
            result.append({
                "role": "system",
                "content": f"Previous conversation summary:\n{self.summary}"
            })

        # Add recent messages
        result.extend([msg.to_dict() for msg in self.messages])
        return result

    def clear(self):
        """Clear all messages and summary."""
        self.messages.clear()
        self.summary = None
        self.system_message = None


# Utility functions
def create_simple_memory() -> ConversationMemory:
    """Create a simple conversation memory."""
    return ConversationMemory()


def create_windowed_memory(window_size: int = 10) -> SlidingWindowMemory:
    """Create a sliding window memory."""
    return SlidingWindowMemory(max_messages=window_size)


def create_token_limited_memory(max_tokens: int = 4000) -> TokenLimitedMemory:
    """Create a token-limited memory."""
    return TokenLimitedMemory(max_tokens=max_tokens)


def create_semantic_memory(
    embeddings: Any = None,
    *,
    recent_tail: int = 6,
    top_k: int = 4,
):
    """Convenience factory for a SemanticMemory backed by embeddings.

    When ``embeddings`` is None, falls back to ``HashEmbeddings(256)`` so
    the call succeeds without any API keys — useful for tests and demos.
    Pass ``OpenAIEmbeddings()`` for a real semantic backend.

    Lives here (not in Embeddings.py) so the ``create_*_memory`` factory
    naming is consistent — callers importing from ``agentx_dev`` get
    all memory constructors from the same surface.
    """
    from agentx_dev.Embeddings import (
        SemanticMemory as _SemanticMemory,
        HashEmbeddings as _HashEmbeddings,
    )
    if embeddings is None:
        embeddings = _HashEmbeddings(dim=256)
    return _SemanticMemory(
        embeddings=embeddings, recent_tail=recent_tail, top_k=top_k,
    )
