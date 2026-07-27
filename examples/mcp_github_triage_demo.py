"""GitHub issue triage via MCP.

What it does:
  1. Connects to the official MCP GitHub server (subprocess via npx).
  2. Lists the N most recent OPEN issues on shadrach098/agentx_dev.
  3. Has the model categorize each issue into a fixed taxonomy:
        bug / feature / docs / question / duplicate / needs-info
     plus a priority (P0/P1/P2).
  4. Prints the proposed labels + a one-line rationale per issue.
  5. HITL step (ask_human): operator sees the full plan and can
     approve -> labels get written to GitHub, or reject -> dry-run
     ends with nothing changed.

Prereqs:
  - Node + npx installed (the MCP GitHub server ships as an npm pkg).
  - GITHUB_PERSONAL_ACCESS_TOKEN in env -- token needs `repo` scope
    to read issues + write labels.
  - agentx-dev[mcp] installed:  pip install agentx-dev[mcp]
  - Anthropic key OR OpenAI key in env.

Run:
    python examples/mcp_github_triage_demo.py

The example is deliberately single-runner (not a Supervisor) so
you can follow the MCP -> runner path clearly. Once you get the
shape, splitting into (reader -> labeller) specialists with a
HandoffCoordinator is one more step.
"""

import asyncio
import os
import sys

from pydantic import BaseModel, Field

from agentx_dev import AsyncAgentRunner, AgentType
from agentx_dev.MCP import MCPClient
from agentx_dev.Tools import StructuredTool


# --- provider fallback (same pattern as the other demos) --------------

def build_llm():
    if os.environ.get("ANTHROPIC_API_KEY"):
        from agentx_dev import Claude
        return Claude(model="claude-sonnet-4-6", max_tokens=2048)
    if os.environ.get("OPENAI_API_KEY"):
        from agentx_dev import GPT
        return GPT(model="gpt-4o-mini", temperature=0)
    raise RuntimeError("Set ANTHROPIC_API_KEY or OPENAI_API_KEY before running.")


# --- human-in-the-loop tool (opens the controlling terminal directly) ---

class _AskHumanArgs(BaseModel):
    question: str = Field(..., description="Question for the operator. Self-contained.")
    context: str = Field("", description="One-line status shown above the question.")


def ask_human_tool(*, prompt_prefix: str = "[triager]") -> StructuredTool:
    """Approver prompt that talks to the controlling terminal via /dev/tty
    (POSIX) or CONIN$/CONOUT$ (Windows) instead of stdin/stdout. Works
    when the script was launched from an IDE run panel or through a
    subprocess wrapper. Returns a clear ERROR string only when there is
    genuinely no terminal (cron / Docker without -it)."""

    def _open_tty():
        try:
            if sys.platform == "win32":
                return open("CONIN$", "r"), open("CONOUT$", "w")
            return open("/dev/tty", "r"), open("/dev/tty", "w")
        except OSError:
            return None, None

    def _ask(question: str, context: str = "") -> str:
        tty_in, tty_out = _open_tty()
        if tty_in is None:
            return (
                "ERROR: no controlling terminal (headless run). Do NOT "
                "apply any labels; return the plan as your final answer."
            )
        banner = f"\n{prompt_prefix} \U0001F64B needs approval"
        if context:
            banner += f"\n  context: {context}"
        banner += f"\n  question: {question}\n  > "
        try:
            tty_out.write(banner)
            tty_out.flush()
            reply = tty_in.readline().strip()
        except (EOFError, KeyboardInterrupt):
            return "ERROR: operator declined; do NOT write any labels."
        finally:
            try: tty_in.close()
            except Exception: pass
            try: tty_out.close()
            except Exception: pass
        return f"[operator] {reply}" if reply else "(operator pressed Enter - treat as REJECT)"

    return StructuredTool(
        func=_ask, args_schema=_AskHumanArgs, name="ask_human",
        description=(
            "Ask the operator to approve the proposed label plan. Call ONCE "
            "after you've categorized every issue and have a full plan ready. "
            "If the operator types anything starting with 'y' or 'yes', "
            "APPLY the labels by calling the MCP add_issue_labels tool for "
            "each issue. Any other reply -- abort and return the plan as your "
            "final answer without writing anything."
        ),
    )


# --- config -----------------------------------------------------------

REPO_OWNER = "shadrach098"
REPO_NAME  = "agentx_dev"
LIMIT      = 10          # how many recent open issues to triage per run


