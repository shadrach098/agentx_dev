# Supervisor Sub-Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a Supervisor plan and create its own sub-agents (inline `new_agent` plan steps) and let specialists hand work to fresh sub-agents mid-run (`delegate` tool), all bounded by one developer-set ceiling, sync and async, persistent-mode aware.

**Architecture:** A new module `agentx_dev/SubAgents.py` owns `AgentSpec`, `SpawnConfig` (moved from `Supervisor.py`, extended), a per-run `SpawnRun`, the single checkpoint `SpawnPolicy` (the only code that turns a spec into a runner), and the `delegate` tool factories. The Supervisors gain a per-run agent registry (swapped in and restored, so nothing leaks), `new_agent` handling in planning, sanitization and dispatch, and `spawn` events. Runners gain `add_tool` / `remove_tool`, a `system_addendum` (async only; sync has it), and an optional `delegation=` config.

**Tech Stack:** Python 3.12, pydantic v2, pytest, the existing `AgentRunner` / `AsyncAgentRunner` / `Supervisor` / `Persistence` modules. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-supervisor-subagents-design.md` (the binding authority). Task 1 amends it where reading the code showed a conflict; those amendments are listed there.

## Global Constraints

- Version target is **3.6.0** (a new feature release). Releasing is a separate step the user must approve (Task 8 is a checklist only).
- With no spawning configured, behavior is unchanged. A `Supervisor`/`AsyncSupervisor` with `persistence=` set and no `spawn_config` gets `SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)`; without persistence the default stays `SpawnConfig(enabled=False)`. An explicit `spawn_config=` always wins, including `SpawnConfig(enabled=False)`.
- `SpawnConfig` keeps every existing field and meaning (`enabled`, `auto_spawn`, `approver`, `allowed_paths`, `max_spawns`, `auto_spawn_allowed_caps`) and adds `tools: Optional[List[Any]] = None`, `capabilities: Optional[Set[str]] = None`, `max_depth: int = 1`. **Ceiling mode** = `tools` or `capabilities` is not `None` (no per-spawn approval; an `approver`, if set, is still called). **Legacy mode** = both `None` (the 3.5.0 approval flow).
- `max_spawns` default: 3 in legacy mode, 6 in ceiling mode and in the persistent default. It counts planner-time spawns and `delegate` calls together, per run.
- Preset capability words: `web`, `files`, `code`, `delete`, plus new read-only `files_read` (`read_path`, `list_directory`). Tool-name to preset map: `web_search`/`web_fetch` -> `web`; `read_path`/`list_directory` -> `files_read` then `files`; `write_file`/`edit_file` -> `files`; `run_python` -> `code`; `delete_path` -> `delete`.
- Clip, never refuse the whole spawn: unknown or over-ceiling tool names are dropped and reported in `dropped`. If tools were requested and none survive: refuse with `no usable tools`. Instructions never change tools, permissions, or the sandbox; the data-reply addendum (`_SPAWNED_SPECIALIST_ADDENDUM`) is always present.
- `delegate(task: str, instructions: str = "", tools: list[str] = [])` returns the sub-agent's final answer capped at 4000 characters plus a `[truncated]` marker. Refusals are plain results (`delegation refused: <reason>; do this yourself`). A sub-agent that ends with an outcome other than `done`, or raises, makes the tool raise `DelegationFailed` whose message starts `[delegate failed: <outcome>]` (the registry turns that into a tool error that the caller's stuck tracker sees).
- Depth: a specialist is depth 0; a sub-agent built from it is depth 1. An agent at depth `d` gets the `delegate` tool only if `d < max_depth` (default 1).
- Events: `{"type": "spawn", "name", "origin": "plan"|"delegate", "tools": [...], "dropped": [...], "reused": None|"registered"|"spawned", "refused": None|str, "capabilities": [...], "rerouted_from": None}` (a superset of the 3.5.0 event: `name`, `capabilities`, `rerouted_from` are kept) and `{"type": "delegate_result", "name", "outcome", "chars"}`. Existing event types are otherwise unchanged.
- The `delegate` tool is marked `cacheable = False` (Task 1 adds the registry opt-out): a delegation has side effects, and the default tool-result cache is keyed without the caller, so a cached answer could come from a different run.
- Every behavior is implemented and tested for both the sync and async runner/supervisor. No real sleeping or wall-clock assertions in tests.
- **Commit messages carry NO `Co-Authored-By` trailer** (standing user rule: the user is the sole contributor). No bare `git stash`. Work on branch `feat/supervisor-subagents`.
- Run tests with `python -m pytest <path> -q -p no:cacheprovider` from the worktree root. The existing suite (581 passed, 4 skipped at the start of this plan) must stay green after every task.
- Files use LF line endings in the index; preserve each file's existing endings when editing.
- The cost cap API is `model.configure_limits(budget_usd=..., input_price_per_1k=..., output_price_per_1k=...)`.

## File Structure

| File | Responsibility |
|---|---|
| `agentx_dev/SubAgents.py` (new) | `AgentSpec`, `SpecError`, `parse_agent_spec`, `spec_from_legacy_spawn`, `SpawnRequest`, `SpawnConfig`, `SpawnRefused`, `SpawnRun`, `Built`, `SpawnPolicy`, `DelegationFailed`, `DelegateArgs`, `make_delegate_tool`, `attach_delegation`, `spawn_instruction` |
| `agentx_dev/Runner/AgentRun.py` | `ToolRegistry.unregister`; `AgentRunner.add_tool/remove_tool`; `delegation=` parameter; per-run reset hook |
| `agentx_dev/Runner/AsyncAgentRun.py` | `system_addendum` parameter; `add_tool/remove_tool`; `delegation=` parameter; per-run reset hook |
| `agentx_dev/Runner/Persistence.py` | `_active_budget` on `PersistenceMixin`, set by `run_persistent` / `run_persistent_async`; `_begin_delegation_run` hook |
| `agentx_dev/Supervisor.py` | re-export `SpawnRequest`/`SpawnConfig`; `new_agent` in prompts, `_sanitize_plan`, `_run_plan`; per-run agent registry; `_handle_spawn` shim; `SupervisorResult.spawned`; `AsyncSupervisor(spawn_config=)` |
| `agentx_dev/__init__.py` | export `AgentSpec` |
| `tests/subagent_helpers.py` (new) | shared test doubles: `router`, plan builders |
| `tests/test_runner_tools_runtime.py`, `test_subagent_policy.py`, `test_subagent_delegate.py`, `test_subagent_delegate_async.py`, `test_subagent_plan.py`, `test_subagent_plan_async.py`, `test_subagent_persistent.py` (new) | per-task tests |
| `docs/guides/sub-agents.md` (new) + cookbook, FAQ, troubleshooting, upgrading, API summary, supervisor docs, README, CHANGELOG, `pyproject.toml`, `host/*` | Task 7 |

## Task order

Tasks must run in order: Task 1 (runner support) and Task 2 (policy) are used by Task 3 (delegate), which is used by Tasks 4 and 5 (supervisors). Task 6 is integration tests; Task 7 docs and version; Task 8 is the release checklist (never run without the user's go-ahead).

---

### Task 1: Spec amendments and runner support (tool add/remove, async `system_addendum`, run budget)

**Files:**
- Modify: `docs/superpowers/specs/2026-10-04-supervisor-subagents-design.md`
- Modify: `agentx_dev/Runner/AgentRun.py` (`ToolRegistry`, `AgentRunner`)
- Modify: `agentx_dev/Runner/AsyncAgentRun.py` (`AsyncAgentRunner`)
- Modify: `agentx_dev/Runner/Persistence.py` (`PersistenceMixin`, `run_persistent`, `run_persistent_async`)
- Test: `tests/test_runner_tools_runtime.py`

**Interfaces:**
- Consumes: existing `ToolRegistry._register_one`, `AgentRunner.registry`, `PersistenceMixin`.
- Produces:
  - `ToolRegistry.unregister(name: str) -> None`
  - tools with `cacheable = False` are never read from or written to the registry's tool-result cache
  - `AgentRunner.add_tool(tool) -> None`, `AgentRunner.remove_tool(name: str) -> None` (same on `AsyncAgentRunner`)
  - `AsyncAgentRunner(..., system_addendum: Optional[str] = None)`; the text is appended to the system prompt after the act-don't-announce line, exactly like the sync runner
  - `PersistenceMixin._active_budget: Optional[RunBudget]` (class default `None`): the run's `RunBudget` while a persistent run is in progress, `None` otherwise
  - `PersistenceMixin._delegation: Any` (class default `None`) and `PersistenceMixin._begin_delegation_run()` (no-op unless `_delegation` is set; Task 3 fills it in)

- [ ] **Step 1: Amend the spec**

Run this from the worktree root (it asserts every anchor matches exactly once):

```bash
python - <<'PY'
import pathlib
p = pathlib.Path("docs/superpowers/specs/2026-10-04-supervisor-subagents-design.md")
s = p.read_text(encoding="utf-8")

def sub(a, b):
    global s
    assert s.count(a) == 1, a[:70]
    s = s.replace(a, b)

# 3.2: max_spawns is Optional so "unset" can mean 3 (legacy) or 6 (ceiling)
sub("- `max_spawns` (default stays 3 in legacy mode; 6 in ceiling mode and in the persistent\n  default) counts planner-time spawns and `delegate` calls together, per run.\n",
    "- `max_spawns` becomes `Optional[int] = None`. Unset means 3 in legacy mode and 6 in ceiling\n  mode and in the persistent default. It counts planner-time spawns and `delegate` calls\n  together, per run.\n")

# 4.1.5: function calling is auto-detected, as for any runner
sub("`max_iterations=15`, `use_function_calling=True`, `verbose=False`, and",
    "`max_iterations=15`, `verbose=False` (function calling is auto-detected from the model, as\n   for any runner), and")
sub("5. Build an `AgentRunner` (or `AsyncAgentRunner` under an async parent) with",
    "5. Build an `AgentRunner` (or `AsyncAgentRunner` under an async parent; `AsyncAgentRunner`\n   gains the same `system_addendum` parameter for this) with")

# 4.2: overlap guard becomes static prompt guidance plus a verbose log
sub("- **Overlap guard becomes advice.** `_find_existing_for_capabilities` no longer\n  refuses a spawn. When an existing specialist already covers the requested tools, the\n  planner receives a one-line note in the next planning prompt (recovery rounds), and\n  the verbose log records it. Custom instructions with the same tools are allowed.\n",
    "- **Overlap guard becomes advice.** `_find_existing_for_capabilities` and the reroute\n  machinery are removed: a spawn is never refused for overlapping tools. The planner prompt\n  tells it to prefer an existing specialist when one fits, and the verbose log notes when a\n  new agent duplicates an existing specialist's tools. Custom instructions with the same\n  tools are allowed.\n")

# 4.3.7: concurrency rides on the existing native batching
sub("7. **Concurrency.** In the async runner `delegate` is an async tool; several\n   `delegate` calls in one model turn run concurrently through the existing batch path.\n   In the sync runner they run in sequence.\n",
    "7. **Concurrency.** In the async runner `delegate` is an async tool. Several `delegate`\n   calls in one model turn run concurrently wherever the runner already dispatches a\n   turn's tool calls together (`bind_tools_natively=True`); in text and function-calling\n   mode a turn carries one call, so delegations from one agent run one after another.\n   Parallelism across plan steps (several specialists each delegating) is unaffected. In the\n   sync runner delegations run in sequence.\n")

# 4.6: the existing spawn event is kept and extended; standalone runners record, not stream
sub("- `{\"type\": \"spawn\", \"name\": str, \"origin\": \"plan\" | \"delegate\", \"tools\": [str], \"dropped\": [str], \"reused\": str | None}` on both supervisors' streams, emitted when a sub-agent is\n  built or reused. `reused` is `None` for a fresh build, `\"registered\"` or `\"spawned\"`\n  otherwise.\n",
    "- `{\"type\": \"spawn\", \"name\": str, \"origin\": \"plan\" | \"delegate\", \"tools\": [str], \"dropped\": [str], \"reused\": str | None, \"refused\": str | None, \"capabilities\": [str], \"rerouted_from\": None}`\n  on both supervisors' streams, emitted when a sub-agent is built, reused or refused. It is a\n  superset of the 3.5.0 spawn event (`name`, `capabilities`, `rerouted_from` keep their\n  meaning; `capabilities` repeats `tools`, `rerouted_from` is always `None` now). `reused`\n  is `None` for a fresh build, `\"registered\"` or `\"spawned\"` otherwise; `refused` is the\n  reason when the spawn was refused.\n")
sub("- Standalone runners with `delegation=` emit the same events to their stream where\n  they have one; the async runner replays them after the run like its other events\n  (3.5.0 behavior). `verbose=True` prints `[spawn]` lines.\n",
    "- Standalone runners with `delegation=` do not stream these events; they expose the same\n  records as `runner.spawned` (a list of dicts, reset at the start of each run).\n  `verbose=True` prints `[spawn]` lines everywhere.\n")

# 6: the one existing test that pins the leak stays valid
sub("all existing spawn tests pass unchanged; legacy-mode approval\n  flow and `auto_spawn_allowed_caps` behave as in 3.5.0;",
    "all existing spawn tests pass unchanged (`Supervisor._handle_spawn`\n  called directly, outside a run, still registers the agent on `self.agents`; inside a run\n  it registers on the per-run copy); legacy-mode approval flow and `auto_spawn_allowed_caps`\n  behave as in 3.5.0;")
p.write_text(s, encoding="utf-8", newline="\n")
print("spec amended")
PY
```
Expected: `spec amended`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_runner_tools_runtime.py`:

```python
"""Runner support for sub-agents: add/remove tools at run time, async system_addendum, run budget."""

import asyncio

import pytest

from agentx_dev import (
    AgentRunner, AgentType, AsyncAgentRunner, AsyncStandardTool, Persistence, StandardTool,
)
from tests.conftest import MockModel, make_final, make_react_response


def echo(x):
    return f"echo {x}"


ECHO = StandardTool(func=echo, name="echo", description="echoes")
LATE = StandardTool(func=lambda x: f"late {x}", name="late", description="added later")


def sync_runner(model, tools=(ECHO,), **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=list(tools), verbose=False, **kw)


def async_runner(model, tools=(ECHO,), **kw):
    return AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=list(tools), verbose=False, **kw)


class TestRegistryUnregister:
    def test_unregister_removes_every_trace(self):
        r = sync_runner(MockModel())
        r.registry.unregister("echo")
        assert not r.registry.has("echo") and r.registry.names == []
        assert "echo" not in r.registry.names_block()
        r.registry.unregister("echo")          # unknown names are ignored


class TestSyncAddRemove:
    def test_added_tool_is_dispatchable_and_listed_in_the_prompt(self):
        model = MockModel(script=[make_react_response("late", "x"), make_final("done")])
        r = sync_runner(model)
        r.add_tool(LATE)
        assert r.registry.has("late") and "late" in r._tool_names_block and LATE in r.tools
        result = r.invoke("go")
        assert "late x" in str(result.history)
        assert "late" in str(model.calls[0][0]["content"])      # the system prompt lists it

    def test_remove_restores_the_original_tools_and_does_not_mutate_the_callers_list(self):
        mine = [ECHO]
        r = sync_runner(MockModel(), tools=mine)
        r.add_tool(LATE)
        r.remove_tool("late")
        assert r.registry.names == ["echo"] and [t.name for t in r.tools] == ["echo"]
        assert mine == [ECHO]
        assert "late" not in r._tool_names_block

    def test_adding_a_duplicate_name_raises(self):
        r = sync_runner(MockModel())
        with pytest.raises(ValueError, match="already registered"):
            r.add_tool(StandardTool(func=echo, name="echo", description="dup"))


class TestAsyncAddRemove:
    def test_async_runner_add_and_remove(self):
        async def alate(x):
            return f"alate {x}"
        tool = AsyncStandardTool(func=alate, name="alate", description="async late")
        model = MockModel(script=[make_react_response("alate", "x"), make_final("done")])
        r = async_runner(model)
        r.add_tool(tool)
        assert r.registry.has("alate") and "alate" in r._tool_names_block
        result = asyncio.run(r.ainvoke("go"))
        assert "alate x" in str(result.history)
        r.remove_tool("alate")
        assert r.registry.names == ["echo"] and "alate" not in r._tool_names_block

    def test_adding_a_duplicate_name_raises(self):
        with pytest.raises(ValueError, match="already registered"):
            async_runner(MockModel()).add_tool(StandardTool(func=echo, name="echo", description="dup"))


class TestAsyncSystemAddendum:
    def test_the_addendum_reaches_the_system_prompt_after_the_act_line(self):
        model = MockModel(script=[make_final("ok")])
        r = async_runner(model, system_addendum="ROLE: be brief and cite sources")
        asyncio.run(r.ainvoke("hi"))
        system = str(model.calls[0][0]["content"])
        assert system.rstrip().endswith("ROLE: be brief and cite sources")

    def test_without_an_addendum_nothing_is_appended(self):
        model = MockModel(script=[make_final("ok")])
        asyncio.run(async_runner(model).ainvoke("hi"))
        assert "ROLE:" not in str(model.calls[0][0]["content"])


class TestActiveBudget:
    def test_it_is_none_outside_a_run_and_the_runs_budget_inside_one(self):
        seen = []
        box = {}

        def peek(x):
            seen.append(box["runner"]._active_budget)
            return "ok"

        tool = StandardTool(func=peek, name="peek", description="peeks")
        model = MockModel(script=[make_react_response("peek", "x"), make_final("done")])
        r = sync_runner(model, tools=(tool,), persistence=Persistence(max_minutes=5))
        box["runner"] = r
        assert r._active_budget is None
        r.invoke("go")
        assert len(seen) == 1 and seen[0] is not None and seen[0].remaining() > 0
        assert r._active_budget is None

    def test_async_runner_sets_and_clears_it_too(self):
        seen = []
        box = {}

        async def peek(x):
            seen.append(box["runner"]._active_budget)
            return "ok"

        tool = AsyncStandardTool(func=peek, name="peek", description="peeks")
        model = MockModel(script=[make_react_response("peek", "x"), make_final("done")])
        r = async_runner(model, tools=(tool,), persistence=Persistence(max_minutes=5))
        box["runner"] = r
        asyncio.run(r.ainvoke("go"))
        assert len(seen) == 1 and seen[0] is not None
        assert r._active_budget is None


class TestCacheableOptOut:
    def _counting_tool(self, cacheable):
        calls = []
        tool = StandardTool(func=lambda x: calls.append(x) or f"ran {len(calls)}",
                            name="side", description="may have side effects")
        if cacheable is not None:
            tool.cacheable = cacheable
        return tool, calls

    def test_a_tool_marked_not_cacheable_runs_every_time_even_with_the_cache_on(self):
        from agentx_dev import InMemoryCache
        tool, calls = self._counting_tool(False)
        r = sync_runner(MockModel(), tools=(tool,))
        r.registry.configure_cache(InMemoryCache())
        assert r.registry.dispatch("side", "same") == "ran 1"
        assert r.registry.dispatch("side", "same") == "ran 2"
        assert len(calls) == 2

    def test_an_ordinary_tool_is_still_cached(self):
        from agentx_dev import InMemoryCache
        tool, calls = self._counting_tool(None)
        r = sync_runner(MockModel(), tools=(tool,))
        r.registry.configure_cache(InMemoryCache())
        assert r.registry.dispatch("side", "same") == "ran 1"
        assert r.registry.dispatch("side", "same") == "ran 1"
        assert len(calls) == 1

    def test_the_async_dispatch_honours_the_opt_out(self):
        from agentx_dev import InMemoryCache
        tool, calls = self._counting_tool(False)
        r = async_runner(MockModel(), tools=(tool,))
        r.registry.configure_cache(InMemoryCache())
        assert asyncio.run(r.registry.adispatch("side", "same")) == "ran 1"
        assert asyncio.run(r.registry.adispatch("side", "same")) == "ran 2"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_runner_tools_runtime.py -q -p no:cacheprovider`
Expected: FAIL (collection or attribute errors: `unregister`, `add_tool`, `system_addendum`, `_active_budget` do not exist).

- [ ] **Step 4: Implement `ToolRegistry.unregister` and the `cacheable` opt-out**

In `agentx_dev/Runner/AgentRun.py`, replace
```python
    def get_breaker(self, name: str) -> Optional[CircuitBreaker]:
```
with
```python
    def unregister(self, name: str) -> None:
        """Remove a tool by name (a no-op for unknown names). The runner-level
        ``func`` / ``args`` views point at these dicts, so they follow."""
        self.sync_std.pop(name, None)
        self.sync_struct.pop(name, None)
        self.async_std.pop(name, None)
        self.async_struct.pop(name, None)
        self._tools = [t for t in self._tools if t.name != name]
        self._tool_by_name.pop(name, None)
        self._breakers.pop(name, None)

    def get_breaker(self, name: str) -> Optional[CircuitBreaker]:
```

A tool can opt out of the tool-result cache by setting ``cacheable = False`` on the tool object. The ``delegate`` tool does (Task 3): a delegation has side effects, and the default cache is keyed without the caller, so a cached answer could come from a different run. In the same file, replace
```python
    def _cache_get(self, name: str, args):
        cache = getattr(self, "_cache", None)
        if cache is None:
            return None
```
with
```python
    def _uncacheable(self, name: str) -> bool:
        """A tool marked ``cacheable = False`` (side effects, or results that depend on the
        caller) always executes."""
        return getattr(self._tool_by_name.get(name), "cacheable", True) is False

    def _cache_get(self, name: str, args):
        cache = getattr(self, "_cache", None)
        if cache is None or self._uncacheable(name):
            return None
```
and replace
```python
    def _cache_set(self, name: str, args, result) -> None:
        cache = getattr(self, "_cache", None)
        if cache is None:
            return
```
with
```python
    def _cache_set(self, name: str, args, result) -> None:
        cache = getattr(self, "_cache", None)
        if cache is None or self._uncacheable(name):
            return
```

- [ ] **Step 5: Implement `add_tool` / `remove_tool` on `AgentRunner`**

In `agentx_dev/Runner/AgentRun.py`, replace
```python
    def _iter_run(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ):
```
with
```python
    def add_tool(self, tool) -> None:
        """Register ``tool`` on a built runner (used to attach ``delegate`` for the
        duration of a Supervisor run). The caller's original tool list is not mutated."""
        if self.registry.has(tool.name):
            raise ValueError(f"tool '{tool.name}' is already registered")
        self.registry._register_one(tool)
        self.tools = list(self.tools) + [tool]
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    def remove_tool(self, name: str) -> None:
        """Remove a tool by name (a no-op for unknown names)."""
        self.registry.unregister(name)
        self.tools = [t for t in self.tools if t.name != name]
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    def _iter_run(
        self,
        user_input: str,
        chat_history: Optional[List[Dict[str, str]]] = None,
        stream_tokens: bool = False,
        media: Optional[List[Any]] = None,
        _budget: Optional[RunBudget] = None,
    ):
```

- [ ] **Step 6: Implement `system_addendum`, `add_tool`, `remove_tool` on `AsyncAgentRunner`**

In `agentx_dev/Runner/AsyncAgentRun.py`:

(a) In the constructor signature, replace
```python
        strict_tool_dispatch: bool = False,
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
        persistence: Optional[Persistence] = None,
    ):
        """Construct an ``AsyncAgentRunner``. Parameters mirror
```
with
```python
        strict_tool_dispatch: bool = False,
        text_turn_nudges: int = 1,
        output_schema: Optional[Type[BaseModel]] = None,
        persistence: Optional[Persistence] = None,
        system_addendum: Optional[str] = None,
    ):
        """Construct an ``AsyncAgentRunner``. Parameters mirror
```

(b) Replace
```python
        # See AgentRunner.__init__ for the strict_tool_dispatch contract.
        self.strict_tool_dispatch = strict_tool_dispatch
```
with
```python
        # Role-specific instructions appended to the system prompt at run time
        # (same contract as AgentRunner.system_addendum).
        self.system_addendum = system_addendum
        # See AgentRunner.__init__ for the strict_tool_dispatch contract.
        self.strict_tool_dispatch = strict_tool_dispatch
```

(c) Replace
```python
        if self.registry.names:
            system_prompt = system_prompt + "\n\n" + _ACT_DONT_ANNOUNCE

        working_history = [{"role": "system", "content": system_prompt}]
```
with
```python
        if self.registry.names:
            system_prompt = system_prompt + "\n\n" + _ACT_DONT_ANNOUNCE
        if self.system_addendum:
            system_prompt = system_prompt + "\n\n" + self.system_addendum

        working_history = [{"role": "system", "content": system_prompt}]
```

(d) Replace
```python
    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
```
with
```python
    def add_tool(self, tool) -> None:
        """Register ``tool`` on a built runner. See ``AgentRunner.add_tool``."""
        if self.registry.has(tool.name):
            raise ValueError(f"tool '{tool.name}' is already registered")
        self.registry._register_one(tool)
        self.tools = list(self.tools) + [tool]
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    def remove_tool(self, name: str) -> None:
        """Remove a tool by name (a no-op for unknown names)."""
        self.registry.unregister(name)
        self.tools = [t for t in self.tools if t.name != name]
        self._tool_prompt_block = self.registry.prompt_block()
        self._tool_names_block = self.registry.names_block()

    async def Initialize(
        self,
        user_input: str,
        ChatHistory: Optional[List[Dict[str, str]]] = None,
        stream: bool = False,
```

- [ ] **Step 7: `PersistenceMixin` attributes and `run_persistent` budget exposure**

In `agentx_dev/Runner/Persistence.py`:

(a) Replace
```python
    _persistence: Optional[Persistence] = None
    _base_max_iterations: int = 4
    _saved_cache: Tuple[Any, Any] = (None, None)
```
with
```python
    _persistence: Optional[Persistence] = None
    _base_max_iterations: int = 4
    _saved_cache: Tuple[Any, Any] = (None, None)
    # The RunBudget of the persistent run in progress (None otherwise). Tools that
    # start sub-agents (``delegate``) read it so the sub-agent shares the deadline.
    _active_budget: Optional[RunBudget] = None
    # A SpawnPolicy when the runner was built with ``delegation=`` (see SubAgents).
    _delegation: Any = None

    def _begin_delegation_run(self) -> None:
        """Start a fresh spawn count for this invocation of a runner built with ``delegation=``."""
        policy = self._delegation
        if policy is not None:
            policy.persistence = self.persistence
            policy.new_run()
```

(b) Replace the whole `run_persistent` function (from `def run_persistent(` down to and including the line `    yield from events`) with:
```python
def run_persistent(runner, user_input, chat_history, stream_tokens, media, budget):
    """Generator wrapper for ``AgentRunner._iter_run``. Runs the loop under a
    ``PersistentRun`` and turns the exceptions that legitimately end a
    persistent run (deadline, exhausted ladder, cost limit) into a normal
    ``completion`` event whose ``outcome`` says why. Anything else
    (authentication errors, programming errors) propagates."""
    state = PersistentRun(runner.persistence, user_input, budget=budget, verbose=runner.verbose)
    runner._active_budget = state.budget
    try:
        try:
            yield from runner._iter_run_core(user_input, chat_history, stream_tokens, media, state)
            return
        except BudgetExpired:
            state.finish(OUTCOME_OUT_OF_TIME)
        except RunStuck as e:
            state.finish(OUTCOME_STUCK, str(e))
        except CostBudgetExceeded as e:
            state.finish(OUTCOME_OUT_OF_BUDGET, str(e))
        event = budget_event(state.outcome)
        if event is not None:
            state.emit(event)
            state._say(f"budget: the {event['reason']} limit ended the run")
        yield from state.drain()
        events = list(state.exit_events(runner.model.__class__.__name__, user_input))
        content = events[-1]["completion"].content
        _end_agent_event(state, content)
        _remember(runner, user_input, content)
        yield from events
    finally:
        runner._active_budget = None
```

(c) Replace the whole `run_persistent_async` function (from `async def run_persistent_async(` down to and including `    return completion`) with:
```python
async def run_persistent_async(runner, user_input, budget, run):
    """Async twin of :func:`run_persistent`. ``run`` is ``lambda state: <coroutine>``
    that runs the loop with the given ``PersistentRun``."""
    state = PersistentRun(runner.persistence, user_input, budget=budget, verbose=runner.verbose)
    runner._active_budget = state.budget
    try:
        try:
            return await run(state)
        except BudgetExpired:
            state.finish(OUTCOME_OUT_OF_TIME)
        except RunStuck as e:
            state.finish(OUTCOME_STUCK, str(e))
        except CostBudgetExceeded as e:
            state.finish(OUTCOME_OUT_OF_BUDGET, str(e))
        event = budget_event(state.outcome)
        if event is not None:
            # The async runner has no live event stream: astream() derives this
            # event from the completion's outcome. Log it here like the sync path.
            state._say(f"budget: the {event['reason']} limit ended the run")
        completion = state.exit_completion(runner.model.__class__.__name__, user_input)
        _end_agent_event(state, completion.content)
        _remember(runner, user_input, completion.content)
        return completion
    finally:
        runner._active_budget = None
```

- [ ] **Step 8: Run the new tests, then the full suite**

Run: `python -m pytest tests/test_runner_tools_runtime.py -q -p no:cacheprovider`
Expected: all pass.

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green (no regressions).

- [ ] **Step 9: Commit**

```bash
git add docs/superpowers/specs/2026-10-04-supervisor-subagents-design.md agentx_dev/Runner tests/test_runner_tools_runtime.py
git commit -m "feat(runner): add/remove tools at run time, async system_addendum, expose the run budget"
```

---

### Task 2: `SubAgents.py` — specs, the ceiling, and the policy that builds sub-agents

**Files:**
- Create: `agentx_dev/SubAgents.py`
- Create: `tests/subagent_helpers.py`
- Modify: `agentx_dev/Supervisor.py` (delete the spawn block that moves, import it back)
- Modify: `agentx_dev/__init__.py` (export `AgentSpec`)
- Test: `tests/test_subagent_policy.py`

**Interfaces:**
- Consumes: Task 1's `AsyncAgentRunner(system_addendum=)`, `add_tool`; existing `Permissions`, `web_search_tool`, `web_fetch_tool`, `AgentRunner`, `AsyncAgentRunner`, `Persistence`.
- Produces (all in `agentx_dev/SubAgents.py`):
  - `SpawnRequest`, `SpawnConfig` (moved from `Supervisor.py`, still importable from `agentx_dev.Supervisor` and `agentx_dev`); new `SpawnConfig.tools`, `.capabilities`, `.max_depth`, `.ceiling_mode` (property), `.effective_max_spawns` (property); `max_spawns: Optional[int] = None`
  - `AgentSpec(name, instructions, tools=(), origin="plan")` frozen dataclass; `SpecError(ValueError)`; `parse_agent_spec(raw, origin="plan") -> AgentSpec`; `spec_from_legacy_spawn(step: dict) -> AgentSpec`
  - `SpawnRefused(Exception)` with `.reason: str`
  - `Built(spec, runner, description, granted, dropped, reused=None)` dataclass
  - `SpawnRun` (`count`, `built`, `records`, `events`, `next_delegate_name()`, `emit(event)`, `drain() -> list`, `finish(name, outcome, chars)`)
  - `SpawnPolicy(config, model, *, persistence=None, is_async=False, verbose=False, run=None)` with `.enabled`, `.run`, `.new_run()`, `.menu() -> str`, `.resolve(requested) -> (granted, pool_tools, dropped)`, `.build(spec, depth=1, *, is_async=None) -> Built` (raises `SpawnRefused`), `.obtain(spec, depth=1, *, registered=(), is_async=None) -> Built` (reuse / collision handling, then `build`)
  - `spawn_instruction(policy) -> str` (the planner-prompt block)
  - Constants `PRESET_TOOLS`, `TOOL_TO_PRESETS`, `DELEGATE_TOOL_NAME = "delegate"`, `MAX_INSTRUCTIONS_CHARS = 4000`, `SUMMARY_CAP_CHARS = 4000`

- [ ] **Step 1: Fix the spec's `files_read` tool list**

`DefaultTools` gives read access `find_files` and `grep` as well. Run from the worktree root:

```bash
python - <<'PY'
import pathlib
p = pathlib.Path("docs/superpowers/specs/2026-10-04-supervisor-subagents-design.md")
s = p.read_text(encoding="utf-8")
a = "`files_read` (read-only: `read_path`, `list_directory`)."
assert s.count(a) == 1
p.write_text(s.replace(a, "`files_read` (read-only: `read_path`, `list_directory`, `find_files`, `grep`)."),
             encoding="utf-8", newline="\n")
PY
```

- [ ] **Step 2: Write the shared test helpers**

Create `tests/subagent_helpers.py`:

```python
"""Test doubles for the sub-agent tests: one scripted model that plays planner, synthesizer,
specialist and sub-agent, told apart by what is in the first message."""

import json
from typing import Callable, List, Optional

from tests.conftest import MockModel, make_final

PLANNER_MARK = "You are a Supervisor"
SYNTH_MARK = "answering the user's question directly"
SPAWNED_MARK = "You were spawned by a Supervisor"


def plan_json(*steps) -> str:
    return json.dumps({"plan": list(steps)})


def step(id_: str, agent: str, query: str = "q", deps: Optional[List[str]] = None, **extra) -> dict:
    d = {"id": id_, "agent": agent, "query": query}
    if deps:
        d["depends_on"] = deps
    d.update(extra)
    return d


def new_agent_step(id_: str, name: str, instructions: str, tools=(), query: str = "q",
                   deps: Optional[List[str]] = None, **extra) -> dict:
    d = {"id": id_, "query": query,
         "new_agent": {"name": name, "instructions": instructions, "tools": list(tools)}}
    if deps:
        d["depends_on"] = deps
    d.update(extra)
    return d


def router(plans=(), synth: str = "Final.", agent: Optional[Callable] = None, sub: Optional[Callable] = None):
    """A MockModel whose script routes by role.

    - planner prompt  -> the next item of ``plans`` (a JSON string)
    - synthesis       -> ``synth``
    - an agent whose system prompt carries the spawned-specialist addendum -> ``sub(messages)``
    - any other agent -> ``agent(messages)``; the default answers ``make_final("agent done")``
    """
    queue = list(plans)

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first:
            return queue.pop(0)
        if SYNTH_MARK in first:
            return synth
        if SPAWNED_MARK in first and sub is not None:
            return sub(messages)
        if agent is not None:
            return agent(messages)
        return make_final("agent done")

    model = MockModel(script=script)
    model.planner_prompts = lambda: [str(c[0]["content"]) for c in model.calls if PLANNER_MARK in str(c[0]["content"])]
    return model
```

- [ ] **Step 3: Write the failing tests for specs, config and the policy**

Create `tests/test_subagent_policy.py`:

```python
"""Specs, the SpawnConfig ceiling, and SpawnPolicy: the one place a sub-agent is built."""

import asyncio

import pytest

from agentx_dev import AgentRunner, AsyncAgentRunner, Persistence, StandardTool
from agentx_dev.SubAgents import (
    AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpecError, parse_agent_spec,
    spawn_instruction, spec_from_legacy_spawn,
)
from tests.conftest import MockModel


def tool_names(runner):
    return sorted(runner.registry.names)


def policy(**cfg):
    cfg.setdefault("enabled", True)
    return SpawnPolicy(SpawnConfig(**cfg), MockModel(), verbose=False)


CEILING = dict(capabilities={"web", "files_read"}, allowed_paths=["./workspace"])


class TestSpecs:
    def test_parse_agent_spec_normalizes(self):
        s = parse_agent_spec({"name": " analyst ", "instructions": "  Compare prices.  ",
                              "tools": ["web", "web", " web_fetch "]})
        assert s == AgentSpec("analyst", "Compare prices.", ("web", "web_fetch"), "plan")

    def test_instructions_are_capped(self):
        s = parse_agent_spec({"name": "a", "instructions": "x" * 9000})
        assert len(s.instructions) == 4000

    @pytest.mark.parametrize("raw", [
        "nope", None, [],
        {"instructions": "i"},                                # no name
        {"name": "has space", "instructions": "i"},
        {"name": "x" * 41, "instructions": "i"},
        {"name": "a"},                                        # no instructions
        {"name": "a", "instructions": "   "},
        {"name": "a", "instructions": "i", "tools": "web"},   # not a list
        {"name": "a", "instructions": "i", "tools": [1]},
    ])
    def test_invalid_specs_raise(self, raw):
        with pytest.raises(SpecError):
            parse_agent_spec(raw)

    def test_legacy_spawn_step_becomes_a_spec(self):
        s = spec_from_legacy_spawn({"agent": "__spawn__", "name": "analyst",
                                    "description": "runs python", "capabilities": ["code", "files"]})
        assert s == AgentSpec("analyst", "runs python", ("code", "files"), "plan")
        with pytest.raises(SpecError):
            spec_from_legacy_spawn({"agent": "__spawn__", "name": "", "description": "d"})


class TestSpawnConfig:
    def test_modes_and_defaults(self):
        legacy = SpawnConfig(enabled=True)
        assert not legacy.ceiling_mode and legacy.effective_max_spawns == 3 and legacy.max_depth == 1
        assert SpawnConfig(enabled=True, capabilities=set()).ceiling_mode
        assert SpawnConfig(enabled=True, tools=[]).ceiling_mode
        assert SpawnConfig(enabled=True, capabilities={"web"}).effective_max_spawns == 6
        assert SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=2).effective_max_spawns == 2
        assert SpawnConfig(enabled=True, max_spawns=9).effective_max_spawns == 9

    def test_still_importable_from_the_old_places(self):
        import agentx_dev
        from agentx_dev.Supervisor import SpawnConfig as S2, SpawnRequest as R2
        assert S2 is SpawnConfig and agentx_dev.SpawnConfig is SpawnConfig and R2 is agentx_dev.SpawnRequest


class TestResolve:
    def test_clips_to_the_ceiling_and_reports_what_was_dropped(self):
        p = policy(capabilities={"web"})
        granted, pool, dropped = p.resolve(["web", "code", "bogus"])
        assert granted == ["web"] and pool == [] and dropped == ["code", "bogus"]

    def test_single_tool_names_map_to_the_narrowest_allowed_preset(self):
        p = policy(**CEILING)
        assert p.resolve(["read_path"])[0] == ["files_read"]
        assert p.resolve(["web_fetch"])[0] == ["web"]
        granted, _, dropped = p.resolve(["write_file", "run_python"])
        assert granted == [] and dropped == ["write_file", "run_python"]

    def test_files_subsumes_files_read(self):
        p = policy(capabilities={"files"})
        assert p.resolve(["files_read", "files"])[0] == ["files"]
        assert p.resolve(["files_read"])[0] == ["files_read"]      # "files" allows the read-only preset

    def test_pool_tools_resolve_by_name(self):
        mine = StandardTool(func=lambda x: x, name="lookup", description="look something up")
        p = policy(tools=[mine])
        granted, pool, dropped = p.resolve(["lookup", "web"])
        assert granted == [] and pool == [mine] and dropped == ["web"]      # no presets allowed in this ceiling

    def test_legacy_mode_allows_every_preset_word(self):
        granted, _, dropped = policy().resolve(["web", "files", "code", "delete", "nope"])
        assert granted == ["web", "files", "code", "delete"] and dropped == ["nope"]


class TestBuild:
    def test_builds_a_sync_runner_with_the_clipped_tools_and_the_instructions(self):
        p = policy(**CEILING)
        built = p.build(AgentSpec("analyst", "Compare prices. Return a table.", ("web", "files", "code")))
        r = built.runner
        assert isinstance(r, AgentRunner) and not isinstance(r, AsyncAgentRunner)
        assert built.granted == ["web"] and built.dropped == ["files", "code"]   # preset words must be allowed exactly
        assert "web_search" in tool_names(r) and "run_python" not in tool_names(r)
        assert "write_file" not in tool_names(r)
        assert "Compare prices. Return a table." in r.system_addendum
        assert "You were spawned by a Supervisor" in r.system_addendum
        assert "delegate" not in tool_names(r)                     # depth 1 == max_depth 1

    def test_the_read_only_preset_grants_read_tools_only(self):
        built = policy(**CEILING).build(AgentSpec("reader", "Read.", ("files_read",)))
        assert built.granted == ["files_read"]
        names = tool_names(built.runner)
        assert "read_path" in names and "grep" in names
        assert "write_file" not in names and "edit_file" not in names

    def test_async_policy_builds_an_async_runner(self):
        p = SpawnPolicy(SpawnConfig(enabled=True, **CEILING), MockModel(), is_async=True)
        built = p.build(AgentSpec("a", "Be useful.", ("web",)))
        assert isinstance(built.runner, AsyncAgentRunner) and "Be useful." in built.runner.system_addendum
        sync = p.build(AgentSpec("b", "Be useful.", ("web",)), is_async=False)
        assert type(sync.runner) is AgentRunner

    def test_persistence_is_inherited(self):
        persistence = Persistence(max_minutes=5)
        p = SpawnPolicy(SpawnConfig(enabled=True, **CEILING), MockModel(), persistence=persistence)
        runner = p.build(AgentSpec("a", "x", ("web",))).runner
        assert runner.persistence is persistence and runner.max_iterations == persistence.max_turns

    def test_a_spec_with_no_tools_gets_none(self):
        runner = policy(**CEILING).build(AgentSpec("thinker", "Just reason.")).runner
        assert tool_names(runner) == []

    def test_requested_tools_none_usable_is_a_refusal(self):
        with pytest.raises(SpawnRefused) as e:
            policy(**CEILING).build(AgentSpec("a", "x", ("code", "delete")))
        assert e.value.reason == "no usable tools"

    def test_disabled_and_limit_refusals(self):
        with pytest.raises(SpawnRefused, match="disabled"):
            policy(enabled=False).build(AgentSpec("a", "x"))
        p = policy(max_spawns=2, **CEILING)
        p.build(AgentSpec("a", "x", ("web",)))
        p.build(AgentSpec("b", "x", ("web",)))
        with pytest.raises(SpawnRefused, match="spawn limit reached"):
            p.build(AgentSpec("c", "x", ("web",)))
        assert p.run.count == 2

    def test_pool_tool_colliding_with_a_default_tool_is_refused(self):
        clash = StandardTool(func=lambda x: x, name="read_path", description="clashes")
        p = policy(tools=[clash], capabilities={"files_read"})
        with pytest.raises(SpawnRefused, match="could not build"):
            p.build(AgentSpec("a", "x", ("read_path", "files_read")))

    def test_pool_tools_reach_the_runner(self):
        mine = StandardTool(func=lambda x: x, name="lookup", description="look something up")
        runner = policy(tools=[mine]).build(AgentSpec("a", "x", ("lookup",))).runner
        assert "lookup" in tool_names(runner)


class TestApproval:
    def test_ceiling_mode_needs_no_approval(self):
        policy(**CEILING).build(AgentSpec("a", "x", ("web",)))           # no approver, no prompt

    def test_ceiling_mode_still_consults_an_approver_when_one_is_set(self):
        asked = []
        p = policy(approver=lambda req: asked.append(req) or False, **CEILING)
        with pytest.raises(SpawnRefused, match="not approved"):
            p.build(AgentSpec("a", "why", ("web",)))
        assert asked[0].name == "a" and asked[0].capabilities == ["web"] and asked[0].description == "why"

    def test_an_approver_that_raises_counts_as_a_refusal(self):
        def boom(req):
            raise RuntimeError("no")
        with pytest.raises(SpawnRefused, match="not approved"):
            policy(approver=boom, **CEILING).build(AgentSpec("a", "x", ("web",)))

    def test_legacy_auto_spawn_with_an_allowed_caps_gate(self):
        p = policy(auto_spawn=True, auto_spawn_allowed_caps={"web"})
        p.build(AgentSpec("ok", "x", ("web",)))
        with pytest.raises(SpawnRefused, match="not approved"):            # code is outside the gate, no approver
            p.build(AgentSpec("bad", "x", ("code",)))

    def test_legacy_gate_falls_through_to_the_approver(self):
        p = policy(auto_spawn=True, auto_spawn_allowed_caps={"web"}, approver=lambda r: True)
        assert p.build(AgentSpec("code_agent", "x", ("code",))).granted == ["code"]

    def test_legacy_without_auto_spawn_uses_the_approver(self):
        assert policy(approver=lambda r: True).build(AgentSpec("a", "x", ("web",))).granted == ["web"]
        with pytest.raises(SpawnRefused):
            policy(approver=lambda r: False).build(AgentSpec("a", "x", ("web",)))


class TestObtain:
    def test_identical_redefinition_reuses_without_spending_a_spawn(self):
        p = policy(max_spawns=1, **CEILING)
        first = p.obtain(AgentSpec("a", "same", ("web",)))
        again = p.obtain(AgentSpec("a", "same", ("web",)))
        assert again.reused == "spawned" and again.runner is first.runner and p.run.count == 1

    def test_a_differing_redefinition_gets_a_suffix(self):
        p = policy(**CEILING)
        p.obtain(AgentSpec("a", "one", ("web",)))
        other = p.obtain(AgentSpec("a", "two", ("web",)))
        assert other.spec.name == "a_2" and other.reused is None
        assert "a" in p.run.built and "a_2" in p.run.built

    def test_a_name_registered_by_the_developer_is_not_rebuilt(self):
        p = policy(**CEILING)
        got = p.obtain(AgentSpec("researcher", "ignored", ("web",)), registered={"researcher"})
        assert got.reused == "registered" and got.runner is None and p.run.count == 0


class TestEventsAndRecords:
    def test_spawn_event_shape_and_record(self):
        p = policy(**CEILING)
        p.build(AgentSpec("a", "x", ("web", "code")))
        [ev] = p.run.drain()
        assert ev == {"type": "spawn", "name": "a", "origin": "plan", "tools": ["web"],
                      "dropped": ["code"], "reused": None, "refused": None,
                      "capabilities": ["web"], "rerouted_from": None}
        assert p.run.records == [{"name": "a", "origin": "plan", "tools": ["web"],
                                  "dropped": ["code"], "outcome": None, "chars": None}]
        assert p.run.drain() == []
        p.run.finish("a", "done", 12)
        assert p.run.records[0]["outcome"] == "done" and p.run.records[0]["chars"] == 12

    def test_a_refusal_emits_an_event_with_the_reason(self):
        p = policy(**CEILING)
        with pytest.raises(SpawnRefused):
            p.build(AgentSpec("a", "x", ("code",)))
        [ev] = p.run.drain()
        assert ev["refused"] == "no usable tools" and ev["name"] == "a" and ev["reused"] is None

    def test_new_run_resets_counters_and_registry(self):
        p = policy(**CEILING)
        p.build(AgentSpec("a", "x", ("web",)))
        p.new_run()
        assert p.run.count == 0 and p.run.built == {} and p.run.records == []

    def test_delegate_names_are_unique_and_valid(self):
        p = policy(**CEILING)
        assert [p.run.next_delegate_name() for _ in range(3)] == ["delegate_1", "delegate_2", "delegate_3"]


class TestPromptBlock:
    def test_lists_only_what_the_ceiling_allows_and_the_spawn_budget(self):
        mine = StandardTool(func=lambda x: x, name="lookup", description="look something up\nmore")
        text = spawn_instruction(policy(tools=[mine], capabilities={"web"}, max_spawns=4))
        assert "web:" in text and "files:" not in text and "code:" not in text
        assert "lookup: look something up" in text and "more" not in text
        assert "at most 4" in text and "new_agent" in text

    def test_legacy_mode_lists_every_preset(self):
        text = spawn_instruction(policy())
        for word in ("web:", "files:", "code:", "delete:"):
            assert word in text
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m pytest tests/test_subagent_policy.py -q -p no:cacheprovider`
Expected: FAIL at import (`agentx_dev.SubAgents` does not exist).

- [ ] **Step 5: Create `agentx_dev/SubAgents.py` — move the spawn block, then add the new code**

Run this from the worktree root. It cuts the spawn block out of `Supervisor.py` (everything from the "Dynamic sub-agent spawning" banner up to the "Result types" banner), keeps the pieces that move (`SpawnRequest`, `SpawnConfig`, `_default_interactive_approver`, `_SPAWNED_SPECIALIST_ADDENDUM`) and drops `_build_spawned_agent` (the policy replaces it). It writes the head of `SubAgents.py`; Step 6 appends the rest.

```bash
python - <<'PY'
import pathlib

sup_path = pathlib.Path("agentx_dev/Supervisor.py")
sup = sup_path.read_text(encoding="utf-8")
crlf = "\r\n" in sup
sup = sup.replace("\r\n", "\n")

banner_start = "# ----------------------------------------------------------------------------\n# Dynamic sub-agent spawning\n# ----------------------------------------------------------------------------\n"
banner_end = "# ----------------------------------------------------------------------------\n# Result types\n"
i, j = sup.index(banner_start), sup.index(banner_end)
block = sup[i:j]

# Keep everything up to _build_spawned_agent (dropped: the policy replaces it).
cut = block.index("def _build_spawned_agent(")
kept = block[len(banner_start):cut].rstrip() + "\n"
assert "class SpawnRequest" in kept and "class SpawnConfig" in kept
assert "_SPAWNED_SPECIALIST_ADDENDUM" in kept and "_default_interactive_approver" in kept

# --- extend SpawnConfig -------------------------------------------------
old_fields = (
    "    allowed_paths: List[str] = field(default_factory=lambda: [\"./workspace\"])\n"
    "    max_spawns: int = 3\n"
    "    auto_spawn_allowed_caps: Optional[Set[str]] = None\n"
)
assert kept.count(old_fields) == 1
new_fields = (
    "    allowed_paths: List[str] = field(default_factory=lambda: [\"./workspace\"])\n"
    "    max_spawns: Optional[int] = None\n"
    "    auto_spawn_allowed_caps: Optional[Set[str]] = None\n"
    "    tools: Optional[List[Any]] = None\n"
    "    capabilities: Optional[Set[str]] = None\n"
    "    max_depth: int = 1\n"
    "\n"
    "    @property\n"
    "    def ceiling_mode(self) -> bool:\n"
    "        \"\"\"True when ``tools`` or ``capabilities`` is set: spawns inside that ceiling\n"
    "        need no approval. Otherwise the legacy approval flow applies.\"\"\"\n"
    "        return self.tools is not None or self.capabilities is not None\n"
    "\n"
    "    @property\n"
    "    def effective_max_spawns(self) -> int:\n"
    "        \"\"\"``max_spawns`` if set, else 6 in ceiling mode and 3 in legacy mode.\"\"\"\n"
    "        if self.max_spawns is not None:\n"
    "            return int(self.max_spawns)\n"
    "        return 6 if self.ceiling_mode else 3\n"
)
kept = kept.replace(old_fields, new_fields)

old_doc_tail = "            planner's source.\n    \"\"\"\n    enabled: bool = False\n"
assert kept.count(old_doc_tail) == 1
new_doc_tail = (
    "            planner's source.\n"
    "        tools: (3.6) Pool of tool objects a spawned agent may pick from by\n"
    "            name. Setting this (or ``capabilities``) switches to ceiling\n"
    "            mode: spawns inside the ceiling need no approval.\n"
    "        capabilities: (3.6) Preset words a spawn may use: 'web', 'files',\n"
    "            'files_read' (read-only), 'code', 'delete'. A ceiling, not a\n"
    "            request: anything outside it is dropped.\n"
    "        max_depth: (3.6) 1 (default) = a spawned agent cannot spawn more;\n"
    "            specialists you registered can delegate. Raise to allow deeper trees.\n"
    "        max_spawns: Defaults to 3 in legacy mode and 6 in ceiling mode;\n"
    "            counts planner-time spawns and ``delegate`` calls together.\n"
    "    \"\"\"\n"
    "    enabled: bool = False\n"
)
kept = kept.replace(old_doc_tail, new_doc_tail)
# The original docstring documented max_spawns as a plain int cap; keep one description.
kept = kept.replace(
    "        max_spawns: Upper bound on how many new specialists can be\n"
    "            added during one Supervisor run (guards against runaway).\n",
    "")

head = '''"""
Sub-agent creation for the Supervisor and for standalone runners.

Two ways to get a fresh, narrowly-scoped agent mid-task:

- **Planner-time**: a plan step carries a ``new_agent`` (name, free-form
  instructions, tools). The Supervisor builds it, runs the step on it, and
  discards it when the run ends.
- **Runtime**: a specialist calls the ``delegate`` tool to hand a piece of its
  own work to a fresh sub-agent and gets a short summary back.

Both go through one checkpoint, :class:`SpawnPolicy`, which clips every request to
the ceiling the developer set in :class:`SpawnConfig`. Instructions never widen the
ceiling.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from agentx_dev.Agents.Agent import AgentType
from agentx_dev.AsyncTools import AsyncStructuredTool
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner
from agentx_dev.Runner.Persistence import Persistence, accepts_budget
from agentx_dev.Tools import StructuredTool, logger


# ----------------------------------------------------------------------------
# Configuration (moved here from Supervisor.py; still importable from there)
# ----------------------------------------------------------------------------

'''

sub_path = pathlib.Path("agentx_dev/SubAgents.py")
sub_path.write_text(head + kept, encoding="utf-8", newline="\n")

# --- remove the block from Supervisor.py and import the pieces back ------
sup = sup[:i] + sup[j:]
old_import = "from agentx_dev.Tools import logger\n"
assert sup.count(old_import) == 1
sup = sup.replace(old_import, old_import +
    "from agentx_dev.SubAgents import (   # noqa: F401  (SpawnConfig/SpawnRequest are re-exported)\n"
    "    AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpawnRequest, SpecError,\n"
    "    _default_interactive_approver, attach_delegation, parse_agent_spec, spawn_instruction,\n"
    "    spec_from_legacy_spawn,\n"
    ")\n")
sup_path.write_text(sup.replace("\n", "\r\n") if crlf else sup, encoding="utf-8", newline="")
print("moved; Supervisor.py was", "CRLF" if crlf else "LF")
PY
```
Expected: `moved; ...`. (`Supervisor.py` now imports names `SpawnPolicy`, `attach_delegation`, ... that Step 6 defines; it will not import until Step 6 is done.)

- [ ] **Step 6: Append the new code to `agentx_dev/SubAgents.py`**

Append (after the moved block) exactly:

```python


# ----------------------------------------------------------------------------
# Specs
# ----------------------------------------------------------------------------

MAX_INSTRUCTIONS_CHARS = 4000
SUMMARY_CAP_CHARS = 4000
DELEGATE_TOOL_NAME = "delegate"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# Preset capability word -> the concrete tool names it installs.
PRESET_TOOLS: Dict[str, Set[str]] = {
    "web": {"web_search", "web_fetch"},
    "files": {"read_path", "list_directory", "find_files", "grep", "write_file", "edit_file"},
    "files_read": {"read_path", "list_directory", "find_files", "grep"},
    "code": {"run_python"},
    "delete": {"delete_path"},
}

# A single tool name resolves to the narrowest preset that contains it and that the ceiling allows.
TOOL_TO_PRESETS: Dict[str, Tuple[str, ...]] = {
    "web_search": ("web",), "web_fetch": ("web",),
    "read_path": ("files_read", "files"), "list_directory": ("files_read", "files"),
    "find_files": ("files_read", "files"), "grep": ("files_read", "files"),
    "write_file": ("files",), "edit_file": ("files",),
    "run_python": ("code",), "delete_path": ("delete",),
}

_PRESET_PERMS: Dict[str, Dict[str, bool]] = {
    "files": dict(read_files=True, write_files=True, edit_files=True, list_directories=True),
    "files_read": dict(read_files=True, list_directories=True),
    "code": dict(execute_python=True),
    "delete": dict(delete_files=True),
}

_PRESET_DOC: Dict[str, str] = {
    "web": "web_search + web_fetch (search the web, fetch pages)",
    "files": "read, write, edit and list files inside the sandbox",
    "files_read": "read, list, find and grep files inside the sandbox (read-only)",
    "code": "run_python (execute Python for computation)",
    "delete": "delete files inside the sandbox",
}

_DEFAULT_DELEGATE_INSTRUCTIONS = (
    "You are a focused helper. Do exactly the task you are given and report the result."
)


class SpecError(ValueError):
    """A ``new_agent`` / legacy spawn description is malformed."""


@dataclass(frozen=True)
class AgentSpec:
    """One sub-agent to build: who it is and which tools it asks for."""

    name: str
    instructions: str
    tools: Tuple[str, ...] = ()
    origin: str = "plan"            # "plan" | "delegate"


def parse_agent_spec(raw: Any, origin: str = "plan") -> AgentSpec:
    """Validate and normalize a ``new_agent`` object. Raises :class:`SpecError`."""
    if not isinstance(raw, dict):
        raise SpecError("new_agent must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
        raise SpecError("name must match [A-Za-z0-9_-] and be 1-40 characters")
    instructions = raw.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise SpecError("instructions must be a non-empty string")
    tools = raw.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise SpecError("tools must be a list of tool names")
    cleaned = tuple(dict.fromkeys(t.strip() for t in tools if t.strip()))
    return AgentSpec(
        name=name.strip(),
        instructions=instructions.strip()[:MAX_INSTRUCTIONS_CHARS],
        tools=cleaned,
        origin=origin,
    )


def spec_from_legacy_spawn(step: Dict[str, Any]) -> AgentSpec:
    """Turn a 3.5 ``{"agent": "__spawn__", name, description, capabilities}`` step into a spec."""
    return parse_agent_spec({
        "name": str(step.get("name", "")).strip(),
        "instructions": str(step.get("description", "")).strip(),
        "tools": [str(c).strip() for c in (step.get("capabilities") or []) if c],
    })


class SpawnRefused(Exception):
    """The policy declined to build a sub-agent. ``reason`` is shown to the planner / caller."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class DelegationFailed(Exception):
    """Raised by the ``delegate`` tool when the sub-agent did not finish. The tool registry
    turns it into a tool error, so the caller's stuck tracker sees it."""


@dataclass
class Built:
    """What the policy hands back for one spec."""

    spec: AgentSpec
    runner: Any                      # None when the name resolved to a developer-registered specialist
    description: str
    granted: List[str]               # preset words and pool tool names actually granted
    dropped: List[str]               # requested names that were clipped
    reused: Optional[str] = None     # None | "registered" | "spawned"


class SpawnRun:
    """Per-run state shared by every spawn in one Supervisor run (or one runner invocation)."""

    def __init__(self) -> None:
        self.count = 0
        self.built: Dict[str, Built] = {}
        self.records: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._delegates = 0

    def next_delegate_name(self) -> str:
        self._delegates += 1
        return f"delegate_{self._delegates}"

    def emit(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def drain(self) -> List[Dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def finish(self, name: str, outcome: str, chars: int) -> None:
        """Fill in the outcome of the most recent open record for ``name``."""
        for rec in reversed(self.records):
            if rec["name"] == name and rec.get("outcome") is None:
                rec["outcome"] = outcome
                rec["chars"] = chars
                return


# ----------------------------------------------------------------------------
# The policy: the single checkpoint
# ----------------------------------------------------------------------------

_SPAWN_INSTRUCTION = """

── CREATING NEW SPECIALISTS ─────────────────────────────────────────
You may define a NEW specialist inline when none of the available specialists fits a sub-task: a different role, different instructions, or tools nobody else has. Use it sparingly: at most <<MAX>> new specialists per run.

Put the definition on the step that first uses it:

  {"id": "s2", "query": "<the sub-task>",
   "new_agent": {"name": "<short_snake_case>",
                 "instructions": "<who the specialist is and exactly what data it must return>",
                 "tools": ["<tool name>", "..."]}}

Later steps reuse it by name: {"id": "s3", "agent": "<that name>", "query": "..."}. Do NOT define it again.

Rules:
  - Prefer an existing specialist whenever one fits. Define a new one only for a genuinely different role or toolset.
  - "instructions" must say what the specialist should RETURN (facts, numbers, a table), not just what to do. The framework adds: return the real data, never invent it.
  - "tools" may only name tools from the menu below; anything else is dropped. A specialist with no tools can still reason.
  - A step uses EITHER "agent" OR "new_agent", never both. "name" is letters, digits, "_" or "-" (max 40).

Tools you can grant:
<<MENU>>
────────────────────────────────────────────────────────────────────
"""


def spawn_instruction(policy: "SpawnPolicy") -> str:
    """The planner-prompt block that teaches ``new_agent``."""
    return (_SPAWN_INSTRUCTION
            .replace("<<MAX>>", str(policy.config.effective_max_spawns))
            .replace("<<MENU>>", policy.menu()))


class SpawnPolicy:
    """Turns an :class:`AgentSpec` into a runner, inside the ceiling in ``config``.

    One policy serves one Supervisor run (or one invocation of a runner built with
    ``delegation=``). Everything that creates a sub-agent calls :meth:`build` /
    :meth:`obtain` here, so the rules cannot drift apart.
    """

    def __init__(
        self,
        config: SpawnConfig,
        model: Any,
        *,
        persistence: Optional[Persistence] = None,
        is_async: bool = False,
        verbose: bool = False,
        run: Optional[SpawnRun] = None,
    ):
        self.config = config
        self.model = model
        self.persistence = persistence
        self.is_async = is_async
        self.verbose = verbose
        self.run = run or SpawnRun()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def new_run(self) -> None:
        self.run = SpawnRun()

    def _say(self, text: str) -> None:
        if self.verbose:
            print(f"[spawn] {text}")

    # -- the ceiling ----------------------------------------------------------

    def _preset_allowed(self, preset: str) -> bool:
        cfg = self.config
        if not cfg.ceiling_mode:
            return True
        allowed = {c.lower() for c in (cfg.capabilities or set())}
        return preset in allowed or (preset == "files_read" and "files" in allowed)

    def menu(self) -> str:
        """The tools a planner may name, one per line, for the planner prompt."""
        lines: List[str] = []
        for preset in ("web", "files_read", "files", "code", "delete"):
            if self._preset_allowed(preset):
                lines.append(f"  - {preset}: {_PRESET_DOC[preset]}")
        for tool in self.config.tools or []:
            first = str(getattr(tool, "description", "") or "").strip().splitlines()
            lines.append(f"  - {tool.name}: {first[0][:120] if first else ''}".rstrip())
        return "\n".join(lines) if lines else "  (none: new specialists can only reason)"

    def resolve(self, requested: Iterable[str]) -> Tuple[List[str], List[Any], List[str]]:
        """Clip ``requested`` to the ceiling. Returns ``(granted presets, pool tools, dropped names)``."""
        pool = {t.name: t for t in (self.config.tools or []) if getattr(t, "name", None)}
        granted: List[str] = []
        tools: List[Any] = []
        dropped: List[str] = []
        for raw in requested:
            word = str(raw).strip()
            low = word.lower()
            if low in PRESET_TOOLS:
                if self._preset_allowed(low):
                    if low not in granted:
                        granted.append(low)
                else:
                    dropped.append(word)
            elif word in pool:
                if pool[word] not in tools:
                    tools.append(pool[word])
            elif word in TOOL_TO_PRESETS:
                pick = next((p for p in TOOL_TO_PRESETS[word] if self._preset_allowed(p)), None)
                if pick is None:
                    dropped.append(word)
                elif pick not in granted:
                    granted.append(pick)
            else:
                dropped.append(word)
        if "files" in granted and "files_read" in granted:
            granted.remove("files_read")            # "files" already covers read access
        return granted, tools, dropped

    # -- approval -------------------------------------------------------------

    def _approve(self, spec: AgentSpec) -> bool:
        cfg = self.config
        req = SpawnRequest(name=spec.name, description=spec.instructions,
                           capabilities=list(spec.tools), rationale="")
        try:
            if cfg.ceiling_mode:
                return bool(cfg.approver(req)) if cfg.approver is not None else True
            if cfg.auto_spawn:
                asked = {c.lower() for c in req.capabilities}
                allow = cfg.auto_spawn_allowed_caps
                if allow is not None and not asked.issubset(allow):
                    if cfg.approver is None:
                        return False
                    return bool(cfg.approver(req))
                return True
            return bool((cfg.approver or _default_interactive_approver)(req))
        except Exception as e:                       # an approver that raises is a "no"
            logger.warning(f"spawn approver raised; treating as a refusal: {e}")
            return False

    # -- building -------------------------------------------------------------

    def _describe(self, spec: AgentSpec, granted: List[str]) -> str:
        first = spec.instructions.strip().splitlines()[0][:200]
        return f"{first} (tools: {', '.join(granted) or 'none'})"

    def _make_runner(self, spec: AgentSpec, granted_presets: List[str], pool_tools: List[Any],
                     is_async: bool) -> Any:
        from agentx_dev.DefaultTools import Permissions
        from agentx_dev.WebTools import web_fetch_tool, web_search_tool

        cfg = self.config
        flags: Dict[str, bool] = {}
        tools: List[Any] = []
        file_caps = {"files", "code", "delete"} & set(granted_presets)
        cache_dir = str(cfg.allowed_paths[0]) if (cfg.allowed_paths and file_caps) else None
        for preset in granted_presets:
            if preset == "web":
                tools.extend([web_search_tool(), web_fetch_tool(cache_dir=cache_dir)])
            else:
                flags.update(_PRESET_PERMS[preset])
        tools.extend(pool_tools)
        perms = Permissions(allowed_paths=list(cfg.allowed_paths), **flags) if flags else None
        cls = AsyncAgentRunner if is_async else AgentRunner
        return cls(
            model=self.model,
            agent=AgentType.ReAct,
            tools=tools,
            permissions=perms,
            max_iterations=15,
            verbose=False,
            system_addendum=_SPAWNED_SPECIALIST_ADDENDUM + "\n\nYour role:\n" + spec.instructions,
        )

    def build(self, spec: AgentSpec, depth: int = 1, *, is_async: Optional[bool] = None) -> Built:
        """Build a runner for ``spec`` at ``depth`` or raise :class:`SpawnRefused`.
        Emits the ``spawn`` event either way."""
        try:
            built = self._build(spec, depth, self.is_async if is_async is None else is_async)
        except SpawnRefused as e:
            self.run.emit({"type": "spawn", "name": spec.name, "origin": spec.origin,
                           "tools": [], "dropped": list(spec.tools), "reused": None,
                           "refused": e.reason, "capabilities": [], "rerouted_from": None})
            self._say(f"refused '{spec.name}': {e.reason}")
            raise
        self.run.count += 1
        self.run.built[spec.name] = built
        self.run.records.append({"name": spec.name, "origin": spec.origin,
                                 "tools": list(built.granted), "dropped": list(built.dropped),
                                 "outcome": None, "chars": None})
        self._announce(built)
        return built

    def _build(self, spec: AgentSpec, depth: int, is_async: bool) -> Built:
        cfg = self.config
        if not cfg.enabled:
            raise SpawnRefused("spawning is disabled")
        if self.run.count >= cfg.effective_max_spawns:
            raise SpawnRefused("spawn limit reached")
        presets, pool_tools, dropped = self.resolve(spec.tools)
        if spec.tools and not presets and not pool_tools:
            raise SpawnRefused("no usable tools")
        if not self._approve(spec):
            raise SpawnRefused("not approved")
        try:
            runner = self._make_runner(spec, presets, pool_tools, is_async)
        except Exception as e:                       # e.g. a pool tool clashing with a default tool
            raise SpawnRefused(f"could not build the agent: {e}") from e
        if self.persistence is not None:
            runner.persistence = self.persistence
        if depth < cfg.max_depth:
            runner.add_tool(make_delegate_tool(runner, self, depth + 1))
        granted = presets + [t.name for t in pool_tools]
        return Built(spec=spec, runner=runner, description=self._describe(spec, granted),
                     granted=granted, dropped=dropped)

    def obtain(self, spec: AgentSpec, depth: int = 1, *, registered: Iterable[str] = (),
               is_async: Optional[bool] = None) -> Built:
        """Reuse or build. A name the developer registered is not rebuilt; an identical
        earlier spawn is reused for free; a clashing name gets a numeric suffix."""
        taken = set(registered)
        prior = self.run.built.get(spec.name)
        if prior is None and spec.name in taken:
            built = Built(spec=spec, runner=None, description="", granted=[], dropped=[], reused="registered")
            self._announce(built)
            return built
        if prior is not None:
            if (prior.spec.instructions, prior.spec.tools) == (spec.instructions, spec.tools):
                built = dataclasses.replace(prior, reused="spawned")
                self._announce(built)
                return built
            n = 2
            while f"{spec.name}_{n}" in taken or f"{spec.name}_{n}" in self.run.built:
                n += 1
            spec = dataclasses.replace(spec, name=f"{spec.name}_{n}")
        return self.build(spec, depth, is_async=is_async)

    def _announce(self, built: Built) -> None:
        self.run.emit({"type": "spawn", "name": built.spec.name, "origin": built.spec.origin,
                       "tools": list(built.granted), "dropped": list(built.dropped),
                       "reused": built.reused, "refused": None,
                       "capabilities": list(built.granted), "rerouted_from": None})
        what = f"reusing {built.reused} agent" if built.reused else "built"
        self._say(f"{what} '{built.spec.name}' ({built.spec.origin}) tools={built.granted or 'none'}"
                  + (f" dropped={built.dropped}" if built.dropped else ""))


# The delegate tool is defined in Task 3; the policy only needs the name at call time.
def make_delegate_tool(parent: Any, policy: SpawnPolicy, depth: int) -> Any:  # replaced in Task 3
    raise NotImplementedError("delegate tools are added in Task 3")


@contextmanager
def attach_delegation(runners: Iterable[Any], policy: SpawnPolicy, depth: int = 0):  # replaced in Task 3
    yield
```

(Task 3 replaces the two stubs at the bottom with the real implementations.)

- [ ] **Step 7: Export `AgentSpec` and keep the old exports**

In `agentx_dev/__init__.py`, replace
```python
from .Supervisor import (
    Supervisor, AsyncSupervisor,
    SpawnConfig, SpawnRequest, Specialist,
    SupervisorResult, SubtaskResult,
)
```
with
```python
from .Supervisor import (
    Supervisor, AsyncSupervisor,
    SpawnConfig, SpawnRequest, Specialist,
    SupervisorResult, SubtaskResult,
)
from .SubAgents import AgentSpec
```
and in the `__all__` list, after the `"Persistence",` entry, add `"AgentSpec",`.

- [ ] **Step 8: Keep the 3.5 spawn path working until Task 4 replaces it**

Step 5 removed `_build_spawned_agent` and made `SpawnConfig.max_spawns` optional, so the still-unchanged `Supervisor._handle_spawn` needs a small bridge. In `agentx_dev/Supervisor.py`:

(a) Immediately before the line `def _normalize_agents(agents: Dict[str, Any]) -> Dict[str, Specialist]:` add the following, followed by two blank lines:
```python
def _build_spawned_agent(request, model, allowed_paths):
    """Bridge for ``Supervisor._handle_spawn`` until Task 4 rewrites it: builds the 3.5
    capability-word specialist through the policy (approval already happened there)."""
    cfg = SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=list(allowed_paths))
    built = SpawnPolicy(cfg, model).build(
        AgentSpec(request.name, request.description, tuple(request.capabilities)))
    return built.description, built.runner
```

(b) In `Supervisor._handle_spawn`, replace
```python
        if self._spawns_this_run >= cfg.max_spawns:
```
with
```python
        if self._spawns_this_run >= cfg.effective_max_spawns:
```
and replace
```python
                      f"({cfg.max_spawns}) already reached{_C_RESET}")
```
with
```python
                      f"({cfg.effective_max_spawns}) already reached{_C_RESET}")
```

- [ ] **Step 9: Run the tests, then the whole suite**

Run: `python -m pytest tests/test_subagent_policy.py -q -p no:cacheprovider`
Expected: all pass.

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green (the existing `tests/test_supervisor_persistent.py::TestSpawnedSpecialists` passes through the bridge).

- [ ] **Step 10: Commit**

```bash
git add agentx_dev/SubAgents.py agentx_dev/Supervisor.py agentx_dev/__init__.py tests/subagent_helpers.py tests/test_subagent_policy.py docs/superpowers/specs/2026-10-04-supervisor-subagents-design.md
git commit -m "feat(subagents): AgentSpec, the SpawnConfig ceiling, and the SpawnPolicy checkpoint"
```

---

### Task 3: The `delegate` tool, `attach_delegation`, and `delegation=` on runners

**Files:**
- Modify: `agentx_dev/SubAgents.py` (replace the two stubs from Task 2)
- Modify: `agentx_dev/Runner/Persistence.py` (`PersistenceMixin.spawned`)
- Modify: `agentx_dev/Runner/AgentRun.py`, `agentx_dev/Runner/AsyncAgentRun.py` (`delegation=` parameter, per-run reset)
- Test: `tests/test_subagent_delegate.py`, `tests/test_subagent_delegate_async.py`

**Interfaces:**
- Consumes: Task 1 (`add_tool`, `remove_tool`, `_active_budget`, `_delegation`, `_begin_delegation_run`), Task 2 (`SpawnPolicy.build`, `SpawnRun`, `AgentSpec`, `SpawnRefused`, `DelegationFailed`, `DELEGATE_TOOL_NAME`).
- Produces:
  - `DelegateArgs` (pydantic: `task: str`, `instructions: str = ""`, `tools: List[str] = []`)
  - `make_delegate_tool(parent, policy, depth) -> StructuredTool | AsyncStructuredTool` (async tool when `parent.Initialize` is a coroutine function)
  - `attach_delegation(runners, policy, depth=0)` context manager: adds the tool to each runner that has `add_tool`, no existing `delegate`, and only when `policy.enabled` and `depth < policy.config.max_depth`; removes it on exit
  - `cap_summary(text) -> str` (4000 characters plus `\n[truncated]`)
  - `AgentRunner(..., delegation: Optional[SpawnConfig] = None)` and the same on `AsyncAgentRunner`: with an enabled config the runner gets a `delegate` tool; `runner.spawned` lists this run's records; the spawn count resets at the start of every run

- [ ] **Step 1: Write the failing sync tests**

Create `tests/test_subagent_delegate.py`:

```python
"""The delegate tool and attach_delegation (sync runners)."""

from types import SimpleNamespace

import pytest

from agentx_dev import AgentRunner, AgentType, Persistence, StandardTool, ToolError
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.SubAgents import (
    AgentSpec, Built, SpawnConfig, SpawnPolicy, attach_delegation, cap_summary,
)
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import router

CEILING = dict(enabled=True, capabilities={"web"})


def parent(model, cfg=None, **kw):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                       delegation=cfg or SpawnConfig(**CEILING), **kw)


def delegating_agent(args):
    """A parent model that delegates on its first turn and finishes on its second."""
    turns = []

    def script(messages):
        turns.append(1)
        return make_react_response("delegate", args) if len(turns) == 1 else make_final("parent done")
    return script


class FakeRunner:
    def __init__(self, content="fake answer", outcome="done", boom=None):
        self.content, self.outcome, self.boom = content, outcome, boom
        self.task = None
        self.budget = None

    def Initialize(self, task, _budget=None):
        self.task, self.budget = task, _budget
        if self.boom:
            raise self.boom
        return SimpleNamespace(content=self.content, outcome=self.outcome)


def fake_build(p, runner, dropped=()):
    def build(spec, depth=1, *, is_async=None):
        return Built(spec=spec, runner=runner, description="d", granted=[], dropped=list(dropped))
    p._delegation.build = build


class TestDelegateEndToEnd:
    def test_a_specialist_hands_work_to_a_fresh_sub_agent_and_gets_the_answer_back(self):
        args = {"task": "find X", "instructions": "You find X. Return it.", "tools": ["web"]}
        model = router(agent=delegating_agent(args), sub=lambda m: make_final("X is 42"))
        p = parent(model)
        result = p.invoke("PARENT-SECRET: do the thing")
        assert result.content == "parent done" and "X is 42" in str(result.history)
        [rec] = p.spawned
        assert rec["name"] == "delegate_1" and rec["origin"] == "delegate"
        assert rec["outcome"] == "done" and rec["chars"] == len("X is 42") and rec["tools"] == ["web"]

    def test_the_sub_agent_sees_its_task_and_role_but_not_the_callers_history(self):
        args = {"task": "find X", "instructions": "You find X. Return it.", "tools": []}
        seen = []
        model = router(agent=delegating_agent(args), sub=lambda m: seen.append(m) or make_final("ok"))
        parent(model).invoke("PARENT-SECRET: do the thing")
        text = str(seen[0])
        assert "find X" in text and "You find X. Return it." in text
        assert "PARENT-SECRET" not in text

    def test_the_summary_is_capped_with_a_marker(self):
        model = router(agent=delegating_agent({"task": "t"}), sub=lambda m: make_final("y" * 9000))
        p = parent(model)
        out = p.registry.dispatch("delegate", {"task": "t"})
        assert isinstance(out, str) and len(out) == 4000 + len("\n[truncated]") and out.endswith("[truncated]")
        assert cap_summary("short") == "short"

    def test_clipped_tools_are_reported_back_to_the_caller(self):
        model = router(sub=lambda m: make_final("done"))
        out = parent(model).registry.dispatch("delegate", {"task": "t", "tools": ["web", "code"]})
        assert out.startswith("done") and "[note: these tools were not granted: code]" in out

    def test_events_and_the_spawn_count_reset_for_every_run(self):
        cfg = SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1)
        model = router(sub=lambda m: make_final("ok"))
        p = parent(model, cfg)
        assert p.registry.dispatch("delegate", {"task": "one"}) == "ok"
        events = p._delegation.run.drain()
        assert [e["type"] for e in events] == ["spawn", "delegate_result"]
        assert events[1] == {"type": "delegate_result", "name": "delegate_1", "outcome": "done", "chars": 2}
        refused = p.registry.dispatch("delegate", {"task": "two"})
        assert refused == "delegation refused: spawn limit reached; do this yourself"
        # a new invocation starts a fresh count and a fresh record list
        p.invoke("again")
        assert p._delegation.run.count == 0 and p._delegation.run.records == []
        assert p.registry.dispatch("delegate", {"task": "three"}) == "ok"


class TestRefusalsAndFailures:
    def test_refusals_are_plain_results_not_errors(self):
        p = parent(router())
        assert p.registry.dispatch("delegate", {"task": "   "}) == \
            "delegation refused: task is empty; do this yourself"
        off = parent(router(), SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=0))
        out = off.registry.dispatch("delegate", {"task": "t"})
        assert out == "delegation refused: spawn limit reached; do this yourself"
        none = parent(router()).registry.dispatch("delegate", {"task": "t", "tools": ["code"]})
        assert none == "delegation refused: no usable tools; do this yourself"

    def test_a_sub_agent_that_gave_up_raises_a_tool_error_with_its_report(self):
        p = parent(router())
        fake_build(p, FakeRunner("tried A, tried B", outcome="stuck"))
        out = p.registry.dispatch("delegate", {"task": "t"})
        assert isinstance(out, ToolError)
        assert "[delegate failed: stuck] tried A, tried B" in str(out)
        events = p._delegation.run.drain()
        assert events[-1] == {"type": "delegate_result", "name": "delegate_1", "outcome": "stuck", "chars": 16}

    def test_a_sub_agent_that_raises_is_a_tool_error(self):
        p = parent(router())
        fake_build(p, FakeRunner(boom=RuntimeError("provider exploded")))
        out = p.registry.dispatch("delegate", {"task": "t"})
        assert isinstance(out, ToolError) and "[delegate failed: error] provider exploded" in str(out)

    def test_a_stuck_delegation_does_not_end_the_callers_run(self):
        args = {"task": "t"}
        model = router(agent=delegating_agent(args))
        p = parent(model)
        fake_build(p, FakeRunner("nope", outcome="stuck"))
        result = p.invoke("go")
        assert result.outcome == "done" and result.content == "parent done"
        assert "[delegate failed: stuck] nope" in str(result.history)


class TestPersistenceAndBudget:
    def test_the_sub_agent_shares_the_callers_deadline(self):
        args = {"task": "t"}
        model = router(agent=delegating_agent(args))
        p = parent(model, persistence=Persistence(max_minutes=5))
        fake = FakeRunner("ok")
        fake_build(p, fake)
        p.invoke("go")
        assert isinstance(fake.budget, RunBudget) and fake.budget.remaining() > 0

    def test_without_persistence_no_budget_is_passed(self):
        p = parent(router(agent=delegating_agent({"task": "t"})))
        fake = FakeRunner("ok")
        fake_build(p, fake)
        p.invoke("go")
        assert fake.budget is None

    def test_built_sub_agents_inherit_the_callers_persistence(self):
        persistence = Persistence(max_minutes=5)
        model = router(agent=delegating_agent({"task": "t"}), sub=lambda m: make_final("ok"))
        p = parent(model, persistence=persistence)
        p.invoke("go")
        assert p._delegation.persistence is persistence
        assert p._delegation.run.built["delegate_1"].runner.persistence is persistence


class TestDepth:
    def test_depth_decides_whether_a_built_sub_agent_can_delegate(self):
        p = SpawnPolicy(SpawnConfig(max_depth=2, **CEILING), MockModel())
        assert "delegate" in p.build(AgentSpec("a", "x", ("web",)), depth=1).runner.registry.names
        assert "delegate" not in p.build(AgentSpec("b", "x", ("web",)), depth=2).runner.registry.names

    def test_the_default_depth_gives_a_sub_agent_no_delegate(self):
        p = SpawnPolicy(SpawnConfig(**CEILING), MockModel())
        assert "delegate" not in p.build(AgentSpec("a", "x", ("web",)), depth=1).runner.registry.names


class TestWiring:
    def test_no_config_or_a_disabled_config_means_no_delegate_tool(self):
        plain = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        off = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False,
                          delegation=SpawnConfig(enabled=False))
        assert "delegate" not in plain.registry.names and "delegate" not in off.registry.names
        assert plain.spawned == []

    def test_a_delegation_is_never_answered_from_the_tool_cache(self):
        from agentx_dev import InMemoryCache
        answers = iter(["one", "two"])
        p = parent(router(sub=lambda m: make_final(next(answers))))
        p.registry.configure_cache(InMemoryCache())
        assert p.registry.dispatch("delegate", {"task": "same"}) == "one"
        assert p.registry.dispatch("delegate", {"task": "same"}) == "two"

    def test_the_tool_description_carries_the_menu_of_grantable_tools(self):
        tool = parent(MockModel()).registry._tool_by_name["delegate"]
        assert "web:" in tool.description and "files:" not in tool.description


class TestAttachDelegation:
    def test_adds_then_removes_and_skips_runners_that_already_have_it(self):
        p = SpawnPolicy(SpawnConfig(**CEILING), MockModel())
        a = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        b = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False,
                        delegation=SpawnConfig(**CEILING))          # already has its own delegate
        own = b.registry._tool_by_name["delegate"]
        plain = object()                                            # no add_tool: ignored
        with attach_delegation([a, b, plain, a], p):
            assert "delegate" in a.registry.names and a.registry.names.count("delegate") == 1
            assert b.registry._tool_by_name["delegate"] is own
        assert "delegate" not in a.registry.names
        assert b.registry._tool_by_name["delegate"] is own           # the runner's own tool stays

    def test_not_attached_when_disabled_or_at_max_depth(self):
        a = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        with attach_delegation([a], SpawnPolicy(SpawnConfig(enabled=False), MockModel())):
            assert "delegate" not in a.registry.names
        with attach_delegation([a], SpawnPolicy(SpawnConfig(**CEILING), MockModel()), depth=1):
            assert "delegate" not in a.registry.names              # depth 1 is not < max_depth 1

    def test_removed_even_when_the_block_raises(self):
        a = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        with pytest.raises(RuntimeError):
            with attach_delegation([a], SpawnPolicy(SpawnConfig(**CEILING), MockModel())):
                raise RuntimeError("boom")
        assert "delegate" not in a.registry.names
```

- [ ] **Step 2: Write the failing async tests**

Create `tests/test_subagent_delegate_async.py`:

```python
"""The delegate tool on async runners, including concurrent delegation."""

import asyncio
from types import SimpleNamespace

import pytest

from agentx_dev import AsyncAgentRunner, AgentType, Persistence, ToolError
from agentx_dev.Runner.Persistence import RunBudget
from agentx_dev.SubAgents import Built, SpawnConfig, SpawnPolicy, attach_delegation
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import router

CEILING = dict(enabled=True, capabilities={"web"})


def aparent(model, cfg=None, **kw):
    return AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False,
                            delegation=cfg or SpawnConfig(**CEILING), **kw)


def delegating_agent(args):
    turns = []

    def script(messages):
        turns.append(1)
        return make_react_response("delegate", args) if len(turns) == 1 else make_final("parent done")
    return script


class FakeAsyncRunner:
    def __init__(self, content="fake answer", outcome="done", boom=None, barrier=None):
        self.content, self.outcome, self.boom, self.barrier = content, outcome, boom, barrier
        self.budget = None
        self.started = False

    async def Initialize(self, task, _budget=None):
        self.started, self.budget = True, _budget
        if self.barrier is not None:
            await self.barrier.hit()
        if self.boom:
            raise self.boom
        return SimpleNamespace(content=self.content, outcome=self.outcome)


class Barrier:
    """Both delegations must be in flight at once, or the wait times out and the test fails."""

    def __init__(self, n):
        self.n, self.seen, self.evt = n, 0, asyncio.Event()

    async def hit(self):
        self.seen += 1
        if self.seen >= self.n:
            self.evt.set()
        await asyncio.wait_for(self.evt.wait(), 2)


def fake_build(p, runners):
    queue = list(runners)

    def build(spec, depth=1, *, is_async=None):
        return Built(spec=spec, runner=queue.pop(0), description="d", granted=[], dropped=[])
    p._delegation.build = build


class TestAsyncDelegate:
    def test_the_async_runner_gets_an_async_delegate_tool_and_a_sub_agent_answers(self):
        model = router(agent=delegating_agent({"task": "find X", "instructions": "You find X."}),
                       sub=lambda m: make_final("X is 42"))
        p = aparent(model)
        assert p.registry.is_async("delegate")
        result = asyncio.run(p.ainvoke("PARENT-SECRET go"))
        assert result.content == "parent done" and "X is 42" in str(result.history)
        [rec] = p.spawned
        assert rec["name"] == "delegate_1" and rec["outcome"] == "done"
        # the policy built an AsyncAgentRunner for it
        assert isinstance(p._delegation.run.built["delegate_1"].runner, AsyncAgentRunner)

    def test_the_sub_agent_does_not_see_the_callers_history(self):
        seen = []
        model = router(agent=delegating_agent({"task": "find X", "instructions": "You find X."}),
                       sub=lambda m: seen.append(m) or make_final("ok"))
        asyncio.run(aparent(model).ainvoke("PARENT-SECRET go"))
        assert "find X" in str(seen[0]) and "PARENT-SECRET" not in str(seen[0])

    def test_refusals_and_failures_match_the_sync_tool(self):
        p = aparent(router())
        refusal = asyncio.run(p.registry.adispatch("delegate", {"task": " "}))
        assert refusal == "delegation refused: task is empty; do this yourself"
        fake_build(p, [FakeAsyncRunner("gave up", outcome="stuck"), FakeAsyncRunner(boom=RuntimeError("boom"))])
        stuck = asyncio.run(p.registry.adispatch("delegate", {"task": "t"}))
        assert isinstance(stuck, ToolError) and "[delegate failed: stuck] gave up" in str(stuck)
        crashed = asyncio.run(p.registry.adispatch("delegate", {"task": "t"}))
        assert isinstance(crashed, ToolError) and "[delegate failed: error] boom" in str(crashed)

    def test_the_sub_agent_shares_the_callers_deadline(self):
        model = router(agent=delegating_agent({"task": "t"}))
        p = aparent(model, persistence=Persistence(max_minutes=5))
        fake = FakeAsyncRunner("ok")
        fake_build(p, [fake])
        asyncio.run(p.ainvoke("go"))
        assert isinstance(fake.budget, RunBudget) and fake.budget.remaining() > 0

    def test_a_sync_parent_under_async_code_still_gets_a_sync_tool(self):
        from agentx_dev import AgentRunner
        sync_parent = AgentRunner(model=MockModel(), agent=AgentType.ReAct, tools=[], verbose=False)
        policy = SpawnPolicy(SpawnConfig(**CEILING), MockModel(), is_async=True)
        with attach_delegation([sync_parent], policy):
            assert not sync_parent.registry.is_async("delegate")


class TestConcurrentDelegation:
    def test_two_delegations_in_one_native_turn_run_at_the_same_time(self):
        calls = [{"name": "delegate", "input": {"task": f"job {i}"}, "id": f"c0_{i}"} for i in range(2)]
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "delegate", "input": {"task": "job 0"}, "tool_calls": calls},
            {"type": "tool_use", "name": "respond", "id": "c9", "input": {"answer": "both done"}},
        ])
        p = aparent(model, bind_tools_natively=True)
        barrier = Barrier(2)
        fake_build(p, [FakeAsyncRunner("A", barrier=barrier), FakeAsyncRunner("B", barrier=barrier)])
        result = asyncio.run(p.ainvoke("go"))
        assert result.content == "both done"
        assert barrier.seen == 2
        text = str([m for m in result.history if m["role"] == "tool"])
        assert "A" in text and "B" in text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_subagent_delegate.py tests/test_subagent_delegate_async.py -q -p no:cacheprovider`
Expected: FAIL (`delegation=` is not a parameter; `cap_summary` does not exist).

- [ ] **Step 4: Implement the delegate tool and `attach_delegation`**

In `agentx_dev/SubAgents.py`, replace the two stubs at the bottom of the file, from the comment line `# The delegate tool is defined in Task 3; ...` through the end of the file, with:

```python
# ----------------------------------------------------------------------------
# The delegate tool
# ----------------------------------------------------------------------------

class DelegateArgs(BaseModel):
    task: str = Field(description="Everything the sub-agent needs to do the job. It sees ONLY this, "
                                  "not your conversation, so include every fact it needs.")
    instructions: str = Field(default="", description="Who the sub-agent is and exactly what it must return.")
    tools: List[str] = Field(default_factory=list,
                             description="Names of the tools it may use. Anything not offered is dropped.")


DELEGATE_DESCRIPTION = (
    "Hand a self-contained piece of your work to a fresh sub-agent and get its final answer back. "
    "The sub-agent starts with a clean context and sees only `task`, so put every fact it needs in "
    "`task`. Use it for large side jobs that would clutter your own context (reading many files, "
    "researching a subtopic), not for trivial steps. It returns a short summary."
)


def cap_summary(text: Any, cap: int = SUMMARY_CAP_CHARS) -> str:
    """The sub-agent's answer as the caller sees it: at most ``cap`` characters plus a marker."""
    s = str(text)
    return s if len(s) <= cap else s[:cap] + "\n[truncated]"


def _is_async_runner(runner: Any) -> bool:
    return asyncio.iscoroutinefunction(getattr(runner, "Initialize", None))


def _delegate_spec(policy: SpawnPolicy, instructions: Any, tools: Any) -> AgentSpec:
    inst = str(instructions or "").strip()[:MAX_INSTRUCTIONS_CHARS] or _DEFAULT_DELEGATE_INSTRUCTIONS
    names = tuple(dict.fromkeys(str(t).strip() for t in (tools or []) if str(t).strip()))
    return AgentSpec(name=policy.run.next_delegate_name(), instructions=inst, tools=names, origin="delegate")


def _prepare(policy: SpawnPolicy, depth: int, is_async: bool, task: Any, instructions: Any,
             tools: Any) -> Tuple[Optional[str], Optional[Built]]:
    """``(refusal text, None)`` or ``(None, built sub-agent)``."""
    if not str(task or "").strip():
        return "delegation refused: task is empty; do this yourself", None
    spec = _delegate_spec(policy, instructions, tools)
    try:
        return None, policy.build(spec, depth, is_async=is_async)
    except SpawnRefused as e:
        return f"delegation refused: {e.reason}; do this yourself", None


def _delegate_error(policy: SpawnPolicy, built: Built, exc: BaseException) -> DelegationFailed:
    name = built.spec.name
    policy.run.finish(name, "error", 0)
    policy.run.emit({"type": "delegate_result", "name": name, "outcome": "error", "chars": 0})
    return DelegationFailed(f"[delegate failed: error] {exc}")


def _delegate_result(policy: SpawnPolicy, built: Built, completion: Any) -> str:
    name = built.spec.name
    outcome = getattr(completion, "outcome", "done")
    outcome = outcome if isinstance(outcome, str) else "done"
    text = str(getattr(completion, "content", "") or "")
    policy.run.finish(name, outcome, len(text))
    policy.run.emit({"type": "delegate_result", "name": name, "outcome": outcome, "chars": len(text)})
    if outcome != "done":
        raise DelegationFailed(f"[delegate failed: {outcome}] {cap_summary(text)}")
    note = (f"\n[note: these tools were not granted: {', '.join(built.dropped)}]" if built.dropped else "")
    return cap_summary(text) + note


def make_delegate_tool(parent: Any, policy: SpawnPolicy, depth: int) -> Any:
    """The ``delegate`` tool for ``parent``. ``depth`` is the depth a sub-agent built by it gets.
    An async parent gets an async tool that builds async sub-agents; a sync parent a sync tool."""
    description = DELEGATE_DESCRIPTION + "\nTools you may grant:\n" + policy.menu()

    if _is_async_runner(parent):
        async def delegate(task: str, instructions: str = "", tools: Optional[List[str]] = None) -> str:
            refusal, built = _prepare(policy, depth, True, task, instructions, tools)
            if refusal is not None:
                return refusal
            budget = getattr(parent, "_active_budget", None)
            kw = {"_budget": budget} if budget is not None and accepts_budget(built.runner.Initialize) else {}
            try:
                completion = await built.runner.Initialize(task, **kw)
            except Exception as e:
                raise _delegate_error(policy, built, e) from e
            return _delegate_result(policy, built, completion)

        tool = AsyncStructuredTool(func=delegate, args_schema=DelegateArgs,
                                   name=DELEGATE_TOOL_NAME, description=description)
        tool.cacheable = False        # a delegation has side effects: never answer it from the cache
        return tool

    def delegate(task: str, instructions: str = "", tools: Optional[List[str]] = None) -> str:
        refusal, built = _prepare(policy, depth, False, task, instructions, tools)
        if refusal is not None:
            return refusal
        budget = getattr(parent, "_active_budget", None)
        kw = {"_budget": budget} if budget is not None and accepts_budget(built.runner.Initialize) else {}
        try:
            completion = built.runner.Initialize(task, **kw)
        except Exception as e:
            raise _delegate_error(policy, built, e) from e
        return _delegate_result(policy, built, completion)

    tool = StructuredTool(func=delegate, args_schema=DelegateArgs,
                          name=DELEGATE_TOOL_NAME, description=description)
    tool.cacheable = False            # a delegation has side effects: never answer it from the cache
    return tool


@contextmanager
def attach_delegation(runners: Iterable[Any], policy: SpawnPolicy, depth: int = 0):
    """Give every runner in ``runners`` the ``delegate`` tool for the duration of the block.

    Runners that cannot take a tool (no ``add_tool``), that already have a ``delegate`` tool,
    or are at ``depth >= max_depth`` are left alone. The tool is removed on exit, even if the
    block raises, so the developer's runners are exactly as they were."""
    attached: List[Any] = []
    if policy.enabled and depth < policy.config.max_depth:
        for r in runners:
            if not callable(getattr(r, "add_tool", None)) or not hasattr(r, "registry"):
                continue
            if r.registry.has(DELEGATE_TOOL_NAME):
                continue
            r.add_tool(make_delegate_tool(r, policy, depth + 1))
            attached.append(r)
    try:
        yield
    finally:
        for r in attached:
            r.remove_tool(DELEGATE_TOOL_NAME)
```

- [ ] **Step 5: `PersistenceMixin.spawned`**

In `agentx_dev/Runner/Persistence.py`, inside `PersistenceMixin`, immediately after the `_begin_delegation_run` method added in Task 1, add:
```python

    @property
    def spawned(self) -> List[Dict[str, Any]]:
        """This run's sub-agents (name, origin, tools, dropped, outcome, chars). Empty
        unless the runner was built with ``delegation=``."""
        policy = self._delegation
        return list(policy.run.records) if policy is not None else []
```

- [ ] **Step 6: `delegation=` on `AgentRunner`**

In `agentx_dev/Runner/AgentRun.py`:

(a) Replace
```python
        persistence: Optional[Persistence] = None,
    ):
        """Construct an ``AgentRunner``.
```
with
```python
        persistence: Optional[Persistence] = None,
        delegation: Optional[Any] = None,
    ):
        """Construct an ``AgentRunner``.
```

(b) In the docstring, replace
```python
                keeps the ordinary loop.
```
with
```python
                keeps the ordinary loop.
            delegation: (3.6) An enabled ``SpawnConfig`` gives the runner a
                ``delegate`` tool: it can hand part of its work to a fresh
                sub-agent (clean context, tools clipped to the config's
                ceiling) and get a short summary back. ``runner.spawned``
                lists this run's sub-agents. Default ``None``: no tool.
```

(c) Replace
```python
            raise ValueError("The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}, or an AgentFormattor instance.")

    def Tool_Runner(self, tool_name: str, args_str) -> Any:
```
with
```python
            raise ValueError("The 'Agent' object must be a template string containing '{tools}','{tool_names}',{user_input}, or an AgentFormattor instance.")

        if delegation is not None and delegation.enabled:
            from agentx_dev.SubAgents import SpawnPolicy, make_delegate_tool
            self._delegation = SpawnPolicy(delegation, self.model, persistence=self.persistence,
                                           is_async=False, verbose=self.verbose)
            if delegation.max_depth > 0:
                self.add_tool(make_delegate_tool(self, self._delegation, 1))

    def Tool_Runner(self, tool_name: str, args_str) -> Any:
```

(d) Replace
```python
        ``outcome`` says why."""
        if self.persistence is None:
            yield from self._iter_run_core(user_input, chat_history, stream_tokens, media, None)
            return
```
with
```python
        ``outcome`` says why."""
        self._begin_delegation_run()
        if self.persistence is None:
            yield from self._iter_run_core(user_input, chat_history, stream_tokens, media, None)
            return
```

- [ ] **Step 7: `delegation=` on `AsyncAgentRunner`**

In `agentx_dev/Runner/AsyncAgentRun.py`:

(a) Replace
```python
        persistence: Optional[Persistence] = None,
        system_addendum: Optional[str] = None,
    ):
        """Construct an ``AsyncAgentRunner``. Parameters mirror
```
with
```python
        persistence: Optional[Persistence] = None,
        system_addendum: Optional[str] = None,
        delegation: Optional[Any] = None,
    ):
        """Construct an ``AsyncAgentRunner``. Parameters mirror
```

(b) Replace
```python
                "or an AgentFormattor instance."
            )

    def _auto_add_batch_concurrent_tool(self):
```
with
```python
                "or an AgentFormattor instance."
            )

        if delegation is not None and delegation.enabled:
            from agentx_dev.SubAgents import SpawnPolicy, make_delegate_tool
            self._delegation = SpawnPolicy(delegation, self.model, persistence=self.persistence,
                                           is_async=True, verbose=self.verbose)
            if delegation.max_depth > 0:
                self.add_tool(make_delegate_tool(self, self._delegation, 1))

    def _auto_add_batch_concurrent_tool(self):
```

(c) Replace
```python
    ) -> AgentCompletion:
        if self.persistence is None:
            return await self._initialize_core(
```
with
```python
    ) -> AgentCompletion:
        self._begin_delegation_run()
        if self.persistence is None:
            return await self._initialize_core(
```

- [ ] **Step 8: Run the tests, then the whole suite**

Run: `python -m pytest tests/test_subagent_delegate.py tests/test_subagent_delegate_async.py -q -p no:cacheprovider`
Expected: all pass.

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green.

- [ ] **Step 9: Commit**

```bash
git add agentx_dev/SubAgents.py agentx_dev/Runner tests/test_subagent_delegate.py tests/test_subagent_delegate_async.py
git commit -m "feat(subagents): the delegate tool, attach_delegation, and delegation= on runners"
```

---

### Task 4: `Supervisor` — `new_agent` plan steps, per-run registry, delegation for specialists

**Files:**
- Modify: `agentx_dev/Supervisor.py`
- Modify: `tests/subagent_helpers.py` (add `ScriptedRunner`)
- Test: `tests/test_subagent_plan.py`

**Interfaces:**
- Consumes: Task 2 (`SpawnPolicy`, `AgentSpec`, `parse_agent_spec`, `spec_from_legacy_spawn`, `SpecError`, `SpawnRefused`, `spawn_instruction`, `SpawnConfig`) and Task 3 (`attach_delegation`, `DELEGATE_TOOL_NAME`).
- Produces:
  - Plan steps may carry `"new_agent": {"name", "instructions", "tools"}`; after `_sanitize_plan` such a step has `agent == new_agent["name"]` plus the normalized `new_agent` dict
  - `SupervisorResult.spawned: List[Dict]`
  - class `_SpawnMixin` (base of `Supervisor`; Task 5 adds it to `AsyncSupervisor`) with `_spawn_policy()`, `_new_spawn_policy()`, `_spawn_spec(spec) -> (name | None, reason)`, `_drain_spawn_events()`, `_handle_spawn(step) -> (name | None, None)` (rewritten; legacy `__spawn__` entry point), class attribute `_SPAWN_ASYNC = False`
  - module helpers `_per_run_agents(sup)` and `_step_agent_name(item)`
  - `spawn` and `delegate_result` events on `Supervisor.stream`
  - persistent default: `Supervisor(persistence=P)` with no `spawn_config` uses `SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)`

- [ ] **Step 1: Add `ScriptedRunner` to the helpers**

Append to `tests/subagent_helpers.py`:

```python


class ScriptedRunner:
    """A fake specialist: each call returns the next ``(content, outcome)`` (default ``("ok", "done")``)."""

    def __init__(self, *results):
        from types import SimpleNamespace
        self._ns = SimpleNamespace
        self.results = list(results)
        self.calls = []
        self.tools = []
        self.persistence = None

    def Initialize(self, query, _budget=None):
        self.calls.append((query, _budget))
        content, outcome = self.results.pop(0) if self.results else ("ok", "done")
        return self._ns(content=content, outcome=outcome, output=None, progress=None)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_subagent_plan.py`:

```python
"""Supervisor: new_agent plan steps, reuse and collisions, the per-run registry, delegation."""

import pytest

from agentx_dev import AgentRunner, AgentType, Persistence, Supervisor
from agentx_dev.Supervisor import SpawnConfig, _sanitize_plan
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import (
    ScriptedRunner, new_agent_step, plan_json, router, step,
)

WEB = SpawnConfig(enabled=True, capabilities={"web"})


def sup(model, agents=None, cfg=WEB, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return Supervisor(model=model, agents=agents or {}, spawn_config=cfg, verbose=False, **kw)


def events_of(supervisor, task="task"):
    return list(supervisor.stream(task))


class TestSanitizer:
    def test_a_valid_new_agent_is_normalized_and_names_its_step_agent(self):
        sane, repairs = _sanitize_plan([new_agent_step("s1", " a ", " Be useful. ", ["web", "web"])])
        assert repairs == []
        assert sane[0]["agent"] == "a"
        assert sane[0]["new_agent"] == {"name": "a", "instructions": "Be useful.", "tools": ["web"]}

    def test_sanitizing_twice_is_stable(self):
        once, _ = _sanitize_plan([new_agent_step("s1", "a", "Be useful.", ["web"])])
        twice, repairs = _sanitize_plan(once)
        assert twice == once and repairs == []

    @pytest.mark.parametrize("bad", [
        {"name": "has space", "instructions": "i"},
        {"name": "a"},
        {"name": "a", "instructions": "i", "tools": "web"},
        "not an object",
    ])
    def test_a_malformed_new_agent_drops_the_step_and_reports_it(self, bad):
        plan = [{"id": "s1", "query": "q", "new_agent": bad}, step("s2", "w", deps=["s1"])]
        sane, repairs = _sanitize_plan(plan)
        assert [s["id"] for s in sane] == ["s2"]
        assert sane[0]["depends_on"] == []                      # the dependency on the dropped step is cleaned
        assert any("new_agent invalid" in r for r in repairs) and any("unknown dependency" in r for r in repairs)

    def test_agent_and_a_different_new_agent_name_together_keep_the_step_but_drop_the_definition(self):
        plan = [{"id": "s1", "agent": "worker", "query": "q",
                 "new_agent": {"name": "other", "instructions": "i"}}]
        sane, repairs = _sanitize_plan(plan)
        assert sane[0]["agent"] == "worker" and "new_agent" not in sane[0]
        assert any("both" in r for r in repairs)


class TestPlannerPrompt:
    def test_enabled_spawning_teaches_new_agent_with_the_menu(self):
        model = router(plans=[plan_json(step("s1", "w"))])
        sup(model, {"w": ("w", ScriptedRunner())}).run("task")
        prompt = model.planner_prompts()[0]
        assert "CREATING NEW SPECIALISTS" in prompt and "new_agent" in prompt
        assert "web:" in prompt and "files:" not in prompt and "at most 6" in prompt

    def test_disabled_spawning_leaves_the_prompt_alone(self):
        model = router(plans=[plan_json(step("s1", "w"))])
        Supervisor(model=model, agents={"w": ("w", ScriptedRunner())}, verbose=False).run("task")
        assert "new_agent" not in model.planner_prompts()[0]


class TestNewAgentSteps:
    def test_spawns_runs_the_step_and_discards_the_agent_afterwards(self):
        model = router(plans=[plan_json(new_agent_step(
            "s1", "pricing_analyst", "You compare prices. Return a table.", ["web"], query="compare"))],
            sub=lambda m: make_final("TABLE"))
        s = sup(model)
        result = s.run("task")
        assert result.subtasks[0].agent == "pricing_analyst" and result.subtasks[0].content == "TABLE"
        assert result.outcome == "done" and result.content == "Final."
        assert s.agents == {}                                   # nothing leaked onto the supervisor
        assert result.spawned == [{"name": "pricing_analyst", "origin": "plan", "tools": ["web"],
                                   "dropped": [], "outcome": "done", "chars": 5}]

    def test_the_stream_carries_the_spawn_event(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))],
                       sub=lambda m: make_final("x"))
        evs = [e for e in events_of(sup(model)) if e["type"] == "spawn"]
        assert evs == [{"type": "spawn", "name": "a", "origin": "plan", "tools": ["web"], "dropped": [],
                        "reused": None, "refused": None, "capabilities": ["web"], "rerouted_from": None}]

    def test_the_sub_agent_gets_the_planners_instructions_and_only_the_granted_tools(self):
        seen = []
        model = router(plans=[plan_json(new_agent_step("s1", "a", "You are the pricing analyst.", ["web", "code"]))],
                       sub=lambda m: seen.append(str(m[0]["content"])) or make_final("x"))
        sup(model).run("task")
        system = seen[0]
        assert "You are the pricing analyst." in system and "- web_search :" in system
        assert "- run_python :" not in system                   # the tool list, not the template's prose

    def test_a_later_step_reuses_the_agent_by_name_for_one_spawn(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "Be useful.", ["web"]),
            step("s2", "a", "again", deps=["s1"]))], sub=lambda m: make_final("x"))
        s = sup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1))
        result = s.run("task")
        assert [r.agent for r in result.subtasks] == ["a", "a"] and len(result.spawned) == 1

    def test_an_identical_redefinition_reuses_and_a_different_one_is_suffixed(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "one", ["web"]),
            new_agent_step("s2", "a", "one", ["web"]),
            new_agent_step("s3", "a", "two", ["web"]))], sub=lambda m: make_final("x"))
        result = sup(model).run("task")
        assert [r.agent for r in result.subtasks] == ["a", "a", "a_2"]
        assert [s["name"] for s in result.spawned] == ["a", "a_2"]

    def test_a_name_registered_by_the_developer_dispatches_that_specialist(self):
        worker = ScriptedRunner(("from the registered one", "done"))
        model = router(plans=[plan_json(new_agent_step("s1", "worker", "ignored", ["web"]))])
        s = sup(model, {"worker": ("w", worker)})
        result = s.run("task")
        assert result.subtasks[0].content == "from the registered one" and result.spawned == []
        evs = [e for e in events_of(sup(router(plans=[plan_json(new_agent_step("s1", "worker", "ignored"))]),
                                        {"worker": ("w", ScriptedRunner())})) if e["type"] == "spawn"]
        assert evs[0]["reused"] == "registered"

    def test_a_refused_spawn_fails_the_step_with_the_reason(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["code"]))])
        result = sup(model).run("task")
        assert result.subtasks[0].error == "spawn refused: no usable tools"
        assert result.outcome != "done"
        evs = [e for e in events_of(sup(router(plans=[plan_json(new_agent_step("s1", "a", "x", ["code"]))])))
               if e["type"] == "spawn"]
        assert evs[0]["refused"] == "no usable tools"

    def test_spawning_disabled_refuses_new_agent_steps(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x"))])
        result = Supervisor(model=model, agents={}, verbose=False, max_subtask_retries=0).run("task")
        assert result.subtasks[0].error == "spawn refused: spawning is disabled"

    def test_the_spawn_limit_applies_across_the_plan(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]),
                                        new_agent_step("s2", "b", "y", ["web"]))],
                       sub=lambda m: make_final("x"))
        result = sup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1)).run("task")
        assert result.subtasks[0].error is None
        assert result.subtasks[1].error == "spawn refused: spawn limit reached"

    def test_a_skipped_step_does_not_spend_a_spawn(self):
        model = router(plans=[plan_json(
            step("s1", "bad"), new_agent_step("s2", "a", "x", ["web"], deps=["s1"]))],
            sub=lambda m: make_final("x"))
        bad = ScriptedRunner(("nope", "stuck"))
        s = sup(model, {"bad": ("b", bad)})
        result = s.run("task")
        assert result.subtasks[1].skipped and result.spawned == []

    def test_a_malformed_definition_triggers_the_plan_repair_retry(self):
        bad = plan_json({"id": "s1", "query": "q", "new_agent": {"name": "a"}})
        good = plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))
        model = router(plans=[bad, good], sub=lambda m: make_final("x"))
        result = sup(model).run("task")
        prompts = model.planner_prompts()
        assert len(prompts) == 2 and "new_agent invalid" in prompts[1] and "must not be combined with agent" in prompts[1]
        assert result.subtasks[0].agent == "a" and result.subtasks[0].content == "x"


class TestLegacySpawnStep:
    def test_the_3_5_spawn_step_still_works_and_nothing_leaks(self, tmp_path):
        cfg = SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=[str(tmp_path)])
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "reads files",
             "capabilities": ["files"]},
            step("s1", "scout", "look"))], sub=lambda m: make_final("scouted"))
        s = sup(model, cfg=cfg)
        result = s.run("task")
        assert result.subtasks[0].agent == "__spawn__"
        assert result.subtasks[0].content.startswith("registered new specialist 'scout'")
        assert result.subtasks[1].agent == "scout" and result.subtasks[1].content == "scouted"
        assert "scout" not in s.agents

    def test_a_refused_legacy_spawn_is_reported(self):
        cfg = SpawnConfig(enabled=True, approver=lambda r: False)
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "d", "capabilities": ["web"]})])
        result = sup(model, cfg=cfg).run("task")
        assert result.subtasks[0].error == "spawn refused (see log for reason)"

    def test_handle_spawn_called_directly_registers_on_the_instance(self, tmp_path):
        s = Supervisor(model=router(), agents={}, verbose=False,
                       spawn_config=SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=[str(tmp_path)]))
        name, rewrite = s._handle_spawn({"name": "scout", "description": "reads", "capabilities": ["files"]})
        assert (name, rewrite) == ("scout", None) and "scout" in s.agents
        assert s._handle_spawn({"name": "", "description": "d"}) == (None, None)


class TestPerRunRegistry:
    def test_closing_the_stream_early_leaves_the_supervisor_and_its_runners_as_they_were(self):
        worker = AgentRunner(model=router(), agent=AgentType.ReAct, tools=[], verbose=False)
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]), step("s2", "worker", deps=["s1"]))],
                       sub=lambda m: make_final("x"), agent=lambda m: make_final("w"))
        worker.model = model
        s = sup(model, {"worker": ("w", worker)})
        before = dict(s.agents)
        gen = s.stream("task")
        for ev in gen:
            if ev["type"] == "spawn":
                break
        gen.close()
        assert s.agents == before and "a" not in s.agents
        assert "delegate" not in worker.registry.names

    def test_the_registry_is_restored_when_a_run_raises(self):
        class Boom(ScriptedRunner):
            def Initialize(self, query, _budget=None):
                raise KeyboardInterrupt

        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]), step("s2", "boom", deps=["s1"]))],
                       sub=lambda m: make_final("x"))
        s = sup(model, {"boom": ("b", Boom())})
        before = dict(s.agents)
        with pytest.raises(KeyboardInterrupt):
            s.run("task")
        assert s.agents == before

    def test_consecutive_runs_do_not_share_spawns(self):
        plans = [plan_json(new_agent_step("s1", "a", "x", ["web"]))] * 2
        model = router(plans=plans, sub=lambda m: make_final("x"))
        s = sup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1))
        assert s.run("one").spawned[0]["name"] == "a"
        second = s.run("two")
        assert second.subtasks[0].error is None and len(second.spawned) == 1


class TestSpecialistsDelegate:
    def test_specialists_have_delegate_during_the_run_and_lose_it_after(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        sup(model, {"worker": ("w", worker)}).run("task")
        assert seen == [True] and "delegate" not in worker.registry.names

    def test_a_specialist_delegates_and_the_record_and_events_show_it(self):
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "find X", "instructions": "You find X.",
                                                        "tools": ["web"]})
            return make_final("worker done")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: make_final("X is 42"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        s = sup(model, {"worker": ("w", worker)})
        evs = events_of(s)
        done = [e for e in evs if e["type"] == "completion"][0]["result"]
        assert done.subtasks[0].content == "worker done"
        assert done.spawned == [{"name": "delegate_1", "origin": "delegate", "tools": ["web"],
                                 "dropped": [], "outcome": "done", "chars": 7}]
        kinds = [e["type"] for e in evs]
        assert kinds.index("spawn") < kinds.index("delegate_result") < kinds.index("subtask_result")

    def test_no_spawn_config_means_no_delegate_and_no_prompt_text(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = Supervisor(model=model, agents={"worker": ("w", worker)}, verbose=False).run("task")
        assert seen == [False] and result.spawned == []


class TestPersistentDefault:
    def test_a_persistent_supervisor_gets_the_safe_ceiling_by_default(self):
        s = Supervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5))
        cfg = s.spawn_config
        assert cfg.enabled and cfg.capabilities == {"web", "files_read"} and cfg.effective_max_spawns == 6

    def test_an_explicit_config_always_wins(self):
        s = Supervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5),
                       spawn_config=SpawnConfig(enabled=False))
        assert not s.spawn_config.enabled

    def test_without_persistence_the_default_is_off(self):
        assert not Supervisor(model=router(), agents={}, verbose=False).spawn_config.enabled
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_subagent_plan.py -q -p no:cacheprovider`
Expected: FAIL (the sanitizer ignores `new_agent`, the prompt never mentions it, `SupervisorResult` has no `spawned`).

- [ ] **Step 4: Remove the old spawn prompt, the bridge, and the overlap machinery**

Run from the worktree root:
```bash
python - <<'PY'
import pathlib
p = pathlib.Path("agentx_dev/Supervisor.py")
s = p.read_text(encoding="utf-8")
crlf = "\r\n" in s
s = s.replace("\r\n", "\n")

# (a) delete SUPERVISOR_SPAWN_INSTRUCTION (taught the old __spawn__ format; spawn_instruction replaces it)
i = s.index('SUPERVISOR_SPAWN_INSTRUCTION = """')
j = s.index('SUPERVISOR_PLAN_PROMPT = """')
s = s[:i] + s[j:]

# (b) delete the Task 2 bridge (it ends at the blank lines before the next top-level construct)
i = s.index("def _build_spawned_agent(request, model, allowed_paths):")
k = s.index("\n\n\n", i)
s = s[:i].rstrip("\n") + "\n" + s[k:]

# (c) delete _CAP_TO_TOOLS, _find_existing_for_capabilities and the old _handle_spawn
i = s.index("    # Map from SpawnRequest capability keyword -> the concrete tool names")
j = s.index("    def _dispatch_with_retry(")
s = s[:i] + s[j:]

p.write_text(s.replace("\n", "\r\n") if crlf else s, encoding="utf-8", newline="")
print("old spawn code removed;", "CRLF" if crlf else "LF")
PY
```
Expected: `old spawn code removed; ...`. (`Supervisor` no longer has `_handle_spawn`; Step 6 adds the new methods.)

- [ ] **Step 5: Module-level helpers, imports, result field, sanitizer, repair note**

In `agentx_dev/Supervisor.py`:

(a) Replace
```python
import asyncio
import json
import sys
from dataclasses import dataclass, field
```
with
```python
import asyncio
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
```

(b) Replace the import block from Task 2
```python
from agentx_dev.SubAgents import (   # noqa: F401  (SpawnConfig/SpawnRequest are re-exported)
    AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpawnRequest, SpecError,
    _default_interactive_approver, attach_delegation, parse_agent_spec, spawn_instruction,
    spec_from_legacy_spawn,
)
```
with
```python
from agentx_dev.SubAgents import (   # noqa: F401  (SpawnConfig/SpawnRequest are re-exported)
    DELEGATE_TOOL_NAME, AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpawnRequest, SpecError,
    _default_interactive_approver, attach_delegation, parse_agent_spec, spawn_instruction,
    spec_from_legacy_spawn,
)
```

(c) Replace
```python
    # "done" | "partial" | "stuck" | "out_of_time" | "out_of_budget"
    outcome: str = "done"
```
with
```python
    # "done" | "partial" | "stuck" | "out_of_time" | "out_of_budget"
    outcome: str = "done"
    # Sub-agents created during the run (planner-time spawns and delegations):
    # name, origin, tools, dropped, outcome, chars. Empty when nothing was spawned.
    spawned: List[Dict[str, Any]] = Field(default_factory=list)
```

(d) Immediately after the `_normalize_agents` function (find `def _normalize_agents(`; add after its `return out`), add:
```python


@contextmanager
def _per_run_agents(sup):
    """Give one run its own copy of the specialist registry, so agents spawned during
    the run are discarded when it ends and ``sup.agents`` is exactly as it was."""
    base = sup.agents
    sup.agents = dict(base)
    try:
        yield
    finally:
        sup.agents = base


def _step_agent_name(item: dict) -> Optional[str]:
    """The agent a plan step runs on: its ``agent``, or the name its ``new_agent`` defines."""
    name = item.get("agent")
    if name:
        return name
    spec = item.get("new_agent")
    return spec.get("name") if isinstance(spec, dict) else None
```

(e) In `_plan_repair_note`, replace
```python
        "depend on itself or on a __spawn__ step, and the graph must "
        "contain no cycles."
    )
```
with
```python
        "depend on itself or on a __spawn__ step, and the graph must "
        "contain no cycles. A new_agent needs a name (letters, digits, "
        "_ or -, at most 40 characters), non-empty instructions, tools as "
        "a list of names, and must not be combined with agent."
    )
```

(f) In `_sanitize_plan`, add rule 7 (and mention it in the docstring list). Replace
```python
        seen.add(step["id"])

    ids = [s["id"] for s in plan]
```
with
```python
        seen.add(step["id"])

    # -- 7. new_agent validation (3.6) ----------------------------------------
    # A valid definition is normalized and also names the step's agent; an invalid one is
    # removed, and a step left with nothing to run is dropped (dependents lose the edge below).
    checked: List[dict] = []
    for step in plan:
        if "new_agent" not in step:
            checked.append(step)
            continue
        raw = step.pop("new_agent")
        problem = ""
        spec = None
        try:
            spec = parse_agent_spec(raw)
        except SpecError as e:
            problem = str(e)
        if spec is not None and step.get("agent") not in (None, "", spec.name):
            problem = f"a step cannot have both agent={step.get('agent')!r} and new_agent"
        if problem:
            repairs.append(f"step {step['id']!r}: new_agent invalid ({problem}) -- dropped")
            if not step.get("agent") or step.get("agent") == "__spawn__":
                continue
            checked.append(step)
            continue
        step["new_agent"] = {"name": spec.name, "instructions": spec.instructions, "tools": list(spec.tools)}
        step["agent"] = spec.name
        checked.append(step)
    plan = checked

    ids = [s["id"] for s in plan]
```

- [ ] **Step 6: The sub-agent methods, as a mixin both supervisors share**

Insert this class immediately before `class Supervisor:` in `agentx_dev/Supervisor.py`, and change the class line `class Supervisor:` to `class Supervisor(_SpawnMixin):`. (Task 5 gives `AsyncSupervisor` the same base, so the logic exists once.)

```python
class _SpawnMixin:
    """Sub-agent creation shared by ``Supervisor`` and ``AsyncSupervisor`` (3.6). Needs
    ``self.agents``, ``self.model``, ``self.persistence``, ``self.spawn_config``,
    ``self.verbose`` and ``self._spawn_run_policy``."""

    _SPAWN_ASYNC = False        # AsyncSupervisor builds async sub-agents

    def _new_spawn_policy(self) -> SpawnPolicy:
        return SpawnPolicy(self.spawn_config, self.model, persistence=self.persistence,
                           is_async=self._SPAWN_ASYNC, verbose=self.verbose)

    def _spawn_policy(self) -> SpawnPolicy:
        """The policy for the run in progress (created on demand outside a run)."""
        if self._spawn_run_policy is None:
            self._spawn_run_policy = self._new_spawn_policy()
        return self._spawn_run_policy

    def _drain_spawn_events(self):
        policy = self._spawn_run_policy
        if policy is not None:
            yield from policy.run.drain()

    def _overlapping(self, built) -> Optional[str]:
        """Name of a registered specialist that already has every tool ``built`` has (log only)."""
        have = {getattr(t, "name", None) for t in getattr(built.runner, "tools", [])}
        have -= {DELEGATE_TOOL_NAME, None}
        if not have:
            return None
        for n, (_d, runner) in self.agents.items():
            if n == built.spec.name:
                continue
            if have <= {getattr(t, "name", None) for t in getattr(runner, "tools", [])}:
                return n
        return None

    def _spawn_spec(self, spec: AgentSpec) -> Tuple[Optional[str], str]:
        """Build, reuse or refuse ``spec``. Returns ``(name, "")`` when an agent is available
        under ``name`` (a clashing name may have been suffixed), or ``(None, reason)``."""
        policy = self._spawn_policy()
        registered = {n for n in self.agents if n not in policy.run.built}
        try:
            built = policy.obtain(spec, registered=registered)
        except SpawnRefused as e:
            return None, e.reason
        name = built.spec.name
        if built.reused != "registered" and name not in self.agents:
            self.agents[name] = Specialist(description=built.description, runner=built.runner)
        if built.reused is None and self.verbose:
            twin = self._overlapping(built)
            if twin:
                print(f"{_C_PLAN}[supervisor.spawn] '{name}' has the same tools as the registered "
                      f"specialist '{twin}'; keeping both (different instructions){_C_RESET}")
        return name, ""

    def _handle_spawn(self, spawn_step: dict) -> Tuple[Optional[str], Optional[str]]:
        """Process a legacy ``__spawn__`` step. Returns ``(name, None)`` when the agent is
        available and ``(None, None)`` otherwise. The 3.5 reroute is gone: a spawn whose
        tools overlap an existing specialist is no longer refused."""
        try:
            spec = spec_from_legacy_spawn(spawn_step)
        except SpecError as e:
            if self.verbose:
                print(f"{_C_ERROR}[supervisor.spawn] malformed request: {e}{_C_RESET}")
            return None, None
        name, reason = self._spawn_spec(spec)
        if name is None and self.verbose:
            print(f"{_C_ERROR}[supervisor.spawn] refused: {reason}{_C_RESET}")
        return name, None
```

- [ ] **Step 7: Constructor, prompt, `_run_plan`, `stream`**

In `agentx_dev/Supervisor.py`, class `Supervisor`:

(a) Constructor docstring: replace
```python
                auto_spawn / approver knobs.
```
with
```python
                auto_spawn / approver knobs. (3.6) The planner can define a new
                specialist inline (``new_agent``: name, instructions, tools) and
                specialists can ``delegate`` to fresh sub-agents; tools are clipped
                to the config's ceiling (``tools`` / ``capabilities``). With
                ``persistence`` set and no ``spawn_config`` the default is
                ``SpawnConfig(enabled=True, capabilities={"web", "files_read"},
                max_spawns=6)``; pass ``SpawnConfig(enabled=False)`` to opt out.
```

(b) Constructor body: replace
```python
        self.spawn_config = spawn_config or SpawnConfig(enabled=False)
        self._spawns_this_run = 0
```
with
```python
        if spawn_config is None:
            # Persistent supervisors may create sub-agents out of the box, inside a safe
            # ceiling (web search plus read-only files). Without persistence the default is off.
            spawn_config = (
                SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)
                if persistence is not None else SpawnConfig(enabled=False)
            )
        self.spawn_config = spawn_config
        self._spawn_run_policy: Optional[SpawnPolicy] = None
```

(c) `_plan_once`: replace
```python
        if self.spawn_config.enabled:
            prompt = prompt + SUPERVISOR_SPAWN_INSTRUCTION
```
with
```python
        if self.spawn_config.enabled:
            prompt = prompt + spawn_instruction(self._spawn_policy())
```

(d) `_run_plan`: replace the whole legacy spawn block. Run:
```bash
python - <<'PY'
import pathlib
p = pathlib.Path("agentx_dev/Supervisor.py")
s = p.read_text(encoding="utf-8")
crlf = "\r\n" in s
s = s.replace("\r\n", "\n")

start = '            if agent_name == "__spawn__":\n                spawned_name, rewrite_from = self._handle_spawn(item)\n'
end = "            # 3.3 failure cascade: a FAILED direct dependency"
i, j = s.index(start), s.index(end)
new_block = '''            if agent_name == "__spawn__":
                spawned_name, _ = self._handle_spawn(item)
                yield from self._drain_spawn_events()
                label = item.get("name", "?")
                if spawned_name:
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {label}",
                        content=f"registered new specialist '{spawned_name}' with "
                                f"capabilities: {', '.join(item.get('capabilities', []))}",
                        step_id=step_id,
                    )
                else:
                    sub_result = SubtaskResult(
                        agent="__spawn__",
                        query=f"spawn: {label}",
                        content="",
                        error="spawn refused (see log for reason)",
                        step_id=step_id,
                    )
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                continue

'''
s = s[:i] + new_block + s[j:]

# new_agent steps: spawn just before dispatch (after the cascade and skip checks)
anchor = "            if agent_name in spawn_rewrites:\n                original = agent_name\n"
assert s.count(anchor) == 1
spawn_step = '''            if item.get("new_agent"):
                try:
                    spawned_name, reason = self._spawn_spec(parse_agent_spec(item["new_agent"]))
                except SpecError as e:
                    spawned_name, reason = None, str(e)
                yield from self._drain_spawn_events()
                if spawned_name is None:
                    sub_result = SubtaskResult(
                        agent=agent_name or "<none>", query=item.get("query", ""), content="",
                        error=f"spawn refused: {reason}", step_id=step_id, depends_on=step_deps,
                    )
                    if self.verbose:
                        print(f"{_C_ERROR}[supervisor.spawn] step {step_id}: {reason}{_C_RESET}")
                    subtask_results.append(sub_result)
                    results_by_id[step_id] = sub_result
                    yield {"type": "subtask_result", "result": sub_result,
                           "step": step_idx, "step_id": step_id}
                    continue
                agent_name = spawned_name

'''
s = s.replace(anchor, spawn_step + anchor)

# after a dispatch: close the sub-agent's record and flush spawn / delegate events
anchor2 = ("            sub_result.step_id = step_id\n"
           "            sub_result.depends_on = step_deps\n"
           "            subtask_results.append(sub_result)\n"
           "            results_by_id[step_id] = sub_result\n")
assert s.count(anchor2) == 1
s = s.replace(anchor2, anchor2 +
    "            policy = self._spawn_run_policy\n"
    "            if policy is not None and agent_name in policy.run.built:\n"
    "                policy.run.finish(agent_name, sub_result.outcome, len(sub_result.content))\n"
    "            yield from self._drain_spawn_events()\n")

p.write_text(s.replace("\n", "\r\n") if crlf else s, encoding="utf-8", newline="")
print("run_plan patched")
PY
```

(e) `stream`: replace
```python
          - {"type": "spawn",          "name": str, "capabilities": list}
```
with
```python
          - {"type": "spawn",          "name": str, "origin": "plan" | "delegate", "tools": list,
                                        "dropped": list, "reused": str | None, "refused": str | None,
                                        "capabilities": list, "rerouted_from": None}
          - {"type": "delegate_result", "name": str, "outcome": str, "chars": int}
```

(f) `stream` body: replace
```python
        self._spawns_this_run = 0
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
```
with
```python
        self._spawn_run_policy = self._new_spawn_policy()
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
```
and replace
```python
        spawn_rewrites: Dict[str, str] = {}
        budget_reason: Optional[str] = None
        with apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            yield from self._run_plan(plan, subtask_results, results_by_id, spawn_rewrites, budget)
```
with
```python
        spawn_rewrites: Dict[str, str] = {}
        budget_reason: Optional[str] = None
        with _per_run_agents(self), \
                attach_delegation([s.runner for s in self.agents.values()], self._spawn_policy()), \
                apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            yield from self._run_plan(plan, subtask_results, results_by_id, spawn_rewrites, budget)
```
and replace
```python
        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
            outcome=_supervisor_outcome(subtask_results, budget_reason),
        )
```
with
```python
        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=subtask_results, plan=plan,
            outcome=_supervisor_outcome(subtask_results, budget_reason),
            spawned=list(self._spawn_policy().run.records),
        )
```

- [ ] **Step 8: Run the tests, then the whole suite**

Run: `python -m pytest tests/test_subagent_plan.py -q -p no:cacheprovider`
Expected: all pass.

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green, including `tests/test_supervisor_persistent.py::TestSpawnedSpecialists` (the rewritten `_handle_spawn` registers `scout` on `sup.agents` and the spawned runner inherits `P`).

- [ ] **Step 9: Commit**

```bash
git add agentx_dev/Supervisor.py tests/subagent_helpers.py tests/test_subagent_plan.py
git commit -m "feat(supervisor): plan steps can define new agents; specialists can delegate; spawns no longer leak"
```

---

### Task 5: `AsyncSupervisor` — the same sub-agent behavior, with parallel spawned steps

**Files:**
- Modify: `agentx_dev/Supervisor.py` (`AsyncSupervisor`)
- Test: `tests/test_subagent_plan_async.py`

**Interfaces:**
- Consumes: Task 4's `_SpawnMixin`, `_per_run_agents`, `_step_agent_name`, `_sanitize_plan` rule 7, `SupervisorResult.spawned`; Task 3's `attach_delegation`.
- Produces: `AsyncSupervisor(..., spawn_config: Optional[SpawnConfig] = None)` (last keyword parameter); `AsyncSupervisor` inherits `_SpawnMixin` with `_SPAWN_ASYNC = True`, so planner-time spawns are `AsyncAgentRunner`s that run in the same parallel batches as other steps; the same `spawn` / `delegate_result` events; `SupervisorResult.spawned`; the persistent default; the per-run registry.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_plan_async.py`:

```python
"""AsyncSupervisor: new_agent steps, reuse, the per-run registry, delegation, parallel spawned steps."""

import asyncio

import pytest

from agentx_dev import AgentType, AsyncAgentRunner, AsyncSupervisor, Persistence
from agentx_dev.Supervisor import SpawnConfig
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import (
    SPAWNED_MARK, ScriptedRunner, new_agent_step, plan_json, router, step,
)

WEB = SpawnConfig(enabled=True, capabilities={"web"})


class AsyncScripted(ScriptedRunner):
    async def Initialize(self, query, _budget=None):
        return ScriptedRunner.Initialize(self, query, _budget)


def asup(model, agents=None, cfg=WEB, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return AsyncSupervisor(model=model, agents=agents or {}, spawn_config=cfg, verbose=False, **kw)


def run(s, task="task"):
    return asyncio.run(s.run(task))


async def _collect(s, task="task", stop=None):
    out = []
    gen = s.astream(task)
    try:
        async for ev in gen:
            out.append(ev)
            if stop and stop(ev):
                break
    finally:
        await gen.aclose()
    return out


def events_of(s, task="task", stop=None):
    return asyncio.run(_collect(s, task, stop))


class Barrier:
    """Both spawned steps must be in flight at the same moment, or the wait times out."""

    def __init__(self, n):
        self.n, self.seen, self.evt = n, 0, asyncio.Event()

    async def hit(self):
        self.seen += 1
        if self.seen >= self.n:
            self.evt.set()
        await asyncio.wait_for(self.evt.wait(), 2)


class TestAsyncNewAgentSteps:
    def test_spawns_an_async_runner_runs_the_step_and_discards_it(self):
        model = router(plans=[plan_json(new_agent_step(
            "s1", "analyst", "You compare prices.", ["web"], query="compare"))],
            sub=lambda m: make_final("TABLE"))
        s = asup(model)
        result = run(s)
        assert result.subtasks[0].agent == "analyst" and result.subtasks[0].content == "TABLE"
        assert result.outcome == "done" and s.agents == {}
        assert result.spawned == [{"name": "analyst", "origin": "plan", "tools": ["web"],
                                   "dropped": [], "outcome": "done", "chars": 5}]

    def test_the_planned_agent_is_an_async_runner_and_gets_the_instructions(self):
        seen = []
        model = router(plans=[plan_json(new_agent_step("s1", "a", "You are the analyst.", ["web", "code"]))],
                       sub=lambda m: seen.append(str(m[0]["content"])) or make_final("x"))
        s = asup(model)
        events_of(s)
        assert "You are the analyst." in seen[0] and "- web_search :" in seen[0]
        assert "- run_python :" not in seen[0]
        assert isinstance(s._spawn_policy().run.built["a"].runner, AsyncAgentRunner)

    def test_the_prompt_teaches_new_agent_only_when_enabled(self):
        on = router(plans=[plan_json(step("s1", "w"))])
        run(asup(on, {"w": ("w", AsyncScripted())}))
        assert "CREATING NEW SPECIALISTS" in on.planner_prompts()[0]
        off = router(plans=[plan_json(step("s1", "w"))])
        run(AsyncSupervisor(model=off, agents={"w": ("w", AsyncScripted())}, verbose=False))
        assert "new_agent" not in off.planner_prompts()[0]

    def test_reuse_by_name_collision_suffix_and_registered_names(self):
        model = router(plans=[plan_json(
            new_agent_step("s1", "a", "one", ["web"]),
            new_agent_step("s2", "a", "one", ["web"]),
            new_agent_step("s3", "a", "two", ["web"]),
            new_agent_step("s4", "worker", "ignored", ["web"]))], sub=lambda m: make_final("x"))
        worker = AsyncScripted(("registered answer", "done"))
        s = asup(model, {"worker": ("w", worker)})
        result = run(s)
        assert sorted(r.agent for r in result.subtasks) == ["a", "a", "a_2", "worker"]
        assert [x["name"] for x in result.spawned] == ["a", "a_2"]
        reused = [e["reused"] for e in events_of(asup(
            router(plans=[plan_json(new_agent_step("s1", "a", "one", ["web"]),
                                    new_agent_step("s2", "a", "one", ["web"]))], sub=lambda m: make_final("x"))))
            if e["type"] == "spawn"]
        assert reused == [None, "spawned"]

    def test_refusals_fail_the_step_with_the_reason(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["code"]))])
        assert run(asup(model)).subtasks[0].error == "spawn refused: no usable tools"
        off = router(plans=[plan_json(new_agent_step("s1", "a", "x"))])
        r = run(AsyncSupervisor(model=off, agents={}, verbose=False, max_subtask_retries=0))
        assert r.subtasks[0].error == "spawn refused: spawning is disabled"
        lim = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]),
                                      new_agent_step("s2", "b", "y", ["web"]))], sub=lambda m: make_final("x"))
        r = run(asup(lim, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1), sequential=True))
        assert r.subtasks[0].error is None and r.subtasks[1].error == "spawn refused: spawn limit reached"

    def test_a_malformed_definition_triggers_the_repair_retry(self):
        bad = plan_json({"id": "s1", "query": "q", "new_agent": {"name": "a"}})
        good = plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))
        model = router(plans=[bad, good], sub=lambda m: make_final("x"))
        result = run(asup(model))
        assert len(model.planner_prompts()) == 2 and "new_agent invalid" in model.planner_prompts()[1]
        assert result.subtasks[0].agent == "a"

    def test_the_stream_carries_the_spawn_event(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "Be useful.", ["web"]))],
                       sub=lambda m: make_final("x"))
        evs = [e for e in events_of(asup(model)) if e["type"] == "spawn"]
        assert evs == [{"type": "spawn", "name": "a", "origin": "plan", "tools": ["web"], "dropped": [],
                        "reused": None, "refused": None, "capabilities": ["web"], "rerouted_from": None}]

    def test_a_skipped_step_does_not_spend_a_spawn(self):
        model = router(plans=[plan_json(step("s1", "bad"),
                                        new_agent_step("s2", "a", "x", ["web"], deps=["s1"]))],
                       sub=lambda m: make_final("x"))
        result = run(asup(model, {"bad": ("b", AsyncScripted(("nope", "stuck")))}))
        assert result.subtasks[1].skipped and result.spawned == []


class TestAsyncLegacySpawn:
    def test_the_3_5_spawn_step_works_in_async_too(self, tmp_path):
        cfg = SpawnConfig(enabled=True, auto_spawn=True, allowed_paths=[str(tmp_path)])
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "reads files",
             "capabilities": ["files"]},
            step("s1", "scout", "look"))], sub=lambda m: make_final("scouted"))
        s = asup(model, cfg=cfg, sequential=True)
        result = run(s)
        assert result.subtasks[0].agent == "__spawn__" and result.subtasks[0].content.startswith("registered new specialist 'scout'")
        assert result.subtasks[1].agent == "scout" and result.subtasks[1].content == "scouted"
        assert "scout" not in s.agents

    def test_a_refused_legacy_spawn_is_reported(self):
        cfg = SpawnConfig(enabled=True, approver=lambda r: False)
        model = router(plans=[plan_json(
            {"id": "sp", "agent": "__spawn__", "name": "scout", "description": "d", "capabilities": ["web"]})])
        assert run(asup(model, cfg=cfg)).subtasks[0].error == "spawn refused (see log for reason)"


class TestAsyncPerRunRegistry:
    def test_closing_the_stream_early_restores_everything(self):
        worker = AsyncAgentRunner(model=router(), agent=AgentType.ReAct, tools=[], verbose=False)
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]), step("s2", "worker", deps=["s1"]))],
                       sub=lambda m: make_final("x"), agent=lambda m: make_final("w"))
        worker.model = model
        s = asup(model, {"worker": ("w", worker)})
        before = dict(s.agents)
        events_of(s, stop=lambda ev: ev["type"] == "spawn")
        assert s.agents == before and "a" not in s.agents
        assert "delegate" not in worker.registry.names

    def test_consecutive_runs_do_not_share_spawns(self):
        model = router(plans=[plan_json(new_agent_step("s1", "a", "x", ["web"]))] * 2,
                       sub=lambda m: make_final("x"))
        s = asup(model, cfg=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=1))
        assert run(s, "one").spawned[0]["name"] == "a"
        second = run(s, "two")
        assert second.subtasks[0].error is None and len(second.spawned) == 1


class TestParallelSpawnedSteps:
    def test_two_independent_new_agent_steps_run_at_the_same_time(self):
        barrier = Barrier(2)
        model = router(plans=[plan_json(new_agent_step("s1", "a", "one", ["web"]),
                                        new_agent_step("s2", "b", "two", ["web"]))],
                       sub=lambda m: make_final("x"))
        original = model.async_initialize

        async def gated(messages):
            if SPAWNED_MARK in str(messages[0]["content"]):
                await barrier.hit()
            return await original(messages)

        model.async_initialize = gated
        result = run(asup(model))
        assert barrier.seen == 2 and result.outcome == "done"
        assert sorted(r.agent for r in result.subtasks) == ["a", "b"]


class TestAsyncSpecialistsDelegate:
    def test_specialists_have_delegate_during_the_run_and_lose_it_after(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        run(asup(model, {"worker": ("w", worker)}))
        assert seen == [True] and "delegate" not in worker.registry.names

    def test_a_specialist_delegates_and_the_record_and_events_show_it(self):
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "find X", "instructions": "You find X.",
                                                        "tools": ["web"]})
            return make_final("worker done")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: make_final("X is 42"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        evs = events_of(asup(model, {"worker": ("w", worker)}))
        done = [e for e in evs if e["type"] == "completion"][0]["result"]
        assert done.subtasks[0].content == "worker done"
        assert done.spawned == [{"name": "delegate_1", "origin": "delegate", "tools": ["web"],
                                 "dropped": [], "outcome": "done", "chars": 7}]
        kinds = [e["type"] for e in evs]
        assert kinds.index("spawn") < kinds.index("delegate_result") < kinds.index("subtask_result")

    def test_no_spawn_config_means_no_delegate(self):
        seen = []
        model = router(plans=[plan_json(step("s1", "worker"))],
                       agent=lambda m: seen.append("delegate" in str(m[0]["content"])) or make_final("ok"))
        worker = AsyncAgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = run(AsyncSupervisor(model=model, agents={"worker": ("w", worker)}, verbose=False))
        assert seen == [False] and result.spawned == []


class TestAsyncPersistentDefault:
    def test_persistent_default_and_explicit_override(self):
        s = AsyncSupervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5))
        cfg = s.spawn_config
        assert cfg.enabled and cfg.capabilities == {"web", "files_read"} and cfg.effective_max_spawns == 6
        off = AsyncSupervisor(model=router(), agents={}, verbose=False, persistence=Persistence(max_minutes=5),
                              spawn_config=SpawnConfig(enabled=False))
        assert not off.spawn_config.enabled
        assert not AsyncSupervisor(model=router(), agents={}, verbose=False).spawn_config.enabled
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_subagent_plan_async.py -q -p no:cacheprovider`
Expected: FAIL (`AsyncSupervisor` has no `spawn_config` parameter).

- [ ] **Step 3: Constructor, mixin, prompt**

In `agentx_dev/Supervisor.py`, class `AsyncSupervisor`:

(a) Change the class line `class AsyncSupervisor:` to `class AsyncSupervisor(_SpawnMixin):` and add `_SPAWN_ASYNC = True` as the first line of the class body, immediately before `def __init__(`. To do that, replace
```python
    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, Union[AgentRunner, AsyncAgentRunner]]],
```
with
```python
    _SPAWN_ASYNC = True

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, Union[AgentRunner, AsyncAgentRunner]]],
```

(b) Signature: replace
```python
        max_parallel: Optional[int] = None,
        max_plan_retries: int = 1,
        persistence: Optional[Persistence] = None,
    ):
```
with
```python
        max_parallel: Optional[int] = None,
        max_plan_retries: int = 1,
        persistence: Optional[Persistence] = None,
        spawn_config: Optional[SpawnConfig] = None,
    ):
```

(c) Docstring: replace
```python
            persistence: (3.5) Opt-in ``Persistence(...)``; see
                :class:`Supervisor`. Recovery plans cannot spawn new
                specialists here (the async supervisor has no
                ``spawn_config``).
```
with
```python
            persistence: (3.5) Opt-in ``Persistence(...)``; see
                :class:`Supervisor`.
            spawn_config: (3.6) Lets the planner define new specialists inline
                (``new_agent``) and specialists ``delegate`` to fresh sub-agents,
                clipped to the config's ceiling. Spawned steps run in the same
                parallel batches as any other step. Same defaults as
                :class:`Supervisor` (on, with a safe ceiling, when ``persistence``
                is set and no config is given).
```

(d) Body: replace
```python
        self.max_plan_retries = max(0, int(max_plan_retries))
        self.persistence = persistence
        # Persistent runs only: waits out transient planner/synthesis errors until the deadline.
        self._patience: Optional[PersistentRun] = None

    def _evaluate_success(self, result: SubtaskResult) -> tuple:
        """See ``Supervisor._evaluate_success``."""
```
with
```python
        self.max_plan_retries = max(0, int(max_plan_retries))
        self.persistence = persistence
        if spawn_config is None:
            # Same default as Supervisor: persistent runs may create sub-agents inside a safe ceiling.
            spawn_config = (
                SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)
                if persistence is not None else SpawnConfig(enabled=False)
            )
        self.spawn_config = spawn_config
        self._spawn_run_policy: Optional[SpawnPolicy] = None
        # Persistent runs only: waits out transient planner/synthesis errors until the deadline.
        self._patience: Optional[PersistentRun] = None

    def _evaluate_success(self, result: SubtaskResult) -> tuple:
        """See ``Supervisor._evaluate_success``."""
```

(e) `_plan_once`: replace
```python
        if repair_note:
            prompt = prompt + repair_note
        messages = [{"role": "user", "content": prompt}]
        response = await self._call_model(messages)
```
with
```python
        if self.spawn_config.enabled:
            prompt = prompt + spawn_instruction(self._spawn_policy())
        if repair_note:
            prompt = prompt + repair_note
        messages = [{"role": "user", "content": prompt}]
        response = await self._call_model(messages)
```
and replace
```python
        plan = parsed.get("plan", []) or []
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and item.get("agent") in self.agents
            and item.get("query")
        ]
        return filtered[: self.max_subtasks]
```
with
```python
        plan = parsed.get("plan", []) or []
        # A step may name a registered agent, define one inline (new_agent), or use one an
        # earlier step defines; a legacy __spawn__ step carries no query.
        defined = {s["new_agent"].get("name") for s in plan
                   if isinstance(s, dict) and isinstance(s.get("new_agent"), dict)}
        defined |= {s.get("name") for s in plan
                    if isinstance(s, dict) and s.get("agent") == "__spawn__"}
        filtered = [
            item for item in plan
            if isinstance(item, dict)
            and (
                item.get("agent") == "__spawn__"
                or (item.get("query") and (
                    isinstance(item.get("new_agent"), dict)
                    or item.get("agent") in self.agents
                    or item.get("agent") in defined
                ))
            )
        ]
        return filtered[: self.max_subtasks]
```

- [ ] **Step 4: `_run_plan`**

In `AsyncSupervisor._run_plan`:

(a) Replace
```python
        for step_idx, item in enumerate(plan):
            yield {"type": "dispatch",
                   "agent": item.get("agent"), "query": item.get("query", ""),
                   "step": step_idx}
```
with
```python
        for step_idx, item in enumerate(plan):
            if item.get("agent") == "__spawn__":
                continue                      # bookkeeping, not a dispatch
            yield {"type": "dispatch",
                   "agent": _step_agent_name(item), "query": item.get("query", ""),
                   "step": step_idx}
```

(b) Replace
```python
        done_ids: set = set()
        launched: set = set()
        running: Dict[asyncio.Task, int] = {}
```
with
```python
        done_ids: set = set()
        launched: set = set()
        running: Dict[asyncio.Task, int] = {}
        launched_as: Dict[int, str] = {}      # step index -> the agent it was launched on
```

(c) Replace
```python
                    if self.max_parallel is not None and len(running) >= self.max_parallel:
                        break
                    launched.add(i)
                    dep_results = [
                        results_by_id[d] for d in deps_of[i] if d in results_by_id
                    ]
                    task = asyncio.create_task(self._run_subtask(
                        plan[i]["agent"], plan[i]["query"],
                        prior_results=dep_results if dep_results else None,
                        budget=budget,
                    ))
                    running[task] = i
                    progressed = True
```
with
```python
                    if self.max_parallel is not None and len(running) >= self.max_parallel:
                        break
                    launched.add(i)
                    if plan[i].get("agent") == "__spawn__":
                        spawned_name, _ = self._handle_spawn(plan[i])
                        for ev in self._drain_spawn_events():
                            yield ev
                        _record(i, SubtaskResult(
                            agent="__spawn__", query=f"spawn: {plan[i].get('name', '?')}",
                            content=(f"registered new specialist '{spawned_name}' with capabilities: "
                                     f"{', '.join(plan[i].get('capabilities', []))}") if spawned_name else "",
                            error=None if spawned_name else "spawn refused (see log for reason)",
                        ))
                        progressed = True
                        continue
                    launch_agent = plan[i].get("agent")
                    if plan[i].get("new_agent"):
                        try:
                            spawned_name, reason = self._spawn_spec(parse_agent_spec(plan[i]["new_agent"]))
                        except SpecError as e:
                            spawned_name, reason = None, str(e)
                        for ev in self._drain_spawn_events():
                            yield ev
                        if spawned_name is None:
                            r = _record(i, SubtaskResult(
                                agent=_step_agent_name(plan[i]) or "<none>", query=plan[i].get("query", ""),
                                content="", error=f"spawn refused: {reason}",
                            ))
                            yield {"type": "subtask_result", "result": r,
                                   "step": i, "step_id": step_ids[i]}
                            progressed = True
                            continue
                        launch_agent = spawned_name
                    dep_results = [
                        results_by_id[d] for d in deps_of[i] if d in results_by_id
                    ]
                    task = asyncio.create_task(self._run_subtask(
                        launch_agent, plan[i]["query"],
                        prior_results=dep_results if dep_results else None,
                        budget=budget,
                    ))
                    running[task] = i
                    launched_as[i] = launch_agent
                    progressed = True
```

(d) Replace
```python
                for t in done:
                    i = running.pop(t)
                    r = _record(i, t.result())
                    yield {"type": "subtask_result", "result": r,
                           "step": i, "step_id": step_ids[i]}
```
with
```python
                for t in done:
                    i = running.pop(t)
                    r = _record(i, t.result())
                    policy = self._spawn_run_policy
                    if policy is not None and launched_as.get(i) in policy.run.built:
                        policy.run.finish(launched_as[i], r.outcome, len(r.content))
                    for ev in self._drain_spawn_events():
                        yield ev
                    yield {"type": "subtask_result", "result": r,
                           "step": i, "step_id": step_ids[i]}
```

- [ ] **Step 5: `astream`**

In `AsyncSupervisor.astream`:

(a) Replace
```python
        UI sees whichever completes first, not the plan order.
        """
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
```
with
```python
        UI sees whichever completes first, not the plan order.
        """
        self._spawn_run_policy = self._new_spawn_policy()
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
```

(b) Replace
```python
        budget_reason: Optional[str] = None
        with apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            plan_run = self._run_plan(plan, subtask_results, results_by_id, budget)
```
with
```python
        budget_reason: Optional[str] = None
        with _per_run_agents(self), \
                attach_delegation([s.runner for s in self.agents.values()], self._spawn_policy()), \
                apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            plan_run = self._run_plan(plan, subtask_results, results_by_id, budget)
```

(c) Replace
```python
            subtasks=list(subtask_results), plan=plan,
            outcome=_supervisor_outcome(subtask_results, budget_reason),
        )
```
with
```python
            subtasks=list(subtask_results), plan=plan,
            outcome=_supervisor_outcome(subtask_results, budget_reason),
            spawned=list(self._spawn_policy().run.records),
        )
```

- [ ] **Step 6: Run the tests, then the whole suite**

Run: `python -m pytest tests/test_subagent_plan_async.py -q -p no:cacheprovider`
Expected: all pass.

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green.

- [ ] **Step 7: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_subagent_plan_async.py
git commit -m "feat(supervisor): AsyncSupervisor creates and delegates to sub-agents, in parallel"
```

---

### Task 6: Sub-agents inside persistent mode

**Files:**
- Test: `tests/test_subagent_persistent.py`
- Modify only if a test below exposes a defect (fix in the module the test points at; do not weaken the test)

**Interfaces:**
- Consumes: everything from Tasks 1-5, plus the 3.5.0 persistent Supervisor (`replaces`, `_resolve_replaced`, `RunBudget`).
- Produces: pinned behavior: recovery rounds can define a replacement agent; spawned and delegated agents inherit persistence and the shared deadline; the default ceiling clips code execution.

- [ ] **Step 1: Write the tests**

Create `tests/test_subagent_persistent.py`:

```python
"""Sub-agents under persistent mode: recovery by spawning, inherited persistence, shared deadline."""

import asyncio

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, AsyncSupervisor, Persistence, Supervisor
from agentx_dev.Supervisor import SpawnConfig
from tests.conftest import make_final, make_react_response
from tests.subagent_helpers import ScriptedRunner, plan_json, router, step

P = Persistence(max_minutes=5)


def fixer_step(tools=("web",)):
    return {"id": "fix", "query": "another way", "replaces": ["s1"],
            "new_agent": {"name": "fixer", "instructions": "Do it differently. Return the data.",
                          "tools": list(tools)}}


class AsyncStuck(ScriptedRunner):
    async def Initialize(self, query, _budget=None):
        return ScriptedRunner.Initialize(self, query, _budget)


def sync_sup(model, agents, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return Supervisor(model=model, agents=agents, verbose=False, persistence=P, **kw)


def async_sup(model, agents, **kw):
    kw.setdefault("max_subtask_retries", 0)
    return AsyncSupervisor(model=model, agents=agents, verbose=False, persistence=P, **kw)


class TestRecoveryBySpawning:
    def test_a_recovery_round_replaces_a_stuck_step_with_a_new_agent(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step())],
                       sub=lambda m: make_final("fixed"), synth="All done.")
        worker = ScriptedRunner(("gave up", "stuck"))
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        assert result.outcome == "done" and result.content == "All done."
        assert [(r.step_id, r.superseded, r.outcome) for r in result.subtasks] == [
            ("s1", True, "stuck"), ("fix", False, "done")]
        assert result.subtasks[1].agent == "fixer" and result.subtasks[1].content == "fixed"
        assert [(x["name"], x["origin"]) for x in result.spawned] == [("fixer", "plan")]
        recovery_prompt = model.planner_prompts()[1]
        assert "RECOVERY ROUND 2" in recovery_prompt and "CREATING NEW SPECIALISTS" in recovery_prompt

    def test_the_same_recovery_in_async(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step())],
                       sub=lambda m: make_final("fixed"), synth="All done.")
        worker = AsyncStuck(("gave up", "stuck"))
        result = asyncio.run(async_sup(model, {"worker": ("w", worker)}).run("task"))
        assert result.outcome == "done"
        assert [(r.step_id, r.superseded) for r in result.subtasks] == [("s1", True), ("fix", False)]
        assert result.subtasks[1].agent == "fixer" and [x["name"] for x in result.spawned] == ["fixer"]

    def test_the_default_ceiling_clips_code_execution(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step(tools=("code",)))],
                       sub=lambda m: make_final("fixed"))
        worker = ScriptedRunner(("gave up", "stuck"))
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        fix = [r for r in result.subtasks if r.step_id == "fix"][0]
        assert fix.error == "spawn refused: no usable tools"
        assert result.outcome != "done"                       # s1 stays unresolved, nothing replaced it

    def test_an_explicit_ceiling_can_allow_more(self):
        model = router(plans=[plan_json(step("s1", "worker")), plan_json(fixer_step(tools=("code",)))],
                       sub=lambda m: make_final("fixed"))
        worker = ScriptedRunner(("gave up", "stuck"))
        cfg = SpawnConfig(enabled=True, capabilities={"web", "code"}, allowed_paths=["./workspace"])
        result = sync_sup(model, {"worker": ("w", worker)}, spawn_config=cfg).run("task")
        assert result.outcome == "done" and result.subtasks[-1].agent == "fixer"


class TestInheritance:
    def test_spawned_agents_carry_persistence_and_run_without_the_tool_cache(self):
        model = router(plans=[plan_json({"id": "s1", "query": "q",
                                         "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}})],
                       sub=lambda m: make_final("x"))
        s = sync_sup(model, {})
        s.run("task")
        runner = s._spawn_policy().run.built["a"].runner
        assert runner.persistence is P and runner.max_iterations == P.max_turns
        assert getattr(runner.registry, "cache", None) is None

    def test_async_spawned_agents_carry_persistence_too(self):
        model = router(plans=[plan_json({"id": "s1", "query": "q",
                                         "new_agent": {"name": "a", "instructions": "Be useful.", "tools": ["web"]}})],
                       sub=lambda m: make_final("x"))
        s = async_sup(model, {})
        asyncio.run(s.run("task"))
        runner = s._spawn_policy().run.built["a"].runner
        assert isinstance(runner, AsyncAgentRunner) and runner.persistence is P


class TestSharedDeadline:
    def test_a_delegated_sub_agent_gets_the_deadline_the_supervisor_gave_its_specialist(self, monkeypatch):
        budgets = []
        original = AgentRunner.Initialize

        def spy(self, user_input, *a, **kw):
            budgets.append((self, kw.get("_budget")))
            return original(self, user_input, *a, **kw)

        monkeypatch.setattr(AgentRunner, "Initialize", spy)
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "find X", "tools": ["web"]})
            return make_final("worker done")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: make_final("X is 42"))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        s = sync_sup(model, {"worker": ("w", worker)})
        result = s.run("task")
        assert result.outcome == "done" and result.spawned[0]["origin"] == "delegate"
        by_runner = {id(r): b for r, b in budgets}
        worker_budget = by_runner[id(worker)]
        sub_runner = s._spawn_policy().run.built["delegate_1"].runner
        sub_budget = by_runner[id(sub_runner)]
        assert worker_budget is not None and sub_budget is not None
        assert sub_budget.deadline == worker_budget.deadline

    def test_a_stuck_delegation_does_not_end_the_specialists_run_under_persistence(self):
        turns = []

        def worker_script(messages):
            turns.append(1)
            if len(turns) == 1:
                return make_react_response("delegate", {"task": "t", "tools": ["web"]})
            return make_final("worker carried on")

        model = router(plans=[plan_json(step("s1", "worker"))], agent=worker_script,
                       sub=lambda m: (_ for _ in ()).throw(RuntimeError("provider down for good")))
        worker = AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False)
        result = sync_sup(model, {"worker": ("w", worker)}).run("task")
        assert result.subtasks[0].content == "worker carried on" and result.outcome == "done"
```

- [ ] **Step 2: Run the tests**

Run: `python -m pytest tests/test_subagent_persistent.py -q -p no:cacheprovider`
Expected: all pass. A failure here points at a real defect in Tasks 1-5, not at the test: fix the code and keep the test.

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_subagent_persistent.py
git commit -m "test(subagents): recovery by spawning, inherited persistence, shared deadline"
```

---

### Task 7: Documentation, cookbook, and the 3.6.0 version

**Files:**
- Create: `docs/guides/sub-agents.md`
- Modify: `host/build_data.py`, `host/app.js`, `README.md`, `docs/cookbook/patterns.md`, `docs/cookbook/faq.md`, `docs/cookbook/troubleshooting.md`, `docs/guides/upgrading.md`, `docs/reference/api-summary.md`, `docs/advanced/supervisor.md`, `docs/concepts/agents.md`, `CHANGELOG.md`, `pyproject.toml`, `host/data.js` (regenerated)

**Interfaces:**
- Consumes: the finished behavior of Tasks 1-6.
- Produces: user-facing documentation and version 3.6.0.

Every code claim in these docs must match the implemented API. The names used: `SpawnConfig(enabled, capabilities, tools, allowed_paths, max_spawns, max_depth, approver, auto_spawn, auto_spawn_allowed_caps)`, `AgentSpec`, `AgentRunner(..., delegation=SpawnConfig(...))`, `runner.spawned`, `SupervisorResult.spawned`, plan field `new_agent`, tool `delegate`, events `spawn` and `delegate_result`.

- [ ] **Step 1: Create `docs/guides/sub-agents.md`**

````markdown
# Sub-agents

A Supervisor can create its own helpers, the way Claude Code launches a sub-agent for a side job. There are two ways, and both go through one set of limits that you set once.

- **The planner defines a helper inline.** When no specialist you registered fits a sub-task, the planner writes a new one into the plan: a name, its own instructions, and the tools it needs. The Supervisor builds it, runs the step on it, and discards it when the run ends.
- **A specialist hands off part of its own work.** While working, a specialist can call a `delegate` tool. A fresh agent does the side job in a clean context and a short summary comes back, so the specialist's own context stays small.

Nothing is on by default, except in persistent mode (below).

## Quick start

```python
from agentx_dev import Supervisor, SpawnConfig

supervisor = Supervisor(
    model=model,
    agents={"explorer": ("Reads the codebase", explorer)},   # your specialists
    spawn_config=SpawnConfig(
        enabled=True,
        capabilities={"web", "files_read"},   # the ceiling: what a spawned agent may use
        allowed_paths=["./workspace"],        # its file sandbox
        max_spawns=6,                         # sub-agents per run, plans and delegations together
    ),
)

result = supervisor.run("Compare the pricing pages of our three competitors")
for sub in result.spawned:
    print(sub["name"], sub["origin"], sub["tools"], sub["outcome"])
```

`AsyncSupervisor` takes the same `spawn_config=`. Spawned steps run in the same parallel batches as every other step.

## What the planner writes

A plan step can carry its own agent instead of naming one:

```json
{"id": "s2", "query": "Compare the three pricing pages",
 "new_agent": {"name": "pricing_analyst",
               "instructions": "You compare SaaS pricing. Return a table: plan, price, limits, source URL.",
               "tools": ["web_search", "web_fetch"]}}
```

- `instructions` are free-form: the planner tells the helper who it is and what to return. The framework always adds its own rule: return the real data and never invent it.
- `tools` name tools the ceiling allows. Anything else is dropped and the planner is told what was dropped.
- Later steps reuse the helper by name (`{"agent": "pricing_analyst", ...}`) without defining it again. Repeating an identical definition also reuses it for free. A different definition under the same name gets a suffix (`pricing_analyst_2`).
- A name that matches a specialist you registered runs that specialist and ignores the definition.
- A helper exists for one run. When the run ends, your supervisor's registry is exactly as it was.

## The ceiling

The planner's plan is model output, which can be steered by the user's text. So you decide the most a helper may ever do, once, and nothing the planner or a helper writes can raise it.

| `SpawnConfig(...)` | Default | What it does |
|---|---|---|
| `enabled` | `False` | Master switch. |
| `capabilities` | `None` | Preset words a helper may use: `web` (search and fetch), `files_read` (read, list, find, grep), `files` (also write and edit), `code` (`run_python`), `delete`. Setting this or `tools` turns on ceiling mode. |
| `tools` | `None` | A pool of your own tool objects a helper may pick from by name. |
| `allowed_paths` | `["./workspace"]` | The file sandbox for `files_read`, `files`, `code` and `delete`. |
| `max_spawns` | 3 (6 in ceiling mode) | Sub-agents per run, planner spawns and delegations together. |
| `max_depth` | `1` | A helper can't spawn its own helpers. Raise it to allow deeper trees. |
| `approver` | `None` | Optional `(SpawnRequest) -> bool`. In ceiling mode it is an extra gate, not required. |
| `auto_spawn`, `auto_spawn_allowed_caps` | | The older approval flow, used only when neither `capabilities` nor `tools` is set. |

Inside the ceiling, nobody is asked for approval, so a long unattended run never stalls. A request outside it is clipped, not fatal: the helper is built with what is allowed. If a helper asked for tools and none are allowed, the step fails with `spawn refused: no usable tools`.

Your own tools join the pool like this:

```python
from agentx_dev import SpawnConfig, StandardTool

def lookup(query: str) -> str:
    return f"results for {query}"

lookup_tool = StandardTool(func=lookup, name="lookup", description="Search our internal wiki")
config = SpawnConfig(enabled=True, tools=[lookup_tool], capabilities={"web"})
```

## Delegation

Every specialist of a Supervisor with spawning on gets a `delegate` tool for the duration of the run (it is removed afterwards, so your runners are untouched):

```
delegate(task, instructions="", tools=[])  ->  the sub-agent's answer
```

- The helper sees only `task`, not the specialist's conversation. A thin `task` gives a thin result, so the specialist is told to put every needed fact in it.
- The answer comes back as the tool result, cut at 4,000 characters with a `[truncated]` marker. The full text and a progress ledger are kept on the run record, not in the specialist's context.
- A refusal comes back as plain text (`delegation refused: spawn limit reached; do this yourself`). It is information, not an error.
- A helper that gives up or crashes comes back as a tool error that starts `[delegate failed: stuck]` (or `error`, `out_of_time`, ...). The specialist's own stuck logic sees it and can retry with different instructions or do the work itself. A failed delegation never ends the specialist's run.

You can also give a standalone runner the same ability:

```python
from agentx_dev import AgentRunner, AgentType, SpawnConfig

researcher = AgentRunner(
    model=model, agent=AgentType.ReAct, tools=my_tools,
    delegation=SpawnConfig(enabled=True, capabilities={"web"}),
)
result = researcher.invoke("Survey the market. Hand each competitor to a helper.")
print(researcher.spawned)      # name, origin, tools, dropped, outcome, chars for this run
```

In the async runner, several `delegate` calls in one model turn run at the same time when the runner dispatches a turn's tool calls together (`bind_tools_natively=True`).

## Persistent mode

With `persistence=Persistence(...)` on a Supervisor and no `spawn_config`, spawning is on with a safe ceiling: `capabilities={"web", "files_read"}`, `max_spawns=6`. Pass `spawn_config=SpawnConfig(enabled=False)` to turn it off, or your own config to change it.

- Helpers inherit `persistence`: they get the same stuck handling, ledger and compaction, and they share the one run deadline and cost cap.
- A recovery plan can define a replacement helper and mark the failed step as replaced (`"replaces": ["s1"]`), so a stuck specialist can be swapped for a differently-instructed one.
- Code execution, writes and deletes are not in the default ceiling. Allow them explicitly in your own `SpawnConfig`.

See [Long-running agents](long-running-agents.md).

## Watching it

`supervisor.stream(task)` includes:

| Event | Meaning |
|---|---|
| `{"type": "spawn", "name", "origin": "plan"\|"delegate", "tools", "dropped", "reused", "refused", ...}` | A helper was built, reused, or refused. |
| `{"type": "delegate_result", "name", "outcome", "chars"}` | A delegation returned. |

With `verbose=True` the same moments print as `[spawn]` lines.

## Things to know

- **The ceiling bounds tools, not behavior.** Instructions can steer a helper within what you allowed (for example to fetch a particular URL with `web`). Keep `code`, `files` and `delete` out of the ceiling for supervisors that handle untrusted input.
- **Cost.** Each helper is a full agent with its own turns. `max_spawns` and `max_depth` bound the count; for unattended runs also use `Persistence` or a model cost cap (`model.configure_limits(budget_usd=..., input_price_per_1k=..., output_price_per_1k=...)`).
- **Fresh context is the point, and the catch.** A helper knows nothing you don't put in its task.
- **Tool cache.** Helpers in persistent mode run with the tool-result cache off, like any persistent agent.
````

- [ ] **Step 2: Register the page, the site card**

(a) In `host/build_data.py`, after the line
```python
        ("guides/long-running-agents", DOCS_DIR / "guides" / "long-running-agents.md", "Long-running agents"),
```
add
```python
        ("guides/sub-agents", DOCS_DIR / "guides" / "sub-agents.md", "Sub-agents"),
```

(b) In `host/app.js`, replace
```html
      <h2 class="section-title">What's new in 3.5</h2>
      <ul class="whats-new-list">
        <li>
          <strong>Agents that keep working</strong>
```
with
```html
      <h2 class="section-title">What's new in 3.6</h2>
      <ul class="whats-new-list">
        <li>
          <strong>Sub-agents</strong>
          <div class="desc">The planner can define a helper inline (<code>new_agent</code>: name, instructions, tools) and specialists can <code>delegate</code> a side job to a fresh agent and get a summary back. One ceiling you set bounds every helper: <code>SpawnConfig(capabilities={"web", "files_read"})</code>. <a href="#guides/sub-agents">Guide</a>.</div>
        </li>
        <li>
          <strong>Agents that keep working</strong>
```

- [ ] **Step 3: README**

Run from the worktree root (the outer fence has four backticks because the script contains fenced code):
````bash
python - <<'PY'
import pathlib
p = pathlib.Path("README.md")
s = p.read_text(encoding="utf-8")
crlf = "\r\n" in s
s = s.replace("\r\n", "\n")

def sub(a, b):
    global s
    assert s.count(a) == 1, a[:70]
    s = s.replace(a, b)

section = '''## What's new in 3.6 — sub-agents

A Supervisor can now create its own helpers, like Claude Code's Task tool.
The planner defines a helper inline (its own instructions and tools), and a
specialist can hand a side job to a fresh agent and get a short summary back.
You set the most a helper may ever do, once.

```python
from agentx_dev import Supervisor, SpawnConfig

supervisor = Supervisor(
    model=model, agents=my_specialists,
    spawn_config=SpawnConfig(enabled=True, capabilities={"web", "files_read"}),
)
result = supervisor.run("Compare the pricing pages of our three competitors")
print(result.spawned)       # the helpers it created, with outcomes
```

| Feature | What you get |
|---|---|
| **Inline helpers** | A plan step can carry `new_agent` (name, instructions, tools). Reused by name, discarded when the run ends. |
| **`delegate` tool** | A specialist hands part of its work to a fresh sub-agent (clean context) and gets a summary back. |
| **One ceiling** | `SpawnConfig(capabilities=..., tools=..., max_spawns=..., max_depth=...)` bounds every helper. Over-asks are clipped, not fatal. |
| **Sync and async** | `AsyncSupervisor` takes `spawn_config=` too; spawned steps run in parallel. |
| **Persistent mode** | On by default with a safe ceiling (web plus read-only files); helpers share the run deadline; recovery plans can spawn a replacement. |

[Guide](docs/guides/sub-agents.md).

### Upgrade notes

- Nothing is on unless you configure it, except persistent supervisors (`persistence=` with no `spawn_config`), which now allow web and read-only file helpers. Pass `spawn_config=SpawnConfig(enabled=False)` to opt out.
- Helpers a Supervisor spawns no longer stay on it after the run.
- A spawn is no longer refused because an existing specialist has the same tools.

Full notes: [docs/guides/upgrading.md](docs/guides/upgrading.md).

'''
sub("## What's new in 3.5 — agents that keep working\n", section + "## What's new in 3.5 — agents that keep working\n")

sub("""The framework's capability-overlap guard refuses duplicate spawns
(you already registered a specialist with those tools? planner tries
to spawn another one? refused, and any follow-up dispatches to the
refused name auto-reroute to the existing specialist).""",
"""Since 3.6 a spawn is not refused because an existing specialist has the
same tools (a helper can carry its own instructions), the planner can
write those instructions inline, and a `capabilities=` / `tools=` ceiling
replaces per-spawn approval. See [Sub-agents](docs/guides/sub-agents.md).""")
p.write_text(s.replace("\n", "\r\n") if crlf else s, encoding="utf-8", newline="")
print("README patched")
PY
````

- [ ] **Step 4: The other spawn docs, API summary, upgrading**

Run:
```bash
python - <<'PY'
import pathlib

def patch(path, pairs, append=None):
    p = pathlib.Path(path)
    s = p.read_text(encoding="utf-8")
    crlf = "\r\n" in s
    s = s.replace("\r\n", "\n")
    for a, b in pairs:
        assert s.count(a) == 1, (path, a[:70])
        s = s.replace(a, b)
    if append:
        s = s.rstrip("\n") + "\n" + append
    p.write_text(s.replace("\n", "\r\n") if crlf else s, encoding="utf-8", newline="")

# docs/advanced/supervisor.md
patch("docs/advanced/supervisor.md", [(
"""**Capability-overlap guard:** the framework refuses duplicate spawns.
If you already registered a specialist with those tools and the planner
tries to spawn another, the spawn is refused AND follow-up dispatches
to the refused name auto-reroute to the existing specialist.""",
"""**Overlap (3.6):** a spawn is no longer refused because a specialist you
registered has the same tools: a spawned agent can carry its own
instructions, so two agents with the same tools can still be different
specialists. The planner can now write those instructions itself
(`new_agent` in a plan step), specialists can `delegate` side jobs, and
`SpawnConfig(capabilities=..., tools=...)` sets a ceiling that replaces
per-spawn approval. See [Sub-agents](../guides/sub-agents.md).""")])

# docs/concepts/agents.md
patch("docs/concepts/agents.md", [(
"""cover a capability. Recognized capability keywords: `"web"`,
`"files"`, `"code"`, `"delete"`.""",
"""cover a capability. Recognized capability keywords: `"web"`,
`"files"`, `"code"`, `"delete"` (3.6 adds read-only `"files_read"`). Since 3.6
the planner can also give a spawned agent its own instructions and tools,
and specialists can `delegate` a side job to a fresh sub-agent: see
[Sub-agents](../guides/sub-agents.md).""")])

# docs/reference/api-summary.md
patch("docs/reference/api-summary.md", [(
"| `SpawnConfig` | dataclass | Dynamic specialist spawning settings |",
"| `SpawnConfig` | dataclass | Sub-agent settings: `enabled`, `capabilities` / `tools` (the ceiling), `allowed_paths`, `max_spawns`, `max_depth`, `approver` (3.6: ceiling mode; `AsyncSupervisor` and `AgentRunner(delegation=)` accept it) |\n"
"| `AgentSpec` *(3.6)* | dataclass | One sub-agent to build: `name`, `instructions`, `tools`, `origin` |")])

# docs/guides/upgrading.md
patch("docs/guides/upgrading.md", [(
"## Upgrading to 3.5.0\n",
"""## Upgrading to 3.6.0

**Nothing breaks.** Sub-agents are opt-in, with three behavior changes for code that already used spawning.

- **New:** a plan step can define a helper inline (`new_agent`), specialists get a `delegate` tool, and `SpawnConfig(capabilities=..., tools=...)` sets a ceiling that needs no per-spawn approval. `AsyncSupervisor(spawn_config=...)` and `AgentRunner(delegation=...)` are new. See [Sub-agents](sub-agents.md).
- **Changed:** a spawned agent no longer stays registered on the supervisor after the run. Previously `supervisor.agents` kept it, and a later run could see it.
- **Changed:** a spawn is no longer refused because an existing specialist has the same tools, so follow-up dispatches are not rerouted (`rerouted_from` in the `spawn` event is always `None`). The `spawn` event gained `origin`, `tools`, `dropped`, `reused`, `refused`.
- **Changed:** a `Supervisor` or `AsyncSupervisor` with `persistence=` and no `spawn_config` now allows helpers with web and read-only file access (`SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)`), and its specialists get the `delegate` tool for the run. Pass `spawn_config=SpawnConfig(enabled=False)` to keep the 3.5 behavior.
- **Small:** `SpawnConfig.max_spawns` is now `Optional[int]` (unset means 3, or 6 in ceiling mode); `AsyncAgentRunner` accepts `system_addendum=` like `AgentRunner`; the old `SUPERVISOR_SPAWN_INSTRUCTION` constant is gone (the planner prompt is built by `spawn_instruction`).

---

## Upgrading to 3.5.0
""")])
print("docs patched")
PY
```

- [ ] **Step 5: Cookbook, FAQ, troubleshooting**

(a) Append to the end of `docs/cookbook/patterns.md`:

````markdown

---

## 29. A supervisor that hires its own helpers *(3.6)*

The task decides which specialists it needs, and you decide the most any of them may do.

```python
from agentx_dev import Persistence, SpawnConfig, Supervisor

supervisor = Supervisor(
    model=model,
    agents={"writer": ("Drafts the final brief", writer)},      # the one specialist you know you need
    persistence=Persistence(max_minutes=45),
    spawn_config=SpawnConfig(
        enabled=True,
        capabilities={"web", "files_read"},     # helpers may search the web and read files, nothing else
        allowed_paths=["./workspace"],
        max_spawns=6,
    ),
)

result = supervisor.run(
    "Research our three closest competitors' pricing, then write a one-page brief."
)
print(result.content)
for sub in result.spawned:
    print(sub["name"], sub["origin"], sub["outcome"])
```

What happens: the planner defines a `pricing_researcher` helper with its own instructions and the `web` tool, runs it for each competitor, and hands the findings to `writer`. If a research step gets stuck, the recovery plan can define a differently-instructed replacement.

Things to know:

- **Say what to return.** The planner writes the helper's instructions; the framework adds "return the real data, don't invent it". Your task text should say what the brief must contain.
- **The ceiling is yours.** Without `code`, `files` or `delete` in `capabilities`, no plan can give a helper those, however the task is worded.
- **Helpers are for one run.** `supervisor.agents` is unchanged afterwards; `result.spawned` is the record.
- **A specialist can do this itself** with the `delegate` tool, to keep its own context small. See [Sub-agents](../guides/sub-agents.md).
````

(b) In `docs/cookbook/faq.md`, immediately before the heading `## When should I use ReAct vs. function-calling vs. native binding?` insert:

```markdown
## Can the supervisor create its own sub-agents, like Claude Code's Task tool? *(3.6)*

Yes. Give the Supervisor a `spawn_config`:
`Supervisor(..., spawn_config=SpawnConfig(enabled=True, capabilities={"web", "files_read"}))`.
The planner can then define a helper inline (its own instructions and
tools), and your specialists get a `delegate` tool to hand a side job to
a fresh agent and get a short summary back. You set the most a helper may
do (`capabilities=`, `tools=`, `max_spawns=`); nothing the model writes
can exceed it. With `persistence=` set and no `spawn_config`, this is on
with web plus read-only files. See [Sub-agents](../guides/sub-agents.md).

```

(c) In `docs/cookbook/troubleshooting.md`, replace
```
`persistence` is set, so every call executes.

## Tool errors
```
with
```
`persistence` is set, so every call executes.

**`spawn refused: no usable tools`**
A helper asked for tools and none are inside the ceiling. Add the preset
(for example `"files"` or `"code"`) to `SpawnConfig(capabilities=...)` or
put your tool in `SpawnConfig(tools=[...])`. The persistent default
allows only `web` and read-only files.

**`delegation refused: spawn limit reached; do this yourself`**
`max_spawns` counts planner-created helpers and `delegate` calls
together, per run. Raise it in `SpawnConfig`, or have the specialist do
the work itself.

**A helper I spawned isn't on the supervisor after the run**
Helpers last for one run (since 3.6). The record is in
`result.spawned`; to keep an agent, register it in `agents=`.

**A delegated helper keeps coming back `[delegate failed: ...]`**
It gave up or crashed; the message after the colon says how, and
`runner.spawned` / `result.spawned` hold its outcome. Give it a clearer
`task` (it sees nothing else) or different `instructions`.

## Tool errors
```

- [ ] **Step 6: CHANGELOG and version**

(a) In `CHANGELOG.md`, insert immediately before `## [3.5.0] - 2026-10-04`:

```markdown
## [3.6.0] - 2026-10-04

Sub-agents: the Supervisor can create its own helpers. Opt-in, with the behavior changes listed below.

### Added

- **Inline helpers**: a plan step can carry `new_agent` (name, free-form
  instructions, tools). The helper runs the step, is reused by name, and is
  discarded when the run ends. Recovery plans can define a replacement for a
  stuck step.
- **`delegate` tool**: specialists hand a side job to a fresh sub-agent (clean
  context) and get a short summary back; refusals are plain text and a helper
  that fails comes back as a tool error the caller's stuck logic sees.
  `AgentRunner(..., delegation=SpawnConfig(...))` gives a standalone runner the
  same tool; `runner.spawned` lists what it created.
- **One ceiling**: `SpawnConfig(capabilities=..., tools=..., max_spawns=...,
  max_depth=...)` bounds every helper; spawns inside it need no approval and
  over-asks are clipped, not fatal. New read-only `files_read` preset.
- **`AsyncSupervisor(spawn_config=...)`**: async parity; spawned steps run in
  parallel. `AsyncAgentRunner` gained `system_addendum=`.
- `SupervisorResult.spawned`, `spawn` and `delegate_result` stream events,
  `AgentSpec`, `ToolRegistry.unregister`, `AgentRunner.add_tool` /
  `remove_tool`.
- New guide: Sub-agents.

### Changed

- A spawned agent no longer stays on the supervisor after the run.
- A spawn is no longer refused (or rerouted) because an existing specialist
  has the same tools. The `spawn` event gained `origin`, `tools`, `dropped`,
  `reused` and `refused`; `rerouted_from` is always `None`.
- A `Supervisor` / `AsyncSupervisor` with `persistence=` and no `spawn_config`
  now allows helpers with web and read-only file access (`max_spawns=6`) and
  gives its specialists the `delegate` tool. Pass
  `spawn_config=SpawnConfig(enabled=False)` to opt out.
- `SpawnConfig.max_spawns` is `Optional[int]` (unset: 3, or 6 in ceiling mode).
  `SUPERVISOR_SPAWN_INSTRUCTION` was removed.

```

(b) In `pyproject.toml`, change `version = "3.5.0"` to `version = "3.6.0"`. Also search for any other place the version string is duplicated: run `grep -rn "3\.5\.0" agentx_dev pyproject.toml host/build_data.py` and update a `__version__` or similar constant to `3.6.0` if one exists (the site's `AGENTX_VERSION` is generated from `pyproject.toml` in Step 7).

- [ ] **Step 7: Regenerate the site data and verify the docs**

Run: `python host/build_data.py`
Expected: no error; `grep -m1 AGENTX_VERSION host/data.js` shows version `3.6.0`.

Check that every Python block in the new guide parses and every `agentx_dev` name it imports exists:
```bash
python - <<'PY'
import ast, importlib, pathlib, re
text = pathlib.Path("docs/guides/sub-agents.md").read_text(encoding="utf-8")
blocks = re.findall(r"```python\n(.*?)```", text, re.S)
for block in blocks:
    for node in ast.walk(ast.parse(block)):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("agentx_dev"):
            mod = importlib.import_module(node.module)
            for alias in node.names:
                assert hasattr(mod, alias.name), f"{node.module} has no {alias.name}"
print(len(blocks), "python blocks OK")
PY
```
Expected: `3 python blocks OK`.

Check that every parameter the guide's table names exists on `SpawnConfig`:
```bash
python -c "
import dataclasses
from agentx_dev import SpawnConfig
names = {f.name for f in dataclasses.fields(SpawnConfig)}
need = {'enabled','capabilities','tools','allowed_paths','max_spawns','max_depth','approver','auto_spawn','auto_spawn_allowed_caps'}
assert need <= names, need - names
print('SpawnConfig fields OK')
"
```
Expected: `SpawnConfig fields OK`.

- [ ] **Step 8: Full suite**

Run: `python -m pytest tests -q -p no:cacheprovider`
Expected: everything green.

- [ ] **Step 9: Commit**

```bash
git add docs/guides/sub-agents.md docs/cookbook docs/guides/upgrading.md docs/reference/api-summary.md docs/advanced/supervisor.md docs/concepts/agents.md README.md CHANGELOG.md pyproject.toml host/build_data.py host/app.js host/data.js
git commit -m "docs: sub-agents guide, cookbook, upgrade notes; 3.6.0"
```

---

### Task 8: Release checklist (do not run without the user's go-ahead)

Releasing publishes to PyPI and GitHub, which cannot be undone. The user approves each release explicitly; this task is the checklist, not an instruction to execute. It is the same pipeline used for 3.5.0.

- [ ] Confirm the CHANGELOG date for `[3.6.0]` is the release day (edit it if not), then re-run `python host/build_data.py`.
- [ ] Full suite green: `python -m pytest tests -q -p no:cacheprovider`.
- [ ] Merge `feat/supervisor-subagents` into `main` (fast-forward) in the main checkout, once it is clean.
- [ ] Sync the `PIP version` folder from the main checkout: mirror `agentx_dev/`, copy `README.md`, `CHANGELOG.md`, `pyproject.toml`, `LICENSE`, `MANIFEST.in`, `AGENTX.md`, `CONTRIBUTING.md`.
- [ ] Build there (`python -m build`), `python -m twine check dist/agentx_dev-3.6.0*`, verify the wheel contains `agentx_dev/SubAgents.py`, and that `AgentSpec` and `SpawnConfig` import from an isolated `pip install --no-deps --target <dir>` copy; run the sub-agent tests against that copy from a directory outside the repo.
- [ ] Push `main` and the annotated tag `v3.6.0` (no `Co-Authored-By` trailer anywhere). The upload (`python -m twine upload --config-file <pypirc.ini> dist/agentx_dev-3.6.0*`) is run by the user (the classifier blocks passing the credentials file); then verify `https://pypi.org/pypi/agentx-dev/3.6.0/json`.
- [ ] With the user's permission, upgrade their local install (`python -m pip install -U agentx-dev`) and remind them to restart any running kernel.
