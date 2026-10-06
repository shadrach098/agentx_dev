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

from pydantic import BaseModel, Field

from agentx_dev import AsyncAgentRunner, AgentType, ask_human_tool
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


# --- approval gate ----------------------------------------------------
# ask_human_tool() prompts in the notebook's input box or the terminal and, when nobody can
# answer, returns a note that tells an agent to proceed on an assumption. That is right for
# a missing fact and wrong for an approval to WRITE labels. So the agent gets this wrapper:
# it returns APPROVED only for a typed "y" or "yes"; every other result, including no answer,
# is NOT APPROVED.

class _ApprovalArgs(BaseModel):
    question: str = Field(..., description="Question for the operator. Self-contained.")
    context: str = Field("", description="One-line status shown above the question.")


NOT_APPROVED = "NOT APPROVED: apply nothing and report that no approval was given."


def approval_tool(*, prompt_prefix: str = "[triager]") -> StructuredTool:
    asker = ask_human_tool(prompt_prefix=prompt_prefix)

    def _ask(question: str, context: str = "") -> str:
        try:
            reply = asker.func(question=question, context=context)
        except Exception:
            return NOT_APPROVED
        prefix = "[operator] "
        if isinstance(reply, str) and reply.startswith(prefix):
            if reply[len(prefix):].strip().lower() in ("y", "yes"):
                return "APPROVED"
        return NOT_APPROVED

    return StructuredTool(
        func=_ask, args_schema=_ApprovalArgs, name="ask_human",
        description=(
            "Ask the operator to approve the proposed label plan. Call ONCE after you have "
            "categorized every issue and have a full plan ready. Returns APPROVED only when "
            "the operator typed y or yes; then APPLY the labels by calling the MCP "
            "add_issue_labels tool for each issue. Any other result (NOT APPROVED, any other "
            "reply, no answer) means abort: return the plan as your final answer without "
            "writing anything."
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

5. Branch on the result of `ask_human`:
   - "APPROVED" -> for EACH issue, call the MCP
     `add_issue_labels` tool with owner, repo, issue_number, and
     labels=[type, priority]. Then emit Final_Answer summarizing what
     was written.
   - Anything else (including "NOT APPROVED") -> do NOT apply anything;
     emit Final_Answer with the plan and the note
     "Not applied -- no approval given."

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
            tools=[*picked, approval_tool(prompt_prefix="[triager]")],
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
