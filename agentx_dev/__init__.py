from .Agents.Agent import (
    AgentType, AgentFormattor, AgentFormatter,
    StandardParser, React_, ChainOfThought, ZeroShot, FewShot, Instruction_Tuned_,
    ToolCall, ToolError, AgentCompletion,
    to_openai_tool, to_anthropic_tool, to_tool_spec,
)
from .Runner.AgentRun import AgentRunner, ToolRegistry, CircuitBreaker, CircuitBreakerConfig
from .Runner.AsyncAgentRun import AsyncAgentRunner
from .ChatModel import (
    BaseChatModel, GPT, Claude,
    StructuredOutputRunnable,
    TokenBucket, TokenUsage, RetryBudgetExceeded, CostBudgetExceeded,
)
from .Tools import StandardTool, StructuredTool
from .Config import config, get_config, set_config
from .AsyncTools import AsyncStandardTool, AsyncStructuredTool, execute_tools_concurrently
from .Streaming import (
    StreamChunk, StreamBuffer, StreamProcessor,
    OpenAIStreamAdapter, simple_stream
)
from .Observability import (
    observability, ObservabilityManager, EventType, Event,
    ConsoleHook, FileHook, MetricsHook, CallbackHook, OTelHook,
    track_tool_call, track_async_tool_call,
    redact_secrets, redact_dict,
)
from .Memory import (
    ConversationMemory, SlidingWindowMemory, TokenLimitedMemory,
    ImportanceBasedMemory, SummaryMemory,
    create_simple_memory, create_windowed_memory, create_token_limited_memory
)
from .Cache import (
    InMemoryCache, LRUCache, FileCache,
    cached_tool, generate_cache_key, get_global_cache, set_global_cache
)
from .Loader import (
    load_agent_from_yaml, load_async_agent_from_yaml,
    build_runner_from_config, AgentConfigError,
)
from .DefaultTools import DefaultTools, Permissions, DEFAULT_PERMISSIONS_PATH, mint_session_dir
from .Session import Session
from .WebTools import web_fetch_tool, web_search_tool

# MCP is an optional dependency — only re-export the names if the module imports cleanly.
try:
    from .MCP import (
        MCPClient,
        json_schema_to_pydantic,
        mcp_tool_to_async_structured_tool,
    )
    _MCP_AVAILABLE = True
except Exception:
    _MCP_AVAILABLE = False

__all__ = [
    # Core agent
    "AgentType",
    "AgentFormatter",
    "AgentFormattor",   # legacy typo'd name, kept for backward compat
    "AgentRunner",
    "AsyncAgentRunner",
    "AgentCompletion",
    "ToolCall",
    "ToolError",

    # Tools
    "StandardTool",
    "StructuredTool",
    "AsyncStandardTool",
    "AsyncStructuredTool",
    "ToolRegistry",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "execute_tools_concurrently",

    # Parser models + schema helpers
    "StandardParser", "React_", "ChainOfThought", "ZeroShot", "FewShot", "Instruction_Tuned_",
    "to_openai_tool", "to_anthropic_tool", "to_tool_spec",

    # Chat models
    "BaseChatModel",
    "GPT",
    "Claude",
    "StructuredOutputRunnable",
    "TokenBucket",
    "TokenUsage",
    "RetryBudgetExceeded",
    "CostBudgetExceeded",

    # Configuration
    "config",
    "get_config",
    "set_config",

    # Streaming
    "StreamChunk",
    "StreamBuffer",
    "StreamProcessor",
    "OpenAIStreamAdapter",
    "simple_stream",

    # Observability
    "observability",
    "ObservabilityManager",
    "EventType",
    "Event",
    "ConsoleHook",
    "FileHook",
    "MetricsHook",
    "CallbackHook",
    "OTelHook",
    "track_tool_call",
    "track_async_tool_call",

    # Memory
    "ConversationMemory",
    "SlidingWindowMemory",
    "TokenLimitedMemory",
    "ImportanceBasedMemory",
    "SummaryMemory",
    "create_simple_memory",
    "create_windowed_memory",
    "create_token_limited_memory",

    # Cache
    "InMemoryCache",
    "LRUCache",
    "FileCache",
    "cached_tool",
    "generate_cache_key",
    "get_global_cache",
    "set_global_cache",

    # YAML / dict-based config loading
    "load_agent_from_yaml",
    "load_async_agent_from_yaml",
    "build_runner_from_config",
    "AgentConfigError",

    # Default tools + capability-based permissions
    "DefaultTools",
    "Permissions",
    "DEFAULT_PERMISSIONS_PATH",
    "mint_session_dir",

    # Session persistence
    "Session",
]

if _MCP_AVAILABLE:
    __all__.extend([
        "MCPClient",
        "json_schema_to_pydantic",
        "mcp_tool_to_async_structured_tool",
    ])
