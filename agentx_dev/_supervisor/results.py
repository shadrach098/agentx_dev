"""Result types: SubtaskResult, Specialist, SupervisorResult, and the agent registry helpers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field



class SubtaskResult(BaseModel):
    """Result from a single specialist sub-agent invocation.

    ``content`` is the human-readable answer text (always present).
    ``output`` carries the specialist's validated Pydantic instance when
    the underlying runner declared an ``output_schema`` — machine-readable
    structured data that downstream steps can consume without re-parsing
    prose. Stays ``None`` for schema-less specialists, so existing
    consumers of ``content`` are unaffected.

    3.3 additions (all optional; absent in legacy plans):
    ``step_id`` is the plan step's id, ``depends_on`` the ids it consumed,
    and ``skipped`` marks steps that never dispatched — either because a
    dependency failed (``error`` explains the cascade) or because a
    ``skip_when`` condition matched (``error`` is None; the reason is in
    ``content``).
    """

    agent: str
    query: str
    content: str
    error: Optional[str] = None
    output: Optional[Any] = None
    step_id: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)
    skipped: bool = False
    # How the specialist's run ended: "done", "stuck", "iteration_limit",
    # "out_of_time", "out_of_budget". Anything but "done" is a failed attempt.
    outcome: str = "done"
    # Persistent specialists attach their progress ledger (goal/done/failed/next).
    progress: Optional[Dict[str, Any]] = None
    # Set when a recovery step that "replaces" this failed step finished "done";
    # superseded failures no longer count.
    superseded: bool = False

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class Specialist:
    """Registry entry for one specialist (3.3).

    ``agents={}`` accepts either the classic ``(description, runner)``
    tuple or a ``Specialist``. Tuples are wrapped internally, so nothing
    breaks; ``Specialist`` adds planner-facing metadata:

    - ``depends_on``: names of specialists this one TYPICALLY follows.
      A hint rendered into the planning catalog — never an execution
      constraint (the same specialist can appear twice in one plan, so
      name-level deps are ambiguous at runtime; step-ids are not).
    - ``output_schema``: shown in the catalog so the planner can write
      ``skip_when`` conditions against real field names. Defaults from
      ``runner.output_schema`` when unset.
    - ``when_to_use``: extra routing guidance for the planner.

    Iterating a Specialist yields ``(description, runner)`` so existing
    tuple-unpacking call sites keep working unchanged.
    """

    description: str
    runner: Any
    depends_on: List[str] = field(default_factory=list)
    output_schema: Optional[type] = None
    when_to_use: str = ""

    def __post_init__(self):
        if self.output_schema is None:
            self.output_schema = getattr(self.runner, "output_schema", None)

    def __iter__(self):
        # Tuple-compat: `desc, runner = specialist` and
        # `for name, (desc, _) in agents.items()` both keep working.
        return iter((self.description, self.runner))



def _normalize_agents(agents: Dict[str, Any]) -> Dict[str, Specialist]:
    """Wrap classic ``(description, runner)`` tuples as ``Specialist``.
    Existing Specialist values pass through untouched."""
    out: Dict[str, Specialist] = {}
    for name, entry in (agents or {}).items():
        if isinstance(entry, Specialist):
            out[name] = entry
        else:
            desc, runner = entry
            out[name] = Specialist(description=desc, runner=runner)
    return out


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


class SupervisorResult(BaseModel):
    """Aggregate result produced by a Supervisor run."""

    query: str
    content: str  # final synthesized answer
    subtasks: List[SubtaskResult] = Field(default_factory=list)
    plan: List[dict] = Field(default_factory=list)  # the decomposed plan
    # "done" | "partial" | "stuck" | "out_of_time" | "out_of_budget"
    outcome: str = "done"
    # Sub-agents created during the run (planner-time spawns and delegations):
    # name, origin, tools, dropped, outcome, chars. Empty when nothing was spawned.
    spawned: List[Dict[str, Any]] = Field(default_factory=list)
    # Questions put to the operator (3.6): source, question, answered, reason, deduped.
    # Answer text is not kept here (it may be sensitive). Empty when ask_user is off.
    asked: List[Dict[str, Any]] = Field(default_factory=list)
    # What this run saved to long-term memory (3.6): kind, id, text (first 80 characters).
    # Empty when memory is off, read-only, or nothing qualified.
    memory: List[Dict[str, Any]] = Field(default_factory=list)
    # The operator's reply to "what should I do next?" (3.6.2), as typed; None when
    # ask_when_done is off or nobody answered.
    next_step: Optional[str] = None
    # One {"round", "query", "content", "outcome"} per round, filled only once a follow-up round
    # has run (follow_up=True). Empty for a single-round run.
    rounds: List[Dict[str, Any]] = Field(default_factory=list)
