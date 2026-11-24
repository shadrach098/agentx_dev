"""The chat's model and the team's helper models.

A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from agentx_dev.LocalModel import DEFAULT_WINDOW, Local, _hostport
from agentx_dev.cli.models import (LOCAL_TYPO_HINT, SUGGESTED_MODELS, ModelError, describe_prompt, is_auto_note,
                                   is_local_name, key_variable, larger_window, local_copy_name, model_label,
                                   other_provider,
                                   parse_descriptions, parse_team_models, provider_for, runnable_local_names,
                                   team_models_line, usable_team_models)
from agentx_dev.cli.models import _acceptable_name
from agentx_dev.cli.settings import (project_settings_path, read_user_model, user_settings_path, write_project_mode,
                                     write_project_model,
                                     write_project_team_models, write_user_model, write_user_setting)


CHECK_WAIT_SEC = 150.0      # the check's own calls time out sooner (its model call after 120 s): a last resort
FALLBACK_WINDOW_WAIT_SEC = 2.0    # how long the background read of the fallback's window waits for the server

FALLBACK_NOTICE = ("{maker} failed: {reason}. This conversation continues on {to} (local). Choose a model to try "
                   "{maker} again.")
NO_KEY_START = "No key for {provider}: starting on {fallback} (local)."
FALLBACK_OFFER = ("{server} is running with {names}. Use one as the fallback when Claude or GPT fails? /fallback "
                  "{first} sets it; /fallback off stops this question.")
NO_KEY = "no key"                     # the reason a conversation started on the fallback
NO_ROOM = ("{name}'s {window:,}-token window leaves almost no room for the conversation; a variant with a larger "
           "window fixes it: a Modelfile with FROM {name} and PARAMETER num_ctx {target}, then ollama create "
           "{variant} -f Modelfile.")
ROOM_SHARE = 0.25                     # below this share of a request left for the conversation, the window is too small
HOSTED_LISTED = ("{server} lists {names}, but agentx treats names that start with gpt, claude or o and a digit as "
                 "hosted models, so they can't be used as local models. To use one here, give it a local name: "
                 "ollama cp {first} {copy}")


def maker_of(name: str) -> str:
    """``Claude`` or ``GPT`` for a hosted model's name, as the fallback notice says it; any other name as it is."""
    return {"claude": "Claude", "gpt": "GPT"}.get(provider_for(str(name or "")) or "", str(name or ""))


def server_name(url: str) -> str:
    """What the offer calls the local server: Ollama and LM Studio by their usual ports, else its address."""
    try:
        port = urlparse(str(url or "")).port
    except ValueError:
        port = None
    where = _hostport(str(url or ""))                # never a user:password@ the address may carry
    return {11434: "Ollama", 1234: "LM Studio"}.get(port or 0, f"The local server at {where}")


LOCAL_FAILED_START = "Could not use {failed} ({reason}). Starting on {to} instead; /model {failed} tries it again."


