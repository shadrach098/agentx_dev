"""Team jobs on the screen and the skills commands.

A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agentx_dev.cli.approval import DECLINED
from agentx_dev.cli.commands import command_names, skill_request
from agentx_dev.cli.routing import NUDGE, TEAM_DECLINED, Route, assess, by_rule
from agentx_dev.cli.settings import write_user_auto_team
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

    # -- automatic delegation (routing.py) ---------------------------------------------------------------------

    def _decide_route(self, text: str, carry_on: bool) -> Optional[Route]:
        """This message's route, shown with its reason; None when agentx does not route (turned off, plan mode, no
        model). Ctrl+C here stops the turn like any other."""
        if not self.auto_team or self.model is None or self.gate.mode == "plan":
            return None
        route = by_rule(text, carry_on)
        if route is None:
            self.progress.start("Sizing up the request")
            try:
                route = assess(text, self._summary_call)
            finally:
                self.progress.stop()
        if route.reason:
            self.notice(self._safe(route.shown(), 200))
        return route

    def _route_guarded(self, tool: Any) -> Any:
        """``tool`` for the main agent: refused while this message's route says it is the team's work (see
        ``Route.refusal``). The team tool and a team's helpers are never held back by it."""
        inner = tool.func

        def routed(**kwargs):
            route = self._route
            refusal = (route.refusal(tool.name) if route is not None and current_status() is None
                       and not self.gate.closed else "")           # wrapping up: the gate's own refusal
            return refusal if refusal else inner(**kwargs)

        routed.__name__, routed.__qualname__, routed.__module__ = tool.name, f"routed.{tool.name}", __name__
        wrapped = type(tool)(func=routed, args_schema=tool.args_schema, name=tool.name, description=tool.description)
        for attr in ("cacheable", "mcp_server", "mcp_remote"):
            if hasattr(tool, attr):
                setattr(wrapped, attr, getattr(tool, attr))
        wrapped.cacheable = False
        return wrapped

    def _route_nudge(self, answered: bool, said: str = "") -> str:
        """After the agent's turn: the reminder to send once when a message routed to a team got an answer with no
        team job, else "". An answer that asks the user something is left alone: the user answers first. The
        second time it only says so; /team hands it over for sure."""
        route = self._route
        if route is None or not route.team or route.started or route.blocked or not answered or self.gate.closed:
            return ""
        if said.rstrip().rstrip("*_`)\"'").endswith("?"):
            return ""
        if route.nudged:
            self.notice("The agent answered without the team this request was routed to. /team <task> hands a job "
                        "to the team for sure.")
            return ""
        route.nudged = True
        self.notice("No team job started for a request routed to a team: reminding the agent once.")
        self.progress.start("Thinking")
        return NUDGE.format(reason=route.reason or "a large job")

    def show_auto_team(self) -> None:
        self.notice(f"Automatic teams are {'on' if self.auto_team else 'off'}. With them on, agentx sizes up each "
                    "message first: a large, multi-part job is handed to a team (you still approve it), a small one "
                    "is done directly, and it says why. /team auto off turns this off (saved for next time); /team "
                    "<task> always uses a team.")

    def set_auto_team(self, on: bool) -> None:
        self.auto_team = bool(on)
        _, message = write_user_auto_team(self.home, on)
        self.notice(f"Automatic teams {'on' if on else 'off'}. {self._safe(message, 300)}")

    def _within_job_limit(self, gated: Any) -> Any:
        """``gated`` (the team tool behind the gate) with the jobs-per-message limit checked first, so a call past
        ``MAX_TEAM_JOBS_PER_MESSAGE`` is refused without asking the user again. Other calls go through the gate
        as before; a job the user declined does not count (``_team_jobs`` grows only when a job starts), and after
        it no other team job starts in this message. A message routed to a team learns whether its job started,
        was declined or failed."""
        inner = gated.func

        def once(**kwargs):
            refusal = self._team_refusal()
            if refusal:
                return refusal
            jobs = self._team_jobs
            result = inner(**kwargs)
            route = self._route
            if self._team_jobs > jobs:                    # a job ran
                if route is not None and route.team:
                    route.started = True
                    route.blocked = "failed" if self._team_stalled else ""
            elif isinstance(result, str) and (result == DECLINED or result.startswith("The user declined")):
                self._team_declined = True
                if route is not None and route.team and not route.started:
                    if result == DECLINED:            # a plain no: ask before working alone
                        route.blocked = "was declined"
                    else:                             # no, with what to do instead: that is the user's answer
                        route.team = False
            return result

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
        from agentx_dev.cli.team_panel import TeamPanel

        if getattr(self, "_team_panel", None) is None:
            self._team_panel = TeamPanel()
        self._team_panel.notify(kind, data)
        self.progress.stop()
        if kind == "plan":
            steps = data.get("steps") or []
            self._team_steps = (0, len(steps))
            self._team_plan = [step for step in steps if isinstance(step, dict)]
            self.screen.team_plan(steps)
            if getattr(self.progress, "typeahead", None) is not None or self.fullscreen:
                self.notice("Type to add a note for the team's supervisor · /skip <step> drops a step")
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
                                  status=status,
                                  fallback={"to": data.get("fallback"), "from": data.get("fallback_from"),
                                            "reason": data.get("fallback_reason")})
            self.progress.start("Team is working")
        elif kind == "notice":                     # the planner moved to the fallback: said once, real names
            self.notice(self._safe(data.get("text") or "", 300))
            self.progress.start("Team is working")
        elif kind == "error":
            # why the whole job failed, for the person: the agent is told only the kind of failure (the provider's
            # words can name the model its planner must not learn)
            self.error(f"The team job failed: {self._safe(data.get('text') or 'no reason given', 500)}")

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
        if self._team_declined:
            return TEAM_DECLINED
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
                 + ("" if s.name in self._runner_skills else "  [not in this conversation yet: /skills reload]")
                 for s in store.all()]
        lines += [f"  Skipped: {self._safe(problem, 160)}" for problem in store.problems[:5]]
        shown = "\n".join(lines) if lines else "  none yet; the built-in ones are created when a conversation starts"
        self.notice(f"Skills (the agent reads one when its work matches):\n{shown}\n"
                    "Use one with /<skill> [what you want] (type / to pick from the list). "
                    f"They live in {self._safe(self.skills.dir, 300)}. /skills install <github link or folder> adds "
                    "more and /skills remove <name> deletes one, both at once in this conversation; /skills reload "
                    "picks up a folder copied in by hand. Skills for tools this agent does not have stay inactive.")

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
        self.reload_skills()
        self.notice(f"Removed {skill.name}.")
