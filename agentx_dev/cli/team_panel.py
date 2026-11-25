"""Browser-only deployment roster. Never passed to the planner or helper chooser."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from agentx_dev.cli.approval import subject_of
from agentx_dev.cli.ui import ended_look, helper_activity, report_line, short_time, step_counts, step_stats
from agentx_dev.cli.ui_text import _short

LAST_ACTIONS = 5                    # what a row's details list of the step's calls
REPORT_CHARS = 160                  # the start of a report under an ended row, as the terminal prints it
TASK_SHOWN = 80                     # the start of the task a kept record holds (the key matches the whole task)
TEAM_TOOL = "start_team_job"
# what a kept record's rows hold at most: the window shows no more than this
_KEPT_CHARS = {"helper": 120, "label": 120, "model": 120, "activity": 300, "stats": 160, "path": 300, "summary": 200,
               "report": REPORT_CHARS + 10, "word": 200, "fallback": 120, "fallbackFrom": 120, "fallbackReason": 120}
# a step the job ended without word of: Esc stopped it, or how it ended is not known
_NO_WORD = {"interrupted": ("■", "Stopped", "yellow"), "unknown": ("?", "Ended without word", "dim")}
_FAILED = ("ERROR", "Error:", "Refused", "The user declined")


def _subject(name: str, args: Any) -> str:
    try:
        return str(subject_of(name, args if isinstance(args, dict) else {}) or "")
    except Exception:
        return ""


def task_key(task: Any) -> str:
    """A short fingerprint of a team job's whole task: a kept record is matched to its call by it."""
    return hashlib.sha256(str(task or "").encode("utf-8", "replace")).hexdigest()[:16]


def _cut(value: Any, limit: int) -> Any:
    return value[:limit] if isinstance(value, str) else value


