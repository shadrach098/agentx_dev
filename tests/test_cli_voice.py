"""/voice: record until Enter, write the words out with OpenAI, and start the next input line with them.

Claude models take no audio, so the words always come from OpenAI's speech-to-text. No microphone or network here:
the stream and the OpenAI client are stand-ins."""

import io
import wave

from agentx_dev.cli.app_voice import CHECK_IT, RECORDING
from agentx_dev.cli.commands import command_names
from agentx_dev.cli.voice import (FALLBACK_MODEL, NO_KEY, NO_PACKAGE, SAMPLE_RATE, VOICE_MODEL, Recorder,
                                  VoiceError, to_wav, transcribe, voice_model)
from tests.conftest import MockModel, make_final
from tests.test_cli_app import make_app


class FakeStream:
    """Feeds ``seconds`` of silence to the callback when started."""

    def __init__(self, seconds=1.0, fail=None, **options):
        self.options, self.seconds, self.fail = options, seconds, fail
        self.stopped = self.closed = False

    def start(self):
        if self.fail:
            raise self.fail
        frames = int(self.seconds * SAMPLE_RATE)
        self.options["callback"](b"\x00\x00" * frames, frames, None, None)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeRecorder:
    def __init__(self, seconds=1.0, fail=None):
        self.seconds, self.fail, self.cancelled = seconds, fail, False
        self.full = False

    def start(self):
        if self.fail:
            raise VoiceError(self.fail)

    def cancel(self):
        self.cancelled = True

    def stop(self):
        return to_wav(b"\x00\x00" * int(self.seconds * SAMPLE_RATE)), self.seconds


class PrefillLines:
    """A read_line that, like the real input line, takes ``default=`` and sends it as typed."""

    def __init__(self, *items):
        self.items, self.defaults = list(items), []

    def __call__(self, prompt="", default=""):
        self.defaults.append(default)
        if not self.items:
            raise EOFError
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return default + item if item is not None else default


def voice_app(tmp_path, lines, *, recorder=None, words="fix the login page", env=None, model=None):
    model = model or MockModel(script=[make_final("on it")])
    app, out, cwd, home = make_app(tmp_path, lines, model,
                                   env={"OPENAI_API_KEY": "sk-test"} if env is None else env)
    app._recorder_factory = lambda: recorder or FakeRecorder()
    calls = []

    def fake_transcribe(wav, key, model=VOICE_MODEL):
        calls.append((wav, key, model))
        if isinstance(words, BaseException):
            raise words
        return words

    app._transcriber = fake_transcribe
    return app, out, model, calls


class TestTheCommand:
    def test_listed(self):
        assert "/voice" in command_names()

    def test_the_words_start_the_next_input_line_and_are_sent_after_enter(self, tmp_path):
        # "/voice", Enter to stop recording (read_short), then the input line: Enter as it is, then /exit
        lines = PrefillLines("/voice", "", None, "/exit")
        app, out, model, calls = voice_app(tmp_path, lines)
        app.read_short = lambda prompt="": lines("")
        app.start()
        assert "fix the login page" in lines.defaults
        assert calls and calls[0][1] == "sk-test" and calls[0][2] == VOICE_MODEL
        assert "fix the login page" in str(model.calls[0])          # what the user sent after checking it
        assert CHECK_IT in out.getvalue()

    def test_the_user_can_change_the_words_before_sending(self, tmp_path):
        lines = PrefillLines("/voice", "", " please", "/exit")
        app, out, model, _ = voice_app(tmp_path, lines)
        app.read_short = lambda prompt="": lines("")
        app.start()
        assert "fix the login page please" in str(model.calls[0])

    def test_the_recording_prompt(self, tmp_path):
        prompts = []
        lines = PrefillLines("/voice", "/exit")
        app, out, _, _ = voice_app(tmp_path, lines)
        app.read_short = lambda prompt="": (prompts.append(prompt), "")[1]
        app.start()
        assert prompts == [RECORDING]

    def test_nothing_is_sent_by_itself(self, tmp_path):
        lines = PrefillLines("/voice", "", EOFError())
        app, out, model, _ = voice_app(tmp_path, lines)
        app.read_short = lambda prompt="": lines("")
        app.start()
        assert model.calls == []

    def test_ctrl_c_cancels(self, tmp_path):
        recorder = FakeRecorder()
        lines = PrefillLines("/voice", "/exit")
        app, out, _, calls = voice_app(tmp_path, lines, recorder=recorder)

        def interrupt(prompt=""):
            raise KeyboardInterrupt
        app.read_short = interrupt
        app.start()
        assert recorder.cancelled and calls == []
        assert "Recording cancelled" in out.getvalue()

    def test_no_openai_key(self, tmp_path):
        lines = PrefillLines("/voice", "/exit")
        app, out, _, calls = voice_app(tmp_path, lines, env={"ANTHROPIC_API_KEY": "x"})
        app._saved_key_reader = lambda: {}
        app.start()
        assert calls == [] and "OPENAI_API_KEY" in out.getvalue()

    def test_no_microphone(self, tmp_path):
        lines = PrefillLines("/voice", "/exit")
        app, out, _, calls = voice_app(tmp_path, lines, recorder=FakeRecorder(fail="No microphone could be opened"))
        app.start()
        assert calls == [] and "No microphone" in out.getvalue()

    def test_too_short(self, tmp_path):
        lines = PrefillLines("/voice", "", "/exit")
        app, out, _, calls = voice_app(tmp_path, lines, recorder=FakeRecorder(seconds=0.1))
        app.read_short = lambda prompt="": ""
        app.start()
        assert calls == [] and "Nothing was recorded" in out.getvalue()

    def test_no_words_heard(self, tmp_path):
        lines = PrefillLines("/voice", "/exit")
        app, out, _, _ = voice_app(tmp_path, lines, words="  ")
        app.read_short = lambda prompt="": ""
        app.start()
        assert "No words were heard" in out.getvalue() and "" in lines.defaults

    def test_transcription_fails(self, tmp_path):
        lines = PrefillLines("/voice", "/exit")
        app, out, _, _ = voice_app(tmp_path, lines, words=VoiceError("The recording could not be written out (x)."))
        app.read_short = lambda prompt="": ""
        app.start()
        assert "could not be written out" in out.getvalue()

    def test_a_line_that_cannot_be_filled_shows_the_words(self, tmp_path):
        items = ["/voice", "/exit"]
        app, out, _, _ = voice_app(tmp_path, lambda prompt="": items.pop(0) if items else (_ for _ in ()).throw(EOFError()))
        app.read_short = lambda prompt="": ""
        app.start()
        assert "You said: fix the login page" in out.getvalue()

    def test_something_after_voice_is_an_error(self, tmp_path):
        lines = PrefillLines("/voice now", "/exit")
        app, out, _, calls = voice_app(tmp_path, lines)
        app.start()
        assert calls == [] and "/voice takes nothing" in out.getvalue()


