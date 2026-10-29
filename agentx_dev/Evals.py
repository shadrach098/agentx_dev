"""
Evals harness for AgentX.

Run a suite of test cases against an ``AgentRunner``, an
``AsyncAgentRunner``, a ``Supervisor`` or an ``AsyncSupervisor``, collect
metrics, and print a pass/fail report. Designed to be regression-test
friendly: cases live in JSON files, assertions are pure Python predicates,
and the runner records latency, tokens (with the "counted" figure: cached
input as a tenth), model calls and failed tool calls per case so you can
spot quality regressions before they ship. ``repeat=`` runs each case more
than once (models vary), ``tags=`` runs a subset, and ``compare_reports``
puts two saved reports side by side.

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
    #    $ python -m agentx_dev.Evals run tests/evals/ --config agent.yaml --repeat 3 --json-out a.json
    #    $ python -m agentx_dev.Evals compare a.json b.json

Case file shape (JSON):

    {
      "name": "paris_capital",
      "input": "What's the capital of France?",
      "expected_substrings": ["Paris"],
      "forbidden_substrings": ["Berlin"],
      "must_call_tools": [],
      "max_iterations": 4,
      "outcome": "done",
      "max_tool_errors": 0,
      "used_skills": [],
      "output_fields": {"city": "Paris"},
      "tags": ["smoke"]
    }

The CLI loader translates those declarative fields into assertion callables.
Callers who want richer assertions (e.g. LLM-grading, regex) should
construct ``EvalCase`` in code.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from agentx_dev.Agents.Agent import AgentCompletion
from agentx_dev.RunLog import RunLog


__all__ = [
    "EvalCase", "EvalResult", "EvalReport", "EvalRunner",
    "contains", "not_contains", "matches_regex",
    "called_tool", "tool_count", "max_iterations",
    "llm_judge",
    "outcome_is", "output_field", "used_skill", "used_agent", "subtasks_ok",
    "load_cases_from_dir", "load_case_from_dict", "compare_reports",
]

CACHED_SHARE = 10      # cached input is billed at about a tenth: the "counted" tokens count it so


def counted(tokens_in: int, tokens_out: int, cached: int) -> int:
    """Tokens as ``agentx``'s token limit and the benchmark count them: cached input counts as a tenth."""
    tokens_in, tokens_out = max(0, int(tokens_in or 0)), max(0, int(tokens_out or 0))
    cached = min(max(0, int(cached or 0)), tokens_in)
    return tokens_in - cached + cached // CACHED_SHARE + tokens_out


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
        # Match YES or NO at the start of the reply, respecting word
        # boundaries. Different providers punctuate the verdict
        # differently ("YES", "Yes.", "YES,", "yes -- ") -- all of
        # these should register as YES.
        import re as _re
        m = _re.match(r"\s*(YES|NO)\b", first_line, _re.IGNORECASE)
        verdict = m.group(1).upper() if m else ""
        ok = verdict == "YES"
        return ok, f"llm_judge({criterion!r}): {'PASS' if ok else 'FAIL'} -- {first_line[:120]}"
    return _check


def outcome_is(*outcomes: str) -> AssertionFn:
    """The run ended one of these ways (``"done"``, ``"stuck"``, ``"out_of_time"``, ``"out_of_budget"``,
    ``"iteration_limit"``; a Supervisor's result also has ``"partial"``). Default: ``"done"``."""
    wanted = tuple(outcomes) or ("done",)

    def _check(c: Any) -> Tuple[bool, str]:
        got = str(getattr(c, "outcome", "done") or "done")
        ok = got in wanted
        return ok, f"outcome_is({', '.join(wanted)}): {'PASS' if ok else 'FAIL'} -- {got}"
    return _check


_MISSING = object()


