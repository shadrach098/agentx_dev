"""agentx falls back in the chat: when Claude or GPT fails for good, the conversation carries on on the user's local
fallback model, marked; the /fallback command; the one-time offer when a local server is found."""

import io
import json
import time

import httpx
import openai
import pytest
from rich.console import Console

from agentx_dev.cli import app as app_module
from agentx_dev.cli.app import ChatApp
from agentx_dev.cli.models import ModelError, key_variable, provider_for
from agentx_dev.LocalModel import Local, LocalCheck
from tests.conftest import MockModel, _MockToolCallingModel, make_final, make_react_response


@pytest.fixture(autouse=True)
def folder_is_trusted(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: True)


class Lines:
    def __init__(self, *items):
        self.items = list(items)

    def __call__(self, prompt=""):
        if not self.items:
            raise EOFError
        return self.items.pop(0)


class OutOfCredit(Exception):
    status_code = 400

    def __init__(self):
        super().__init__("Error code: 400 - Your credit balance is too low to access the Anthropic API.")


class KeyRejected(Exception):
    status_code = 401

    def __init__(self):
        super().__init__("Error code: 401 - Incorrect API key provided")


def paid(name, *script):
    """A Claude or GPT stand-in (text mode): each call takes the next item; an exception is raised."""
    steps = list(script)

    def run(messages):
        item = steps.pop(0) if steps else make_final("(nothing scripted)")
        if isinstance(item, BaseException):
            raise item
        return item

    model = MockModel(script=run)
    model.model_name = name
    return model


class LocalTools(_MockToolCallingModel):
    """A local model stand-in: bound natively, with a check that passes. Exceptions in the script are raised."""

    is_local = True
    prefers_native_tools = True

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, name, *tool_script, base_url="http://localhost:11434/v1"):
        super().__init__(tool_script=list(tool_script))
        self.model = name
        self.base_url = base_url
        self.context_window = 32768
        self.checked = 0

    def check(self, timeout=120.0):
        self.checked += 1
        return LocalCheck(True, "", self.context_window)

    def call_with_tools(self, messages, tools, *, force_tool=None):
        result = super().call_with_tools(messages, tools, force_tool=force_tool)
        if isinstance(result, BaseException):
            raise result
        return result


def respond(answer, call_id="r1"):
    return {"type": "tool_use", "name": "respond", "input": {"answer": answer}, "id": call_id}


def write_call(path="hello.txt", content="hi\n"):
    return make_react_response("write_file", {"path": path, "content": content, "if_exists": "refuse"})


def factory_of(models, asked):
    """Builds the named model; a Claude or GPT name without its key fails as choose_model does."""
    def factory(name, env):
        asked.append(name)
        provider = provider_for(name or "")
        if provider in ("claude", "gpt") and not env.get(key_variable(provider)):
            raise ModelError(f"No API key for {provider}: set the {key_variable(provider)} environment variable.")
        if name not in models:
            raise ModelError(f"no model named {name}")
        return models[name]
    return factory


def make_app(tmp_path, lines, models, asked=None, **kw):
    out = io.StringIO()
    console = Console(file=out, width=400, force_terminal=False, color_system=None)
    cwd, home = tmp_path / "proj", tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    kw.setdefault("env", {"ANTHROPIC_API_KEY": "k-anthropic", "OPENAI_API_KEY": "k-openai"})
    app = ChatApp(cwd=cwd, home=home, console=console, read_line=lines,
                  model_factory=factory_of(models, asked if asked is not None else []), **kw)
    return app, out, cwd, home


NOTICE = ("Claude failed: out of credit. This conversation continues on qwen3:8b (local). Choose a model to try "
          "Claude again.")


def started_on_claude(tmp_path, lines, primary, fallback, **kw):
    # A window the agentx prompt fits in (a fallback built from the settings gets 8,192 until its server says more).
    kw.setdefault("local_context", 32768)
    models = {"claude-opus-5-5": primary, "qwen3:8b": fallback}
    return make_app(tmp_path, lines, models, model_name="claude-opus-5-5", fallback_model="qwen3:8b", **kw)


# -- the turn carries on ---------------------------------------------------------------------------------------

