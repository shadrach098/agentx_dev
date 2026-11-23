"""The test suite itself never reaches the network (AGENTX.md: never call a real API in tests)."""
import socket

import pytest


def test_a_connection_off_this_computer_is_refused_and_noticed(_no_real_network):
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("203.0.113.7", 443), timeout=1)      # TEST-NET-3: never a real host
    assert _no_real_network == ["203.0.113.7"]
    _no_real_network.clear()                                           # this test meant to try it


def test_this_computer_is_still_reachable(_no_real_network):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with socket.create_connection(server.getsockname(), timeout=1):
        pass
    server.close()
    assert _no_real_network == []


def test_the_app_keeps_project_notes_offline_even_with_an_openai_key(tmp_path):
    from tests.conftest import MockModel
    from tests.test_cli_app import Lines, make_app
    app, _, _, _ = make_app(tmp_path, Lines("/exit"), MockModel(script=[]), env={"OPENAI_API_KEY": "sk-test-x"})
    app.start()
    assert app.project_memory is not None and app.project_memory.embedder_id == "hash"
