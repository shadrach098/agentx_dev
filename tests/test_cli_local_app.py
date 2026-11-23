"""agentx and local models: the check on choosing one, discovery at startup, and fitting the window."""

import functools
import threading
import io
import json
import time

import pytest
from rich.console import Console

from agentx_dev import AgentRunner, ContextWindowTooSmall
from agentx_dev.cli import app as app_module
from agentx_dev.cli import app_usage
from agentx_dev.cli.app import ChatApp, build_parser
from agentx_dev.cli.compact import Compaction
from agentx_dev.cli.context import MAX_CONTEXT_CHARS, build_addendum
from agentx_dev.cli.models import LOCAL_TYPO_HINT, choose_model
from agentx_dev.LocalModel import Local, LocalCheck
from tests.conftest import MockModel, make_final

SERVER = "http://127.0.0.1:9/v1"          # never contacted: the check and the model list are stubbed


@pytest.fixture(autouse=True)
def folder_is_trusted(monkeypatch):
    monkeypatch.setattr(app_module, "ensure_trusted", lambda *args, **kwargs: True)


@pytest.fixture(autouse=True)
def keep_logging_as_it_was():
    import logging
    loggers = (logging.getLogger(), logging.getLogger("agentx_dev"))
    before = [list(logger.handlers) for logger in loggers]
    yield
    for logger, handlers in zip(loggers, before):
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
                if getattr(handler, "_agentx_cli", False):
                    handler.close()


class Lines:
    def __init__(self, *items):
        self.items = list(items)

    def __call__(self, prompt=""):
        if not self.items:
            raise EOFError
        return self.items.pop(0)


def make_app(tmp_path, lines, factory, **kw):
    out = io.StringIO()
    console = Console(file=out, width=400, force_terminal=False, color_system=None)
    cwd, home = tmp_path / "proj", tmp_path / "home"
    cwd.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    kw.setdefault("env", {})
    app = ChatApp(cwd=cwd, home=home, console=console, read_line=lines, model_factory=factory, **kw)
    return app, out, cwd, home


def cloud_then_local(cloud):
    """``first`` is a cloud model (a mock); any other name goes to the real factory, on a stubbed server."""
    local = functools.partial(choose_model, local_server=SERVER, local_context=None)
    return lambda name, env: cloud if name == "first" else local(name, env)


def stub_check(monkeypatch, ok=True, reason="", window=16384):
    checked = []

    def check(self, timeout=120.0):
        checked.append(self.model)
        if ok:
            self.context_window = window
        return LocalCheck(ok, reason, self.context_window)

    monkeypatch.setattr(Local, "check", check)
    return checked


def progress_labels(app):
    labels = []
    start = app.progress.start
    app.progress.start = lambda label="Thinking", *a, **k: (labels.append(label), start(label, *a, **k))[1]
    return labels


def local_mock(name="qwen3:8b", window=8192, script=None):
    """A scripted model that agentx treats as a local one, with a check that passes."""
    model = MockModel(script=script or [make_final("ok")])
    model.is_local = True
    model.model = name
    model.context_window = window
    model.check = lambda timeout=120.0: LocalCheck(True, "", model.context_window)
    return model


# -- choosing a local model --------------------------------------------------------------------------------------

def test_switch_to_local_runs_check_and_switches(tmp_path, monkeypatch):
    checked = stub_check(monkeypatch, window=16384)
    cloud = MockModel(script=[make_final("hi")])
    app, out, _, _ = make_app(tmp_path, Lines("/model qwen3:8b", "/exit"), cloud_then_local(cloud),
                              model_name="first")
    labels = progress_labels(app)
    assert app.start() == 0
    assert checked == ["qwen3:8b"]
    assert "Loading qwen3:8b on the local server" in labels
    assert isinstance(app.model, Local) and app.model.model == "qwen3:8b"
    assert app.session.runner.model is app.model and app.session.runner._window() == 16384
    assert "Model: qwen3:8b" in out.getvalue()


SMALL_WINDOW = ("qwen3:8b's 8,192-token window leaves almost no room for the conversation; a variant with a larger "
                "window fixes it: a Modelfile with FROM qwen3:8b and PARAMETER num_ctx 16384, then ollama create "
                "qwen3:8b-16k -f Modelfile.")


