# Supervisor sub-agent creation (planner-time spawning and runtime delegation)

Status: design, approved section by section on 2026-10-04. Target release: 3.6.0.
Builds on: `docs/superpowers/specs/2026-10-04-persistent-agents-design.md` (3.5.0).

## 1. Problem

Claude Code can break a big task apart by launching a fresh sub-agent with its own
instructions and tools, then reading back a short summary. The Supervisor can only
do a limited version of this:

- **Sync only.** `Supervisor` can create a specialist through a `__spawn__` plan step.
  `AsyncSupervisor` cannot (`Supervisor.py`, `_handle_spawn`; the async class has no
  spawn code).
- **Off by default and approval-gated.** `SpawnConfig.enabled=False`; every spawn goes
  through an interactive approver or an `auto_spawn_allowed_caps` gate, so an
  unattended long run stalls or refuses.
- **A fixed menu.** A spawn names capability words (`web`, `files`, `code`, `delete`).
  The planner cannot write the sub-agent's instructions or choose individual tools.
- **A rigid overlap rule.** A spawn is refused when an existing specialist already has
  the same tools (`_find_existing_for_capabilities`), which blocks the useful case of
  the same tools with different instructions.
- **Planner-time only.** All decisions are made once, up front, in the plan. A
  specialist that discovers mid-task that it needs help cannot hand a piece of its work
  to a fresh sub-agent.
- **A leak.** A spawned specialist is written into `self.agents` and stays on the
  supervisor instance after the run, so a later run sees agents the new task never
  asked for.

## 2. Goals and non-goals

Goals:

1. The planner can define a new specialist inline in a plan step: name, free-form
   instructions, and a choice of tools. Recovery plans (3.5.0) can do the same.
2. A specialist can hand part of its own work to a fresh sub-agent mid-run through a
   `delegate` tool and receive a summary back.
3. One checkpoint (`SpawnPolicy`) bounds what any spawned agent can do, set once by
   the developer. Inside the ceiling nobody is asked for approval.
4. Sync and async behave the same. Async sub-agents run in parallel.
5. Persistent mode (3.5.0) works with it: spawned agents inherit `persistence`, share
   the one run budget, and a failed delegation is handled by the caller's stuck logic.
6. With no spawning configured, behavior is unchanged.

Non-goals:

- Nested supervisors, saving spawned agents to disk, and MCP tool discovery for the
  tool pool.
- Replacing the plan format. Existing plans, `agents=` registration, and the
  `__spawn__` step keep working.
- Fixing the default tool-result cache hazards (separate fix; sub-agents run with
  the cache suspended while `persistence` is set, as specified in 3.5.0 §4.9).

## 3. Public API

### 3.1 `AgentSpec`

A frozen dataclass describing one sub-agent, produced by the planner (plan step) or by
the `delegate` tool, and consumed by the single builder.

```python
@dataclass(frozen=True)
class AgentSpec:
    name: str                       # identifier; [A-Za-z0-9_-]{1,40}
    instructions: str               # free-form role prompt, max 4000 chars (longer is truncated)
    tools: Tuple[str, ...] = ()     # tool names from the pool, or preset capability words
    origin: str = "plan"            # "plan" | "delegate"
```

### 3.2 `SpawnConfig` (extended)

Every current field keeps its meaning (`enabled`, `auto_spawn`, `approver`,
`allowed_paths`, `max_spawns`, `auto_spawn_allowed_caps`). Three fields are added:

```python
tools: Optional[List[Any]] = None            # pool of tool objects a spawn may pick from
capabilities: Optional[Set[str]] = None      # allowed preset words
max_depth: int = 1                           # 1 = a sub-agent cannot spawn
```

- Preset capability words: `web`, `files`, `code`, `delete` (as today) plus the new
  `files_read` (read-only: `read_path`, `list_directory`, `find_files`, `grep`). A preset expands to the
  tools it already expands to today (`SpawnConfig._CAP_TO_TOOLS`, moved to the policy).
- **Ceiling mode** applies when `tools` or `capabilities` is not `None`. Spawns inside
  the ceiling need no approval. If an `approver` is set it is still called.
- **Legacy mode** applies when both are `None`. The 3.5.0 approval flow and
  `auto_spawn_allowed_caps` gate behave as before.
- `max_spawns` becomes `Optional[int] = None`. Unset means 3 in legacy mode and 6 in ceiling
  mode and in the persistent default. It counts planner-time spawns and `delegate` calls
  together, per run.
- `allowed_paths` is the sandbox for every spawned agent, as today.

### 3.3 Where it is accepted

- `Supervisor(..., spawn_config=SpawnConfig(...))` and the same on `AsyncSupervisor`
  (new; today only the sync class has the parameter).
