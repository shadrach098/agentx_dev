"""
Evals harness for AgentX.

Run a suite of test cases against an ``AgentRunner``, collect metrics,
and print a pass/fail report. Designed to be regression-test friendly:
cases live in JSON files, assertions are pure Python predicates, and
the runner records latency + tokens + tool-error rate per case so you
can spot quality regressions before they ship.

Two shapes of usage:

    # 1. Inline: list of EvalCase objects, run programmatically.
    from agentx_dev.Evals import EvalCase, EvalRunner, contains, called_tool

    cases = [
        EvalCase(
            name="paris_capital",
            input="What's the capital of France?",
            assertions=[contains("Paris")],
        ),
        EvalCase(
            name="uses_calculator",
            input="What is 137 * 91?",
            assertions=[called_tool("calculator"), contains("12467")],
        ),
    ]

    runner_factory = lambda: build_my_runner()
    report = EvalRunner(runner_factory).run(cases)
    print(report.summary())

    # 2. From disk: JSON files under a directory, run via CLI.
    #    $ python -m agentx_dev.Evals run tests/evals/

Case file shape (JSON):

    {
      "name": "paris_capital",
      "input": "What's the capital of France?",
      "expected_substrings": ["Paris"],
      "forbidden_substrings": ["Berlin"],
      "must_call_tools": [],
      "max_iterations": 4
    }

The CLI loader translates those declarative fields into assertion callables.
Callers who want richer assertions (e.g. LLM-grading, regex) should
construct ``EvalCase`` in code.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from agentx_dev.Agents.Agent import AgentCompletion


__all__ = [
    "EvalCase", "EvalResult", "EvalReport", "EvalRunner",
    "contains", "not_contains", "matches_regex",
    "called_tool", "tool_count", "max_iterations",
    "llm_judge",
    "load_cases_from_dir", "load_case_from_dict",
]


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


AssertionFn = Callable[[AgentCompletion], Tuple[bool, str]]
"""An assertion is any callable ``(completion) -> (passed: bool, message: str)``.
Message describes the check on both pass and fail -- used in the report."""


def contains(needle: str, *, case_sensitive: bool = False) -> AssertionFn:
    """The final answer must contain ``needle``."""
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        haystack = c.content or ""
        if case_sensitive:
            ok = needle in haystack
        else:
            ok = needle.lower() in haystack.lower()
        return ok, f"contains {needle!r}: {'PASS' if ok else 'FAIL'}"
    return _check


def not_contains(needle: str, *, case_sensitive: bool = False) -> AssertionFn:
    """The final answer must NOT contain ``needle``."""
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        haystack = c.content or ""
        if case_sensitive:
            ok = needle not in haystack
        else:
            ok = needle.lower() not in haystack.lower()
        return ok, f"not_contains {needle!r}: {'PASS' if ok else 'FAIL'}"
    return _check


def matches_regex(pattern: str, *, flags: int = re.IGNORECASE) -> AssertionFn:
    """The final answer must match ``pattern`` (via ``re.search``)."""
    compiled = re.compile(pattern, flags=flags)
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        ok = bool(compiled.search(c.content or ""))
        return ok, f"matches /{pattern}/: {'PASS' if ok else 'FAIL'}"
    return _check


def called_tool(name: str) -> AssertionFn:
    """The completion's tool_calls list must include a call to ``name``."""
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        names = [tc.name for tc in (c.tool_calls or [])]
        ok = name in names
        return ok, f"called_tool {name!r}: {'PASS' if ok else 'FAIL'} (saw {names})"
    return _check


def tool_count(*, min: int = 0, max: Optional[int] = None) -> AssertionFn:
    """Number of tool calls must be within the given bounds."""
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        n = len(c.tool_calls or [])
        ok = n >= min and (max is None or n <= max)
        rng = f"[{min}, {max if max is not None else 'inf'}]"
        return ok, f"tool_count in {rng}: {'PASS' if ok else 'FAIL'} (n={n})"
    return _check


def max_iterations(limit: int) -> AssertionFn:
    """Number of steps must be <= ``limit``."""
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        n = len(c.steps or [])
        ok = n <= limit
        return ok, f"max_iterations={limit}: {'PASS' if ok else 'FAIL'} (used {n})"
    return _check


