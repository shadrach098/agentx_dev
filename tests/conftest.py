"""Shared pytest fixtures.

MockModel implements just enough of BaseChatModel for the runner + parser
tests to exercise every code path without an actual LLM key. Behavior is
scripted per-test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

import pytest

from agentx_dev import BaseChatModel


class MockModel(BaseChatModel):
    """Scripted chat model.

    ``script`` is either:
      - a list of strings — each `Initialize` call pops the next string.
      - a callable ``(messages) -> str`` — full control per call.

    ``tool_script`` is the analogous shape for ``call_with_tools``: each
    call returns the next dict.

    **Text mode vs function-calling mode.** Since 3.1.7 ``AgentRunner``
    auto-detects ``use_function_calling`` by asking whether the model
    class actually overrides ``call_with_tools``. This mock used to
    define it unconditionally, so every ``MockModel(script=[...])`` --
    a *text* script -- was routed down the FC path, where an empty
    ``tool_script`` returned ``{"type": "text", "text": ""}`` and the
    runner produced ``""``. That silently broke 20 text-mode tests.

    A mock must therefore advertise the capability only when it is
    scripted for it: passing ``tool_script`` returns a subclass that
    implements ``call_with_tools``, and a bare ``script`` returns this
    class, which does not. Auto-detect then picks the mode the test
    actually meant, with no per-test flag.
    """

    def __new__(cls, script: Any = None, tool_script: Optional[List[dict]] = None):
        if cls is MockModel and tool_script is not None:
            cls = _MockToolCallingModel
        return object.__new__(cls)

    def __init__(
        self,
        script: Any = None,
        tool_script: Optional[List[dict]] = None,
    ):
        self._script = script or []
        self._script_fn = script if callable(script) else None
        self._tool_script = list(tool_script or [])
        self.calls: List[List[dict]] = []
        self.tool_calls_made: List[List[dict]] = []

    def Initialize(self, messages) -> str:
        self.calls.append(list(messages))
        self._record_usage_counts(input_tokens=10, output_tokens=5)
        if self._script_fn is not None:
            return self._script_fn(messages)
        if isinstance(self._script, list) and self._script:
            return self._script.pop(0)
        return ""

    async def async_initialize(self, messages) -> str:
        return self.Initialize(messages)


class _MockToolCallingModel(MockModel):
    """MockModel that DOES implement native function calling.

    Constructed for you by ``MockModel(tool_script=[...])``; instantiate
    it directly only if a test needs an FC-capable model with an empty
    script.
    """

    def call_with_tools(self, messages, tools, *, force_tool=None):
        self.tool_calls_made.append(list(messages))
        self._record_usage_counts(input_tokens=10, output_tokens=5)
        if not self._tool_script:
            return {"type": "text", "text": ""}
        return self._tool_script.pop(0)

    async def async_call_with_tools(self, messages, tools, *, force_tool=None):
        return self.call_with_tools(messages, tools, force_tool=force_tool)


@pytest.fixture
def mock_model():
    return MockModel()


@pytest.fixture
def tmp_workspace(tmp_path):
    """A fresh workspace directory scoped to the test."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def make_react_response(action: str, action_input: Any, thought: str = "") -> str:
    """Build a JSON blob shaped like a ReAct parser response."""
    return json.dumps({
        "Thought": thought or "Thinking...",
        "action": action,
        "action_input": action_input,
    })


def make_final(text: str) -> str:
    return make_react_response("Final_Answer", text)


@pytest.fixture
def make_react():
    return make_react_response


@pytest.fixture
def make_final_response():
    return make_final


@pytest.fixture(autouse=True)
def _no_real_tailscale(monkeypatch):
    """No test reaches the Tailscale on the machine running the tests (a test once shared a port through it and
    cleared that machine's own entries): Tailscale is "not installed" unless a test stands in for it."""
    try:
        from agentx_dev.cli import webui
    except Exception:                                   # the CLI's packages are not installed: nothing to guard
        yield
        return
    monkeypatch.setattr(webui, "_tailscale_exe", lambda: None)
    yield


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """No test reaches a computer other than this one (AGENTX.md: never call a real API in tests). A connection
    to anywhere but this computer is refused at once and fails the test, even when the code under test catches
    the error and falls back (recall does); servers on 127.0.0.1 are fine."""
    import socket
    real_connect = socket.socket.connect
    tried: List[str] = []

    def connect(self, address, *args, **kwargs):
        host = str(address[0] if isinstance(address, tuple) else address)
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in _LOCAL_HOSTS \
                and not host.startswith("127."):
            tried.append(host)
            raise ConnectionRefusedError(f"tests may not reach {host} over the network")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    yield tried
    assert not tried, f"the test tried to reach {sorted(set(tried))} over the network; use a stand-in instead"