- `AgentRunner(..., delegation=SpawnConfig(...))` and `AsyncAgentRunner(...)`.
- **Persistent default.** When a Supervisor/AsyncSupervisor has `persistence` set and
  no `spawn_config`, the effective config is
  `SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)`.
  Without persistence the default stays `SpawnConfig(enabled=False)`.
  An explicit `spawn_config=` always wins, including `SpawnConfig(enabled=False)`.
- Standalone runners have no default: delegation is off unless `delegation=` is given.

### 3.4 Plan format

A plan step may carry an inline agent instead of naming one:

```json
{"id": "s2", "query": "Compare the three pricing pages",
 "new_agent": {"name": "pricing_analyst",
               "instructions": "You compare SaaS pricing. Return a table: plan, price, limits, source URL.",
               "tools": ["web_search", "web_fetch"]}}
```

- `new_agent` and `agent` are mutually exclusive on one step. A step with both is
  invalid.
- Recovery steps (3.5.0 §4.7) accept `new_agent` and the existing `replaces` field.
- The legacy `{"agent": "__spawn__", ...}` step is still accepted and is converted to
  an `AgentSpec` (instructions = its `description`, tools = its `capabilities`).

### 3.5 `delegate` tool

```
delegate(task: str, instructions: str = "", tools: list[str] = []) -> str
```

Registered on every specialist of a Supervisor/AsyncSupervisor whose effective config
has `enabled=True`, and on a standalone runner given `delegation=`. A sub-agent built
at depth `max_depth` does not receive it. The sync runner gets a synchronous tool; the
async runner gets an async tool.

## 4. Behavior

### 4.1 The policy (single checkpoint)

`SpawnPolicy(config, model, persistence, budget)` is the only code that turns an
`AgentSpec` into a runner. Everything else (plan steps, `delegate`, legacy `__spawn__`)
calls `policy.build(spec, depth)` and gets back either a runner plus a report or a
refusal.

`policy.build`:

1. Reject when `config.enabled` is False or the run's spawn counter is at `max_spawns`
   (message: `spawn limit reached`).
2. Normalize `name` and `instructions`; reject an empty name or empty instructions.
3. **Clip, never refuse the whole spawn.** Resolve each requested tool name against the
   ceiling: preset words allowed by `capabilities`, and tool names present in `tools`.
   Unknown or over-ceiling names are dropped and listed in `report.dropped`. If no tool
   remains and the spec requested at least one, reject with `no usable tools`. A spec
   that requests no tools gets none.
4. Permissions are derived from the surviving presets only, always sandboxed to
   `allowed_paths`; pool tools are passed as given. Instructions never change tools,
   permissions, the sandbox, or the data-reply addendum.
5. Build an `AgentRunner` (or `AsyncAgentRunner` under an async parent; `AsyncAgentRunner`
   gains the same `system_addendum` parameter for this) with
   `max_iterations=15`, `verbose=False` (function calling is auto-detected from the model, as
   for any runner), and
   `system_addendum` = the existing spawned-specialist addendum followed by the spec's
   instructions. When the parent has `persistence`, set it on the runner (the
   persistence mixin then overrides `max_iterations` with `max_turns` and suspends the
   tool cache, as in 3.5.0).
6. If `depth < max_depth`, add the `delegate` tool at `depth + 1`.
7. Increment the spawn counter and emit the `spawn` event (§4.6).

### 4.2 Planner-time spawning

