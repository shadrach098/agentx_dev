"""Multi-agent orchestration demo — three patterns, same complex task.

The TASK below is a real research job: fetch a homepage, search the
web for competitors, and write a Markdown report. It exercises three
distinct capabilities (web-search, web-fetch, filesystem), which is
enough to differentiate the three orchestration patterns.

PATTERN 1 (single-agent, tool-driven):
    One AgentRunner has ALL the tools — file ops, code exec, web
    search, web fetch. It does the whole task itself in one loop.
    Simplest pattern; the model juggles everything.

PATTERN 2 (orchestrator as ReAct agent + specialist tools):
    A top-level "orchestrator" agent has NO direct tools of its own —
    only three OTHER agents wrapped as StandardTools:

        file_agent        — filesystem specialist (list / delete /
                            write / edit / move — no code, no web)
        python_agent      — code specialist (Python execution +
                            read/write files — no web search)
        researcher_agent  — web specialist (search + fetch — no
                            filesystem, no code)

    The orchestrator decides which specialist to call for each
    sub-task, ONE AT A TIME, in a loop. It can adapt to intermediate
    results (re-plan on the next turn based on what came back).

PATTERN 3 (Supervisor: plan → dispatch → synthesize + DYNAMIC SPAWN):
    Uses the framework's built-in Supervisor. Different shape:
      1. ONE LLM call decomposes the whole task into a fixed plan up
         front.
      2. Each sub-task runs on the assigned specialist.
      3. ONE LLM call synthesizes the results into a final answer.

    ★ DYNAMIC SPAWNING: This demo registers ONLY file_agent and
    python_agent up front. The task needs web research — no
    registered agent can do that. With SpawnConfig(enabled=True,
    auto_spawn=True), the planner is told it can emit a special
    __spawn__ step to create a NEW specialist mid-plan by declaring
    a capability list ("web", "files", "code", "delete"). The
    framework builds the AgentRunner, registers it under the given
    name, and subsequent plan steps can dispatch to it.

    auto_spawn=True → framework silently approves each spawn request.
    auto_spawn=False → framework prompts on stdin for approval
    (unless a custom approver callback is supplied).

Run:
    python examples/orchestration_demo.py
"""

import os

from pydantic import BaseModel, Field

from agentx_dev import (
    AgentRunner, AgentType,
    Permissions, StandardTool, StructuredTool,
    web_search_tool, web_fetch_tool,   # for tasks that need web research
)


# All three patterns write into this shared workspace. When a file
# already exists, the model uses write_file(..., if_exists='rename')
# to save under a fresh name (report_1.md, report_2.md, ...). No
# per-run session subdirectories — the LLM plans confusingly around
# absolute paths in nested dirs. Flat + collision-safe is simpler.
WORKSPACE = "./workspace"


# Shared schema for the three "delegate to specialist" tools in Pattern 2.
# StructuredTool + Pydantic gives OpenAI a concrete parameter name to fill
# instead of the model guessing arbitrary keys, AND forces the model to
# think about what context the specialist needs to see — the specialist
# runs in ISOLATION and cannot see the orchestrator's history.
class DelegateInput(BaseModel):
    task: str = Field(
        ...,
        description="Natural-language description of the sub-task for the specialist.",
    )
    prior_findings: str = Field(
        "",
        description=(
            "REQUIRED when the sub-task depends on data produced by "
            "earlier tool calls: paste the actual URLs, values, text "
            "snippets, competitor names, whatever the specialist needs "
            "to see. The specialist runs in isolation — it has NO "
            "access to your history or to other specialists' outputs. "
            "If you leave this empty for a sub-task that needs prior "
            "data, the specialist will refuse and ask you for the "
            "information (wasting a round-trip). Only leave this empty "
            "for genuinely self-contained sub-tasks."
        ),
    )


TASK = (
    "Research VelteHub (https://veltehub.com) and its competitive landscape. "
    "Specifically: "
    "(a) Fetch the VelteHub homepage and pull the title, all internal links, "
    "and any email/phone patterns you find. "
    "(b) Search the web to find 3 direct competitors in the agency-management-"
    "software space. For each competitor: name, one-line positioning, and "
    "their homepage URL. "
    "(c) Save a Markdown report as report.md with: VelteHub's info at the top, "
    "then a competitor comparison table, then a short summary paragraph. "
    "If report.md already exists, call write_file with if_exists='rename' so "
    "it gets saved under a fresh name (report_1.md, etc.). "
    "(d) Report the actual saved report path and a two-sentence executive summary."
)


# ----------------------------------------------------------------------
# Model picker — prefer Anthropic (better agent behavior at Sonnet+),
# fall back to OpenAI. Set either env var before running.
# ----------------------------------------------------------------------

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


# ======================================================================
# PATTERN 1 — single agent with everything wired up
# ======================================================================

