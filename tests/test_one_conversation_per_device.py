"""Remote use runs one conversation per device in one process: what they share on disk and on this computer must
take several writers at once."""

import threading

from agentx_dev.cli import mcp, memory, sessions
from agentx_dev.cli.team_record import TeamRecord


def _together(work, count=8):
    threads = [threading.Thread(target=work, args=(n,)) for n in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)


def test_titles_written_at_once_are_all_kept(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "project"
    cwd.mkdir()
    _together(lambda n: sessions.save_title(home, cwd, f"2026-11-07-1200{n:02d}-abc.json", f"Title {n}"))
    assert len(sessions.read_titles(home, cwd)) == 8


def test_work_log_entries_written_at_once_are_all_kept(tmp_path):
    _together(lambda n: memory.append_entry(tmp_path, f"## entry {n}\nbody {n}"))
    text = (tmp_path / ".agentx" / "log.md").read_text(encoding="utf-8")
    assert all(f"## entry {n}\nbody {n}" in text for n in range(8))


def test_two_team_jobs_in_one_second_get_their_own_record(tmp_path):
    from datetime import datetime
    when = datetime(2026, 11, 7, 12, 0, 0)
    made = []
    _together(lambda n: made.append(TeamRecord(tmp_path, "same task", when=when).folder), count=4)
    assert None not in made and len(set(made)) == 4


def test_one_mcp_sign_in_at_a_time():
    port = mcp._CallbackPort()
    assert port.take() is True
    assert port.take() is False                    # a second sign-in waits for the first
    port.free()
    assert port.take() is True
