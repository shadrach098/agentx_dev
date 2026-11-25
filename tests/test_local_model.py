"""The Local model, against a fake OpenAI-compatible server on 127.0.0.1."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentx_dev import Local, LocalCheck, LocalModelError
from agentx_dev.LocalModel import normalise_base_url


class _Fake:
    def __init__(self, models=("m",), ps=None, show=None, tool=True, ollama=True):
        self.models, self.ps, self.show, self.tool, self.ollama = list(models), ps, show, tool, ollama
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/models":
                    self._send(200, {"data": [{"id": m} for m in fake.models]})
                elif self.path == "/api/ps" and fake.ollama:
                    self._send(200, {"models": fake.ps or []})
                else:
                    self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/show" and fake.ollama:
                    self._send(200, fake.show or {})
                elif self.path == "/v1/chat/completions":
                    if fake.tool:
                        msg = {"role": "assistant", "content": None, "tool_calls": [
                            {"id": "c1", "type": "function",
                             "function": {"name": "ping", "arguments": "{\"text\": \"hi\"}"}}]}
                        fin = "tool_calls"
                    else:
                        msg, fin = {"role": "assistant", "content": "hello there"}, "stop"
                    self._send(200, {"id": "x", "object": "chat.completion", "created": 0,
                                     "model": body.get("model", "m"),
                                     "choices": [{"index": 0, "message": msg, "finish_reason": fin}]})
                else:
                    self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    made = []

    def make(**kw):
        f = _Fake(**kw)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def _closed_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1", f"127.0.0.1:{port}"


def test_local_never_reads_openai_key(monkeypatch, fake):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    assert Local("m", base_url=fake().url).client.api_key == "local"


def test_local_uses_chat_completions_only():
    m = Local("m")
    assert m.use_responses_api is False
    assert m.is_local is True and m.prefers_native_tools is True
    assert m.context_window is None


def test_base_url_is_normalised():
    assert (normalise_base_url("http://localhost:11434")
            == normalise_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1")


def test_no_name_uses_the_only_model(fake):
    assert Local(base_url=fake(models=["only:1b"]).url).model == "only:1b"


def test_no_name_several_models_names_them(fake):
    with pytest.raises(LocalModelError) as e:
        Local(base_url=fake(models=["a:1b", "b:2b"]).url)
    assert "a:1b" in str(e.value) and "b:2b" in str(e.value)


def test_no_name_no_models(fake):
    with pytest.raises(LocalModelError, match="lists no models"):
        Local(base_url=fake(models=[]).url)


def test_models_lists_names(fake):
    assert Local.models(fake(models=["a", "b"]).url) == ["a", "b"]


def test_models_refused_connection_says_not_running():
    url, hostport = _closed_url()
    with pytest.raises(LocalModelError) as e:
        Local.models(url, timeout=2)
    assert "is not answering" in str(e.value) and hostport in str(e.value)


def test_check_ok_reads_loaded_window(fake):
    f = fake(models=["m"], ps=[{"name": "m", "context_length": 8192}])
    m = Local("m", base_url=f.url)
    assert m.check(timeout=10) == LocalCheck(True, "", 8192)
    assert m.context_window == 8192


def test_check_reads_num_ctx_when_not_loaded(fake):
    f = fake(models=["m"], ps=[],
             show={"parameters": "stop   \"<x>\"\nnum_ctx                        16384\n",
                   "model_info": {"llama.context_length": 131072}})
    m = Local("m", base_url=f.url)
    assert m.check(timeout=10).context_window == 16384


def test_check_defaults_to_8192_without_num_ctx(fake):
    f = fake(models=["m"], ps=[], show={"parameters": "", "model_info": {"llama.context_length": 131072}})
    assert Local("m", base_url=f.url).check(timeout=10).context_window == 8192


def test_check_non_ollama_passes_without_window(fake):
    f = fake(models=["m"], ollama=False)
    m = Local("m", base_url=f.url)
    r = m.check(timeout=10)
    assert r.ok and r.context_window is None and m.server_window() is None


def test_check_given_window_is_kept(fake):
    f = fake(models=["m"], ps=[{"name": "m", "context_length": 8192}])
    m = Local("m", base_url=f.url, context_window=4096)
    assert m.check(timeout=10).context_window == 4096


def test_check_missing_model(fake):
    r = Local("zzz", base_url=fake(models=["a", "b"]).url).check(timeout=10)
    assert not r.ok and "no model named" in r.reason and "a" in r.reason and "b" in r.reason
    assert not r.unreachable                                  # the server answered


def test_check_text_instead_of_tool(fake):
    r = Local("m", base_url=fake(models=["m"], tool=False).url).check(timeout=10)
    assert not r.ok and "answered in text instead of calling the tool" in r.reason and not r.unreachable


def test_check_server_down():
    url, hostport = _closed_url()
    r = Local("m", base_url=url).check(timeout=2)
    assert not r.ok and "is not answering" in r.reason and hostport in r.reason
    assert r.unreachable                                      # another model there would fail the same way


def test_a_scheme_less_address_gets_http():
    assert normalise_base_url("localhost:1234") == "http://localhost:1234/v1"
    assert normalise_base_url("gpu-box.lan") == "http://gpu-box.lan/v1"


def test_messages_name_the_server_without_its_credentials():
    from agentx_dev.LocalModel import _not_answering
    from agentx_dev.cli.app_models import server_name
    text = _not_answering("http://user:s3cret@gpu-box:8080/v1")
    assert text == "the local server at gpu-box:8080 is not answering"
    assert _not_answering("http://[::1]:11434/v1") == "the local server at [::1]:11434 is not answering"
    assert _not_answering("http://localhost/v1") == "the local server at localhost is not answering"
    assert "s3cret" not in server_name("http://user:s3cret@gpu-box:8080/v1")
    assert server_name("http://user:s3cret@gpu-box:8080/v1") == "The local server at gpu-box:8080"
    with pytest.raises(LocalModelError) as e:
        Local.models("http://user:s3cret@127.0.0.1:9/v1", timeout=0.5)
    assert "s3cret" not in str(e.value) and "user" not in str(e.value)
