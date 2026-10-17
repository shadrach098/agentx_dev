# API summary

Every symbol exported from `agentx_dev`, grouped by area. Import shape:

```python
from agentx_dev import X
```

## Chat models

| Symbol | Kind | Doc |
|---|---|---|
| `BaseChatModel` | ABC | [concepts/models](../concepts/models.md) (3.4.2: `invoke` / `ainvoke` take `media=[...]`) |
| `GPT` | class | OpenAI Chat Completions wrapper (3.4: `adapt_params` — adapts to per-model parameter support, e.g. `reasoning_effort` values; 3.4.1: `use_responses_api` — tool calls move to `/v1/responses` when a model requires it) |
| `Claude` | class | Anthropic Messages wrapper (3.1: `enable_prompt_cache`; 3.4: `top_p`, `top_k`, `thinking`, `stop_sequences`, `adapt_params`; `temperature` defaults to unset) |
| `Media` *(3.4)* | class | Image / PDF / audio input, rendered per provider. `Media.image()`, `.document()`, `.audio()`, `.from_path()`, `.from_url()`, `.from_bytes()`; 3.4.2: `.text()` and `.spreadsheet()` for CSV / text / Excel. See [Media reference](../media/reference.md) |
| `StructuredOutputRunnable` | class | Result of `.with_structured_output(schema)` (3.4.2: `invoke` / `ainvoke` take `media=[...]`) |
| `TokenBucket` | class | Rate limiter |
| `TokenUsage` | class | Per-model token counter (3.1: `cache_hit_ratio`) |
| `RetryBudgetExceeded` | exception | Retry cap exhausted |
| `CostBudgetExceeded` | exception | Spend crossed `budget_usd` |

## Agent runners