class TeamPanel:
    """Keep deployments (not helper names or reusable runtime slots) for one job. ``id`` names the job: the app
    window draws one block per job and updates it in place."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.rows: list[dict[str, Any]] = []
        self.round = 0

    def notify(self, kind: str, data: dict) -> bool:
        """Take a job event in. True when it found (or, for a plan, made) its rows: a result the panel has no row
        for is shown another way (the window prints its ended line)."""
        if kind == "plan":
            self.round = data.get("round", self.round + 1)
            for row in self.rows:                   # a new plan replaces the old one: its unstarted steps never run
                if row["state"] == "waiting" and row["round"] != self.round:
                    row["state"] = "skipped"
            for index, step in enumerate(data.get("steps") or []):
                if isinstance(step, dict):
                    self.rows.append({"id": f"deployment-{len(self.rows) + 1}",
                                      "round": self.round, "step": index,
                                      "step_id": step.get("step_id"),
                                      "helper": str(step.get("agent") or ""),
                                      "label": str(step.get("label") or step.get("agent") or ""),
                                      "query": str(step.get("query") or ""),
                                      "state": "waiting", "status": None,
                                      "fallback": "", "fallbackFrom": "", "fallbackReason": "",
                                      "termination": "", "inferred": False, "ended": None, "subjects": {}})
            return True
        if kind not in ("dispatch", "result"):
            return False
        status = data.get("status")
        if "round" in data and "step" in data:
            row = next((r for r in self.rows
                        if (r["round"], r["step"]) == (data["round"], data["step"])), None)
            if row is not None and data.get("step_id") is not None and row["step_id"] != data["step_id"]:
                return False
        else:
            row = next((r for r in self.rows if status is not None and r["status"] is status), None)
        if row is None:
            return False
        if status is not None:
            row["status"] = status
        row["state"] = ("running" if kind == "dispatch" else "skipped" if data.get("skipped")
                        else "done" if data.get("ok") else "failed")
        finished_on = row["state"] == "done" and data.get("fallback")           # the fallback finished it: why
        row["fallback"] = str(data.get("fallback") or "") if finished_on else ""
        row["fallbackFrom"] = str(data.get("fallback_from") or "") if finished_on else ""
        row["fallbackReason"] = str(data.get("fallback_reason") or "") if finished_on else ""
        row["termination"], row["inferred"] = "", False
        if kind == "result":                        # its ended line, worded as the terminal prints it
            mark, word, tone = ended_look(bool(data.get("ok")), partial=bool(data.get("partial")),
                                          skipped=bool(data.get("skipped")),
                                          fallback={"to": data.get("fallback"), "from": data.get("fallback_from"),
                                                    "reason": data.get("fallback_reason")})
            report = _short(report_line(data.get("text")), REPORT_CHARS) or "(no report)"
            row["ended"] = {"mark": mark, "word": word, "tone": tone, "report": report}
        return True

    def finish(self, reason: str = "unknown") -> None:
        reason = "interrupted" if reason == "interrupted" else "unknown"
        for row in self.rows:
            if row["state"] in ("waiting", "running"):
                row["state"] = reason
                row["termination"], row["inferred"] = reason, True

    def snapshot(self) -> list[dict]:
        rows = []
        for row in self.rows:
            status = row["status"]
            snap = status.snapshot() if status is not None else {}
            calls = snap.get("log") or []
            action, args, summary, failed = calls[-1] if calls else ("", {}, "", False)
            current = snap.get("current_call")
            if current:
                action, args = current
                summary, failed = "", False
            ended = row["ended"] if row["state"] in ("done", "failed", "skipped") else None
            if row["state"] in _NO_WORD:
                mark, word, tone = _NO_WORD[row["state"]]
                ended = {"mark": mark, "word": word, "tone": tone, "report": ""}
            elif ended is None and row["state"] == "skipped":          # a new plan dropped it before it ran
                mark, word, tone = ended_look(True, skipped=True)
                ended = {"mark": mark, "word": word, "tone": tone, "report": ""}
            rows.append({"id": row["id"], "helper": row["helper"], "label": row["label"],
                         "state": row["state"], "model": str(snap.get("model") or ""),
                         "fallback": row["fallback"], "fallbackFrom": row["fallbackFrom"],
                         "fallbackReason": row["fallbackReason"],
                         "termination": row["termination"], "inferred": row["inferred"],
                         "activity": helper_activity(step_counts(snap)) if snap else "",
                         "stats": step_stats(snap) if snap else "",
                         "action": str(action), "path": str(args.get("path") or ""),
                         "summary": str(summary or ""), "actionFailed": bool(failed),
                         "inFlight": bool(current),
                         "toolUses": int(snap.get("tool_uses") or 0),
                         "time": short_time(snap.get("seconds") or 0) if snap else "",
                         "actions": [{"name": str(name), "subject": self._subject_at(row, at, name, call_args),
                                      "summary": str(said or ""), "failed": bool(bad)}
                                     for at, (name, call_args, said, bad) in
                                     enumerate(calls[-LAST_ACTIONS:], start=max(0, len(calls) - LAST_ACTIONS))],
                         **(ended or {"mark": "", "word": "", "tone": "", "report": ""})})
        return rows

    @staticmethod
    def _subject_at(row: dict, at: int, name: Any, args: Any) -> str:
        """What call ``at`` of a step was about, worked out once (the status asks for the rows many times a
        second, and a step's log only grows)."""
        subjects = row.setdefault("subjects", {})
        if at not in subjects:
            subjects[at] = _subject(str(name), args)
            for old in [n for n in subjects if n < at - LAST_ACTIONS]:
                del subjects[old]
        return subjects[at]

    def counts(self, rows: list[dict] | None = None) -> tuple[int, int]:
        """(steps that ended, steps planned), as the block's head counts them."""
        rows = self.snapshot() if rows is None else rows
        return sum(row["state"] in ("done", "failed", "skipped") for row in rows), len(rows)

    def record(self, task: str) -> dict:
        """The job in its final form, kept with the conversation: a reloaded window or a reopened conversation
        draws its block from this."""
        snapshot = self.snapshot()
        rows = [{key: _cut(value, _KEPT_CHARS.get(key, 300)) for key, value in row.items() if key != "actions"}
                for row in snapshot]
        for row, full in zip(rows, snapshot):
            row["actions"] = [{"name": _cut(a["name"], 80), "subject": _cut(a["subject"], 200),
                               "summary": _cut(a["summary"], 200), "failed": a["failed"]} for a in full["actions"]]
        done, total = self.counts(rows)
        return {"job": self.id, "task": str(task or "")[:TASK_SHOWN], "taskKey": task_key(task), "rows": rows,
                "done": done, "total": total}


# -- a kept job back under its call (the app window's replay of a conversation) ---------------------------------

def _text_step(text: str) -> Any:
    """``(action, action_input)`` of a text-mode step (``{"action": ..., ...}``), else None."""
    stripped = str(text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}") and '"action"' in stripped):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    return (data["action"], data.get("action_input")) if isinstance(data, dict) and isinstance(
        data.get("action"), str) else None


