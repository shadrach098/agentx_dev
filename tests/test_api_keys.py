"""API keys set from the app window: kept by the system's credential store (here a fake one: a test never touches
the user's real keys), a saved key wins over the environment, a new key is used from the next message, and a key
is never shown in full or sent back to the page."""

import pytest

from agentx_dev.cli import keystore
from tests.conftest import MockModel, make_final
from tests.test_cli_app import Lines, make_app

CLAUDE_KEY = "sk-ant-test-0000000000000000-WXYZ"
NEW_CLAUDE = "sk-ant-test-1111111111111111-ABCD"
ENV_CLAUDE = "sk-ant-env-2222222222222222-ENVK"


class FakeKeyring:
    """The few calls ``keystore`` makes on ``keyring``, kept in a dict."""

    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


@pytest.fixture
def ring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(keystore, "_keyring", lambda: fake)
    return fake


class TestTheStore:
    def test_a_key_is_saved_read_and_removed(self, ring):
        assert keystore.save("ANTHROPIC_API_KEY", f"  {CLAUDE_KEY}\n") == CLAUDE_KEY
        assert ring.values == {(keystore.SERVICE, "ANTHROPIC_API_KEY"): CLAUDE_KEY}
        assert keystore.get("ANTHROPIC_API_KEY") == CLAUDE_KEY and keystore.saved() == {"ANTHROPIC_API_KEY": CLAUDE_KEY}
        assert keystore.remove("ANTHROPIC_API_KEY") is True and keystore.get("ANTHROPIC_API_KEY") is None
        assert keystore.remove("ANTHROPIC_API_KEY") is False

    @pytest.mark.parametrize("bad", ["", "   ", "short", "sk-ant has spaces in it", "sk-ant-\x00-control-char",
                                     "sk-ant-ünicode-0000000000", "x" * 600])
    def test_what_cannot_be_a_key_is_refused_without_repeating_it(self, ring, bad):
        with pytest.raises(keystore.KeyStoreError) as caught:
            keystore.save("OPENAI_API_KEY", bad)
        assert (bad.strip() == "" or bad not in str(caught.value)) and ring.values == {}

    def test_only_the_two_providers_variables_are_kept(self, ring):
        with pytest.raises(keystore.KeyStoreError):
            keystore.save("PATH", CLAUDE_KEY)
        assert keystore.get("PATH") is None and ring.values == {}

    def test_only_the_last_four_characters_are_shown(self):
        assert keystore.tail(CLAUDE_KEY) == "WXYZ" and keystore.tail("short") == ""

    def test_without_keyring_saving_says_what_to_install(self, monkeypatch):
        monkeypatch.setattr(keystore, "_keyring", lambda: None)
        with pytest.raises(keystore.KeyStoreError, match="pip install keyring"):
            keystore.save("OPENAI_API_KEY", CLAUDE_KEY)
        assert keystore.get("OPENAI_API_KEY") is None and keystore.available() is False


def app_with_store(tmp_path, ring, lines, env=None, built=None):
    """A chat app on a Claude model whose factory records the key it was built with."""
    built = [] if built is None else built

    def factory(name, env):
        built.append(env.get("ANTHROPIC_API_KEY"))
        return MockModel(script=[make_final("ok"), make_final("ok again")])
    app, out, cwd, home = make_app(tmp_path, lines, None, env=dict(env or {"ANTHROPIC_API_KEY": ENV_CLAUDE}),
                                   model_factory=factory, model_name="claude-opus-5-5")
    app._key_store = keystore                  # the real module, on the fake keyring
    return app, out, built