def test_turn_continues_on_fallback_without_repeating_tool(tmp_path):
    primary = paid("claude-opus-5-5", write_call(), OutOfCredit())
    fallback = LocalTools("qwen3:8b", respond("Wrote hello.txt; done."))
    approvals = Lines("y")
    app, out, cwd, _ = started_on_claude(tmp_path, Lines("make hello.txt", "/exit"), primary, fallback,
                                         read_short=approvals)
    assert app.start() == 0
    text = out.getvalue()
    assert (cwd / "hello.txt").read_text(encoding="utf-8") == "hi\n"
    assert approvals.items == []                            # asked once: the approved write did not run again
    assert len(primary.calls) == 2 and len(fallback.tool_calls_made) == 1
    sent = " ".join(str(m.get("content")) for m in fallback.tool_calls_made[0])
    assert "The earlier model (claude-opus-5-5) stopped: out of credit." in sent
    assert "Wrote hello.txt; done." in text
    assert "Something went wrong" not in text
    assert app.model is fallback
    assert app.fallback_from == {"model": "claude-opus-5-5", "reason": "out of credit"}
    assert fallback.checked == 0                            # checked lazily: the runner calls it only when needed


def test_usage_counts_both_models_on_the_switching_turn(tmp_path):
    primary = paid("claude-opus-5-5", write_call(), OutOfCredit())
    fallback = LocalTools("qwen3:8b", respond("done"), respond("again"))
    app, _, _, _ = started_on_claude(tmp_path, Lines("make hello.txt", "and again", "/exit"), primary, fallback,
                                     read_short=Lines("y"))
    assert app.start() == 0
    primary_calls, fallback_calls = primary.usage.total_calls, fallback.usage.total_calls
    assert primary_calls == 2 and fallback_calls == 2
    assert app._usage_totals()[0] == primary_calls + fallback_calls
    assert app.session.usage["calls"] == primary_calls + fallback_calls
    assert app.session.usage["input_tokens"] == 10 * (primary_calls + fallback_calls)


def test_notice_text(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = LocalTools("qwen3:8b", respond("fine"))
    app, out, _, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback)
    assert app.start() == 0
    assert out.getvalue().count(NOTICE) == 1

    (tmp_path / "gpt").mkdir()
    gpt = paid("gpt-6.1-sol", KeyRejected())
    fallback = LocalTools("qwen3:8b", respond("fine"))
    app, out, _, _ = make_app(tmp_path / "gpt", Lines("hi", "/exit"), {"gpt-6.1-sol": gpt, "qwen3:8b": fallback},
                              model_name="gpt-6.1-sol", fallback_model="qwen3:8b")
    assert app.start() == 0
    assert ("GPT failed: key rejected. This conversation continues on qwen3:8b (local). Choose a model to try GPT "
            "again.") in out.getvalue()


def test_sticky_until_model_chosen(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit(), make_final("back on Claude"))
    fallback = LocalTools("qwen3:8b", respond("first on the fallback"), respond("second on the fallback"))
    app, out, _, _ = started_on_claude(tmp_path, Lines("one", "two", "/model claude-opus-5-5", "three", "/exit"),
                                       primary, fallback)
    seen = []
    send = app.send

    def spy(text, *args, **kwargs):
        send(text, *args, **kwargs)
        seen.append((app.model, app.fallback_from, app.session.runner, len(primary.calls)))

    app.send = spy
    assert app.start() == 0
    (model_one, from_one, _, _), (model_two, from_two, runner_two, calls_two), (model_three, from_three, _, _) = seen
    assert model_one is fallback and model_two is fallback and from_two == from_one is not None
    assert runner_two.model is fallback and runner_two.fallback_model is None    # rebuilt on the fallback
    assert calls_two == 1                                  # the dead provider was not tried on the second turn
    assert model_three is primary and from_three is None
    text = out.getvalue()
    assert "second on the fallback" in text and "back on Claude" in text
    assert text.count(NOTICE) == 1


def test_local_chat_never_falls_back(tmp_path):
    def refused(messages):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"))

    chat = MockModel(script=refused)
    chat.is_local = True
    chat.model = "qwen3:8b"
    chat.base_url = "http://localhost:11434/v1"
    chat.context_window = 32768
    chat.check = lambda timeout=120.0: LocalCheck(True, "", 32768)
    other = LocalTools("llama3.1:8b", respond("never"))
    claude = paid("claude-opus-5-5", make_final("never"))
    asked = []
    app, out, _, _ = make_app(tmp_path, Lines("hello", "/exit"),
                              {"qwen3:8b": chat, "llama3.1:8b": other, "claude-opus-5-5": claude}, asked,
                              model_name="qwen3:8b", fallback_model="llama3.1:8b")
    assert app.start() == 0
    text = out.getvalue()
    assert "the local server at localhost:11434 is not answering" in text
    assert app.session.runner.fallback_model is None and app.fallback_from is None
    assert other.tool_calls_made == [] and claude.calls == []
    assert not [name for name in asked if provider_for(name or "") in ("claude", "gpt")]
    assert "credit" not in text and "failed: " not in text


