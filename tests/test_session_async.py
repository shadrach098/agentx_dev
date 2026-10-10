"""AsyncSupervisor(ask_when_done=..., follow_up=...): the same session, awaited."""

import asyncio
import json

from agentx_dev import AsyncSupervisor, CostBudgetExceeded, Persistence
from agentx_dev import Operator as op
from agentx_dev.Operator import NoChannel
from agentx_dev.Supervisor import SpawnConfig
from tests.conftest import MockModel, make_final
from tests.memory_helpers import FakeStore, answer_hit
from tests.subagent_helpers import PLANNER_MARK, ScriptedRunner, new_agent_step, plan_json, router, step

PLAN = plan_json(step("s1", "worker", "do it"))
ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})


def asker_of(*replies):
    queue = list(replies)
    seen = []

    def ask(question):
        seen.append(question)
        item = queue.pop(0) if queue else None
        if isinstance(item, BaseException):
            raise item
        return item
    ask.seen = seen
    return ask


def checkpoints(ask):
    return [q for q in ask.seen if "What should I do next?" in q]


def sup(model, ask, **kw):
    kw.setdefault("verbose", False)
    return AsyncSupervisor(model=model, agents={"worker": ("does the work", ScriptedRunner())},
                           ask_user=ask, **kw)


def run(supervisor, task="task"):
    return asyncio.run(supervisor.run(task))


class TestOff:
    def test_nothing_is_asked_and_the_result_is_unchanged(self):
        ask = asker_of("never used")
        result = run(sup(router(plans=[PLAN]), ask))
        assert ask.seen == [] and result.next_step is None and result.rounds == []


class TestAskWhenDone:
    def test_one_round_then_the_question(self):
        ask = asker_of("write the emails")
        model = router(plans=[PLAN], synth="Final.")
        result = run(sup(model, ask, ask_when_done=True))
        shown = checkpoints(ask)[0]
        assert "Final." in shown and "Outcome: done." in shown
        assert result.next_step == "write the emails" and result.rounds == []
        assert len(model.planner_prompts()) == 1
        assert [r["source"] for r in result.asked] == ["supervisor"]

    def test_an_asker_that_raises_ends_quietly(self):
        result = run(sup(router(plans=[PLAN]), asker_of(NoChannel()), follow_up=True))
        assert result.next_step is None and result.asked[-1]["reason"] == "no_channel"

    def test_it_asks_whatever_the_outcome(self):
        failing = ScriptedRunner(("gave up", "stuck"), ("gave up", "stuck"))
        ask = asker_of("try something else")
        s = AsyncSupervisor(model=router(plans=[PLAN]), agents={"worker": ("w", failing)}, verbose=False,
                            ask_user=ask, ask_when_done=True)
        result = run(s)
        assert result.outcome != "done" and "Outcome: " + result.outcome in checkpoints(ask)[0]