def run_single_agent():
    print("\n" + "=" * 70)
    print("PATTERN 1: single agent with all default tools")
    print("=" * 70)

    llm = build_llm()

    perms = Permissions(
        read_files=True,
        list_directories=True,
        write_files=True,
        edit_files=True,
        delete_files=True,
        execute_python=True,
        allowed_paths=[WORKSPACE],
        workspace=WORKSPACE,          # write-relative paths land here
        python_timeout_sec=20,
        python_max_output_bytes=20_000,
    )

    runner = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        permissions=perms,
        tools=[
            web_search_tool(),
            # cache_dir=WORKSPACE means fetched HTML lands on disk and
            # run_python can `open(path).read()` instead of pasting.
            web_fetch_tool(cache_dir=WORKSPACE),
        ],
        max_iterations=20,          # web research adds turns; give it room
        use_function_calling=True,  # cleaner tool dispatch than JSON-in-text
        verbose=True,
    )

    result = runner.invoke(TASK)

    print("\n" + "-" * 70)
    print("FINAL ANSWER:")
    print(result.content)
    print(f"\nTool calls made: {len(result.tool_calls)}")
    print(f"Steps taken:     {len(result.steps)}")
    print(f"Workspace:       {WORKSPACE}")


# ======================================================================
# PATTERN 2 — orchestrator delegates to two specialist agents
# ======================================================================

def build_file_specialist(llm):
    """File management only. No code execution."""
    return AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        permissions=Permissions(
            read_files=True,
            list_directories=True,
            write_files=True,
            edit_files=True,
            delete_files=True,
            allowed_paths=[WORKSPACE],
            workspace=WORKSPACE,
        ),
        max_iterations=15,
        use_function_calling=True,
        verbose=False,
        system_addendum=(
            "You are a FILE SPECIALIST. Your deliverable is a completed "
            "write_file / edit_file / delete_path / move_file tool call — "
            "NOT a message that describes the file's content. "
            "When asked to write a report or save content, you MUST call "
            "write_file with the full content. Never paste the content "
            "into your final message thinking that counts — it does not. "
            "The tool-call receipt IS the deliverable. If write_file "
            "refuses because a file exists, retry with if_exists='rename' "
            "(the tool will pick report_1.md, report_2.md, etc.) unless "
            "the task explicitly says to overwrite or edit. "
            "After the write_file call succeeds, your final answer should "
            "be short: name the actual saved path (from the tool response) "
            "and stop."
        ),
    )


def build_python_specialist(llm):
    """Code execution + file read/write inside the workspace."""
    return AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        permissions=Permissions(
            read_files=True,
            write_files=True,
            execute_python=True,
            allowed_paths=[WORKSPACE],
            workspace=WORKSPACE,
            python_timeout_sec=20,
            python_max_output_bytes=20_000,
        ),
        max_iterations=15,
        use_function_calling=True,
        verbose=False,
    )


def build_researcher_specialist(llm):
    """Web research only — no filesystem, no code execution. Fetches
    URLs, does DDG/Wikipedia searches, returns findings as text."""
    return AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[web_search_tool(), web_fetch_tool()],
        max_iterations=15,
        use_function_calling=True,
        verbose=False,
    )