def test_a_failing_fallback_says_both_reasons(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = LocalTools("qwen3:8b", openai.APIConnectionError(
        request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions")))
    app, out, _, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback)
    assert app.start() == 0
    text = out.getvalue()
    assert ("claude-opus-5-5 failed (out of credit); the fallback qwen3:8b failed too: the local server at "
            "localhost:11434 is not answering") in text
    assert "What it did so far is kept in the conversation" in text


# -- startup without a key ------------------------------------------------------------------------------------------

def _with_user_settings(tmp_path, data):
    settings = tmp_path / "home" / ".agentx" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(data), encoding="utf-8")
    return settings


def test_startup_no_key_uses_fallback_after_offer(tmp_path):
    settings = _with_user_settings(tmp_path, {"model": "gpt-6.1-sol", "fallbackModel": "qwen3:8b"})
    fallback = LocalTools("qwen3:8b", respond("hello from the fallback"))
    offered = []
    app, out, _, _ = make_app(tmp_path, Lines("hi", "/exit"), {"qwen3:8b": fallback}, model_name="gpt-6.1-sol",
                              model_source=settings, env={"ANTHROPIC_API_KEY": "k-anthropic"},
                              fallback_model="qwen3:8b", read_short=Lines(),
                              select=lambda options, default: offered.append(options) or len(options) - 1)
    assert app.start() == 0
    text = out.getvalue()
    assert offered and "claude-opus-5-5" in offered[0][0]                  # today's offer came first
    assert text.index("ANTHROPIC_API_KEY is set") < text.index("No key for OpenAI: starting on qwen3:8b (local).")
    assert app.model is fallback and fallback.checked == 1                  # the chat model is checked as usual
    assert app.fallback_from == {"model": "gpt-6.1-sol", "reason": "no key"}
    assert "hello from the fallback" in text


def test_startup_no_key_and_no_other_key_starts_on_fallback(tmp_path):
    fallback = LocalTools("qwen3:8b", respond("hi"))
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), {"qwen3:8b": fallback}, model_name="claude-opus-5-5",
                              env={}, fallback_model="qwen3:8b", read_short=Lines())
    assert app.start() == 0
    assert "No key for Anthropic: starting on qwen3:8b (local)." in out.getvalue()


def test_startup_without_a_fallback_still_stops(tmp_path):
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), {}, model_name="claude-opus-5-5", env={},
                              fallback_model="", read_short=Lines())
    assert app.start() == 1
    assert "starting on" not in out.getvalue()


# -- /fallback ------------------------------------------------------------------------------------------------------

def test_fallback_command_rejects_paid_name(tmp_path):
    primary = paid("claude-opus-5-5")
    asked = []
    app, out, _, home = make_app(tmp_path, Lines("/fallback claude-sonnet-5-5", "/exit"),
                                 {"claude-opus-5-5": primary}, asked, model_name="claude-opus-5-5")
    assert app.start() == 0
    assert "The fallback must be a local model; claude-sonnet-5-5 is not." in out.getvalue()
    assert "claude-sonnet-5-5" not in asked
    settings = home / ".agentx" / "settings.json"
    assert "fallbackModel" not in (json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {})


