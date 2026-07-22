"""
try.py -- runnable eval suite.

Setup:
  export ANTHROPIC_API_KEY=sk-ant-...    # macOS / Linux
  $env:ANTHROPIC_API_KEY = "sk-ant-..."  # Windows PowerShell

Or use OPENAI_API_KEY if you don't have Anthropic -- the script
auto-detects and picks whichever is available.

Run:
  python try.py
"""

import os
import sys

from agentx_dev import (
    AgentRunner, AgentType, Claude, GPT,
    EvalCase, EvalRunner,
    llm_judge, contains,
)


# ---------------------------------------------------------------
# 1. Pick a model based on which key is set
# ---------------------------------------------------------------

if os.getenv("ANTHROPIC_API_KEY"):
    agent_model_name = "claude-sonnet-4-6"
    judge_model_name = "claude-haiku-4-5"    # cheap judge

    def make_agent_model():
        return Claude(model=agent_model_name, enable_prompt_cache=True)

    judge = Claude(model=judge_model_name)

elif os.getenv("OPENAI_API_KEY"):
    agent_model_name = "gpt-4o"
    judge_model_name = "gpt-4o-mini"

    def make_agent_model():
        return GPT(model=agent_model_name)

    judge = GPT(model=judge_model_name)

else:
    print("ERROR: set ANTHROPIC_API_KEY or OPENAI_API_KEY first.")
    sys.exit(1)


# ---------------------------------------------------------------
# 2. Runner factory -- called fresh per case by EvalRunner
# ---------------------------------------------------------------

def build_runner():
    return AgentRunner(
        model=make_agent_model(),
        agent=AgentType.ReAct,
        tools=[],                # no tools needed for a Q&A eval
        verbose=False,
        max_iterations=3,
    )


# ---------------------------------------------------------------
# 3. Your cases -- unchanged from the snippet you sent
# ---------------------------------------------------------------

cases = [
    EvalCase(
        name="helpful_response",
        input="Explain MVCC.",
        assertions=[
            llm_judge(judge, "The answer accurately explains MVCC."),
            llm_judge(judge, "The answer is under 150 words."),
        ],
    ),
]


# ---------------------------------------------------------------
# 4. Run + print
# ---------------------------------------------------------------

if __name__ == "__main__":
    print(f"Using agent={agent_model_name}, judge={judge_model_name}\n")
    # verbose=True inside EvalRunner already prints the report summary
    # at the end; don't double-print.
    report = EvalRunner(build_runner, verbose=True).run(cases)

    # Exit non-zero on any failure so it can be used as a CI gate.
    sys.exit(0 if report.failed == 0 else 1)
