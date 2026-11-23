"""Saving a conversation while another program has its file open (Windows: "Access is denied")."""
import os
import sys
import threading

import pytest

from agentx_dev.Session import Session

session_module = sys.modules["agentx_dev.Session"]          # the module (the package exports the class by its name)


def _session():
    return Session(history=[{"role": "user", "content": "hello"}])


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to rename over an open file")
def test_a_save_waits_out_a_reader_that_has_the_file_open_for_a_moment(tmp_path):
    target = tmp_path / "s.json"
    _session().save(target)
    reader = open(target, encoding="utf-8")                    # another window listing its conversations
    threading.Timer(0.3, reader.close).start()
    Session(history=[{"role": "user", "content": "again"}]).save(target)
    assert Session.load(target).history[0]["content"] == "again"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to rename over an open file")
def test_a_file_held_open_the_whole_time_still_fails_and_leaves_no_temp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "REPLACE_TRIES", 3)
    target = tmp_path / "s.json"
    _session().save(target)
    with open(target, encoding="utf-8"):
        with pytest.raises(PermissionError):
            Session(history=[{"role": "user", "content": "again"}]).save(target)
    assert Session.load(target).history[0]["content"] == "hello"   # the old copy is intact
    assert not list(tmp_path.glob("*.tmp"))


def test_two_threads_saving_at_once_use_their_own_temp_files(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def recording_replace(src, dst):
        seen.append(os.path.basename(src))
        return real_replace(src, dst)
    monkeypatch.setattr(session_module.os, "replace", recording_replace)
    target = tmp_path / "s.json"
    threads = [threading.Thread(target=_session().save, args=(target,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 2 and len(set(seen)) == 2