def test_fallback_command_checks_and_saves(tmp_path):
    primary = paid("claude-opus-5-5")
    fallback = LocalTools("qwen3:8b")
    app, out, _, home = make_app(tmp_path, Lines("/fallback", "/fallback qwen3:8b", "/fallback", "/exit"),
                                 {"claude-opus-5-5": primary, "qwen3:8b": fallback}, model_name="claude-opus-5-5")
    assert app.start() == 0
    text = out.getvalue()
    assert "No fallback is set" in text
    assert fallback.checked == 1
    data = json.loads((home / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["fallbackModel"] == "qwen3:8b"
    assert app.fallback_name == "qwen3:8b"
    assert app.session.runner.fallback_model is fallback      # this conversation falls back from now on
    assert "Fallback: qwen3:8b (local)." in text


def test_fallback_command_says_why_a_check_failed(tmp_path):
    primary = paid("claude-opus-5-5")
    broken = LocalTools("qwen3:8b")
    broken.check = lambda timeout=120.0: LocalCheck(False, "the local server at localhost:11434 is not answering",
                                                    None)
    app, out, _, home = make_app(tmp_path, Lines("/fallback qwen3:8b", "/exit"),
                                 {"claude-opus-5-5": primary, "qwen3:8b": broken}, model_name="claude-opus-5-5")
    assert app.start() == 0
    assert "Could not use qwen3:8b: the local server at localhost:11434 is not answering" in out.getvalue()
    settings = home / ".agentx" / "settings.json"
    assert "fallbackModel" not in json.loads(settings.read_text(encoding="utf-8"))
    assert app.fallback_name is None


def test_fallback_off_saves_empty(tmp_path):
    primary = paid("claude-opus-5-5")
    fallback = LocalTools("qwen3:8b")
    app, out, _, home = make_app(tmp_path, Lines("/fallback off", "/fallback", "/exit"),
                                 {"claude-opus-5-5": primary, "qwen3:8b": fallback}, model_name="claude-opus-5-5",
                                 fallback_model="qwen3:8b")
    assert app.start() == 0
    data = json.loads((home / ".agentx" / "settings.json").read_text(encoding="utf-8"))
    assert data["fallbackModel"] == ""
    assert app.fallback_name == "" and app.session.runner.fallback_model is None
    assert "The fallback is off" in out.getvalue()


def test_fallback_command_shows_a_failed_save(tmp_path, monkeypatch):
    from agentx_dev.cli import app_models
    monkeypatch.setattr(app_models, "write_user_setting", lambda home, key, value: (None, "Could not write it."))
    primary = paid("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("/fallback off", "/exit"), {"claude-opus-5-5": primary},
                              model_name="claude-opus-5-5", fallback_model="qwen3:8b")
    assert app.start() == 0
    assert "Could not write it." in out.getvalue()


def test_fallback_is_listed_with_its_summary():
    from agentx_dev.cli.commands import command_descriptions, runs_while_working
    assert command_descriptions()["/fallback"] == ("Show or set the local model used when Claude or GPT fails: "
                                                   "/fallback [name|off]")
    assert runs_while_working("/fallback") and not runs_while_working("/fallback off")


# -- the one-time offer ---------------------------------------------------------------------------------------------

OFFER = ("Ollama is running with qwen3:8b, llama3.1:8b. Use one as the fallback when Claude or GPT fails? "
         "/fallback qwen3:8b sets it; /fallback off stops this question.")
ACTIONS = [{"label": "Use qwen3:8b", "send": "/fallback qwen3:8b"}, {"label": "No", "send": "/fallback off"}]


def _listing(monkeypatch, names=("qwen3:8b", "llama3.1:8b")):
    monkeypatch.setattr(Local, "models", staticmethod(lambda base_url="", timeout=5.0: list(names)))


def _record_notices(app):
    shown = []
    notice = app.screen.notice

    def record(text, actions=None):
        shown.append((text, actions))
        notice(text, actions=actions)

    app.screen.notice = record
    return shown


def test_offer_shown_once(tmp_path, monkeypatch):
    _listing(monkeypatch)
    primary = paid("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), {"claude-opus-5-5": primary}, model_name="claude-opus-5-5",
                              local_server="http://localhost:11434/v1")
    shown = _record_notices(app)
    app._local_discovery.join(5)
    assert app.start() == 0
    app._discover_local_models("http://localhost:11434/v1")         # found again: not asked again
    offers = [(text, actions) for text, actions in shown if "Use one as the fallback" in text]
    assert offers == [(OFFER, ACTIONS)]
    assert out.getvalue().count(OFFER) == 1
    assert out.getvalue().index("Welcome") < out.getvalue().index(OFFER)


def test_offer_after_startup_is_shown_when_discovery_ends(tmp_path, monkeypatch):
    primary = paid("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), {"claude-opus-5-5": primary}, model_name="claude-opus-5-5")
    shown = _record_notices(app)
    assert app.start() == 0
    _listing(monkeypatch, ("qwen3:8b",))
    app._discover_local_models("http://127.0.0.1:1234/v1")         # a slow server answered after the welcome
    assert [text for text, _ in shown if "Use one as the fallback" in text] == [
        "LM Studio is running with qwen3:8b. Use one as the fallback when Claude or GPT fails? /fallback qwen3:8b "
        "sets it; /fallback off stops this question."]


def test_no_offer_once_a_fallback_is_set_or_off(tmp_path, monkeypatch):
    _listing(monkeypatch)
    for fallback in ("qwen3:8b", ""):
        folder = tmp_path / ("set" if fallback else "off")
        folder.mkdir()
        primary = paid("claude-opus-5-5")
        app, out, _, _ = make_app(folder, Lines("/exit"), {"claude-opus-5-5": primary},
                                  model_name="claude-opus-5-5", fallback_model=fallback,
                                  local_server="http://localhost:11434/v1")
        app._local_discovery.join(5)
        assert app.start() == 0
        assert "Use one as the fallback" not in out.getvalue()


def test_no_offer_when_the_server_lists_nothing(tmp_path, monkeypatch):
    _listing(monkeypatch, ())
    primary = paid("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), {"claude-opus-5-5": primary}, model_name="claude-opus-5-5",
                              local_server="http://localhost:11434/v1")
    app._local_discovery.join(5)
    assert app.start() == 0
    assert "Use one as the fallback" not in out.getvalue()


# -- marking --------------------------------------------------------------------------------------------------------

MARK = "(on qwen3:8b, fallback from claude-opus-5-5: out of credit)"


def test_log_entry_marks_fallback(tmp_path):
    primary = paid("claude-opus-5-5", write_call(), OutOfCredit())
    fallback = LocalTools("qwen3:8b", respond("done"), respond("again"))
    app, _, cwd, _ = started_on_claude(tmp_path, Lines("make hello.txt", "and again", "/exit"), primary, fallback,
                                       read_short=Lines("y"))
    assert app.start() == 0
    log = (cwd / ".agentx" / "log.md").read_text(encoding="utf-8")
    headings = [line for line in log.splitlines() if line.startswith("## ")]
    assert len(headings) == 2 and all(heading.endswith(MARK) for heading in headings)
    saved = json.loads(app.session_path.read_text(encoding="utf-8"))
    assert [meta.get("ran_on") for meta in saved["completions_meta"]] == [MARK, MARK]


def test_a_turn_on_claude_is_not_marked(tmp_path):
    primary = paid("claude-opus-5-5", make_final("fine"))
    fallback = LocalTools("qwen3:8b")
    app, _, cwd, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback)
    assert app.start() == 0
    assert "fallback from" not in (cwd / ".agentx" / "log.md").read_text(encoding="utf-8")
    saved = json.loads(app.session_path.read_text(encoding="utf-8"))
    assert "ran_on" not in saved["completions_meta"][0]


def test_a_stopped_turn_on_the_fallback_is_marked(tmp_path):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = LocalTools("qwen3:8b")
    fallback.call_with_tools = interrupted
    app, _, cwd, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback)
    assert app.start() == 0
    log = (cwd / ".agentx" / "log.md").read_text(encoding="utf-8")
    assert MARK in log
    assert MARK in str(app.session.history[-1]["content"])


# -- fix round 1 ----------------------------------------------------------------------------------------------------

import threading
from types import SimpleNamespace


class WindowedLocal(LocalTools):
    """A local stand-in whose server says its window when asked (``server_window``), after ``release`` is set."""

    def __init__(self, name, *tool_script, window=16384, release=None):
        super().__init__(name, *tool_script)
        self.context_window = None                  # as the settings build it: the window is not known yet
        self.server_window_asked = []
        self._window = window
        self._release = release

    def server_window(self, timeout=5.0):
        self.server_window_asked.append(timeout)
        if self._release is not None:
            self._release.wait(10)
        return self._window


def test_fallback_reads_its_window_in_the_background(tmp_path):
    release = threading.Event()
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = WindowedLocal("qwen3:8b", respond("done"), window=16384, release=release)
    app, _, _, _ = started_on_claude(tmp_path, Lines("hi", "/clear", "/exit"), primary, fallback, local_context=None)
    seen = []
    send = app.send

    def spy(text, *args, **kwargs):
        release.set()                               # the server answers while the conversation goes on
        app._fallback_window_reader.join(5)
        send(text, *args, **kwargs)
        seen.append(app.session.runner._window())

    app.send = spy
    assert app.start() == 0
    assert fallback.server_window_asked == [2.0]    # asked once, however many runners were built
    assert fallback.context_window == 16384 and seen == [16384]


def test_local_context_wins_over_the_server_window(tmp_path):
    primary = paid("claude-opus-5-5")
    fallback = WindowedLocal("qwen3:8b", window=16384)
    app, _, _, _ = started_on_claude(tmp_path, Lines("/exit"), primary, fallback, local_context=4096)
    assert app.start() == 0
    assert fallback.context_window == 4096 and fallback.server_window_asked == []


def test_a_silent_server_leaves_8192_and_never_blocks_the_turn(tmp_path):
    never = threading.Event()
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = WindowedLocal("qwen3:8b", respond("done anyway"), window=16384, release=never)
    app, out, _, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback, local_context=None)
    began = time.monotonic()
    try:
        assert app.start() == 0
        assert time.monotonic() - began < 5
        assert "done anyway" in out.getvalue()
        assert fallback.context_window == 8192 and app.session.runner._window() == 8192
    finally:
        never.set()


