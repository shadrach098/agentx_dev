"""Supervisor-only demo — codebase analysis with DYNAMIC SPAWNING.

Same task as before (analyze the agentx_dev source tree, produce a
Markdown report) but the INITIAL catalog registers ONLY:

  explorer   — read + list + find_files + grep, sandboxed to
               ./agentx_dev/. NO code execution, NO writes.
  reporter   — read/write in ./workspace. NO code execution.

Neither one can run Python. The task requires computing LOC totals,
counting `class` / `def` occurrences, and ranking files by line count
— things you really want to do in code. The Supervisor is expected
to notice the gap and SPAWN a specialist with code + files capability
mid-plan, then dispatch analysis work to it.

Spawning is gated by SpawnConfig:

  enabled=True          — turns the feature on. Off = no spawn ever.
  auto_spawn=True       — silent approval. Set False and the framework
                          prompts on stdin for each request; useful in
                          dev, not usable headlessly.
  approver=<callable>   — replace auto/stdin with your own policy.
  allowed_paths=[...]   — sandbox for spawned specialists.
  max_spawns=3          — runaway guard.

Watch the transcript for the ★[supervisor.spawn] auto-approving 'X'★
line — that's where the framework materializes a NEW AgentRunner
mid-plan from a capability list the planner emitted.

Run:
    python examples/supervisor_codebase_analysis_demo.py

Prereqs: ANTHROPIC_API_KEY or OPENAI_API_KEY in the environment.
"""

import os


from dotenv import load_dotenv
load_dotenv(r"C:\Users\bruce\Desktop\folder\AgentX\.env")

from agentx_dev import (
    AgentRunner, AgentType,
    Permissions,
)


TASK = (
    "Perform a deep analysis of this framework's Python source tree and "
    "produce a Markdown report named codebase_analysis.md. The report MUST include: "
    "\n\n"
    "(a) Count of Python files (recursive; skip __pycache__, .venv, dist, build, "
    "and hidden directories). "
    "\n"
    "(b) Total lines of code across those files, excluding blank lines and comments. "
    "\n"
    "(c) Top 10 files by line count as a table with columns 'path', 'lines', and "
    "'percentage_of_total'. "
    "\n"
    "(d) Count of `class ` definitions and `def ` definitions across the whole tree, "
    "broken down by file. "
    "\n"
    "(e) Any TODO / FIXME / README / XXX comments — list each with file:line and "
    "the actual comment text (or write 'none found' if there are none). "
    "\n"
    "(f) Detect duplicate function names across different files and report them "
    "in a table with 'function_name', 'file_paths'. "
    "\n"
    "(g) Identify the 5 largest classes (by number of methods) and list them with "
    "class name, file path, and method count. "
    "\n"
    "(h) Compute cyclomatic complexity for each function (using a tool like radon) "
    "and report the top 5 most complex functions with 'function_name', 'file_path', "
    "and 'complexity_score'. "
    "\n"
    "(i) Summarize external dependencies by parsing requirements.txt or setup.py "
    "and list them with version constraints. "
    "\n"
    "(j) Provide a final section 'Code Quality Observations' with any detected "
    "issues such as overly long functions (>100 lines), missing docstrings, or "
    "unused imports. "
    "\n"
    "(k) Write a 'Codebase Purpose & Summary' section that explains, in plain English, "
    "what the framework is about, what problem it solves, and its main components. "
    "This summary must be derived from analyzing README files, docstrings, and "
    "module names — not guesses. "
    "\n\n"
    "Every number must come from an actual tool call — no estimates. "
    "When saving, use write_file with if_exists='rename' so a prior run's report "
    "doesn't block this one."
)



def build_llm():
    if os.getenv("ANTHROPIC_API_KEY"):
        from agentx_dev import Claude
        return Claude(model="claude-sonnet-4-6", max_tokens=2048)
    if os.getenv("OPENAI_API_KEY"):
        from agentx_dev import GPT
        return GPT(model="gpt-4o-mini", temperature=0)
    raise RuntimeError(
        "Set ANTHROPIC_API_KEY or OPENAI_API_KEY before running this demo."
    )


# Distinct read + write scopes. Explorer is read-only on the source tree
# and can't touch the workspace at all. Analyst can read the source and
# both read+write the workspace (writes intermediate CSVs, computes on
# them, etc.). Reporter only touches the workspace.
SOURCE_TREE = "./agentx_dev"
WORKSPACE   = "./workspace"


