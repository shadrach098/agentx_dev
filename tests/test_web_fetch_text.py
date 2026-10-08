"""web_fetch(text_only=True): readable text instead of the first 50k characters of raw markup.

Why: a real run gave three models the same pricing pages. By default web_fetch returned the first
50,000 characters of RAW HTML, and a modern page puts tens of thousands of characters of script and
style before any price. One model saw only the page title, another wrote a BeautifulSoup script to
strip the tags itself. Helpers the Supervisor builds now fetch with text_only=True.
"""

import pytest

from agentx_dev import WebTools
from agentx_dev.SubAgents import AgentSpec, SpawnConfig, SpawnPolicy
from tests.subagent_helpers import router

HEAVY = (
    "<!doctype html><html><head><title>Pricing</title><style>.x{color:red}</style>"
    "<script>var junk = '" + ("x" * 80_000) + "';</script></head>"
    "<body><nav>Menu</nav><h1>Plans</h1><p>Starter $10 per user per month</p>"
    "<script>track()</script></body></html>"
)


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


class FakeOpener:
    def __init__(self, body):
        self.body = body

    def open(self, req, timeout=None):
        return FakeResponse(self.body.encode("utf-8"))


@pytest.fixture
def serve(monkeypatch):
    def install(body):
        monkeypatch.setattr(WebTools, "_GUARDED_OPENER", FakeOpener(body))
        monkeypatch.setattr(WebTools, "_assert_public_url", lambda url: None)
    return install


def fetch(tool, url="https://example.test/pricing", **kw):
    return tool.func(url=url, **kw)


class TestTheProblem:
    def test_by_default_a_script_heavy_page_hides_its_prices_in_the_first_50k_characters(self, serve):
        serve(HEAVY)
        out = fetch(WebTools.web_fetch_tool())
        assert "<script>" in out                      # raw markup, unchanged behavior
        assert "Starter $10" not in out               # the price is past the 50k cut


class TestTextOnly:
    def test_the_price_is_in_the_text(self, serve):
        serve(HEAVY)
        out = fetch(WebTools.web_fetch_tool(text_only=True))
        assert "Starter $10 per user per month" in out and "Plans" in out and "Menu" in out
        assert "var junk" not in out and "<script" not in out and "color:red" not in out

    def test_json_and_plain_text_come_back_untouched(self, serve):
        serve('{"plans": [{"name": "<Starter>", "price": 10}]}')
        out = fetch(WebTools.web_fetch_tool(text_only=True))
        assert out == '{"plans": [{"name": "<Starter>", "price": 10}]}'

    def test_a_page_with_no_readable_text_says_so(self, serve):
        serve("<!doctype html><html><head><script>boot()</script></head>"
              "<body><div id='root'></div><script>render()</script></body></html>")
        out = fetch(WebTools.web_fetch_tool(text_only=True))
        assert "no readable text" in out and "JavaScript" in out

    def test_truncation_counts_readable_text_and_says_so(self, serve):
        serve("<html><body><p>" + ("word " * 100) + "</p></body></html>")
        out = fetch(WebTools.web_fetch_tool(text_only=True), max_chars=40)
        assert "preview truncated at 40 chars" in out and "readable text" in out
        assert len(out.split("\n\n...")[0]) == 40

    def test_errors_still_come_back_as_error_strings(self, serve, monkeypatch):
        monkeypatch.setattr(WebTools, "_assert_public_url", lambda url: None)
        out = fetch(WebTools.web_fetch_tool(text_only=True), url="ftp://example.test/x")
        assert out.startswith("ERROR:")

    def test_the_full_raw_body_is_still_cached_when_a_cache_dir_is_set(self, serve, tmp_path):
        serve(HEAVY)
        out = fetch(WebTools.web_fetch_tool(cache_dir=str(tmp_path), text_only=True))
        assert "Starter $10 per user per month" in out and "cached full body" in out
        saved = next(tmp_path.glob("fetch_*.html"))
        assert "var junk" in saved.read_text(encoding="utf-8")        # the cache keeps the raw page

    def test_the_description_tells_the_model_it_gets_readable_text(self):
        plain = WebTools.web_fetch_tool().description
        text = WebTools.web_fetch_tool(text_only=True).description
        assert "readable text" in text and "readable text" not in plain


class TestHelpersUseIt:
    def spec(self):
        return AgentSpec(name="researcher", instructions="You research.", tools=("web",), origin="plan")

    def test_a_helper_built_with_web_fetches_readable_text(self):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router())
        built = policy.build(self.spec())
        tool = next(t for t in built.runner.tools if t.name == "web_fetch")
        assert "readable text" in tool.description


class TestHelperInstructions:
    def test_a_helper_is_told_not_to_answer_with_a_script_or_guess(self):
        policy = SpawnPolicy(SpawnConfig(enabled=True, capabilities={"web"}), router())
        addendum = policy.build(AgentSpec(name="r", instructions="You research.", tools=("web",),
                                          origin="plan")).runner.system_addendum
        assert "never answer with a script" in addendum
        assert "what you tried and what was missing" in addendum
