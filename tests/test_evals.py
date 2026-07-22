"""Tests for the evals harness itself.

Uses MockRunner + a MockModel so the tests don't need real LLM keys.
"""

import json

import pytest

from agentx_dev import (
    EvalCase, EvalRunner, EvalReport,
    contains, not_contains, matches_regex, called_tool, tool_count, max_iterations,
    load_case_from_dict, load_cases_from_dir,
)
from agentx_dev.Agents.Agent import AgentCompletion, ToolCall


class MockRunner:
    """Runner-shaped object for the eval harness."""

    def __init__(self, script=None):
        self._script = script or {}
        self.model = None

    def invoke(self, query, chat_history=None):
        entry = self._script.get(query, {})
        content = entry.get("content", f"answer to: {query}")
        tcs = entry.get("tool_calls", [])
        return AgentCompletion.from_agent(
            model_name="mock", query=query, content=content,
            tool_calls=tcs, steps=entry.get("steps", []), history=[],
        )


def _completion(content, tool_calls=None, steps=None):
    return AgentCompletion.from_agent(
        model_name="mock", query="q", content=content,
        tool_calls=tool_calls or [], steps=steps or [], history=[],
    )


class TestAssertions:

    def test_contains_passes(self):
        ok, msg = contains("Paris")(_completion("Paris is the capital."))
        assert ok and "PASS" in msg

    def test_contains_fails(self):
        ok, _ = contains("Berlin")(_completion("Paris is the capital."))
        assert not ok

    def test_contains_case_insensitive_default(self):
        ok, _ = contains("PARIS")(_completion("paris"))
        assert ok

    def test_not_contains(self):
        ok, _ = not_contains("Berlin")(_completion("Paris is the capital."))
        assert ok
        ok, _ = not_contains("Paris")(_completion("Paris"))
        assert not ok

    def test_matches_regex(self):
        ok, _ = matches_regex(r"\bParis\b")(_completion("Paris"))
        assert ok
        ok, _ = matches_regex(r"^\d+$")(_completion("Paris"))
        assert not ok

    def test_called_tool_passes(self):
        c = _completion("done", tool_calls=[
            ToolCall(name="calc", args={"Input": "1+1"}, result="2"),
        ])
        ok, msg = called_tool("calc")(c)
        assert ok

    def test_called_tool_fails(self):
        c = _completion("done", tool_calls=[])
        ok, _ = called_tool("calc")(c)
        assert not ok

    def test_tool_count_bounds(self):
        c = _completion("done", tool_calls=[
            ToolCall(name="t", args={}, result="r") for _ in range(3)
        ])
        assert tool_count(min=1)(c)[0]
        assert tool_count(min=1, max=5)(c)[0]
        assert not tool_count(max=2)(c)[0]

    def test_max_iterations(self):
        c = _completion("done", steps=["s1", "s2"])
        assert max_iterations(3)(c)[0]
        assert not max_iterations(1)(c)[0]

    def test_llm_judge_parses_various_verdict_shapes(self):
        """Bug regression: different providers punctuate the YES/NO
        verdict differently. All valid YES shapes must pass."""
        from agentx_dev import llm_judge

        class ScriptedJudge:
            def __init__(self, reply):
                self.reply = reply
            def invoke(self, _prompt):
                return self.reply

        c = _completion("some answer")

        yes_shapes = [
            "YES",
            "YES.",
            "YES!",
            "YES, the answer is accurate.",
            "YES -- exactly what was asked.",
            "Yes.",
            "yes, it does.",
            "  YES\nfollow-up sentence.",
        ]
        for reply in yes_shapes:
            ok, msg = llm_judge(ScriptedJudge(reply), "criterion")(c)
            assert ok, f"expected YES verdict from reply {reply!r}, got FAIL: {msg}"

        no_shapes = [
            "NO",
            "NO.",
            "NO, it doesn't.",
            "no -- the answer is wrong.",
            "  no\nreason",
        ]
        for reply in no_shapes:
            ok, msg = llm_judge(ScriptedJudge(reply), "criterion")(c)
            assert not ok, f"expected NO verdict from reply {reply!r}, got PASS: {msg}"

        # Ambiguous replies should default to NO (fail closed).
        for ambiguous in ["Maybe", "It depends", "I'm not sure", ""]:
            ok, _ = llm_judge(ScriptedJudge(ambiguous), "criterion")(c)
            assert not ok, f"ambiguous reply {ambiguous!r} should fail closed"


class TestEvalRunner:

    def test_all_pass(self):
        script = {
            "q1": {"content": "Paris"},
            "q2": {"content": "42", "tool_calls": [ToolCall(name="calc", args={}, result="42")]},
        }
        cases = [
            EvalCase(name="c1", input="q1", assertions=[contains("Paris")]),
            EvalCase(name="c2", input="q2", assertions=[
                contains("42"), called_tool("calc"),
            ]),
        ]
        runner = EvalRunner(lambda: MockRunner(script=script), verbose=False)
        report = runner.run(cases)
        assert report.passed == 2
        assert report.failed == 0
        assert report.pass_rate == 1.0

    def test_partial_failure(self):
        cases = [
            EvalCase(name="c1", input="q", assertions=[contains("x")]),
            EvalCase(name="c2", input="q", assertions=[contains("y")]),
        ]
        # MockRunner without a script returns "answer to: q" which has neither.
        runner = EvalRunner(lambda: MockRunner(), verbose=False)
        report = runner.run(cases)
        assert report.passed == 0
        assert report.failed == 2

    def test_report_summary_shape(self):
        cases = [EvalCase(name="c", input="q", assertions=[])]
        report = EvalRunner(lambda: MockRunner(), verbose=False).run(cases)
        summary = report.summary()
        assert "1/1 passed" in summary
        assert "100.0%" in summary

    def test_to_dict_shape(self):
        cases = [EvalCase(name="c", input="q", assertions=[])]
        report = EvalRunner(lambda: MockRunner(), verbose=False).run(cases)
        d = report.to_dict()
        for key in [
            "passed", "failed", "total", "pass_rate",
            "total_duration_sec", "total_input_tokens",
            "results",
        ]:
            assert key in d

    def test_runner_factory_exception_captured(self):
        def bad_factory():
            raise RuntimeError("oops")

        cases = [EvalCase(name="c", input="q", assertions=[])]
        report = EvalRunner(bad_factory, verbose=False).run(cases)
        assert report.failed == 1
        assert report.results[0].error is not None


class TestCaseLoaders:

    def test_load_case_from_dict(self):
        case = load_case_from_dict({
            "name": "t", "input": "q?",
            "expected_substrings": ["a", "b"],
            "forbidden_substrings": ["c"],
            "must_call_tools": ["mytool"],
            "matches_regex": [r"\d+"],
            "max_iterations": 4,
        })
        assert case.name == "t"
        # 2 contains + 1 not_contains + 1 called_tool + 1 regex + 1 max_iter = 6
        assert len(case.assertions) == 6

    def test_load_case_missing_required(self):
        with pytest.raises(ValueError):
            load_case_from_dict({"input": "q"})   # no name
        with pytest.raises(ValueError):
            load_case_from_dict({"name": "n"})    # no input

    def test_load_cases_from_dir(self, tmp_path):
        f = tmp_path / "cases.eval.json"
        f.write_text(json.dumps({
            "cases": [
                {"name": "a", "input": "q1"},
                {"name": "b", "input": "q2"},
            ]
        }))
        cases = load_cases_from_dir(tmp_path)
        assert len(cases) == 2
        assert cases[0].name == "a"
