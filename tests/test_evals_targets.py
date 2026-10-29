"""The eval harness on everything the framework runs (sync and async runners, both Supervisors), with the checks
added since 3.1: the outcome, structured output, skills, a Supervisor's steps, failed tool calls; repeats, tags,
counted tokens and comparing two reports."""

import asyncio
import json

from pydantic import BaseModel

from agentx_dev import (AgentRunner, AgentType, AsyncAgentRunner, AsyncSupervisor, EvalCase, EvalRunner,
                        StandardTool, Supervisor, ToolCall, compare_reports, contains, outcome_is, output_field,
                        subtasks_ok, used_agent, used_skill)
from agentx_dev.Evals import _cli, counted, load_case_from_dict
from tests.conftest import MockModel, make_final, make_react_response
from tests.subagent_helpers import plan_json, router, step


def ok(x):
    return "fine"


def bad(x):
    raise ValueError("boom")


def runner(cls=AgentRunner, answer="Paris", **kw):
    return cls(model=MockModel(script=[make_react_response("bad_tool", {"input": "x"}),
                                       make_react_response("ok_tool", {"input": "y"}), make_final(answer)]),
               agent=AgentType.ReAct, tools=[StandardTool(name="ok_tool", func=ok, description="ok"),
                                             StandardTool(name="bad_tool", func=bad, description="fails")],
               verbose=False, **kw)


def specialist(answer):
    return AgentRunner(model=MockModel(script=lambda messages: make_final(answer)), agent=AgentType.ReAct, tools=[],
                       verbose=False)


def supervisor(cls=Supervisor):
    model = router(plans=[plan_json(step("a", "reader", "read"))], synth="The answer is Paris")
    return cls(model=model, agents={"reader": ("Reads.", specialist("read it"))}, verbose=False)


CASE = EvalCase(name="capital", input="capital of France?", assertions=[contains("Paris"), outcome_is("done")])


class TestEveryKindOfRunner:
    def test_a_sync_runner_counts_the_tool_call_it_recovered_from(self):
        report = EvalRunner(runner, verbose=False).run([CASE])
        result = report.results[0]
        assert result.passed and result.tool_errors == ["bad_tool"] and result.tool_error_count == 1
        assert result.model_calls == 3 and result.counted_tokens == counted(30, 15, 0)

    def test_an_async_runner(self):
        result = EvalRunner(lambda: runner(AsyncAgentRunner), verbose=False).run([CASE]).results[0]
        assert result.passed and result.tool_errors == ["bad_tool"]

    def test_an_async_runner_from_inside_a_running_event_loop(self):
        async def in_a_notebook():
            return EvalRunner(lambda: runner(AsyncAgentRunner), verbose=False).run([CASE])
        assert asyncio.run(in_a_notebook()).passed == 1

    def test_both_supervisors_with_their_own_checks(self):
        case = EvalCase(name="sup", input="capital?", assertions=[contains("Paris"), used_agent("reader"),
                                                                  subtasks_ok()])
        for cls in (Supervisor, AsyncSupervisor):
            result = EvalRunner(lambda: supervisor(cls), verbose=False).run([case]).results[0]
            assert result.passed, (cls.__name__, result.assertion_lines, result.error)
            assert result.input_tokens > 0                # the planner's and the specialist's tokens

    def test_max_tool_errors_fails_a_run_with_too_many(self):
        case = EvalCase(name="strict", input="q", assertions=[contains("Paris")], max_tool_errors=0)
        result = EvalRunner(runner, verbose=False).run([case]).results[0]
        assert not result.passed and any("max_tool_errors(0): FAIL -- 1 (bad_tool)" in line
                                         for line in result.assertion_lines)


class TestTheNewChecks:
    def test_outcome_is(self):
        result = EvalRunner(lambda: runner(), verbose=False).run(
            [EvalCase(name="o", input="q", assertions=[outcome_is("stuck")])]).results[0]
        assert not result.passed and "FAIL -- done" in result.assertion_lines[0]

    def test_output_field_on_structured_output(self):
        class City(BaseModel):
            name: str
            country: str

        def build():
            model = MockModel(script=[make_final('{"name": "Paris", "country": "France"}')] * 3)
            return AgentRunner(model=model, agent=AgentType.ReAct, tools=[], verbose=False, output_schema=City)
        case = EvalCase(name="s", input="q", assertions=[output_field("name", "Paris"),
                                                         output_field("country", check=lambda v: v.isalpha())])
        result = EvalRunner(build, verbose=False).run([case]).results[0]
        assert result.passed, result.assertion_lines
        missing = output_field("name")(type("C", (), {"output": None})())
        assert not missing[0] and "no structured output" in missing[1]

    def test_used_skill(self):
        completion = type("C", (), {"tool_calls": [ToolCall(name="use_skill", args={"Input": {"name": "Audit"}},
                                                            result="...")]})()
        assert used_skill("audit")(completion)[0] and not used_skill("other")(completion)[0]

    def test_json_cases_take_the_new_fields(self):
        case = load_case_from_dict({"name": "j", "input": "q", "outcome": ["done", "partial"],
                                    "used_skills": ["audit"], "used_agents": ["reader"], "subtasks_ok": True,
                                    "output_fields": {"name": "Paris"}, "max_tool_errors": 2, "tags": ["smoke"]})
        assert len(case.assertions) == 5 and case.max_tool_errors == 2 and case.tags == ["smoke"]


class TestRepeatsTagsAndComparing:
    def test_repeat_runs_each_case_again_and_the_summary_counts_passes(self):
        report = EvalRunner(runner, verbose=False, repeat=3).run([CASE])
        assert report.total == 3 and [r.round for r in report.results] == [1, 2, 3]
        assert "[3/3] capital" in report.summary() and "counted:" in report.summary()

    def test_tags_pick_cases(self):
        cases = [EvalCase(name="a", input="q", tags=["smoke"]), EvalCase(name="b", input="q", tags=["slow"])]
        report = EvalRunner(runner, verbose=False, tags=["smoke"]).run(cases)
        assert [r.case.name for r in report.results] == ["a"]
        assert [r.case.name for r in EvalRunner(runner, verbose=False).run(cases, tags=["slow"]).results] == ["b"]

    def test_compare_two_reports(self, tmp_path, capsys):
        a = EvalRunner(runner, verbose=False).run([CASE]).to_dict()
        a["results"][0]["counted_tokens"] = 40
        b = json.loads(json.dumps(a))
        b["results"][0]["counted_tokens"] = 20
        b["results"][0]["passed"] = False
        text = compare_reports(a, b)
        assert "capital" in text and "1/1 -> 0/1" in text and "-50%" in text
        for name, report in (("a.json", a), ("b.json", b)):
            (tmp_path / name).write_text(json.dumps(report), encoding="utf-8")
        assert _cli(["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 0
        assert "-50%" in capsys.readouterr().out

    def test_the_report_dict_carries_the_new_fields(self):
        row = EvalRunner(runner, verbose=False).run([CASE]).to_dict()["results"][0]
        for key in ("round", "counted_tokens", "model_calls", "tool_errors", "outcome", "tags"):
            assert key in row
        assert row["tool_errors"] == ["bad_tool"] and row["outcome"] == "done"
