"""Browser-only deployment roster. Never passed to the planner or helper chooser."""
from __future__ import annotations

from typing import Any

from agentx_dev.cli.ui import helper_activity, step_counts, step_stats


class TeamPanel:
    """Keep deployments (not helper names or reusable runtime slots) for one job."""

    def __init__(self) -> None:
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
                                      "termination": "", "inferred": False})
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
            rows.append({"id": row["id"], "helper": row["helper"], "label": row["label"],
                         "state": row["state"], "model": str(snap.get("model") or ""),
                         "fallback": row["fallback"], "fallbackFrom": row["fallbackFrom"],
                         "fallbackReason": row["fallbackReason"],
                         "termination": row["termination"], "inferred": row["inferred"],
                         "activity": helper_activity(step_counts(snap)) if snap else "",
                         "stats": step_stats(snap) if snap else "",
                         "action": str(action), "path": str(args.get("path") or ""),
                         "summary": str(summary or ""), "actionFailed": bool(failed),
                         "inFlight": bool(current)})
        return rows