- The planner prompt describes `new_agent` (fields, the tool menu from the ceiling, the
  limit on spawns, "prefer an existing specialist when one fits", "write instructions
  that say what data to return"). When spawning is disabled the prompt is unchanged
  from today.
- Plan sanitization accepts `new_agent` steps, validates them, and treats the
  step's `name` like a specialist name for later steps.
- **Reuse within a run.** A later step that sets `agent` to a name defined earlier in
  this run's plans uses that sub-agent; it is not rebuilt and does not consume another
  spawn. An identical `new_agent` repeated later (same name, same instructions, same
  tools) also reuses it. The same name with different content is a conflict (below).
- **Name collisions.** A `new_agent` whose name matches a developer-registered
  specialist dispatches that specialist and ignores the spec; the verbose log and the
  `spawn` event (with `reused: "registered"`) say so. A name that matches an earlier
  spawn but with different content gets a numeric suffix (`name_2`) and later steps in
  the same plan that referenced the plain name keep resolving to the first one.
- **Overlap guard becomes advice.** `_find_existing_for_capabilities` and the reroute
  machinery are removed: a spawn is never refused for overlapping tools. The planner prompt
  tells it to prefer an existing specialist when one fits, and the verbose log notes when a
  new agent duplicates an existing specialist's tools. Custom instructions with the same
  tools are allowed.
- **Lifetime.** Spawned sub-agents live in a per-run registry on the run state, not in
  `self.agents`. They are discarded when `stream`/`astream` ends, including early exit.
  `self.agents` is not mutated.
- **Async parity.** `AsyncSupervisor` handles `new_agent` and legacy `__spawn__`
  steps. Spawned steps run in the same parallel DAG batches as any other step.
- **Recovery rounds (persistent).** Recovery plans may define new agents; a recovery
  step may both define `new_agent` and declare `replaces`, so a stuck specialist can be
  replaced by a differently instructed one. Spawns count toward `max_spawns`; spawned
  agents share the run's single `RunBudget`.
- Legacy mode (no `tools`/`capabilities`): the approval flow of 3.5.0 runs in
  `policy.build` before step 1's remaining checks, unchanged, including
  `auto_spawn_allowed_caps`.

### 4.3 Runtime delegation

`delegate(task, instructions, tools)`:

1. Build an `AgentSpec(origin="delegate")` with a generated name
   (`delegate_<n>`), pass it through `policy.build` at the caller's depth.
2. Run the sub-agent on `task` with a fresh context: it sees only `task`; the
   caller's history is not passed.
3. Return the sub-agent's final answer as the tool result, truncated to 4,000
   characters with a trailing `[truncated]` marker. The full text and the sub-agent's
   progress ledger are recorded on the run record (`SupervisorResult` gains
   `spawned: List[Dict]` with name, origin, tools, dropped, outcome, and sizes) and are
   not put into the caller's context.
4. Refusals return plain tool results: `delegation refused: spawn limit reached; do
   this yourself` / `no usable tools` / `delegation is disabled`. They are not
   errors.
5. A sub-agent that ends with a non-`done` outcome returns a tool result starting
   `[delegate failed: <outcome>]` followed by its ledger report (or error text), and is
   marked as a tool error so the caller's stuck tracker and reflection ladder (3.5.0
   §4.3) see it. A failed delegation never ends the caller's run by itself.
6. **Depth.** A sub-agent built at depth `max_depth` does not get `delegate`. With
   `max_depth=1` (default) only the supervisor's specialists delegate; their sub-agents
   cannot.
7. **Concurrency.** In the async runner `delegate` is an async tool. Several `delegate`
   calls in one model turn run concurrently wherever the runner already dispatches a
   turn's tool calls together (`bind_tools_natively=True`); in text and function-calling
   mode a turn carries one call, so delegations from one agent run one after another.
   Parallelism across plan steps (several specialists each delegating) is unaffected. In the
   sync runner delegations run in sequence.
8. **Budget.** A sub-agent receives the parent's shared `RunBudget` (via the private
   `_budget=` argument only if the runner accepts it, as in 3.5.0 §4.7), so one
   delegation cannot outlast the deadline or the cost cap.

### 4.4 Persistent mode

- Spawned and delegated agents get the parent's `Persistence` (stuck ladder, ledger,
  compaction, patient retries) and the cache suspension that comes with it.
- A delegated agent that ends `out_of_time`/`out_of_budget` returns the §4.3.5 failure
  result; the caller then ends through its own budget check at the next turn.
- The supervisor's unresolved-step accounting is unchanged: a plan step that ran on a
  spawned agent is a normal step with a normal outcome.

### 4.5 Errors

| Situation | Result |
|---|---|
| Malformed `new_agent` (missing name or instructions, both `agent` and `new_agent`, bad types) | Plan sanitization drops the plan and the existing repair retry runs (`max_plan_retries`); the repair note names the problem. |
| Unknown or over-ceiling tool names | Dropped; spawn proceeds; the planner/caller is told what was dropped. |
| No tool survives clipping though some were requested | `no usable tools`: the step fails (plan) or the tool returns the refusal (delegate). |
| Spawn limit reached | Plan step fails with `spawn limit reached`; recovery planning is told; `delegate` returns the refusal message. |
| Name collides with a registered specialist | Registered specialist is used; spec ignored; logged. |
| Spawned agent raises or gives up | Ordinary specialist rules (retries, outcome, supersede). |
| Approver raises (legacy mode, or ceiling mode with an approver) | Treated as a refusal; logged. |

### 4.6 Events and logs

- `{"type": "spawn", "name": str, "origin": "plan" | "delegate", "tools": [str], "dropped": [str], "reused": str | None, "refused": str | None, "capabilities": [str], "rerouted_from": None}`
  on both supervisors' streams, emitted when a sub-agent is built, reused or refused. It is a
  superset of the 3.5.0 spawn event (`name`, `capabilities`, `rerouted_from` keep their
  meaning; `capabilities` repeats `tools`, `rerouted_from` is always `None` now). `reused`
  is `None` for a fresh build, `"registered"` or `"spawned"` otherwise; `refused` is the
  reason when the spawn was refused.