class TestFollowUp:
    def test_two_rounds_then_done(self):
        ask = asker_of("compare pricing", "done")
        model = router(plans=[PLAN, PLAN], synth="Final.")
        result = run(sup(model, ask, follow_up=True), "Research X")
        second = model.planner_prompts()[1]
        assert "WHAT HAS HAPPENED SO FAR" in second and "The operator now asks: compare pricing" in second
        assert result.query == "compare pricing" and result.next_step == "done"
        assert [r["round"] for r in result.rounds] == [1, 2]

    def test_events_end_with_one_completion_and_mark_the_rounds(self):
        async def collect():
            s = sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True)
            return [e async for e in s.astream("task")]
        events = asyncio.run(collect())
        types = [e["type"] for e in events]
        assert types.count("completion") == 1 and types[-1] == "completion"
        assert types.count("round") == 1 and types.count("checkpoint") == 2
        assert [e["source"] for e in events if e["type"] == "question"] == ["supervisor", "supervisor"]

    def test_the_cap_stops_the_questions(self):
        ask = asker_of("a", "b", "c")
        model = router(plans=[PLAN, PLAN, PLAN])
        result = run(sup(model, ask, follow_up=True, max_follow_ups=2))
        assert len(model.planner_prompts()) == 3 and len(checkpoints(ask)) == 2
        assert len(result.rounds) == 3 and result.next_step is None

    def test_a_question_answered_in_round_one_is_not_asked_again(self):
        ask = asker_of("Notion, Obsidian, Coda", "more please", "done")
        model = router(plans=[ASK_PLAN, PLAN, ASK_PLAN, PLAN])
        result = run(sup(model, ask, follow_up=True), "Compare our competitors")
        assert len([q for q in ask.seen if "Which three competitors" in q]) == 1
        assert "Notion, Obsidian, Coda" in model.planner_prompts()[-1] and len(result.rounds) == 2

    def test_only_round_one_is_saved_to_memory(self):
        store = FakeStore()
        run(sup(router(plans=[PLAN, PLAN], synth="Final."), asker_of("more", "done"), follow_up=True,
                memory=store))
        assert store.kinds_added().count("run_result") == 1

    def test_no_round_starts_after_the_cost_budget_is_spent(self):
        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "Final."
        model = MockModel(script=script)
        model.planner_prompts = lambda: [str(c[0]["content"]) for c in model.calls
                                         if PLANNER_MARK in str(c[0]["content"])]
        ask = asker_of("try again")
        result = run(sup(model, ask, follow_up=True, persistence=Persistence(max_minutes=5)))
        assert result.outcome == "out_of_budget" and "cost limit reached" in checkpoints(ask)[0]
        assert result.next_step == "try again" and result.rounds == []
        assert len(model.planner_prompts()) == 1

    def test_an_async_asker_is_awaited_through_the_checkpoint(self):
        seen = []

        async def ask(question):
            seen.append(question)
            await asyncio.sleep(0)
            return "write the emails"
        result = run(sup(router(plans=[PLAN], synth="Final."), ask, ask_when_done=True))
        assert len(seen) == 1 and "Final." in seen[0] and "What should I do next?" in seen[0]
        assert result.next_step == "write the emails" and result.asked[0]["answered"] is True

    def test_an_async_asker_can_drive_a_follow_up_round(self):
        replies = ["compare pricing", "done"]

        async def ask(question):
            return replies.pop(0)
        model = router(plans=[PLAN, PLAN], synth="Final.")
        result = run(sup(model, ask, follow_up=True))
        assert len(model.planner_prompts()) == 2 and result.next_step == "done"
        assert [r["round"] for r in result.rounds] == [1, 2]

    def test_a_huge_answer_is_cut_in_the_prompt_but_not_in_the_result(self):
        ask = asker_of("go")
        result = run(sup(router(plans=[PLAN], synth="A" * 10000), ask, ask_when_done=True))
        assert len(result.content) == 10000
        assert len(checkpoints(ask)[0]) < 3300 and "(full text in result.content)" in checkpoints(ask)[0]

    def test_a_blank_reply_is_none(self):
        result = run(sup(router(plans=[PLAN]), asker_of("   "), follow_up=True))
        assert result.next_step is None and result.asked[-1]["reason"] == "declined"

    def test_it_uses_no_question_slot(self):
        result = run(sup(router(plans=[PLAN]), asker_of("thanks"), ask_when_done=True, max_questions=0))
        assert result.next_step == "thanks"

    def test_a_missing_channel_is_a_no_answer_not_an_assertion_error(self):
        s = sup(router(plans=[PLAN]), asker_of("never used"), ask_when_done=True)
        s.ask_user = None
        result = run(s)
        assert result.next_step is None and result.rounds == [] and result.content

    def test_a_reply_over_the_cap_reaches_the_next_round_cut_to_2000(self):
        model = router(plans=[PLAN, PLAN])
        run(sup(model, asker_of("j" * 5000, "done"), follow_up=True))
        second = model.planner_prompts()[1]
        assert "j" * 2000 in second and "j" * 2001 not in second

    def test_a_reply_that_matches_memory_is_not_shown_twice_in_round_two(self):
        question = "Which three competitors should I compare?"
        store = FakeStore([answer_hit(question, "Notion, Obsidian, Coda")])
        model = router(plans=[ASK_PLAN, PLAN, PLAN])
        run(sup(model, asker_of("more", "done"), follow_up=True, memory=store), "Compare")
        second = model.planner_prompts()[-1]
        assert second.count("Notion, Obsidian, Coda") == 1 and "FROM MEMORY" not in second

    def test_the_built_in_prompt_puts_the_question_first(self, monkeypatch):
        shown = []

        def fake(text, prefix="", timeout=None):
            shown.append(text)
            return "done"
        monkeypatch.setattr(op, "builtin_asker", fake)
        s = AsyncSupervisor(model=router(plans=[PLAN], synth="Final."), ask_user=True, verbose=False,
                            agents={"worker": ("does the work", ScriptedRunner())}, ask_when_done=True)
        result = run(s)
        assert shown[0].startswith('What should I do next? (blank or "done" to finish)')
        assert shown[0].endswith("Final.") and result.next_step == "done"


SPAWN_WEB = SpawnConfig(enabled=True, capabilities={"web"})
SPAWN_PLAN = plan_json(new_agent_step("s1", "scout", "Find things.", ["web"]), step("s2", "worker", deps=["s1"]))


class AsyncWorker(ScriptedRunner):
    async def Initialize(self, query, _budget=None):
        return ScriptedRunner.Initialize(self, query, _budget)


class TestEarlyClose:
    def test_closing_the_session_stream_mid_round_releases_the_per_run_state(self):
        ask = asker_of("more")
        model = router(plans=[SPAWN_PLAN, SPAWN_PLAN], sub=lambda m: make_final("found"))
        s = AsyncSupervisor(model=model, agents={"worker": ("does the work", AsyncWorker())}, ask_user=ask,
                            verbose=False, follow_up=True, spawn_config=SPAWN_WEB)
        base = s.agents

        async def scenario():
            gen = s.astream("task")
            async for ev in gen:
                if ev["type"] == "dispatch":
                    assert s.agents is not base             # the run's own registry copy is live here
                    break
            await gen.aclose()
            return s.agents                                 # must already be released when aclose() returns

        assert asyncio.run(scenario()) is base
        assert list(base) == ["worker"] and checkpoints(ask) == []
