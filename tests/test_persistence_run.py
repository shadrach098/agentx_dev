import asyncio
import json
from types import SimpleNamespace

import pytest

from agentx_dev import CostBudgetExceeded
from agentx_dev.Runner.Persistence import (
    BudgetExpired, NOTES_MARK, OUTCOME_OUT_OF_TIME, OUTCOME_STUCK, Persistence,
    PersistenceMixin, PersistentRun, RunStuck, accepts_budget, apply_persistence, is_transient,
)


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def make_run(cfg=None):
    clock = Clock()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.t += s

    async def asleep(s):
        sleep(s)

    run = PersistentRun(cfg or Persistence(max_minutes=10), "the goal",
                        clock=clock, sleep=sleep, asleep=asleep)
    return run, clock, sleeps


def history(turns, size=10):
    h = [{"role": "system", "content": "sys"}, {"role": "user", "content": "TASK"}]
    for i in range(turns):
        h.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
        h.append({"role": "tool", "name": "t", "tool_call_id": f"c{i}", "content": "r" * size})
    return h


def bind(run, h):
    run.bind(h, [], [], 1)
    return h


class TestTransient:
    @pytest.mark.parametrize("exc,expected", [
        (StatusError(429), True), (StatusError(503), True), (StatusError(408), True),
        (StatusError(401), False), (StatusError(400), False), (StatusError(404), False),
        (TimeoutError(), True), (ConnectionError(), True),
        (type("APITimeoutError", (Exception,), {})(), True),
        (type("RateLimitError", (Exception,), {})(), True),
        (ValueError("bug"), False), (KeyError("x"), False),
        (CostBudgetExceeded(1.0, 0.5), False),
    ])
    def test_classification(self, exc, expected):
        assert is_transient(exc) is expected


class TestPatientRetries:
    def test_retries_transient_errors_with_backoff(self):
        run, _, sleeps = make_run()
        attempts = []

        def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise StatusError(429)
            return "ok"

        assert run.patient(fn) == "ok"
        assert sleeps == [1.0, 2.0]

    def test_does_not_retry_auth_or_programming_errors(self):
        run, _, sleeps = make_run()
        with pytest.raises(StatusError):
            run.patient(lambda: (_ for _ in ()).throw(StatusError(401)))
        with pytest.raises(ValueError):
            run.patient(lambda: (_ for _ in ()).throw(ValueError("bug")))
        assert sleeps == []

    def test_gives_up_when_the_next_wait_would_pass_the_deadline(self):
        run, _, sleeps = make_run(Persistence(max_minutes=0.05))     # 3 seconds
        with pytest.raises(BudgetExpired):
            run.patient(lambda: (_ for _ in ()).throw(StatusError(503)))
        assert sleeps == [1.0]

    def test_patient_retries_can_be_switched_off(self):
        run, _, sleeps = make_run(Persistence(max_minutes=10, patient_retries=False))
        with pytest.raises(StatusError):
            run.patient(lambda: (_ for _ in ()).throw(StatusError(503)))
        assert sleeps == []

    def test_async_version(self):
        run, _, sleeps = make_run()
        attempts = []

        async def fn():
            attempts.append(1)
            if len(attempts) < 2:
                raise StatusError(503)
            return "ok"

        assert asyncio.run(run.apatient(fn)) == "ok"
        assert sleeps == [1.0]


class TestBudgetHook:
    def test_before_turn_raises_when_the_deadline_has_passed(self):
        run, clock, _ = make_run()
        bind(run, history(1))
        run.before_turn(SimpleNamespace())          # fine at t=0
        clock.t = 601
        with pytest.raises(BudgetExpired):
            run.before_turn(SimpleNamespace())

    def test_a_parent_budget_caps_the_run(self):
        from agentx_dev.Runner.Persistence import RunBudget
        clock = Clock()
        parent = RunBudget.start(1, clock)
        run = PersistentRun(Persistence(max_minutes=30), "g", budget=parent, clock=clock)
        assert run.budget.deadline == parent.deadline


