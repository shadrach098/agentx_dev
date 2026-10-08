"""examples/subagents_demo.py: --memory parsing and saving the store even when the run crashes."""

import importlib.util
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent.parent / "examples" / "subagents_demo.py"


@pytest.fixture(scope="module")
def demo():
    if not DEMO.exists():
        pytest.skip("examples/subagents_demo.py is not available")
    spec = importlib.util.spec_from_file_location("subagents_demo_under_test", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # defines functions only; main() runs under __main__
    return module


class TestParseArgs:
    def test_memory_with_a_space(self, demo):
        assert demo.parse_args(["--ask", "--memory", "m.json", "my task"]) == ({"--ask"}, "m.json", ["my task"])

    def test_memory_with_an_equals_sign(self, demo):
        assert demo.parse_args(["--memory=m.json", "--persistent"]) == ({"--persistent"}, "m.json", [])

    def test_plain_runs_are_unchanged(self, demo):
        assert demo.parse_args([]) == (set(), None, [])
        assert demo.parse_args(["--ask", "--persistent", "task"]) == ({"--ask", "--persistent"}, None, ["task"])

    @pytest.mark.parametrize("argv", [["--memory"], ["--memory="], ["--memory", "--ask"]])
    def test_a_missing_file_name_is_an_error(self, demo, argv):
        with pytest.raises(SystemExit):
            demo.parse_args(argv)


class TestSaveOnCrash:
    def test_the_store_is_saved_when_the_run_raises(self, demo, monkeypatch, tmp_path):
        target = tmp_path / "memory.json"
        saved = []

        class Store:
            def save(self, path):
                saved.append(path)

        class Boom:
            agents = {}

            def run(self, task):
                raise KeyboardInterrupt

        monkeypatch.setattr(demo, "seed_workspace", lambda: None)
        monkeypatch.setattr(demo, "build_model", lambda: object())
        monkeypatch.setattr(demo, "load_memory", lambda path: Store())
        monkeypatch.setattr(demo, "build_supervisor", lambda *a, **k: Boom())
        with pytest.raises(KeyboardInterrupt):
            demo.main(["--memory=" + str(target), "task"])
        assert saved == [str(target)]

    def test_no_memory_file_means_no_save(self, demo, monkeypatch):
        class Boom:
            agents = {}

            def run(self, task):
                raise RuntimeError("boom")

        monkeypatch.setattr(demo, "seed_workspace", lambda: None)
        monkeypatch.setattr(demo, "build_model", lambda: object())
        monkeypatch.setattr(demo, "load_memory", lambda path: pytest.fail("no memory file was given"))
        monkeypatch.setattr(demo, "build_supervisor", lambda *a, **k: Boom())
        with pytest.raises(RuntimeError):
            demo.main(["task"])
