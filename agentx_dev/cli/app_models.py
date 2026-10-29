"""The chat's model and the team's helper models.

A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from agentx_dev.cli.models import (SUGGESTED_MODELS, ModelError, describe_prompt, is_auto_note, key_variable,
                                   other_provider, parse_descriptions, parse_team_models, provider_for,
                                   team_models_line, usable_team_models)
from agentx_dev.cli.settings import (project_settings_path, user_settings_path, write_project_model,
                                     write_project_team_models, write_user_model)


class ModelCommands:
    """Mixed into ``ChatApp``, whose attributes these methods use."""

    def _announce_project_team_models(self) -> None:
        """A repository's own ``teamModels`` can send a team job's work to another provider or a dearer model, and
        in auto mode (or with an allow rule for the team tool) nothing else would say so: one notice, no prompt.
        A list from the user's own settings, or one set in this session, is not announced."""
        source = self.team_models_source
        if not self.team_models or source is None:
            return
        try:
            from_project = Path(source).resolve() == project_settings_path(self.cwd).resolve()
        except OSError:
            from_project = False
        if not from_project:
            return
        self.notice(f"This project's settings let team helpers also use: "
                    f"{self._safe(self._listed_with_uses(), 800)} (from {self._safe(source, 300)}). "
                    "/team-models clear turns that off for this session.")

    def switch_model(self, name: str) -> None:
        """Switch the model, keeping the conversation. A model that cannot be built raises (``ModelError``)
        before anything changes, so the old model stays."""
        model = self.model_factory(name, self.env)
        self.model = model
        self._track_usage_model(self.model_label, model, "chat")
        self.requested_model = name
        assert self.session is not None
        self.session.attach(self._make_runner(model))

    def save_folder_model(self, name: str) -> None:
        """``/model <name> --folder``: keep the model for this folder in its settings file."""
        if not self._memory_on:
            self.notice("No folder settings file is kept for your home folder or a drive root.")
            return
        path, message = write_project_model(self.cwd, name)
        (self.notice if path is not None else self.error)(self._safe(message, 600))

    def helper_models_line(self) -> str:
        """The approval panel's ``Helper models:`` line; "" when no extra models are set or there is no model."""
        if not self.team_models or self.model is None:
            return ""
        return team_models_line(self.model_label, self.team_models, self.env)

    def show_team_models(self) -> None:
        """``/team-models``: the list, where it came from, and which entries a job would use now."""
        if not self.team_models and self.team_models_source is not None:
            self.notice(f"Helper models: none - turned off by {self._safe(self.team_models_source, 300)}, so team "
                        f"helpers use the chat's model ({self._safe(self.model_label, 80)}). Set some with "
                        "/team-models <name> [<name> ...]; add --folder to keep them for this folder.")
            return
        if not self.team_models:
            self.notice("Helper models: none set, so team helpers use the chat's model "
                        f"({self._safe(self.model_label, 80)}). Set some with /team-models <name> [<name> ...]; "
                        "add --folder to keep them for this folder.")
            return
        usable, notes = usable_team_models(self.model_label, self.team_models, self.env)
        source = (f" (from {self._safe(self.team_models_source, 300)})" if self.team_models_source
                  else " (this session)")
        lines = [f"Helper models{source}: {self._safe(self._listed_with_uses(), 800)}",
                 f"A team job may use: {self._safe(self.model_label, 110)} (chat"
                 + (f"; {self._safe(self._use_of(self.model_label), 90)})" if self._use_of(self.model_label) else ")")
                 + "".join(f", {self._safe(name, 110)}" for name in usable)]
        lines += [self._safe(note.replace('Not used:', 'Not used now:', 1), 200) for note in notes]
        lines.append('Change them with /team-models <name>[="what it is for"] ..., /team-models clear, or add --folder '
                     "to save.")
        self.notice("\n".join(lines))

    def _use_of(self, name: str) -> str:
        """The user's note on what model ``name`` is for ("" when none); names match in any case."""
        wanted = str(name).strip().lower()
        return next((use for listed, use in self.team_model_uses.items() if listed.lower() == wanted), "")

    def _with_use(self, name: str) -> str:
        use = self._use_of(name)
        return f"{name} ({use})" if use else name

    def _listed_with_uses(self) -> str:
        """The list as shown: ``gpt-6.1-sol (hard coding), gpt-5.5``."""
        return ", ".join(self._with_use(name) for name in self.team_models)

    def set_team_models(self, names: Sequence[str], folder: bool = False) -> None:
        """``/team-models <name>[=<what it is for>] ...`` (session) or ``... --folder`` (also saved in this
        folder's settings file)."""
        cleaned, uses, problems = parse_team_models(list(names))
        for problem in problems:
            self.error(self._safe(problem, 300))
        if names and not cleaned:
            self.error("No usable model name was given, so the list was left unchanged.")
            return
        self.team_models = cleaned
        self.team_model_uses = uses
        self.team_models_source = None
        if cleaned:
            self._apply_known_notes()             # what the chat model already said this session is kept
            self._describe_models()
        report = self._save_team_models_in_folder() if folder else None
        self.show_team_models()               # after the write, so it names the file as the source
        if report is not None:
            report[0](report[1])

    def describe_team_models(self, folder: bool = False) -> None:
        """``/team-models describe``: ask the chat model again what each model is good for, replacing only its own
        (``auto:``) notes, then show the list; with ``folder`` the list and its notes are saved in this folder's
        settings file. The list itself and where it came from are not changed."""
        if not self.team_models:
            self.notice("No helper models are set, so there is nothing to describe. Set some with "
                        "/team-models <name> ...")
            return
        self._describe_models(force=True)
        report = self._save_team_models_in_folder() if folder else None
        self.show_team_models()
        if report is not None:
            report[0](report[1])

    def _save_team_models_in_folder(self) -> Tuple[Callable[[str], None], str]:
        """Write the list and its notes to this folder's settings file; what to print about it."""
        if not self._memory_on:
            return self.notice, "No folder settings file is kept for your home folder or a drive root."
        path, message = write_project_team_models(self.cwd, self.team_models, self.team_model_uses)
        if path is not None:
            self.team_models_source = path
        return (self.notice if path is not None else self.error), self._safe(message, 600)

    def _apply_known_notes(self) -> None:
        """Give each model on the list (and the chat's own) the note the chat model already wrote for it this
        session, unless it has a note."""
        for name in [self.model_label, *self.team_models]:
            known = self._auto_notes.get(name.lower(), "")
            if known and not self._use_of(name):
                self.team_model_uses[name] = known

    def _describe_models(self, force: bool = False) -> None:
        """Ask the chat model, in one call, what each model a team may use (the list and the chat's own) is good
        for, and keep the answers as ``auto:`` notes. A note the user wrote is never asked about or replaced.
        Each model is asked about once per session (its answer, or the lack of one, is remembered and reused
        when the list is set again); ``force`` asks again about every model without a note of the user's. A
        model the chat model does not know gets no note. A failure or an unreadable reply gives one notice;
        Ctrl+C skips it. The reply is model output: only short, printable notes for the names asked about are
        kept."""
        if self.model is None:
            return
        wanted: List[str] = []
        for name in [self.model_label, *self.team_models]:
            note = self._use_of(name)
            if name.lower() in (w.lower() for w in wanted) or (note and not is_auto_note(note)):
                continue
            if force or (not note and name.lower() not in self._auto_notes):
                wanted.append(name)
        if not wanted:
            self._apply_known_notes()
            return
        for name in wanted:                      # asked once, whatever happens below
            self._auto_notes.setdefault(name.lower(), "")
        self.progress.start("Asking the model about the helper models")
        try:
            reply = self._summary_call(describe_prompt(wanted))
        except KeyboardInterrupt:
            self.progress.stop()
            self.notice("Skipped describing the helper models.")
            return
        except Exception as error:
            self.progress.stop()
            self.notice(f"Could not describe the models ({type(error).__name__}); add notes yourself with "
                        '/team-models <name>="what it is for".')
            return
        self.progress.stop()
        found = parse_descriptions(reply, wanted)
        if found is None:
            self.notice("The model's answer about the helper models could not be read; add notes yourself with "
                        '/team-models <name>="what it is for", or try /team-models describe.')
            return
        for name in wanted:
            self._auto_notes[name.lower()] = found.get(name, "")
            if name not in found:                 # now unknown: an older auto note must not linger
                for key in [key for key, use in self.team_model_uses.items()
                            if key.lower() == name.lower() and is_auto_note(use)]:
                    del self.team_model_uses[key]
        for name, note in found.items():
            for key in [key for key in self.team_model_uses if key.lower() == name.lower()]:
                del self.team_model_uses[key]
            self.team_model_uses[name] = note
        self._apply_known_notes()
        unknown = [name for name in wanted if name not in found]
        if unknown:
            self.notice("No note for " + self._safe(", ".join(unknown), 400) + ": the chat model does not know "
                        'what they are good at. Add one with /team-models <name>="what it is for" if you know.')

    # -- no key for the saved model ----------------------------------------------------------------------------

    def model_from_the_other_key(self, error: Exception) -> Any:
        """At start, the saved model could not be built (``error``). When its provider has no key but the other
        provider's key is set (you moved from GPT to Claude, say), offer that provider's models, use the one
        picked, and offer to save it where the old name came from. None: nothing was picked (the error stays)."""
        self.error(self._safe(error) + self._model_source_note())
        other = other_provider(self.requested_model, self.env)
        if other is None:
            return None
        name = self._pick_model_of(other)
        if not name:
            return None
        try:
            model = self.model_factory(name, self.env)
        except Exception as e:                      # never echo a constructor's message beyond ModelError's own
            self.error(f"Could not use {name}: " + (self._safe(e) if isinstance(e, ModelError) else type(e).__name__))
            return None
        old, self.requested_model = self.requested_model, name
        self._offer_to_save_model(old, name)
        return model

    def _pick_model_of(self, provider: str) -> Optional[str]:
        maker = "Claude" if provider == "claude" else "GPT"
        self.notice(f"{key_variable(provider)} is set, so you can use a {maker} model instead.")
        options = SUGGESTED_MODELS.get(provider, ())
        labels = [f"{name}  ({use})" if use else name for name, use in options]
        if options and self.select is not None:                # arrow keys; None when there is no console
            picked = self.select(labels + ["Type another name", "Quit"], 0)
            if picked is None or picked == len(labels) + 1:
                return None
            if picked < len(labels):
                return options[picked][0]
        elif options:
            self.notice("\n".join(f"  {number}. {label}" for number, label in enumerate(labels, 1)))
        prompt = "Model (number or name, blank to quit): " if options else f"{maker} model name (blank to quit): "
        try:
            answer = str(self.read_short(prompt)).strip()
        except (EOFError, KeyboardInterrupt, Exception):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        if answer and provider_for(answer) == provider:
            return answer[:80]
        if answer:
            self.error(f"{self._safe(answer, 60)!r} is not a {maker} model name.")
        return None

    def _offer_to_save_model(self, old: Optional[str], name: str) -> None:
        """Save ``name`` in the settings file the old name came from, if the user says so (a --model name: no)."""
        source = self.model_source
        if source is None:
            return
        where = self._safe(source, 300)
        if not self._yes(f"Save {name} in {where} in place of {self._safe(old, 60)}? [y/N] "):
            self.notice(f"Using {name} for this session only.")
            return

        def same(a: Any, b: Any) -> bool:
            try:
                return Path(a).resolve() == Path(b).resolve()
            except (OSError, ValueError, TypeError):
                return False

        if same(source, user_settings_path(self.home)):
            path, message = write_user_model(self.home, name)
        elif same(source, project_settings_path(self.cwd)):
            path, message = write_project_model(self.cwd, name)
        else:
            self.notice(f"Using {name} for this session; change {where} yourself to keep it.")
            return
        (self.notice if path is not None else self.error)(self._safe(message, 400))
