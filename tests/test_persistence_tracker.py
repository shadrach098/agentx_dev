from agentx_dev.Runner.Persistence import (
    ProgressLedger, REFLECTION_RUNGS, StuckTracker, _signature,
    reflection_message,
)


class TestLedger:
    def test_records_successes_and_failures(self):
        led = ProgressLedger(goal="fix the build")
        led.record("read_path", {"path": "a.py"}, "line one\nline two", False)
        led.record("run_python", {"code": "1/0"}, "ZeroDivisionError: division by zero", True, rung=2)
        assert led.done == ['read_path({"path": "a.py"}) -> line one']
        assert led.failed == ['run_python({"code": "1/0"}) -> ZeroDivisionError: division by zero [rung 2]']

    def test_entries_are_capped(self):
        led = ProgressLedger(max_entries=3)
        for i in range(10):
            led.record("t", {"i": i}, f"r{i}", False)
        assert len(led.done) == 3 and led.done[-1].endswith("r9")

    def test_render_has_every_section(self):
        led = ProgressLedger(goal="g")
        led.record("t", {}, "ok", False)
        led.record("u", {}, "boom", True)
        led.note_plan("try the other file")
        text = led.render()
        for needle in ("Goal: g", "Done (1 calls", "Failed (1 calls", "u({}) -> boom", "Next: try the other file"):
            assert needle in text, needle

    def test_render_when_empty(self):
        text = ProgressLedger(goal="g").render()
        assert "(nothing yet)" in text and "(none)" in text and "(not stated)" in text

    def test_to_dict_is_plain_data(self):
        led = ProgressLedger(goal="g")
        led.record("t", {}, "ok", False)
        d = led.to_dict()
        assert d == {"goal": "g", "done": ["t({}) -> ok"], "failed": [], "next": ""}

    def test_the_report_counts_every_call_even_past_the_cap(self):
        led = ProgressLedger(max_entries=3)
        for i in range(10):
            led.record("t", {"i": i}, "ok", False)
        for i in range(7):
            led.record("u", {"i": i}, "boom", True)
        text = led.render()
        assert len(led.done) == 3 and len(led.failed) == 3
        assert "Done (10 calls" in text and "Failed (7 calls" in text


class TestTracker:
    def test_three_errors_in_a_row_fire_even_with_different_calls(self):
        t = StuckTracker(3)
        assert t.observe("a", True) == (None, False)
        assert t.observe("b", True) == (None, False)
        reason, progressed = t.observe("c", True)
        assert reason == "3 tool errors in a row" and not progressed

    def test_three_identical_calls_fire(self):
        t = StuckTracker(3)
        assert t.observe("a", False) == (None, True)
        assert t.observe("b", False) == (None, True)
        assert t.observe("b", False) == (None, False)
        reason, progressed = t.observe("b", False)
        assert reason == "the same call repeated 3 times" and not progressed

    def test_distinct_successful_calls_never_fire_however_many(self):
        """Identical *results* are not a signal: a tool that always answers "ok" is healthy."""
        t = StuckTracker(3)
        for i in range(50):
            assert t.observe(f"write::{i}", False) == (None, True)

    def test_success_resets_the_error_streak(self):
        t = StuckTracker(3)
        t.observe("a", True)
        t.observe("b", True)
        assert t.observe("c", False) == (None, True)
        assert t.observe("d", True) == (None, False)      # streak restarted at 1

    def test_reset_clears_the_counters(self):
        t = StuckTracker(3)
        t.observe("a", True)
        t.observe("b", True)
        t.reset()
        assert t.observe("c", True) == (None, False)

    def test_signature_is_stable(self):
        assert _signature("t", {"b": 1, "a": 2}) == _signature("t", {"a": 2, "b": 1})


class TestReflectionLadder:
    def test_rungs_escalate(self):
        m1 = reflection_message(1, "3 tool errors in a row", "  - a")
        m2 = reflection_message(2, "3 tool errors in a row", "  - a failed thing")
        m3 = reflection_message(3, "x", "")
        m4 = reflection_message(4, "x", "")
        assert "recovery step 1" in m1 and "root cause" in m1
        assert "recovery step 2" in m2 and "a failed thing" in m2 and "NOT tried" in m2
        assert "read-only probe" in m3
        assert "still blocked" in m4

    def test_every_message_is_marked_as_framework_text(self):
        for rung in range(1, 5):
            assert reflection_message(rung, "r", "").startswith("\n\n[framework] You appear to be stuck (r)")

    def test_rungs_past_the_last_reuse_it(self):
        assert reflection_message(9, "r", "").endswith(REFLECTION_RUNGS[-1])
