"""The skills commands that add, approve and remove skills: ``/skills install``, ``/skills trust``, ``/skills
remove`` and the ``/skills`` list. A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it.

Only the user reaches these (no tool does). An install downloads into a private temporary folder (the
quarantine: nothing there is ever loaded as a skill), inspects every skill there twice, with the pattern scan
(``Skills/Inspect.py``) and with one tool-free review by the chat model (``skill_review.py``), shows both, and
asks. Only after a yes are exactly the inspected bytes written, into this project's ``.agentx/skills`` (or, with
``--global``, into ``~/.agentx/skills`` for every folder), and their digest recorded (``skill_trust.py``): a
skill whose files later differ from the record is not offered to the agent until the user approves it again."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agentx_dev.cli.safepaths import safe_location
from agentx_dev.cli.skill_review import Review, review
from agentx_dev.Skills import SkillInstallError, discover, install, open_source, preview_text, remove_skill
from agentx_dev.Skills.Inspect import DANGER, NOTE, WARNING, Report, Snapshot, SnapshotError, scan, snapshot, \
    snapshot_files

SHOWN_FINDINGS = 12
SERIOUS_PROMPT = ("Serious findings above. Type install to go ahead anyway, or press Enter to stop: ")


class SkillCommands:
    """Mixed into ``ChatApp``, whose attributes these methods use (``skills``, ``skill_trust``, ``cwd``...)."""

    # -- where ------------------------------------------------------------------------------------------------

    def _project_skills_dir(self) -> Optional[Path]:
        """This project's ``.agentx/skills``, or None (the home folder, a drive root, or a link out of it)."""
        if not self._memory_on or not safe_location(self.cwd, "skills"):
            return None
        return self.cwd / ".agentx" / "skills"

    # -- inspecting ---------------------------------------------------------------------------------------------

    def _inspect(self, name: str, snap: Snapshot) -> Tuple[Report, Review]:
        report = scan(snap)
        if self.model is None:
            return report, Review(problem="no model is available for the review")
        self.progress.start(f"Reviewing {name} with {self._safe(self.model_label, 60)}")
        try:
            found = review(snap, self._summary_call)
        finally:
            self.progress.stop()
        return report, found

    def _inspection_text(self, name: str, snap: Snapshot, report: Report, found: Review) -> str:
        counts = ", ".join(f"{report.count(level)} {level}" for level in (DANGER, WARNING, NOTE))
        lines = [f"Inspection of {name} ({len(snap.files)} file{'s' if len(snap.files) != 1 else ''}):",
                 f"  Pattern scan: {counts}."]
        shown = report.findings[:SHOWN_FINDINGS]
        lines += [f"    {self._safe(f.shown(), 240)}" for f in shown]
        left = len(report.findings) - len(shown) + report.more
        if left:
            lines.append(f"    ... and {left} more")
        if found.ok:
            lines.append(f"  Model review ({self._safe(self.model_label, 60)}): {found.verdict}. "
                         f"{self._safe(found.summary, 240)}")
            lines += [f"    {self._safe(concern, 260)}" for concern in found.concerns]
            if not found.complete:
                lines.append("    (it saw only part of the files: they were over its size limit)")
        else:
            lines.append(f"  Model review: none - {self._safe(found.problem, 120)}. Treat {name} as unreviewed.")
        return "\n".join(lines)

    @staticmethod
    def _serious(report: Report, found: Review) -> bool:
        return report.count(DANGER) > 0 or not found.ok or found.verdict == "dangerous"

    def _confirm(self, question: str, serious: bool) -> bool:
        """A yes, and when something serious was found, the word ``install`` typed as well."""
        if not self._yes(question):
            return False
        if not serious:
            return True
        try:
            answer = str(self.read_short(SERIOUS_PROMPT)).strip().lower()
        except (EOFError, KeyboardInterrupt, Exception):
            return False
        return answer == "install"

    # -- /skills install ------------------------------------------------------------------------------------

    def install_skills(self, arg: str) -> None:
        """``/skills install [--global] [name ...] <link>``. Only the user can start this (no tool does), and nothing
        is written until they have seen the inspection and said yes. Names before a github.com link pick those
        skills only. Without ``--global`` the skills go into this project."""
        words = arg.split()
        everywhere = "--global" in words
        words = [w for w in words if w != "--global"]
        if not words:
            self.error("Usage: /skills install [--global] [name ...] <github link or folder>")
            return
        link, wanted = " ".join(words), []
        if len(words) > 1 and "github.com" in words[-1].lower() and "github.com" not in " ".join(words[:-1]).lower():
            link, wanted = words[-1], [w.strip(",").lower() for w in words[:-1] if w.strip(",")]
        if everywhere:
            target, scope, where = self.skills.dir, "global", "every folder"
        else:
            target, scope, where = self._project_skills_dir(), "project", "this project"
            if target is None:
                self.error("This folder is not a project (your home folder or a drive root), so a skill cannot be "
                           "installed into it. Add --global to install it for every folder.")
                return
        self.notice("Downloading into a private temporary folder (nothing there is used until you say yes)...")
        try:
            with open_source(link) as fetched:
                found, problems = discover(fetched.root, fetched.subpath)
                if found and wanted:
                    picked = [c for c in found if c.name.lower() in wanted]
                    if not picked:
                        self.error(f"No skill named {', '.join(self._safe(w, 40) for w in wanted)} there. It has: "
                                   + ", ".join(c.name for c in found) + ".")
                        return
                    found = picked
                if not found:
                    skipped = "".join(f"\n  {self._safe(p, 160)}" for p in problems[:5])
                    self.error("No skills found there (a skill is a folder with a SKILL.md file)." + skipped)
                    return
                clash = self._name_clash([c.name for c in found], scope)
                if clash:
                    self.error(clash)
                    return
                self.notice(preview_text(fetched, found, problems))
                snaps: Dict[str, Snapshot] = {}
                serious = False
                for candidate in found:
                    try:
                        snap = snapshot_files(candidate.folder, candidate.files)
                    except SnapshotError as e:
                        self.error(f"{candidate.name} cannot be inspected ({self._safe(e, 160)}), so nothing was "
                                   "installed.")
                        return
                    report, found_review = self._inspect(candidate.name, snap)
                    self.notice(self._inspection_text(candidate.name, snap, report, found_review))
                    serious = serious or self._serious(report, found_review)
                    snaps[candidate.name] = snap
                question = (f"Install {len(found)} skill{'s' if len(found) != 1 else ''} into "
                            f"{self._safe(target, 200)} ({where})? The agent will read and follow "
                            f"{'them' if len(found) != 1 else 'it'}. [y/N] ")
                if not self._confirm(question, serious):
                    self.notice("Nothing was installed.")
                    return
                names = install(found, target, fetched, contents=snaps)
                for name in names:
                    self._record_skill(target / name, scope, how="installed with /skills install",
                                       origin=fetched.label + (f" (commit {fetched.commit[:10]})"
                                                               if fetched.commit else ""))
        except SkillInstallError as e:
            self.error(self._safe(e, 400))
            return
        self.reload_skills()
        self.notice("Installed: " + ", ".join(names) + f" ({where}). " + ("They are" if len(names) != 1 else "It is")
                    + " ready in this conversation: " + ", ".join(f"/{name}" for name in names)
                    + " or ask for it by name.")

    def _name_clash(self, names: List[str], scope: str) -> str:
        """Why these names cannot go in ``scope`` ("" when they can): a skill for every folder wins over a project's
        skill of the same name, so a project install under a name already used there would never be offered."""
        if scope != "project" or self.skills.dir is None:
            return ""
        taken = [n for n in names if (self.skills.dir / n).exists()]
        if taken:
            return (f"{', '.join(taken)} is already installed for every folder, which would win over this project's "
                    "copy. Remove it first (/skills remove --global <name>), or install with --global. Nothing was "
                    "installed.")
        return ""

    def _record_skill(self, folder: Path, scope: str, *, how: str, origin: str = "", note: str = "") -> None:
        try:
            self.skill_trust.record(folder, snapshot(folder), scope=scope, how=how, origin=origin, review=note)
        except (SnapshotError, OSError) as e:
            self.error(f"{folder.name} was written but its record could not be kept ({type(e).__name__}); it stays "
                       f"held back until /skills trust {folder.name}.")

    # -- /skills trust ----------------------------------------------------------------------------------------

    def trust_skill(self, name: str) -> None:
        """``/skills trust <name>``: inspect a skill that is on disk but held back (copied in by hand, changed, or
        part of a cloned project) and, after a yes, approve it exactly as it is now."""
        name = name.strip()
        skill = self.skills.store().skills.get(name)
        if skill is None or skill.folder is None:
            self.error(f"No skill named {self._safe(name, 60)!r} on disk. /skills lists them.")
            return
        if not self.skills.held_back(skill):
            self.notice(f"{skill.name} is already approved and unchanged.")
            return
        try:
            snap = snapshot(skill.folder)
        except SnapshotError as e:
            self.error(f"{skill.name} cannot be inspected ({self._safe(e, 160)}): it stays held back.")
            return
        report, found_review = self._inspect(skill.name, snap)
        self.notice(self._inspection_text(skill.name, snap, report, found_review))
        question = (f"Approve {skill.name} as it is now ({self._safe(skill.folder, 200)})? The agent will read and "
                    "follow it. [y/N] ")
        if not self._confirm(question, self._serious(report, found_review)):
            self.notice(f"{skill.name} stays held back.")
            return
        try:
            again = snapshot(skill.folder)
        except SnapshotError:
            again = None
        if again is None or again.digest != snap.digest:
            self.error(f"{skill.name} changed while you were looking at it, so it was not approved. Run /skills "
                       f"trust {skill.name} again.")
            return
        scope = "project" if skill.source == "project" else "global"
        verdict = found_review.verdict if found_review.ok else "not reviewed"
        self.skill_trust.record(skill.folder, snap, scope=scope, how="approved with /skills trust",
                                review=f"{report.count(DANGER)} danger, {report.count(WARNING)} warning; model: {verdict}")
        self.reload_skills()
        self.notice(f"Approved {skill.name}: it is ready in this conversation.")

    # -- /skills remove ---------------------------------------------------------------------------------------

    def remove_skill(self, arg: str) -> None:
        """``/skills remove [--global] <name>``: delete a skill folder after a yes, and forget its record. Without
        ``--global`` this project's copy goes when there is one, else the one for every folder."""
        words = arg.split()
        everywhere = "--global" in words
        name = " ".join(w for w in words if w != "--global").strip()
        project = self._project_skills_dir()
        here = project / name if project is not None and not everywhere else None
        if here is not None and here.is_dir():
            target, where = project, "this project"
        elif self.skills.dir is not None and (self.skills.dir / name).is_dir():
            target, where = self.skills.dir, "every folder"
        else:
            self.error(f"No skill named {self._safe(name, 60)!r}. /skills lists them.")
            return
        if not self._yes(f"Remove the skill {self._safe(name, 60)} from {self._safe(target, 200)} ({where})? [y/N] "):
            self.notice("Nothing was removed.")
            return
        try:
            remove_skill(name, target)
        except SkillInstallError as e:
            self.error(self._safe(e, 400))
            return
        self.skill_trust.forget(target / name)
        self.reload_skills()
        self.notice(f"Removed {self._safe(name, 60)}.")

    # -- what there is ----------------------------------------------------------------------------------------

    def _held_skills(self) -> List[Tuple[str, str]]:
        """``[(name, why)]`` for every skill on disk that is not offered to the agent."""
        held = []
        for skill in self.skills.store().all():
            why = self.skills.held_back(skill)
            if why:
                held.append((skill.name, why))
        return held

    def _announce_skills(self) -> None:
        """At start: record the skills already installed the first time (see skill_trust.py), and say which skills
        on disk are held back."""
        try:
            adopted = self.skill_trust.adopt_existing(self.skills.dir)
        except Exception:                         # a record that cannot be kept must not stop agentx
            adopted = []
        if adopted:
            self.notice(f"agentx now keeps a record of each skill's files, so a skill cannot be added or changed "
                        f"behind your back. The {len(adopted)} skill{'s' if len(adopted) != 1 else ''} already in "
                        f"{self._safe(self.skills.dir, 200)} {'were' if len(adopted) != 1 else 'was'} recorded as "
                        "they are.")
        held = self._held_skills()
        if held:
            names = ", ".join(self._safe(n, 40) for n, _ in held[:8]) + (f" and {len(held) - 8} more"
                                                                          if len(held) > 8 else "")
            self.notice(f"Skills held back (not offered to the agent): {names}. They were not installed or approved "
                        "with /skills, or changed since. /skills trust <name> inspects one; /skills lists why.")

    def show_skills(self) -> None:
        """``/skills``: every skill found on disk, which ones this conversation's agent has (shown as the ``/name``
        that runs them), which are held back and why, and how to add, approve or remove one."""
        from agentx_dev.cli.commands import command_names
        store = self.skills.store()
        taken = set(command_names())

        def label(skill: Any) -> str:
            if skill.name not in self._runner_skills:
                return skill.name
            return f"/{skill.name}" if f"/{skill.name}" not in taken else f"/skills use {skill.name}"

        lines = []
        for s in store.all():
            held = self.skills.held_back(s)
            note = (f"  [held back: {self._safe(held, 160)}]" if held else
                    "" if s.name in self._runner_skills else "  [not in this conversation yet: /skills reload]")
            lines.append(f"  {label(s)} ({s.source}) - {self._safe(s.description, 100)}{note}")
        lines += [f"  Skipped: {self._safe(problem, 160)}" for problem in store.problems[:5]]
        shown = "\n".join(lines) if lines else "  none yet; the built-in ones are created when a conversation starts"
        project = self._project_skills_dir()
        self.notice(f"Skills (the agent reads one when its work matches):\n{shown}\n"
                    "Use one with /<skill> [what you want] (type / to pick from the list). "
                    f"For every folder they live in {self._safe(self.skills.dir, 300)}"
                    + (f"; for this project in {self._safe(project, 300)}" if project is not None else "") + ". "
                    "/skills install <github link or folder> inspects skills and installs them into this project "
                    "(--global: every folder); /skills trust <name> inspects and approves one that is held back; "
                    "/skills remove <name> deletes one.")
