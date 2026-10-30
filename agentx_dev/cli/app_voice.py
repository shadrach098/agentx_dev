"""``/voice`` in the chat app: record until Enter, write the words out, and put them in the next input line."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from agentx_dev.cli.models import OPENAI_KEY
from agentx_dev.cli.voice import MAX_SECONDS, MIN_SECONDS, NO_KEY, Recorder, VoiceError, transcribe, voice_model

RECORDING = "● Recording… press Enter to stop (Ctrl+C cancels) "
CHECK_IT = "Check the words below, change them if you like, then press Enter to send."


class VoiceCommands:
    """Mixed into ``ChatApp``. ``_prefill`` is the text the next input line starts with."""

    _prefill: Optional[str] = None
    _recorder_factory: Callable[[], Any] = Recorder          # stand-ins in tests
    _transcriber: Callable[..., str] = staticmethod(transcribe)

    def _openai_key(self) -> str:
        for source in (lambda: self.env, self._saved_key_reader):
            try:
                value = source().get(OPENAI_KEY)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def voice(self) -> None:
        key = self._openai_key()
        if not key:
            self.error(NO_KEY)
            return
        recorder = self._recorder_factory()
        try:
            recorder.start()
        except VoiceError as error:
            self.error(str(error))
            return
        try:
            self.read_short(RECORDING)
        except (KeyboardInterrupt, EOFError):
            recorder.cancel()
            self.notice("Recording cancelled.")
            return
        except BaseException:
            recorder.cancel()
            raise
        wav, seconds = recorder.stop()
        if getattr(recorder, "full", False):
            self.notice(f"The recording stopped by itself at {MAX_SECONDS // 60} minutes.")
        if seconds < MIN_SECONDS:
            self.notice("Nothing was recorded.")
            return
        self.progress.start("Writing out what you said")
        try:
            text = self._transcriber(wav, key, model=voice_model(self.env))
        except VoiceError as error:
            self.progress.stop()
            self.error(self._safe(error))
            return
        except KeyboardInterrupt:
            self.progress.stop()
            self.notice("Stopped writing it out (Ctrl+C).")
            return
        self.progress.stop()
        if not text.strip():
            self.notice("No words were heard. Check the microphone and try /voice again.")
            return
        self._prefill = text.strip()
        self.notice(CHECK_IT)

    def _read_chat(self) -> str:
        """The next message from the input line, starting with the words from ``/voice`` when there are some."""
        if getattr(self, "_live_voice", None) is not None:
            self.notice("● Still recording (started while the agent worked): press Enter to stop, Ctrl+C to cancel.")
        prefill, self._prefill = self._prefill, None
        if not prefill:
            return self.read_line("> ")
        try:
            takes_default = "default" in inspect.signature(self.read_line).parameters
        except (TypeError, ValueError):
            takes_default = False
        if takes_default:
            return self.read_line("> ", default=prefill)
        self.notice(f"You said: {prefill}  (this input line cannot be filled in: type or paste it)")
        return self.read_line("> ")


class LiveVoice:
    """``/voice`` typed while the agent works (mixed into ``ChatApp`` with ``VoiceCommands``): the microphone starts at
    once, Enter on the line under the spinner stops it, and the words are put in the line being typed, so the user
    checks them and sends them to the agent at its next step. ``_live_voice`` is ``(recorder, entry, key)`` while it
    records; ``entry`` is its line under the spinner."""

    _live_voice: Optional[tuple] = None
    insert_input: Optional[Callable[[str], bool]] = None     # puts words in the open input line (set by main)
    set_submit_empty: Optional[Callable[[bool], None]] = None  # full-screen: Enter on an empty line is handed over
    WORDS_WAIT_SEC = 600.0                                   # how long words may wait for an input line to open

    def _submit_empty(self, on: bool) -> None:
        typeahead = getattr(self.progress, "typeahead", None)
        if typeahead is not None:
            typeahead.submit_empty = on
        if self.set_submit_empty is not None:
            try:
                self.set_submit_empty(on)
            except Exception:
                pass

    def recording(self) -> bool:
        return self._live_voice is not None

    def start_live_voice(self) -> None:
        if self._live_voice is not None:
            self._echo("/voice", "already recording: press Enter to stop", done=False)
            return
        key = self._openai_key()
        if not key:
            self._echo("/voice", NO_KEY, done=False)
            return
        recorder = self._recorder_factory()
        try:
            recorder.start()
        except VoiceError as error:
            self._echo("/voice", str(error), done=False)
            return
        entry = {"text": "/voice", "for": "recording"}
        with self._typed_lock:
            self._waiting.append(entry)
            self._live_voice = (recorder, entry, key)
        self._submit_empty(True)

    def cancel_live_voice(self) -> bool:
        """Stop recording and drop it (Ctrl+C at the input line, or ``/unsend``). False when nothing was recording."""
        with self._typed_lock:
            live, self._live_voice = self._live_voice, None
            if live is not None:
                self._waiting = [waiting for waiting in self._waiting if waiting is not live[1]]
        if live is None:
            return False
        self._submit_empty(False)
        live[0].cancel()
        return True

    def stop_live_voice(self, typed: str = "") -> None:
        """Enter while recording: stop, and write the words out on a thread of their own. ``typed`` (a line typed
        before Enter) goes back in front of the words."""
        with self._typed_lock:
            live, self._live_voice = self._live_voice, None
        if live is None:
            return
        self._submit_empty(False)
        recorder, entry, key = live
        wav, seconds = recorder.stop()
        if seconds < MIN_SECONDS:
            self._drop_waiting(entry)
            self._echo("/voice", "nothing was recorded", done=False)
            if typed:
                self._deliver_words(typed)
            return
        entry["for"] = "transcribing"
        import threading
        threading.Thread(target=self._write_out, args=(wav, key, entry, typed), name="agentx-voice",
                         daemon=True).start()

    def _drop_waiting(self, entry: Any) -> None:
        with self._typed_lock:
            self._waiting = [waiting for waiting in self._waiting if waiting is not entry]

    def _write_out(self, wav: bytes, key: str, entry: Any, typed: str) -> None:
        try:
            words = self._transcriber(wav, key, model=voice_model(self.env))
        except VoiceError as error:
            self._drop_waiting(entry)
            self._echo("/voice", self._safe(error), done=False)
            if typed:
                self._deliver_words(typed)
            return
        except Exception as error:                   # a thread of its own: report, never raise
            self._drop_waiting(entry)
            self._echo("/voice", f"could not write it out ({type(error).__name__})", done=False)
            return
        self._drop_waiting(entry)
        words = " ".join(str(words or "").split())
        if not words:
            self._echo("/voice", "no words were heard", done=False)
            if typed:
                self._deliver_words(typed)
            return
        self._deliver_words((typed + " " + words).strip())
        self._echo("/voice", "the words are in the input line: check them, then press Enter")

    def _deliver_words(self, text: str) -> None:
        """Put ``text`` in the input line that is open: the line under the spinner while the agent works, the
        full-screen input, or the prompt once the turn is over; else the next prompt starts with it."""
        import time
        deadline = time.monotonic() + self.WORDS_WAIT_SEC
        while time.monotonic() < deadline and (getattr(self.progress, "typeahead", None) is not None
                                               or self.insert_input is not None):
            typeahead = getattr(self.progress, "typeahead", None)
            if typeahead is not None and typeahead.active:
                typeahead.insert(text)
                return
            if self.insert_input is not None:
                try:
                    if self.insert_input(text):
                        return
                except Exception:
                    pass
            time.sleep(0.1)
        self._prefill = (str(self._prefill) + " " + text).strip() if self._prefill else text
