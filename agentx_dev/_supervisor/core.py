"""What Supervisor and AsyncSupervisor share: sub-agent creation (_SpawnMixin) and the core that plans, retries, synthesizes and runs rounds (_SupervisorCore), and the driver that runs it without an event loop for the sync class."""

from __future__ import annotations

import json
from contextlib import aclosing
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


from agentx_dev.ChatModel import CostBudgetExceeded
from agentx_dev.Tools import logger
from agentx_dev.SubAgents import (
    DELEGATE_TOOL_NAME, AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpecError,
    attach_delegation, spawn_instruction,
    spec_from_legacy_spawn,
)
from agentx_dev.Operator import (
    ASK_TOOL_NAME, NO_ANSWER_PLAN_NOTE, PLANNER_SOURCE, OperatorChannel, ask_instruction,
    attach_ask_user, checkpoint_text, is_stop_reply, parse_ask_request, validate_ask_user,
)
from agentx_dev.SupervisorMemory import MEMORY_ASK_LINE, RunMemory, validate_memory
from agentx_dev.Runner.Persistence import (
    OUTCOME_DONE, OUTCOME_ITERATION_LIMIT, OUTCOME_OUT_OF_BUDGET, OUTCOME_OUT_OF_TIME, OUTCOME_STUCK,
    BudgetExpired, PersistentRun, RunBudget, apply_persistence, budget_event,
)
from agentx_dev._supervisor.results import (
    Specialist, SubtaskResult, SupervisorResult, _normalize_agents,
    _per_run_agents,
)
from agentx_dev._supervisor.prompts import (
    SUPERVISOR_PLAN_PROMPT, SUPERVISOR_SYNTHESIZE_PROMPT,
)
from agentx_dev._supervisor.plans import (
    _plan_repair_note, _render_agent_catalog, _sanitize_plan,
)
from agentx_dev._supervisor.steps import (
    _C_ERROR, _C_PLAN, _C_RESET, _augment_query_with_error,
    _augment_query_with_iteration_limit, _augment_query_with_unmet_criteria, _budget_outcome_of, _budget_reason,
    _cap_text, _evaluate_success_verdict, _follow_up_task, _format_results_block,
    _log_budget, _log_final, _log_no_plan, _log_plan,
    _log_replan, _outcome_line, _outcome_of, _progress_of,
    _recovery_note, _rename_colliding_ids, _resolve_replaced, _result_failed,
    _session_settings, _stopped_before_planning, _strip_code_fences, _supervisor_outcome,
    _unfinished_reason, _unresolved_note,
)


