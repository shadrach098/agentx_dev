"""The suite's own guards (tests/conftest.py): no test reaches the real home, and none leaves a log behind there."""

import logging
import os
from pathlib import Path

from tests import conftest


def test_a_test_runs_with_a_tmp_home():
    real = os.path.realpath(conftest._REAL_HOME)
    for home in (str(Path.home()), os.path.expanduser("~"), os.environ.get("USERPROFILE", ""),
                 os.environ.get("HOME", "")):
        assert home and os.path.realpath(home) != real
        assert conftest._in_real_home(os.path.join(home, ".agentx", "agentx.log")) is False


def test_a_file_log_in_the_real_home_is_found_on_any_agentx_logger():
    probe = os.path.join(conftest._REAL_HOME, ".agentx-guard-probe", "probe.log")
    child = logging.getLogger("agentx_dev.some.child")
    handler = logging.FileHandler(probe, delay=True)          # delay: the file is never opened
    child.addHandler(handler)
    try:
        found = conftest._real_home_logs(conftest._log_handlers())
    finally:
        child.removeHandler(handler)
        handler.close()
    assert os.path.realpath(probe) in [os.path.realpath(path) for path in found]
    assert not os.path.exists(os.path.dirname(probe))


def test_a_file_log_under_the_temp_folder_is_not_the_real_home(tmp_path):
    assert conftest._in_real_home(str(tmp_path / "home" / ".agentx" / "agentx.log")) is False
    assert conftest._in_real_home(os.path.join(conftest._REAL_HOME, ".agentx", "agentx.log")) is True
