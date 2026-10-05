"""Sub-agents demo (3.6): a Supervisor that creates its own helpers.

One registered specialist, ``explorer``, reads files in ./workspace. The task also
needs web research, which ``explorer`` cannot do, so the planner defines a new helper
inline (``new_agent``: its own instructions and the ``web`` tool). You set the ceiling
once with ``SpawnConfig(capabilities=...)``; nothing a plan asks for can exceed it.

What to look for in the output:

  - the plan the Supervisor wrote, including any ``new_agent`` step;
  - ``[spawn]`` lines when a helper is built (verbose=True);
  - ``result.spawned``: every helper it created, with its tools and outcome.

Run (set ANTHROPIC_API_KEY or OPENAI_API_KEY first):

    python examples/subagents_demo.py
    python examples/subagents_demo.py "Your own task here"
    python examples/subagents_demo.py --persistent     # recovery rounds + one shared deadline
"""

import os
import sys
from pathlib import Path

from agentx_dev import (
    AgentRunner, AgentType, Permissions, Persistence, SpawnConfig, Supervisor,
)

WORKSPACE = "./workspace"

DEFAULT_TASK = (
    "Read ./workspace/competitors.md (it names three competitors). Then look up each "
    "competitor's public pricing page on the web and compare their plans and prices in a "
    "short table, citing the page you used for each."
)

SAMPLE_COMPETITORS = """# Competitors

- Notion
- Obsidian
- Coda
"""


def build_model():
    """Prefer Anthropic, fall back to OpenAI."""
    if os.getenv("ANTHROPIC_API_KEY"):
        from agentx_dev import Claude
        return Claude(model="claude-sonnet-4-6", max_tokens=2048)
    if os.getenv("OPENAI_API_KEY"):
        from agentx_dev import GPT
        return GPT(model="gpt-4o-mini", temperature=0)
    raise RuntimeError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY before running this demo.")


def build_explorer(model):
    """The specialist you register yourself: reads and lists files under ./workspace."""
    return AgentRunner(
        model=model,
        agent=AgentType.ReAct,
        tools=[],
        permissions=Permissions(
            read_files=True,
            list_directories=True,
            allowed_paths=[WORKSPACE],
            workspace=WORKSPACE,            # "/competitors.md" means ./workspace/competitors.md
        ),
        max_iterations=8,
        verbose=False,
        system_addendum=(
            "You read files in the workspace and report what is in them, quoting the "
            "relevant lines. You cannot browse the web."
        ),
    )


def build_supervisor(model, persistent=False):
    explorer = build_explorer(model)
    return Supervisor(
        model=model,
        agents={"explorer": ("Reads and lists files in ./workspace", explorer)},
        spawn_config=SpawnConfig(
            enabled=True,
            capabilities={"web", "files_read"},   # the ceiling: what a spawned helper may use
            allowed_paths=[WORKSPACE],            # its file sandbox
            max_spawns=6,                         # helpers per run, plans and delegations together
        ),
        persistence=Persistence(max_minutes=15) if persistent else None,
        verbose=True,
    )


def seed_workspace():
    """Create ./workspace/competitors.md on the first run so explorer has something to read."""
    folder = Path(WORKSPACE)
    folder.mkdir(exist_ok=True)
    target = folder / "competitors.md"
    if not target.exists():
        target.write_text(SAMPLE_COMPETITORS, encoding="utf-8")
        print(f"(created {target})")


def main(argv):
    persistent = "--persistent" in argv
    args = [a for a in argv if not a.startswith("--")]
    task = args[0] if args else DEFAULT_TASK

    seed_workspace()
    model = build_model()
    supervisor = build_supervisor(model, persistent=persistent)

    result = supervisor.run(task)

    print("\n" + "=" * 70)
    print("FINAL ANSWER")
    print("=" * 70)
    print(result.content)
    print(f"\noutcome: {result.outcome}")
    print("\nHelpers created this run:")
    if not result.spawned:
        print("  (none: the registered specialist handled everything)")
    for sub in result.spawned:
        print(f"  {sub['name']:<22} origin={sub['origin']:<8} tools={sub['tools']} "
              f"dropped={sub['dropped']} outcome={sub['outcome']} chars={sub['chars']}")
    print(f"\nThe supervisor's own registry is unchanged: {list(supervisor.agents)}")


if __name__ == "__main__":
    main(sys.argv[1:])