class _SpawnMixin:
    """Sub-agent creation shared by ``Supervisor`` and ``AsyncSupervisor`` (3.6). Needs
    ``self.agents``, ``self.model``, ``self.persistence``, ``self.spawn_config``,
    ``self.verbose``, ``self._spawn_run_policy``, ``self._operator``, ``self.memory``,
    ``self.memory_top_k``, ``self.memory_min_score``, ``self.memory_write`` and
    ``self._run_memory``."""

    _SPAWN_ASYNC = False        # AsyncSupervisor builds async sub-agents
    _operator: Optional[OperatorChannel] = None     # the per-run channel; None when ask_user is off
    _run_memory: Optional[RunMemory] = None         # the per-run memory; None when memory is off
    _synth_fell_back: bool = False                  # True when the last synthesis returned the budget stub

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
        yield from self._drain_operator_events()

    def _drain_operator_events(self):
        if self._operator is not None:
            yield from self._operator.drain()
        if self._run_memory is not None:
            yield from self._run_memory.drain()

    def _new_operator_channel(self) -> Optional[OperatorChannel]:
        channel = OperatorChannel.create(self.ask_user, max_questions=self.max_questions,
                                         timeout=self.ask_timeout, is_async=self._SPAWN_ASYNC,
                                         verbose=self.verbose)
        if channel is not None:
            # the Supervisor asks "what next?" itself: the planner and the helpers are told not to
            channel.session_checkpoint = bool(self.ask_when_done)
        return channel

    def _task_for_model(self, user_task: str) -> str:
        """The task as every model prompt sees it: the original plus any operator answers."""
        return self._operator.with_answers(user_task) if self._operator is not None else user_task

    def _with_answers(self, query: str) -> str:
        """``query`` with the operator's answers so far, for a dispatched step."""
        return self._operator.with_answers(query) if self._operator is not None else query

    def _asked_records(self) -> List[Dict[str, Any]]:
        return list(self._operator.records) if self._operator is not None else []

    def _new_run_memory(self) -> Optional[RunMemory]:
        return RunMemory.create(self.memory, top_k=self.memory_top_k, min_score=self.memory_min_score,
                                write=self.memory_write, verbose=self.verbose)

    def _memory_block(self, query: str, stage: str) -> str:
        """The FROM MEMORY block for ``query`` ("" when memory is off or nothing matches)."""
        return self._run_memory.recall(query, stage) if self._run_memory is not None else ""

    def _planning_task(self, user_task: str) -> str:
        """The task as a PLANNING prompt sees it: the original, any operator answers, and the
        memory block for the task. Synthesis keeps ``_task_for_model`` (no memory there)."""
        task = self._task_for_model(user_task)
        block = self._memory_block(user_task, "plan")
        return f"{task}\n\n{block}" if block else task

    def _step_context(self, dispatched_query: str, step_query: str) -> str:
        """A dispatched step's query with the operator answers and the memory block for the
        step's own query."""
        query = self._with_answers(dispatched_query)
        block = self._memory_block(step_query, "step")
        return f"{query}\n\n{block}" if block else query

    async def _amemory_block(self, query: str, stage: str) -> str:
        """Async twin of :meth:`_memory_block`; the store is called off the event loop."""
        return await self._run_memory.arecall(query, stage) if self._run_memory is not None else ""

    async def _aplanning_task(self, user_task: str) -> str:
        task = self._task_for_model(user_task)
        block = await self._amemory_block(user_task, "plan")
        return f"{task}\n\n{block}" if block else task

    async def _astep_context(self, dispatched_query: str, step_query: str) -> str:
        query = self._with_answers(dispatched_query)
        block = await self._amemory_block(step_query, "step")
        return f"{query}\n\n{block}" if block else query

    def _memory_records(self) -> List[Dict[str, Any]]:
        return list(self._run_memory.written) if self._run_memory is not None else []

    def _overlapping(self, built) -> Optional[str]:
        """Name of a registered specialist that already has every tool ``built`` has (log only)."""
        have = {getattr(t, "name", None) for t in getattr(built.runner, "tools", [])}
        have -= {DELEGATE_TOOL_NAME, ASK_TOOL_NAME, None}      # the framework gives these to every agent
        if not have:
            return None
        for n, (_d, runner) in self.agents.items():
            if n == built.spec.name:
                continue
            if have <= {getattr(t, "name", None) for t in getattr(runner, "tools", [])}:
                return n
        return None

    # -- public extension points (stable: see docs/reference/public-api.md) ---------------------------

    planner_note: str = ""      # text added to the end of the planner's catalog of specialists

    def prepare_plan(self, plan: List[dict]) -> List[dict]:
        """Called with every plan just before it runs (the first plan and each recovery plan, after the
        plan checker), and returns the plan to run. The default returns it unchanged. Override it to
        add ``depends_on`` edges, reorder steps, or note what the plan asks for; return a new list rather
        than editing steps in place if the caller's copy must stay as the planner wrote it."""
        return plan

    def spawn_specialist(self, spec: AgentSpec) -> Tuple[Optional[str], str]:
        """Build, reuse or refuse the specialist a plan step defines (``new_agent``; ``spec.model`` is
        the model it asked for, or ""). Return ``(name, "")`` once a specialist is registered in
        ``self.agents`` under ``name``, or ``(None, reason)`` to refuse (the step then fails with the
        reason). The default applies ``spawn_config``'s ceiling and runs the helper on this Supervisor's
        model. Override it to build helpers your own way; an override is called only when
        ``spawn_config.enabled`` is on."""
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

    def _spawn_spec(self, spec: AgentSpec) -> Tuple[Optional[str], str]:
        return self.spawn_specialist(spec)

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


