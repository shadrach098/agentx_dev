"""End-to-end demo of the function-calling feature.

Runs three scenarios:

  1. ``llm.with_structured_output(Receipt)`` — direct Pydantic extraction from text.
  2. ``AgentRunner(..., use_function_calling=True)`` with the ReAct agent calling
     a real tool (multiply) and returning a final answer, all via tool-calling.
  3. Showing the raw ``to_openai_tool`` / ``to_anthropic_tool`` specs that get
     sent over the wire.

If ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY`` is set in the environment, the
demo uses the real provider. Otherwise it falls back to ``StubModel`` — an
in-script BaseChatModel that scripts what a well-behaved LLM would return,
so the demo still executes end-to-end with zero credentials.

Run:
    python examples/function_calling_demo.py
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from agentx_dev.Agents.Agent import AgentType, React_, to_openai_tool, to_anthropic_tool
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Tools import StructuredTool


# ---------- Stub model used when no API key is available ----------------------

class StubModel(BaseChatModel):
    """Pretends to be an LLM. Scripts a tool-use response per call.

    Each call to ``call_with_tools`` pops one entry off ``scripted``. Entries
    are either ``{"type": "tool_use", "name": ..., "input": ...}`` (the model
    decided to call a tool) or ``{"type": "text", "text": ...}`` (the model
    decided to return free text).
    """

    def __init__(self, scripted: List[Dict[str, Any]]):
        self._scripted = iter(scripted)

    def Initialize(self, messages) -> str:  # required by ABC
        raise NotImplementedError("StubModel is for tool-use scenarios only")

    def call_with_tools(self, messages, tools, *, force_tool: Optional[str] = None):
        # Echo what the framework sends so the demo prints it.
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "<no user message>",
        )
        print(f"   [stub] forced_tool={force_tool!r}, last_user_msg={last_user[:80]!r}")
        return next(self._scripted)


# ---------- Demo data ---------------------------------------------------------

class Receipt(BaseModel):
    """A parsed receipt extracted from raw OCR text."""
    merchant: str
    total: float
    currency: str = "USD"


class MultiplyArgs(BaseModel):
    """Inputs to the multiply tool."""
    a: int
    b: int


def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


# ---------- Provider selection ------------------------------------------------

def pick_llm() -> tuple[BaseChatModel, BaseChatModel, str]:
    """Return (llm_for_structured_output, llm_for_agent, label).

    Two separate model instances because the stub mode needs different
    scripted responses for the two scenarios.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        from agentx_dev.ChatModel import Claude
        m1 = Claude(model="claude-haiku-4-5-20251001", max_tokens=512)
        m2 = Claude(model="claude-haiku-4-5-20251001", max_tokens=512)
        return m1, m2, "Claude (live API)"

    if os.getenv("OPENAI_API_KEY"):
        from agentx_dev.ChatModel import GPT
        m1 = GPT(model="gpt-4o-mini", temperature=0)
        m2 = GPT(model="gpt-4o-mini", temperature=0)
        return m1, m2, "GPT (live API)"

    # Stub mode — script what a sensible LLM would do.
    structured_stub = StubModel([
        {"type": "tool_use", "name": "Receipt", "input": {
            "merchant": "Blue Bottle Coffee", "total": 4.5, "currency": "USD",
        }},
    ])
    agent_stub = StubModel([
        # Iteration 1: model decides to call multiply(6, 7)
        {"type": "tool_use", "name": "React_", "input": {
            "Thought": "I need to multiply 6 and 7. I'll call the multiply tool.",
            "action": "multiply",
            "action_input": {"a": 6, "b": 7},
        }},
        # Iteration 2: model sees the result and returns the final answer
        {"type": "tool_use", "name": "React_", "input": {
            "Thought": "The result is 42. I'll return that.",
            "action": "Final_Answer",
            "action_input": "42",
        }},
    ])
    return structured_stub, agent_stub, "StubModel (no API key — scripted responses)"


# ---------- Scenarios ---------------------------------------------------------

def scenario_1_structured_output(llm: BaseChatModel):
    print("\n" + "=" * 70)
    print("Scenario 1: llm.with_structured_output(Receipt, method='function_calling')")
    print("=" * 70)

    extractor = llm.with_structured_output(Receipt, method="function_calling")

    ocr_text = "Blue Bottle Coffee — Latte — $4.50"
    print(f"\n  Input OCR text: {ocr_text!r}")
    print(f"  Calling extractor.invoke(...)")

    receipt = extractor.invoke(
        f"Extract the receipt fields from this text. Respond by calling the "
        f"Receipt tool.\n\nText: {ocr_text}"
    )

    print(f"\n  Result type:    {type(receipt).__name__}")
    print(f"  Result fields:  merchant={receipt.merchant!r}, total={receipt.total}, "
          f"currency={receipt.currency!r}")
    print(f"  isinstance(receipt, Receipt) -> {isinstance(receipt, Receipt)}")


def scenario_2_agent_runner(llm: BaseChatModel):
    print("\n" + "=" * 70)
    print("Scenario 2: AgentRunner(use_function_calling=True) with ReAct + multiply")
    print("=" * 70)

    tool = StructuredTool(
        func=multiply,
        args_schema=MultiplyArgs,
        name="multiply",
        description="Multiply two integers and return the product.",
    )
    runner = AgentRunner(
        model=llm,
        Agent=AgentType.ReAct,
        tools=[tool],
        use_function_calling=True,
        max_iterations=4,
    )

    user_query = "What is 6 multiplied by 7? Use the multiply tool."
    print(f"\n  User query:   {user_query!r}")
    print(f"  Running agent (forces React_ as the response tool every iteration)…")

    result = runner.invoke(user_query)

    print(f"\n  Final answer: {result.content!r}")
    print(f"  Steps taken:  {len(result.steps)}")
    for i, step in enumerate(result.steps, 1):
        print(f"    {i}. {step}")
    print(f"  Tool calls recorded: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"    - {tc.name}({tc.args}) -> {tc.result}")


def scenario_3_show_schemas():
    print("\n" + "=" * 70)
    print("Scenario 3: Schema helpers — same Pydantic class, two provider formats")
    print("=" * 70)

    print("\n  to_openai_tool(Receipt):")
    print(json.dumps(to_openai_tool(Receipt), indent=4))

    print("\n  to_anthropic_tool(Receipt):")
    print(json.dumps(to_anthropic_tool(Receipt), indent=4))

    print("\n  AgentType.ReAct.to_openai_tool()  (the agent's structured-response schema):")
    print(json.dumps(AgentType.ReAct.to_openai_tool(), indent=4))


def main():
    structured_llm, agent_llm, label = pick_llm()
    print(f"Using: {label}")

    scenario_1_structured_output(structured_llm)
    scenario_2_agent_runner(agent_llm)
    scenario_3_show_schemas()

    print("\n" + "=" * 70)
    print("Done.")
    print("=" * 70)


if __name__ == "__main__":
    main()
