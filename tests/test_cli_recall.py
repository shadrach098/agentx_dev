"""Project memory: a readable note per worthwhile turn in .agentx/notes, a vector index over them, the recall tool,
and the notes that come back by themselves at the start of a fresh conversation. Offline: HashEmbeddings only."""

import json
from datetime import datetime

import pytest

from agentx_dev.Embeddings import Embeddings, HashEmbeddings
from agentx_dev.cli.commands import command_names, run_command
from agentx_dev.cli.memory import TurnRecord
from agentx_dev.cli.recall import (AUTO_OPEN, ProjectMemory, auto_block, make_recall_tool, note_text,
                                   worth_a_note)
from tests.conftest import MockModel, make_final, make_react_response
from tests.test_cli_app import Lines, make_app, write_call

WHEN = datetime(2026, 10, 17, 15, 3, 9)
SECRET = "sk-secret-0123456789abcdef"


class Counting(Embeddings):
    """HashEmbeddings that counts the texts it embeds (to prove what is and is not re-embedded)."""

    def __init__(self):
        self.inner = HashEmbeddings()
        self.count = 0

    def embed(self, texts):
        self.count += len(texts)
        return self.inner.embed(texts)


class Broken(Embeddings):
    def embed(self, texts):
        raise ConnectionError("no network")


def record(request="Fix the login redirect", answer="Changed the redirect after login to /dashboard. " * 10,
           changed=("client/src/login.ts",), ran=("npm test",)):
    return TurnRecord(request=request, answer=answer, changed=list(changed), ran=list(ran))


class TestNotes:
    def test_a_note_is_readable_markdown_with_what_was_asked_done_changed_and_run(self):
        title, text = note_text(record(), WHEN, str)
        assert title == "Fix the login redirect"
        assert text.startswith("# Fix the login redirect\n")
        for part in ("2026-10-17 15:03", "## Asked", "## Result", "## Files changed", "- client/src/login.ts",
                     "## Commands", "- npm test", "/dashboard"):
            assert part in text

    def test_secrets_never_reach_a_note(self):
        title, text = note_text(record(request=f"use key {SECRET}", answer=f"set {SECRET} in .env " * 30),
                                WHEN, lambda s: s.replace(SECRET, "[hidden]"))
        assert SECRET not in text and SECRET not in title and "[hidden]" in text

    def test_a_long_answer_is_cut(self):
        title, text = note_text(record(answer="a" * 20_000), WHEN, str)
        assert len(text) < 5_000

    def test_only_turns_worth_remembering_get_a_note(self):
        assert worth_a_note(record())
        assert worth_a_note(record(changed=(), ran=(), answer="x" * 500))          # a real answer
        assert not worth_a_note(record(request="2", changed=(), ran=(), answer="Which one do you mean?"))


