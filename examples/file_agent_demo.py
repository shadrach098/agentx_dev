"""A complete walk-through of the agentx_dev framework with three tools.

Tools defined here:
    1. read_path  — read a file's contents, or list a folder's entries
    2. save_file  — write (or append to) a file
    3. run_python — execute a Python snippet in a fresh subprocess

We wire them into an AgentRunner with ``use_function_calling=True``. The
ReAct prompt drives the loop, but instead of the LLM emitting JSON inside
its text reply, the LLM calls the ``React_`` tool with structured args
(Thought / action / action_input). The runner dispatches whatever
``action`` the model picks to the matching tool below.

How the loop reads end-to-end:

    user_input ──► AgentRunner.Initialize()
                        │
                        ▼
            ┌──────────────────────────────┐
            │  build system prompt         │
            │  (lists tools + user query)  │
            └──────────────────────────────┘
                        │
                        ▼            ┌──────── React_ schema ────────┐
            ┌──────────────────────────┐                              │
            │ model.call_with_tools(    ◄────── forces this tool ─────┘
            │   messages, [React_])     │
            └──────────────────────────┘
                        │  returns {Thought, action, action_input}
                        ▼
            ┌──────────────────────────┐
            │ action == "Final_Answer"?│ ── yes ──► return content
            └──────────────────────────┘
                        │ no
                        ▼
            ┌──────────────────────────┐
            │ Tool_Runner(action, args)│ ── invokes your Python fn
            └──────────────────────────┘
                        │   tool result appended to history
                        └──► loop until Final_Answer or max_iterations

Run:
    python examples/file_agent_demo.py

If ANTHROPIC_API_KEY or OPENAI_API_KEY is set, a real Claude/GPT call is
made. Otherwise a scripted StubModel plays the role of the LLM so you
can see the loop execute end-to-end with no credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agentx_dev.Agents.Agent import AgentType
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Tools import StructuredTool


# ----------------------------------------------------------------------
# Step 1 — define each tool as (schema + function)
# ----------------------------------------------------------------------
#
# StructuredTool wraps a Pydantic schema (so the LLM knows the param
# names and types) and a Python function (the actual work). The runner
# validates the model's args against the schema before calling the
# function, so a missing/mistyped field surfaces as an error string
# rather than blowing up your code.


class ReadPathArgs(BaseModel):
    """Read a file's full contents, or list a folder's immediate entries."""
    path: str = Field(..., description="Filesystem path. May be a file or a directory.")


def read_path(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return f"ERROR: path does not exist: {path}"
    if p.is_dir():
        entries = sorted(child.name for child in p.iterdir())
        if not entries:
            return f"(empty directory: {path})"
        return "Directory contents of " + str(p) + ":\n" + "\n".join(f"  - {e}" for e in entries)
    # File
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: file is not utf-8 text: {path}"


class SaveFileArgs(BaseModel):
    """Write (or append) text content to a file."""
    path: str = Field(..., description="Destination file path. Parent dirs are created as needed.")
    content: str = Field(..., description="Text to write.")
    mode: str = Field("overwrite", description="'overwrite' (default) or 'append'.")


def save_file(path: str, content: str, mode: str = "overwrite") -> str:
    if mode not in ("overwrite", "append"):
        return f"ERROR: mode must be 'overwrite' or 'append', got {mode!r}"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    flag = "w" if mode == "overwrite" else "a"
    with open(p, flag, encoding="utf-8") as f:
        n = f.write(content)
    return f"wrote {n} chars to {p} (mode={mode})"


class RunPythonArgs(BaseModel):
    """Execute a Python snippet in a fresh subprocess and capture its output."""
    code: str = Field(..., description="Python source code to execute.")


def run_python(code: str) -> str:
    # Run in a fresh interpreter so the agent can't poison this process,
    # and so import state doesn't bleed across calls.
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "ERROR: code execution timed out after 10s"

    parts = []
    if proc.stdout:
        parts.append(f"--- stdout ---\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"--- stderr ---\n{proc.stderr.rstrip()}")
    if proc.returncode != 0:
        parts.append(f"(exit code {proc.returncode})")
    return "\n".join(parts) if parts else "(no output)"


# ----------------------------------------------------------------------
# Step 2 — wrap each function as a StructuredTool the runner can dispatch
# ----------------------------------------------------------------------

read_tool = StructuredTool(
    func=read_path,
    args_schema=ReadPathArgs,
    name="read_path",
    description="Read a file's contents, or list a directory's entries. Args: path.",
)

save_tool = StructuredTool(
    func=save_file,
    args_schema=SaveFileArgs,
    name="save_file",
    description="Write or append text to a file. Args: path, content, mode ('overwrite'|'append').",
)

run_tool = StructuredTool(
    func=run_python,
    args_schema=RunPythonArgs,
    name="run_python",
    description="Execute a Python snippet in a subprocess and return stdout/stderr. Args: code.",
)


# ----------------------------------------------------------------------
# Step 3 — model selection: real provider if key set, else scripted stub
# ----------------------------------------------------------------------
#
# The stub model below pretends to be a well-behaved LLM. Each time the
# runner calls call_with_tools(), the stub pops the next entry off its
# scripted list and returns it as if the model had produced it.
#
# This is exactly the shape a real Claude/GPT response takes after the
# framework normalizes it — so the runner can't tell the difference.

class StubModel(BaseChatModel):
    def __init__(self, scripted: List[Dict[str, Any]]):
        self._scripted = iter(scripted)

    def Initialize(self, messages) -> str:
        raise NotImplementedError("StubModel only supports call_with_tools")

    def call_with_tools(self, messages, tools, *, force_tool: Optional[str] = None):
        # Show which step the stub is on so the demo trace is clear.
        try:
            response = next(self._scripted)
        except StopIteration:
            raise RuntimeError("Stub ran out of scripted responses — agent looped further than expected.")
        if response["type"] == "tool_use":
            print(f"   [stub LLM] returning tool_use: action={response['input'].get('action')!r}")
        else:
            print(f"   [stub LLM] returning text: {response['text']!r}")
        return response


def build_model(sandbox: Path):
    """Pick a real model if a key is available, else a scripted stub."""

    if os.getenv("ANTHROPIC_API_KEY"):
        from agentx_dev.ChatModel import Claude
        return Claude(model="claude-haiku-4-5-20251001", max_tokens=1024), "Claude (live API)"

    if os.getenv("OPENAI_API_KEY"):
        from agentx_dev.ChatModel import GPT
        return GPT(model="gpt-4o-mini", temperature=0), "GPT (live API)"

    # No key — script a sensible sequence that exercises all three tools.
    greeting_path = sandbox / "greeting.py"
    code_body = "print('Hello from the agent!')"
    scripted = [
        # Iter 1 — write the file
        {"type": "tool_use", "name": "React_", "input": {
            "Thought": (
                "The user wants me to create greeting.py, run it, and report the output. "
                "Step 1: save the file using save_file."
            ),
            "action": "save_file",
            "action_input": {
                "path": str(greeting_path),
                "content": code_body,
                "mode": "overwrite",
            },
        }},
        # Iter 2 — read it back so we (and the user) can see what was written
        {"type": "tool_use", "name": "React_", "input": {
            "Thought": "Step 2: confirm what's on disk by reading the file back with read_path.",
            "action": "read_path",
            "action_input": {"path": str(greeting_path)},
        }},
        # Iter 3 — execute the code
        {"type": "tool_use", "name": "React_", "input": {
            "Thought": "Step 3: execute the snippet with run_python and capture its stdout.",
            "action": "run_python",
            "action_input": {"code": code_body},
        }},
        # Iter 4 — return the final answer
        {"type": "tool_use", "name": "React_", "input": {
            "Thought": "I have everything I need. Returning the captured stdout as the final answer.",
            "action": "Final_Answer",
            "action_input": "The script printed: Hello from the agent!",
        }},
    ]
    return StubModel(scripted), "StubModel (no API key — scripted responses)"


# ----------------------------------------------------------------------
# Step 4 — build the runner and run a task
# ----------------------------------------------------------------------

def main():
    sandbox = Path(__file__).parent / "_sandbox"
    sandbox.mkdir(exist_ok=True)

    model, label = build_model(sandbox)
    print(f"Model in use: {label}")
    print(f"Sandbox dir : {sandbox}")
    print()

    runner = AgentRunner(
        model=model,
        Agent=AgentType.ReAct,            # standard ReAct prompt template
        tools=[read_tool, save_tool, run_tool],
        max_iterations=6,                  # safety cap on the loop
        use_function_calling=True,         # ← the new path: tool-use, not JSON-in-text
    )

    user_task = (
        f"Create a file at {sandbox / 'greeting.py'} containing the Python code "
        f"`print('Hello from the agent!')`, then execute it and tell me what it prints."
    )
    print(f"User task: {user_task}\n")
    print("─" * 70)

    result = runner.invoke(user_task)   # .invoke is the canonical name; .Initialize still works

    print("─" * 70)
    print("\nFinal answer returned to caller:")
    print(f"  {result.content}\n")

    print("Steps the agent took:")
    for i, step in enumerate(result.steps, 1):
        print(f"  {i}. {step}")
    print()

    print("Tool calls recorded on the AgentCompletion:")
    for tc in result.tool_calls:
        # Truncate long results so the demo output stays readable.
        result_preview = tc.result if len(tc.result) < 200 else tc.result[:200] + "…"
        print(f"  - {tc.name}({tc.args})")
        print(f"      → {result_preview}")

    print(f"\nMessage history grew to {len(result.history)} entries "
          f"(system + user + N×(assistant + function/result)).")


if __name__ == "__main__":
    main()
