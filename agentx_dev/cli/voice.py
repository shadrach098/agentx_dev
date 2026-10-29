"""``/voice``: record from the microphone, write out what was said with OpenAI's speech-to-text, and put the words
in the input line for the user to check before sending.

Claude models take no audio, so the words always come from OpenAI (``OPENAI_API_KEY``), whichever model the chat
uses. The microphone is read with the ``sounddevice`` package (``pip install "agentx-dev[voice]"``)."""

from __future__ import annotations

import io
import threading
import wave
from typing import Any, Callable, List, Mapping, Optional, Tuple

SAMPLE_RATE = 16_000            # what speech-to-text works at; 32 KB a second as 16-bit mono
MAX_SECONDS = 10 * 60           # OpenAI takes files up to 25 MB; 10 minutes is about 19 MB
MIN_SECONDS = 0.3               # shorter than this is a stray key press, not speech
VOICE_MODEL = "gpt-4o-transcribe"
FALLBACK_MODEL = "whisper-1"    # for an account or endpoint without the newer model
MODEL_VARIABLE = "AGENTX_VOICE_MODEL"
TRANSCRIBE_TIMEOUT_SEC = 120.0

NO_PACKAGE = ('/voice needs the sounddevice package to use the microphone: python -m pip install "agentx-dev[voice]" '
              "(or python -m pip install sounddevice numpy), then start agentx again.")
NO_KEY = ("/voice writes out what you say with OpenAI's speech-to-text (Claude models take no audio), which needs "
          "OPENAI_API_KEY. Set it and start agentx again.")


class VoiceError(Exception):
    """Something the user can act on: no package, no microphone, the transcription failed."""


class Recorder:
    """Records 16 kHz mono from the default microphone until ``stop`` (or ``MAX_SECONDS``).

    ``stream_factory`` stands in for ``sounddevice.RawInputStream`` in tests."""

    def __init__(self, stream_factory: Optional[Callable[..., Any]] = None, fallback_rate: int = 0) -> None:
        self._factory = stream_factory
        self._fallback_rate = fallback_rate        # with ``stream_factory``: the rate to try after SAMPLE_RATE
        self._chunks: List[bytes] = []
        self._frames = 0
        self._lock = threading.Lock()
        self._stream: Any = None
        self.rate = SAMPLE_RATE          # the microphone's own rate when it cannot record at SAMPLE_RATE
        self.full = False                # MAX_SECONDS reached: recording stopped by itself

    def _callback(self, data: Any, frames: int, time: Any, status: Any) -> None:
        with self._lock:
            if self._frames >= MAX_SECONDS * self.rate:
                self.full = True
                return
            self._chunks.append(bytes(data))
            self._frames += frames

    def start(self) -> None:
        factory = self._factory
        if factory is None:
            try:
                import sounddevice
            except ImportError as error:
                raise VoiceError(NO_PACKAGE) from error
            except OSError as error:            # the package is there but PortAudio is not
                raise VoiceError(f"The microphone library could not load ({error}).") from error
            factory = sounddevice.RawInputStream
            rates = [SAMPLE_RATE, _default_rate(sounddevice)]
        else:
            rates = [SAMPLE_RATE, self._fallback_rate]
        error: Optional[Exception] = None
        for rate in dict.fromkeys(r for r in rates if r):       # a microphone that cannot do 16 kHz: its own rate
            try:
                self.rate = rate
                self._stream = factory(samplerate=rate, channels=1, dtype="int16", callback=self._callback)
                self._stream.start()
                return
            except Exception as failure:
                self._close()
                error = failure
        if error is not None:
            self._stream = None
            raise VoiceError(f"No microphone could be opened ({_short(error)}). Check that one is connected and "
                             "that this terminal may use it (Windows: Settings > Privacy > Microphone).") from error

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for step in ("stop", "close"):
            try:
                getattr(stream, step)()
            except Exception:
                pass

    def cancel(self) -> None:
        self._close()
        with self._lock:
            self._chunks, self._frames = [], 0

    def stop(self) -> Tuple[bytes, float]:
        """``(the recording as WAV, its length in seconds)``."""
        self._close()
        with self._lock:
            pcm, frames = b"".join(self._chunks), self._frames
        return to_wav(pcm, self.rate), frames / self.rate


def _default_rate(sounddevice: Any) -> int:
    try:
        return int(sounddevice.query_devices(kind="input")["default_samplerate"])
    except Exception:
        return 0


def to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(pcm)
    return out.getvalue()


def _short(error: Any, limit: int = 160) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _model_missing(error: Exception) -> bool:
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    return status == 404 or (status == 400 and "model" in text)


def voice_model(env: Mapping[str, str]) -> str:
    chosen = env.get(MODEL_VARIABLE)
    return chosen.strip() if isinstance(chosen, str) and chosen.strip() else VOICE_MODEL


def transcribe(wav: bytes, key: str, *, model: str = VOICE_MODEL, client: Any = None) -> str:
    """The words in ``wav``, written out by OpenAI. ``client`` stands in for ``openai.OpenAI`` in tests."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=TRANSCRIBE_TIMEOUT_SEC, max_retries=1)
    models = [model] + ([FALLBACK_MODEL] if model != FALLBACK_MODEL else [])
    for index, name in enumerate(models):
        try:
            result = client.audio.transcriptions.create(model=name, file=("voice.wav", wav, "audio/wav"))
        except Exception as error:
            if index + 1 < len(models) and _model_missing(error):
                continue
            raise VoiceError(f"The recording could not be written out ({_short(error)}).") from error
        text = result if isinstance(result, str) else getattr(result, "text", "")
        return " ".join(str(text or "").split())
    return ""
