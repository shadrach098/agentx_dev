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