class _SupervisorCore(_SpawnMixin):
    """What ``Supervisor`` and ``AsyncSupervisor`` share, written once: planning, replanning, synthesis,
    the retry loop of a step, and the rounds of a session. These are coroutines that reach the outside
    world only through the ``_io_*`` methods and ``_plan_events``: ``AsyncSupervisor`` awaits real work
    there, while ``Supervisor`` calls its sync code directly, so its coroutines never suspend and it runs
    them to the end without an event loop (``_run_sync``, ``_iterate_sync``), in the caller's thread."""

    def _configure(self, model, agents, max_subtasks, verbose, max_subtask_retries, subtask_success_check,
                   max_plan_retries, persistence, spawn_config, ask_user, max_questions, ask_timeout, memory,
                   memory_top_k, memory_min_score, memory_write, ask_when_done, follow_up, max_follow_ups,
                   planner_note) -> None:
        self.model = model
        # Normalize (and copy) so run-time spawns don't mutate the caller's dict. Accepts
        # (description, runner) tuples or Specialist entries (3.3); everything is stored as
        # Specialist, which still tuple-unpacks for older code.
        self.agents = _normalize_agents(agents)
        self.max_subtasks = max_subtasks
        self.verbose = verbose
        if spawn_config is None:
            # Persistent supervisors may create sub-agents out of the box, inside a safe
            # ceiling (web search plus read-only files). Without persistence the default is off.
            spawn_config = (
                SpawnConfig(enabled=True, capabilities={"web", "files_read"}, max_spawns=6)
                if persistence is not None else SpawnConfig(enabled=False)
            )
        self.spawn_config = spawn_config
        self._spawn_run_policy: Optional[SpawnPolicy] = None
        self.max_subtask_retries = max(0, int(max_subtask_retries))
        self.subtask_success_check = subtask_success_check
        self.max_plan_retries = max(0, int(max_plan_retries))
        self.persistence = persistence
        self.ask_user = validate_ask_user(ask_user, is_async=self._SPAWN_ASYNC)
        self.max_questions = max(0, int(max_questions))
        self.ask_timeout = ask_timeout
        self._operator: Optional[OperatorChannel] = None
        self.planner_note = str(planner_note or "")      # added to the planner's catalog of specialists
        self.follow_up, self.ask_when_done, self.max_follow_ups = _session_settings(
            self.ask_user, ask_when_done, follow_up, max_follow_ups)
        self.memory = validate_memory(memory)
        if int(memory_top_k) < 0:
            raise ValueError("memory_top_k must be >= 0")
        self.memory_top_k = int(memory_top_k)
        self.memory_min_score = float(memory_min_score)
        self.memory_write = bool(memory_write)
        self._run_memory: Optional[RunMemory] = None
        # Persistent runs only: waits out transient planner/synthesis errors until the deadline.
        self._patience: Optional[PersistentRun] = None

    # -- I/O: each class says how it talks to the model, the operator and the store -----------------

    async def _io_model(self, messages: List[Dict[str, str]]) -> str:
        raise NotImplementedError

    async def _io_planning_task(self, user_task: str) -> str:
        raise NotImplementedError

    async def _io_memory_block(self, query: str, stage: str) -> str:
        raise NotImplementedError

    async def _io_ask(self, question: str, why: str, source: str) -> None:
        raise NotImplementedError

    async def _io_checkpoint(self, channel: OperatorChannel, text: str) -> Any:
        raise NotImplementedError

    async def _io_remember(self, user_task: str, final: str) -> None:
        raise NotImplementedError

    def _plan_events(self, plan: List[dict], subtask_results: List[SubtaskResult],
                     results_by_id: Dict[str, SubtaskResult], budget: Optional[RunBudget]):
        """The events of running ``plan`` (an async generator over ``_run_plan``)."""
        raise NotImplementedError

    def _filter_plan(self, plan: List[Any]) -> List[dict]:
        """The planned steps worth keeping (before the plan checker)."""
        raise NotImplementedError

    # -- shared logic ---------------------------------------------------------------------------------

    def _evaluate_success(self, result: SubtaskResult) -> tuple:
        """Run ``subtask_success_check`` against a returned result.
        Returns ``(ok, reason)``. No check configured → always ``(True, "")``.
        A check that raises is swallowed (treated as pass) so a broken
        predicate can't wedge the run."""
        if self.subtask_success_check is None:
            return True, ""
        try:
            verdict = self.subtask_success_check(result)
        except Exception as e:
            logger.warning(f"subtask_success_check raised; treating as pass: {e}")
            return True, ""
        return _evaluate_success_verdict(verdict)

    def _build_agent_catalog(self) -> str:
        return _render_agent_catalog(self.agents) + (self.planner_note or "")

    async def _plan_once(self, user_task: str, repair_note: str = "", ask_allowed: bool = False) -> List[dict]:
        offer = ask_allowed and self._operator is not None and self._operator.remaining() > 0
        prompt = SUPERVISOR_PLAN_PROMPT.format(
            agent_catalog=self._build_agent_catalog(),
            user_task=await self._io_planning_task(user_task),
            max_subtasks=self.max_subtasks,
        )
        if self.spawn_config.enabled:
            prompt = prompt + spawn_instruction(self._spawn_policy())
        if offer:
            prompt = prompt + ask_instruction(self._operator.remaining())
            if await self._io_memory_block(user_task, "plan"):
                prompt = prompt + MEMORY_ASK_LINE
        if self._operator is not None:
            prompt = prompt + self._operator.plan_note()
        if repair_note:
            prompt = prompt + repair_note
        messages = [{"role": "user", "content": prompt}]
        response = await self._io_model(messages)

        cleaned = _strip_code_fences(response)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

        if offer and isinstance(parsed, dict) and not (parsed.get("plan") or []):
            questions = parse_ask_request(parsed.get("ask"), self._operator.remaining())
            if questions:
                for q in questions:
                    await self._io_ask(q["question"], q["why"], PLANNER_SOURCE)
                note = "" if self._operator.has_answers() else NO_ANSWER_PLAN_NOTE
                # Plan again on the task plus the answers; no ask option this time.
                return await self._plan_once(user_task, repair_note=repair_note + note)

        plan = parsed.get("plan", []) or []
        return self._filter_plan(plan)[: self.max_subtasks]

    async def _plan(self, user_task: str) -> List[dict]:
        """Plan, sanitize, and (3.3) replan once per repair budget when
        sanitization had to fix the graph. Returns a sanitized plan whose
        every step carries a valid ``id`` and acyclic ``depends_on``."""
        ask_kw = {"ask_allowed": True} if self._operator is not None else {}
        plan = await self._plan_once(user_task, **ask_kw)
        if not plan:
            return []
        sane, repairs = _sanitize_plan(plan, verbose=self.verbose)
        retries = self.max_plan_retries
        while repairs and retries > 0:
            retries -= 1
            retry_plan = await self._plan_once(user_task, repair_note=_plan_repair_note(repairs))
            if not retry_plan:
                break
            retry_sane, retry_repairs = _sanitize_plan(retry_plan, verbose=self.verbose)
            if len(retry_repairs) < len(repairs):
                sane, repairs = retry_sane, retry_repairs
            else:
                break   # retry was no better; keep the sanitized original
        return sane

    async def _plan_recovery(self, user_task: str, results: List[SubtaskResult], round_no: int) -> List[dict]:
        """Ask the planner for a recovery plan covering only the unfinished
        work. Returns ``[]`` when it cannot (the caller then synthesizes with
        what it has). A spent budget propagates so the caller can say so."""
        try:
            raw = await self._plan_once(user_task, repair_note=_recovery_note(results, round_no))
        except (CostBudgetExceeded, BudgetExpired):
            raise
        except Exception as e:
            logger.warning(f"recovery planning failed: {e}")
            return []
        if not raw:
            return []
        known = {r.step_id for r in results
                 if r.step_id and r.agent != "__spawn__" and not _result_failed(r) and not r.superseded}
        replaceable = {r.step_id for r in results if r.step_id and _result_failed(r)}
        sane, _ = _sanitize_plan(raw, verbose=self.verbose, known_ids=known, replaceable=replaceable)
        prior = {r.step_id for r in results if r.step_id}
        return _rename_colliding_ids(sane, prior, round_no)

    async def _synthesize(
        self,
        user_task: str,
        subtask_results: List[SubtaskResult],
        unresolved: Optional[List[SubtaskResult]] = None,
    ) -> str:
        self._synth_fell_back = False
        results_block = _format_results_block(subtask_results)
        prompt = SUPERVISOR_SYNTHESIZE_PROMPT.format(
            user_task=self._task_for_model(user_task),
            results_block=results_block,
        )
        if unresolved:
            prompt += _unresolved_note(unresolved)
        messages = [{"role": "user", "content": prompt}]
        try:
            return await self._io_model(messages)
        except (CostBudgetExceeded, BudgetExpired) as e:
            if self.persistence is None:
                raise
            self._synth_fell_back = True
            what = "cost budget" if isinstance(e, CostBudgetExceeded) else "time limit"
            return f"Stopped: the {what} was reached.\n\n{results_block}"

    async def _attempts(self, call: Callable[[str], Awaitable[Any]], agent_name: str, sub_query: str,
                        dispatched_query: str) -> SubtaskResult:
        """Run one sub-task through ``call(query)`` (the specialist), retrying up to
        ``max_subtask_retries`` times.

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
        attempt cannot help.

        On exhaustion: returns the last result with ``error`` set (content
        preserved) if we got one, else an empty errored SubtaskResult."""
        attempts = self.max_subtask_retries + 1
        query = dispatched_query
        last_error = ""
        last_result: Optional[SubtaskResult] = None
        for attempt in range(1, attempts + 1):
            try:
                completion = await call(query)
                result = SubtaskResult(
                    agent=agent_name, query=sub_query, content=completion.content,
                    # Preserve the runner's validated Pydantic instance so
                    # downstream steps get typed data, not just prose.
                    output=getattr(completion, "output", None),
                    outcome=_outcome_of(completion),
                    progress=_progress_of(completion),
                )
            except Exception as e:
                last_error = str(e)
                last_result = None
                if self.verbose:
                    print(
                        f"{_C_ERROR}[supervisor.retry <- {agent_name}] "
                        f"attempt {attempt}/{attempts} failed: {last_error}"
                        f"{_C_RESET}"
                    )
                if attempt < attempts:
                    query = _augment_query_with_error(
                        dispatched_query, last_error, attempt,
                    )
                continue

            # No exception — now apply the caller's success criteria.
            if result.outcome != OUTCOME_DONE:
                ok, reason = False, _unfinished_reason(result.outcome)
            else:
                ok, reason = self._evaluate_success(result)
            if ok:
                return result
            last_result = result
            last_error = f"did not meet success criteria: {reason}"
            if result.outcome in (OUTCOME_OUT_OF_TIME, OUTCOME_OUT_OF_BUDGET):
                break          # the shared budget is spent; another attempt cannot help
            if self.verbose:
                print(
                    f"{_C_ERROR}[supervisor.retry <- {agent_name}] "
                    f"attempt {attempt}/{attempts} rejected: {reason}"
                    f"{_C_RESET}"
                )
            if attempt < attempts:
                if result.outcome == OUTCOME_ITERATION_LIMIT:
                    query = _augment_query_with_iteration_limit(
                        dispatched_query, result.content, attempt,
                    )
                else:
                    query = _augment_query_with_unmet_criteria(
                        dispatched_query, reason, attempt,
                    )

        if last_result is not None:
            # Keep the content the specialist produced, but flag it failed.
            last_result.error = last_error
            return last_result
        return SubtaskResult(
            agent=agent_name, query=sub_query, content="", error=last_error,
        )

    async def _round_events(self, user_task: str, *, save_result: bool = True,
                            seed: Optional[OperatorChannel] = None):
        """One plan / dispatch / synthesize round, as a stream of events. ``save_result`` False
        keeps the round's answer out of long-term memory (a follow-up round, whose task is a
        composed one). ``seed`` is the previous round's operator channel, whose answers this
        round's channel starts with. The last event is this round's ``completion``."""
        self._spawn_run_policy = self._new_spawn_policy()
        budget = RunBudget.start(self.persistence.max_minutes) if self.persistence is not None else None
        self._spawn_run_policy.budget = budget        # delegate falls back to it (see SpawnPolicy)
        self._operator = self._new_operator_channel()
        self._spawn_run_policy.operator = self._operator
        if self._operator is not None:
            self._operator.budget = budget            # waiting on the operator pauses the deadline
        if self._operator is not None and seed is not None:
            self._operator.seed_from(seed)
        self._run_memory = self._new_run_memory()
        if self._operator is not None:
            self._operator.memory = self._run_memory
            if seed is not None and self._run_memory is not None:
                for question, _answer in seed._answers:       # OPERATOR ANSWERS already has these
                    self._run_memory.note_answered(question)
        self._patience = (PersistentRun(self.persistence, user_task, budget=budget, verbose=self.verbose)
                          if budget is not None else None)

        yield {"type": "plan_start"}
        try:
            plan = await self._plan(user_task)
        except (CostBudgetExceeded, BudgetExpired) as e:
            if self.persistence is None:
                raise
            stopped = _stopped_before_planning(user_task, e)
            stopped.asked = self._asked_records()
            stopped.memory = self._memory_records()
            for ev in self._drain_operator_events():
                yield ev
            spent = budget_event(stopped.outcome)
            yield spent
            if self.verbose:
                _log_budget(spent)
            yield {"type": "final", "content": stopped.content}
            yield {"type": "completion", "result": stopped}
            return
        for ev in self._drain_operator_events():
            yield ev

        if not plan:
            if self.verbose:
                _log_no_plan()
            result = SupervisorResult(
                query=user_task,
                content="Supervisor failed to produce a valid plan.",
                plan=[],
                subtasks=[],
                outcome=OUTCOME_STUCK,
                asked=self._asked_records(),
                memory=self._memory_records(),
            )
            yield {"type": "final", "content": result.content}
            yield {"type": "completion", "result": result}
            return

        yield {"type": "plan", "plan": plan}
        if self.verbose:
            _log_plan(plan)

        subtask_results: List[SubtaskResult] = []
        results_by_id: Dict[str, SubtaskResult] = {}
        budget_reason: Optional[str] = None
        with _per_run_agents(self), \
                attach_delegation([s.runner for s in self.agents.values()], self._spawn_policy()), \
                attach_ask_user({n: s.runner for n, s in self.agents.items()}, self._operator), \
                apply_persistence([s.runner for s in self.agents.values()], self.persistence):
            async with aclosing(self._plan_events(plan, subtask_results, results_by_id, budget)) as events:
                async for event in events:
                    yield event
            if self.persistence is not None:
                stagnant = 0
                round_no = 1
                while True:
                    unresolved = [r for r in subtask_results if _result_failed(r)]
                    if not unresolved:
                        break
                    budget_reason = _budget_reason(budget, subtask_results)
                    if budget_reason or stagnant >= self.persistence.max_replans:
                        break
                    try:
                        recovery = await self._plan_recovery(user_task, subtask_results, round_no + 1)
                    except (CostBudgetExceeded, BudgetExpired) as e:
                        budget_reason = _budget_outcome_of(e)
                        break
                    if not recovery:
                        break
                    round_no += 1
                    yield {"type": "replan", "round": round_no,
                           "unresolved": [r.step_id for r in unresolved], "plan": recovery}
                    if self.verbose:
                        _log_replan(round_no, unresolved)
                    plan.extend(recovery)
                    async with aclosing(self._plan_events(recovery, subtask_results, results_by_id,
                                                          budget)) as events:
                        async for event in events:
                            yield event
                    # Progress is an unresolved step resolved by a replacement that finished;
                    # a round that resolves nothing counts toward max_replans.
                    stagnant = 0 if _resolve_replaced(recovery, subtask_results) else stagnant + 1

        spent = budget_event(budget_reason)
        if spent is not None:
            yield spent
            if self.verbose:
                _log_budget(spent)
        for ev in self._drain_operator_events():
            yield ev
        yield {"type": "synthesize_start"}
        persistent = self.persistence is not None
        unresolved = [r for r in subtask_results if _result_failed(r)] if persistent else []
        shown = [r for r in subtask_results if not r.superseded] if persistent else list(subtask_results)
        final = await self._synthesize(user_task, shown, unresolved=unresolved)
        if self.verbose:
            _log_final(final)

        outcome = _supervisor_outcome(subtask_results, budget_reason)
        if (save_result and self._run_memory is not None and outcome == OUTCOME_DONE
                and not self._synth_fell_back and str(final).strip()):
            await self._io_remember(user_task, final)

        result = SupervisorResult(
            query=user_task, content=final,
            subtasks=list(subtask_results), plan=plan,
            outcome=outcome,
            spawned=list(self._spawn_policy().run.records),
            asked=self._asked_records(),
            memory=self._memory_records(),
        )
        yield {"type": "final", "content": final}
        yield {"type": "completion", "result": result}

    async def _session_events(self, user_task: str):
        """Every round of a session (one, unless ``ask_when_done`` / ``follow_up``), as events; the last
        is one ``completion`` for the session. Closing it early closes the round in progress too, so a
        running round's steps are stopped before it returns."""
        if not self.ask_when_done:
            async with aclosing(self._round_events(user_task)) as stream:
                async for event in stream:
                    yield event
            return
        rounds: List[Dict[str, Any]] = []
        asked: List[Dict[str, Any]] = []
        task, instruction, seed = user_task, user_task, None
        while True:
            number = len(rounds) + 1
            if number > 1:
                yield {"type": "round", "round": number, "query": instruction}
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.round] round {number}: {_cap_text(instruction, 120)}{_C_RESET}")
            result: Optional[SupervisorResult] = None
            async with aclosing(self._round_events(task, save_result=number == 1, seed=seed)) as stream:
                async for event in stream:
                    if event["type"] == "completion":
                        result = event["result"]
                    else:
                        yield event
            assert result is not None, "a round must yield a completion event"
            rounds.append({"round": number, "query": instruction, "content": result.content,
                           "outcome": result.outcome})
            asked.extend(result.asked)
            channel = self._operator
            if channel is None:                           # ask_user was cleared after construction: nobody to ask
                next_step = None
                break
            if self.follow_up and number > 1 and number == 1 + self.max_follow_ups:
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.round] follow-up limit reached{_C_RESET}")
                next_step = None
                break
            yield {"type": "checkpoint", "round": number, "outcome": result.outcome}
            before = len(channel.records)
            reply = await self._io_checkpoint(channel, checkpoint_text(
                result.content, _outcome_line(result), question_first=channel.builtin))
            asked.extend(channel.records[before:])
            for event in self._drain_operator_events():
                yield event
            next_step = reply.text
            more = (self.follow_up and number <= self.max_follow_ups
                    and result.outcome != OUTCOME_OUT_OF_BUDGET and not is_stop_reply(reply.text))
            if not more:
                break
            task, instruction, seed = _follow_up_task(user_task, rounds, reply.text), reply.text, channel
        yield {"type": "completion", "result": result.model_copy(update={
            "query": instruction, "asked": asked, "next_step": next_step,
            "rounds": rounds if len(rounds) > 1 else []})}

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__}(agents={list(self.agents.keys())}, "
            f"max_subtasks={self.max_subtasks})>"
        )


def _run_sync(awaitable: Any) -> Any:
    """Run a coroutine of a sync ``Supervisor`` to the end, in this thread, without an event loop. Its
    I/O is sync, so it never suspends; one that does (a coroutine awaiting real async work) is an error."""
    try:
        awaitable.send(None)
    except StopIteration as done:
        return done.value
    awaitable.close()
    raise RuntimeError("a sync Supervisor awaited async work; use AsyncSupervisor for async specialists")


def _iterate_sync(events: Any):
    """A sync generator over an async generator of a sync ``Supervisor`` (see ``_run_sync``). Closing it
    closes ``events``, so their ``finally`` blocks and context managers run as they would in ``for``."""
    try:
        while True:
            try:
                event = _run_sync(events.__anext__())
            except StopAsyncIteration:
                return
            yield event
    finally:
        _run_sync(events.aclose())
