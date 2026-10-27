"""The benchmark's tasks: what agentx is asked, on which sample repo, and how the result is checked.

A check gets a ``Workspace`` (the repo copy after the run, what it looked like before, and the agent's last answer)
and returns ``(passed, note)``. Checks never trust the agent's word for a code change: they run the tests, read the
files, or run a hidden test kept out of the repo until the check."""

from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPOS = HERE / "repos"
HIDDEN = HERE / "hidden"
IGNORED_PARTS = {".agentx", "__pycache__", ".pytest_cache"}


def snapshot(root: Path) -> Dict[str, str]:
    """``{relative path: sha256}`` of every file, leaving out agentx's own folder and caches."""
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not IGNORED_PARTS & set(path.relative_to(root).parts):
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@dataclass
class Workspace:
    repo: Path
    before: Dict[str, str]
    answer: str

    def changed(self) -> List[str]:
        after = snapshot(self.repo)
        return sorted(name for name in set(self.before) | set(after) if self.before.get(name) != after.get(name))

    def read(self, relative: str) -> str:
        path = self.repo / relative
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def pytest(self, *args: str, hidden: Optional[str] = None) -> Tuple[bool, str]:
        """Run the repo's tests (``args`` narrow them); ``hidden`` copies a test from bench/hidden in first."""
        if hidden:
            shutil.copy(HIDDEN / hidden, self.repo / "tests" / hidden)
        try:
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                                  cwd=self.repo, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return False, "the tests did not finish in 3 minutes"
        finally:
            if hidden:
                (self.repo / "tests" / hidden).unlink(missing_ok=True)
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        return done.returncode == 0, (lines[-1] if lines else done.stderr.strip()[-200:])


Check = Callable[[Workspace], Tuple[bool, str]]


@dataclass
class Task:
    id: str
    repo: str
    prompt: str
    check: Check
    budget: int = 200_000          # tokens (cached input counted as a tenth); past it agentx is told to wrap up
    what: str = ""
    tags: Tuple[str, ...] = field(default_factory=tuple)


def unchanged(ws: Workspace) -> Tuple[bool, str]:
    changed = ws.changed()
    return (not changed, "no file changed" if not changed else f"changed files it was asked not to: {changed}")


def both(*checks: Check) -> Check:
    def run(ws: Workspace) -> Tuple[bool, str]:
        notes = []
        for check in checks:
            ok, note = check(ws)
            notes.append(note)
            if not ok:
                return False, "; ".join(notes)
        return True, "; ".join(notes)
    return run


def mentions(*groups: Tuple[str, ...]) -> Check:
    """The answer mentions, for every group, at least one of its words (case-insensitive)."""
    def run(ws: Workspace) -> Tuple[bool, str]:
        text = ws.answer.lower()
        missing = [group for group in groups if not any(word.lower() in text for word in group)]
        return (not missing, "the answer covers it" if not missing else f"the answer misses {missing}")
    return run


def tests_pass(*args: str, hidden: Optional[str] = None, what: str = "tests") -> Check:
    def run(ws: Workspace) -> Tuple[bool, str]:
        ok, last = ws.pytest(*args, hidden=hidden)
        return ok, f"{what}: {last}"
    return run


# -- the checks that need their own code ----------------------------------------------------------------------

def restore_line(ws: Workspace) -> Tuple[bool, str]:
    """The answer names orders.py and the line where cancel() puts stock back."""
    source = (REPOS / "shop" / "shop" / "orders.py").read_text(encoding="utf-8").splitlines()
    line = next(number for number, text in enumerate(source, 1) if "inventory.restore" in text)
    numbers = {int(n) for n in re.findall(r"\b(\d{1,3})\b", ws.answer)}
    if "orders.py" not in ws.answer:
        return False, "the answer does not name orders.py"
    if not numbers & {line - 1, line, line + 1}:
        return False, f"the answer does not give line {line} (gave {sorted(numbers)[:6]})"
    return True, f"names orders.py line {line}"


def test_file_untouched(ws: Workspace) -> Tuple[bool, str]:
    touched = "tests/test_orders.py" in ws.changed()
    return (not touched, "the test file is unchanged" if not touched else "it changed the test instead of the code")


def a_test_was_added(ws: Workspace) -> Tuple[bool, str]:
    before = len(re.findall(r"^def test_", (REPOS / "shop" / "tests" / "test_orders.py").read_text(encoding="utf-8"),
                            re.M))
    after = sum(len(re.findall(r"^def test_", (ws.repo / "tests" / name).read_text(encoding="utf-8"), re.M))
                for name in [p.name for p in (ws.repo / "tests").glob("test_*.py")])
    return (after > before, f"tests {before} -> {after}")


