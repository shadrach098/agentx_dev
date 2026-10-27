"""The planner's catalog of specialists, the plan checker, dependency order and skip conditions."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple


from agentx_dev.SubAgents import (
    SpecError,
    parse_agent_spec,
)
from agentx_dev._supervisor.results import (
    Specialist, SubtaskResult,
)
from agentx_dev._supervisor.steps import (
    _C_ERROR, _C_RESET,
)


_CATALOG_HIDDEN_TOOLS = frozenset({"ask_user", "delegate"})      # added by the framework, not the developer
_CATALOG_MAX_TOOLS = 12


def _catalog_tools_line(runner: Any) -> str:
    """``"    tools: a, b"`` for a registered agent, or ``""`` when it has none to show.

    The planner chooses between a registered agent and a new helper from this catalog, and it
    saw only the description. A developer who wrote "File management" had to add "writes and
    edits files" by hand so the planner would pick it; the agent's own tool list says so."""
    try:
        names = [n for n in runner.registry.names if n not in _CATALOG_HIDDEN_TOOLS]
    except Exception:
        return ""
    if not names:
        return ""
    shown = ", ".join(names[:_CATALOG_MAX_TOOLS])
    if len(names) > _CATALOG_MAX_TOOLS:
        shown += f", ... ({len(names) - _CATALOG_MAX_TOOLS} more)"
    return f"    tools: {shown}"


def _render_agent_catalog(agents: Dict[str, Any]) -> str:
    """Render the planner-facing catalog. Specialist entries (3.3) get
    their extra metadata lines; classic tuples render as before. Each agent's tools are
    listed under it."""
    lines: List[str] = []
    for name, entry in agents.items():
        tools_line = _catalog_tools_line(entry.runner if isinstance(entry, Specialist) else entry[1])
        if isinstance(entry, Specialist):
            lines.append(f"- {name}: {entry.description}")
            if tools_line:
                lines.append(tools_line)
            if entry.depends_on:
                lines.append(f"    typically after: {', '.join(entry.depends_on)}")
            if entry.output_schema is not None:
                try:
                    fields = ", ".join(entry.output_schema.model_fields.keys())
                    lines.append(f"    returns: {entry.output_schema.__name__}({fields})")
                except Exception:
                    lines.append(f"    returns: {getattr(entry.output_schema, '__name__', 'typed output')}")
            if entry.when_to_use:
                lines.append(f"    use when: {entry.when_to_use}")
        else:
            desc, _ = entry
            lines.append(f"- {name}: {desc}")
            if tools_line:
                lines.append(tools_line)
    return "\n".join(lines)


def _plan_repair_note(repairs: List[str]) -> str:
    """Feedback block appended to the planning prompt on a replan after
    sanitization had to fix the previous attempt's graph."""
    bullet = "\n".join(f"- {r}" for r in repairs)
    note = (
        "\n\nYOUR PREVIOUS PLAN HAD DEPENDENCY ERRORS that were auto-repaired:\n"
        f"{bullet}\n"
        "Write the plan again with a valid dependency graph: every "
        "depends_on entry must name an existing step id, no step may "
        "depend on itself or on a __spawn__ step, and the graph must "
        "contain no cycles."
    )
    if any("new_agent" in r for r in repairs):
        note += (
            " A new_agent needs a name (letters, digits, "
            "_ or -, at most 40 characters), non-empty instructions, tools as "
            "a list of names, and must not be combined with agent."
        )
    return note


def _plan_uses_deps(plan: List[dict]) -> bool:
    """True when ANY step declares ``depends_on`` — the DAG-mode switch.
    A dep-free plan keeps byte-identical legacy semantics."""
    return any(
        isinstance(step, dict) and step.get("depends_on")
        for step in plan
    )