- `{"type": "delegate_result", "name": str, "outcome": str, "chars": int}` when a
  delegation returns.
- Standalone runners with `delegation=` do not stream these events; they expose the same
  records as `runner.spawned` (a list of dicts, reset at the start of each run).
  `verbose=True` prints `[spawn]` lines everywhere.
- Existing event types are unchanged.

## 5. Files

- `agentx_dev/SubAgents.py` (new): `AgentSpec`, `SpawnPolicy`, the builder, the
  `delegate` tool factories (sync and async), the per-run registry, preset expansion.
- `agentx_dev/Supervisor.py`: `SpawnConfig` fields; `new_agent` in planner prompts
  and plan sanitization; per-run registry replacing `self.agents` mutation; legacy
  `__spawn__` conversion; async spawn handling; `spawn_config` on `AsyncSupervisor`;
  persistent default; `spawned` on `SupervisorResult`; events.
- `agentx_dev/Runner/AgentRun.py`, `AsyncAgentRun.py`: `delegation=` parameter that
  registers the `delegate` tool.
- `agentx_dev/__init__.py`: export `AgentSpec`.
- Tests: `tests/test_subagent_policy.py`, `test_subagent_plan.py`,
  `test_subagent_plan_async.py`, `test_subagent_delegate.py`,
  `test_subagent_delegate_async.py`, `test_subagent_persistent.py`; existing spawn
  tests stay and must pass unchanged.
- Docs: `docs/guides/sub-agents.md` (new), cookbook #29, FAQ, troubleshooting,
  upgrading, api-summary rows, README section, CHANGELOG `[3.6.0]`, `pyproject.toml`
  3.6.0, regenerated `host/data.js`.

## 6. Testing

Every behavior is tested on the sync and async classes. Mock models only; no real
sleeping and no wall-clock assertions (use injected clocks and event ordering).

- **Policy:** clipping (unknown and over-ceiling names dropped and reported; none left
  → `no usable tools`); presets expand correctly including `files_read`; permissions
  derive only from surviving presets; instructions cannot add tools or widen the
  sandbox; `max_spawns` shared by plan spawns and delegations; the persistent default
  ceiling is exactly web plus read-only files.
- **Plan:** `new_agent` sanitization (valid, malformed, both `agent` and `new_agent`);
  reuse of a defined name across steps; identical redefinition reuses; differing
  redefinition suffixes; collision with a registered specialist; legacy `__spawn__`
  conversion; overlap advice appears and does not block.
- **Supervisor runs:** a plan that spawns, runs, and synthesizes; async parallel DAG
  with two spawned steps; recovery round that spawns a replacement and supersedes the
  failed step; spawn limit reached mid-plan; `self.agents` identical before and after a
  run, including after an early `stream` close (regression for the leak).
- **Delegate:** summary returned and capped at 4,000 characters; fresh context (the
  caller's history is absent from the sub-agent's input); refusals are plain results;
  a failed or gave-up delegation is a tool error with the ledger report; depth limit
  (sub-agent has no `delegate` at `max_depth=1`; has it at 2); shared `RunBudget` ends
  a delegation at the deadline; concurrent delegation in one async turn.
- **Persistence:** spawned and delegated agents carry the parent's `Persistence`;
  cache suspended on them; a stuck delegated agent does not end the caller's run.
- **Compatibility:** all existing spawn tests pass unchanged (`Supervisor._handle_spawn`
  called directly, outside a run, still registers the agent on `self.agents`; inside a run
  it registers on the per-run copy); legacy-mode approval flow and `auto_spawn_allowed_caps`
  behave as in 3.5.0; a Supervisor with no spawn
  configuration behaves identically.

## 7. Risks

- **Prompt injection.** The plan and `delegate` arguments are model output downstream
  of user text. The ceiling is the defence: tools and permissions are clipped to what
  the developer allowed, never widened by instructions. The default ceiling excludes
  code execution, writes, and deletes. Residual risk: instructions can steer a
  sub-agent within the ceiling (for example to fetch an attacker-chosen URL); the
  documentation says so.
- **Cost amplification.** Each spawn is a fresh agent with its own turns. Bounded by
  `max_spawns`, `max_depth`, and the one shared `RunBudget`. Without persistence there
  is no run budget; the guide recommends `Persistence` or a cost cap for unattended
  runs, and `max_spawns` still bounds the count.
- **Lost context.** A sub-agent sees only its task. A thin `task` yields a thin result.
  The tool description and the guide say to put needed facts in `task`.
- **Behavior change.** Spawned agents no longer persist on the supervisor after a run
  (leak fix) and the overlap guard no longer refuses; both are listed in the upgrade
  notes. Async supervisors gain spawning only when configured or persistent.
- **Plan size.** Tool and spawn guidance adds prompt text when spawning is enabled;
  it is omitted when disabled.
