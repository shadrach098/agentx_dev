"""A runner's log: one line per step while it runs, for production and for tests.

``AgentRunner(..., log=...)`` and ``AsyncAgentRunner(..., log=...)`` take:

- ``True``: short readable lines on stderr, as the run goes;
- a path (``"run.jsonl"``, ``Path(...)``): one JSON object per event appended to that file (a path not ending in
  ``.jsonl`` or ``.json`` gets the readable lines instead);
- a ``logging.Logger``: readable lines at INFO, with the event as ``record.agentx`` for structured handlers;
- any function ``(event: dict) -> None``;
- a ``RunLog`` (to set the preview length), or ``None`` (the default: nothing is logged).

Events, each with ``event``, ``time`` (ISO 8601) and ``run`` (an id shared by one run's lines):

- ``run.start``: ``query``, ``model``, ``agent``;
- ``model.call``: ``call`` (1, 2, ...), ``ms``, ``tokens_in``, ``tokens_out``, ``cached`` (and ``error`` when it
  failed);
- ``tool``: ``tool``, ``args``, ``ok``, ``result``;
- ``run.end``: ``outcome``, ``ms``, ``model_calls``, ``tool_calls``, ``tool_errors``, ``tokens_in``, ``tokens_out``,
  ``cached``, ``answer``;
- ``run.error``: ``ms``, ``error`` (the run raised; the exception still reaches the caller).

Text (the query, tool arguments and results, the answer) is cut to ``preview`` characters and has likely secrets
(API keys, tokens, passwords) replaced with ``[REDACTED]``. A log that fails to write never stops the run.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

from agentx_dev.Observability import redact_secrets

__all__ = ["RunLog", "RunRecord"]

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 300

LogTarget = Union[None, bool, str, Path, logging.Logger, Callable[[Dict[str, Any]], None], "RunLog"]


def _model_name(model: Any) -> str:
    for attribute in ("model_name", "model"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    defaults = getattr(model, "defaults", None)
    if isinstance(defaults, dict) and isinstance(defaults.get("model"), str):
        return defaults["model"]
    return type(model).__name__


def _usage(model: Any):
    usage = getattr(model, "usage", None)
    return (int(getattr(usage, "total_input_tokens", 0) or 0), int(getattr(usage, "total_output_tokens", 0) or 0),
            int(getattr(usage, "total_cache_read_tokens", 0) or 0))


class RunLog:
    """Where a runner's events go (see the module docstring). ``RunLog.to(target)`` builds one from what a runner's
    ``log=`` was given; ``begin`` starts one run's ``RunRecord``."""

    def __init__(self, write: Callable[[Dict[str, Any]], None], *, preview: int = PREVIEW_CHARS) -> None:
        self._write = write
        self.preview = max(20, int(preview))
        self._failed = False

    # -- building ---------------------------------------------------------------------------------------------

    @classmethod
    def to(cls, target: LogTarget, *, preview: int = PREVIEW_CHARS) -> Optional["RunLog"]:
        if target is None or target is False:
            return None
        if isinstance(target, RunLog):
            return target
        if target is True:
            return cls(_lines_to(sys.stderr), preview=preview)
        if isinstance(target, (str, Path)):
            path = Path(target)
            json_lines = path.suffix.lower() in (".jsonl", ".json")
            return cls(_append_to(path, json_lines), preview=preview)
        if isinstance(target, logging.Logger):
            return cls(_to_logger(target), preview=preview)
        if callable(target):
            return cls(target, preview=preview)
        raise TypeError("log= takes True, a file path, a logging.Logger, a function (event) -> None, or a RunLog")

    @classmethod
    def chain(cls, *logs: Optional["RunLog"]) -> Optional["RunLog"]:
        """One log that writes every event to each of ``logs`` (the ``None`` ones are left out)."""
        present = [log for log in logs if log is not None]
        if not present:
            return None
        if len(present) == 1:
            return present[0]

        def write(event: Dict[str, Any]) -> None:
            for log in present:
                log.emit(event)
        return cls(write, preview=max(log.preview for log in present))

    # -- writing ----------------------------------------------------------------------------------------------

    def emit(self, event: Dict[str, Any]) -> None:
        try:
            self._write(event)
        except Exception as e:                       # a log must never stop the run
            if not self._failed:
                self._failed = True
                logger.warning(f"agentx run log could not be written ({type(e).__name__}: {e}); "
                               "later failures are not reported")

    def text(self, value: Any) -> str:
        """``value`` as one line: secrets redacted, cut to ``preview`` characters."""
        text = redact_secrets(value if isinstance(value, str) else _as_text(value))
        text = " ".join(text.split())
        return text if len(text) <= self.preview else text[: self.preview - 3] + "..."

    def begin(self, query: Any, *, model: Any = None, agent: Any = None) -> "RunRecord":
        return RunRecord(self, query, model=model, agent=agent)


