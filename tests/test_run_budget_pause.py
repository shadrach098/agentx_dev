"""RunBudget.paused(): time spent waiting on the operator does not count against the deadline."""

import pytest

from agentx_dev.Runner.Persistence import PausableClock, RunBudget


class Base:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_a_pausable_clock_stands_still_while_paused():
    base = Base()
    clock = PausableClock(base)
    base.t = 10.0
    assert clock() == 10.0
    clock.pause()
    base.t = 50.0
    assert clock() == 10.0
    clock.resume()
    assert clock() == 10.0
    base.t = 55.0
    assert clock() == 15.0


def test_pause_and_resume_are_idempotent():
    base = Base()
    clock = PausableClock(base)
    clock.resume()                       # not paused: nothing happens
    clock.pause()
    base.t = 5.0
    clock.pause()                        # a second pause must not restart the window
    base.t = 9.0
    clock.resume()
    clock.resume()
    assert clock() == 0.0


def test_paused_extends_the_run_budget_and_its_capped_children():
    base = Base()
    budget = RunBudget.start(1.0, PausableClock(base))       # 60 s
    child = budget.capped(0.5)                                # 30 s
    base.t = 20.0
    with budget.paused():
        base.t = 520.0
    assert budget.remaining() == pytest.approx(40.0)
    assert child.remaining() == pytest.approx(10.0)
    assert not budget.expired()


def test_time_outside_a_pause_still_counts():
    base = Base()
    budget = RunBudget.start(1.0, PausableClock(base))
    with budget.paused():
        base.t = 100.0
    base.t = 161.0
    assert budget.expired()


def test_paused_resumes_even_if_the_block_raises():
    base = Base()
    budget = RunBudget.start(1.0, PausableClock(base))
    with pytest.raises(RuntimeError):
        with budget.paused():
            base.t = 30.0
            raise RuntimeError("boom")
    base.t = 40.0
    assert budget.remaining() == pytest.approx(50.0)


def test_paused_is_a_no_op_on_a_plain_clock():
    budget = RunBudget(deadline=100.0, clock=lambda: 1.0)
    with budget.paused():
        pass
    assert budget.remaining() == 99.0


def test_a_default_budget_gets_a_pausable_clock():
    assert isinstance(RunBudget.start(1.0)._clock, PausableClock)
