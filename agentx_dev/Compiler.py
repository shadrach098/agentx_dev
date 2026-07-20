"""
Prompt optimization for agent runners.

``Compiled(runner_factory, metric, trainset, ...)`` iteratively refines
the ``system_addendum`` string of an ``AgentRunner`` against a metric
scored by the ``Evals`` harness. Half of DSPy's power at a tenth of
the surface area:

    - a "student" runner that we're improving,
    - a "teacher" model that proposes candidate improvements,
    - a "judge" (the eval harness + your metric callable),
    - a bounded search loop that keeps the best system_addendum seen.

Usage::

    from agentx_dev import (
        AgentRunner, AgentType, Claude, EvalCase, contains,
    )
    from agentx_dev.Compiler import Compiled

    def build():
        return AgentRunner(model=Claude(), agent=AgentType.ReAct, tools=[])

    trainset = [
        EvalCase("paris", "Capital of France?", [contains("Paris")]),
        EvalCase("tokyo", "Capital of Japan?",  [contains("Tokyo")]),
        EvalCase("berlin","Capital of Germany?",[contains("Berlin")]),
    ]

    optimized = Compiled(
        runner_factory=build,
        trainset=trainset,
        iterations=4,
        candidates_per_iter=3,
    ).compile()

    # Now use the optimized runner. It's a normal AgentRunner with a tuned
    # system_addendum stitched in.
    print(optimized.system_addendum)
    result = optimized.invoke("Capital of Italy?")

Design:
  - Seed: run the trainset with the unmodified runner, score baseline.
  - Improve: teacher model reads {failed_case + rationale} and proposes
    K candidate addenda.
  - Score: rebuild the runner with each candidate as system_addendum
    and run the trainset. Keep the best-scoring addendum.
  - Repeat for ``iterations`` rounds. Return the runner built with the
    best addendum seen.

Not a compiler in the DSPy sense (no gradient over programs, no
teleprompter mixing). But it's small, self-contained, and rides on
the eval harness you already trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

from agentx_dev.Evals import EvalCase, EvalReport, EvalResult, EvalRunner


__all__ = ["Compiled", "CompileResult"]


@dataclass
class CompileResult:
    """Return value of ``Compiled.compile()``.

    ``runner`` is the best AgentRunner produced (with its
    ``system_addendum`` set to ``best_addendum``). ``history`` records
    every candidate tried and its score, useful for auditing what
    the optimizer explored.
    """
    runner: Any
    best_addendum: str
    baseline_score: float
    best_score: float
    history: List[dict] = field(default_factory=list)


DEFAULT_TEACHER_PROMPT = """You are an expert prompt engineer improving an agent.

The agent's current SYSTEM INSTRUCTION (may be empty):
---
{current_addendum}
---

It was evaluated against {n_cases} test cases. It PASSED {passed} / {total}.

Failing cases (input, actual output, what was expected):
{failures}

Your job: propose {k} NEW improved SYSTEM INSTRUCTIONS that would fix
the failures. Each one should be a concise instruction the agent
will follow -- not an explanation. Be specific: name behaviors to
adopt or avoid, name output formats to hit. Do NOT reference the
test cases themselves; the agent will see production inputs.

Reply as a JSON array of {k} strings. Example:
["Answer in a single sentence.", "Always spell city names capitalized."]

