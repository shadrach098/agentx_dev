"""Team jobs on the screen and the skills commands.

A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agentx_dev.cli.commands import command_names, skill_request
from agentx_dev.cli.team import MAX_TEAM_JOBS_PER_MESSAGE, current_status, team_request
from agentx_dev.Skills import SkillInstallError, remove_skill


TEAM_LIMIT_REACHED = (f"Refused: {MAX_TEAM_JOBS_PER_MESSAGE} team jobs already ran for this message. Report to the "
                      "user what is done and what remains, and let them say whether to continue.")
TEAM_STALLED = ("Refused: no step of the last team job finished, so another job would likely fail the same way. "
                "Report to the user what happened and what remains, and let them decide.")


class TeamCommands:
    """Mixed into ``ChatApp``, whose attributes these methods use."""

    def run_team_command(self, task: str) -> None:
        """``/team <task>``: ask the main agent to call the team tool for ``task`` (same prompt, gate and history)."""
        self.send(team_request(task), log_title=f"/team {task}")

    def _within_job_limit(self, gated: Any) -> Any:
        """``gated`` (the team tool behind the gate) with the jobs-per-message limit checked first, so a call past
        ``MAX_TEAM_JOBS_PER_MESSAGE`` is refused without asking the user again. Other calls go through the gate
        as before; a job the user declined does not count (``_team_jobs`` grows only when a job starts)."""
        inner = gated.func

        def once(**kwargs):
            refusal = self._team_refusal()
            return refusal if refusal else inner(**kwargs)

        once.__name__, once.__qualname__, once.__module__ = gated.name, f"within_job_limit.{gated.name}", __name__
        wrapped = type(gated)(func=once, args_schema=gated.args_schema, name=gated.name,
                              description=gated.description)
        wrapped.cacheable = False
        return wrapped

    def _observe_tool(self, name: str, args: dict) -> None:
        """Called by the tool wrappers before the gate check, on the thread that runs the tool. A team helper's calls
        are not printed: they are counted on its step's line above the spinner (the step's own wrapper counts the
        other tools; the skills it opens are counted here) and listed by /team log."""
        status = current_status()
        if status is not None and name == "use_skill" and isinstance(args, dict):
            status.note_skill(str(args.get("name") or "?"))

    def _team_board(self) -> Any:
        rows = list(self._team_rows.values())
        return self.screen.team_board(rows, *self._team_steps) if rows else None

    def _board(self) -> Any:
        """What is drawn above the spinner: the team's running steps, else the chat's open look-around flow."""
        return self._team_board() or self.screen.flow_block()

    def show_team_log(self) -> None:
        """``/team log``: every call of every step of the last team job in this session."""
        if not self._team_log:
            self.notice("No team job has run in this session yet.")
            return
        self.screen.team_log(self._team_log)

    def _show_helper_call(self, helper: str, name: str, args: dict) -> None:
        self.progress.stop()
        self.screen.helper_call(helper, name, args)
        self.progress.start(self._team_label())

    def _team_label(self) -> str:
        """The spinner's words during a team job: what the helpers are doing, or which are running."""
        activity = self.screen.helpers_line()
        if activity:
            return self._safe(activity, 160)
        running = self._team_running
        if not running:
            return "Team is working"
        if len(running) == 1:
            return f"Running {self._safe(running[0], 80)}"
        return f"Running {len(running)} helpers"

    def _team_notify(self, kind: str, data: dict) -> None:
        self.progress.stop()
        if kind == "plan":
            steps = data.get("steps") or []
            self._team_steps = (0, len(steps))
            self.screen.team_plan(steps)
            self.progress.start("Starting the team")
        elif kind == "dispatch":
            self._team_running.append(str(data.get("agent", "")))
            status = data.get("status")
            if status is not None:
                self._team_rows[status.slot] = status
                self._team_log.append(status)
            else:                                   # no status to draw: say that the step started
                self.screen.team_dispatch(data.get("agent"), data.get("query"))
            self.progress.start("Team is working")
        elif kind == "result":
            agent = str(data.get("agent", ""))
            if agent in self._team_running:
                self._team_running.remove(agent)
            status = data.get("status")
            if status is not None:
                self._team_rows.pop(status.slot, None)
            ended, planned = self._team_steps
            self._team_steps = (ended + 1, planned)
            shown = getattr(status, "helper", "") or agent        # a new helper with its model: name@model
            self.screen.team_done(shown, data.get("label") or agent, bool(data.get("ok")), data.get("text"),
                                  partial=bool(data.get("partial")), skipped=bool(data.get("skipped")),
                                  status=status)
            self.progress.start("Team is working")

    def _team_question(self, question: str) -> Optional[str]:
        """The planner or a helper needs a fact: ask in the terminal. Enter, Ctrl+D and Ctrl+C skip it."""
        with self.progress.paused():
            self.screen.team_question(question)
            try:
                answer = self.read_short("Answer (Enter to skip): ")
            except (EOFError, KeyboardInterrupt):
                return None
        return str(answer).strip() or None

    def _team_refusal(self) -> str:
        """Why another team job may not start in this message ("" when it may)."""
        if self._team_jobs >= MAX_TEAM_JOBS_PER_MESSAGE:
            return TEAM_LIMIT_REACHED
        if self._team_stalled:
            return TEAM_STALLED
        return ""

    def show_skills(self) -> None:
        """``/skills``: every skill found on disk (the ones this conversation's agent has first-class, shown as the
        ``/name`` that runs them), and how to use, add or remove one."""
        store = self.skills.store()
        taken = set(command_names())

        def label(skill: Any) -> str:
            if skill.name not in self._runner_skills:
                return skill.name
            return f"/{skill.name}" if f"/{skill.name}" not in taken else f"/skills use {skill.name}"

        lines = [f"  {label(s)} ({s.source}) - {self._safe(s.description, 100)}"
                 + ("" if s.name in self._runner_skills else "  [not active in this conversation]")
                 for s in store.all()]
        lines += [f"  Skipped: {self._safe(problem, 160)}" for problem in store.problems[:5]]
        shown = "\n".join(lines) if lines else "  none yet; the built-in ones are created when a conversation starts"
        self.notice(f"Skills (the agent reads one when its work matches):\n{shown}\n"
                    "Use one with /<skill> [what you want] (type / to pick from the list). "
                    f"They live in {self._safe(self.skills.dir, 300)}. /skills install <github link or folder> adds "
                    "more; /skills remove <name> deletes one. A new conversation (/clear) picks up changes, and "
                    "skills for tools this agent does not have stay inactive.")

    def skill_entries(self) -> Dict[str, str]:
        """``{"/name": "(skill) description"}`` for the `/` pop-up: the skills this conversation's agent has, except
        ones named like a built-in command (those are reached with ``/skills use <name>``)."""
        taken = set(command_names())
        return {f"/{name}": f"(skill) {self._safe(description, 120)}"
                for name, description in sorted(self._runner_skills.items()) if f"/{name}" not in taken}

    def run_skill(self, name: str, text: str = "") -> bool:
        """``/<skill> [what you want]`` and ``/skills use <name> [what you want]``: ask the agent to read the skill
        and do the task. False (nothing sent) when this conversation's agent has no skill of that name."""
        if name not in self._runner_skills:
            return False
        self.send(skill_request(name, text), log_title=f"/{name} {text}".strip())
        return True

    def remove_skill(self, name: str) -> None:
        """``/skills remove <name>``: deletes a skill folder from the user's skills folder after a yes."""
        skill = self.skills.store().skills.get(name.strip())
        if skill is None or skill.folder is None:
            self.error(f"No skill named {self._safe(name, 60)!r}. /skills lists them.")
            return
        if skill.source == "project":
            self.error("That skill belongs to this project's folder. Delete .agentx/skills/<name> yourself if you "
                       "want it gone.")
            return
        if not self._yes(f"Remove the skill {skill.name} from {self._safe(self.skills.dir, 200)}? [y/N] "):
            self.notice("Nothing was removed.")
            return
        try:
            remove_skill(skill.folder.name, self.skills.dir)
        except SkillInstallError as e:
            self.error(self._safe(e, 400))
            return
        self.notice(f"Removed {skill.name}.")