def test_a_local_model_whose_window_leaves_no_room_is_warned_about_once(tmp_path, monkeypatch):
    stub_check(monkeypatch, window=8192)
    cloud = MockModel(script=[make_final("hi")])
    app, out, _, _ = make_app(tmp_path, Lines("/model qwen3:8b", "/exit"), cloud_then_local(cloud),
                              model_name="first")
    assert app.start() == 0
    text = out.getvalue()
    assert text.count(SMALL_WINDOW) == 1
    assert isinstance(app.model, Local) and app.model.model == "qwen3:8b"       # still allowed


def test_a_window_with_room_left_is_not_warned_about(tmp_path, monkeypatch):
    stub_check(monkeypatch, window=16384)
    app, out, _, _ = make_app(tmp_path, Lines("/model qwen3:8b", "/exit"), cloud_then_local(MockModel(script=[])),
                              model_name="first")
    assert app.start() == 0
    assert "leaves almost no room" not in out.getvalue()


def test_the_estimate_counts_the_prompt_and_the_tool_specs(tmp_path, monkeypatch):
    stub_check(monkeypatch, window=8192)
    app, _, _, _ = make_app(tmp_path, Lines(), cloud_then_local(MockModel(script=[])), model_name="first")
    model = local_mock(window=8192)
    model.prefers_native_tools = True
    (app.cwd / "AGENTX.md").write_text("rule " * 6000, encoding="utf-8")     # not part of the fixed share
    fixed = app._fixed_chars(model)
    assert 15_000 < fixed < 45_000                         # agentx's own prompt and its tools, before any message


def test_failed_check_keeps_model_and_says_why(tmp_path, monkeypatch):
    stub_check(monkeypatch, ok=False, reason="the local server at 127.0.0.1:9 is not answering")
    cloud = MockModel(script=[make_final("still the cloud one")])
    app, out, _, _ = make_app(tmp_path, Lines("/model qwen3:8b", "hello", "/exit"), cloud_then_local(cloud),
                              model_name="first")
    assert app.start() == 0
    text = out.getvalue()
    assert app.model is cloud and "still the cloud one" in text
    assert "Could not use qwen3:8b: the local server at 127.0.0.1:9 is not answering" in text
    assert LOCAL_TYPO_HINT not in text


def test_typo_gets_hint(tmp_path, monkeypatch):
    reason = "the local server at 127.0.0.1:9 has no model named cluade-opus-5-5; it lists: qwen3:8b"
    stub_check(monkeypatch, ok=False, reason=reason)
    cloud = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/model cluade-opus-5-5", "/exit"), cloud_then_local(cloud),
                              model_name="first")
    assert app.start() == 0
    assert f"Could not use cluade-opus-5-5: {reason}. {LOCAL_TYPO_HINT}" in out.getvalue()
    assert app.model is cloud


def test_startup_check_failure_is_the_no_model_path(tmp_path, monkeypatch):
    reason = "the local server at 127.0.0.1:9 has no model named cluade-opus-5-5; it lists: qwen3:8b"
    stub_check(monkeypatch, ok=False, reason=reason)
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), cloud_then_local(None), model_name="cluade-opus-5-5")
    assert app.start() == 1
    assert f"Could not use cluade-opus-5-5: {reason}. {LOCAL_TYPO_HINT}" in out.getvalue()


def test_startup_on_a_local_model_runs_the_check(tmp_path, monkeypatch):
    checked = stub_check(monkeypatch, window=32768)
    app, out, _, _ = make_app(tmp_path, Lines("/exit"), cloud_then_local(None), model_name="qwen3:8b")
    labels = progress_labels(app)
    assert app.start() == 0
    assert checked == ["qwen3:8b"] and "Loading qwen3:8b on the local server" in labels
    assert app.session.runner._window() == 32768


def test_a_window_the_server_does_not_say_is_taken_as_8192(tmp_path, monkeypatch):
    stub_check(monkeypatch, window=None)
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), cloud_then_local(None), model_name="qwen3:8b")
    assert app.start() == 0
    assert app.session.runner._window() == 8192


# -- finding the local models --------------------------------------------------------------------------------------

def test_startup_does_not_wait_for_discovery(tmp_path, monkeypatch):
    asked = []

    def slow_models(base_url="", timeout=5.0):
        asked.append((base_url, timeout))
        time.sleep(2)
        return ["qwen3:8b", "llama3.1:8b"]

    monkeypatch.setattr(Local, "models", staticmethod(slow_models))
    began = time.monotonic()
    app, _, _, _ = make_app(tmp_path, Lines(), cloud_then_local(None), local_server=SERVER)
    assert time.monotonic() - began < 1
    assert app.local_models == []
    deadline = time.monotonic() + 10
    while not app.local_models and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.local_models == ["qwen3:8b", "llama3.1:8b"]
    assert asked == [(SERVER, 0.5)]
    assert app._local_discovery.daemon