class TestAfterTurn:
    def errors(self, run, h, n, start=0):
        for i in range(n):
            h.append({"role": "user", "content": "Error: boom"})
            run.after_turn(h, [("flaky", {"i": start + i}, "Error: boom", True)])

    def test_reflects_and_escalates_then_gets_stuck(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3, max_reflections=2))
        h = bind(run, history(0))
        self.errors(run, h, 3)
        assert "recovery step 1" in h[-1]["content"]
        assert run.drain() == [{"type": "reflect", "rung": 1, "reason": "3 tool errors in a row"}]
        self.errors(run, h, 3, start=3)
        assert "recovery step 2" in h[-1]["content"]
        with pytest.raises(RunStuck, match="3 tool errors in a row"):
            self.errors(run, h, 3, start=6)

    def test_progress_resets_the_ladder(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3, max_reflections=2))
        h = bind(run, history(0))
        self.errors(run, h, 3)
        assert run.rung == 1
        h.append({"role": "user", "content": "fine"})
        run.after_turn(h, [("steady", {"p": 1}, "all good", False)])
        assert run.rung == 0
        self.errors(run, h, 3, start=10)
        assert run.rung == 1 and "recovery step 1" in h[-1]["content"]

    def test_a_multi_call_turn_is_one_observation_batch(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3))
        h = bind(run, history(0))
        h.append({"role": "tool", "name": "a", "tool_call_id": "1", "content": "e"})
        h.append({"role": "tool", "name": "b", "tool_call_id": "2", "content": "e"})
        h.append({"role": "tool", "name": "c", "tool_call_id": "3", "content": "e"})
        run.after_turn(h, [("a", {}, "e", True), ("b", {}, "e", True), ("c", {}, "e", True)])
        assert run.rung == 1
        assert "recovery step 1" in h[-1]["content"] and "recovery step" not in h[-2]["content"]

    def test_a_repeated_batch_is_not_progress_and_fires_a_signal(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3))
        h = bind(run, history(0))
        batch = [("read", {"p": "a"}, "A", False), ("read", {"p": "b"}, "B", False)]
        run.after_turn(h, batch)
        run.after_turn(h, list(reversed(batch)))         # same calls, other order: same turn
        assert run.rung == 0 and run.drain() == []
        run.after_turn(h, batch)
        assert run.rung == 1
        assert run.drain() == [{"type": "reflect", "rung": 1,
                                "reason": "the same batch of calls repeated 3 times"}]

    def test_a_repeated_batch_does_not_reset_the_ladder(self):
        run, _, _ = make_run(Persistence(max_minutes=10, reflect_after=3))
        h = bind(run, history(0))
        batch = [("read", {"p": "a"}, "A", False), ("read", {"p": "b"}, "B", False)]
        run.after_turn(h, batch)
        self.errors(run, h, 3)
        assert run.rung == 1
        run.after_turn(h, batch)                          # differs from the previous turn: progress
        assert run.rung == 0
        self.errors(run, h, 3, start=10)
        run.after_turn(h, [("read", {"p": "c"}, "C", False), ("read", {"p": "d"}, "D", False)])
        run.after_turn(h, [("read", {"p": "c"}, "C", False), ("read", {"p": "d"}, "D", False)])
        assert run.rung == 0                              # the first one was progress

    def test_the_ledger_sees_every_call(self):
        run, _, _ = make_run()
        h = bind(run, history(0))
        run.after_turn(h, [("t", {"x": 1}, "result line", False)])
        assert run.ledger.done == ['t({"x": 1}) -> result line']


class TestCompactionHook:
    def test_compacts_when_over_the_threshold(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))
        model = SimpleNamespace(Initialize=lambda messages: "SUMMARY-TEXT")
        run.ledger.record("t", {}, "boom", True)
        run.before_turn(model)
        ev = run.drain()
        assert ev and ev[0]["type"] == "compact" and ev[0]["after_tokens"] < ev[0]["before_tokens"]
        assert NOTES_MARK in h[1]["content"] and "SUMMARY-TEXT" in h[1]["content"]
        assert "boom" in h[1]["content"]
        assert h[2]["role"] == "assistant"

    def test_summary_failure_falls_back_to_the_ledger(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))

        def boom(messages):
            raise RuntimeError("down")

        run.before_turn(SimpleNamespace(Initialize=boom))
        assert "no summary was available" in h[1]["content"]

    def test_a_cost_error_in_the_summary_call_propagates(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        bind(run, history(8, size=200))

        def over(messages):
            raise CostBudgetExceeded(1.0, 0.5)

        with pytest.raises(CostBudgetExceeded):
            run.before_turn(SimpleNamespace(Initialize=over))

    def test_does_not_thrash(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))
        calls = []
        model = SimpleNamespace(Initialize=lambda messages: calls.append(1) or "S")
        run.before_turn(model)
        h.append({"role": "tool", "name": "t", "tool_call_id": "c7", "content": "ok"})   # tiny growth
        run.before_turn(model)
        assert len(calls) == 1

    def test_async_version(self):
        run, _, _ = make_run(Persistence(max_minutes=10, compact_at_tokens=100, keep_recent_turns=4))
        h = bind(run, history(8, size=200))

        async def summarize(messages):
            return "ASYNC-SUMMARY"

        asyncio.run(run.abefore_turn(SimpleNamespace(async_initialize=summarize)))
        assert "ASYNC-SUMMARY" in h[1]["content"]


