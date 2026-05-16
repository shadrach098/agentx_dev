"""
Observability and logging hooks for AgentX framework.

This module provides a comprehensive observability system with:
- Event-based hooks for monitoring agent execution
- Structured logging with context
- Performance metrics tracking
- Custom callback support
"""

from typing import Dict, Any, Callable, List, Optional, Protocol
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import json
import time
import uuid

logger = logging.getLogger(__name__)


class EventType(Enum):
    """Types of events that can be tracked in agent execution."""
    AGENT_START = "agent.start"
    AGENT_COMPLETE = "agent.complete"
    AGENT_ERROR = "agent.error"

    TOOL_CALL_START = "tool.call.start"
    TOOL_CALL_COMPLETE = "tool.call.complete"
    TOOL_CALL_ERROR = "tool.call.error"

    LLM_CALL_START = "llm.call.start"
    LLM_CALL_COMPLETE = "llm.call.complete"
    LLM_CALL_ERROR = "llm.call.error"

    ITERATION_START = "iteration.start"
    ITERATION_COMPLETE = "iteration.complete"

    CUSTOM = "custom"


@dataclass
class Event:
    """Represents a single event in agent execution."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    type: EventType = EventType.CUSTOM
    timestamp: datetime = field(default_factory=datetime.now)
    data: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    duration_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "id": self.id,
            "type": self.type.value,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms
        }

    def to_json(self) -> str:
        """Convert event to JSON string."""
        return json.dumps(self.to_dict(), indent=2)


class ObservabilityHook(Protocol):
    """Protocol defining the interface for observability hooks."""

    def on_event(self, event: Event) -> None:
        """Called when an event occurs."""
        ...


class ConsoleHook:
    """Prints events to console with formatting."""

    def __init__(self, verbose: bool = True, color: bool = True):
        self.verbose = verbose
        self.color = color

    def on_event(self, event: Event) -> None:
        """Print event to console."""
        if not self.verbose and event.type == EventType.CUSTOM:
            return

        color_codes = {
            EventType.AGENT_START: "\x1B[1;34m",      # Blue
            EventType.AGENT_COMPLETE: "\x1B[1;32m",   # Green
            EventType.AGENT_ERROR: "\x1B[1;31m",      # Red
            EventType.TOOL_CALL_START: "\x1B[1;33m",  # Yellow
            EventType.TOOL_CALL_COMPLETE: "\x1B[32m", # Green
            EventType.LLM_CALL_START: "\x1B[1;35m",   # Magenta
        }
        reset = "\x1B[0m"

        prefix = color_codes.get(event.type, "") if self.color else ""

        if event.duration_ms:
            print(f"{prefix}[{event.type.value}] ({event.duration_ms:.2f}ms){reset}")
        else:
            print(f"{prefix}[{event.type.value}]{reset}")

        if self.verbose and event.data:
            print(f"  Data: {json.dumps(event.data, indent=2)}")


class FileHook:
    """Writes events to a JSON lines file."""

    def __init__(self, filepath: str, append: bool = True):
        self.filepath = filepath
        self.mode = 'a' if append else 'w'

    def on_event(self, event: Event) -> None:
        """Write event to file."""
        try:
            with open(self.filepath, self.mode) as f:
                f.write(event.to_json() + '\n')
        except Exception as e:
            logger.error(f"Failed to write event to file: {e}")


class MetricsHook:
    """Collects performance metrics from events."""

    def __init__(self):
        self.metrics: Dict[str, List[float]] = {}
        self.event_counts: Dict[str, int] = {}

    def on_event(self, event: Event) -> None:
        """Collect metrics from event."""
        event_type = event.type.value

        # Count events
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1

        # Collect duration metrics
        if event.duration_ms is not None:
            if event_type not in self.metrics:
                self.metrics[event_type] = []
            self.metrics[event_type].append(event.duration_ms)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of collected metrics."""
        summary = {
            "event_counts": self.event_counts,
            "duration_stats": {}
        }

        for event_type, durations in self.metrics.items():
            if durations:
                summary["duration_stats"][event_type] = {
                    "count": len(durations),
                    "total_ms": sum(durations),
                    "avg_ms": sum(durations) / len(durations),
                    "min_ms": min(durations),
                    "max_ms": max(durations)
                }

        return summary

    def reset(self):
        """Reset all collected metrics."""
        self.metrics.clear()
        self.event_counts.clear()