class RunRecord:
    """One run's events. The runner calls ``model_call`` around each model request, ``tool`` for each tool result,
    and ``end`` or ``fail`` once."""

    def __init__(self, log: RunLog, query: Any, *, model: Any = None, agent: Any = None) -> None:
        self.log = log
        self.run_id = uuid.uuid4().hex[:8]
        self.model = model
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self.model_calls = 0
        self.tool_calls = 0
        self.tool_errors: List[str] = []
        self._before = _usage(model)
        self._closed = False
        extra = {"agent": str(getattr(agent, "value", agent))} if agent else {}
        self._emit("run.start", query=log.text(query), model=_model_name(model) if model is not None else "",
                   **extra)

    def _emit(self, name: str, **fields: Any) -> None:
        self.log.emit({"event": name, "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                       "run": self.run_id, **fields})

    def model_call(self, call: Callable[[], Any]) -> Any:
        """Run ``call`` (one model request) and log how long it took and the tokens it used."""
        with self._lock:
            self.model_calls += 1
            number = self.model_calls
        before = _usage(self.model)
        started = time.monotonic()
        try:
            result = call()
        except BaseException as e:
            self._model_line(number, started, before, error=f"{type(e).__name__}: {e}")
            raise
        self._model_line(number, started, before)
        return result

    async def amodel_call(self, call: Callable[[], Any]) -> Any:
        """``model_call`` for a coroutine factory."""
        with self._lock:
            self.model_calls += 1
            number = self.model_calls
        before = _usage(self.model)
        started = time.monotonic()
        try:
            result = await call()
        except BaseException as e:
            self._model_line(number, started, before, error=f"{type(e).__name__}: {e}")
            raise
        self._model_line(number, started, before)
        return result

    def _model_line(self, number: int, started: float, before, error: Optional[str] = None) -> None:
        after = _usage(self.model)
        fields: Dict[str, Any] = {"call": number, "ms": round((time.monotonic() - started) * 1000),
                                  "tokens_in": after[0] - before[0], "tokens_out": after[1] - before[1],
                                  "cached": after[2] - before[2]}
        if error:
            fields["error"] = self.log.text(error)
        self._emit("model.call", **fields)

    def tool(self, name: str, args: Any, result: Any, is_error: bool) -> None:
        with self._lock:
            self.tool_calls += 1
            if is_error:
                self.tool_errors.append(str(name))
        self._emit("tool", tool=str(name), args=self.log.text(args), ok=not is_error, result=self.log.text(result))

    def _totals(self) -> Dict[str, Any]:
        after = _usage(self.model)
        return {"ms": round((time.monotonic() - self._started) * 1000), "model_calls": self.model_calls,
                "tool_calls": self.tool_calls, "tool_errors": len(self.tool_errors),
                "tokens_in": after[0] - self._before[0], "tokens_out": after[1] - self._before[1],
                "cached": after[2] - self._before[2]}

    def end(self, completion: Any) -> None:
        if self._closed:
            return
        self._closed = True
        self._emit("run.end", outcome=str(getattr(completion, "outcome", "done") or "done"), **self._totals(),
                   answer=self.log.text(getattr(completion, "content", "")))

    def fail(self, error: BaseException) -> None:
        if self._closed:
            return
        self._closed = True
        totals = self._totals()
        self._emit("run.error", ms=totals["ms"], error=self.log.text(f"{type(error).__name__}: {error}"))

    def follow(self, events: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
        """Pass a sync runner's step events through, logging each tool result and the end of the run."""
        pending_args: Dict[str, Any] = {}
        try:
            for event in events:
                kind = event.get("type")
                if kind == "tool_call":
                    pending_args[str(event.get("name"))] = event.get("args")
                elif kind == "tool_result":
                    name = str(event.get("name"))
                    self.tool(name, pending_args.pop(name, ""), event.get("result", ""), bool(event.get("is_error")))
                elif kind == "completion":
                    self.end(event.get("completion"))
                yield event
        except GeneratorExit:
            if not self._closed:
                self._closed = True
                self._emit("run.error", ms=self._totals()["ms"], error="stopped early by the caller")
            raise
        except BaseException as e:
            self.fail(e)
            raise


# -- destinations -------------------------------------------------------------------------------------------------

def _as_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _line(event: Dict[str, Any]) -> str:
    """The readable form: ``14:02:11 run 3f2a1c9d  tool read_path ok: <result>``."""
    try:
        clock = datetime.fromisoformat(str(event.get("time"))).astimezone().strftime("%H:%M:%S")   # local time
    except (TypeError, ValueError):
        clock = str(event.get("time", ""))[11:19]
    head = f"{clock} run {event.get('run', '')}  "
    name = event.get("event")
    if name == "run.start":
        return head + f"start {event.get('model', '')}: {event.get('query', '')}"
    if name == "model.call":
        cached = f", {event['cached']:,} cached" if event.get("cached") else ""
        error = f"  FAILED: {event['error']}" if event.get("error") else ""
        return head + (f"model call {event.get('call')} {event.get('ms', 0):,} ms, {event.get('tokens_in', 0):,} in"
                       f"{cached}, {event.get('tokens_out', 0):,} out{error}")
    if name == "tool":
        status = "ok" if event.get("ok") else "ERROR"
        return head + f"tool {event.get('tool')} {status} {event.get('args', '')} -> {event.get('result', '')}"
    if name == "run.end":
        return head + (f"end {event.get('outcome')} in {event.get('ms', 0):,} ms, {event.get('model_calls', 0)} "
                       f"model calls, {event.get('tool_calls', 0)} tool calls ({event.get('tool_errors', 0)} failed), "
                       f"{event.get('tokens_in', 0):,} in / {event.get('tokens_out', 0):,} out: {event.get('answer', '')}")
    if name == "run.error":
        return head + f"error after {event.get('ms', 0):,} ms: {event.get('error', '')}"
    return head + _as_text(event)


def _lines_to(stream: Any) -> Callable[[Dict[str, Any]], None]:
    lock = threading.Lock()

    def write(event: Dict[str, Any]) -> None:
        with lock:
            stream.write(_line(event) + "\n")
            stream.flush()
    return write


def _append_to(path: Path, json_lines: bool) -> Callable[[Dict[str, Any]], None]:
    lock = threading.Lock()

    def write(event: Dict[str, Any]) -> None:
        text = json.dumps(event, ensure_ascii=False, default=str) if json_lines else _line(event)
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
    return write


def _to_logger(target: logging.Logger) -> Callable[[Dict[str, Any]], None]:
    def write(event: Dict[str, Any]) -> None:
        level = logging.WARNING if event.get("event") == "run.error" or event.get("ok") is False else logging.INFO
        target.log(level, _line(event), extra={"agentx": event})
    return write