class TestReport:
    def test_out_of_time_report_and_completion(self):
        run, _, _ = make_run()
        h = bind(run, history(0))
        run.ledger.record("t", {}, "ok", False)
        run.finish(OUTCOME_OUT_OF_TIME)
        assert run.report().startswith("Stopped: the 10-minute time limit was reached.")
        c = run.exit_completion("GPT", "do it")
        assert c.outcome == "out_of_time" and c.content == run.report()
        assert c.progress["done"] == ["t({}) -> ok"] and c.query == "do it" and c.history == h

    def test_stuck_report_names_the_reason(self):
        run, _, _ = make_run()
        bind(run, history(0))
        run.finish(OUTCOME_STUCK, "3 tool errors in a row")
        assert "stuck after 4 recovery attempts (3 tool errors in a row)" in run.report()

    def test_exit_events_are_final_then_completion(self):
        run, _, _ = make_run()
        bind(run, history(0))
        run.finish(OUTCOME_STUCK, "x")
        events = list(run.exit_events("GPT", "q"))
        assert [e["type"] for e in events] == ["final", "completion"]
        assert events[0]["content"] == events[1]["completion"].content


class TestHelpers:
    def test_accepts_budget(self):
        assert accepts_budget(lambda q, _budget=None: 1)
        assert accepts_budget(lambda q, **kw: 1)
        assert not accepts_budget(lambda q: 1)

    def test_apply_persistence_sets_and_restores(self):
        p = Persistence()
        bare = SimpleNamespace(persistence=None)
        own = SimpleNamespace(persistence=Persistence(max_minutes=5))
        fake = SimpleNamespace()                       # no persistence attribute at all
        with apply_persistence([bare, own, fake], p):
            assert bare.persistence is p
            assert own.persistence.max_minutes == 5
            assert not hasattr(fake, "persistence")
        assert bare.persistence is None and own.persistence.max_minutes == 5

    def test_apply_persistence_with_none_is_a_noop(self):
        bare = SimpleNamespace(persistence=None)
        with apply_persistence([bare], None):
            assert bare.persistence is None


class TestMixin:
    class Runner(PersistenceMixin):
        def __init__(self):
            self.max_iterations = 4

    def test_persistence_overrides_max_iterations_and_restores_it(self):
        r = self.Runner()
        assert r.persistence is None
        r.persistence = Persistence(max_turns=50)
        assert r.max_iterations == 50
        r.persistence = None
        assert r.max_iterations == 4 and r.persistence is None

    def test_switching_between_two_settings_keeps_the_original_cap(self):
        r = self.Runner()
        r.persistence = Persistence(max_turns=50)
        r.persistence = Persistence(max_turns=70)
        assert r.max_iterations == 70
        r.persistence = None
        assert r.max_iterations == 4

    def test_rejects_the_wrong_type(self):
        with pytest.raises(TypeError, match="Persistence"):
            self.Runner().persistence = "yes"

    def test_persistence_suspends_the_tool_cache_and_restores_it(self):
        class FakeRegistry:
            def __init__(self):
                self.cache, self._cache_ttl = "CACHE", 300

            def configure_cache(self, cache, cache_ttl=None):
                self.cache, self._cache_ttl = cache, cache_ttl

        r = self.Runner()
        r.registry = FakeRegistry()
        r.persistence = Persistence()
        assert r.registry.cache is None
        r.persistence = Persistence(max_turns=5)          # switching settings keeps it suspended
        assert r.registry.cache is None
        r.persistence = None
        assert (r.registry.cache, r.registry._cache_ttl) == ("CACHE", 300)

    def test_apply_persistence_drives_a_real_mixin(self):
        r = self.Runner()
        with apply_persistence([r], Persistence(max_turns=9)):
            assert r.max_iterations == 9
        assert r.max_iterations == 4