@pytest.fixture(autouse=True)
def _offline_recall(monkeypatch):
    """The app's project notes use the offline embeddings in tests, even when a test's env holds an OpenAI key
    (a fake one would otherwise send note text to api.openai.com). ``choose_embeddings`` itself is tested directly."""
    try:
        from agentx_dev.cli import app
        from agentx_dev.Embeddings import HashEmbeddings
    except Exception:                                   # the CLI's packages are not installed: nothing to change
        yield
        return
    monkeypatch.setattr(app, "choose_embeddings", lambda env: (HashEmbeddings(), "hash"))
    yield


@pytest.fixture(autouse=True)
def _forget_learned_endpoints():
    """Which OpenAI models need the Responses API is remembered for the whole process; each test starts fresh."""
    from agentx_dev.ChatModel import forget_learned_endpoints
    forget_learned_endpoints()
    yield
    forget_learned_endpoints()


import logging as _logging
import os as _os
import tempfile as _tempfile

# The real home and the temp folder, read before any test can change them (the temp folder is inside the home on
# Windows: a path under it is a test's own).
_REAL_HOME = str(Path.home())
_REAL_HOME_PATH = _os.path.realpath(_REAL_HOME)
_TEMP_PATH = _os.path.realpath(_tempfile.gettempdir())


def _under(path: str, folder: str) -> bool:
    try:
        return _os.path.commonpath([_os.path.normcase(path), _os.path.normcase(folder)]) == _os.path.normcase(folder)
    except ValueError:                                  # another drive
        return False


def _in_real_home(path: str) -> bool:
    """Whether ``path`` is in the real home (and not in the temp folder)."""
    real = _os.path.realpath(str(path))
    return _under(real, _REAL_HOME_PATH) and not _under(real, _TEMP_PATH)


def _log_handlers() -> list:
    """(logger, handler) for the root logger, ``agentx_dev`` and every ``agentx_dev.*`` logger."""
    names = [""] + [name for name in list(_logging.root.manager.loggerDict)
                    if name == "agentx_dev" or name.startswith("agentx_dev.")]
    pairs = []
    for name in names:
        logger = _logging.getLogger(name) if name else _logging.getLogger()
        pairs += [(logger, handler) for handler in list(logger.handlers)]
    return pairs


def _real_home_logs(pairs) -> list:
    """The paths of the file handlers among ``pairs`` that write in the real home."""
    return [handler.baseFilename for _, handler in pairs
            if isinstance(handler, _logging.FileHandler) and _in_real_home(handler.baseFilename)]


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path_factory):
    """Each test runs with a home of its own in the temp folder (``HOME``, ``USERPROFILE``, so ``Path.home()`` and
    ``~``): nothing a test runs writes in the real home. A test that sets its own home (``Path.home`` patched, or
    ``home=``) wins over this."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    yield home


@pytest.fixture(autouse=True)
def _no_log_left_behind():
    """No test leaves agentx's log handlers behind, and none writes a log in the real home: ``setup_logging`` (run by
    ``main``, ``_run`` and ``_run_ui``) puts a file handler on the ``agentx_dev`` logger that every later test would
    write its tracebacks through. Handlers agentx added during a test are removed and closed after it; a file handler
    added on the root logger or an ``agentx_dev`` logger that writes in the real home is removed too and fails the
    test (give it a tmp home: ``Path.home`` patched, or ``home=``)."""
    before = {(id(logger), id(handler)) for logger, handler in _log_handlers()}
    yield
    added = [(logger, handler) for logger, handler in _log_handlers() if (id(logger), id(handler)) not in before]
    real = _real_home_logs(added)
    for logger, handler in added:
        if getattr(handler, "_agentx_cli", False) or handler.__dict__.get("baseFilename") in real:
            logger.removeHandler(handler)
            handler.close()
    assert not real, f"the test set up a log in the real home ({real}); give it a tmp home instead"
