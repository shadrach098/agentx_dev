"""Conversation persistence for agents.

A ``Session`` captures everything needed to resume an agent conversation
across process restarts: the message history, accumulated tool calls,
token usage, and per-completion metadata. Serializes to JSON; runner +
model are NOT persisted (they hold live API clients, file handles,
tool closures) and must be re-attached on load.

Why this exists:

Without persistence, every restart loses state. The agent forgets what
the user asked an hour ago, what tools it already called, how many
tokens it's spent. For any long-running or multi-turn use case
(chatbots, research assistants, automation agents), that's a blocker.

Usage::

    from agentx_dev import AgentRunner, AgentType, Claude, Session, DefaultTools, Permissions

    runner = AgentRunner(
        model=Claude(),
        agent=AgentType.ReAct,
        tools=DefaultTools.build(Permissions.full_access(['./work'])),
    )

    # Fresh conversation
    session = Session.start(runner)
    session.invoke("Plan a 3-day Paris trip")
    session.save('./sessions/paris-trip.json')

    # ... process restarts ...

    # Resume
    session = Session.load('./sessions/paris-trip.json').attach(runner)
    session.invoke("Add a stop in Versailles on day 2")
    session.save('./sessions/paris-trip.json')

Design choices:

- Session subclasses pydantic.BaseModel so serialization is free + we get
  validation on load (catches corrupted files at the boundary).
- ``runner`` is a non-serialized field. Loading requires ``.attach(runner)``
  before any invoke / ainvoke. Forgetting raises a clear RuntimeError
  rather than crashing inside the loop.
- A ``version: int`` field is set on every save. Future schema changes
  can branch on it during load to migrate old files.
- Token usage is captured on every invoke (when the model exposes
  ``.usage``) so totals persist across save/load cycles. After loading,
  the existing usage counts seed the model's counter? No — the model
  starts at zero again; the session's recorded usage is the historical
  total. Mixing them would be confusing. Document the distinction.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from agentx_dev.Agents.Agent import AgentCompletion


_SESSION_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Session(BaseModel):
    """A persistable agent conversation.

    Use ``Session.start(runner)`` to begin a fresh session, or
    ``Session.load(path).attach(runner)`` to resume one. Call
    ``.save(path)`` after any state-changing invoke to write to disk.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Persisted fields ----------------------------------------------
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version: int = _SESSION_SCHEMA_VERSION
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)

    model_class_name: Optional[str] = None
    agent_class_name: Optional[str] = None

    # Working message history — the assistant + user + tool messages the
    # runner builds up. Threaded back through ``chat_history`` on resume.
    history: List[Dict[str, Any]] = Field(default_factory=list)

    # Flat append-only log of every tool call across all invokes in this
    # session, so callers can audit "what did this agent actually do?"
    # without replaying.
    tool_calls_log: List[Dict[str, Any]] = Field(default_factory=list)

    # Per-completion metadata (id, content, steps). The full completion
    # object isn't persisted; this is enough to reconstruct what happened.
    completions_meta: List[Dict[str, Any]] = Field(default_factory=list)

    # Cumulative token usage across every model call in this session.
    # NOT seeded back into model.usage on load — see module docstring.
    usage: Dict[str, int] = Field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    )

    # Free-form metadata users can stash (tenant id, user email, etc.).
    metadata: Dict[str, Any] = Field(default_factory=dict)

    # --- Non-persisted runner handle (live, can't be JSON'd) -----------
    runner: Any = Field(default=None, exclude=True, repr=False)

    # --- Constructors --------------------------------------------------

    @classmethod
    def start(cls, runner: Any, *, metadata: Optional[Dict[str, Any]] = None) -> "Session":
        """Begin a fresh session bound to ``runner``.

        Captures the model + agent class names for diagnostics on load.
        """
        s = cls(metadata=dict(metadata or {}))
        s.runner = runner
        try:
            s.model_class_name = type(runner.model).__name__
        except AttributeError:
            pass
        try:
            agent = getattr(runner, "Agent", None)
            if agent is not None:
                s.agent_class_name = type(agent).__name__
        except AttributeError:
            pass
        return s

    def attach(self, runner: Any) -> "Session":
        """Wire a runner onto a loaded session so invoke/ainvoke work.

        Returns self so callers can chain: ``Session.load(p).attach(r)``.
        """
        self.runner = runner
        return self

    # --- Invocation ----------------------------------------------------

    def _require_runner(self) -> Any:
        if self.runner is None:
            raise RuntimeError(
                "Session has no runner attached. Call .attach(runner) "
                "before invoke/ainvoke (the runner isn't serialized — it "
                "holds live API clients and tool closures)."
            )
        return self.runner

    def _absorb(self, result: AgentCompletion) -> None:
        """Pull state out of an AgentCompletion into the session."""
        # history is the COMPLETE working_history including all prior
        # turns, so replacement (not append) is correct.
        if result.history is not None:
            self.history = list(result.history)
        for tc in result.tool_calls:
            self.tool_calls_log.append({
                "name": tc.name,
                "args": tc.args,
                "result": tc.result,
            })
        self.completions_meta.append({
            "id": result.id,
            "created": result.created,
            "model": result.model,
            "query": result.query,
            "content": result.content,
            "steps": list(result.steps or []),
            "tool_calls_count": len(result.tool_calls or []),
        })
        # Snapshot model.usage if the model tracks it (every BaseChatModel
        # does as of #22, but defensively .get).
        runner = self.runner
        model = getattr(runner, "model", None)
        u = getattr(model, "usage", None) if model is not None else None
        if u is not None:
            self.usage = {
                "input_tokens": int(u.total_input_tokens),
                "output_tokens": int(u.total_output_tokens),
                "calls": int(u.total_calls),
            }
        self.updated_at = _now_iso()

    def invoke(self, user_input: str) -> AgentCompletion:
        """Run a sync turn and absorb the result into session state."""
        runner = self._require_runner()
        result = runner.invoke(user_input, chat_history=self.history or None)
        self._absorb(result)
        return result

    async def ainvoke(self, user_input: str) -> AgentCompletion:
        """Run an async turn and absorb the result into session state."""
        runner = self._require_runner()
        if hasattr(runner, "ainvoke"):
            result = await runner.ainvoke(user_input, chat_history=self.history or None)
        else:
            raise RuntimeError(
                f"Session.ainvoke requires a runner with an .ainvoke method; "
                f"{type(runner).__name__} only has .invoke. Use Session.invoke instead."
            )
        self._absorb(result)
        return result

    # --- Persistence ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Plain dict suitable for json.dump. Excludes the live runner."""
        return self.model_dump(exclude={"runner"})

    def save(self, path: Any) -> Path:
        """Atomically write the session to ``path`` as JSON. Returns the
        resolved path so callers can chain / log it."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp sibling then rename so a crashed write can't
        # leave a half-written session file behind.
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Any, *, runner: Any = None) -> "Session":
        """Load a session from disk. Optionally attaches a runner in one step.

        Raises ``FileNotFoundError`` if ``path`` doesn't exist and
        ``ValueError`` (wrapping the underlying parse error) on a corrupt
        file. Future schema versions can migrate by inspecting ``version``
        before constructing the Session.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Session file not found: {p}")
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Session file {p} is not valid JSON: {e}") from e

        version = data.get("version", 1)
        if version > _SESSION_SCHEMA_VERSION:
            raise ValueError(
                f"Session file {p} has schema version {version}, but this "
                f"build only knows up to {_SESSION_SCHEMA_VERSION}. "
                "Upgrade agentx_dev or downgrade the file."
            )

        session = cls(**data)
        if runner is not None:
            session.runner = runner
        return session
