# Public API

What you can rely on from one release to the next, and what may change without notice. The test
`tests/test_public_api.py` reads the lists on this page and fails when the package and the page disagree, so a
change to the public surface is always deliberate.

## What "public" means

**Stable:** every name exported from the package (`from agentx_dev import X`, listed below), and the few import
paths below it. Their names, their documented arguments and what they return keep working across minor releases.

**Not public, may change in any release:**

- any name that starts with an underscore (`_plan_once`, `_run_subtask`, ...), in any module;
- the Python modules of the `agentx` command (`agentx_dev.cli.*`). The command itself is the interface: its
  options, slash commands, settings files and their keys are documented in the [terminal guide](../guides/cli.md)
  and change only with an entry in [Upgrading](../guides/upgrading.md);
- the text of prompts, error messages and log lines, and the exact wording of anything shown on the screen;
- internal modules not listed on this page (import their names from `agentx_dev` instead).

## How versions change

- **Patch** (3.7.1): fixes only. No name is added or removed.
- **Minor** (3.8.0): new names and new optional arguments. A change of behaviour is listed in
  [Upgrading](../guides/upgrading.md), with the old behaviour still reachable where that is reasonable.
- **Major** (4.0.0): the only place a public name is removed or renamed, or an argument removed. Before that, it
  warns (`DeprecationWarning`) for at least one minor release.
- **Optional extras** (`[anthropic]`, `[mcp]`, `[otel]`, `[rag]`, `[excel]`) may change which packages they pull in
  a minor release; the names they enable stay.

## The names exported from `agentx_dev`

<!-- public-names: the test reads this block -->
```text
AgentCompletion AgentConfigError AgentFormatter AgentFormattor AgentRunner AgentSpec AgentType
AsyncAgentRunner AsyncStandardTool AsyncStructuredTool AsyncSupervisor BaseChatModel CallbackHook
ChainOfThought CircuitBreaker CircuitBreakerConfig Claude CompileResult Compiled ConsoleHook
ConversationMemory CostBudgetExceeded DEFAULT_PERMISSIONS_PATH DefaultTools Document Embeddings
EvalCase EvalReport EvalResult EvalRunner Event EventType FewShot FileCache FileHook GPT
HandoffCoordinator HandoffRequest HandoffResult HashEmbeddings ImportanceBasedMemory InMemoryCache
Instruction_Tuned_ LRUCache MCPClient Media MetricsHook OTelHook ObservabilityManager
OpenAIEmbeddings OpenAIStreamAdapter Permissions Persistence React_ RetryBudgetExceeded
SemanticMemory Session SlidingWindowMemory SpawnConfig SpawnRequest Specialist StandardParser
StandardTool StreamBuffer StreamChunk StreamProcessor StructuredOutputRunnable StructuredTool
SubtaskResult SummaryMemory Supervisor SupervisorResult TextSplitter TokenBucket TokenLimitedMemory
TokenUsage ToolCall ToolError ToolRegistry VectorHit VectorStore ZeroShot ask_human_tool
build_runner_from_config cached_tool called_tool config contains create_semantic_memory
create_simple_memory create_token_limited_memory create_windowed_memory execute_tools_concurrently
generate_cache_key get_config get_global_cache handoff_tool json_schema_to_pydantic llm_judge
load_agent_from_yaml load_async_agent_from_yaml load_case_from_dict load_cases_from_dir
matches_regex max_iterations mcp_tool_to_async_structured_tool mint_session_dir not_contains
observability set_config set_global_cache simple_stream to_anthropic_tool to_openai_tool
to_tool_spec tool_count track_async_tool_call track_tool_call vector_search_tool web_fetch_tool
web_search_tool
```

`MCPClient`, `json_schema_to_pydantic` and `mcp_tool_to_async_structured_tool` are exported only when the `mcp`
package is installed (`pip install "agentx-dev[mcp]"`).

`AgentFormattor` is the old misspelling of `AgentFormatter`, kept so older code runs; use `AgentFormatter`.

What each name does: the [API summary](api-summary.md).

## Stable import paths below the package

These are documented in the guides and stay where they are:

<!-- public-paths: the test reads this block -->
```text
agentx_dev.Skills: Skills discover install open_source preview_text remove_skill
agentx_dev.Runner.Trim: TrimPolicy
agentx_dev.VectorStores: ChromaVectorStore PgVectorStore QdrantVectorStore
agentx_dev.SubAgents: AgentSpec SpawnConfig
agentx_dev.Supervisor: AsyncSupervisor SpawnConfig Supervisor
```
