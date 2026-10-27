"""The benchmark harness, with no real model: every task's check passes a correct reference solution and fails the
untouched repo (or a wrong answer), and a whole run through agentx records its measurements."""

import json
import re
import shutil

import pytest

from bench.__main__ import main as bench_main, table
from bench.runner import run_task
from bench.tasks import BY_ID, REPOS, TASKS, Workspace, snapshot
from tests.conftest import MockModel, make_final, make_react_response


def copy(tmp_path, repo):
    target = tmp_path / repo
    shutil.copytree(REPOS / repo, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return target, snapshot(target)


def edit(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")


RESTORE_LINE = next(n for n, line in enumerate((REPOS / "shop" / "shop" / "orders.py").read_text().splitlines(), 1)
                    if "inventory.restore" in line)


def fix_rounding(repo):
    edit(repo / "shop" / "pricing.py", "    discounted = amount_cents * (100 - percent) / 100\n    return int(discounted)",
         "    return (amount_cents * (100 - percent) + 50) // 100")


def fix_double_cancel(repo):
    edit(repo / "shop" / "orders.py", "    def cancel(self):\n", "    def cancel(self):\n        if self.status == "
         "\"cancelled\":\n            return\n")
    with open(repo / "tests" / "test_orders.py", "a", encoding="utf-8") as f:
        f.write("\n\ndef test_cancel_twice():\n    order = make_order()\n    order.add_item('mug', 2, 1200)\n"
                "    order.cancel()\n    order.cancel()\n    assert order.inventory.available('mug') == 5\n")


def add_validation(repo):
    edit(repo / "shop" / "orders.py", "    def add_item(self, sku, quantity, unit_price_cents):\n",
         "    def add_item(self, sku, quantity, unit_price_cents):\n        if quantity < 1:\n"
         "            raise ValueError('quantity must be at least 1')\n")


def add_word_count(repo):
    with open(repo / "notes" / "text.py", "a", encoding="utf-8") as f:
        f.write("\n\ndef word_count(text):\n    return len(re.findall(r\"[A-Za-z0-9']+\", text))\n")


def rename(repo):
    for path in repo.rglob("*.py"):
        path.write_text(path.read_text(encoding="utf-8").replace("slugify", "make_slug"), encoding="utf-8")


def add_hints(repo):
    text = repo / "notes" / "text.py"
    edit(text, "def slugify(title):", "def slugify(title: str) -> str:")
    edit(text, "def first_line(text):", "def first_line(text: str) -> str:")
    edit(text, "def shorten(text, limit=40):", "def shorten(text: str, limit: int = 40) -> str:")
    store = repo / "notes" / "store.py"
    edit(store, "def __init__(self):", "def __init__(self) -> None:")
    edit(store, "def add(self, text):", "def add(self, text: str) -> str:")
    edit(store, "def get(self, note_id):", "def get(self, note_id: str) -> str:")
    edit(store, "def titles(self):", "def titles(self) -> dict:")
    edit(repo / "notes" / "export.py", "def index(store):", "def index(store: \"NoteStore\") -> str:")


GOOD = {
    "find-code": (None, f"It is in shop/orders.py, line {RESTORE_LINE}, inside cancel()."),
    "explain-failure": (None, "apply_discount truncates with int() instead of rounding, so 850.85 becomes 850."),
    "fix-failing-test": (fix_rounding, "Fixed."),
    "fix-bug-add-test": (fix_double_cancel, "Fixed."),
    "add-validation": (add_validation, "Done."),
    "add-feature": (add_word_count, "Done."),
    "rename-everywhere": (rename, "Renamed."),
    "audit": (None, "1. orders.py:28 cancel() restores stock again when called twice. 2. add_item accepts a negative "
                    "quantity. 3. pricing.py:7 truncates instead of rounding."),
    "team-job": (add_hints, "Done."),
    "run-and-report": (None, "test_total_with_discount fails: apply_discount truncates 850.85 to 850 instead of "
                             "rounding to 851."),
}


def test_there_are_ten_tasks_on_two_repos_each_with_a_reference():
    assert len(TASKS) == 10 and {task.repo for task in TASKS} == {"shop", "notes"}
    assert set(GOOD) == set(BY_ID)


@pytest.mark.parametrize("task_id", sorted(GOOD))
def test_a_correct_solution_passes(tmp_path, task_id):
    task = BY_ID[task_id]
    repo, before = copy(tmp_path, task.repo)
    change, answer = GOOD[task_id]
    if change:
        change(repo)
    passed, note = task.check(Workspace(repo, before, answer))
    assert passed, note


@pytest.mark.parametrize("task_id", sorted(GOOD))
def test_doing_nothing_fails(tmp_path, task_id):
    task = BY_ID[task_id]
    repo, before = copy(tmp_path, task.repo)
    passed, note = task.check(Workspace(repo, before, "I could not do it."))
    assert not passed, note


def test_a_read_only_task_fails_when_files_were_changed(tmp_path):
    repo, before = copy(tmp_path, "shop")
    fix_rounding(repo)
    passed, note = BY_ID["find-code"].check(Workspace(repo, before, f"orders.py line {RESTORE_LINE}"))
    assert not passed and "changed files" in note


def test_changing_the_test_instead_of_the_code_fails(tmp_path):
    repo, before = copy(tmp_path, "shop")
    edit(repo / "tests" / "test_orders.py", "== 851", "== 850")
    passed, note = BY_ID["fix-failing-test"].check(Workspace(repo, before, "Fixed."))
    assert not passed and "changed the test" in note


def test_a_whole_run_through_agentx_is_measured_and_logged(tmp_path):
    model = MockModel(script=[make_react_response("read_path", {"path": "shop/orders.py"}),
                              make_final(f"shop/orders.py, line {RESTORE_LINE}.")])
    result = run_task(BY_ID["find-code"], "mock", log_dir=tmp_path / "logs",
                      model_factory=lambda name, env: model, env={})
    assert result.passed, result.note
    assert result.requests == 2 and result.tokens_in > 0 and result.tool_calls == 1 and not result.error
    log = (tmp_path / "logs" / "find-code.log").read_text(encoding="utf-8")
    assert "result: PASS" in log and "read_path" in log


def test_a_failed_run_says_why_and_keeps_the_diff(tmp_path):
    model = MockModel(script=[make_react_response("write_file", {"path": "extra.txt", "content": "x",
                                                                 "if_exists": "refuse"}),
                              make_final("orders.py line 1")])
    result = run_task(BY_ID["find-code"], "mock", log_dir=tmp_path / "logs",
                      model_factory=lambda name, env: model, env={})
    assert not result.passed
    log = (tmp_path / "logs" / "find-code.log").read_text(encoding="utf-8")
    assert "result: FAIL" in log and "b/extra.txt" in log


def test_the_table_and_compare(tmp_path, capsys):
    row = {"task": "find-code", "passed": True, "note": "ok", "seconds": 12.0, "requests": 3, "tokens_in": 30_000,
           "tokens_out": 1_000, "cached": 24_000, "counted": 9_400, "tool_calls": 2, "team_tool_uses": 0}
    text = table([row])
    assert "find-code" in text and "PASS" in text and "80%" in text and "1/1 passed" in text
    for name, counted in (("a.json", 9_400), ("b.json", 4_700)):
        (tmp_path / name).write_text(json.dumps({"model": "m", "started": "s", "commit": "c",
                                                 "tasks": [{**row, "counted": counted}]}), encoding="utf-8")
    assert bench_main(["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 0
    assert re.search(r"find-code\s+1/1 -> 1/1\s+9k -> 5k\s+-50%", capsys.readouterr().out)


def test_run_asks_before_spending_and_a_no_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert bench_main(["run", "--model", "gpt-x", "--task", "find-code"]) == 1
    assert "Nothing was run." in capsys.readouterr().out


def test_list(capsys):
    assert bench_main(["list"]) == 0
    assert "team-job" in capsys.readouterr().out