def test_a_new_window_conversation_tries_the_chosen_model(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = LocalTools("qwen3:8b", respond("done"))
    app, _, _, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback)
    assert app.start() == 0 and app.fallback_from is not None
    room = SimpleNamespace(console=Console(file=io.StringIO()), read=Lines(), read_short=Lines(), select=None)
    other = app_module.device_app(app, room)
    assert other.requested_model == "claude-opus-5-5" and other.fallback_name == "qwen3:8b"

    (tmp_path / "plain").mkdir()
    app, _, _, _ = started_on_claude(tmp_path / "plain", Lines("/exit"), paid("claude-opus-5-5"),
                                     LocalTools("qwen3:8b"))
    assert app.start() == 0
    assert app_module.device_app(app, room).requested_model == "claude-opus-5-5"


def test_the_offer_from_the_thread_goes_above_an_open_prompt(tmp_path, monkeypatch):
    import prompt_toolkit.application as pt_application
    ran = []
    monkeypatch.setattr(pt_application, "run_in_terminal", lambda fn: (ran.append(fn), fn())[1])
    primary = paid("claude-opus-5-5")
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), {"claude-opus-5-5": primary}, model_name="claude-opus-5-5")
    assert app.start() == 0
    scheduled = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn: (scheduled.append(fn), fn()))
    app.prompt_input = SimpleNamespace(_chat=SimpleNamespace(app=SimpleNamespace(loop=loop, is_running=True)))
    _listing(monkeypatch, ("qwen3:8b",))
    worker = threading.Thread(target=app._discover_local_models, args=("http://localhost:11434/v1",))
    worker.start()
    worker.join(5)
    assert scheduled and ran
    assert "Ollama is running with qwen3:8b." in out.getvalue()