def llm_judge(
    judge_model: Any,
    criterion: str,
) -> AssertionFn:
    """Use a chat model to grade the completion.

    The judge is asked a yes/no question:

        Given the criterion "<criterion>", does the following answer
        satisfy it? Answer only YES or NO followed by one short sentence.

        Answer: <completion.content>

    ``judge_model`` must implement ``.invoke(str) -> str`` (any BaseChatModel).
    Useful for open-ended quality checks where substring matching is
    too brittle ("is this response helpful?", "is it grammatically correct?").
    """
    def _check(c: AgentCompletion) -> Tuple[bool, str]:
        prompt = (
            f"Given the criterion \"{criterion}\", does the following answer "
            f"satisfy it? Reply with YES or NO followed by one short sentence.\n\n"
            f"Answer: {c.content}"
        )
        try:
            reply = str(judge_model.invoke(prompt)).strip()
        except Exception as e:
            return False, f"llm_judge({criterion!r}): ERROR calling judge -- {e}"
        first_line = reply.splitlines()[0] if reply else ""
        first_word = first_line.split(None, 1)[0].upper() if first_line else ""
        ok = first_word == "YES"
        return ok, f"llm_judge({criterion!r}): {'PASS' if ok else 'FAIL'} -- {first_line[:120]}"
    return _check


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """One input + a list of assertions to run against the completion."""
    name: str
    input: str
    assertions: List[AssertionFn] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalResult:
    """Outcome of running one ``EvalCase`` through a runner."""
    case: EvalCase
    completion: Optional[AgentCompletion]
    passed: bool
    duration_sec: float
    assertion_lines: List[str] = field(default_factory=list)
    error: Optional[str] = None
    tool_error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class EvalReport:
    """Aggregate of all case results plus a summary string."""
    results: List[EvalResult]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def total_duration_sec(self) -> float:
        return sum(r.duration_sec for r in self.results)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.results)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.results)

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(r.cache_read_tokens for r in self.results)

    def summary(self) -> str:
        lines = [
            f"AgentX Evals -- {self.passed}/{self.total} passed "
            f"({self.pass_rate:.1%}) in {self.total_duration_sec:.2f}s",
            f"  input tokens:  {self.total_input_tokens}",
            f"  output tokens: {self.total_output_tokens}",
        ]
        if self.total_cache_read_tokens:
            hit_ratio = self.total_cache_read_tokens / max(1, self.total_input_tokens)
            lines.append(
                f"  cache reads:   {self.total_cache_read_tokens} "
                f"({hit_ratio:.1%} of input)"
            )
        lines.append("")
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(
                f"  [{status}] {r.case.name}  ({r.duration_sec:.2f}s, "
                f"{r.total_tokens} tokens)"
            )
            if not r.passed:
                for al in r.assertion_lines:
                    lines.append(f"       - {al}")
                if r.error:
                    lines.append(f"       - runner error: {r.error}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable summary -- for CI dashboards, artifact upload, etc."""
        return {
            "passed": self.passed, "failed": self.failed, "total": self.total,
            "pass_rate": self.pass_rate,
            "total_duration_sec": self.total_duration_sec,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "results": [
                {
                    "name": r.case.name,
                    "passed": r.passed,
                    "duration_sec": r.duration_sec,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cache_read_tokens": r.cache_read_tokens,
                    "tool_error_count": r.tool_error_count,
                    "assertions": r.assertion_lines,
                    "error": r.error,
                    "content_preview": (r.completion.content or "")[:200] if r.completion else "",
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class EvalRunner:
    """Run a set of ``EvalCase`` objects against an ``AgentRunner``.

    The ``runner_factory`` builds a FRESH runner per case so state doesn't
    leak between tests. This matters for auto_cache, memory, and any
    tool-side accumulators.
    """

    def __init__(
        self,
        runner_factory: Callable[[], Any],
        *,
        verbose: bool = True,
    ):
        self._factory = runner_factory
        self.verbose = verbose

    def run(self, cases: Sequence[EvalCase]) -> EvalReport:
        results: List[EvalResult] = []
        for case in cases:
            if self.verbose:
                print(f"[eval] running {case.name!r} ...")
            results.append(self._run_one(case))
        report = EvalReport(results=results)
        if self.verbose:
            print()
            print(report.summary())
        return report

    def _run_one(self, case: EvalCase) -> EvalResult:
        try:
            runner = self._factory()
        except Exception as e:
            return EvalResult(
                case=case, completion=None, passed=False, duration_sec=0.0,
                error=f"runner_factory raised: {e}",
            )
        start = time.perf_counter()
        completion: Optional[AgentCompletion] = None
        err: Optional[str] = None
        # Snapshot the pre-run usage so we can attribute deltas to this case.
        model = getattr(runner, "model", None)
        pre_input = getattr(getattr(model, "usage", None), "total_input_tokens", 0) or 0
        pre_output = getattr(getattr(model, "usage", None), "total_output_tokens", 0) or 0
        pre_cache = getattr(getattr(model, "usage", None), "total_cache_read_tokens", 0) or 0
        try:
            completion = runner.invoke(case.input)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        duration = time.perf_counter() - start

        assertion_lines: List[str] = []
        passed = err is None and completion is not None
        if passed and case.assertions:
            for check in case.assertions:
                try:
                    ok, msg = check(completion)
                except Exception as e:
                    ok, msg = False, f"assertion raised: {e}"
                assertion_lines.append(msg)
                if not ok:
                    passed = False

        post_input = getattr(getattr(model, "usage", None), "total_input_tokens", 0) or 0
        post_output = getattr(getattr(model, "usage", None), "total_output_tokens", 0) or 0
        post_cache = getattr(getattr(model, "usage", None), "total_cache_read_tokens", 0) or 0

        tool_error_count = 0
        if completion is not None:
            for tc in completion.tool_calls or []:
                if isinstance(tc.result, str) and tc.result.startswith("ToolError"):
                    tool_error_count += 1

        return EvalResult(
            case=case,
            completion=completion,
            passed=passed,
            duration_sec=duration,
            assertion_lines=assertion_lines,
            error=err,
            tool_error_count=tool_error_count,
            input_tokens=post_input - pre_input,
            output_tokens=post_output - pre_output,
            cache_read_tokens=post_cache - pre_cache,
        )


# ---------------------------------------------------------------------------
# JSON case loader (for the CLI)
# ---------------------------------------------------------------------------


def load_case_from_dict(data: Dict[str, Any]) -> EvalCase:
    """Translate a declarative JSON dict into an ``EvalCase``.

    Recognized fields:
      - ``name`` (str, required)
      - ``input`` (str, required)
      - ``expected_substrings`` (list of str) -> ``contains(...)`` per item
      - ``forbidden_substrings`` (list of str) -> ``not_contains(...)`` per item
      - ``must_call_tools`` (list of str) -> ``called_tool(...)`` per item
      - ``matches_regex`` (list of str) -> ``matches_regex(...)`` per item
      - ``max_iterations`` (int) -> ``max_iterations(N)``
      - ``tags`` (list of str)
      - ``metadata`` (dict)
    """
    name = data.get("name")
    inp = data.get("input")
    if not isinstance(name, str) or not isinstance(inp, str):
        raise ValueError("eval case JSON must have string 'name' and 'input'")
    assertions: List[AssertionFn] = []
    for s in data.get("expected_substrings") or []:
        assertions.append(contains(str(s)))
    for s in data.get("forbidden_substrings") or []:
        assertions.append(not_contains(str(s)))
    for pattern in data.get("matches_regex") or []:
        assertions.append(matches_regex(str(pattern)))
    for tool in data.get("must_call_tools") or []:
        assertions.append(called_tool(str(tool)))
    if "max_iterations" in data:
        assertions.append(max_iterations(int(data["max_iterations"])))
    return EvalCase(
        name=name, input=inp, assertions=assertions,
        tags=list(data.get("tags") or []),
        metadata=dict(data.get("metadata") or {}),
    )


def load_cases_from_dir(path: Any, *, pattern: str = "*.eval.json") -> List[EvalCase]:
    """Walk ``path`` for files matching ``pattern`` and build EvalCases.

    Each matching file may contain either a single case dict or a
    ``{"cases": [...]}`` wrapper for multiple cases in one file.
    """
    root = Path(path)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")
    out: List[EvalCase] = []
    for f in sorted(root.rglob(pattern)):
        with open(f, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            for d in data:
                out.append(load_case_from_dict(d))
        elif isinstance(data, dict) and isinstance(data.get("cases"), list):
            for d in data["cases"]:
                out.append(load_case_from_dict(d))
        elif isinstance(data, dict):
            out.append(load_case_from_dict(data))
        else:
            raise ValueError(f"unrecognized eval file shape: {f}")
    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli(argv: Optional[List[str]] = None) -> int:
    """``python -m agentx_dev.Evals run <dir> --config <yaml>``.

    Loads a runner from a YAML config (uses ``load_agent_from_yaml``),
    runs every ``*.eval.json`` file under <dir>, prints the summary,
    and exits 0 on all-pass, 1 on any failure.
    """
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="agentx_dev.Evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run every case under <dir>")
    run.add_argument("dir", help="directory containing *.eval.json files")
    run.add_argument("--config", required=True,
                     help="YAML config for the runner (loaded via load_agent_from_yaml)")
    run.add_argument("--pattern", default="*.eval.json",
                     help="filename glob for eval cases (default *.eval.json)")
    run.add_argument("--json-out", default=None,
                     help="write report as JSON to this path")
    args = parser.parse_args(argv)

    if args.cmd != "run":
        parser.error("unknown command")

    from agentx_dev.Loader import load_agent_from_yaml
    cases = load_cases_from_dir(args.dir, pattern=args.pattern)
    if not cases:
        print(f"no cases matched {args.pattern} under {args.dir}")
        return 1

    def _factory():
        return load_agent_from_yaml(args.config)

    report = EvalRunner(_factory).run(cases)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2)
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
