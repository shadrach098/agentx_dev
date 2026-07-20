# API summary

Every symbol exported from `agentx_dev`, grouped by area. Import shape:

```python
from agentx_dev import X
```

## Chat models

| Symbol | Kind | Doc |
|---|---|---|
| `BaseChatModel` | ABC | [concepts/models](../concepts/models.md) |
| `GPT` | class | OpenAI Chat Completions wrapper |
| `Claude` | class | Anthropic Messages wrapper (3.1: `enable_prompt_cache`) |
| `StructuredOutputRunnable` | class | Result of `.with_structured_output(schema)` |
| `TokenBucket` | class | Rate limiter |
| `TokenUsage` | class | Per-model token counter (3.1: `cache_hit_ratio`) |
| `RetryBudgetExceeded` | exception | Retry cap exhausted |
| `CostBudgetExceeded` | exception | Spend crossed `budget_usd` |

## Agent runners

| Symbol | Kind | Doc |
|---|---|---|
| `AgentRunner` | class | Sync runner (3.1: `bind_tools_natively`, `parallel_tool_workers`) |
| `AsyncAgentRunner` | class | Async runner with concurrent tool dispatch |
| `AgentType` | enum | `ReAct` / `Chain_of_Thought` / `Zero_Shot` / `Few_Shot` / `Instruction_Tuned` |
| `AgentFormatter` | class | Custom prompt template + parser pair |
| `AgentFormattor` | class | Legacy alias (typo kept for BC) |
| `AgentCompletion` | class | Result of `runner.invoke` |
| `ToolCall` | class | One dispatched call inside a completion |
| `ToolError` | class | Tool dispatch failure |
| `StandardParser` | class | Default parser |
| `React_`, `ChainOfThought`, `ZeroShot`, `FewShot`, `Instruction_Tuned_` | classes | Per-AgentType parsers |
| `to_openai_tool`, `to_anthropic_tool`, `to_tool_spec` | funcs | Cross-provider schema helpers |

## Tool infrastructure

| Symbol | Kind | Doc |
|---|---|---|
| `StandardTool` | class | Single-string-arg tool |
| `StructuredTool` | class | Multi-arg Pydantic tool |
| `AsyncStandardTool` | class | async variant |
| `AsyncStructuredTool` | class | async variant |
| `ToolRegistry` | class | Tool storage + dispatch |
| `CircuitBreaker` | class | Per-tool breaker |
| `CircuitBreakerConfig` | dataclass | Breaker settings |
| `execute_tools_concurrently` | func | Batch async tool dispatch |

## Default tools & permissions

| Symbol | Kind | Doc |
|---|---|---|
| `DefaultTools` | class | Builder for permission-gated tools |
| `Permissions` | class | Capability + sandbox config |
| `DEFAULT_PERMISSIONS_PATH` | str | Where `Permissions.from_file()` looks |
| `mint_session_dir` | func | Per-run subdirectory |

Tools registered by `DefaultTools`: `read_path`, `list_directory`,
`find_files`, `grep`, `write_file`, `edit_file`, `delete_path`,
`move_file`, `run_python`, `run_shell`.

## Web tools

| Symbol | Kind | Doc |
|---|---|---|
| `web_search_tool` | factory | DuckDuckGo + Wikipedia fallback |
| `web_fetch_tool` | factory | GET a URL (optional disk cache) |

## Memory

| Symbol | Kind | Doc |
|---|---|---|
| `ConversationMemory` | class | Keep everything |
| `SlidingWindowMemory` | class | Last N messages |
| `TokenLimitedMemory` | class | Drop until under token cap |
| `ImportanceBasedMemory` | class | Head + tail + top-K middle |
| `SummaryMemory` | class | LLM-compress old turns |
| `SemanticMemory` *(3.1)* | class | Embed + retrieve by similarity |
| `create_simple_memory`, `create_windowed_memory`, `create_token_limited_memory` | factories | Convenience constructors |
| `create_semantic_memory` *(3.1)* | factory | Convenience for SemanticMemory |

## Embeddings + RAG *(3.1)*