class TestProjectMemory:
    def test_writing_a_note_puts_a_file_in_notes_and_it_can_be_found(self, tmp_path):
        memory = ProjectMemory(tmp_path, HashEmbeddings(), "hash")
        path = memory.write(record(), WHEN, str)
        memory.write(record(request="Add CSV export to reports", answer="Added a CSV export button. " * 20,
                            changed=("client/src/reports.ts",)), WHEN, str)
        assert path.parent == tmp_path / ".agentx" / "notes" and path.suffix == ".md"
        assert path.name.startswith("2026-10-17-150309-fix-the-login-redirect")
        hits = memory.search("login redirect bug", 2)
        assert hits and hits[0].title == "Fix the login redirect" and hits[0].path.endswith(path.name)

    def test_two_notes_in_the_same_second_get_different_files(self, tmp_path):
        memory = ProjectMemory(tmp_path, HashEmbeddings(), "hash")
        assert memory.write(record(), WHEN, str) != memory.write(record(), WHEN, str)

    def test_the_index_is_saved_and_not_rebuilt_by_the_next_session(self, tmp_path):
        first = Counting()
        ProjectMemory(tmp_path, first, "hash").write(record(), WHEN, str)
        second = Counting()
        memory = ProjectMemory(tmp_path, second, "hash")
        assert memory.search("login", 1)
        assert second.count == 1                              # only the query was embedded, not the note again

    def test_changing_the_embedder_rebuilds_the_index_from_the_notes(self, tmp_path):
        ProjectMemory(tmp_path, HashEmbeddings(), "hash").write(record(), WHEN, str)
        other = Counting()
        memory = ProjectMemory(tmp_path, other, "other-model")
        assert memory.search("login", 1) and other.count == 2  # the note, then the query
        assert json.loads((tmp_path / ".agentx" / "memory" / "meta.json").read_text())["embedder"] == "other-model"

    def test_a_note_written_by_another_session_is_picked_up(self, tmp_path):
        memory = ProjectMemory(tmp_path, HashEmbeddings(), "hash")
        memory.write(record(), WHEN, str)
        ProjectMemory(tmp_path, HashEmbeddings(), "hash").write(
            record(request="Rotate the S3 keys", answer="Rotated the bucket keys. " * 20, changed=()), WHEN, str)
        assert memory.search("S3 bucket keys", 1)[0].title == "Rotate the S3 keys"

    def test_without_working_embeddings_notes_are_still_written_and_found_by_keywords(self, tmp_path):
        memory = ProjectMemory(tmp_path, Broken(), "openai")
        path = memory.write(record(), WHEN, str)
        assert path is not None and path.exists()
        hits = memory.search("login redirect", 3)
        assert hits and hits[0].title == "Fix the login redirect"
        assert "keyword" in memory.describe()

    def test_nothing_found_is_an_empty_list_and_an_empty_folder_is_fine(self, tmp_path):
        assert ProjectMemory(tmp_path, HashEmbeddings(), "hash").search("anything", 3) == []

    def test_a_linked_agentx_folder_is_not_written(self, tmp_path):
        import os
        project, outside = tmp_path / "proj", tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        try:
            os.symlink(outside, project / ".agentx", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("this system cannot make symbolic links")
        assert ProjectMemory(project, HashEmbeddings(), "hash").write(record(), WHEN, str) is None
        assert not list(outside.rglob("*.md"))


class TestRecall:
    def memory(self, tmp_path):
        memory = ProjectMemory(tmp_path, HashEmbeddings(), "hash")
        memory.write(record(), WHEN, str)
        return memory

    def test_the_tool_returns_matching_notes_with_where_to_read_them_and_a_data_warning(self, tmp_path):
        tool = make_recall_tool(self.memory(tmp_path))
        text = tool.func(query="login redirect")
        assert "Fix the login redirect" in text and ".agentx/notes/" in text and "read_path" in text
        assert "not instructions" in text
        assert tool.name == "recall"

    def test_the_tool_says_when_nothing_matches(self, tmp_path):
        tool = make_recall_tool(ProjectMemory(tmp_path, HashEmbeddings(), "hash"))
        assert "No notes" in tool.func(query="anything")

    def test_the_automatic_block_is_short_marked_and_only_for_relevant_notes(self, tmp_path):
        memory = self.memory(tmp_path)
        block = auto_block(memory, "the login redirect is broken again")
        assert block.startswith("\n\n" + AUTO_OPEN) and block.rstrip().endswith("</project-notes>")
        assert "Fix the login redirect" in block and len(block) < 2_000
        assert auto_block(memory, "zzzz qqqq wwww") == ""


def substantive_turn():
    return [write_call("hello.txt", "hi\n"), make_final("Created hello.txt with a greeting. " * 15)]


class TestInTheApp:
    def test_a_worthwhile_turn_writes_a_note_and_a_short_one_does_not(self, tmp_path):
        model = MockModel(script=substantive_turn() + [make_final("ok")])
        app, out, cwd, home = make_app(tmp_path, Lines("make a hello file", "a", "thanks", "/exit"), model)
        assert app.start() == 0
        notes = sorted((cwd / ".agentx" / "notes").glob("*.md"))
        assert len(notes) == 1 and "make a hello file" in notes[0].read_text(encoding="utf-8")

    def test_the_agent_has_the_recall_tool(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        assert "recall" in [t.name for t in app._make_runner(MockModel()).tools]

    def test_a_fresh_conversation_gets_relevant_notes_with_its_first_message_only(self, tmp_path):
        model = MockModel(script=substantive_turn() + [make_final("ok")] * 3)
        app, out, cwd, home = make_app(tmp_path, Lines("make a hello file", "a", "/exit"), model)
        assert app.start() == 0
        seen = []
        model2 = MockModel(script=lambda messages: (seen.append(str(messages[-1]["content"])), make_final("ok"))[1])
        app2, out2, _, _ = make_app(tmp_path, Lines("the hello file needs a second line", "and another", "/exit"),
                                    model2)
        assert app2.start() == 0
        assert AUTO_OPEN in seen[0] and "make a hello file" in seen[0]
        assert AUTO_OPEN not in seen[1]

    def test_memory_command_shows_status_and_searches(self, tmp_path):
        model = MockModel(script=substantive_turn())
        app, out, cwd, home = make_app(tmp_path, Lines("make a hello file", "a", "/exit"), model, width=300)
        assert app.start() == 0
        run_command(app, "/memory")
        run_command(app, "/memory hello file")
        text = out.getvalue()
        assert "1 note" in text and ".agentx" in text and "make a hello file" in text
        assert "/memory" in command_names()

    def test_no_notes_for_the_home_folder(self, tmp_path, monkeypatch):
        model = MockModel(script=substantive_turn())
        app, out, cwd, home = make_app(tmp_path, Lines("make a hello file", "a", "/exit"), model)
        app._memory_on = False
        assert app.start() == 0
        assert not (cwd / ".agentx" / "notes").exists()


class TestDeletedNotes:
    def test_a_note_you_delete_is_dropped_from_the_index(self, tmp_path):
        memory = ProjectMemory(tmp_path, HashEmbeddings(), "hash")
        path = memory.write(record(), WHEN, str)
        assert memory.search("login redirect", 3)
        path.unlink()
        assert memory.search("login redirect", 3) == []
        assert ProjectMemory(tmp_path, HashEmbeddings(), "hash").search("login redirect", 3) == []