def _sanitize_plan(
    plan: List[dict], verbose: bool = False, known_ids: Iterable[str] = (),
    replaceable: Optional[Iterable[str]] = None,
) -> Tuple[List[dict], List[str]]:
    """Normalize a planner-emitted plan into a valid DAG. Deterministic;
    never raises. Returns ``(plan, repairs)`` where ``repairs`` lists
    every fix made (empty = the plan was already clean). The list feeds
    the optional plan-repair replan loop.

    Rules, in order (see docs/design/3.3-depends-on-dag.md §3.1):
      1. auto-assign missing/duplicate ids as ``step_N`` (1-based position)
      2. drop depends_on entries naming unknown step ids
      3. drop self-dependencies
      4. break cycles by dropping the back-edge in plan order
      5. spawn steps cannot be depended on (such deps are dropped)
      6. (recovery plans, when ``replaceable`` is given) ``replaces`` keeps
         only ids of steps that are currently unresolved; anything else is
         dropped, and an empty or malformed ``replaces`` is removed
      7. (3.6, applied right after rule 1) a ``new_agent`` is validated with
         ``parse_agent_spec``: a valid one is normalized and names the step's
         ``agent``; an invalid one (bad fields, a reserved name, or beside a
         different ``agent``) is removed with a repair, and a step left with
         nothing to run is dropped. ``"new_agent": null`` counts as absent.

    ``known_ids`` are step ids finished in earlier recovery rounds; a
    dependency on one is valid and kept (it is simply not an edge in this
    plan's graph).
    """
    repairs: List[str] = []
    plan = [dict(step) for step in plan if isinstance(step, dict)]

    # -- 1. ids ------------------------------------------------------------
    seen: set = set()
    for i, step in enumerate(plan, 1):
        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip() or sid in seen:
            new_id = f"step_{i}"
            # Extremely defensive: if the auto-name itself collides with a
            # planner-chosen id, suffix until unique.
            while new_id in seen:
                new_id += "_"
            if sid in seen:
                repairs.append(f"duplicate id {sid!r} at position {i} renamed to {new_id!r}")
            step["id"] = new_id
        seen.add(step["id"])

    # -- 7. new_agent validation (3.6) ----------------------------------------
    # A valid definition is normalized and also names the step's agent; an invalid one is
    # removed, and a step left with nothing to run is dropped (dependents lose the edge below).
    checked: List[dict] = []
    for step in plan:
        if step.get("new_agent") is None:
            step.pop("new_agent", None)          # absent, or an explicit null: nothing to validate
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
        step["new_agent"] = {"name": spec.name, "instructions": spec.instructions, "tools": list(spec.tools),
                             **({"model": spec.model} if spec.model else {})}
        step["agent"] = spec.name
        checked.append(step)
    plan = checked

    ids = [s["id"] for s in plan]
    id_pos = {sid: i for i, sid in enumerate(ids)}
    spawn_ids = {s["id"] for s in plan if s.get("agent") == "__spawn__"}

    known = set(known_ids)

    # -- 2/3/5. dep validation ----------------------------------------------
    for step in plan:
        deps = step.get("depends_on") or []
        if not isinstance(deps, list):
            repairs.append(f"step {step['id']!r}: depends_on was not a list -- dropped")
            step["depends_on"] = []
            continue
        clean: List[str] = []
        for d in deps:
            if d == step["id"]:
                repairs.append(f"step {step['id']!r}: self-dependency dropped")
            elif d not in id_pos and d not in known:
                repairs.append(f"step {step['id']!r}: unknown dependency {d!r} dropped")
            elif d in spawn_ids:
                repairs.append(
                    f"step {step['id']!r}: dependency on spawn step {d!r} dropped "
                    f"(spawn steps are bookkeeping, not data producers)"
                )
            elif d not in clean:
                clean.append(d)
        step["depends_on"] = clean

    # -- 4. cycle breaking (Kahn's; drop the back-edge in plan order) -------
    while True:
        indeg = {sid: 0 for sid in ids}
        dependents: Dict[str, List[str]] = {sid: [] for sid in ids}
        for step in plan:
            for d in step["depends_on"]:
                if d not in id_pos:      # an earlier round's step: not part of this graph
                    continue
                indeg[step["id"]] += 1
                dependents[d].append(step["id"])
        queue = [sid for sid in ids if indeg[sid] == 0]
        visited = 0
        qi = 0
        while qi < len(queue):
            sid = queue[qi]; qi += 1
            visited += 1
            for dep_id in dependents[sid]:
                indeg[dep_id] -= 1
                if indeg[dep_id] == 0:
                    queue.append(dep_id)
        if visited == len(ids):
            break
        # Cycle exists. Among cyclic nodes, find the edge whose SOURCE is
        # latest in plan order and TARGET earliest — the back-edge — and
        # drop it. Plan order is the planner's own statement of intended
        # sequence, so the forward reading survives.
        cyclic = {sid for sid in ids if indeg[sid] > 0}
        back_edge = None   # (source_dep, step_id) — step depends_on source
        for step in plan:
            if step["id"] not in cyclic:
                continue
            for d in step["depends_on"]:
                if d in cyclic and id_pos[d] > id_pos[step["id"]]:
                    cand = (d, step["id"])
                    if back_edge is None or id_pos[d] > id_pos[back_edge[0]]:
                        back_edge = cand
        if back_edge is None:
            # Pure forward-edge cycle can't exist; belt-and-braces: drop
            # the first cyclic step's first dep so the loop terminates.
            for step in plan:
                if step["id"] in cyclic and step["depends_on"]:
                    back_edge = (step["depends_on"][0], step["id"])
                    break
        src, tgt = back_edge
        plan[[s["id"] for s in plan].index(tgt)]["depends_on"].remove(src)
        repairs.append(f"cycle broken: dropped dependency {src!r} from step {tgt!r}")

    # -- 6. replaces (recovery plans only) ----------------------------------
    if replaceable is not None:
        allowed = set(replaceable)
        for step in plan:
            if "replaces" not in step:
                continue
            raw = step.pop("replaces")
            if not isinstance(raw, list):
                repairs.append(f"step {step['id']!r}: replaces was not a list -- dropped")
                continue
            kept: List[str] = []
            for rid in raw:
                if rid in allowed:
                    if rid not in kept:
                        kept.append(rid)
                else:
                    repairs.append(f"step {step['id']!r}: replaces unknown or finished step {rid!r} -- dropped")
            if kept:
                step["replaces"] = kept

    if repairs and verbose:
        for r in repairs:
            print(f"{_C_ERROR}[supervisor.plan] repaired: {r}{_C_RESET}")
    return plan, repairs