def test_no_local_server_means_no_local_models(tmp_path, monkeypatch):
    from agentx_dev.LocalModel import LocalModelError

    def refused(base_url="", timeout=5.0):
        raise LocalModelError("the local server at 127.0.0.1:9 is not answering")

    monkeypatch.setattr(Local, "models", staticmethod(refused))
    app, out, _, _ = make_app(tmp_path, Lines(), cloud_then_local(None), local_server=SERVER)
    app._local_discovery.join(5)
    assert app.local_models == [] and out.getvalue() == ""


def test_model_lists_local_models(tmp_path):
    cloud = MockModel(script=[])
    app, out, _, _ = make_app(tmp_path, Lines("/model", "/exit"), cloud_then_local(cloud), model_name="first")
    app.local_models[:] = ["qwen3:8b", "llama3.1:8b"]
    assert app.start() == 0
    text = out.getvalue()
    assert "qwen3:8b (local), llama3.1:8b (local)" in text
    assert text.index("Model: ") < text.index("qwen3:8b (local)")


# -- fitting the window --------------------------------------------------------------------------------------------

def test_addendum_shortened_for_window(tmp_path):
    cwd, home = tmp_path / "proj", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    (cwd / "AGENTX.md").write_text("rule " * 6000, encoding="utf-8")          # 30,000 characters
    log = "".join(f"\n## entry {n}\n" + "did things " * 40 for n in range(40))
    note = "(shortened to fit qwen3:8b's 8,192-token window)"
    full = build_addendum(cwd, home, log_text=log)
    assert note not in full and full.count("rule") >= MAX_CONTEXT_CHARS // 5 - 1
    fitted = build_addendum(cwd, home, log_text=log, max_context_chars=8192, max_log_chars=2730,
                            shortened_note=note)
    assert note in fitted
    assert fitted.count("rule ") <= 8192 // 5 + 1
    log_part = fitted.split("<project-log>")[1].split("</project-log>")[0]
    assert len(log_part) <= 2730 + 40
    assert note not in build_addendum(cwd, home, shortened_note=note.replace("8,192", "1,000,000"),
                                      max_context_chars=10**6, max_log_chars=10**6)


def test_a_local_app_gives_the_instructions_a_third_of_the_window(tmp_path):
    model = local_mock(window=8192)
    app, _, cwd, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="qwen3:8b")
    (cwd / "AGENTX.md").write_text("rule " * 6000, encoding="utf-8")
    assert app.start() == 0
    assert "(shortened to fit qwen3:8b's 8,192-token window)" in app.addendum
    # share = 8192 * 4 // 3 = 10922; the log gets a quarter (2730), the instruction files the rest (8192)
    assert app.addendum.count("rule ") <= 8192 // 5 + 1
    assert app.addendum in app.session.runner.system_addendum


def test_switching_to_a_cloud_model_gives_the_instructions_their_full_room(tmp_path, monkeypatch):
    local = local_mock(window=8192)
    cloud = MockModel(script=[])
    models = {"qwen3:8b": local, "first": cloud}
    app, _, cwd, _ = make_app(tmp_path, Lines("/model first", "/exit"), lambda name, env: models[name],
                              model_name="qwen3:8b")
    (cwd / "AGENTX.md").write_text("rule " * 6000, encoding="utf-8")
    assert app.start() == 0
    assert app.model is cloud and "shortened to fit" not in app.addendum
    assert app.addendum.count("rule ") >= MAX_CONTEXT_CHARS // 5 - 1


def _long_history(turns=8, chars=3500):
    history = []
    for n in range(turns):
        history.append({"role": "user", "content": f"question {n} " + "x" * chars})
        history.append({"role": "assistant", "content": f"answer {n}"})
    return history                              # about 28,000 characters: past 60% of 8k tokens, under 60,000


def test_compaction_threshold_follows_window(tmp_path, monkeypatch):
    calls = []

    def spy(history, call, **kw):
        calls.append(kw)
        return Compaction("none", history)

    monkeypatch.setattr(app_usage, "compact", spy)
    model = local_mock(window=8192)
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="qwen3:8b")
    app.start()
    app.session.history = _long_history()
    app._maybe_compact()
    assert calls == [{"threshold": int(8192 * 4 * 0.6), "max_input_chars": int(8192 * 4 * 0.5)}]

    calls.clear()
    cloud = MockModel(script=[])
    (tmp_path / "cloud").mkdir()
    app, _, _, _ = make_app(tmp_path / "cloud", Lines("/exit"), lambda name, env: cloud, model_name="first")
    app.start()
    app.session.history = _long_history()
    app._maybe_compact()
    assert calls == []                          # a cloud model keeps today's 60,000 characters


