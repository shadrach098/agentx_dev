"""The Supervisor's public extension points: prepare_plan, run_step, spawn_specialist and planner_note, on both the
sync and the async Supervisor, and the model a planner-defined helper asks for (AgentSpec.model)."""

import asyncio
import json

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncSupervisor, SpawnConfig, SubtaskResult, Supervisor
from agentx_dev.SubAgents import AgentSpec, parse_agent_spec
from tests.conftest import MockModel, make_final
from tests.subagent_helpers import PLANNER_MARK, new_agent_step, plan_json, router, step


def specialist(answer="done"):
    return AgentRunner(model=MockModel(script=lambda messages: make_final(answer)), agent=AgentType.ReAct,
                       tools=[], verbose=False)


def agents():
    return {"reader": ("Reads things.", specialist("read it")), "writer": ("Writes things.", specialist("wrote it"))}


def run_sync(supervisor, task="the job"):
    return supervisor.run(task)


def run_async(supervisor, task="the job"):
    return asyncio.run(supervisor.run(task))


KINDS = [("sync", Supervisor, run_sync), ("async", AsyncSupervisor, run_async)]


@pytest.mark.parametrize("kind,cls,run", KINDS, ids=[k[0] for k in KINDS])
class TestTheHooks:
    def test_prepare_plan_sees_every_plan_and_can_change_it(self, kind, cls, run):
        seen = []

        class Mine(cls):
            def prepare_plan(self, plan):
                seen.append([s["id"] for s in plan])
                return [s for s in plan if s["id"] != "b"]            # drop a step

        model = router(plans=[plan_json(step("a", "reader", "read"), step("b", "writer", "write"))])
        result = run(Mine(model=model, agents=agents(), verbose=False))
        assert seen == [["a", "b"]]
        assert [sub.agent for sub in result.subtasks] == ["reader"]

    def test_run_step_wraps_every_step_and_can_skip_one(self, kind, cls, run):
        wrapped = []

        if kind == "sync":
            class Mine(cls):
                def run_step(self, agent_name, query, run_it):
                    wrapped.append(agent_name)
                    if agent_name == "writer":
                        return SubtaskResult(agent=agent_name, query=query, content="", error="skipped by me",
                                             skipped=True)
                    return run_it()
        else:
            class Mine(cls):
                async def run_step(self, agent_name, query, run_it):
                    wrapped.append(agent_name)
                    if agent_name == "writer":
                        return SubtaskResult(agent=agent_name, query=query, content="", error="skipped by me",
                                             skipped=True)
                    return await run_it()

        model = router(plans=[plan_json(step("a", "reader", "read"), step("b", "writer", "write", deps=["a"]))])
        result = run(Mine(model=model, agents=agents(), verbose=False))
        assert sorted(wrapped) == ["reader", "writer"]
        by_agent = {sub.agent: sub for sub in result.subtasks}
        assert by_agent["reader"].content == "read it" and by_agent["writer"].error == "skipped by me"

    def test_spawn_specialist_builds_the_helper_a_plan_defines_with_its_model(self, kind, cls, run):
        built = []

        class Mine(cls):
            def spawn_specialist(self, spec):
                built.append((spec.name, spec.tools, spec.model))
                self.agents[spec.name] = (spec.instructions, specialist("made by me"))
                return spec.name, ""

        plan = plan_json({"id": "s1", "query": "check", "new_agent": {"name": "checker", "instructions": "Check it.",
                                                                      "tools": ["read_path"], "model": "small-model"}})
        model = router(plans=[plan])
        result = run(Mine(model=model, agents=agents(), verbose=False, spawn_config=SpawnConfig(enabled=True)))
        assert built == [("checker", ("read_path",), "small-model")]
        assert result.subtasks[0].agent == "checker" and result.subtasks[0].content == "made by me"

    def test_a_refusal_from_spawn_specialist_fails_the_step_with_the_reason(self, kind, cls, run):
        class Mine(cls):
            def spawn_specialist(self, spec):
                return None, "not today"

        model = router(plans=[plan_json(new_agent_step("s1", "checker", "Check it.", query="check"))])
        result = run(Mine(model=model, agents=agents(), verbose=False, spawn_config=SpawnConfig(enabled=True)))
        assert "not today" in (result.subtasks[0].error or "")

    def test_planner_note_reaches_the_planner(self, kind, cls, run):
        model = router(plans=[plan_json(step("a", "reader", "read"))])
        run(cls(model=model, agents=agents(), verbose=False, planner_note="\n\nPREFER THE READER."))
        assert "PREFER THE READER." in model.planner_prompts()[0]


class TestTheModelOfAPlannedHelper:
    def test_it_is_read_cleaned_and_kept(self):
        spec = parse_agent_spec({"name": "x", "instructions": "do", "tools": [], "model": "  gpt-5.5\x07 "})
        assert spec.model == "gpt-5.5"
        assert parse_agent_spec({"name": "x", "instructions": "do"}).model == ""
        assert parse_agent_spec({"name": "x", "instructions": "do", "model": 5}).model == ""

    def test_the_plan_checker_keeps_it(self):
        from agentx_dev.Supervisor import _sanitize_plan
        plan = [{"id": "a", "query": "q", "new_agent": {"name": "x", "instructions": "do", "model": "m1"}}]
        cleaned, _ = _sanitize_plan(plan)
        assert cleaned[0]["new_agent"]["model"] == "m1"

    def test_the_default_builder_ignores_it(self):
        assert AgentSpec("x", "do").model == ""