class TestTheApp:
    def test_a_saved_key_wins_over_the_environment_at_the_start(self, tmp_path, ring):
        keystore.save("ANTHROPIC_API_KEY", CLAUDE_KEY)
        app, out, built = app_with_store(tmp_path, ring, Lines("/exit"))
        assert app.start() == 0
        assert built == [CLAUDE_KEY]
        assert CLAUDE_KEY not in out.getvalue()

    def test_a_key_saved_in_the_window_is_used_from_the_next_message(self, tmp_path, ring):
        holder = {}
        lines = Lines("hello", lambda: holder.update(said=holder["app"].change_key("claude", NEW_CLAUDE)),
                      "and again", "/exit")
        app, out, built = app_with_store(tmp_path, ring, lines)
        holder["app"] = app
        assert app.start() == 0
        assert built == [ENV_CLAUDE, NEW_CLAUDE]                 # built again before the second message
        assert keystore.get("ANTHROPIC_API_KEY") == NEW_CLAUDE    # and kept for the next start
        assert "ends in ABCD" in holder["said"] and NEW_CLAUDE not in holder["said"]
        text = out.getvalue()
        assert "Using your new Claude (Anthropic) key (ends in ABCD)" in text and NEW_CLAUDE not in text

    def test_removing_the_saved_key_goes_back_to_the_environment(self, tmp_path, ring):
        keystore.save("ANTHROPIC_API_KEY", CLAUDE_KEY)
        holder = {}
        lines = Lines("hello", lambda: holder.update(said=holder["app"].change_key("claude", None)), "again", "/exit")
        app, out, built = app_with_store(tmp_path, ring, lines)
        holder["app"] = app
        assert app.start() == 0
        assert built == [CLAUDE_KEY, ENV_CLAUDE] and keystore.get("ANTHROPIC_API_KEY") is None
        assert "goes back to ANTHROPIC_API_KEY" in holder["said"]

    def test_the_status_names_the_source_and_the_last_four_only(self, tmp_path, ring):
        keystore.save("ANTHROPIC_API_KEY", CLAUDE_KEY)
        app, out, built = app_with_store(tmp_path, ring, Lines("/exit"), env={"ANTHROPIC_API_KEY": ENV_CLAUDE})
        app.load_stored_keys()
        rows = {row["provider"]: row for row in app.key_status()}
        assert rows["claude"]["source"] == "saved" and rows["claude"]["tail"] == "WXYZ" and rows["claude"]["shadows"]
        assert rows["openai"] == {**rows["openai"], "set": False, "source": "", "tail": ""}
        assert CLAUDE_KEY not in repr(rows) and ENV_CLAUDE not in repr(rows)

    def test_a_test_run_never_reaches_the_real_store(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines("/exit"), MockModel())
        assert app._key_store is None
        with pytest.raises(keystore.KeyStoreError):
            app.change_key("claude", CLAUDE_KEY)


class TestTheWindow:
    def test_the_page_gets_the_status_and_never_the_key(self, tmp_path, ring):
        from agentx_dev.cli.webui import WebFront
        app, out, built = app_with_store(tmp_path, ring, Lines("/exit"))
        front = WebFront(cwd=tmp_path / "proj", home=tmp_path / "home", open_it=False)
        front.app = app
        answer = front.change_key("claude", NEW_CLAUDE)
        assert answer["ok"] and "ends in ABCD" in answer["message"] and NEW_CLAUDE not in repr(answer)
        assert keystore.get("ANTHROPIC_API_KEY") == NEW_CLAUDE
        assert NEW_CLAUDE not in repr(front.keys_view())
        refused = front.change_key("claude", "not a key")
        assert not refused["ok"] and "not a key" not in refused["message"]
        assert front.change_key("claude", None)["ok"] and keystore.get("ANTHROPIC_API_KEY") is None
        assert not front.change_key("nobody", NEW_CLAUDE)["ok"]

    def test_the_settings_sheet_has_an_api_keys_tab(self):
        from agentx_dev.cli.webui import RESOURCES
        html = (RESOURCES / "index.html").read_text(encoding="utf-8")
        script = (RESOURCES / "app.js").read_text(encoding="utf-8")
        assert 'data-scope="keys"' in html and 'type="password"' in script and 'get("/api/keys")' in script
        assert 'if (field) field.value = "";' in script          # the key leaves the page once sent