Return ONLY the JSON array, no other text.
"""


class Compiled:
    """Prompt optimizer over a runner's ``system_addendum``."""

    def __init__(
        self,
        runner_factory: Callable[..., Any],
        trainset: Sequence[EvalCase],
        *,
        teacher_model: Optional[Any] = None,
        iterations: int = 4,
        candidates_per_iter: int = 3,
        min_delta: float = 0.0,
        verbose: bool = True,
        teacher_prompt: str = DEFAULT_TEACHER_PROMPT,
    ):
        """
        Args:
            runner_factory: A callable ``(system_addendum: str | None) -> AgentRunner``.
                The optimizer calls this to build the runner under test.
                Accept a keyword arg named ``system_addendum`` (default
                ``None``). See the module docstring for an example.
            trainset: Cases to score against. Same shape ``EvalRunner``
                consumes.
            teacher_model: A ``BaseChatModel`` used to propose candidate
                addenda. Defaults to reusing the same model class as the
                first runner produced by ``runner_factory``.
            iterations: Number of improvement rounds.
            candidates_per_iter: How many alternative addenda to try per
                round. Bigger -> slower + more thorough.
            min_delta: Minimum absolute score improvement to accept a
                new best. Zero means "any strict improvement wins."
            verbose: Print per-round summaries.
            teacher_prompt: Prompt template for the teacher. Override to
                bias the optimizer toward a specific style.
        """
        # Sanity: the factory must accept a ``system_addendum`` kwarg.
        import inspect
        sig = inspect.signature(runner_factory)
        params = sig.parameters
        if "system_addendum" not in params:
            # Wrap it so callers who don't accept the kwarg still work.
            base_factory = runner_factory
            def _wrapper(system_addendum: Optional[str] = None):
                r = base_factory()
                if system_addendum is not None:
                    r.system_addendum = system_addendum
                return r
            self._factory = _wrapper
        else:
            self._factory = runner_factory

        self._trainset = list(trainset)
        self.iterations = int(iterations)
        self.candidates_per_iter = max(1, int(candidates_per_iter))
        self.min_delta = float(min_delta)
        self.verbose = bool(verbose)
        self.teacher_prompt = teacher_prompt

        self._teacher = teacher_model
        if self._teacher is None:
            probe = self._factory(system_addendum=None)
            self._teacher = probe.model   # reuse the same provider

    # -- Public API ----------------------------------------------------------

    def compile(self) -> CompileResult:
        """Run the optimization loop. Returns a ``CompileResult``."""
        history: List[dict] = []

        # Baseline evaluation.
        baseline_addendum = ""
        baseline_score, baseline_report = self._score(baseline_addendum)
        history.append({
            "iteration": 0, "kind": "baseline",
            "addendum": baseline_addendum, "score": baseline_score,
        })
        if self.verbose:
            print(f"[compile] baseline score: {baseline_score:.2%}")

        best_addendum = baseline_addendum
        best_score = baseline_score
        best_report = baseline_report

        for i in range(1, self.iterations + 1):
            candidates = self._propose_candidates(best_addendum, best_report)
            if not candidates:
                if self.verbose:
                    print(f"[compile] iter {i}: teacher returned no candidates; stopping")
                break

            for cand in candidates:
                score, report = self._score(cand)
                history.append({
                    "iteration": i, "kind": "candidate",
                    "addendum": cand, "score": score,
                })
                if self.verbose:
                    print(f"[compile] iter {i}  score={score:.2%}  addendum={cand[:60]!r}")
                if score - best_score > self.min_delta:
                    best_addendum = cand
                    best_score = score
                    best_report = report
                    if self.verbose:
                        print(f"[compile]   -> new best")

            if best_score >= 1.0:
                if self.verbose:
                    print(f"[compile] perfect score reached; stopping")
                break

        final_runner = self._factory(system_addendum=best_addendum or None)
        if self.verbose:
            print(f"[compile] done. baseline={baseline_score:.2%} -> "
                  f"best={best_score:.2%}  (+{(best_score - baseline_score) * 100:.1f}pp)")
        return CompileResult(
            runner=final_runner,
            best_addendum=best_addendum,
            baseline_score=baseline_score,
            best_score=best_score,
            history=history,
        )

    # -- Internals -----------------------------------------------------------

    def _score(self, addendum: str) -> tuple:
        """Run the trainset against a runner built with ``addendum``.
        Returns ``(pass_rate, report)``."""
        factory = lambda: self._factory(system_addendum=addendum or None)
        runner = EvalRunner(factory, verbose=False)
        report = runner.run(self._trainset)
        return report.pass_rate, report

    def _propose_candidates(self, current: str, report: EvalReport) -> List[str]:
        """Ask the teacher for K new candidate addenda based on the failures
        in ``report``. Returns a list of strings (possibly shorter than
        ``candidates_per_iter`` if the teacher misfired)."""
        failures = self._format_failures(report)
        if not failures:
            return []
        prompt = self.teacher_prompt.format(
            current_addendum=current or "(none)",
            n_cases=len(self._trainset),
            passed=report.passed,
            total=report.total,
            failures=failures,
            k=self.candidates_per_iter,
        )
        try:
            reply = self._teacher.invoke(prompt)
        except Exception as e:
            if self.verbose:
                print(f"[compile] teacher call failed: {e}")
            return []
        return self._parse_candidates(str(reply))

    @staticmethod
    def _format_failures(report: EvalReport, max_cases: int = 6) -> str:
        """Compact list of the failing cases to feed the teacher."""
        lines = []
        n = 0
        for r in report.results:
            if r.passed or n >= max_cases:
                continue
            actual = (r.completion.content if r.completion else "") or ""
            lines.append(
                f"- input: {r.case.input!r}\n"
                f"  actual: {actual[:200]!r}\n"
                f"  failed assertions: {[al for al in r.assertion_lines if 'FAIL' in al]}"
            )
            n += 1
        return "\n".join(lines) or "(no failures -- baseline is already perfect)"

    @staticmethod
    def _parse_candidates(text: str) -> List[str]:
        """Extract a JSON array of strings from the teacher's reply.

        Tolerant of code fences and trailing prose since teacher models
        occasionally decorate JSON with markdown.
        """
        import json
        import re
        # Trim code fences.
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        payload = m.group(1) if m else text
        # Find the first `[` and its matching `]`.
        start = payload.find("[")
        end = payload.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        chunk = payload[start:end + 1]
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        out = []
        for x in parsed:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