def slugify_is_gone(ws: Workspace) -> Tuple[bool, str]:
    left = [str(path.relative_to(ws.repo)) for path in ws.repo.rglob("*.py")
            if not IGNORED_PARTS & set(path.relative_to(ws.repo).parts)
            and "slugify" in path.read_text(encoding="utf-8")]
    return (not left, "no slugify left" if not left else f"slugify still in {left}")


def every_function_typed(ws: Workspace) -> Tuple[bool, str]:
    untyped = []
    for path in sorted((ws.repo / "notes").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
                if node.returns is None or any(a.annotation is None for a in args):
                    untyped.append(f"{path.name}:{node.name}")
    return (not untyped, "every function has type hints" if not untyped else f"missing hints: {untyped}")


# -- the tasks ----------------------------------------------------------------------------------------------------

NOT_THE_DISCOUNT_TEST = ("-k", "not test_total_with_discount")

TASKS: List[Task] = [
    Task("find-code", "shop",
         "Where does cancelling an order put the stock back? Answer with the file name and the line number. "
         "Do not change any files.",
         both(restore_line, unchanged), budget=80_000, what="find a line of code", tags=("read",)),
    Task("explain-failure", "shop",
         "Why does test_total_with_discount fail? Explain the cause in one or two sentences. Do not change any files.",
         both(mentions(("round", "truncat", "int(")), unchanged), budget=80_000,
         what="explain a failing test", tags=("read", "run")),
    Task("fix-failing-test", "shop",
         "test_total_with_discount fails. Fix the code, not the test, so the whole test suite passes.",
         both(tests_pass(what="all tests"), test_file_untouched), budget=150_000,
         what="fix code to pass a test", tags=("edit", "run")),
    Task("fix-bug-add-test", "shop",
         "Cancelling an order twice puts the stock back twice. Fix it so a second cancel changes nothing, and add a "
         "test for it.",
         both(tests_pass(*NOT_THE_DISCOUNT_TEST, hidden="test_hidden_cancel.py", what="hidden test"),
              a_test_was_added),
         budget=150_000, what="fix a bug and add a test", tags=("edit", "run")),
    Task("add-validation", "shop",
         "add_item must reject a quantity below 1 with a ValueError, before touching the stock. Add a test.",
         tests_pass(*NOT_THE_DISCOUNT_TEST, hidden="test_hidden_quantity.py", what="hidden test"),
         budget=150_000, what="add input validation", tags=("edit", "run")),
    Task("add-feature", "notes",
         "Add word_count(text) to notes/text.py: the number of words, where a word is a run of letters, digits or "
         "apostrophes (so \"don't\" is one word). Add tests for it.",
         tests_pass(hidden="test_hidden_words.py", what="hidden test"),
         budget=150_000, what="add a function with tests", tags=("edit", "run")),
    Task("rename-everywhere", "notes",
         "Rename the function slugify to make_slug everywhere in this project, code and tests included, and make "
         "sure the tests pass.",
         both(slugify_is_gone, tests_pass(hidden="test_hidden_rename.py", what="tests")),
         budget=150_000, what="rename across files", tags=("edit", "run")),
    Task("audit", "shop",
         "/auditing-a-project Audit this repository for correctness bugs. Report each with its file and line. "
         "Do not change any files.",
         both(mentions(("cancel",), ("twice", "double", "already", "again", "more than once")),
              mentions(("negative", "zero", "below 1", "quantity <", "<= 0", "< 1")),
              mentions(("round", "truncat")), unchanged),
         budget=400_000, what="audit with the skill", tags=("read", "team")),
    Task("team-job", "notes",
         "/team Add type hints (parameters and return values) to every function in the notes package, then run the "
         "tests and make sure they pass.",
         both(every_function_typed, tests_pass(what="all tests")),
         budget=400_000, what="a team job: edit, then run", tags=("team", "edit", "run")),
    Task("run-and-report", "shop",
         "Run the test suite and tell me which tests fail and why. Do not change anything.",
         both(mentions(("test_total_with_discount",), ("round", "truncat", "851", "850")), unchanged),
         budget=80_000, what="run tests and report", tags=("run",)),
]

BY_ID = {task.id: task for task in TASKS}