| Symbol | Kind | Doc |
|---|---|---|
| `Embeddings` | ABC | Backend base |
| `OpenAIEmbeddings` | class | text-embedding-3-{small,large} |
| `HashEmbeddings` | class | Zero-dep fallback |
| `VectorStore` | class | In-memory cosine store |
| `VectorHit` | dataclass | One search result |
| `vector_search_tool` | factory | RAG tool for the agent |

## Handoffs *(3.1)*

| Symbol | Kind | Doc |
|---|---|---|
| `HandoffRequest` | dataclass | Sentinel returned by handoff tool |
| `HandoffResult` | dataclass | Coordinator's return value |
| `HandoffCoordinator` | class | Routes between agents |
| `handoff_tool` | factory | Builds `handoff_to_<target>` |

## Evals *(3.1)*

| Symbol | Kind | Doc |
|---|---|---|
| `EvalCase` | dataclass | One test case |
| `EvalResult` | dataclass | Outcome of one case |
| `EvalReport` | dataclass | Aggregate + `.summary()` |
| `EvalRunner` | class | Executes cases through a factory |
| `contains`, `not_contains`, `matches_regex` | assertions | Text checks |
| `called_tool`, `tool_count` | assertions | Tool-call checks |
| `max_iterations` | assertion | Step count check |
| `llm_judge` | assertion | Model grades yes/no |
| `load_case_from_dict`, `load_cases_from_dir` | funcs | JSON case loaders |

CLI: `python -m agentx_dev.Evals run <dir> --config <yaml>`

## Multi-agent orchestration

| Symbol | Kind | Doc |
|---|---|---|
| `Supervisor` | class | Sync decompose + dispatch + synthesize |
| `AsyncSupervisor` | class | Async concurrent dispatch |
| `SupervisorResult` | dataclass | Plan + subtask results + final |
| `SubtaskResult` | dataclass | One specialist's contribution |
| `SpawnConfig` | dataclass | Dynamic specialist spawning settings |
| `SpawnRequest` | dataclass | One planner-issued spawn ask |

## Session persistence

| Symbol | Kind | Doc |
|---|---|---|
| `Session` | class | Serializable conversation state |

## Streaming

| Symbol | Kind | Doc |
|---|---|---|
| `StreamChunk` | class | Normalized chunk shape |
| `StreamBuffer` | class | Accumulate stream + get joined text |
| `StreamProcessor` | class | Per-chunk transform |
| `OpenAIStreamAdapter` | class | Normalize OpenAI SDK chunks |
| `simple_stream` | func | Yield strings from a model |

## Cache

| Symbol | Kind | Doc |
|---|---|---|
| `InMemoryCache` | class | dict-backed |
| `LRUCache` | class | max_size + TTL |
| `FileCache` | class | disk-backed |
| `cached_tool` | func | Wrap a tool with caching |
| `generate_cache_key` | func | Deterministic tool-call key |
| `get_global_cache`, `set_global_cache` | funcs | Process-wide singleton |

## Observability

| Symbol | Kind | Doc |
|---|---|---|
| `observability` | singleton | Global event bus |
| `ObservabilityManager` | class | Bus type |
| `EventType` | enum | AGENT_START, TOOL_CALL_END, ... |
| `Event` | dataclass | One emission |
| `ConsoleHook`, `FileHook`, `MetricsHook`, `CallbackHook`, `OTelHook` | classes | Ship-in hooks |
| `track_tool_call`, `track_async_tool_call` | decorators | Instrument custom funcs |
| `redact_secrets`, `redact_dict` | funcs | Scrub sensitive strings/dicts |

## Configuration

| Symbol | Kind | Doc |
|---|---|---|
| `config` | object | Global settings singleton |
| `get_config`, `set_config` | funcs | Programmatic access |

## YAML / dict loaders

| Symbol | Kind | Doc |
|---|---|---|
| `load_agent_from_yaml` | func | Sync runner from YAML |
| `load_async_agent_from_yaml` | func | Async runner from YAML |
| `build_runner_from_config` | func | Same, from a Python dict |
| `AgentConfigError` | exception | YAML validation failure |

## MCP (optional; requires `agentx-dev[mcp]`)

| Symbol | Kind | Doc |
|---|---|---|
| `MCPClient` | class | stdio / sse / http transports |
| `json_schema_to_pydantic` | func | Adapter |
| `mcp_tool_to_async_structured_tool` | func | Wrap MCP tool as AgentX tool |