def run_orchestrated():
    print("\n" + "=" * 70)
    print("PATTERN 2: orchestrator + specialist sub-agents")
    print("=" * 70)

    llm = build_llm()

    file_agent       = build_file_specialist(llm)
    python_agent     = build_python_specialist(llm)
    researcher_agent = build_researcher_specialist(llm)

    # The specialist sees `task` + any `prior_findings` the orchestrator
    # decided to pass. Concatenating with a clear header makes the
    # boundary obvious to the specialist model.
    def _combine(task: str, prior_findings: str) -> str:
        if prior_findings and prior_findings.strip():
            return (
                "PRIOR FINDINGS (context you should use — do NOT ask "
                f"the operator for data that's already here):\n\n{prior_findings}\n\n"
                f"===\n\nYOUR TASK NOW:\n{task}"
            )
        return task

    def delegate_to_file_agent(task: str, prior_findings: str = "") -> str:
        print(f"\n  [orchestrator -> file_agent] {task[:100]}...")
        return file_agent.invoke(_combine(task, prior_findings)).content

    def delegate_to_python_agent(task: str, prior_findings: str = "") -> str:
        print(f"\n  [orchestrator -> python_agent] {task[:100]}...")
        return python_agent.invoke(_combine(task, prior_findings)).content

    def delegate_to_researcher_agent(task: str, prior_findings: str = "") -> str:
        print(f"\n  [orchestrator -> researcher_agent] {task[:100]}...")
        return researcher_agent.invoke(_combine(task, prior_findings)).content

    # StructuredTool with an explicit DelegateInput schema — the model
    # sees a real "task" parameter in the function-calling spec and
    # can't invent arbitrary key names.
    file_tool = StructuredTool(
        func=delegate_to_file_agent,
        name="file_agent",
        description=(
            "Delegate a file-management sub-task (list, delete, move, edit, "
            "write files inside ./workspace). This specialist has NO memory "
            "of prior tool calls — when the sub-task is to WRITE a file "
            "using data from earlier steps, you MUST paste that data into "
            "prior_findings (the actual title, URLs, text, table rows). "
            "'Write a report' with empty prior_findings will fail."
        ),
        args_schema=DelegateInput,
    )
    python_tool = StructuredTool(
        func=delegate_to_python_agent,
        name="python_agent",
        description=(
            "Delegate a Python code sub-task (write a script, run it, "
            "parse HTML/JSON, do math, transform data). The specialist "
            "writes and executes the code and returns stdout. Does NOT "
            "have web-search access. Runs in ISOLATION — paste any prior "
            "data the code needs (URLs, HTML snippets, values) into "
            "prior_findings."
        ),
        args_schema=DelegateInput,
    )
    researcher_tool = StructuredTool(
        func=delegate_to_researcher_agent,
        name="researcher_agent",
        description=(
            "Delegate a WEB-RESEARCH sub-task — searching for information, "
            "finding URLs, reading pages, gathering competitor / market / "
            "background info. Give the question in `task` (e.g. 'Find 3 "
            "competitors of X and their homepage URLs'). This one is "
            "usually SELF-CONTAINED — leave prior_findings empty unless "
            "the search depends on data from an earlier step. Returns "
            "the findings as text. Cannot write files or run code."
        ),
        args_schema=DelegateInput,
    )

    # The orchestrator has NO direct file or code tools — only the
    # two specialists. This is what makes it an ORCHESTRATOR: it plans
    # who does what, doesn't do the work itself.
    orchestrator = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        tools=[file_tool, python_tool, researcher_tool],
        max_iterations=10,   # bumped from 6
        use_function_calling=True,
        verbose=True,
    )

    result = orchestrator.invoke(TASK)

    print("\n" + "-" * 70)
    print("FINAL ANSWER:")
    print(result.content)
    print(f"\nOrchestrator delegations: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"  -> {tc.name}: {tc.result[:120]}...")
    print(f"Workspace:  {WORKSPACE}")


# ======================================================================
# PATTERN 3 — Supervisor: plan up front, dispatch, synthesize
# ======================================================================

def run_supervised():
    """Uses the framework's built-in Supervisor class. Different shape
    from Pattern 2 — the LLM produces the WHOLE plan in one call up
    front, sub-tasks then execute (sequentially here; AsyncSupervisor
    runs them concurrently), and a final call synthesizes results."""
    print("\n" + "=" * 70)
    print("PATTERN 3: Supervisor (plan -> dispatch -> synthesize)")
    print("=" * 70)

    from agentx_dev.Supervisor import Supervisor, SpawnConfig

    llm = build_llm()

    # Only TWO specialists in the initial catalog — no researcher. The
    # task requires web search, which neither of these can do. The
    # Supervisor should decide to SPAWN a researcher specialist mid-plan
    # (that's the whole point of the demo — dynamic capability expansion).
    file_agent   = build_file_specialist(llm)
    python_agent = build_python_specialist(llm)

    supervisor = Supervisor(
        model=llm,
        agents={
            "file_agent": (
                "File management inside ./workspace: list, delete "
                "(supports recursive=True for non-empty dirs), write, "
                "edit, move files/directories. No code execution. "
                "No network access.",
                file_agent,
            ),
            "python_agent": (
                "Python code execution + read/write files inside "
                "./workspace. Can write scripts, run them, do math, "
                "parse data. NOTE: cannot search the web for open-ended "
                "queries (no search engine access).",
                python_agent,
            ),
        },
        max_subtasks=8,
        verbose=True,
        # ★ Dynamic spawning: let the planner create a NEW specialist
        # mid-plan if none of the registered agents has the capability
        # it needs. auto_spawn=True → silent approval; set to False and
        # the framework will prompt on stdin for each spawn request.
        # Capability keywords the spawn handler recognizes today:
        #   "web"    → web_search_tool + web_fetch_tool
        #   "files"  → read/write/edit/list + ./workspace sandbox
        #   "code"   → execute_python + read/write + sandbox
        #   "delete" → adds delete permission on top of "files"
        spawn_config=SpawnConfig(
            enabled=True,
            auto_spawn=True,
            allowed_paths=[WORKSPACE],   # spawned specialists write into the shared workspace
            max_spawns=3,
        ),
    )

    result = supervisor.run(TASK)

    # The verbose=True flag above already printed every stage as it
    # happened — plan, each dispatch, each result, the synthesized
    # final answer. If you want to work with the structured objects
    # after the fact (audit, log to a database, etc.) they're all on
    # result.plan / result.subtasks / result.content.
    print("\n" + "-" * 70)
    print(f"(Structured audit trail: plan has {len(result.plan)} steps, "
          f"{len(result.subtasks)} sub-tasks completed)")
    print(f"Workspace: {WORKSPACE}")


# ======================================================================
# Main
# ======================================================================

if __name__ == "__main__":
    # No manual mkdir needed — Permissions auto-creates its workspace
    # + allowed_paths on construction. Collisions on ./workspace/report.md
    # across runs are handled by write_file(if_exists='rename').

    # Run all three patterns so you can compare them.
    # Comment out whichever you don't want to run.
    run_single_agent()
    run_orchestrated()
    run_supervised()