def test_compact_caps_its_input(monkeypatch):
    from agentx_dev.cli import compact as compact_module
    sent = []
    history = _long_history(turns=10, chars=5000)
    result = compact_module.compact(history, lambda prompt: (sent.append(prompt), "a summary of all of it, long enough")[1],
                                    threshold=1000, max_input_chars=4000)
    assert result.status == "done"
    assert len(sent[0]) <= len(compact_module.SUMMARY_PROMPT) + 4000 + len("(earlier part omitted)\n")


def test_too_small_message(tmp_path):
    model = local_mock(window=300, script=[make_final("never sent")])
    app, out, _, _ = make_app(tmp_path, Lines("hello", "/exit"), lambda name, env: model, model_name="qwen3:8b")
    assert app.start() == 0
    text = out.getvalue()
    assert ("qwen3:8b's 300-token window is too small for this conversation. A variant with a larger window fixes "
            "it: a Modelfile with FROM qwen3:8b and PARAMETER num_ctx 16384, then ollama create qwen3:8b-16k -f "
            "Modelfile.") in text
    assert "Something went wrong" not in text and model.calls == []


def test_too_small_message_names_the_window_it_was_given(tmp_path, monkeypatch):
    def stream(self, *args, **kwargs):
        raise ContextWindowTooSmall(8192, 9000)
        yield                                      # a generator, like the real one

    monkeypatch.setattr(AgentRunner, "stream", stream)
    model = local_mock(window=8192)
    app, out, _, _ = make_app(tmp_path, Lines("hello", "/exit"), lambda name, env: model, model_name="qwen3:8b")
    app.start()
    assert "qwen3:8b's 8,192-token window is too small for this conversation." in out.getvalue()


def _trimming_stream(how):
    def stream(self, *args, **kwargs):
        yield {"type": "context_trimmed", "window": 8192, "how": how}
        yield {"type": "context_trimmed", "window": 8192, "how": how}
        yield {"type": "final", "content": "done"}
    return stream


def test_trimmed_notice_once_per_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentRunner, "stream", _trimming_stream("results"))
    model = local_mock(window=8192)
    app, out, _, _ = make_app(tmp_path, Lines("one", "two", "/clear", "three", "/exit"),
                              lambda name, env: model, model_name="qwen3:8b")
    assert app.start() == 0
    notice = ("qwen3:8b is working with less than the whole conversation: older tool results were shortened to fit "
              "its 8,192-token window.")
    assert out.getvalue().count(notice) == 2          # once before /clear, once after


def test_trimmed_notice_for_a_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(AgentRunner, "stream", _trimming_stream("summary"))
    model = local_mock(window=8192)
    app, out, _, _ = make_app(tmp_path, Lines("one", "/exit"), lambda name, env: model, model_name="qwen3:8b")
    app.start()
    assert ("qwen3:8b is working with less than the whole conversation: the earlier conversation was summarised to "
            "fit its 8,192-token window.") in out.getvalue()


def test_a_local_model_never_gets_the_credit_wording(tmp_path, monkeypatch):
    def stream(self, *args, **kwargs):
        raise RuntimeError("Error code: 429 - insufficient_quota")
        yield

    monkeypatch.setattr(AgentRunner, "stream", stream)
    model = local_mock()
    app, out, _, _ = make_app(tmp_path, Lines("hello", "/exit"), lambda name, env: model, model_name="qwen3:8b")
    app.start()
    text = out.getvalue()
    assert "credit" not in text and "Something went wrong (RuntimeError)" in text


# -- the settings reach the app ------------------------------------------------------------------------------------