def test_a_stopped_switching_turn_keeps_the_fallbacks_usage(tmp_path):
    primary = paid("claude-opus-5-5", OutOfCredit())
    fallback = LocalTools("qwen3:8b", openai.APIConnectionError(
        request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions")))
    app, _, _, _ = started_on_claude(tmp_path, Lines("hi", "/exit"), primary, fallback)
    assert app.start() == 0
    assert fallback.usage.total_calls == 1
    saved = json.loads(app.session_path.read_text(encoding="utf-8"))
    assert saved["usage"]["calls"] == primary.usage.total_calls + fallback.usage.total_calls


def test_a_fallback_that_cannot_be_built_is_said_once(tmp_path):
    primary = paid("claude-opus-5-5", make_final("one"), make_final("two"))
    asked = []
    app, out, _, _ = make_app(tmp_path, Lines("hi", "/clear", "again", "/exit"), {"claude-opus-5-5": primary}, asked,
                              model_name="claude-opus-5-5", fallback_model="qwen3:8b")
    assert app.start() == 0
    assert asked.count("qwen3:8b") == 1
    assert out.getvalue().count("The fallback qwen3:8b could not be set up") == 1


def test_the_offer_sends_the_whole_name(tmp_path, monkeypatch):
    long_name = "q" * 90
    _listing(monkeypatch, (long_name,))
    primary = paid("claude-opus-5-5")
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), {"claude-opus-5-5": primary}, model_name="claude-opus-5-5",
                            local_server="http://localhost:11434/v1")
    shown = _record_notices(app)
    app._local_discovery.join(5)
    assert app.start() == 0
    (_, actions), = [(text, actions) for text, actions in shown if actions]
    assert actions[0]["send"] == f"/fallback {long_name}"
    assert actions[0]["label"].startswith("Use q") and len(actions[0]["label"]) <= 84
