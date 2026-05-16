"""
Chatbot example — multi-turn conversation with tool use and memory.

Demonstrates:
- Reusable AgentRunner across many turns (the stateless-per-call fix)
- Conversation memory carried turn-to-turn via ChatHistory
- A handful of useful tools (calculator, notes, time)
- Works with GPT or Claude — change one line

Run:
    export ANTHROPIC_API_KEY=sk-ant-...   # for Claude
    # or
    export OPENAI_API_KEY=sk-...          # for GPT
    python examples/chatbot_example.py
"""

from datetime import datetime
from pydantic import BaseModel
from agentx_dev import AgentRunner, AgentType, GPT, Claude, TokenLimitedMemory
from agentx_dev.Tools import StandardTool, StructuredTool


# ---------- Tools ----------

class CalcArgs(BaseModel):
    expression: str


def calculator(expression: str) -> str:
    """Evaluate a simple math expression (e.g. '23 * 17 + 4')."""
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))
    except Exception as e:
        return f"calc error: {e}"


# Simple in-memory note store — survives the chat session
_notes: list[str] = []


def save_note(note: str) -> str:
    """Save a short note for the user."""
    _notes.append(note)
    return f"Note saved (#{len(_notes)}): {note}"


def list_notes(_: str = "") -> str:
    """List all saved notes."""
    if not _notes:
        return "No notes saved yet."
    return "\n".join(f"{i + 1}. {n}" for i, n in enumerate(_notes))


def current_time(_: str = "") -> str:
    """Get the current date and time."""
    return datetime.now().strftime("%A, %B %d %Y at %H:%M")


TOOLS = [
    StructuredTool(
        func=calculator,
        args_schema=CalcArgs,
        name="calculator",
        description="Evaluate a math expression.",
    ),
    StandardTool(
        func=save_note,
        name="save_note",
        description="Save a short note for the user. Input: the note text.",
    ),
    StandardTool(
        func=list_notes,
        name="list_notes",
        description="List all previously saved notes. No input needed.",
    ),
    StandardTool(
        func=current_time,
        name="current_time",
        description="Get the current date and time. No input needed.",
    ),
]


# ---------- Chatbot ----------

def build_chatbot():
    # Swap models freely:
    model = Claude(model="claude-sonnet-4-6", max_tokens=1024)
    # model = GPT(model="gpt-4o", temperature=0.4)

    runner = AgentRunner(
        model=model,
        Agent=AgentType.ReAct,
        tools=TOOLS,
        max_iterations=6,
    )

    # Memory keeps the conversation under a token budget by dropping
    # the oldest non-system messages when needed.
    memory = TokenLimitedMemory(max_tokens=3000, preserve_system=True)

    return runner, memory


def chat_loop():
    runner, memory = build_chatbot()
    print("Chatbot ready. Type 'quit' to exit, '/notes' to see notes.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "/quit"):
            print("Bye.")
            break
        if user_input == "/notes":
            print(list_notes())
            continue

        # Pass prior conversation as ChatHistory — the runner adds the
        # current turn on top, executes, and returns a fresh history.
        result = runner.Initialize(
            user_input,
            ChatHistory=memory.get_messages(),
        )

        # Record this turn in memory for the next round
        memory.add_message("user", user_input)
        memory.add_message("assistant", result.content)

        print(f"\nBot: {result.content}\n")

        # Show what the bot used under the hood (optional)
        if result.tool_calls:
            tools_used = ", ".join(tc.name for tc in result.tool_calls)
            print(f"  [used: {tools_used}]\n")


# ---------- Scripted demo (no real LLM, runs without API keys) ----------

def scripted_demo():
    """Same chatbot, but driven by a mock model so it runs offline."""
    import json
    from agentx_dev.ChatModel import BaseChatModel

    class ScriptedModel(BaseChatModel):
        def __init__(self, turns):
            self._turns = iter(turns)

        def Initialize(self, messages) -> str:
            return next(self._turns)

    # Pre-scripted responses for a 3-turn conversation:
    scripted = [
        # Turn 1: user asks for the time
        json.dumps({"Thought": "Use current_time tool.", "action": "current_time", "action_input": ""}),
        json.dumps({"Thought": "Done.", "action": "Final_Answer", "action_input": "It's the current time (see tool output)."}),

        # Turn 2: user asks to compute something
        json.dumps({"Thought": "Use calculator.", "action": "calculator", "action_input": {"expression": "23 * 17 + 4"}}),
        json.dumps({"Thought": "Done.", "action": "Final_Answer", "action_input": "23 * 17 + 4 = 395."}),

        # Turn 3: user asks to save a note
        json.dumps({"Thought": "Use save_note.", "action": "save_note", "action_input": "Pick up milk on Friday"}),
        json.dumps({"Thought": "Done.", "action": "Final_Answer", "action_input": "I saved your note about picking up milk on Friday."}),
    ]

    runner = AgentRunner(
        model=ScriptedModel(scripted),
        Agent=AgentType.ReAct,
        tools=TOOLS,
        max_iterations=4,
    )
    memory = TokenLimitedMemory(max_tokens=3000)

    fake_turns = [
        "What time is it?",
        "What's 23 times 17 plus 4?",
        "Save a note: pick up milk on Friday.",
    ]

    for user_input in fake_turns:
        print(f"You: {user_input}")
        result = runner.Initialize(user_input, ChatHistory=memory.get_messages())
        memory.add_message("user", user_input)
        memory.add_message("assistant", result.content)
        print(f"Bot: {result.content}")
        if result.tool_calls:
            print(f"  [used: {', '.join(tc.name for tc in result.tool_calls)}]")
        print()

    print("Saved notes at end of conversation:")
    print(list_notes())


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        scripted_demo()       # offline, no API key needed
    else:
        chat_loop()           # interactive — needs an API key