class TestRecorder:
    def test_records_16khz_mono_wav(self):
        streams = []
        recorder = Recorder(stream_factory=lambda **o: streams.append(FakeStream(seconds=0.5, **o)) or streams[-1])
        recorder.start()
        wav, seconds = recorder.stop()
        assert seconds == 0.5 and streams[0].stopped and streams[0].closed
        with wave.open(io.BytesIO(wav)) as file:
            assert (file.getnchannels(), file.getsampwidth(), file.getframerate()) == (1, 2, SAMPLE_RATE)
            assert file.getnframes() == SAMPLE_RATE // 2
        assert streams[0].options["samplerate"] == SAMPLE_RATE and streams[0].options["dtype"] == "int16"

    def test_a_microphone_that_cannot_do_16khz_records_at_its_own_rate(self):
        def factory(**options):
            if options["samplerate"] == SAMPLE_RATE:
                raise OSError("Invalid sample rate")
            return FakeStream(seconds=0.5 * SAMPLE_RATE / 44100, **options)
        recorder = Recorder(stream_factory=factory, fallback_rate=44100)
        recorder.start()
        wav, _ = recorder.stop()
        with wave.open(io.BytesIO(wav)) as file:
            assert file.getframerate() == 44100

    def test_a_microphone_that_cannot_open(self):
        recorder = Recorder(stream_factory=lambda **o: FakeStream(fail=OSError("Error querying device -1"), **o))
        try:
            recorder.start()
        except VoiceError as error:
            assert "No microphone could be opened" in str(error) and "device -1" in str(error)
        else:
            raise AssertionError("no error")

    def test_without_the_package(self, monkeypatch):
        import builtins
        real = builtins.__import__

        def no_sounddevice(name, *args, **kwargs):
            if name == "sounddevice":
                raise ImportError("no module")
            return real(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", no_sounddevice)
        try:
            Recorder().start()
        except VoiceError as error:
            assert str(error) == NO_PACKAGE and "agentx-dev[voice]" in str(error)
        else:
            raise AssertionError("no error")

    def test_cancel_drops_the_recording(self):
        recorder = Recorder(stream_factory=lambda **o: FakeStream(seconds=1, **o))
        recorder.start()
        recorder.cancel()
        assert recorder.stop()[1] == 0


class Transcriptions:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeClient:
    def __init__(self, *answers):
        self.audio = type("Audio", (), {})()
        self.audio.transcriptions = Transcriptions(answers)


class Answer:
    def __init__(self, text):
        self.text = text


class MissingModel(Exception):
    status_code = 404


class TestTranscribe:
    def test_sends_the_wav_and_returns_the_words(self):
        client = FakeClient(Answer("  fix the\nlogin page  "))
        assert transcribe(b"WAV", "sk", client=client) == "fix the login page"
        call = client.audio.transcriptions.calls[0]
        assert call["model"] == VOICE_MODEL and call["file"] == ("voice.wav", b"WAV", "audio/wav")

    def test_falls_back_when_the_model_is_missing(self):
        client = FakeClient(MissingModel("model not found"), Answer("hello"))
        assert transcribe(b"WAV", "sk", client=client) == "hello"
        assert [c["model"] for c in client.audio.transcriptions.calls] == [VOICE_MODEL, FALLBACK_MODEL]

    def test_other_failures_say_so(self):
        client = FakeClient(RuntimeError("connection reset"))
        try:
            transcribe(b"WAV", "sk", client=client)
        except VoiceError as error:
            assert "could not be written out" in str(error) and "connection reset" in str(error)
        else:
            raise AssertionError("no error")

    def test_the_model_can_be_chosen(self):
        assert voice_model({}) == VOICE_MODEL
        assert voice_model({"AGENTX_VOICE_MODEL": " whisper-1 "}) == "whisper-1"


def test_no_key_message_says_claude_takes_no_audio():
    assert "OPENAI_API_KEY" in NO_KEY and "Claude" in NO_KEY