| Symbol | Kind | Doc |
|---|---|---|
| `AgentRunner` | class | Sync runner (3.1: `bind_tools_natively`, `parallel_tool_workers`; 3.2: constructor `output_schema=` — typed output coerced via native FC after the loop, on `completion.output`; 3.4: `invoke` / `stream` take `media=[...]`; new: constructor `skills=True` / `Skills(...)` — a `use_skill` tool and a skill index in the system prompt, see the [skills guide](../guides/skills.md)) |
| `AsyncAgentRunner` | class | Async runner with concurrent tool dispatch (3.2: constructor `output_schema=` too; 3.4: `ainvoke` / `astream` take `media=[...]`) |
| `Persistence` *(3.5)* | dataclass | Opt-in settings for runs that keep working through errors: `max_minutes`, `reflect_after`, `max_reflections`, `compact_at_tokens`, `keep_recent_turns`, `max_replans`, `max_turns`, `patient_retries`. Pass as `persistence=` to a runner or Supervisor |
| `AgentType` | enum | `ReAct` / `Chain_of_Thought` / `Zero_Shot` / `Few_Shot` / `Instruction_Tuned` |
| `AgentFormatter` | class | Custom prompt template + parser pair |
| `AgentFormattor` | class | Legacy alias (typo kept for BC) |
| `AgentCompletion` | class | Result of `runner.invoke` (3.5: `.outcome`, `.progress`) |
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
| `web_fetch_tool` | factory | GET a URL (optional disk cache; 3.6: `text_only=True` returns readable text instead of raw markup) |

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
| `vector_search_tool` | factory | RAG tool for the agent. *(3.2)* kwargs: `max_text_chars` (0 = full passages), `structured_output` (JSON `{id, text, vector_score, metadata}` array) |

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
| `Supervisor` | class | Sync decompose + dispatch + synthesize (3.3: `depends_on` DAG plans, `skip_when` conditional steps, `max_plan_retries`) (3.5: `persistence=` replans unfinished steps under one shared deadline) (3.6: `ask_user=True \| function`, `max_questions=3`, `ask_timeout=` let the planner and every agent ask the operator for missing facts) (3.6: `memory=<vector store>`, `memory_top_k=4`, `memory_min_score=0.2`, `memory_write=True` give the planner and every dispatched step relevant stored items, save operator answers and `done` results, and answer an exact repeat of an answered question from memory) (3.6.2: `ask_when_done=True` asks "What should I do next?" when a round's answer is ready, `follow_up=True` runs another round on each reply, `max_follow_ups=5` bounds it; both need `ask_user`) |
| `AsyncSupervisor` | class | Async concurrent dispatch (3.3: completion-driven DAG scheduler, `max_parallel=` cap) (3.6: same `ask_user=`, `max_questions=`, `ask_timeout=`; `ask_user` may be an `async` function; same `memory=`, `memory_top_k=`, `memory_min_score=`, `memory_write=`) (3.6.2: same `ask_when_done=`, `follow_up=`, `max_follow_ups=`) |
| `SupervisorResult` | dataclass | Plan + subtask results + final (3.5: `.outcome`) (3.6: `.asked`, one dict per question put to the operator: `source`, `question`, `answered`, `reason`, `deduped`; no answer text) (3.6: `.memory`, what the run saved to long-term memory: `kind`, `id`, `text` cut at 80 characters) (3.6.2: `.next_step`, the operator's reply to "What should I do next?" or `None`; `.rounds`, one dict per round: `round`, `query`, `content`, `outcome`, filled once a follow-up has run) |
| `SubtaskResult` | dataclass | One specialist's contribution. *(3.2)* `.output` carries the validated Pydantic instance when the runner declared an `output_schema` |
| `SpawnConfig` | dataclass | Sub-agent settings: `enabled`, `capabilities` / `tools` (the ceiling), `allowed_paths`, `max_spawns`, `max_depth`, `max_iterations` (step limit for Supervisor-created helpers), `approver` (3.6: ceiling mode; `AsyncSupervisor` and `AgentRunner(delegation=)` accept it) |
| `AgentSpec` *(3.6)* | dataclass | One sub-agent to build: `name`, `instructions`, `tools`, `origin` |
| `SpawnRequest` | dataclass | One planner-issued spawn ask |
| `Specialist` *(3.3)* | dataclass | Registry entry with planner metadata: `depends_on` hints, `output_schema` display, `when_to_use` |
| `ask_human_tool` *(3.6)* | function | `ask_human_tool(prompt_prefix="[agent]", ask_timeout=None)` returns a standalone `ask_human` tool (`question`, `context`) for any `AgentRunner`, built on the notebook-aware built-in asker |
| Supervisor stream events *(3.6)* | events | `{"type": "question", "source", "question", "context"}` and `{"type": "answer", "source", "answered", "reason"}` when `ask_user` is set; `{"type": "checkpoint", "round", "outcome"}` and `{"type": "round", "round", "query"}` with `ask_when_done` / `follow_up` (3.6.2). See [Sub-agents](../guides/sub-agents.md#asking-the-operator) and [Asking when the work is done](../guides/sub-agents.md#asking-when-the-work-is-done) |
| Supervisor `memory` event *(3.6)* | event | `{"type": "memory", "stage": "plan" or "step", "hits": int}` when items from long-term memory were added to a prompt; the `answer` event gains `"from_memory": True` when an answer came from memory. See [Long-term memory](../guides/sub-agents.md#long-term-memory) |

## Session persistence

| Symbol | Kind | Doc |
|---|---|---|
| `Session` | class | Serializable conversation state |
| `Session.stream(user_input, **kw)` *(3.7)* | method | Like `invoke`, but yields the runner's events (`thought`, `tool_call`, `tool_result`, `final`, `completion`) and absorbs the result on the `completion` event. A turn abandoned before then (Ctrl+C, closing the generator) is not recorded |

## Skills *(next release)*

Import from `agentx_dev.Skills`. See the [Skills guide](../guides/skills.md).

| Symbol | Kind | Doc |
|---|---|---|
| `Skills(dir=None, *, project_dirs=(), seed=True)` | class | Where an agent finds its skills. `dir` is the folder the built-in skills are created in (when `seed`) and links are installed into; `project_dirs` are read-only and untrusted. `Skills.default(home=None)` is `~/.agentx/skills`. `.store()` lists what is on disk; `.load(tools)` picks the skills that apply. Pass it as `skills=` to `AgentRunner` / `AsyncAgentRunner` (`skills=True` means `Skills.default()`). |
| `attach_skills(skills, tools, addendum)` | function | What the runners call: returns the tools plus `use_skill` and the addendum with the skill list at the end; unchanged when no skill applies, or when loading fails. |
| `make_use_skill_tool(skills)` | function | The read-only `use_skill(name, file="")` tool over a dict of skills. |
| `Skill`, `SkillStore` | classes | One skill (`name`, `description`, `body`, `tools`, `folder`, `source`); the skills found in some folders, with `.all()` and `.problems`. |
| `open_source(link)` | context manager | Resolve a `https://github.com/<owner>/<repo>[/tree/<ref>/<folder>]` link (to its exact commit, downloaded) or a folder to files on disk; yields `Fetched(root, label, commit, subpath)`. |
| `discover(root, subpath="")` | function | The installable skills under `root`, and one-line problems for those skipped. |
| `preview_text(fetched, found, problems=())` | function | What to show the user before installing: names, descriptions, file counts, commit, program files. |
| `install(found, target, fetched)` | function | Copy skills into `target` (refuses a taken name or a built-in's name; installs nothing then) and record the source and commit. |
| `remove_skill(name, target)` | function | Delete a skill folder from `target`. |
| `SkillInstallError` | exception | A problem to tell the user, in a sentence. |
| `AgentRunner(skills=)`, `AsyncAgentRunner(skills=)` | parameter | `True` or a `Skills`; default `None` changes nothing. |
| `/skills [install <link>\|remove <name>\|use <name> ...]`, `/<skill> [what you want]` | `agentx` commands | List, install, remove and use skills in the terminal app; each skill the conversation's agent has is also offered in the `/` pop-up. |

## Command line

| Symbol | Kind | Doc |
|---|---|---|
| `agentx` *(3.7)* | command | Terminal coding assistant: chat loop, file and shell tools behind approval prompts, `AGENTX.md` context, saved conversations. Also `python -m agentx_dev`. Needs `prompt_toolkit>=3.0` (a base dependency). See [the agentx command](../guides/cli.md) |

## Streaming

| Symbol | Kind | Doc |
|---|---|---|
| `StreamChunk` | class | Normalized chunk shape |
| `StreamBuffer` | class | Accumulate stream + get joined text |
| `StreamProcessor` | class | Per-chunk transform |
| `OpenAIStreamAdapter` | class | Normalize OpenAI SDK chunks |
| `simple_stream` | async func | `await simple_stream(async_iter_of_str, print_chunks=True)` → accumulated `str`. Takes a stream, NOT a model |

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
