"""Browser-only deployment roster. Never passed to the planner or helper chooser."""
from __future__ import annotations

import uuid
from typing import Any

from agentx_dev.cli.ui import ended_look, helper_activity, report_line, short_time, step_counts, step_stats
from agentx_dev.cli.ui_text import _short

LAST_ACTIONS = 5                    # what a row's details list of the step's calls
REPORT_CHARS = 160                  # the start of a report under an ended row, as the terminal prints it
# a step the job ended without word of: Esc stopped it, or how it ended is not known
_NO_WORD = {"interrupted": ("■", "Stopped", "yellow"), "unknown": ("?", "Ended without word", "dim")}


def _subject(name: str, args: Any) -> str:
    try:
        from agentx_dev.cli.approval import subject_of
        return str(subject_of(name, args if isinstance(args, dict) else {}) or "")
    except Exception:
        return ""


class TeamPanel:
    """Keep deployments (not helper names or reusable runtime slots) for one job. ``id`` names the job: the app
    window draws one block per job and updates it in place."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.rows: list[dict[str, Any]] = []
        self.round = 0

    def notify(self, kind: str, data: dict) -> None:
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
                                      "termination": "", "inferred": False, "ended": None})
            return
        if kind not in ("dispatch", "result"):
            return
        status = data.get("status")
        if "round" in data and "step" in data:
            row = next((r for r in self.rows
                        if (r["round"], r["step"]) == (data["round"], data["step"])), None)
            if row is not None and data.get("step_id") is not None and row["step_id"] != data["step_id"]:
                return
        else:
            row = next((r for r in self.rows if status is not None and r["status"] is status), None)
        if row is None:
            return
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
                         "actions": [{"name": str(name), "subject": _subject(str(name), call_args),
                                      "summary": str(said or ""), "failed": bool(bad)}
                                     for name, call_args, said, bad in calls[-LAST_ACTIONS:]],
                         **(ended or {"mark": "", "word": "", "tone": "", "report": ""})})
        return rows

    def counts(self, rows: list[dict] | None = None) -> tuple[int, int]:
        """(steps that ended, steps planned), as the block's head counts them."""
        rows = self.snapshot() if rows is None else rows
        return sum(row["state"] in ("done", "failed", "skipped") for row in rows), len(rows)

    def record(self, task: str) -> dict:
        """The job in its final form, kept with the conversation: a reloaded window or a reopened conversation
        draws its block from this."""
        rows = self.snapshot()
        done, total = self.counts(rows)
        return {"job": self.id, "task": str(task or ""), "rows": rows, "done": done, "total": total}