def output_field(name: str, expected: Any = _MISSING, *,
                 check: Optional[Callable[[Any], bool]] = None) -> AssertionFn:
    """The structured output (``output_schema=``) has field ``name``; equal to ``expected`` when given, and
    passing ``check(value)`` when given. Works on a Pydantic model or a dict."""
    def _check(c: Any) -> Tuple[bool, str]:
        output = getattr(c, "output", None)
        if output is None:
            return False, f"output_field({name!r}): FAIL -- no structured output (pass output_schema=)"
        value = output.get(name, _MISSING) if isinstance(output, dict) else getattr(output, name, _MISSING)
        if value is _MISSING:
            return False, f"output_field({name!r}): FAIL -- no such field"
        if expected is not _MISSING and value != expected:
            return False, f"output_field({name!r}): FAIL -- {value!r} != {expected!r}"
        if check is not None:
            try:
                if not check(value):
                    return False, f"output_field({name!r}): FAIL -- check rejected {value!r}"
            except Exception as e:
                return False, f"output_field({name!r}): FAIL -- check raised {type(e).__name__}: {e}"
        return True, f"output_field({name!r}): PASS -- {value!r}"
    return _check


def used_skill(name: str) -> AssertionFn:
    """The agent opened the skill ``name`` (a ``use_skill`` call with that name)."""
    def _check(c: Any) -> Tuple[bool, str]:
        for tc in getattr(c, "tool_calls", None) or []:
            if tc.name != "use_skill":
                continue
            args = tc.args.get("Input", tc.args) if isinstance(tc.args, dict) else tc.args
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"name": args}
            if isinstance(args, dict) and str(args.get("name", "")).lower() == name.lower():
                return True, f"used_skill({name!r}): PASS"
        return False, f"used_skill({name!r}): FAIL -- not opened"
    return _check


def used_agent(name: str) -> AssertionFn:
    """A Supervisor ran a step on the specialist ``name``."""
    def _check(c: Any) -> Tuple[bool, str]:
        agents = [getattr(s, "agent", "") for s in getattr(c, "subtasks", None) or []]
        ok = name in agents
        return ok, f"used_agent({name!r}): {'PASS' if ok else 'FAIL'} -- ran {', '.join(agents) or 'nothing'}"
    return _check


