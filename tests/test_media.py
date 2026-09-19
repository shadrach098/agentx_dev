"""Media input: Media objects, provider translation, and runner plumbing."""

import json
import types

import pytest

from agentx_dev import AgentRunner, AgentType, GPT, Claude, Media
from agentx_dev.Media import content_for_anthropic, content_for_openai, split_text_and_media

FINAL = json.dumps({"Thought": "t", "action": "Final_Answer", "action_input": "a red square"})


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "bruce.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return str(path)


def fake_gpt():
    g = GPT(api_key="x", model="gpt-4o")
    calls = []

    def create(**kw):
        calls.append(kw)
        msg = types.SimpleNamespace(content=FINAL, tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                     usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    g.client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))
    return g, calls


def fake_claude():
    c = Claude(api_key="x")
    calls = []

    def create(**kw):
        calls.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=FINAL)],
                                     usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    c.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return c, calls


def last_user(messages):
    return [m for m in messages if m.get("role") == "user"][-1]["content"]


class TestMediaObject:
    def test_from_path_infers_kind_and_type(self, png):
        m = Media.from_path(png)
        assert (m.kind, m.media_type, m.filename) == ("image", "image/png", "bruce.png")

    def test_url_is_not_fetched(self):
        m = Media.image("https://x.test/cat.jpg")
        assert m.url == "https://x.test/cat.jpg" and m.data is None

    def test_bytes_need_a_media_type(self):
        with pytest.raises(ValueError):
            Media.image(b"raw")

    def test_missing_file_is_a_clear_error(self):
        with pytest.raises(FileNotFoundError):
            Media.image("does/not/exist.png")

    def test_part_is_json_serialisable(self, png):
        json.dumps(Media.image(png).to_part())


class TestTranslation:
    def test_openai_shapes(self):
        parts = content_for_openai([
            "look",
            Media.image(b"img", media_type="image/png", detail="low"),
            Media.document(b"%PDF", filename="r.pdf"),
            Media.audio(b"RIFF", media_type="audio/wav"),
        ])
        assert [p["type"] for p in parts] == ["text", "image_url", "file", "input_audio"]
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert parts[1]["image_url"]["detail"] == "low"
        assert parts[2]["file"]["filename"] == "r.pdf"
        assert parts[3]["input_audio"]["format"] == "wav"

    def test_anthropic_shapes_accept_openai_native_parts(self):
        blocks = content_for_anthropic([
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
            {"type": "image_url", "image_url": {"url": "https://x.test/c.jpg"}},
            Media.document(b"%PDF", filename="r.pdf"),
        ])
        assert blocks[0]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}
        assert blocks[1]["source"] == {"type": "url", "url": "https://x.test/c.jpg"}
        assert blocks[2]["title"] == "r.pdf" and "filename" not in blocks[2]

    def test_anthropic_strips_framework_only_fields(self):
        block = content_for_anthropic([Media.image(b"i", media_type="image/png", detail="high")])[0]
        assert "detail" not in block

    def test_audio_to_claude_is_a_clear_error(self):
        with pytest.raises(ValueError, match="does not accept audio"):
            content_for_anthropic([Media.audio(b"RIFF", media_type="audio/wav")])

    def test_document_url_to_gpt_is_a_clear_error(self):
        with pytest.raises(ValueError, match="cannot fetch a document"):
            content_for_openai([Media.document("https://x.test/r.pdf")])

    def test_split_keeps_base64_out_of_text(self):
        text, media = split_text_and_media([
            {"type": "text", "text": "read this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ])
        assert text == "read this" and len(media) == 1


class TestRunnerPlumbing:
    def test_gpt_runner_media_kwarg(self, png):
        g, calls = fake_gpt()
        r = AgentRunner(model=g, agent=AgentType.ReAct, tools=[],
                        use_function_calling=False, verbose=False)
        assert r.invoke("What is this?", media=[png]).content == "a red square"
        content = last_user(calls[-1]["messages"])
        assert content[0] == {"type": "text", "text": "What is this?"}
        assert content[1]["type"] == "image_url"
        assert "base64" not in calls[-1]["messages"][0]["content"]

    def test_claude_runner_media_kwarg(self, png):
        c, calls = fake_claude()
        r = AgentRunner(model=c, agent=AgentType.ReAct, tools=[],
                        use_function_calling=False, verbose=False)
        r.invoke("Describe", media=[Media.image(png)])
        assert [b["type"] for b in calls[-1]["messages"][-1]["content"]] == ["text", "image"]

    def test_message_list_media_not_stringified_into_prompt(self):
        g, calls = fake_gpt()
        r = AgentRunner(model=g, agent=AgentType.ReAct, tools=[],
                        use_function_calling=False, verbose=False)
        r.invoke([{"role": "user", "content": [
            {"type": "text", "text": "Read this"},
            {"type": "image_url", "image_url": {"url": "https://x.test/cat.jpg"}},
        ]}])
        payload = calls[-1]["messages"]
        assert "x.test" not in payload[0]["content"]
        assert last_user(payload)[1]["image_url"]["url"] == "https://x.test/cat.jpg"

    def test_image_in_chat_history_replayed_as_image(self, png):
        g, calls = fake_gpt()
        r = AgentRunner(model=g, agent=AgentType.ReAct, tools=[],
                        use_function_calling=False, verbose=False)
        r.invoke("and now?", chat_history=[
            {"role": "user", "content": [{"type": "text", "text": "earlier"}, Media.image(png)]},
            {"role": "assistant", "content": "noted"},
        ])
        first_user = [m for m in calls[-1]["messages"] if m.get("role") == "user"][0]["content"]
        assert any(p.get("type") == "image_url" for p in first_user)

    def test_text_only_turn_is_still_a_plain_string(self):
        g, calls = fake_gpt()
        AgentRunner(model=g, agent=AgentType.ReAct, tools=[],
                    use_function_calling=False, verbose=False).invoke("plain")
        assert last_user(calls[-1]["messages"]) == "plain"

    def test_history_with_media_is_json_serialisable(self, png):
        g, _ = fake_gpt()
        comp = AgentRunner(model=g, agent=AgentType.ReAct, tools=[],
                           use_function_calling=False, verbose=False).invoke("x", media=[png])
        json.dumps(comp.history)