def build_explorer(llm):
    """Read-only exploration of the source tree. AgentRunner auto-injects
    the sandbox description into the system prompt, so the addendum only
    covers ROLE (not paths)."""
    return AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        permissions=Permissions(
            read_files=True,
            list_directories=True,
            allowed_paths=[SOURCE_TREE],
            workspace=SOURCE_TREE,
        ),
        max_iterations=12,
        use_function_calling=True,
        verbose=False,
        system_addendum=(
            "You are a CODE EXPLORATION specialist. Use find_files for "
            "'which files match this glob', grep for 'which lines "
            "contain this pattern', read_path when you need a specific "
            "file's contents. Return structured findings — lists, "
            "tables, file:line hits — in your final message. The "
            "Supervisor cannot re-run your queries; only your reply "
            "is passed forward."
        ),
    )


# NOTE: no build_analyst() here — the analyst-equivalent gets SPAWNED
# by the Supervisor. Look at _build_spawned_agent in Supervisor.py to
# see the AgentRunner it materializes when the planner emits a
# __spawn__ step with capabilities=["code", "files"] — same shape
# as the explorer/reporter above, but constructed at run time from
# the capability list rather than hand-built up front.


def build_reporter(llm):
    """Writes the final Markdown deliverable."""
    return AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        permissions=Permissions(
            read_files=True,
            list_directories=True,
            write_files=True,
            edit_files=True,
            allowed_paths=[WORKSPACE],
            workspace=WORKSPACE,
        ),
        max_iterations=10,
        use_function_calling=True,
        verbose=False,
        system_addendum=(
            "You are a REPORT WRITER. Your deliverable is a completed "
            "write_file (or edit_file) tool call — NOT a message "
            "containing the report body. If the file already exists, "
            "retry with if_exists='rename'. After the write succeeds, "
            "name the actual saved path from the tool response and stop."
        ),
    )


def main():
    print("=" * 70)
    print("SUPERVISOR CODEBASE ANALYSIS DEMO (with dynamic spawning)")
    print("=" * 70)

    from agentx_dev.Supervisor import Supervisor, SpawnConfig, SpawnRequest

    llm = build_llm()

    # ★ Optional custom approver — logs each spawn request before
    # approving. Passing None (or auto_spawn=True) skips this. Shown
    # here so you can see the request shape the planner emits.
    def approver(req: SpawnRequest) -> bool:
        print(f"\n[approver] request name='{req.name}' "
              f"caps={req.capabilities} rationale={req.rationale!r}")
        print(f"[approver] description='{req.description}'")
        # Toy policy: approve any spawn that isn't asking for delete.
        # Real policies could inspect req.name against a whitelist,
        # cap per-run cost, log to an audit trail, prompt a human, etc.
        approved = "delete" not in req.capabilities
        print(f"[approver] verdict: {'APPROVED' if approved else 'REJECTED'}")
        return approved

    supervisor = Supervisor(
        model=llm,
        agents={
            # NOTE: no code-execution specialist here — that's the point.
            # The Supervisor's planner will realize it needs Python to
            # compute LOC / count definitions / rank files, and emit a
            # __spawn__ step to create one. Watch the trace for it.
            "explorer": (
                "Read-only exploration of ./agentx_dev source tree. "
                "Use for: find_files by glob, grep for patterns, "
                "read_path for specific file contents. NO writes, "
                "NO code execution.",
                build_explorer(llm),
            ),
            "reporter": (
                "Write / edit files in ./workspace only. Use for: "
                "producing the final Markdown deliverable, appending "
                "sections. NO code execution.",
                build_reporter(llm),
            ),
        },
        max_subtasks=8,
        verbose=True,
        spawn_config=SpawnConfig(
            enabled=True,
            # Flip to False and the approver callback below runs;
            # or drop `approver=` entirely and stdin gets prompted
            # (which refuses if there's no TTY — safe for CI).
            auto_spawn=False,
            approver=approver,
            # Sandbox for any spawned specialist that asks for files/code.
            # Both subtrees the analyst-like spawn will need are listed
            # so run_python can read the source and write the workspace.
            allowed_paths=[SOURCE_TREE, WORKSPACE],
            max_spawns=3,
        ),
    )

    result = supervisor.run(TASK)

    print("\n" + "-" * 70)
    print(
        f"Plan had {len(result.plan)} step(s); "
        f"{len(result.subtasks)} sub-task(s) completed."
    )
    # Enumerate spawns that actually landed. __spawn__ steps appear as
    # subtasks with agent='__spawn__' so callers can audit them.
    spawns = [r for r in result.subtasks if r.agent == "__spawn__"]
    if spawns:
        print(f"Spawn steps executed: {len(spawns)}")
        for s in spawns:
            print(f"  - {s.query}: "
                  f"{'ok' if not s.error else 'FAILED (' + s.error + ')'}")
    print("Deliverable expected under:", WORKSPACE)


if __name__ == "__main__":
    main()
