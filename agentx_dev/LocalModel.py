"""``Local``: a chat model for a server on this machine (or network) that speaks the OpenAI chat-completions
protocol -- Ollama, LM Studio, llama.cpp. Any model name that is not a hosted one is a local one."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from .ChatModel import GPT

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
_DEFAULT_WINDOW = 8192
_PLACEHOLDER_KEY = "local"


class LocalModelError(Exception):
    """The local server could not be used; the message says why in a short sentence."""


@dataclass(frozen=True)
class LocalCheck:
    ok: bool
    reason: str
    context_window: Optional[int]


def normalise_base_url(url: str) -> str:
    url = (url or DEFAULT_LOCAL_URL).strip().rstrip("/")
    if urlparse(url).path in ("", "/"):
        url += "/v1"
    return url


def _hostport(base_url: str) -> str:
    return urlparse(base_url).netloc or base_url


def _root(base_url: str) -> str:
    """The server's address without the /v1 path (where Ollama's /api/* lives)."""
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def _not_answering(base_url: str) -> str:
    return f"the local server at {_hostport(base_url)} is not answering"


def _http_json(url: str, timeout: float, body: Optional[dict] = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class Local(GPT):
    """A model served locally. Chat completions only; never reads ``OPENAI_API_KEY``."""

    is_local = True
    prefers_native_tools = True

    def __init__(self, model: Optional[str] = None, base_url: str = DEFAULT_LOCAL_URL,
                 api_key: Optional[str] = None, context_window: Optional[int] = None,
                 timeout: float = 300.0, **kw):
        base_url = normalise_base_url(base_url)
        if not model:
            names = Local.models(base_url)
            if not names:
                raise LocalModelError(f"the local server at {_hostport(base_url)} lists no models")
            if len(names) > 1:
                raise LocalModelError(
                    f"the local server at {_hostport(base_url)} has several models, name one: "
                    + ", ".join(names))
            model = names[0]
        kw.pop("use_responses_api", None)
        super().__init__(api_key=api_key or _PLACEHOLDER_KEY, base_url=base_url, timeout=timeout,
                         model=model, use_responses_api=False, **kw)
        self.model = model
        self.base_url = base_url
        self.context_window = context_window

    @staticmethod
    def models(base_url: str = DEFAULT_LOCAL_URL, timeout: float = 5.0) -> List[str]:
        base_url = normalise_base_url(base_url)
        try:
            data = _http_json(base_url + "/models", timeout)
            return [str(m["id"]) for m in data["data"]]
        except urllib.error.HTTPError as e:
            raise LocalModelError(f"the local server at {_hostport(base_url)} answered {e.code} to the model list")
        except (urllib.error.URLError, OSError, TimeoutError):
            raise LocalModelError(_not_answering(base_url))
        except (ValueError, KeyError, TypeError):
            raise LocalModelError(f"the local server at {_hostport(base_url)} sent a model list agentx cannot read")

    def server_window(self, timeout: float = 5.0) -> Optional[int]:
        """The window Ollama will use for this model, or None for a server that is not Ollama."""
        root = _root(self.base_url)
        try:
            ps = _http_json(root + "/api/ps", timeout)
        except Exception:
            return None
        if not isinstance(ps, dict) or "models" not in ps:
            return None
        for m in ps.get("models") or []:
            names = {m.get("name"), m.get("model")}
            if self.model in names or f"{self.model}:latest" in names:
                n = m.get("context_length")
                if isinstance(n, int) and n > 0:
                    return n
        try:
            show = _http_json(root + "/api/show", timeout, {"model": self.model})
            hit = re.search(r"^\s*num_ctx\s+(\d+)", str(show.get("parameters") or ""), re.M)
            if hit:
                return int(hit.group(1))
        except Exception:
            pass
        return _DEFAULT_WINDOW

    def check(self, timeout: float = 120.0) -> LocalCheck:
        try:
            names = Local.models(self.base_url, timeout=min(timeout, 5.0))
        except LocalModelError as e:
            return LocalCheck(False, str(e), self.context_window)
        if self.model not in names and f"{self.model}:latest" not in names:
            return LocalCheck(False, f"the local server at {_hostport(self.base_url)} has no model named "
                                     f"{self.model}; it lists: {', '.join(names) or 'nothing'}",
                              self.context_window)
        ping = {"type": "function", "function": {
            "name": "ping", "description": "Send a short text back.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"]}}}
        try:
            client = self.client.with_options(timeout=timeout, max_retries=0)
            resp = client.chat.completions.create(
                model=self.model, tools=[ping],
                messages=[{"role": "user", "content": "Call the ping tool with the text hi."}])
        except Exception as e:
            if type(e).__name__ in ("APIConnectionError", "APITimeoutError"):
                return LocalCheck(False, _not_answering(self.base_url), self.context_window)
            return LocalCheck(False, f"the local server at {_hostport(self.base_url)} refused the test "
                                     f"request: {str(e)[:200]}", self.context_window)
        calls = None
        try:
            calls = resp.choices[0].message.tool_calls
        except (AttributeError, IndexError):
            pass
        if not calls:
            return LocalCheck(False, f"{self.model} answered in text instead of calling the tool, so it "
                                     "cannot drive agentx's tools", self.context_window)
        if self.context_window is None:
            self.context_window = self.server_window()
        return LocalCheck(True, "", self.context_window)