def _unusable(shown: str, reason: str) -> ModelError:
    """``Could not use <name>: <reason>``, keeping the reason on its own (``reason``) for the start's notice."""
    error = ModelError(f"Could not use {shown}: {reason}")
    error.reason = reason                                          # type: ignore[attr-defined]
    return error


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
        """Switch the model, keeping the conversation. A model that cannot be built, or a local one that fails its
        check, raises (``ModelError``) before anything changes, so the old model stays."""
        self.pick_up_saved_keys()
        assert self.session is not None
        model = self._built_model(name)
        # The runner first (it may read AGENTX.md again): a failure or Ctrl+C there leaves the old model in place.
        before = (self.addendum, self._addendum_for, self._runner_skills)
        try:
            runner = self._make_runner(model)
        except BaseException:
            self.addendum, self._addendum_for, self._runner_skills = before
            raise
        self.model = model
        self._track_usage_model(self.model_label, model, "chat")
        self.requested_model = name
        self.fallback_from = None               # a model chosen again is tried afresh
        self.session.attach(runner)

    def _built_model(self, name: Optional[str]) -> Any:
        """The model for ``name`` from the factory. A local one is checked first (the first time, the server loads
        the model, which can take a couple of minutes) and gets 8,192 tokens when its server does not say its window.
        Raises ``ModelError`` when the model cannot be built or fails the check. The check runs on its own thread
        and this one waits in short slices, so Ctrl+C (and the window's Stop) ends the wait at once; the check
        itself is left to finish on its own."""
        model = self.model_factory(name, self.env)
        if not getattr(model, "is_local", False):
            return model
        shown = self._safe(str(name or "").strip() or model_label(model), 80)
        self.progress.start(f"Loading {shown} on the local server")
        box: dict = {}

        def run() -> None:
            try:
                box["result"] = model.check()
            except BaseException as error:              # noqa: BLE001 - reported below
                box["error"] = error

        worker = threading.Thread(target=run, name="agentx-local-check", daemon=True)
        try:
            worker.start()
            waited = 0.0
            while worker.is_alive() and waited < CHECK_WAIT_SEC:     # short waits: Ctrl+C and Stop reach this thread
                worker.join(0.1)
                waited += 0.1
        finally:
            self.progress.stop()
        if worker.is_alive():
            raise _unusable(shown, f"the local server did not finish the check in {CHECK_WAIT_SEC:g}s")
        if "error" in box:
            raise _unusable(shown, f"the check failed ({type(box['error']).__name__})")
        result = box["result"]
        if not result.ok:
            hint = f". {LOCAL_TYPO_HINT}" if shown.lower().startswith(("cl", "gp")) else ""
            raise _unusable(shown, f"{self._safe(result.reason, 400)}{hint}")
        model.context_window = self._window_of(model)      # the runner keeps each request inside it
        self._warn_if_no_room(model)
        return model

    def _lean_for(self, model: Any) -> bool:
        """Whether requests to ``model`` are lean: a local model, unless ``localTools`` is "all". A lean request has
        the core tools only (``lean_tools``: no web or MCP tools) and the short skill index, and names the tools
        without repeating their descriptions (see ``AgentRunner(compact_prompt=)``)."""
        return bool(getattr(model, "is_local", False)) and getattr(self, "local_tools", "lean") != "all"

    def _fixed_chars(self, model: Any) -> int:
        """The characters each request on ``model`` would carry before the conversation: agentx's base prompt (no
        AGENTX.md or work log), the runner's own prompt for the binding mode ``model`` gets, the skill index, and the
        tools' specs; the lean request's when ``model`` gets one."""
        from agentx_dev import AgentRunner, AgentType
        from agentx_dev.cli.context import build_addendum
        from agentx_dev.cli.tools import lean_tools
        base = build_addendum(self.cwd, self.home, max_context_chars=0, max_log_chars=0)
        lean = self._lean_for(model)
        runner = AgentRunner(model=model, agent=AgentType.ReAct,
                             tools=lean_tools(self.tools) if lean else list(self.tools), system_addendum=base,
                             skills=self.skills, verbose=False, auto_cache=False, strict_tool_dispatch=True,
                             compact_prompt=lean)
        return runner.fixed_request_chars()

    def _warn_if_no_room(self, model: Any) -> None:
        """One warning (per model and window) when a local model's window leaves less than ``ROOM_SHARE`` of a
        request for the conversation once agentx's own prompt and tools are in: it is still used, but would soon stop
        with "too small". May run on a background thread (the fallback's window read)."""
        from agentx_dev.Runner.AgentRun import CHARS_PER_TOKEN, REQUEST_SHARE
        window = self._window_of(model)
        if window is None:
            return
        key = (model_label(model), window)
        with self._offer_lock:
            if key in self._room_warned:
                return
        try:
            fixed = self._fixed_chars(model)
        except Exception:                            # an estimate: when it cannot be made, nothing is said
            logging.getLogger(__name__).debug("could not estimate the fixed part of a request", exc_info=True)
            return
        budget = window * REQUEST_SHARE * CHARS_PER_TOKEN
        if budget - fixed >= ROOM_SHARE * budget:
            return
        with self._offer_lock:
            if key in self._room_warned:
                return
            self._room_warned.add(key)
        target, variant = larger_window(str(model_label(model)), window)
        self._notice_from_any_thread(NO_ROOM.format(name=self._safe(model_label(model), 80), window=window,
                                                    target=target, variant=self._safe(variant, 90)))

    @staticmethod
    def _window_of(model: Any) -> Optional[int]:
        """``model``'s window in tokens when it is a local model (8,192 when it does not say); None otherwise."""
        if not getattr(model, "is_local", False):
            return None
        window = getattr(model, "context_window", None)
        return window if isinstance(window, int) and not isinstance(window, bool) and window > 0 else DEFAULT_WINDOW

    def _local_window(self) -> Optional[int]:
        """The chat model's window in tokens when it is a local model; None for a hosted one."""
        return self._window_of(self.model)

    def _discover_local_models(self, server: str) -> None:
        """In the background at startup: the models the local server lists, for ``/model`` and the model menu. A
        refused connection answers at once and a slow server gets half a second; no answer leaves the list empty
        and says nothing (typing a local name still runs the check). Models found with no ``fallbackModel`` ever set
        bring the one-time offer to use one as the fallback. A listed gpt*, claude* or o<digit>* name (Ollama's
        ``gpt-oss:20b``) is left out, with one note saying why: choosing it would build the hosted model."""
        try:
            names = [str(name) for name in Local.models(server, timeout=0.5)]
        except Exception:
            return
        self.local_models[:] = runnable_local_names(names)
        hosted = [name for name in names if _acceptable_name(name) and name.strip() and not is_local_name(name)]
        if hosted:
            shown = [self._safe(name, 100) for name in hosted[:8]]
            self._discovery_note(HOSTED_LISTED.format(server=self._safe(server_name(server), 120),
                                                      names=", ".join(shown), first=shown[0],
                                                      copy=self._safe(local_copy_name(hosted[0]), 100)))
        self._offer_fallback(server)

    # -- the fallback model ----------------------------------------------------------------------------------------

    def _offer_fallback(self, server: str) -> None:
        """Once per run, when the local server lists models and no fallback was ever set (``/fallback off`` stops it):
        a notice offering them. It comes from the discovery thread; until the welcome box is drawn it waits for
        ``start`` to show it (``_show_fallback_offer``)."""
        usable = [name for name in self.local_models if _acceptable_name(name) and is_local_name(name)]
        if self.fallback_name is not None or not usable:
            return
        first = usable[0]                     # a plain name: what the button sends is the name itself, never cut
        names = [self._safe(name, 100) for name in usable[:8]]
        text = FALLBACK_OFFER.format(server=self._safe(server_name(server), 120), names=", ".join(names),
                                     first=names[0])
        actions = [{"label": f"Use {self._safe(first, 80)}", "send": f"/fallback {first}"},
                   {"label": "No", "send": "/fallback off"}]
        with self._offer_lock:
            if self._offer is not None:
                return
            self._offer = (text, actions)
            ready = self._offer_ready
        if ready:
            self._show_fallback_offer()

    def _discovery_note(self, text: str) -> None:
        """A note from discovery (once per run): shown at once when the welcome box is drawn, else by ``start``."""
        with self._offer_lock:
            if self._hosted_note is not None:
                return
            self._hosted_note = text
            ready = self._offer_ready
        if ready:
            self._show_fallback_offer()

    def _show_fallback_offer(self) -> None:
        """From now on the offer may be shown; show it now if discovery has made it (once; never once a fallback has
        been set or turned off). Discovery's note on hosted-looking names comes first, also once."""
        with self._offer_lock:
            self._offer_ready = True
            note = None if self._hosted_note_shown else self._hosted_note
            if note is not None:
                self._hosted_note_shown = True
            offer = None if self._offer_shown else self._offer
            if offer is not None:
                self._offer_shown = True
        if note is not None:
            self._notice_from_any_thread(note)
        if offer is None or self.fallback_name is not None:
            return
        self._notice_from_any_thread(offer[0], offer[1])

    def _notice_from_any_thread(self, text: str, actions: Any = None) -> None:
        """A notice that may come from another thread: above the terminal's open prompt (so the line being typed is
        not broken, as ``remote.py`` does), else printed (the console prints above the spinner; the window's bus
        takes it from any thread)."""
        session = getattr(getattr(self, "prompt_input", None), "_chat", None)
        application = getattr(session, "app", None)
        loop = getattr(application, "loop", None) if application is not None else None
        if loop is not None and getattr(application, "is_running", False):
            try:
                from prompt_toolkit.application import run_in_terminal
                loop.call_soon_threadsafe(lambda: run_in_terminal(lambda: self.screen.notice(text, actions=actions)))
                return
            except Exception:
                pass
        self.screen.notice(text, actions=actions)

    def _fallback_for(self, model: Any) -> Any:
        """The model a runner on ``model`` falls back to: the ``fallbackModel``, built once and not checked (the
        runner calls it only when ``model`` fails for good). None when none is set, when ``model`` is local (a local
        model never falls back) or when it cannot be built."""
        name = self.fallback_name
        if not name or model is None or getattr(model, "is_local", False) or name == self._fallback_unusable:
            return None
        built = self._fallback_built
        if built is None or built[0] != name:
            try:
                fallback = self.model_factory(name, self.env)
                why = "" if getattr(fallback, "is_local", False) else "it is not a local model"   # only ever local
            except Exception as error:
                logging.getLogger(__name__).warning("the fallback model %r could not be set up", name, exc_info=True)
                fallback, why = None, (str(error) if isinstance(error, ModelError) else type(error).__name__)
            if why:
                self._fallback_unusable = name                  # said once; not tried again in this session
                self.notice(f"The fallback {self._safe(name, 80)} could not be set up: {self._safe(why, 300)}. Claude "
                            "or GPT failing will not fall back in this session; /fallback <name> sets another.")
                return None
            self._fit_fallback_window(fallback)
            built = self._fallback_built = (name, fallback)
        self._track_usage_model(model_label(built[1]), built[1], "chat")
        return built[1]

    def _fit_fallback_window(self, fallback: Any, remember: bool = True) -> None:
        """The window of a fallback built from the settings, without its check (which loads the model): the user's
        ``localContext`` when set; else 8,192 for now, and the server is asked in the background (``server_window``)
        once; its answer applies from the next request on (the runner reads ``context_window`` before each)."""
        if self.local_context:
            fallback.context_window = self.local_context
            self._warn_if_no_room(fallback)
            return
        fallback.context_window = DEFAULT_WINDOW       # a guess until the server says: not warned about
        read = getattr(fallback, "server_window", None)
        if not callable(read):
            return

        def work() -> None:
            try:
                window = read(timeout=FALLBACK_WINDOW_WAIT_SEC)
            except Exception:                            # no answer: 8,192 stays
                return
            if isinstance(window, int) and not isinstance(window, bool) and window > 0 and not self.local_context:
                fallback.context_window = window
                self._warn_if_no_room(fallback)

        reader = threading.Thread(target=work, name="agentx-fallback-window", daemon=True)
        if remember:                                     # the fallback's own read; a team helper's is not kept
            self._fallback_window_reader = reader
        reader.start()

    def _took_over(self, event: Dict[str, Any]) -> None:
        """The runner's ``fallback`` event: the chat model failed for good and the fallback carries the turn on. The
        app moves to it for the rest of the conversation (the runner is rebuilt on it once the turn ends) and says
        so; choosing a model again clears it."""
        model = getattr(getattr(self.session, "runner", None), "model", None)
        if model is None or not getattr(model, "is_local", False):
            logging.getLogger(__name__).warning("a fallback event, but the runner's model %r is not local", model)
            return
        self.model = model
        self._track_usage_model(model_label(model), model, "chat")
        failed, reason = str(event.get("from") or ""), str(event.get("reason") or "")
        self.fallback_from = {"model": failed, "reason": reason}
        self._rebuild_after_turn = True
        maker = self._safe(maker_of(failed), 80)
        self.notice(FALLBACK_NOTICE.format(maker=maker, reason=self._safe(reason, 80),
                                           to=self._safe(model_label(model), 80)))

    def _fallback_mark(self) -> str:
        """``(on <model>, fallback from <model>: <reason>)`` while the conversation runs on the fallback; else ""."""
        if not self.fallback_from or not getattr(self.model, "is_local", False):
            return ""
        return (f"(on {model_label(self.model)}, fallback from {self.fallback_from.get('model', '')}: "
                f"{self.fallback_from.get('reason', '')})")

    def model_instead_of_the_local(self, error: Exception) -> Any:
        """At start, when the model that failed (``error``) is a local one: the next usable model, tried in order
        with ``_built_model`` (a local one is checked): your own ``model`` from ~/.agentx/settings.json, your
        ``fallbackModel``, then agentx's usual choice with no name (the first key found). Each is tried only when it
        differs from the one that failed. The first that works is used and said, in one notice; no settings file is
        rewritten. None when the failed model is not local (a cloud model keeps its own path) or none works; then
        nothing has changed. Ctrl+C (and the window's Stop) still interrupts."""
        failed = str(self.requested_model or "").strip()
        if not failed or provider_for(failed) != "local":
            return None
        own = read_user_model(self.home)
        names: List[Optional[str]] = []
        for name in (own, str(self.fallback_name or "").strip()):
            if name and name.lower() != failed.lower() and name not in names:
                names.append(name)
        names.append(None)
        for name in names:
            try:
                model = self._built_model(name)
            except Exception as candidate_error:            # not KeyboardInterrupt: that still stops the start
                logging.getLogger(__name__).info("could not start on %s either (%s)", name or "the usual model",
                                                 type(candidate_error).__name__)
                continue
            reason = getattr(error, "reason", None) or str(error)
            note = self._model_source_note()
            self.notice(LOCAL_FAILED_START.format(failed=self._safe(failed, 80), reason=self._safe(reason, 400),
                                                  to=self._safe(model_label(model), 80)) + note)
            self.requested_model = name                     # what /model, titles and a new key now build
            self.model_source = user_settings_path(self.home) if name and name == own else None
            return model
        return None

    def model_from_the_fallback(self) -> Any:
        """At start, after today's offer of the other provider's models came to nothing: when the saved model's
        provider has no key and a fallback is set, start on it (checked, as any local chat model is) and say why.
        None when that is not the case, or when the fallback fails its check (which is shown)."""
        name = self.fallback_name
        requested = str(self.requested_model or "").strip()
        provider = provider_for(requested) if requested else None
        if not name or provider == "local":
            return None
        wanted = [provider] if provider else ["claude", "gpt"]
        if any(str(self.env.get(key_variable(p)) or "").strip() for p in wanted):
            return None                                   # the key is there: the model failed for another reason
        try:
            model = self._built_model(name)
        except ModelError as error:
            self.error(self._safe(error, 600))
            return None
        except Exception as error:
            self.error(f"Could not use {self._safe(name, 80)} ({type(error).__name__}).")
            return None
        who = {"claude": "Anthropic", "gpt": "OpenAI"}.get(provider or "", "Anthropic or OpenAI")
        self.notice(NO_KEY_START.format(provider=who, fallback=self._safe(model_label(model), 80)))
        self.fallback_from = {"model": requested or "Claude or GPT", "reason": NO_KEY}
        return model

    def show_fallback(self) -> None:
        """``/fallback``: the fallback model, or that there is none."""
        name = self.fallback_name
        if name:
            lines = [f"Fallback: {self._safe(name, 80)} (local). When Claude or GPT fails for good, the conversation "
                     "continues on it. /fallback <name> changes it; /fallback off turns it off."]
        else:
            others = [self._safe(model, 80) for model in runnable_local_names(self.local_models)[:8]]
            listed = f" The local server lists: {', '.join(others)}." if others else ""
            state = "The fallback is off" if name == "" else "No fallback is set"
            lines = [f"{state}: when Claude or GPT fails for good, the turn stops.{listed} /fallback <local name> "
                     "sets one."]
        if self.fallback_from:
            lines.append(f"This conversation runs on {self._safe(self.model_label, 80)} because "
                         f"{self._safe(self.fallback_from.get('model'), 80)} failed "
                         f"({self._safe(self.fallback_from.get('reason'), 80)}); choose a model to try it again.")
        self.notice("\n".join(lines))

    def set_fallback(self, name: str) -> None:
        """``/fallback <local name>`` (checked first, then saved in ~/.agentx/settings.json) or ``/fallback off``
        (``name`` ""). It applies to this conversation at once; a save that fails is said, and the setting then lasts
        for this session only."""
        model = None
        if name:
            if not is_local_name(name):
                self.error(f"The fallback must be a local model; {self._safe(name, 80)} is not.")
                return
            try:
                model = self._built_model(name)
            except ModelError as error:
                self.error(self._safe(error, 600))
                return
        path, message = write_user_setting(self.home, "fallbackModel", name)
        self.fallback_name = name
        self._fallback_built = (name, model) if model is not None else None
        self._fallback_unusable = None
        if self.session is not None and self.model is not None and not getattr(self.model, "is_local", False):
            self.session.attach(self._make_runner(self.model))      # this conversation falls back from now on
        saved = f" {self._safe(message, 400)}" if path is not None else ""
        self.notice(f"Fallback: {self._safe(model_label(model), 80)} (local).{saved}" if name
                    else f"The fallback is off.{saved}")
        if path is None:
            self.error(self._safe(message, 400) + " It applies to this session only.")

    def save_folder_model(self, name: str) -> None:
        """``/model <name> --folder``: keep the model for this folder in its settings file."""
        if not self._memory_on:
            self.notice("No folder settings file is kept for your home folder or a drive root.")
            return
        path, message = write_project_model(self.cwd, name)
        (self.notice if path is not None else self.error)(self._safe(message, 600))

    def save_folder_mode(self, mode: str) -> None:
        """``/mode <mode> --folder``: keep default or plan mode for this folder. A folder's file may not turn the
        prompts off (a cloned project could ship one), so auto and accept-edits stay for this session and the user
        is told where to set them instead."""
        if mode not in ("default", "plan"):
            self.notice(f"{mode} mode is on for this session only: a folder's settings file cannot keep it, so a "
                        f"project you clone cannot switch off your prompts. To start in {mode} mode, use "
                        f'agentx --mode {mode}, or put "mode": "{mode}" in your own settings file '
                        f"({self._safe(user_settings_path(self.home), 300)}), which applies to every folder.")
            return
        if not self._memory_on:
            self.notice("No folder settings file is kept for your home folder or a drive root.")
            return
        path, message = write_project_mode(self.cwd, mode)
        (self.notice if path is not None else self.error)(self._safe(message, 600))

    def helper_models_line(self) -> str:
        """The approval panel's ``Helper models:`` line; "" when no extra models are set or there is no model. On a
        local chat model, a second line names the Claude and GPT helpers the job would use: the project's files go
        to them (the user listed them, so they are allowed, but the approval says so)."""
        if not self.team_models or self.model is None:
            return ""
        line = team_models_line(self.model_label, self.team_models, self.env)
        if getattr(self.model, "is_local", False):
            usable, _ = usable_team_models(self.model_label, self.team_models, self.env)
            cloud = [name for name in usable if not is_local_name(name)]
            if cloud:
                line += f"\nHelpers on {', '.join(cloud)} will see this project's files."
        return line

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
        self.error(self._safe(error, 600) + self._model_source_note())     # a failed check's reason, in full
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