def subtasks_ok() -> AssertionFn:
    """Every step a Supervisor ran finished without an error."""
    def _check(c: Any) -> Tuple[bool, str]:
        subtasks = getattr(c, "subtasks", None)
        if subtasks is None:
            return False, "subtasks_ok(): FAIL -- not a Supervisor result"
        failed = [f"{s.agent}: {str(s.error)[:60]}" for s in subtasks if getattr(s, "error", None)]
        return not failed, "subtasks_ok(): " + ("PASS" if not failed else "FAIL -- " + "; ".join(failed[:3]))
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
    # At most this many failed tool calls (None: not checked). Counted from the run log, so it covers calls whose
    # failure the agent recovered from, which ``completion.tool_calls`` does not list.
    max_tool_errors: Optional[int] = None


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
    tool_errors: List[str] = field(default_factory=list)     # the tools whose calls failed, one entry per failure
    model_calls: int = 0
    round: int = 1                                          # which repeat this was (``repeat=``)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def counted_tokens(self) -> int:
        """Tokens with cached input counted as a tenth, as ``agentx`` and the benchmark count them."""
        return counted(self.input_tokens, self.output_tokens, self.cache_read_tokens)


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

    @property
    def total_counted_tokens(self) -> int:
        return sum(r.counted_tokens for r in self.results)

    @property
    def total_tool_errors(self) -> int:
        return sum(len(r.tool_errors) for r in self.results)

    def by_case(self) -> Dict[str, List[EvalResult]]:
        """Each case's results (several with ``repeat=``), in the order the cases ran."""
        out: Dict[str, List[EvalResult]] = {}
        for r in self.results:
            out.setdefault(r.case.name, []).append(r)
        return out

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
        lines.append(f"  counted:       {self.total_counted_tokens} (cached input as a tenth)")
        if self.total_tool_errors:
            lines.append(f"  failed tool calls: {self.total_tool_errors}")
        lines.append("")
        for name, runs in self.by_case().items():
            if len(runs) > 1:
                passed = sum(1 for r in runs if r.passed)
                lines.append(f"  [{passed}/{len(runs)}] {name}  ({sum(r.duration_sec for r in runs) / len(runs):.2f}s, "
                             f"{sum(r.counted_tokens for r in runs) // len(runs)} counted tokens a run)")
            for r in runs:
                if len(runs) == 1:
                    status = "PASS" if r.passed else "FAIL"
                    lines.append(
                        f"  [{status}] {r.case.name}  ({r.duration_sec:.2f}s, "
                        f"{r.total_tokens} tokens)"
                    )
                if r.passed:
                    continue
                prefix = f"       round {r.round}: " if len(runs) > 1 else "       - "
                for al in r.assertion_lines:
                    if len(runs) == 1 or "FAIL" in al:
                        lines.append(f"{prefix}{al}")
                if r.error:
                    lines.append(f"{prefix}runner error: {r.error}")
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
            "total_counted_tokens": self.total_counted_tokens,
            "results": [
                {
                    "name": r.case.name,
                    "round": r.round,
                    "tags": list(r.case.tags),
                    "passed": r.passed,
                    "duration_sec": r.duration_sec,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cache_read_tokens": r.cache_read_tokens,
                    "counted_tokens": r.counted_tokens,
                    "model_calls": r.model_calls,
                    "tool_error_count": r.tool_error_count,
                    "tool_errors": list(r.tool_errors),
                    "outcome": str(getattr(r.completion, "outcome", "") or "") if r.completion is not None else "",
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
    """Run a set of ``EvalCase`` objects against what ``runner_factory`` builds: an ``AgentRunner``
    (``invoke``), an ``AsyncAgentRunner`` (``ainvoke``), a ``Supervisor`` (``run``) or an ``AsyncSupervisor``
    (``await run``). Async ones run on their own event loop, in a worker thread when this thread already has one
    (a notebook).

    The ``runner_factory`` builds a FRESH runner per case (and per repeat) so state doesn't leak between tests.
    This matters for auto_cache, memory, and any tool-side accumulators.

    ``repeat`` runs every case that many times (models vary from run to run; the summary shows each case's pass
    count). ``tags`` (here or in ``run``) keeps only the cases that have one of those tags.
    """

    def __init__(
        self,
        runner_factory: Callable[[], Any],
        *,
        verbose: bool = True,
        repeat: int = 1,
        tags: Optional[Iterable[str]] = None,
    ):
        self._factory = runner_factory
        self.verbose = verbose
        self.repeat = max(1, int(repeat))
        self.tags = set(tags) if tags else None

    def run(self, cases: Sequence[EvalCase], *, tags: Optional[Iterable[str]] = None) -> EvalReport:
        wanted = set(tags) if tags else self.tags
        chosen = [c for c in cases if wanted is None or wanted & set(c.tags)]
        results: List[EvalResult] = []
        for round_number in range(1, self.repeat + 1):
            for case in chosen:
                if self.verbose:
                    again = f" (round {round_number}/{self.repeat})" if self.repeat > 1 else ""
                    print(f"[eval] running {case.name!r}{again} ...")
                result = self._run_one(case)
                result.round = round_number
                results.append(result)
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
        # The run log of every runner involved (a Supervisor's specialists too) also reports to this case, so
        # failed tool calls are counted even when the agent recovered from them.
        events: List[Dict[str, Any]] = []
        collector = RunLog(events.append)
        for target in _runners_of(runner):
            try:
                target.log = RunLog.chain(getattr(target, "log", None), collector)
            except Exception:
                pass                                     # a runner without a log: no tool errors from it
        models = _models_of(runner)
        before = [_usage_of(m) for m in models]
        start = time.perf_counter()
        completion: Optional[Any] = None
        err: Optional[str] = None
        try:
            completion = _call(runner, case.input)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        duration = time.perf_counter() - start

        tool_errors = [str(ev.get("tool")) for ev in events if ev.get("event") == "tool" and ev.get("ok") is False]
        model_calls = sum(1 for ev in events if ev.get("event") == "model.call")

        assertion_lines: List[str] = []
        passed = err is None and completion is not None
        checks = list(case.assertions)
        if case.max_tool_errors is not None:
            limit = case.max_tool_errors

            def _tool_errors(_c: Any, limit: int = limit) -> Tuple[bool, str]:
                ok = len(tool_errors) <= limit
                listed = f" ({', '.join(tool_errors[:5])})" if tool_errors else ""
                return ok, f"max_tool_errors({limit}): {'PASS' if ok else 'FAIL'} -- {len(tool_errors)}{listed}"
            checks.append(_tool_errors)
        if passed and checks:
            for check in checks:
                try:
                    ok, msg = check(completion)
                except Exception as e:
                    ok, msg = False, f"assertion raised: {e}"
                assertion_lines.append(msg)
                if not ok:
                    passed = False

        after = [_usage_of(m) for m in models]
        spent = [sum(a[i] - b[i] for a, b in zip(after, before)) for i in range(3)]

        return EvalResult(
            case=case,
            completion=completion,
            passed=passed,
            duration_sec=duration,
            assertion_lines=assertion_lines,
            error=err,
            tool_error_count=len(tool_errors),
            input_tokens=spent[0],
            output_tokens=spent[1],
            cache_read_tokens=spent[2],
            tool_errors=tool_errors,
            model_calls=model_calls,
        )


def _call(target: Any, text: str) -> Any:
    """Run one case on ``target``, whichever kind it is."""
    invoke = getattr(target, "invoke", None)
    if callable(invoke) and not inspect.iscoroutinefunction(invoke):
        return invoke(text)
    ainvoke = getattr(target, "ainvoke", None)
    if callable(ainvoke):
        return _wait(ainvoke(text))
    run = getattr(target, "run", None)
    if callable(run):
        out = run(text)
        return _wait(out) if inspect.isawaitable(out) else out
    raise TypeError(f"{type(target).__name__} has no invoke, ainvoke or run method")


def _wait(awaitable: Any) -> Any:
    """Run a coroutine to the end: on a new event loop, or in a worker thread when one is already running here."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    box: Dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except BaseException as e:               # handed back to the caller's thread below
            box["error"] = e
    thread = threading.Thread(target=work, name="agentx-eval", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _runners_of(target: Any) -> List[Any]:
    """``target`` and, for a Supervisor, each registered specialist's runner."""
    out = [target]
    agents = getattr(target, "agents", None)
    if isinstance(agents, dict):
        for entry in agents.values():
            runner = getattr(entry, "runner", None)
            if runner is None and isinstance(entry, tuple) and len(entry) == 2:
                runner = entry[1]
            if runner is not None and runner is not target:
                out.append(runner)
    return out


def _models_of(target: Any) -> List[Any]:
    """Every distinct model a case can spend tokens on (a Supervisor's planner and its specialists')."""
    seen: Dict[int, Any] = {}
    for runner in _runners_of(target):
        model = getattr(runner, "model", None)
        if model is not None:
            seen.setdefault(id(model), model)
    return list(seen.values())


def _usage_of(model: Any) -> Tuple[int, int, int]:
    usage = getattr(model, "usage", None)
    return (int(getattr(usage, "total_input_tokens", 0) or 0), int(getattr(usage, "total_output_tokens", 0) or 0),
            int(getattr(usage, "total_cache_read_tokens", 0) or 0))


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
      - ``outcome`` (str or list of str) -> ``outcome_is(...)``
      - ``used_skills`` (list of str) -> ``used_skill(...)`` per item
      - ``used_agents`` (list of str) -> ``used_agent(...)`` per item (Supervisors)
      - ``subtasks_ok`` (true) -> ``subtasks_ok()`` (Supervisors)
      - ``output_fields`` (dict) -> ``output_field(name, value)`` per item
      - ``max_tool_errors`` (int) -> at most that many failed tool calls
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
    if data.get("outcome"):
        wanted = data["outcome"]
        assertions.append(outcome_is(*([wanted] if isinstance(wanted, str) else [str(w) for w in wanted])))
    for skill in data.get("used_skills") or []:
        assertions.append(used_skill(str(skill)))
    for agent in data.get("used_agents") or []:
        assertions.append(used_agent(str(agent)))
    if data.get("subtasks_ok"):
        assertions.append(subtasks_ok())
    fields = data.get("output_fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("'output_fields' must be an object of field: expected value")
    for field_name, expected in fields.items():
        assertions.append(output_field(str(field_name), expected))
    limit = data.get("max_tool_errors")
    return EvalCase(
        name=name, input=inp, assertions=assertions,
        tags=list(data.get("tags") or []),
        metadata=dict(data.get("metadata") or {}),
        max_tool_errors=int(limit) if limit is not None else None,
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


def compare_reports(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    """Two saved reports (``EvalReport.to_dict()``, ``--json-out``) side by side: each case's pass count and
    counted tokens a run, and the change in tokens."""
    def per_case(report: Dict[str, Any]) -> Dict[str, Tuple[int, int, int]]:
        out: Dict[str, List[int]] = {}
        for r in report.get("results") or []:
            row = out.setdefault(str(r.get("name")), [0, 0, 0])
            row[0] += 1 if r.get("passed") else 0
            row[1] += 1
            tokens = r.get("counted_tokens")
            if tokens is None:                       # a report from before counted tokens
                tokens = counted(r.get("input_tokens", 0), r.get("output_tokens", 0), r.get("cache_read_tokens", 0))
            row[2] += int(tokens or 0)
        return {name: (p, n, t) for name, (p, n, t) in out.items()}

    a, b = per_case(before), per_case(after)
    names = list(a) + [name for name in b if name not in a]
    width = max([len(n) for n in names] + [4])
    lines = [f"{'case':<{width}}  passed        counted tokens a run"]
    for name in names:
        pa, na, ta = a.get(name, (0, 0, 0))
        pb, nb, tb = b.get(name, (0, 0, 0))
        mean_a, mean_b = (ta // na if na else 0), (tb // nb if nb else 0)
        change = f"{round((mean_b - mean_a) * 100 / mean_a):+d}%" if mean_a and nb else ""
        left = f"{pa}/{na}" if na else "-"
        right = f"{pb}/{nb}" if nb else "-"
        lines.append(f"{name:<{width}}  {left:>5} -> {right:<5}  {mean_a:>8} -> {mean_b:<8} {change}")
    total_a = sum(p for p, _, _ in a.values()), sum(n for _, n, _ in a.values())
    total_b = sum(p for p, _, _ in b.values()), sum(n for _, n, _ in b.values())
    lines.append(f"{'all':<{width}}  {total_a[0]}/{total_a[1]} -> {total_b[0]}/{total_b[1]}")
    return "\n".join(lines)


def _cli(argv: Optional[List[str]] = None) -> int:
    """``python -m agentx_dev.Evals run <dir> --config <yaml> [--repeat N] [--tag T] [--json-out F]`` and
    ``python -m agentx_dev.Evals compare <a.json> <b.json>``.

    ``run`` loads a runner from a YAML config (uses ``load_agent_from_yaml``), runs every ``*.eval.json`` file
    under <dir>, prints the summary, and exits 0 on all-pass, 1 on any failure.
    """
    import argparse
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
    run.add_argument("--repeat", type=int, default=1, help="run every case this many times (models vary)")
    run.add_argument("--tag", action="append", default=None, help="only cases with this tag (repeatable)")
    run.add_argument("--log", default=None, help="also write every run's log to this file (.jsonl: JSON lines)")
    compare = sub.add_parser("compare", help="two saved reports (--json-out) side by side")
    compare.add_argument("before")
    compare.add_argument("after")
    args = parser.parse_args(argv)

    if args.cmd == "compare":
        with open(args.before, encoding="utf-8") as f:
            before = json.load(f)
        with open(args.after, encoding="utf-8") as f:
            after = json.load(f)
        print(compare_reports(before, after))
        return 0

    from agentx_dev.Loader import load_agent_from_yaml
    cases = load_cases_from_dir(args.dir, pattern=args.pattern)
    if not cases:
        print(f"no cases matched {args.pattern} under {args.dir}")
        return 1

    def _factory():
        runner = load_agent_from_yaml(args.config)
        if args.log:
            runner.log = RunLog.chain(getattr(runner, "log", None), RunLog.to(args.log))
        return runner

    report = EvalRunner(_factory, repeat=args.repeat, tags=args.tag).run(cases)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2)
    return 0 if report.failed == 0 and report.total > 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
