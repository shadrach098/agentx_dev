"""Text-like files, spreadsheets, size limits, and refused file types."""

import base64
import json
import sys
import types

import pytest

from agentx_dev import AgentRunner, AgentType, Claude, GPT, Media
from agentx_dev.Media import (
    content_for_anthropic, content_for_openai, content_for_openai_responses, to_media_part,
)

CSV = "zip,region,total\n02134,North,120\n10001,South,95\n"


@pytest.fixture
def files(tmp_path):
    (tmp_path / "sales.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("plain notes", encoding="utf-8")
    (tmp_path / "config.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "readme.md").write_text("# Title", encoding="utf-8")
    (tmp_path / "data.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    (tmp_path / "euro.csv").write_bytes("name,city\nRené,Zürich\n".encode("cp1252"))
    (tmp_path / "report.docx").write_bytes(b"PK\x03\x04 fake")
    (tmp_path / "deck.pptx").write_bytes(b"PK\x03\x04 fake")
    (tmp_path / "bundle.zip").write_bytes(b"PK\x03\x04 fake")
    return tmp_path


def text_of(part):
    return part.get("text") or ""


class TestTextFiles:
    def test_csv_is_kept_as_text_exactly(self, files):
        m = Media.from_path(files / "sales.csv")
        part = m.to_part()
        assert part["type"] == "document" and part["filename"] == "sales.csv"
        assert part["source"] == {"type": "text", "media_type": "text/csv", "data": CSV}
        assert "02134" in part["source"]["data"], "leading zeros must survive"

    @pytest.mark.parametrize("name,mt", [("notes.txt", "text/plain"), ("config.json", "application/json"),
                                         ("readme.md", "text/markdown"), ("data.tsv", "text/tab-separated-values")])
    def test_text_like_types(self, files, name, mt):
        assert Media.from_path(files / name).to_part()["source"]["media_type"] == mt

    def test_windows_cp1252_csv_decodes(self, files):
        assert "René" in Media.text(files / "euro.csv").body

    def test_every_provider_gets_a_labelled_text_block(self, files):
        part = to_media_part(str(files / "sales.csv"))
        oa = content_for_openai([part])[0]
        rs = content_for_openai_responses([part])[0]
        an = content_for_anthropic([part])[0]
        assert oa["type"] == "text" and rs["type"] == "input_text" and an["type"] == "text"
        for block in (oa["text"], rs["text"], an["text"]):
            assert block.startswith('<file name="sales.csv" type="text/csv">')
            assert CSV in block and block.endswith("</file>")

    def test_txt_to_claude_is_a_text_block_not_a_document(self, files):
        """3.4.0 sent .txt as a base64 document, which Claude only allows for PDF."""
        block = content_for_anthropic([Media.from_path(files / "notes.txt")])[0]
        assert block["type"] == "text" and "plain notes" in block["text"]

    def test_old_base64_text_parts_in_history_are_decoded(self):
        legacy = {"type": "document", "filename": "notes.txt",
                  "source": {"type": "base64", "media_type": "text/plain",
                             "data": base64.b64encode(b"from 3.4.0").decode()}}
        assert "from 3.4.0" in content_for_anthropic([legacy])[0]["text"]
        assert "from 3.4.0" in content_for_openai([legacy])[0]["text"]


class TestSizeLimit:
    def test_over_the_cap_raises_with_the_fix(self, files):
        with pytest.raises(ValueError, match="truncate=True"):
            Media.text(files / "sales.csv", max_chars=20)

    def test_truncate_keeps_whole_lines_and_says_so(self, files):
        body = Media.text(files / "sales.csv", max_chars=40, truncate=True).body
        kept, note = body.rsplit("\n", 1)
        assert kept == "zip,region,total\n02134,North,120"
        assert note == "[... truncated: showing the first 2 of 4 lines of sales.csv]"

    def test_no_cap(self, files):
        assert Media.text(files / "sales.csv", max_chars=None).body == CSV

    def test_default_cap_applies_to_media_kwarg_paths(self, tmp_path):
        big = tmp_path / "big.csv"
        big.write_text("x\n" * 60_000, encoding="utf-8")
        with pytest.raises(ValueError, match="over max_chars=100,000"):
            to_media_part(str(big))


class TestSpreadsheets:
    def _fake_pandas(self, monkeypatch, frames, seen):
        pd = pytest.importorskip("pandas")

        def read_excel(src, sheet_name=None, dtype=None):
            seen.update(sheet_name=sheet_name, dtype=dtype)
            if sheet_name is None:
                return {k: pd.DataFrame(v) for k, v in frames.items()}
            return pd.DataFrame(frames[sheet_name])

        monkeypatch.setattr(pd, "read_excel", read_excel)

    def test_each_sheet_becomes_a_csv_block(self, tmp_path, monkeypatch):
        seen = {}
        self._fake_pandas(monkeypatch, {
            "Q1": {"zip": ["02134", "10001"], "total": [120, 95]},
            "Q2": {"zip": ["60601"], "total": [80]},
        }, seen)
        path = tmp_path / "sales.xlsx"
        path.write_bytes(b"PK\x03\x04 stub")
        m = Media.from_path(path)
        assert m.media_type == "text/csv" and m.filename == "sales.xlsx"
        assert m.body == ("## Sheet: Q1 (2 rows x 2 columns)\nzip,total\n02134,120\n10001,95\n\n"
                          "## Sheet: Q2 (1 rows x 2 columns)\nzip,total\n60601,80")
        assert seen["dtype"] is object, "must not let pandas re-infer types"

    def test_pick_one_sheet(self, tmp_path, monkeypatch):
        seen = {}
        self._fake_pandas(monkeypatch, {"Q1": {"a": [1]}, "Q2": {"b": [2]}}, seen)
        path = tmp_path / "sales.xlsx"
        path.write_bytes(b"PK")
        body = Media.spreadsheet(path, sheets="Q2").body
        assert seen["sheet_name"] == "Q2" and body.startswith("## Sheet: Q2")

    def test_missing_pandas_names_the_install(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "pandas", None)
        path = tmp_path / "sales.xlsx"
        path.write_bytes(b"PK")
        with pytest.raises(ImportError, match=r"agentx-dev\[excel\]"):
            Media.from_path(path)

    def test_real_xlsx_round_trip(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        pytest.importorskip("pandas")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Sales"
        ws.append(["zip", "region", "total"])
        ws.append(["02134", "North", 120])
        path = tmp_path / "sales.xlsx"
        wb.save(path)
        body = Media.from_path(path).body
        assert body.startswith("## Sheet: Sales (1 rows x 3 columns)")
        assert "02134,North,120" in body


class TestRefused:
    @pytest.mark.parametrize("name,msg", [("report.docx", "Word files"),
                                          ("deck.pptx", "PowerPoint files"),
                                          ("bundle.zip", "Archives")])
    def test_unsupported_files_fail_before_sending(self, files, name, msg):
        with pytest.raises(ValueError, match=msg):
            Media.from_path(files / name)

    def test_non_pdf_bytes_as_document(self):
        with pytest.raises(ValueError, match="Word files"):
            Media.document(b"PK", media_type="application/vnd.openxmlformats-officedocument."
                                              "wordprocessingml.document")

    def test_csv_by_url(self):
        with pytest.raises(ValueError, match="Only PDFs can be passed by URL"):
            Media.from_path("https://example.com/sales.csv")

    def test_raw_spreadsheet_part_is_refused_by_translators(self):
        legacy = {"type": "document", "source": {"type": "base64", "data": "UEs=",
                  "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}}
        for translate in (content_for_openai, content_for_anthropic, content_for_openai_responses):
            with pytest.raises(ValueError, match="Media.spreadsheet"):
                translate([legacy])

    def test_wrong_kind(self, files):
        with pytest.raises(ValueError, match="isn't an image type"):
            Media.image(files / "sales.csv")


class TestThroughTheStack:
    def _gpt(self):
        g = GPT(api_key="x", model="gpt-4o")
        calls = []

        def create(**k):
            calls.append(k)
            msg = types.SimpleNamespace(content=json.dumps(
                {"Thought": "t", "action": "Final_Answer", "action_input": "North leads"}), tool_calls=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)],
                                         usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))

        g.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))
        return g, calls

    def test_runner_media_csv(self, files):
        g, calls = self._gpt()
        r = AgentRunner(model=g, agent=AgentType.ReAct, tools=[], use_function_calling=False, verbose=False)
        out = r.invoke("Which region leads?", media=[str(files / "sales.csv")])
        assert out.content == "North leads"
        user = [m for m in calls[-1]["messages"] if m["role"] == "user"][-1]["content"]
        assert user[0] == {"type": "text", "text": "Which region leads?"}
        assert user[1]["type"] == "text" and "<file name=\"sales.csv\"" in user[1]["text"]
        json.dumps(out.history)

    def test_llm_invoke_media_csv_claude(self, files):
        c = Claude(api_key="x")
        seen = []
        c.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **k: (
            seen.append(k) or types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="ok")],
                usage=types.SimpleNamespace(input_tokens=1, output_tokens=1)))))
        c.invoke("Summarise", media=[str(files / "sales.csv")])
        blocks = seen[-1]["messages"][-1]["content"]
        assert [b["type"] for b in blocks] == ["text", "text"]
        assert CSV in blocks[1]["text"]
