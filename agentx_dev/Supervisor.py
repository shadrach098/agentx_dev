"""
Supervisor / multi-agent orchestration layer for AgentX.

Provides ``Supervisor`` and ``AsyncSupervisor`` classes that:
1. Accept a registry of named specialist sub-agents.
2. Use an LLM to decompose a high-level task into sub-tasks and assign each to
   the best specialist.
3. Execute those sub-tasks (sequentially for the sync supervisor, concurrently
   for the async supervisor).
4. Synthesize the sub-agent results into a single final answer.

Also supports DYNAMIC SPAWNING — if enabled, the planner can request a
new specialist mid-plan. In auto_spawn mode the framework creates the
requested specialist and adds it to the registry silently; otherwise a
callback (defaulting to interactive terminal input) approves or rejects
the request per-spawn.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing, closing
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Operator import OperatorChannel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner
from agentx_dev.Runner.Persistence import OUTCOME_OUT_OF_TIME, Persistence, RunBudget, accepts_budget
from agentx_dev.SubAgents import SpecError, parse_agent_spec

# The parts of this module live in agentx_dev/_supervisor/. Every name is imported back here, so code that imports
# it from agentx_dev.Supervisor keeps working (the public ones: import them from agentx_dev).
from agentx_dev.SubAgents import SpawnConfig, SpawnRequest  # noqa: F401
# ruff: noqa: F401
from agentx_dev._supervisor.results import (
    Specialist, SubtaskResult, SupervisorResult, _normalize_agents, _per_run_agents, _step_agent_name,
)
from agentx_dev._supervisor.prompts import (
    SUPERVISOR_PLAN_PROMPT, SUPERVISOR_SYNTHESIZE_PROMPT,
)
from agentx_dev._supervisor.plans import (
    _CATALOG_HIDDEN_TOOLS, _CATALOG_MAX_TOOLS, _catalog_tools_line, _evaluate_skip_when, _plan_repair_note,
    _plan_uses_deps, _render_agent_catalog, _sanitize_plan, _topo_order,
)
from agentx_dev._supervisor.steps import (
    SESSION_ANSWER_CHARS, _C_DISPATCH, _C_ERROR, _C_FINAL, _C_PLAN, _C_RESET, _C_RESULT, _RECAP_CHARS,
    _augment_query_with_error, _augment_query_with_iteration_limit, _augment_query_with_unmet_criteria,
    _budget_outcome_of, _budget_reason, _build_augmented_query, _cap_text, _evaluate_success_verdict,
    _follow_up_task, _format_results_block, _log_budget, _log_dispatch, _log_final, _log_no_plan, _log_plan,
    _log_replan, _log_result, _outcome_line, _outcome_of, _progress_of, _recovery_note, _rename_colliding_ids,
    _resolve_replaced, _result_done, _result_failed, _session_settings, _stopped_before_planning,
    _strip_code_fences, _supervisor_outcome, _unfinished_reason, _unresolved_note,
)
from agentx_dev._supervisor.core import (
    _SpawnMixin, _SupervisorCore, _iterate_sync, _run_sync,
)

class Supervisor(_SupervisorCore):
    """
    Synchronous multi-agent supervisor.

    Decomposes a high-level task into sub-tasks, dispatches them to the
    appropriate specialist :class:`AgentRunner`, and synthesizes the
    results into a single final answer.
    """

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, AgentRunner]],
        max_subtasks: int = 5,
        verbose: bool = True,
        spawn_config: Optional[SpawnConfig] = None,
        max_subtask_retries: int = 1,
        subtask_success_check: Optional[Callable[[SubtaskResult], Any]] = None,
        max_plan_retries: int = 1,
        persistence: Optional[Persistence] = None,
        ask_user: Any = None,
        max_questions: int = 3,
        ask_timeout: Optional[float] = None,
        memory: Any = None,
        memory_top_k: int = 4,
        memory_min_score: float = 0.2,
        memory_write: bool = True,
        ask_when_done: bool = False,
        follow_up: bool = False,
        max_follow_ups: int = 5,
        planner_note: str = "",
    ):
        """
        Args:
            model: LLM used for planning and synthesis.
            agents: Mapping of ``name -> (description, AgentRunner)`` or
                ``name -> Specialist`` (3.3). Tuples are wrapped as
                Specialist internally.
            max_subtasks: Hard upper bound on the number of planned sub-tasks.
            verbose: When True (default), print colored progress markers for
                each stage — the plan, each sub-task dispatch and result,
                and the synthesized final answer. Set to False for silent
                operation (only the SupervisorResult is returned).
            spawn_config: Optional SpawnConfig enabling the planner to
                request NEW specialists mid-plan. When None (default),
                spawning is disabled and the planner can only use the
                specialists passed in ``agents``. See SpawnConfig for the
                auto_spawn / approver knobs. (3.6) The planner can define a new
                specialist inline (``new_agent``: name, instructions, tools) and
                specialists can ``delegate`` to fresh sub-agents; tools are clipped
                to the config's ceiling (``tools`` / ``capabilities``). With
                ``persistence`` set and no ``spawn_config`` the default is
                ``SpawnConfig(enabled=True, capabilities={"web", "files_read"},
                max_spawns=6)``; pass ``SpawnConfig(enabled=False)`` to opt out.
            max_subtask_retries: How many times a sub-task that RAISES (or
                returns an outcome other than "done") is re-dispatched
                before the Supervisor gives up on it.
                Default 1 (so a specialist that hits a transient or
                self-correctable failure — a malformed tool call, a bad
                escape — gets a second chance instead of the whole
                sub-task being abandoned on the first error). Each retry
                appends the prior error to the query so the specialist
                knows what to fix. Set to 0 to restore the old
                quit-on-first-failure behavior. Errors and non-"done"
                outcomes trigger a retry (stopping early once the shared
                budget is spent); a sub-task that returns "done" content
                (even thin content) is accepted as-is — unless you also
                pass ``subtask_success_check`` (below).
            subtask_success_check: Optional predicate
                ``(SubtaskResult) -> bool | str`` deciding whether a
                *returned* (non-raised) result is acceptable. Catches the
                "ran fine but produced nothing useful" case — a scraper
                that saved 0 links, an extractor that found no data.
                Return ``True`` to accept; ``False`` or a ``str`` reason
                to reject. A rejected result is retried like a raised
                error (the reason is fed back into the query), bounded by
                ``max_subtask_retries``; once exhausted the last result is
                returned with its ``error`` set to the reason (content
                preserved). A predicate that itself raises is treated as
                "accept" so a buggy check can't wedge the run. Default
                ``None`` keeps the exceptions-only behavior.
            max_plan_retries: (3.3) When plan sanitization has to repair
                the planner's graph (unknown dependency dropped, cycle
                broken), the plan is re-requested up to this many times
                with the repair warnings appended to the prompt. The
                sanitized plan is kept as fallback if the retry is no
                better. ``0`` disables replanning.
            persistence: (3.5) Opt-in ``Persistence(...)``. When a step does
                not finish, the Supervisor replans around it (completed
                steps are kept), bounded by ``max_replans`` per stuck
                episode and by one shared ``max_minutes`` deadline that
                every specialist inherits. It is applied to specialist
                runners that have none, for the duration of the run.
                ``None`` (default) keeps the one-shot plan/run/synthesize
                behaviour.
            ask_user: (3.6) Lets the planner and every agent ask the human operator
                for a fact the task leaves out. ``True`` uses the built-in asker
                (notebook input box, terminal, or the controlling terminal; headless
                means no answer). A function ``(question) -> str | None`` routes the
                question through your own channel (a chatbot, a websocket). ``None``
                (default) is off. No answer means the agent proceeds on a stated
                assumption.
            max_questions: (3.6) Questions per run, shared by the planner and all
                agents. Default 3.
            ask_timeout: (3.6) Seconds to wait for one answer. ``None`` lets the
                channel decide (no limit in a notebook or terminal; 300 s on the
                controlling-terminal path).
            memory: (3.6) Long-term memory: any vector store with ``add`` and ``search``
                (``VectorStore``, ``ChromaVectorStore``, ``QdrantVectorStore``,
                ``PgVectorStore``). The planner and every dispatched step get the few
                relevant stored items as a FROM MEMORY block; the Supervisor saves each
                operator answer and each completed run's final answer; an exact repeat of an
                answered question is answered from memory. ``None`` (default) is off.
            memory_top_k: Items per lookup (default 4; 0 turns recall off).
            memory_min_score: Matches below this score are dropped (default 0.2; about 0.5
                for OpenAI embeddings, about 0.1 for HashEmbeddings).
            memory_write: False makes the store read-only.
            ask_when_done: (3.6.2) When a round's answer is ready, the framework itself asks the
                operator "What should I do next?" through the same asker as ``ask_user`` (the
                answer is shown in the prompt) and returns the reply in ``result.next_step``
                (``None`` if nobody answered). Asked whatever the outcome. Needs ``ask_user``.
                The planner and the helpers are told the Supervisor asks it, so they do not.
            follow_up: (3.6.2) Run another round on the operator's reply, with the earlier
                results as context, until the operator replies blank or with a closing word
                (any of "done", "stop", "quit", "exit", "no", "nothing", "that's all", "that's
                it", "all done", "nope", "bye", "goodbye", "thanks", "thank you", "no thanks",
                "no thank you", "done thanks", "done thank you", "nothing thanks", "stop
                thanks"; case-insensitive; apostrophes (``thats all`` works), commas, extra spaces and trailing ``.``, ``!``, ``?`` ignored).
                Implies ``ask_when_done``. ``result.rounds`` lists every round.
            max_follow_ups: (3.6.2) Rounds allowed after the first (default 5). After the last
                one no question is asked.
        """
        self._configure(
            model=model,
            agents=agents,
            max_subtasks=max_subtasks,
            verbose=verbose,
            max_subtask_retries=max_subtask_retries,
            subtask_success_check=subtask_success_check,
            max_plan_retries=max_plan_retries,
            persistence=persistence,
            spawn_config=spawn_config,
            ask_user=ask_user,
            max_questions=max_questions,
            ask_timeout=ask_timeout,
            memory=memory,
            memory_top_k=memory_top_k,
            memory_min_score=memory_min_score,
            memory_write=memory_write,
            ask_when_done=ask_when_done,
            follow_up=follow_up,
            max_follow_ups=max_follow_ups,
            planner_note=planner_note,
        )

    # -- I/O: sync calls; the shared coroutines never suspend ------------------------------------------

    def _call_model(self, messages: List[Dict[str, str]]) -> str:
        """One planner or synthesis call. In persistent mode a transient provider
        error (429, 5xx, timeout) is retried with backoff until the deadline."""
        patience = getattr(self, "_patience", None)
        if patience is None:
            return self.model.Initialize(messages=messages)
        return patience.patient(lambda: self.model.Initialize(messages=messages))

    async def _io_model(self, messages: List[Dict[str, str]]) -> str:
        return self._call_model(messages)

    async def _io_planning_task(self, user_task: str) -> str:
        return self._planning_task(user_task)

    async def _io_memory_block(self, query: str, stage: str) -> str:
        return self._memory_block(query, stage)

    async def _io_ask(self, question: str, why: str, source: str) -> None:
        self._operator.ask(question, why, source)

    async def _io_checkpoint(self, channel: OperatorChannel, text: str) -> Any:
        return channel.checkpoint(text)

    async def _io_remember(self, user_task: str, final: str) -> None:
        self._run_memory.remember_result(user_task, final)

    async def _plan_events(self, plan, subtask_results, results_by_id, budget):
        with closing(self._run_plan(plan, subtask_results, results_by_id, budget)) as events:
            for event in events:
                yield event

    def _filter_plan(self, plan: List[Any]) -> List[dict]:
        # Keep spawn steps AND known-agent steps. Unknown agents at this
        # stage might refer to a specialist a later spawn step will
        # create; validate order in run() rather than dropping them here.
        return [
            item for item in plan
            if isinstance(item, dict)
            and isinstance(item.get("agent"), (str, type(None)))   # a list/dict agent is unhashable
            and (item.get("agent") == "__spawn__" or item.get("agent") or item.get("query"))
        ]

    # -- running steps ---------------------------------------------------------------------------------

    def run_step(self, agent_name: str, query: str, run: Callable[[], SubtaskResult]) -> SubtaskResult:
        """Runs one step: ``run()`` dispatches it to ``agent_name`` (with retries) and returns its result.
        The default just calls it. Override it to wrap every step (time it, hold a lock, set up context)
        or to skip one by returning your own ``SubtaskResult`` without calling ``run``."""
        return run()

    def _dispatch_with_retry(
        self,
        agent_runner: AgentRunner,
        agent_name: str,
        sub_query: str,
        dispatched_query: str,
        budget: Optional[RunBudget] = None,
    ) -> SubtaskResult:
        """Run one sub-task, retrying up to ``max_subtask_retries`` times.

        Three kinds of failure are retried, each feeding context back into
        the query:
          - a RAISED exception (crash, unrecoverable tool error),
          - a returned completion whose ``outcome`` is not "done" (the
            specialist gave up: stuck, out of time, out of budget,
            iteration limit), with or without a success check, and
          - a returned "done" result that ``subtask_success_check``
            rejects (ran fine but produced nothing useful).

        Without a success check, a "done" result is accepted even if thin,
        since the Supervisor can't tell "terse but correct" from "wrong"
        on its own.

        The retry loop stops early when the shared run budget is spent (a
        result with outcome ``out_of_time`` or ``out_of_budget``): another
        attempt cannot help. ``budget`` is the shared ``RunBudget`` handed
        to specialists that accept one (persistent mode); ``None`` otherwise.

        On exhaustion: returns the last result with ``error`` set (content
        preserved) if we got one, else an empty errored SubtaskResult."""
        def call(query: str) -> Any:
            if budget is not None and accepts_budget(agent_runner.Initialize):
                return agent_runner.Initialize(query, _budget=budget)
            return agent_runner.Initialize(query)

        async def attempt(query: str) -> Any:
            return call(query)

        return _run_sync(self._attempts(attempt, agent_name, sub_query, dispatched_query))

    def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
        budget: Optional[RunBudget] = None,
    ):
        """Execute ``plan``: dispatch each step (spawning specialists, cascading
        failures, evaluating ``skip_when``) and yield ``dispatch`` / ``spawn`` /
        ``subtask_result`` events. Results are appended to the shared
        ``subtask_results`` / ``results_by_id`` so several rounds can build on
        one another."""
        plan = self.prepare_plan(plan)
        # 3.3: DAG mode fires when ANY step declares depends_on. Dep-free
        # plans keep byte-identical legacy semantics (plan order, every
        # step sees ALL prior results).
        dag_mode = _plan_uses_deps(plan)
        order = _topo_order(plan) if dag_mode else list(range(len(plan)))

        for step_idx in order:
            item = plan[step_idx]
            agent_name = item.get("agent")
            step_id = item.get("id") or f"step_{step_idx + 1}"
            step_deps = list(item.get("depends_on") or [])

            if agent_name == "__spawn__":
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

            # 3.3 failure cascade: a FAILED direct dependency (error set,
            # and not a mere condition-skip) skips this step. Condition-
            # skipped deps count as empty successes -- the step still runs.
            failed_dep = next(
                (d for d in step_deps
                 if d in results_by_id
                 and results_by_id[d].error
                 ), None,
            )
            if failed_dep is not None:
                sub_result = SubtaskResult(
                    agent=agent_name or "<none>",
                    query=item.get("query", ""),
                    content="",
                    error=(f"skipped: dependency '{failed_dep}' failed "
                           f"({results_by_id[failed_dep].error})"),
                    skipped=True,
                    step_id=step_id,
                    depends_on=step_deps,
                )
                if self.verbose:
                    print(f"{_C_ERROR}[supervisor.skip -> {agent_name}] "
                          f"dependency '{failed_dep}' failed{_C_RESET}")
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            # 3.3 conditional skip: evaluated against a direct dep's typed
            # output; fail-open. Does NOT cascade -- dependents treat this
            # step as an empty success.
            skip, reason = _evaluate_skip_when(
                item.get("skip_when"), results_by_id, step_deps, self.verbose,
            )
            if skip:
                sub_result = SubtaskResult(
                    agent=agent_name or "<none>",
                    query=item.get("query", ""),
                    content=f"skipped: {reason}",
                    skipped=True,
                    step_id=step_id,
                    depends_on=step_deps,
                )
                if self.verbose:
                    print(f"{_C_DISPATCH}[supervisor.skip -> {agent_name}] "
                          f"{reason}{_C_RESET}")
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            # Past the deadline: skip before spending a spawn on this step.
            if budget is not None and budget.expired():
                sub_result = SubtaskResult(
                    agent=_step_agent_name(item) or "<none>", query=item.get("query", ""), content="",
                    error="skipped: the time limit was reached",
                    outcome=OUTCOME_OUT_OF_TIME, skipped=True,
                    step_id=step_id, depends_on=step_deps,
                )
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            if item.get("new_agent"):
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

            if agent_name not in self.agents:
                if self.verbose:
                    print(f"{_C_ERROR}[supervisor.dispatch -> {agent_name}] "
                          f"UNKNOWN AGENT -- skipping{_C_RESET}")
                sub_result = SubtaskResult(
                    agent=agent_name or "<none>",
                    query=item.get("query", ""),
                    content="",
                    error=f"specialist '{agent_name}' not in registry (spawn may have been refused)",
                    step_id=step_id,
                    depends_on=step_deps,
                )
                subtask_results.append(sub_result)
                results_by_id[step_id] = sub_result
                yield {"type": "subtask_result", "result": sub_result,
                       "step": step_idx, "step_id": step_id}
                continue

            sub_query = item["query"]
            _, agent_runner = self.agents[agent_name]
            # Context routing: DAG mode threads DIRECT dependencies only
            # (explicit routing, focused context budget); legacy mode
            # threads everything prior, exactly as pre-3.3.
            if dag_mode:
                dep_results = [
                    results_by_id[d] for d in step_deps if d in results_by_id
                ]
                dispatched_query = _build_augmented_query(sub_query, dep_results)
            else:
                dispatched_query = _build_augmented_query(sub_query, subtask_results)
            dispatched_query = self._step_context(dispatched_query, sub_query)
            yield {"type": "dispatch", "agent": agent_name, "query": sub_query,
                   "step": step_idx, "step_id": step_id}
            if self.verbose:
                _log_dispatch(agent_name, sub_query)
            sub_result = self.run_step(agent_name, sub_query, lambda runner=agent_runner, name=agent_name,
                                       query=sub_query, dispatched=dispatched_query:
                                       self._dispatch_with_retry(runner, name, query, dispatched, budget))
            sub_result.step_id = step_id
            sub_result.depends_on = step_deps
            subtask_results.append(sub_result)
            results_by_id[step_id] = sub_result
            policy = self._spawn_run_policy
            if policy is not None and agent_name in policy.run.built:
                policy.run.finish(agent_name, sub_result.outcome, len(sub_result.content))
            yield from self._drain_spawn_events()
            yield {"type": "subtask_result", "result": sub_result,
                   "step": step_idx, "step_id": step_id}
            if self.verbose:
                _log_result(sub_result)

    # -- public API ----------------------------------------------------------

    def stream(self, user_task: str):
        """Yield structured events as the supervisor plans, dispatches,
        and synthesizes.

        Event shapes:
          - {"type": "plan_start"}
          - {"type": "plan",           "plan": <list of steps>}
          - {"type": "spawn",          "name": str, "origin": "plan" | "delegate", "tools": list,
                                        "dropped": list, "reused": str | None, "refused": str | None,
                                        "capabilities": list, "rerouted_from": None}
          - {"type": "delegate_result", "name": str, "outcome": str, "chars": int}
          - {"type": "question",       "source": "planner" | "supervisor" | <agent name>, "question": str, "context": str}
          - {"type": "answer",         "source": str, "answered": bool, "reason": None | str}
          - {"type": "memory",         "stage": "plan" | "step", "hits": int}
          - {"type": "dispatch",       "agent": str, "query": str, "step": int}
          - {"type": "subtask_result", "result": SubtaskResult, "step": int}
          - {"type": "replan",         "round": int, "unresolved": list, "plan": list}  (persistent)
          - {"type": "budget",         "reason": "time" | "cost"}  (persistent; before synthesis)
          - {"type": "synthesize_start"}
          - {"type": "final",          "content": str}   (once per round)
          - {"type": "checkpoint",     "round": int, "outcome": str}  (ask_when_done; before the question)
          - {"type": "round",          "round": int, "query": str}  (follow_up; starts each round after the first)
          - {"type": "completion",     "result": SupervisorResult}   (last)

        The last event is always ``completion`` so ``run()`` can build
        on top of this and callers who want live UI updates can consume
        the intermediate events. With ``ask_when_done`` / ``follow_up`` the
        stream covers every round and ends with one ``completion`` for the
        session.
        """
        yield from _iterate_sync(self._session_events(user_task))

    def run(self, user_task: str) -> SupervisorResult:
        """Plan, execute sub-tasks sequentially, and synthesize.

        Thin wrapper over ``stream()``. Consumes every event and returns
        the final ``SupervisorResult``. For live UI progress, use
        ``stream()`` directly.
        """
        result: Optional[SupervisorResult] = None
        for event in self.stream(user_task):
            if event["type"] == "completion":
                result = event["result"]
        assert result is not None, "supervisor.stream() must yield a completion event"
        return result




# ----------------------------------------------------------------------------
# Async Supervisor
# ----------------------------------------------------------------------------

class AsyncSupervisor(_SupervisorCore):
    """
    Asynchronous multi-agent supervisor.

    Like :class:`Supervisor` but runs sub-tasks concurrently via
    ``asyncio.gather``. Accepts both :class:`AsyncAgentRunner` and
    :class:`AgentRunner` instances (sync runners are wrapped in
    ``asyncio.to_thread``).
    """

    _SPAWN_ASYNC = True

    def __init__(
        self,
        model: BaseChatModel,
        agents: Dict[str, Tuple[str, Union[AgentRunner, AsyncAgentRunner]]],
        max_subtasks: int = 5,
        verbose: bool = True,
        sequential: bool = False,
        max_subtask_retries: int = 1,
        subtask_success_check: Optional[Callable[[SubtaskResult], Any]] = None,
        max_parallel: Optional[int] = None,
        max_plan_retries: int = 1,
        persistence: Optional[Persistence] = None,
        spawn_config: Optional[SpawnConfig] = None,
        ask_user: Any = None,
        max_questions: int = 3,
        ask_timeout: Optional[float] = None,
        memory: Any = None,
        memory_top_k: int = 4,
        memory_min_score: float = 0.2,
        memory_write: bool = True,
        ask_when_done: bool = False,
        follow_up: bool = False,
        max_follow_ups: int = 5,
        planner_note: str = "",
    ):
        """
        Args:
            model: LLM used for planning and synthesis.
            agents: Mapping of ``name -> (description, AgentRunner)`` or
                ``name -> Specialist`` (3.3).
            max_subtasks: Hard upper bound on the number of planned sub-tasks.
            verbose: Print colored progress markers for each stage.
            sequential: When True, sub-tasks run one at a time in plan
                order and each specialist receives the prior sub-tasks'
                findings as context (sugar for ``max_parallel=1`` with
                all-prior threading). Default False: dep-free plans run
                fully concurrent as before; plans WITH ``depends_on``
                (3.3) run as a DAG — independent steps concurrent, each
                step threaded ONLY its direct dependencies' results, and
                a step starts the moment its dependencies complete.
            max_subtask_retries: How many times a sub-task that RAISES is
                re-dispatched (with the prior error appended) before the
                Supervisor gives up on it. Default 1. See
                :class:`Supervisor` for the full rationale. Applies in
                both sequential and concurrent modes — each sub-task
                retries independently.
            subtask_success_check: Optional ``(SubtaskResult) -> bool | str``
                predicate that also retries a *returned* result the check
                rejects (ran fine but produced nothing useful). See
                :class:`Supervisor` for the contract. Applies per sub-task
                in both sequential and concurrent modes.
            max_parallel: (3.3) Cap on concurrently running sub-tasks.
                ``None`` (default) = unbounded. Useful under provider
                rate limits. ``sequential=True`` forces this to 1.
            max_plan_retries: (3.3) Replans after sanitization repairs;
                see :class:`Supervisor`.
            persistence: (3.5) Opt-in ``Persistence(...)``; see
                :class:`Supervisor`.
            spawn_config: (3.6) Lets the planner define new specialists inline
                (``new_agent``) and specialists ``delegate`` to fresh sub-agents,
                clipped to the config's ceiling. Spawned steps run in the same
                parallel batches as any other step. Same defaults as
                :class:`Supervisor` (on, with a safe ceiling, when ``persistence``
                is set and no config is given).
            ask_user: (3.6) Lets the planner and every agent ask the human operator
                for a fact the task leaves out. ``True`` uses the built-in asker
                (notebook input box, terminal, or the controlling terminal; headless
                means no answer). A function ``(question) -> str | None``, sync or
                ``async``, routes the question through your own channel (a chatbot, a
                websocket); a sync one runs in a worker thread. ``None`` (default) is
                off. No answer means the agent proceeds on a stated assumption.
            max_questions: (3.6) Questions per run, shared by the planner and all
                agents. Default 3.
            ask_timeout: (3.6) Seconds to wait for one answer. ``None`` lets the
                channel decide (no limit in a notebook or terminal; 300 s on the
                controlling-terminal path).
            memory: (3.6) Long-term memory: any vector store with ``add`` and ``search``
                (``VectorStore``, ``ChromaVectorStore``, ``QdrantVectorStore``,
                ``PgVectorStore``). A slow store is called off the event loop. The planner
                and every dispatched step get the few relevant stored items as a FROM
                MEMORY block; the Supervisor saves each operator answer and each completed
                run's final answer; an exact repeat of an answered question is answered
                from memory. ``None`` (default) is off.
            memory_top_k: Items per lookup (default 4; 0 turns recall off).
            memory_min_score: Matches below this score are dropped (default 0.2; about 0.5
                for OpenAI embeddings, about 0.1 for HashEmbeddings).
            memory_write: False makes the store read-only.
            ask_when_done: (3.6.2) When a round's answer is ready, the framework itself asks the
                operator "What should I do next?" through the same asker as ``ask_user`` (the
                answer is shown in the prompt) and returns the reply in ``result.next_step``
                (``None`` if nobody answered). Asked whatever the outcome. Needs ``ask_user``.
                The planner and the helpers are told the Supervisor asks it, so they do not.
            follow_up: (3.6.2) Run another round on the operator's reply, with the earlier
                results as context, until the operator replies blank or with a closing word
                (any of "done", "stop", "quit", "exit", "no", "nothing", "that's all", "that's
                it", "all done", "nope", "bye", "goodbye", "thanks", "thank you", "no thanks",
                "no thank you", "done thanks", "done thank you", "nothing thanks", "stop
                thanks"; case-insensitive; apostrophes (``thats all`` works), commas, extra spaces and trailing ``.``, ``!``, ``?`` ignored).
                Implies ``ask_when_done``. ``result.rounds`` lists every round.
            max_follow_ups: (3.6.2) Rounds allowed after the first (default 5). After the last
                one no question is asked.
        """
        self.sequential = sequential
        self.max_parallel = 1 if sequential else (
            max(1, int(max_parallel)) if max_parallel is not None else None
        )
        self._configure(
            model=model,
            agents=agents,
            max_subtasks=max_subtasks,
            verbose=verbose,
            max_subtask_retries=max_subtask_retries,
            subtask_success_check=subtask_success_check,
            max_plan_retries=max_plan_retries,
            persistence=persistence,
            spawn_config=spawn_config,
            ask_user=ask_user,
            max_questions=max_questions,
            ask_timeout=ask_timeout,
            memory=memory,
            memory_top_k=memory_top_k,
            memory_min_score=memory_min_score,
            memory_write=memory_write,
            ask_when_done=ask_when_done,
            follow_up=follow_up,
            max_follow_ups=max_follow_ups,
            planner_note=planner_note,
        )

    # -- I/O ---------------------------------------------------------------------------------------------

    async def _call_model(self, messages: List[Dict[str, str]]) -> str:
        """One planner or synthesis call. In persistent mode a transient provider
        error (429, 5xx, timeout) is retried with backoff until the deadline."""
        patience = getattr(self, "_patience", None)
        if patience is None:
            return await self._call_model_once(messages)
        return await patience.apatient(lambda: self._call_model_once(messages))

    async def _call_model_once(self, messages: List[Dict[str, str]]) -> str:
        """Invoke the planning/synthesis model, preferring an async method."""
        if hasattr(self.model, "async_initialize") and asyncio.iscoroutinefunction(
            self.model.async_initialize
        ):
            return await self.model.async_initialize(messages=messages)
        # Fall back to running the sync method in a thread.
        return await asyncio.to_thread(self.model.Initialize, messages)

    async def _io_model(self, messages: List[Dict[str, str]]) -> str:
        return await self._call_model(messages)

    async def _io_planning_task(self, user_task: str) -> str:
        return await self._aplanning_task(user_task)

    async def _io_memory_block(self, query: str, stage: str) -> str:
        return await self._amemory_block(query, stage)

    async def _io_ask(self, question: str, why: str, source: str) -> None:
        await self._operator.aask(question, why, source)

    async def _io_checkpoint(self, channel: OperatorChannel, text: str) -> Any:
        return await channel.acheckpoint(text)

    async def _io_remember(self, user_task: str, final: str) -> None:
        await asyncio.to_thread(self._run_memory.remember_result, user_task, final)

    async def _plan_events(self, plan, subtask_results, results_by_id, budget):
        async with aclosing(self._run_plan(plan, subtask_results, results_by_id, budget)) as events:
            async for event in events:
                yield event

    def _filter_plan(self, plan: List[Any]) -> List[dict]:
        # A step may name a registered agent, define one inline (new_agent), or use one an
        # earlier step defines; a legacy __spawn__ step carries no query.
        # Only string names count: a list or dict here would be unhashable.
        defined = {s["new_agent"].get("name") for s in plan
                   if isinstance(s, dict) and isinstance(s.get("new_agent"), dict)
                   and isinstance(s["new_agent"].get("name"), str)}
        defined |= {s.get("name") for s in plan
                    if isinstance(s, dict) and s.get("agent") == "__spawn__"
                    and isinstance(s.get("name"), str)}
        return [
            item for item in plan
            if isinstance(item, dict)
            and isinstance(item.get("agent"), (str, type(None)))   # a list/dict agent is unhashable
            and (
                item.get("agent") == "__spawn__"
                or (item.get("query") and (
                    isinstance(item.get("new_agent"), dict)
                    or (isinstance(item.get("agent"), str)
                        and (item.get("agent") in self.agents or item.get("agent") in defined))
                ))
            )
        ]

    # -- running steps ---------------------------------------------------------------------------------

    async def run_step(self, agent_name: str, query: str,
                       run: Callable[[], Awaitable[SubtaskResult]]) -> SubtaskResult:
        """Runs one step: ``await run()`` dispatches it to ``agent_name`` (with retries) and returns its
        result. The default just awaits it. Override it to wrap every step (hold a lock so some steps
        never overlap, set a context variable a sync specialist's thread inherits, time it) or to skip
        one by returning your own ``SubtaskResult`` without awaiting ``run``. It runs in the step's own
        task, so steps that may run at once (``max_parallel``) call it concurrently."""
        return await run()

    async def _step(self, agent_name: str, sub_query: str, prior_results: Optional[List[SubtaskResult]],
                    budget: Optional[RunBudget]) -> SubtaskResult:
        async def run() -> SubtaskResult:
            return await self._run_subtask(agent_name, sub_query, prior_results=prior_results, budget=budget)
        return await self.run_step(agent_name, sub_query, run)

    async def _run_subtask(
        self,
        agent_name: str,
        sub_query: str,
        prior_results: Optional[List[SubtaskResult]] = None,
        budget: Optional[RunBudget] = None,
    ) -> SubtaskResult:
        """Run one sub-task, retrying up to ``max_subtask_retries`` times.

        Three kinds of failure are retried, each feeding context back into
        the query: a RAISED exception, a returned completion whose
        ``outcome`` is not "done" (with or without a success check), and a
        returned "done" result that ``subtask_success_check`` rejects. On
        exhaustion the last result is returned with ``error`` set (content
        preserved).

        The retry loop stops early when the shared run budget is spent (a
        result with outcome ``out_of_time`` or ``out_of_budget``): another
        attempt cannot help. ``budget`` is the shared ``RunBudget`` handed
        to specialists that accept one (persistent mode); ``None`` otherwise."""
        _, runner = self.agents[agent_name]
        # In sequential mode the caller passes findings-so-far; in
        # concurrent mode there's nothing to thread and the specialist
        # runs on the plain query. Stored result uses the plain query
        # so the audit trail isn't polluted with the injected context.
        dispatched_query = await self._astep_context(
            _build_augmented_query(sub_query, prior_results)
            if prior_results else sub_query,
            sub_query,
        )
        if self.verbose:
            _log_dispatch(agent_name, sub_query)

        initialize = getattr(runner, "Initialize", None)
        if initialize is None:
            # Config error, not a transient failure — don't burn retries.
            result = SubtaskResult(
                agent=agent_name, query=sub_query, content="",
                error=f"Registered agent '{agent_name}' has no 'Initialize' method.",
            )
            if self.verbose:
                _log_result(result)
            return result
        async def attempt(query: str) -> Any:
            budget_kw = {"_budget": budget} if budget is not None and accepts_budget(initialize) else {}
            if asyncio.iscoroutinefunction(initialize):
                return await initialize(query, **budget_kw)
            return await asyncio.to_thread(initialize, query, **budget_kw)

        result = await self._attempts(attempt, agent_name, sub_query, dispatched_query)
        if self.verbose:
            _log_result(result)
        return result

    # -- public API ----------------------------------------------------------

    async def _run_plan(
        self,
        plan: List[dict],
        subtask_results: List[SubtaskResult],
        results_by_id: Dict[str, SubtaskResult],
        budget: Optional[RunBudget] = None,
    ):
        """Execute ``plan`` with the completion-driven scheduler and yield
        ``dispatch`` / ``subtask_result`` events. Results are appended to the
        shared ``subtask_results`` / ``results_by_id`` so several rounds can
        build on one another."""
        plan = self.prepare_plan(plan)
        # Emit a `dispatch` event for every step up-front so UIs can
        # render the plan as a "task list" before results start landing.
        for step_idx, item in enumerate(plan):
            if item.get("agent") == "__spawn__":
                continue                      # bookkeeping, not a dispatch
            yield {"type": "dispatch",
                   "agent": _step_agent_name(item), "query": item.get("query", ""),
                   "step": step_idx}

        # 3.3 unified completion-driven scheduler. Dependencies per step:
        #   - DAG mode (any step has depends_on): the parsed edges.
        #   - legacy sequential: every prior step (all-prior threading,
        #     identical context to pre-3.3) — with max_parallel=1 this
        #     reproduces the old sequential loop exactly.
        #   - legacy concurrent: no deps — everything runs at once, no
        #     threading, exactly the old asyncio.gather behavior.
        # Cascade + skip_when apply only in DAG mode (legacy plans never
        # skipped later steps on failure, and must not start now).
        dag_mode = _plan_uses_deps(plan)
        step_ids = [
            item.get("id") or f"step_{i + 1}" for i, item in enumerate(plan)
        ]
        if dag_mode:
            deps_of: List[List[str]] = [
                list(item.get("depends_on") or []) for item in plan
            ]
        elif self.sequential:
            deps_of = [step_ids[:i] for i in range(len(plan))]
        else:
            deps_of = [[] for _ in plan]

        done_ids: set = set()
        launched: set = set()
        running: Dict[asyncio.Task, int] = {}
        launched_as: Dict[int, str] = {}      # step index -> the agent it was launched on

        def _ready() -> List[int]:
            out = []
            for i in range(len(plan)):
                if i in launched:
                    continue
                if all(d in done_ids for d in deps_of[i] if d in step_ids):
                    out.append(i)
            return out

        def _record(i: int, r: SubtaskResult) -> SubtaskResult:
            r.step_id = step_ids[i]
            r.depends_on = deps_of[i]
            subtask_results.append(r)
            results_by_id[step_ids[i]] = r
            done_ids.add(step_ids[i])
            return r

        # Every scheduled task is owned by this try/finally. Without it a
        # BaseException out of a specialist (asyncio.CancelledError and
        # KeyboardInterrupt are NOT Exception, so _run_subtask's handler
        # never sees them) propagated out of t.result() and left the
        # sibling tasks running unowned -- in-flight LLM calls still
        # billing with nobody collecting the result. The same finally
        # covers a consumer that stops iterating this generator early.
        try:
            while len(done_ids) < len(plan):
                # Launch everything ready (respecting max_parallel), resolving
                # cascades and condition-skips inline — those complete
                # instantly without dispatching.
                progressed = False
                for i in _ready():
                    if dag_mode:
                        failed_dep = next(
                            (d for d in deps_of[i]
                             if d in results_by_id and results_by_id[d].error),
                            None,
                        )
                        if failed_dep is not None:
                            launched.add(i)
                            r = _record(i, SubtaskResult(
                                agent=plan[i].get("agent", "<none>"),
                                query=plan[i].get("query", ""),
                                content="",
                                error=(f"skipped: dependency '{failed_dep}' failed "
                                       f"({results_by_id[failed_dep].error})"),
                                skipped=True,
                            ))
                            yield {"type": "subtask_result", "result": r,
                                   "step": i, "step_id": step_ids[i]}
                            progressed = True
                            continue
                        skip, reason = _evaluate_skip_when(
                            plan[i].get("skip_when"), results_by_id,
                            deps_of[i], self.verbose,
                        )
                        if skip:
                            launched.add(i)
                            r = _record(i, SubtaskResult(
                                agent=plan[i].get("agent", "<none>"),
                                query=plan[i].get("query", ""),
                                content=f"skipped: {reason}",
                                skipped=True,
                            ))
                            yield {"type": "subtask_result", "result": r,
                                   "step": i, "step_id": step_ids[i]}
                            progressed = True
                            continue
                    if budget is not None and budget.expired():
                        launched.add(i)
                        r = _record(i, SubtaskResult(
                            agent=plan[i].get("agent", "<none>"),
                            query=plan[i].get("query", ""),
                            content="",
                            error="skipped: the time limit was reached",
                            outcome=OUTCOME_OUT_OF_TIME,
                            skipped=True,
                        ))
                        yield {"type": "subtask_result", "result": r,
                               "step": i, "step_id": step_ids[i]}
                        progressed = True
                        continue
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
                    if launch_agent not in self.agents:
                        r = _record(i, SubtaskResult(
                            agent=launch_agent or "<none>", query=plan[i].get("query", ""),
                            content="",
                            error=f"specialist '{launch_agent}' not in registry (spawn may have been refused)",
                        ))
                        yield {"type": "subtask_result", "result": r,
                               "step": i, "step_id": step_ids[i]}
                        progressed = True
                        continue
                    dep_results = [
                        results_by_id[d] for d in deps_of[i] if d in results_by_id
                    ]
                    task = asyncio.create_task(self._step(
                        launch_agent, plan[i]["query"], dep_results if dep_results else None, budget,
                    ))
                    running[task] = i
                    launched_as[i] = launch_agent
                    progressed = True

                if progressed and len(done_ids) >= len(plan):
                    break
                if not running:
                    if not progressed:
                        # Nothing running, nothing launchable: only possible if
                        # a dep id points at a step outside the plan (sanitizer
                        # prevents this) — bail rather than spin.
                        break
                    continue

                done, _ = await asyncio.wait(
                    running.keys(), return_when=asyncio.FIRST_COMPLETED,
                )
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
        finally:
            for _t in list(running):
                if not _t.done():
                    _t.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            running.clear()

    async def astream(self, user_task: str):
        """Async event stream. Same event shapes as ``Supervisor.stream``, including the
        ``checkpoint`` and ``round`` events of ``ask_when_done`` / ``follow_up``.

        Concurrent-dispatch mode (``sequential=False``) yields
        ``subtask_result`` events as each sub-task finishes -- so the
        UI sees whichever completes first, not the plan order.
        """
        async with aclosing(self._session_events(user_task)) as events:
            async for event in events:
                yield event

    async def run(self, user_task: str) -> SupervisorResult:
        """Plan, execute sub-tasks concurrently, and synthesize.

        Thin wrapper over ``astream()`` -- consumes every event and
        returns the final ``SupervisorResult``. Use ``astream()``
        directly if you want to render progress in a UI.
        """
        result: Optional[SupervisorResult] = None
        async for event in self.astream(user_task):
            if event["type"] == "completion":
                result = event["result"]
        assert result is not None, "AsyncSupervisor.astream must yield completion"
        return result
