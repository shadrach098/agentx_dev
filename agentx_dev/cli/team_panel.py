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
_NOT_RUN = ("Refused", "The user declined")   # a call whose job never started (a job that failed did start)
# how the note a stopped turn leaves in the conversation begins (app.py): its team jobs are drawn under it
STOPPED_NOTE = "[agentx] This turn stopped before it finished"


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
    """The ``start_team_job`` calls of a conversation, in order: ``{"ref", "id", "at", "key", "ok", "ran"}``.
    ``ref`` names the call (its id, or ``@<message index>`` for a text-mode step, which has none); ``at`` is the
    index of the message that made it; ``ok`` is False when its result says it did not run or failed (declined,
    refused, an error), ``ran`` False only when its job never started (declined, refused); both None while it has
    no result."""
    from agentx_dev.Runner.AgentRun import _FORMAT_NAMES
    calls: list[dict] = []
    by_id: dict[str, dict] = {}
    waiting = None                                   # a text-mode call waits for the next observation

    def settle(entry: dict, result: str) -> None:
        result = result.lstrip()
        entry["ok"], entry["ran"] = not result.startswith(_FAILED), not result.startswith(_NOT_RUN)
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
                    entry = {"ref": ident or f"@{at}", "id": ident, "at": at, "key": task_key(args.get("task")),
                             "ok": None, "ran": None}
                    calls.append(entry)
                    if ident:
                        by_id[ident] = entry
            step = _text_step(text) if not message.get("tool_calls") else None
            if step is not None and step[0] == TEAM_TOOL and isinstance(step[1], dict):
                waiting = {"ref": f"@{at}", "id": "", "at": at, "key": task_key(step[1].get("task")), "ok": None,
                           "ran": None}
                calls.append(waiting)
        elif role == "tool":
            entry = by_id.get(str(message.get("tool_call_id") or ""))
            if entry is not None and entry["ok"] is None:
                settle(entry, text)
        elif role == "user" and text.startswith("Observation: ") and waiting is not None:
            settle(waiting, text[len("Observation: "):])
            waiting = None
    return calls


def record_key(record: dict) -> str:
    """The task fingerprint of a kept record."""
    return str(record.get("taskKey") or task_key(record.get("task")))


def _index(value: Any) -> Any:
    """A message index kept in a record, or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def is_stopped_note(message: Any) -> bool:
    """The note a stopped turn left (``STOPPED_NOTE``): its team jobs are drawn under it."""
    return (isinstance(message, dict) and message.get("role") == "assistant" and not message.get("tool_calls")
            and _content(message).startswith(STOPPED_NOTE))


def assign_team_blocks(messages: Any, kept: Any) -> dict[str, dict]:
    """Which kept job goes under which call (by the call's ``ref``; a stopped turn's note is ``@<its index>``).

    A record kept under its call's id goes there (and nowhere when that call is gone). One pinned to a message when
    its turn ended (``at``: its text-mode call, or with ``stopped`` the note its stopped turn left) goes there while
    that message is still the same kind of message. One with neither (its turn has not ended yet) goes, newest
    first, to the newest call of the same task made since its turn began (``turnAt``) whose result did not fail."""
    msgs = messages if isinstance(messages, list) else []
    calls = team_calls(msgs)
    records = [r for r in (kept if isinstance(kept, list) else []) if isinstance(r, dict)]
    out: dict[str, dict] = {}
    by_id = {c["id"]: c for c in calls if c["id"]}
    by_at = {c["at"]: c for c in calls if not c["id"]}
    for record in records:
        at = _index(record.get("at"))
        if record.get("call"):
            target = by_id.get(str(record.get("call")))
            ref = target["ref"] if target is not None else None
        elif at is not None and record.get("stopped"):
            ref = f"@{at}" if at < len(msgs) and is_stopped_note(msgs[at]) else None
        elif at is not None:
            target = by_at.get(at)
            ref = target["ref"] if target is not None and target["key"] == record_key(record) else None
        else:
            continue
        if ref is not None and ref not in out:
            out[ref] = record
    loose = [r for r in records if not r.get("call") and _index(r.get("at")) is None]
    free = [c for c in calls if c["ref"] not in out and c["ok"] is not False]
    for record in reversed(loose):
        begun = _index(record.get("turnAt")) or 0
        target = next((c for c in reversed(free) if c["key"] == record_key(record) and c["at"] >= begun
                       and c["ref"] not in out), None)
        if target is not None:
            out[target["ref"]] = record
    return out


def settle_team_blocks(messages: Any, kept: Any, start: int, stopped_at: Any = None) -> None:
    """A turn's messages are in the conversation (from index ``start``): pin each job it kept that has no call id
    (text mode) to its call, the first call of its task since its run began that ran and that no other job has
    (``at``). ``stopped_at``: the turn stopped and left only a note at that index; a job of the turn whose call is
    not in the conversation is drawn under that note (``stopped``, its call id dropped: there is no call)."""
    records = [r for r in (kept if isinstance(kept, list) else []) if isinstance(r, dict)]
    calls = team_calls(messages)
    ids = {c["id"] for c in calls if c["id"]}
    taken = {_index(r.get("at")) for r in records if not r.get("call") and not r.get("stopped")}
    for record in records:
        begun = _index(record.get("turnAt")) or 0
        if begun < start or _index(record.get("at")) is not None or (record.get("call") and
                                                                      str(record["call"]) in ids):
            continue                                 # an earlier turn's, pinned already, or its call is there
        if not record.get("call"):
            target = next((c for c in calls if not c["id"] and c["at"] >= begun and c["at"] not in taken
                           and c["key"] == record_key(record) and c["ran"] is not False), None)
            if target is not None:
                record["at"] = target["at"]
                taken.add(target["at"])
                continue
        if _index(stopped_at) is not None:
            record.pop("call", None)
            record["at"], record["stopped"] = stopped_at, True


def rebase_team_blocks(kept: Any, cut: int, shift: int) -> list[dict]:
    """The kept jobs after a summary replaced the messages before ``cut`` and moved the rest ``shift`` places
    forward: the message indices they hold move with them; one that pointed into the summarised part keeps none
    (``turnAt`` goes to where the kept part now begins; a pinned one, whose message is gone, is dropped)."""
    out = []
    for record in (kept if isinstance(kept, list) else []):
        if not isinstance(record, dict):
            out.append(record)
            continue
        at = _index(record.get("at"))
        if at is not None:
            if at < cut:
                continue
            record["at"] = at - shift
        if "turnAt" in record:
            record["turnAt"] = max((_index(record.get("turnAt")) or 0), cut) - shift
        out.append(record)
    return out


def prune_team_blocks(messages: Any, kept: Any) -> list[dict]:
    """The kept jobs that still have their call in ``messages`` (after an edit or a summary removed some)."""
    shown = {id(record) for record in assign_team_blocks(messages, kept).values()}
    return [record for record in (kept if isinstance(kept, list) else []) if id(record) in shown]