def _content(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def team_calls(messages: Any) -> list[dict]:
    """The ``start_team_job`` calls of a conversation, in order: ``{"ref", "id", "key", "ok"}``. ``ref`` names the
    call (its id, or ``@<message index>`` for a text-mode step, which has none); ``ok`` is False when its result
    says it did not run (declined, refused, an error), None while it has no result."""
    from agentx_dev.Runner.AgentRun import _FORMAT_NAMES
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    waiting = None                                   # a text-mode call waits for the next observation
    for at, message in enumerate(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict):
            continue
        role, text = message.get("role"), _content(message)
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {} if isinstance(call, dict) else {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                name = str(function.get("name") or "")
                if name in _FORMAT_NAMES and isinstance(args, dict) and isinstance(args.get("action"), str):
                    name, args = args["action"], args.get("action_input")
                if name == TEAM_TOOL and isinstance(args, dict):
                    ident = str(call.get("id") or "")
                    entry = {"ref": ident or f"@{at}", "id": ident, "key": task_key(args.get("task")), "ok": None}
                    calls.append(entry)
                    if ident:
                        by_id[ident] = entry
            step = _text_step(text) if not message.get("tool_calls") else None
            if step is not None and step[0] == TEAM_TOOL and isinstance(step[1], dict):
                waiting = {"ref": f"@{at}", "id": "", "key": task_key(step[1].get("task")), "ok": None}
                calls.append(waiting)
        elif role == "tool":
            entry = by_id.get(str(message.get("tool_call_id") or ""))
            if entry is not None and entry["ok"] is None:
                entry["ok"] = not text.lstrip().startswith(_FAILED)
        elif role == "user" and text.startswith("Observation: ") and waiting is not None:
            waiting["ok"] = not text[len("Observation: "):].lstrip().startswith(_FAILED)
            waiting = None
    return calls


def assign_team_blocks(messages: Any, kept: Any) -> dict[str, dict]:
    """Which kept job goes under which call (by the call's ``ref``). A record kept under its call's id goes there
    (and nowhere when that call is gone). One without an id (a text-mode run) goes, newest first, to the newest call
    of the same task whose result did not fail."""
    calls = team_calls(messages)
    records = [r for r in (kept if isinstance(kept, list) else []) if isinstance(r, dict)]
    out: dict[str, dict] = {}
    by_id = {c["id"]: c for c in calls if c["id"]}
    for record in records:
        target = by_id.get(str(record.get("call") or ""))
        if target is not None and target["ref"] not in out:
            out[target["ref"]] = record
    loose = [r for r in records if not r.get("call")]
    free = [c for c in calls if c["ref"] not in out and c["ok"] is not False]
    for key in dict.fromkeys(str(r.get("taskKey") or task_key(r.get("task"))) for r in loose):
        mine = [r for r in loose if str(r.get("taskKey") or task_key(r.get("task"))) == key]
        theirs = [c for c in free if c["key"] == key]
        for record, call in zip(reversed(mine), reversed(theirs)):
            out[call["ref"]] = record
    return out


def prune_team_blocks(messages: Any, kept: Any) -> list[dict]:
    """The kept jobs that still have their call in ``messages`` (after an edit or a summary removed some)."""
    shown = {id(record) for record in assign_team_blocks(messages, kept).values()}
    return [record for record in (kept if isinstance(kept, list) else []) if id(record) in shown]