def test_run_binds_the_local_settings_into_the_factory(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "proj"
    (home / ".agentx").mkdir(parents=True)
    cwd.mkdir()
    (home / ".agentx" / "settings.json").write_text(json.dumps(
        {"model": "qwen3:8b", "localServer": "http://127.0.0.1:1234", "localContext": 16384}), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(app_module, "is_trusted", lambda *args, **kwargs: True)
    given = {}

    class FakeApp:
        def __init__(self, **kwargs):
            given.update(kwargs)

        def start(self, resume=None):
            return 0

    monkeypatch.setattr(app_module, "ChatApp", FakeApp)
    console = Console(file=io.StringIO())
    assert app_module._run(build_parser().parse_args([]), home, console, Lines(), Lines(), None, []) == 0
    assert given["local_server"] == "http://127.0.0.1:1234/v1"
    model = given["model_factory"]("qwen3:8b", {})
    assert isinstance(model, Local)
    assert model.base_url == "http://127.0.0.1:1234/v1" and model.context_window == 16384


# -- fix round 1: the check can be stopped, a switch is all or nothing, the note means the window ------------------

def _started_on_cloud(tmp_path, factory_extra=None):
    cloud = MockModel(script=[make_final("hi")])
    models = {"first": cloud, **(factory_extra or {})}
    app, out, cwd, home = make_app(tmp_path, Lines("/exit"), lambda name, env: models[name], model_name="first")
    assert app.start() == 0
    return app, out, cloud


def test_ctrl_c_stops_a_slow_check_at_once(tmp_path):
    from agentx_dev.cli.fullscreen import raise_in
    release = threading.Event()
    slow = local_mock()
    slow.check = lambda timeout=120.0: (release.wait(), LocalCheck(True, "", 8192))[1]
    app, _, cloud = _started_on_cloud(tmp_path, {"qwen3:8b": slow})
    runner = app.session.runner
    ended = []

    def switch():
        try:
            app.switch_model("qwen3:8b")
        except KeyboardInterrupt:
            ended.append(time.monotonic())

    waiter = threading.Thread(target=switch, daemon=True)
    waiter.start()
    try:
        time.sleep(0.3)                                # the check is running
        raised = time.monotonic()
        assert raise_in(waiter)                        # what Ctrl+C in the window (and Stop) does
        waiter.join(3)
        assert ended and ended[0] - raised < 1
    finally:
        release.set()                                  # the abandoned check ends
    assert app.model is cloud and app.session.runner is runner and app.requested_model == "first"


def test_a_switch_that_fails_building_the_runner_changes_nothing(tmp_path, monkeypatch):
    other = MockModel(script=[])
    app, _, cloud = _started_on_cloud(tmp_path, {"other": other})
    runner = app.session.runner

    def broken(model):
        raise RuntimeError("cannot read AGENTX.md")

    monkeypatch.setattr(app, "_make_runner", broken)
    with pytest.raises(RuntimeError):
        app.switch_model("other")
    assert app.model is cloud and app.session.runner is runner and app.requested_model == "first"


def test_a_large_window_cut_is_the_usual_cap_not_the_window(tmp_path):
    model = local_mock(window=1_000_000)
    app, _, cwd, _ = make_app(tmp_path, Lines("/exit"), lambda name, env: model, model_name="qwen3:8b")
    (cwd / "AGENTX.md").write_text("rule " * 6000, encoding="utf-8")          # past the usual 20,000
    assert app.start() == 0
    assert "(truncated)" in app.addendum and "shortened to fit" not in app.addendum


def _with_log(cwd):
    (cwd / ".agentx").mkdir(exist_ok=True)
    (cwd / ".agentx" / "log.md").write_text("## 2026-11-20 10:00 earlier work\nDid the earlier thing.\n",
                                             encoding="utf-8")


def test_a_switch_after_resume_rebuilds_without_the_log(tmp_path):
    local = local_mock(window=8192)
    cloud = MockModel(script=[make_final("hi")])
    models = {"first": cloud, "qwen3:8b": local}
    app, _, cwd, _ = make_app(tmp_path, Lines("hello", "/exit"), lambda name, env: models[name], model_name="first")
    _with_log(cwd)
    assert app.start() == 0
    old = app.session.session_id
    app.new_conversation()
    assert "<project-log>" in app.addendum            # a fresh conversation starts from the log
    app.resume(old)
    app.switch_model("qwen3:8b")
    assert "<project-log>" not in app.addendum and "<project-log>" not in app.session.runner.system_addendum


def test_a_local_to_cloud_switch_keeps_the_log(tmp_path):
    local = local_mock(window=8192)
    cloud = MockModel(script=[])
    models = {"first": cloud, "qwen3:8b": local}
    app, _, cwd, _ = make_app(tmp_path, Lines("/model first", "/exit"), lambda name, env: models[name],
                              model_name="qwen3:8b")
    _with_log(cwd)
    assert app.start() == 0
    assert app.model is cloud
    assert "Did the earlier thing." in app.addendum and "Did the earlier thing." in app.session.runner.system_addendum