def _topo_order(plan: List[dict]) -> List[int]:
    """Stable topological order over plan indices: a step never precedes
    its dependencies, and ties break by plan position. Assumes the plan
    has been through ``_sanitize_plan`` (acyclic, valid ids)."""
    ids = [s["id"] for s in plan]
    id_idx = {sid: i for i, sid in enumerate(ids)}
    # Dependencies outside this plan (a finished step from an earlier recovery
    # round) are already satisfied and are not part of this graph.
    indeg = [sum(1 for d in (s.get("depends_on") or []) if d in id_idx) for s in plan]
    dependents: List[List[int]] = [[] for _ in plan]
    for i, step in enumerate(plan):
        for d in step.get("depends_on") or []:
            if d in id_idx:
                dependents[id_idx[d]].append(i)

    import heapq
    ready = [i for i, deg in enumerate(indeg) if deg == 0]
    heapq.heapify(ready)
    order: List[int] = []
    while ready:
        i = heapq.heappop(ready)   # smallest plan index first → stable
        order.append(i)
        for j in dependents[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                heapq.heappush(ready, j)
    return order


def _evaluate_skip_when(
    cond: Any,
    results_by_id: Dict[str, SubtaskResult],
    step_deps: List[str],
    verbose: bool = False,
) -> Tuple[bool, str]:
    """Evaluate a step's ``skip_when`` condition against a DIRECT
    dependency's structured output. Returns ``(skip, reason)``.

    FAIL-OPEN by design: malformed condition, non-dep step reference,
    missing output, missing field, comparison error — every failure
    path returns ``(False, ...)`` and the step RUNS. A skip must be
    provably justified. Single operator: ``is`` (equality). Dotted
    field paths supported (``"meta.confidence"``).
    """
    if not isinstance(cond, dict):
        return False, ""
    ref = cond.get("step")
    fld = cond.get("field")
    if "is" not in cond or not isinstance(ref, str) or not isinstance(fld, str):
        return False, ""
    if ref not in step_deps:
        if verbose:
            print(f"{_C_ERROR}[supervisor.skip_when] step {ref!r} is not a "
                  f"direct dependency -- condition ignored (fail-open){_C_RESET}")
        return False, ""
    dep = results_by_id.get(ref)
    if dep is None or dep.output is None:
        return False, ""
    try:
        value: Any = dep.output
        for part in fld.split("."):
            if isinstance(value, dict):
                value = value[part]
            else:
                value = getattr(value, part)
        if value == cond["is"]:
            return True, f"{ref}.{fld} == {cond['is']!r}"
    except Exception:
        return False, ""
    return False, ""