class CallbackHook:
    """Executes custom callback functions on events."""

    def __init__(self, callback: Callable[[Event], None]):
        self.callback = callback

    def on_event(self, event: Event) -> None:
        """Execute callback with event."""
        try:
            self.callback(event)
        except Exception as e:
            logger.error(f"Error in callback hook: {e}", exc_info=True)


class ObservabilityManager:
    """
    Manages observability hooks and event emission.

    This is the central component for observability in AgentX.
    """

    def __init__(self):
        self.hooks: List[ObservabilityHook] = []
        self.enabled = True
        self._event_stack: List[Event] = []

    def add_hook(self, hook: ObservabilityHook):
        """Add an observability hook."""
        self.hooks.append(hook)

    def remove_hook(self, hook: ObservabilityHook):
        """Remove an observability hook."""
        if hook in self.hooks:
            self.hooks.remove(hook)

    def emit(self, event: Event):
        """Emit an event to all hooks."""
        if not self.enabled:
            return

        for hook in self.hooks:
            try:
                hook.on_event(event)
            except Exception as e:
                logger.error(f"Error in observability hook: {e}", exc_info=True)

    def start_event(self, event_type: EventType, data: Optional[Dict[str, Any]] = None) -> Event:
        """
        Start a new event (for tracking duration).

        Returns the event object which should be passed to end_event().
        """
        parent_id = self._event_stack[-1].id if self._event_stack else None
        event = Event(
            type=event_type,
            data=data or {},
            parent_id=parent_id
        )
        self._event_stack.append(event)
        self.emit(event)
        return event

    def end_event(self, event: Event, data: Optional[Dict[str, Any]] = None):
        """
        End an event and calculate its duration.

        Args:
            event: The event object returned from start_event()
            data: Additional data to merge into the event
        """
        if event in self._event_stack:
            self._event_stack.remove(event)

        duration = (datetime.now() - event.timestamp).total_seconds() * 1000
        event.duration_ms = duration

        if data:
            event.data.update(data)

        # Create completion event
        completion_type_map = {
            EventType.AGENT_START: EventType.AGENT_COMPLETE,
            EventType.TOOL_CALL_START: EventType.TOOL_CALL_COMPLETE,
            EventType.LLM_CALL_START: EventType.LLM_CALL_COMPLETE,
            EventType.ITERATION_START: EventType.ITERATION_COMPLETE,
        }

        completion_type = completion_type_map.get(event.type, event.type)
        completion_event = Event(
            type=completion_type,
            data=event.data,
            parent_id=event.parent_id,
            duration_ms=duration
        )

        self.emit(completion_event)

    def error_event(self, event_type: EventType, error: Exception, data: Optional[Dict[str, Any]] = None):
        """Emit an error event."""
        error_data = {
            "error": str(error),
            "error_type": type(error).__name__,
            **(data or {})
        }

        error_event = Event(
            type=event_type,
            data=error_data,
            parent_id=self._event_stack[-1].id if self._event_stack else None
        )

        self.emit(error_event)

    def disable(self):
        """Disable observability (stops emitting events)."""
        self.enabled = False

    def enable(self):
        """Enable observability."""
        self.enabled = True


# Global observability manager instance
observability = ObservabilityManager()


# Decorator for automatic tool call tracking
def track_tool_call(func: Callable) -> Callable:
    """
    Decorator to automatically track tool execution.

    Usage:
        @track_tool_call
        def my_tool(input: str) -> str:
            return process(input)
    """
    def wrapper(*args, **kwargs):
        event = observability.start_event(
            EventType.TOOL_CALL_START,
            {"tool_name": func.__name__, "args": str(args), "kwargs": str(kwargs)}
        )

        try:
            result = func(*args, **kwargs)
            observability.end_event(event, {"result": str(result)[:200]})  # Truncate long results
            return result
        except Exception as e:
            observability.error_event(EventType.TOOL_CALL_ERROR, e, {"tool_name": func.__name__})
            raise

    return wrapper


# Async version of the decorator
def track_async_tool_call(func: Callable) -> Callable:
    """
    Decorator to automatically track async tool execution.

    Usage:
        @track_async_tool_call
        async def my_async_tool(input: str) -> str:
            return await process(input)
    """
    async def wrapper(*args, **kwargs):
        event = observability.start_event(
            EventType.TOOL_CALL_START,
            {"tool_name": func.__name__, "args": str(args), "kwargs": str(kwargs)}
        )

        try:
            result = await func(*args, **kwargs)
            observability.end_event(event, {"result": str(result)[:200]})
            return result
        except Exception as e:
            observability.error_event(EventType.TOOL_CALL_ERROR, e, {"tool_name": func.__name__})
            raise

    return wrapper
