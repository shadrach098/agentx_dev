"""Sub-agents demo (3.6): a Supervisor that creates its own helpers.

One registered specialist, ``explorer``, reads files in ./workspace. The task also
needs web research, which ``explorer`` cannot do, so the planner defines a new helper
inline (``new_agent``: its own instructions and the ``web`` tool). You set the ceiling
once with ``SpawnConfig(capabilities=...)``; nothing a plan asks for can exceed it.

What to look for in the output:

  - the plan the Supervisor wrote, including any ``new_agent`` step;
  - ``[spawn]`` lines when a helper is built (verbose=True);
  - ``result.spawned``: every helper it created, with its tools and outcome;
  - with ``--ask``: the planner asks you for the competitors (the task names none), and
    ``result.asked`` lists each question put to you;
  - with ``--memory FILE``: a vector store is loaded from FILE (created on the first run),
    the Supervisor saves what it learns into it, and the next run answers a repeated
    question from memory instead of asking you again. ``result.memory`` lists what a run saved.

Run (set ANTHROPIC_API_KEY or OPENAI_API_KEY first):

    python examples/subagents_demo.py
    python examples/subagents_demo.py "Your own task here"
    python examples/subagents_demo.py --persistent     # recovery rounds + one shared deadline
    python examples/subagents_demo.py --ask            # the planner asks you for the competitors
    python examples/subagents_demo.py --ask --memory memory.json   # the second run does not ask again
"""

import os
import sys
from pathlib import Path

from agentx_dev import (
    AgentRunner, AgentType, HashEmbeddings, Permissions, Persistence, SpawnConfig, Supervisor,
    VectorStore,
)

WORKSPACE = "./workspace"

DEFAULT_TASK = (
    "Read ./workspace/competitors.md (it names three competitors). Then look up each "
    "competitor's public pricing page on the web and compare their plans and prices in a "
    "short table, citing the page you used for each."
)

# Names no competitors: with --ask the Supervisor asks you for them instead of guessing.
ASK_TASK = "Compare the pricing pages of our three competitors."

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


def build_supervisor(model, persistent=False, ask=False, memory=None):
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
        ask_user=True if ask else None,   # built-in asker: notebook input box or terminal
        memory=memory,                    # a vector store (or None): long-term memory
        memory_min_score=0.1,             # HashEmbeddings scores are low; use ~0.5 with OpenAIEmbeddings
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


def load_memory(path):
    """The store kept in ``path``: loaded when the file exists, otherwise a new empty one."""
    embeddings = HashEmbeddings()          # offline; OpenAIEmbeddings() recalls better
    if Path(path).exists():
        return VectorStore.load(path, embeddings)
    return VectorStore(embeddings)


def parse_args(argv):
    """Return (flags, memory_file, positional). ``--memory`` takes the next argument."""
    memory_file = None
    positional = []
    flags = set()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--memory":
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise SystemExit("--memory needs a file name, for example: --memory memory.json")
            memory_file = argv[i + 1]
            i += 1
        elif arg.startswith("--"):
            flags.add(arg)
        else:
            positional.append(arg)
        i += 1
    return flags, memory_file, positional


def main(argv):
    flags, memory_file, args = parse_args(argv)
    persistent = "--persistent" in flags
    ask = "--ask" in flags
    task = args[0] if args else (ASK_TASK if ask else DEFAULT_TASK)

    seed_workspace()
    model = build_model()
    store = load_memory(memory_file) if memory_file else None
    supervisor = build_supervisor(model, persistent=persistent, ask=ask, memory=store)

    result = supervisor.run(task)
    if store is not None:
        store.save(memory_file)            # the store is yours to persist

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
    if ask:
        print("\nQuestions put to you this run:")
        if not result.asked:
            print("  (none: the planner did not need to ask)")
        for entry in result.asked:
            print(f"  {entry['source']:<12} answered={entry['answered']} {entry['question']}")
    if store is not None:
        print(f"\nSaved to memory ({memory_file}, {len(store)} item(s) in the store):")
        if not result.memory:
            print("  (nothing new this run)")
        for entry in result.memory:
            print(f"  {entry['kind']:<16} {entry['text'][:60]}")
    print(f"\nThe supervisor's own registry is unchanged: {list(supervisor.agents)}")


if __name__ == "__main__":
    main(sys.argv[1:])