TRIAGE_INSTRUCTIONS = f"""You are the ISSUE TRIAGER for {REPO_OWNER}/{REPO_NAME}.

WORKFLOW (in order -- do not skip steps):

1. Call the MCP `list_issues` tool with owner="{REPO_OWNER}", repo="{REPO_NAME}",
   state="open", per_page={LIMIT}. That gives you the issues to triage.

2. For EACH issue, classify along two axes:

   TYPE (pick exactly one, use these EXACT strings -- case-sensitive):
     - bug           -- something demonstrably broken (trace, wrong output, crash)
     - feature       -- request for new capability that doesn't exist
     - docs          -- doc gap, doc bug, missing example, typo
     - question      -- user asking how to do something (framework already supports it)
     - duplicate     -- restates an existing issue
     - needs-info    -- insufficient detail to act; asks for repro / version / trace

   PRIORITY (pick exactly one):
     - P0            -- data loss, security, or blocks all users (rare)
     - P1            -- real bug affecting a common path, or high-signal feature ask
     - P2            -- everything else

   Decide from the title + body. Do not fetch the issue again with a separate
   get_issue call if list_issues already returned the body.

3. Build one final PLAN as a markdown table with columns:
      # | title (truncated to 60 chars) | type | priority | one-line rationale

4. Call `ask_human` ONCE with question="Apply these labels? (y/N)" and
   context="Triaged N issues on {REPO_OWNER}/{REPO_NAME}. Full plan shown above."
   Include the plan table in your Thought BEFORE the ask_human call so it
   appears in the trace above the prompt when it fires.

5. Branch on the reply:
   - Starts with 'y' or 'yes' -> for EACH issue, call the MCP
     `add_issue_labels` tool with owner, repo, issue_number, and
     labels=[type, priority]. Then emit Final_Answer summarizing what
     was written.
   - Anything else -> emit Final_Answer with the plan and the note
     "Not applied -- operator rejected."

DO NOT:
- Invent new label names outside the taxonomy above.
- Write labels before ask_human returns approval.
- Skip issues silently -- every open issue in the fetched batch must
  appear in the plan, even if it's "needs-info".
"""


# --- main -------------------------------------------------------------

async def main():
    if not os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN"):
        raise RuntimeError(
            "Set GITHUB_PERSONAL_ACCESS_TOKEN in env. Needs `repo` scope "
            "to read issues + write labels. Create one at "
            "https://github.com/settings/tokens/new"
        )

    llm = build_llm()

    print("=" * 70)
    print(f"MCP GITHUB TRIAGE DEMO -- {REPO_OWNER}/{REPO_NAME}")
    print("=" * 70)
    print("Spawning MCP github server via npx (may take a few seconds first time)...")

    # connect_stdio spawns `npx -y @modelcontextprotocol/server-github`
    # as a subprocess and speaks JSON-RPC over its stdin/stdout.
    #
    # Env is scrubbed to only what the subprocess actually needs (GitHub
    # token + a few OS vars). Same defence as the framework's 3.0.6
    # `_build_child_env`: don't hand the child process arbitrary secrets
    # from the parent env.
    subprocess_env = {
        "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    }
    for k in ("PATH", "SystemRoot", "SYSTEMROOT", "HOME", "APPDATA",
              "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"):
        if k in os.environ:
            subprocess_env[k] = os.environ[k]

    async with await MCPClient.connect_stdio(
        "npx", "-y", "@modelcontextprotocol/server-github",
        env=subprocess_env,
    ) as mcp:
        mcp_tools = await mcp.list_tools()
        tool_names = sorted(t.name for t in mcp_tools)
        print(f"MCP tools advertised: {len(tool_names)}")
        print(f"  {', '.join(tool_names)}")

        # Whitelist only the tools the triager actually needs. The MCP
        # github server exposes ~26 tools including create_pull_request,
        # delete_file, fork_repository, etc. -- huge attack surface if
        # handed to the model wholesale. Reducing to 3 keeps ReAct
        # planning focused AND minimises blast radius on a prompt
        # injection that talked the model into reaching for `delete_file`.
        allowed = {"list_issues", "get_issue", "add_issue_labels"}
        picked = [t for t in mcp_tools if t.name in allowed]
        missing = allowed - {t.name for t in picked}
        if missing:
            raise RuntimeError(
                f"MCP github server missing expected tools: {missing}. "
                f"Advertised: {tool_names}"
            )
        print(f"Whitelisted for triager: {sorted(t.name for t in picked)}")

        runner = AsyncAgentRunner(
            model=llm,
            agent=AgentType.ReAct,
            tools=[*picked, ask_human_tool()],
            use_function_calling=True,
            verbose=True,
            max_iterations=LIMIT * 3 + 5,   # rough upper bound
            system_addendum=TRIAGE_INSTRUCTIONS,
        )

        result = await runner.ainvoke(
            f"Triage the {LIMIT} most recent open issues on "
            f"{REPO_OWNER}/{REPO_NAME}. Follow the workflow in your "
            f"system prompt exactly."
        )

    print("\n" + "=" * 70)
    print("TRIAGE RESULT")
    print("=" * 70)
    print(result.content)


if __name__ == "__main__":
    asyncio.run(main())
