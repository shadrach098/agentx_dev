"""Pictures in tool results: read_path on a PNG / JPEG / GIF / WebP file hands the model the picture itself.

Before, read_path answered "ERROR: file is not utf-8 text" for a screenshot, and every layer turned a tool result
into a string, so a helper that took a Playwright screenshot could not look at it."""

import asyncio
import base64
import json
import struct
import zlib

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, DefaultTools, Permissions
from agentx_dev import Pictures
from agentx_dev.ChatModel import _messages_for_anthropic, _messages_for_openai, _messages_for_openai_responses
from agentx_dev.Pictures import ToolContent, content_text, has_pictures, picture_result, stub_old_pictures
from agentx_dev.cli.compact import _text as compact_text
from agentx_dev.cli.ui_text import summarize_result
from tests.conftest import MockModel, make_final, make_react_response


def png_bytes(width=3, height=2):
    """A real, tiny PNG (no Pillow needed)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def read_tool(folder):
    return next(t for t in DefaultTools.build(Permissions.read_only([str(folder)])) if t.name == "read_path")


def image_parts(content):
    return [p for p in content if isinstance(p, dict) and p.get("type") in ("image", "image_url", "input_image")] \
        if isinstance(content, list) else []


class TestReadPathOpensPictures:
    def test_a_png_comes_back_as_the_picture(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png_bytes(3, 2))
        result = read_tool(tmp_path).func(path=str(tmp_path / "shot.png"))
        assert isinstance(result, ToolContent) and has_pictures(result)
        image = image_parts(result)[0]
        assert image["source"]["media_type"] == "image/png"
        assert base64.b64decode(image["source"]["data"]) == png_bytes(3, 2)
        assert image["filename"] == "shot.png"

    def test_it_prints_as_one_short_line_without_the_data(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png_bytes(1440, 2))
        result = read_tool(tmp_path).func(path=str(tmp_path / "shot.png"))
        text = str(result)
        assert text.startswith("Picture ") and "PNG, 1440x2" in text
        assert "\n" not in text and len(text) < 300
        assert f"{result}" == text and repr(result) == text

    def test_the_type_comes_from_the_contents_not_the_name(self, tmp_path):
        (tmp_path / "shot.jpg").write_bytes(png_bytes())          # really a PNG
        result = read_tool(tmp_path).func(path=str(tmp_path / "shot.jpg"))
        assert image_parts(result)[0]["source"]["media_type"] == "image/png"

    def test_a_text_file_named_png_is_refused(self, tmp_path):
        (tmp_path / "fake.png").write_text("not a picture", encoding="utf-8")
        result = read_tool(tmp_path).func(path=str(tmp_path / "fake.png"))
        assert isinstance(result, str) and result.startswith("ERROR:") and "not a PNG" in result

    def test_text_files_read_as_before(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert read_tool(tmp_path).func(path=str(tmp_path / "a.py")) == "x = 1\n"

    def test_other_binary_files_say_what_read_path_opens(self, tmp_path):
        (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x81")
        result = read_tool(tmp_path).func(path=str(tmp_path / "blob.bin"))
        assert "not utf-8 text" in result and "PNG" in result

    def test_a_picture_outside_the_allowed_folders_is_still_refused(self, tmp_path):
        inside, outside = tmp_path / "in", tmp_path / "out"
        inside.mkdir(); outside.mkdir()
        (outside / "shot.png").write_bytes(png_bytes())
        with pytest.raises(Exception):
            read_tool(inside).func(path=str(outside / "shot.png"))


class TestSizes:
    @pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")])
    def test_other_formats_and_their_size(self, tmp_path, fmt, mime):
        Image = pytest.importorskip("PIL.Image")
        path = tmp_path / f"shot.{fmt.lower()}"
        Image.new("RGB", (37, 21), "white").save(path, format=fmt)
        result = picture_result(path)
        assert image_parts(result)[0]["source"]["media_type"] == mime
        assert "37x21" in str(result)

    def test_too_big_without_a_way_to_shrink_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Pictures, "MAX_PICTURE_BYTES", 50)
        monkeypatch.setattr(Pictures, "_shrink", lambda raw, media_type: None)
        (tmp_path / "big.png").write_bytes(png_bytes(40, 40))
        result = picture_result(tmp_path / "big.png")
        assert isinstance(result, str) and result.startswith("ERROR:") and "too big" in result
        assert "smaller screenshot" in result

    def test_too_many_pixels_is_made_smaller(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        Image.new("RGB", (9000, 40), "white").save(tmp_path / "wide.png")
        result = picture_result(tmp_path / "wide.png")
        assert has_pictures(result)
        assert "2576x" in str(result) and "Made smaller" in str(result) and "9000x40" in str(result)


class TestTheModelIsSentThePicture:
    def test_text_mode_observation_carries_the_picture(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png_bytes())
        model = MockModel(script=[make_react_response("read_path", {"path": str(tmp_path / "shot.png")}),
                                  make_final("a red square")])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, verbose=False, auto_cache=False,
                             permissions=Permissions.read_only([str(tmp_path)]))
        assert "red square" in runner.invoke("look at shot.png").content
        observation = [m for m in model.calls[1] if m.get("role") == "user" and isinstance(m.get("content"), list)]
        assert observation, "the picture never reached the model"
        content = observation[-1]["content"]
        assert content[0]["text"].startswith("Observation: Picture ")
        assert image_parts(content)

    def test_native_tool_message_carries_the_picture(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png_bytes())
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "read_path", "input": {"path": str(tmp_path / "shot.png")}, "id": "c1"},
            {"type": "tool_use", "name": "respond", "input": {"answer": "red"}, "id": "c2"}])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, verbose=False, auto_cache=False,
                             bind_tools_natively=True, permissions=Permissions.read_only([str(tmp_path)]))
        result = runner.invoke("look")
        assert result.content == "red"
        tool = [m for m in model.tool_calls_made[1] if m.get("role") == "tool"][-1]
        assert image_parts(tool["content"])
        assert result.tool_calls[0].result.startswith("Picture ")       # the recorded result is its text

    def test_async_runner_too(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png_bytes())
        model = MockModel(tool_script=[
            {"type": "tool_use", "name": "read_path", "input": {"path": str(tmp_path / "shot.png")}, "id": "c1"},
            {"type": "tool_use", "name": "respond", "input": {"answer": "red"}, "id": "c2"}])
        runner = AsyncAgentRunner(model=model, agent=AgentType.ReAct, verbose=False, auto_cache=False,
                                  bind_tools_natively=True, permissions=Permissions.read_only([str(tmp_path)]))
        result = asyncio.run(runner.ainvoke("look"))
        assert result.content == "red"
        tool = [m for m in model.tool_calls_made[1] if m.get("role") == "tool"][-1]
        assert image_parts(tool["content"])

    def test_the_history_can_be_saved_as_json(self, tmp_path):
        (tmp_path / "shot.png").write_bytes(png_bytes())
        result = read_tool(tmp_path).func(path=str(tmp_path / "shot.png"))
        assert json.loads(json.dumps({"content": result}))["content"][1]["type"] == "image"


def picture_turn(call_ids=("c1",)):
    picture = picture_result_from_bytes()
    return [
        {"role": "user", "content": "look"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read_path", "arguments": "{}"}} for cid in call_ids]},
        *[{"role": "tool", "name": "read_path", "tool_call_id": cid, "content": picture} for cid in call_ids],
    ]


def picture_result_from_bytes():
    return ToolContent([{"type": "text", "text": "Picture shot.png: PNG, 3x2, 1 KB."},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.b64encode(png_bytes()).decode()},
                         "filename": "shot.png"}])


class TestProviders:
    def test_claude_gets_it_inside_the_tool_result(self):
        out = _messages_for_anthropic(picture_turn())
        block = out[-1]["content"][0]
        assert block["type"] == "tool_result"
        assert block["content"][0] == {"type": "text", "text": "Picture shot.png: PNG, 3x2, 1 KB."}
        assert block["content"][1]["type"] == "image"
        assert block["content"][1]["source"]["type"] == "base64"
        assert "filename" not in block["content"][1]

    def test_gpt_gets_text_in_the_tool_message_and_the_picture_after(self):
        out = _messages_for_openai(picture_turn(("c1", "c2")) + [{"role": "user", "content": "next"}])
        roles = [m["role"] for m in out]
        assert roles == ["user", "assistant", "tool", "tool", "user", "user"]
        assert all(isinstance(m["content"], str) for m in out if m["role"] == "tool")
        pictures = out[4]["content"]
        assert [p["type"] for p in pictures] == ["text", "image_url", "image_url"]
        assert pictures[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_gpt_picture_message_at_the_end(self):
        out = _messages_for_openai(picture_turn())
        assert out[-1]["role"] == "user" and image_parts(out[-1]["content"])

    def test_responses_api(self):
        items = _messages_for_openai_responses(picture_turn())
        output = [i for i in items if i.get("type") == "function_call_output"][0]
        assert output["output"] == "Picture shot.png: PNG, 3x2, 1 KB."
        assert items[-1]["role"] == "user"
        assert [p["type"] for p in items[-1]["content"]] == ["input_text", "input_image"]

    def test_text_results_unchanged(self):
        turn = picture_turn()
        turn[-1] = {**turn[-1], "content": "plain"}
        assert _messages_for_openai(turn)[-1] == turn[-1]
        assert _messages_for_anthropic(turn)[-1]["content"][0]["content"] == "plain"


class TestOldPicturesLeftOut:
    def test_only_the_newest_three_stay(self):
        messages = []
        for i in range(5):
            picture = ToolContent([{"type": "text", "text": f"Picture s{i}.png: PNG, 3x2, 1 KB."},
                                   picture_result_from_bytes()[1]])
            messages.append({"role": "tool", "name": "read_path", "tool_call_id": f"c{i}", "content": picture})
        out = stub_old_pictures(messages)
        assert [has_pictures(m["content"]) for m in out] == [False, False, True, True, True]
        assert out[0]["content"].startswith("Picture s0.png") and "read_path" in out[0]["content"]
        assert out[0]["tool_call_id"] == "c0"
        assert has_pictures(messages[0]["content"])                  # the history itself keeps them

    def test_text_mode_stub_keeps_the_observation_frame(self):
        message = {"role": "user", "content": [{"type": "text", "text": "Observation: Picture a.png: PNG, 3x2, 1 KB."},
                                               picture_result_from_bytes()[1]]}
        out = stub_old_pictures([message], keep=0)
        assert out[0]["content"].startswith("Observation: Picture a.png")

    def test_pictures_the_user_attached_stay(self):
        message = {"role": "user", "content": [{"type": "text", "text": "what is this?"},
                                               picture_result_from_bytes()[1]]}
        assert stub_old_pictures([message], keep=0) == [message]

    def test_the_runner_sends_at_most_three(self, tmp_path):
        for i in range(5):
            (tmp_path / f"s{i}.png").write_bytes(png_bytes(i + 1, 1))
        script = [make_react_response("read_path", {"path": str(tmp_path / f"s{i}.png")}) for i in range(5)]
        model = MockModel(script=script + [make_final("done")])
        runner = AgentRunner(model=model, agent=AgentType.ReAct, verbose=False, auto_cache=False, max_iterations=8,
                             permissions=Permissions.read_only([str(tmp_path)]))
        runner.invoke("look at them all")
        last = model.calls[-1]
        assert sum(len(image_parts(m.get("content"))) for m in last) == 3


class TestScreenAndCompact:
    def test_the_tool_line_says_picture_and_its_size(self):
        assert summarize_result("read_path", "Picture shot.png: PNG, 1440x900, 245 KB.") == ("Picture, 1440x900", False)

    def test_compact_reads_only_the_text(self):
        picture = picture_result_from_bytes()
        assert compact_text(picture) == "Picture shot.png: PNG, 3x2, 1 KB."
        assert compact_text(list(picture)) == "Picture shot.png: PNG, 3x2, 1 KB."      # as loaded from a saved session

    def test_content_text_of_a_bare_picture(self):
        assert content_text([picture_result_from_bytes()[1]]) == "[picture]"
