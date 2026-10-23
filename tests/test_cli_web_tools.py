"""web_search and web_fetch in the terminal app: readable text, capped, marked as untrusted; the careful modes ask
first (a URL or a query can carry text out of the project). Also: a wrapping-up turn refuses every tool call."""

from types import SimpleNamespace

import pytest

from agentx_dev.cli import tools as tools_module
from agentx_dev.cli.approval import WEB_GRANT, Answer, Gate, kind_of, subject_of
from agentx_dev.cli.preview import preview_for
from agentx_dev.cli.team import ROLES
from agentx_dev.cli.tools import WEB_CAUTION, WEB_MAX_CHARS, build_tools, web_tools


@pytest.fixture
def fake_web(monkeypatch):
    calls = []

    def fake_fetch_tool(**kw):
        calls.append(("built", kw))
        real = tools_module.__dict__["_real_fetch_tool"](**kw)
        return SimpleNamespace(func=lambda url, max_chars: calls.append((url, max_chars)) or "Page text",
                               args_schema=real.args_schema, description=real.description)

    monkeypatch.setitem(tools_module.__dict__, "_real_fetch_tool", tools_module.web_fetch_tool)
    monkeypatch.setattr(tools_module, "web_fetch_tool", fake_fetch_tool)
    monkeypatch.setattr(tools_module, "web_search_tool", lambda: SimpleNamespace(
        func=lambda query, num_results: f"1. result for {query}", args_schema=None, description="search"))
    return calls


class TestWhatTheyReturn:
    def test_a_page_is_readable_text_capped_and_marked(self, fake_web):
        fetch = {t.name: t for t in web_tools()}["web_fetch"]
        out = fetch.func(url="https://example.com", max_chars=200_000)
        assert out.startswith(WEB_CAUTION) and out.endswith("Page text")
        assert ("https://example.com", WEB_MAX_CHARS) in fake_web             # the model cannot ask for more
        assert ("built", {"text_only": True}) in fake_web                      # no raw markup

    def test_search_results_are_marked_too(self, fake_web):
        search = {t.name: t for t in web_tools()}["web_search"]
        assert search.func(query="flights") == WEB_CAUTION + "1. result for flights"

    def test_the_app_and_the_explorer_have_them(self, tmp_path):
        names = {t.name for t in build_tools(tmp_path, [], Gate(cwd=tmp_path))}
        assert {"web_search", "web_fetch"} <= names
        explorer = next(role for role in ROLES if role.name == "explorer")
        assert {"web_search", "web_fetch"} <= set(explorer.tools)


class TestApproval:
    def asking_gate(self, mode, answer="y"):
        asked = []
        gate = Gate(mode=mode, ask=lambda request: asked.append(request) or Answer(answer))
        return gate, asked

    def test_they_are_web_lookups(self):
        assert kind_of("web_fetch") == kind_of("web_search") == "web"
        assert subject_of("web_fetch", {"url": "https://a.example/x?q=1", "max_chars": 5}) == "https://a.example/x?q=1"
        assert subject_of("web_search", {"query": "rabat  airport", "num_results": 5}) == "rabat airport"

    def test_default_mode_asks_and_a_lets_them_all_through(self):
        gate, asked = self.asking_gate("default", "a")
        assert gate.check("web_fetch", {"url": "https://a.example"}) is None
        assert len(asked) == 1 and asked[0].grant == WEB_GRANT
        assert gate.check("web_search", {"query": "x"}) is None and len(asked) == 1
        assert gate.mode == "default"                        # "a" for the web does not switch on accept-edits

    def test_a_no_refuses(self):
        gate, asked = self.asking_gate("default", "n")
        assert gate.check("web_fetch", {"url": "https://a.example"}) is not None

    @pytest.mark.parametrize("mode", ["auto", "accept-edits"])
    def test_auto_and_accept_edits_do_not_ask(self, mode):
        gate, asked = self.asking_gate(mode)
        assert gate.check("web_fetch", {"url": "https://a.example"}) is None and asked == []

    def test_plan_mode_may_look_things_up_after_asking(self):
        gate, asked = self.asking_gate("plan")
        assert gate.check("web_search", {"query": "x"}) is None and len(asked) == 1

    def test_a_deny_rule_still_wins(self):
        gate = Gate(mode="auto", deny=["web_fetch:*evil.example*"])
        assert gate.check("web_fetch", {"url": "https://evil.example/a"}) is not None

    def test_the_prompt_shows_the_url_or_the_query(self, tmp_path):
        assert preview_for("web_fetch", {"url": "https://a.example"}, tmp_path).title == "Fetch a web page"
        assert preview_for("web_search", {"query": "x"}, tmp_path).body == "x"


class TestWrappingUp:
    def test_a_closed_gate_refuses_every_tool_without_running_it(self, tmp_path):
        (tmp_path / "a.txt").write_text("secret", encoding="utf-8")
        gate = Gate(mode="auto", cwd=tmp_path)
        tools = {t.name: t for t in build_tools(tmp_path, [], gate)}
        gate.closed = "Refused: wrap up."
        assert tools["read_path"].func(path="a.txt") == "Refused: wrap up."
        assert tools["run_python"].func(code="open('b.txt','w').write('x')") == "Refused: wrap up."
        assert not (tmp_path / "b.txt").exists()
        gate.closed = None
        assert tools["read_path"].func(path="a.txt") == "secret"
