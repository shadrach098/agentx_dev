# Ask When Done and Follow Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Supervisor` and `AsyncSupervisor` take `ask_when_done`, `follow_up` and `max_follow_ups`: when a round's answer is ready the framework asks the operator what to do next itself, returns the reply in `result.next_step`, and with `follow_up` runs another round on the reply.

**Architecture:** The body of `stream()` / `astream()` becomes a private round generator (`_stream_round` / `_astream_round`, logic unchanged apart from two parameters). The public `stream()` / `astream()` loop over rounds and ask through two new `OperatorChannel` methods (`checkpoint`, `acheckpoint`). With both flags off the public methods delegate to one round, so nothing changes.

**Tech Stack:** Python 3.10+, pydantic v2 (`SupervisorResult`), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-09-ask-when-done-follow-up-design.md`

## Global Constraints

- Target version 3.6.2 (unreleased, additive). The version in `pyproject.toml` is already `3.6.2`; do not change it.
- New constructor parameters go at the END of both signatures: `ask_when_done: bool = False`, `follow_up: bool = False`, `max_follow_ups: int = 5`.
- `follow_up=True` implies `ask_when_done`. Either flag with `ask_user` of `None` or `False` raises `ValueError` at construction.
- `max_follow_ups` is clamped to at least 0. The default is 5.
- Stop words (case-insensitive, surrounding whitespace and trailing `.` / `!` ignored): `done`, `stop`, `quit`, `exit`, `no`, `nothing`, `that's all`. A blank reply or no reply also ends the loop.
- The checkpoint prompt shows at most 3,000 characters of the answer, then `(full text in result.content)`. The operator's reply is capped at 2,000 characters by the channel's existing answer cap.
- The checkpoint does not consume `max_questions`, is not subject to the per-agent limit, is not de-duplicated, and is never looked up in or written to memory.
- Only round 1's result is saved to memory. Operator answers are saved as today.
- A round after `out_of_budget` never starts; the checkpoint still asks and returns the reply.
- No question is asked after the last allowed follow-up round (round number `1 + max_follow_ups`, only when that is greater than 1).
- `SupervisorResult` gets `next_step: Optional[str] = None` and `rounds: List[Dict[str, Any]] = []` (filled only once a follow-up round has run).
- Commit messages: plain conventional commits. NEVER add a `Co-Authored-By` line (the user is the sole contributor and has said so).
- Edit files with the Edit tool. Do NOT edit code through a Python script that embeds `\n` inside string literals: the backslashes get mangled (this has already broken the repo twice). Mixed line endings are normal here; the Edit tool preserves them.
- Run the whole suite (`python -m pytest tests -q`) before every commit that touches `agentx_dev/`. It currently passes (1092 passed, 4 skipped).

## Review Focus

Failure modes the spec implies that a person using this will meet. Each has a test in the task named in brackets.

1. The asker raises, times out or reports no channel at the checkpoint: `next_step` is `None`, the loop ends, nothing raises. [Task 1 channel tests, Task 3]
2. The operator types a stop word in another case or with punctuation (`Done!`, ` QUIT. `, `That’s all` with a curly apostrophe). [Task 1]
3. The same question was answered in round 1 and the round-2 planner asks it again: the operator is not prompted twice. [Task 1 `seed_from`, Task 3]
4. A very long final answer: the prompt is cut, `result.content` is not. [Task 1, Task 3]
5. A round ends `out_of_budget` and the operator replies with an instruction: no new round starts, the reply is still returned. [Task 3, Task 4]

---

### Task 1: Channel pieces (`Operator.py`)

**Files:**
- Modify: `agentx_dev/Operator.py`
- Test: `tests/test_checkpoint_channel.py` (create)

**Interfaces:**
- Produces (module level): `CHECKPOINT_SOURCE = "supervisor"`, `CHECKPOINT_QUESTION = "What should I do next?"`, `CHECKPOINT_SHOWN_CHARS = 3000`, `is_stop_reply(text: Optional[str]) -> bool`, `checkpoint_text(answer: Any, outcome_line: str) -> str`.
- Produces (on `OperatorChannel`): `seed_from(previous: OperatorChannel) -> None`, `checkpoint(shown: str) -> Reply`, `async acheckpoint(shown: str) -> Reply`.
- Consumes: existing `Reply`, `_ask_sync`, `_ask_async`, `_lock`, `emit`, `records`, `_say`, `_answers`, `_seen`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_checkpoint_channel.py`:

```python
"""The checkpoint: the question the framework itself asks when a round is done."""

import asyncio
from contextlib import contextmanager

import pytest

from agentx_dev import Operator as op
from agentx_dev.Operator import (
    CHECKPOINT_QUESTION, CHECKPOINT_SOURCE, NoChannel, OperatorChannel, checkpoint_text, is_stop_reply,
)


class TestStopReply:
    @pytest.mark.parametrize("text", [None, "", "   ", "done", "Done!", " QUIT. ", "stop", "exit", "no",
                                      "Nothing", "That's all", "that’s all."])
    def test_these_end_the_session(self, text):
        assert is_stop_reply(text)

    @pytest.mark.parametrize("text", ["write the emails", "do it again", "not done", "nothing special, check pricing"])
    def test_these_are_instructions(self, text):
        assert not is_stop_reply(text)


class TestCheckpointText:
    def test_answer_then_outcome_then_the_question(self):
        text = checkpoint_text("Answer body", "Outcome: done.")
        assert text == 'Answer body\n\nOutcome: done.\nWhat should I do next? (blank or "done" to finish)'

    def test_a_long_answer_is_cut_and_says_where_the_rest_is(self):
        text = checkpoint_text("x" * 5000, "Outcome: done.")
        assert text.count("x") == 3000 and "(full text in result.content)" in text

    def test_an_empty_answer_leaves_just_the_outcome_and_question(self):
        assert checkpoint_text("", "Outcome: stuck.").startswith("Outcome: stuck.")


def channel(asker, **kw):
    return OperatorChannel.create(asker, **kw)


class TestCheckpoint:
    def test_it_asks_and_records_the_reply(self):
        seen = []
        ch = channel(lambda q: seen.append(q) or "write the emails")
        reply = ch.checkpoint("body\n\nOutcome: done.\nWhat should I do next?")
        assert reply.answered and reply.text == "write the emails"
        assert seen == ["body\n\nOutcome: done.\nWhat should I do next?"]
        assert ch.records == [{"source": CHECKPOINT_SOURCE, "question": CHECKPOINT_QUESTION,
                               "answered": True, "reason": None, "deduped": False}]
        events = ch.drain()
        assert [e["type"] for e in events] == ["question", "answer"]
        assert events[0]["source"] == "supervisor" and events[1]["answered"] is True

    def test_it_uses_no_question_slot_and_is_not_an_operator_fact(self):
        ch = channel(lambda q: "go", max_questions=1)
        ch.checkpoint("x")
        ch.checkpoint("y")                     # twice, and not limited, not de-duplicated
        assert ch.asked == 0 and ch.remaining() == 1 and not ch.has_answers()
        assert len(ch.records) == 2

    def test_it_never_touches_memory(self):
        class Boom:
            def __getattr__(self, name):
                raise AssertionError(f"memory.{name} must not be used by the checkpoint")
        ch = channel(lambda q: "go")
        ch.memory = Boom()
        assert ch.checkpoint("x").text == "go"

    def test_the_persistent_deadline_is_paused_while_the_operator_thinks(self):
        class Budget:
            paused_calls = 0

            @contextmanager
            def paused(self):
                Budget.paused_calls += 1
                yield
        ch = channel(lambda q: "go")
        ch.budget = Budget()
        ch.checkpoint("x")
        assert Budget.paused_calls == 1

    @pytest.mark.parametrize("raised,reason", [(NoChannel(), "no_channel"), (RuntimeError("boom"), "error")])
    def test_an_asker_that_cannot_answer_is_a_no_answer(self, raised, reason):
        def asker(q):
            raise raised
        reply = channel(asker).checkpoint("x")
        assert not reply.answered and reply.reason == reason

    def test_a_blank_reply_is_declined(self):
        reply = channel(lambda q: "   ").checkpoint("x")
        assert not reply.answered and reply.reason == "declined"

    def test_the_built_in_prompt_is_labelled_supervisor(self, monkeypatch):
        seen = {}

        def fake(shown, prefix="", timeout=None):
            seen["prefix"] = prefix
            return "go"
        monkeypatch.setattr(op, "builtin_asker", fake)
        OperatorChannel.create(True).checkpoint("x")
        assert seen["prefix"] == "[supervisor]"

    def test_the_async_form_does_the_same(self):
        ch = channel(lambda q: "go", is_async=True)
        reply = asyncio.run(ch.acheckpoint("x"))
        assert reply.text == "go" and ch.records[0]["source"] == "supervisor"
        failed = channel(lambda q: (_ for _ in ()).throw(NoChannel()), is_async=True)
        assert asyncio.run(failed.acheckpoint("x")).reason == "no_channel"


class TestSeedFrom:
    def test_a_later_round_knows_the_answers_and_does_not_ask_them_again(self):
        first = channel(lambda q: "Notion")
        first.ask("Which competitors?", source="planner")
        calls = []
        second = channel(lambda q: calls.append(q) or "should not be asked")
        second.seed_from(first)
        assert second.has_answers() and "Notion" in second.answers_block()
        again = second.ask("Which competitors?", source="planner")
        assert again.text == "Notion" and again.deduped and calls == []

    def test_a_question_nobody_answered_may_be_asked_again(self):
        first = channel(lambda q: None)
        first.ask("Which competitors?", source="planner")
        calls = []
        second = channel(lambda q: calls.append(q) or "Notion")
        second.seed_from(first)
        assert second.ask("Which competitors?", source="planner").text == "Notion" and len(calls) == 1

    def test_the_question_budget_starts_over(self):
        first = channel(lambda q: "a", max_questions=1)
        first.ask("one?", source="planner")
        second = channel(lambda q: "b", max_questions=1)
        second.seed_from(first)
        assert second.remaining() == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_checkpoint_channel.py -q`
Expected: FAIL at collection with `ImportError: cannot import name 'CHECKPOINT_QUESTION'`.

- [ ] **Step 3: Add the constants and the two module-level functions**

In `agentx_dev/Operator.py`, directly after the line `PLANNER_SOURCE = "planner"`, add:

```python
CHECKPOINT_SOURCE = "supervisor"                    # who asks "what next?" at the end of a round
CHECKPOINT_QUESTION = "What should I do next?"
CHECKPOINT_SHOWN_CHARS = 3000                       # how much of the answer the checkpoint prompt shows
_STOP_WORDS = frozenset({"done", "stop", "quit", "exit", "no", "nothing", "that's all"})
```

Directly after the function `_shown` (it ends with `return f"{question}\n(context: {context})" if context else question`), add:

```python
def is_stop_reply(text: Optional[str]) -> bool:
    """True when ``text`` ends a follow-up session: no reply, a blank one, or a stop word
    ("Done!", " quit. ", "That's all")."""
    if text is None:
        return True
    word = text.strip().lower().replace("’", "'").rstrip(".!").strip()
    return word == "" or word in _STOP_WORDS


def checkpoint_text(answer: Any, outcome_line: str) -> str:
    """The one string the asker receives at the end of a round: the answer (cut at
    ``CHECKPOINT_SHOWN_CHARS``), how the round ended, and the question."""
    shown = str(answer or "").strip()
    if len(shown) > CHECKPOINT_SHOWN_CHARS:
        shown = shown[:CHECKPOINT_SHOWN_CHARS].rstrip() + "\n(full text in result.content)"
    head = f"{shown}\n\n" if shown else ""
    return f'{head}{outcome_line}\n{CHECKPOINT_QUESTION} (blank or "done" to finish)'
```

- [ ] **Step 4: Add the three channel methods**

In `OperatorChannel`, directly after the `async def aask(...)` method (it ends with `return result`, just before `def _ask_sync`), add:

```python
    # -- the checkpoint (3.6.2) -----------------------------------------------

    def seed_from(self, previous: "OperatorChannel") -> None:
        """Start a later round knowing what the operator already told this session: the answers
        (so the planner sees them) and the questions already answered (so none is asked twice).
        Questions nobody answered are not carried over, so they may be asked again."""
        self._answers = list(previous._answers)
        self._seen = {key: reply for key, reply in previous._seen.items() if reply.answered}

    def _checkpoint_asked(self) -> None:
        self.emit({"type": "question", "source": CHECKPOINT_SOURCE, "question": CHECKPOINT_QUESTION,
                   "context": ""})
        self._say(f"{CHECKPOINT_SOURCE} asks: {CHECKPOINT_QUESTION}")

    def _checkpoint_answered(self, reply: Reply) -> Reply:
        self.records.append({"source": CHECKPOINT_SOURCE, "question": CHECKPOINT_QUESTION,
                             "answered": reply.answered, "reason": reply.reason, "deduped": False})
        self.emit({"type": "answer", "source": CHECKPOINT_SOURCE, "answered": reply.answered,
                   "reason": reply.reason})
        self._say("answered" if reply.answered else f"no answer ({reply.reason})")
        return reply

    def checkpoint(self, shown: str) -> Reply:
        """Ask what to do next, with ``shown`` as the prompt. Unlike ``ask`` this uses no question
        slot, ignores the per-agent limit, is not de-duplicated and never touches memory (a next
        instruction is a command, not a fact). It shares the ask lock, the timeout, the headless
        handling and the pause of the persistent deadline."""
        self._checkpoint_asked()
        with self._lock:
            reply = self._ask_sync(shown, CHECKPOINT_SOURCE)
        return self._checkpoint_answered(reply)

    async def acheckpoint(self, shown: str) -> Reply:
        """The async form of :meth:`checkpoint`."""
        self._checkpoint_asked()
        while not self._lock.acquire(blocking=False):      # never blocks the loop; a cancel cannot leak the lock
            await asyncio.sleep(0.02)
        try:
            reply = await self._ask_async(shown, CHECKPOINT_SOURCE)
        finally:
            self._lock.release()
        return self._checkpoint_answered(reply)
```

- [ ] **Step 5: Run the tests and the whole suite**

Run: `python -m pytest tests/test_checkpoint_channel.py -q`
Expected: PASS (all).
Run: `python -m pytest tests -q`
Expected: PASS (nothing else changed).

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Operator.py tests/test_checkpoint_channel.py
git commit -m "feat(operator): checkpoint question, stop-word test and channel seeding for follow-up rounds"
```

---

### Task 2: Settings, result fields and pure helpers (`Supervisor.py`)

**Files:**
- Modify: `agentx_dev/Supervisor.py`
- Test: `tests/test_session_settings.py` (create)

**Interfaces:**
- Consumes: `_result_failed`, `OUTCOME_OUT_OF_TIME`, `OUTCOME_OUT_OF_BUDGET`, `OUTCOME_PARTIAL`, `OUTCOME_STUCK`, `SubtaskResult`, `SupervisorResult`.
- Produces: `SupervisorResult.next_step`, `SupervisorResult.rounds`; module-level `_session_settings(ask_user, ask_when_done, follow_up, max_follow_ups) -> (follow_up, ask_when_done, max_follow_ups)`, `_cap_text(text, limit) -> str`, `_outcome_line(result) -> str`, `_follow_up_task(original, rounds, reply) -> str`; instance attributes `self.ask_when_done: bool`, `self.follow_up: bool`, `self.max_follow_ups: int` on both classes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_settings.py`:

```python
"""Constructor settings, the result fields, and the pure helpers of the ask-when-done session."""

import pytest

from agentx_dev import AsyncSupervisor, Supervisor
from agentx_dev.Supervisor import (
    SubtaskResult, SupervisorResult, _cap_text, _follow_up_task, _outcome_line,
)
from tests.subagent_helpers import ScriptedRunner, router


def make(cls, **kw):
    return cls(model=router(), agents={"w": ("w", ScriptedRunner())}, verbose=False, **kw)


@pytest.mark.parametrize("cls", [Supervisor, AsyncSupervisor])
class TestSettings:
    def test_everything_is_off_by_default(self, cls):
        sup = make(cls)
        assert (sup.ask_when_done, sup.follow_up, sup.max_follow_ups) == (False, False, 5)

    def test_follow_up_implies_ask_when_done(self, cls):
        sup = make(cls, ask_user=lambda q: "x", follow_up=True)
        assert sup.follow_up and sup.ask_when_done

    def test_ask_when_done_alone_does_not_follow_up(self, cls):
        sup = make(cls, ask_user=lambda q: "x", ask_when_done=True)
        assert sup.ask_when_done and not sup.follow_up

    @pytest.mark.parametrize("flag", ["ask_when_done", "follow_up"])
    @pytest.mark.parametrize("ask", [None, False])
    def test_either_flag_needs_ask_user(self, cls, flag, ask):
        with pytest.raises(ValueError, match="need ask_user"):
            make(cls, ask_user=ask, **{flag: True})

    def test_max_follow_ups_is_clamped_to_zero(self, cls):
        assert make(cls, ask_user=lambda q: "x", follow_up=True, max_follow_ups=-3).max_follow_ups == 0


class TestResultFields:
    def test_they_default_to_nothing(self):
        r = SupervisorResult(query="q", content="c")
        assert r.next_step is None and r.rounds == []


def sub(**kw):
    return SubtaskResult(agent="w", query="q", content="c", **kw)


class TestOutcomeLine:
    def test_done(self):
        assert _outcome_line(SupervisorResult(query="q", content="c")) == "Outcome: done."

    def test_partial_counts_failed_steps(self):
        r = SupervisorResult(query="q", content="c", outcome="partial",
                             subtasks=[sub(), sub(error="boom", outcome="stuck")])
        assert _outcome_line(r) == "Outcome: partial (1 of 2 steps failed)."

    def test_stuck_with_no_failed_step_has_no_count(self):
        assert _outcome_line(SupervisorResult(query="q", content="c", outcome="stuck")) == "Outcome: stuck."

    def test_spawn_bookkeeping_is_not_counted(self):
        spawn = SubtaskResult(agent="__spawn__", query="q", content="c")
        r = SupervisorResult(query="q", content="c", outcome="partial",
                             subtasks=[spawn, sub(), sub(error="boom", outcome="stuck")])
        assert "1 of 2 steps" in _outcome_line(r)

    def test_the_two_limits_are_named(self):
        assert _outcome_line(SupervisorResult(query="q", content="c", outcome="out_of_time")) == \
            "Outcome: out_of_time (time limit reached)."
        assert _outcome_line(SupervisorResult(query="q", content="c", outcome="out_of_budget")) == \
            "Outcome: out_of_budget (cost limit reached)."


class TestFollowUpTask:
    def rounds(self):
        return [{"round": 1, "query": "Research X", "content": "X is a thing.", "outcome": "done"},
                {"round": 2, "query": "now pricing", "content": "Pricing is $5.", "outcome": "partial"}]

    def test_original_then_history_then_the_new_instruction(self):
        text = _follow_up_task("Research X", self.rounds(), "write the emails")
        assert text.startswith("Research X\n\nWHAT HAS HAPPENED SO FAR")
        assert "Round 1, outcome done:\nX is a thing." in text
        assert "Round 2 (the operator asked: now pricing), outcome partial:\nPricing is $5." in text
        assert text.rstrip().endswith("Do not repeat work that is already done.")
        assert "The operator now asks: write the emails" in text

    def test_an_earlier_answer_is_capped(self):
        rounds = [{"round": 1, "query": "q", "content": "y" * 9000, "outcome": "done"}]
        assert _follow_up_task("q", rounds, "go").count("y") < 3100

    def test_cap_text(self):
        assert _cap_text("short", 10) == "short"
        assert _cap_text("z" * 50, 10) == "z" * 10 + " ..."
        assert _cap_text(None, 10) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_session_settings.py -q`
Expected: FAIL at collection with `ImportError: cannot import name '_cap_text'`.

- [ ] **Step 3: Add the result fields**

In `SupervisorResult` (class at the top of `agentx_dev/Supervisor.py`), after the line `memory: List[Dict[str, Any]] = Field(default_factory=list)`, add:

```python
    # The operator's reply to "what should I do next?" (3.6.2), as typed; None when
    # ask_when_done is off or nobody answered.
    next_step: Optional[str] = None
    # One {"round", "query", "content", "outcome"} per round, filled only once a follow-up round
    # has run (follow_up=True). Empty for a single-round run.
    rounds: List[Dict[str, Any]] = Field(default_factory=list)
```

- [ ] **Step 4: Add the imports and the helpers**

In the `from agentx_dev.Operator import (...)` block, add `checkpoint_text` and `is_stop_reply` (they are used in Tasks 3 and 4), so the block reads:

```python
from agentx_dev.Operator import (
    ASK_TOOL_NAME, NO_ANSWER_PLAN_NOTE, PLANNER_SOURCE, OperatorChannel, ask_instruction, attach_ask_user,
    checkpoint_text, is_stop_reply, parse_ask_request, validate_ask_user,
)
```

Directly before the line `def _supervisor_outcome(` add:

```python
SESSION_ANSWER_CHARS = 3000        # how much of an earlier round's answer a follow-up round is shown


def _session_settings(ask_user: Any, ask_when_done: bool, follow_up: bool, max_follow_ups: int):
    """``(follow_up, ask_when_done, max_follow_ups)`` for a constructor. ``follow_up`` implies
    ``ask_when_done``; either needs ``ask_user``."""
    follow = bool(follow_up)
    when_done = bool(ask_when_done) or follow
    if when_done and (ask_user is None or ask_user is False):
        raise ValueError("ask_when_done and follow_up need ask_user: pass ask_user=True or a function")
    return follow, when_done, max(0, int(max_follow_ups))


def _cap_text(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + " ..."


def _outcome_line(result: "SupervisorResult") -> str:
    """The sentence under the answer in a checkpoint prompt: how the round ended."""
    outcome = result.outcome
    if outcome == OUTCOME_OUT_OF_TIME:
        return f"Outcome: {outcome} (time limit reached)."
    if outcome == OUTCOME_OUT_OF_BUDGET:
        return f"Outcome: {outcome} (cost limit reached)."
    steps = [r for r in result.subtasks if r.agent != "__spawn__" and not r.superseded]
    failed = [r for r in steps if _result_failed(r)]
    if outcome in (OUTCOME_PARTIAL, OUTCOME_STUCK) and failed:
        return f"Outcome: {outcome} ({len(failed)} of {len(steps)} steps failed)."
    return f"Outcome: {outcome}."


def _follow_up_task(original: str, rounds: List[Dict[str, Any]], reply: str) -> str:
    """The task a follow-up round plans: the original, what each earlier round produced, and the
    operator's new instruction. The operator's earlier answers are not repeated here: the round's
    channel is seeded with them and the Supervisor appends its OPERATOR ANSWERS block."""
    lines = [original, "", "WHAT HAS HAPPENED SO FAR"]
    for r in rounds:
        asked = "" if r["round"] == 1 else f" (the operator asked: {_cap_text(r['query'], 500)})"
        lines += [f"Round {r['round']}{asked}, outcome {r['outcome']}:",
                  _cap_text(r["content"], SESSION_ANSWER_CHARS), ""]
    lines += [f"The operator now asks: {reply}",
              "Do this now, building on the results above. Do not repeat work that is already done."]
    return "\n".join(lines)


```

- [ ] **Step 5: Add the constructor parameters to both classes**

`Supervisor.__init__`: after the parameter line `memory_write: bool = True,` add:

```python
        ask_when_done: bool = False,
        follow_up: bool = False,
        max_follow_ups: int = 5,
```

In its docstring `Args:` list, after the `memory_write:` entry (`memory_write: False makes the store read-only.`), add:

```
            ask_when_done: (3.6.2) When a round's answer is ready, the framework itself asks the
                operator "What should I do next?" through the same asker as ``ask_user`` (the
                answer is shown in the prompt) and returns the reply in ``result.next_step``
                (``None`` if nobody answered). Asked whatever the outcome. Needs ``ask_user``.
            follow_up: (3.6.2) Run another round on the operator's reply, with the earlier
                results as context, until the operator replies blank or with a stop word
                ("done", "stop", "quit", "exit", "no", "nothing", "that's all"). Implies
                ``ask_when_done``. ``result.rounds`` lists every round.
            max_follow_ups: (3.6.2) Rounds allowed after the first (default 5). After the last
                one no question is asked.
```

In the body, directly after the line `self._operator: Optional[OperatorChannel] = None` (the first occurrence, in `Supervisor.__init__`), add:

```python
        self.follow_up, self.ask_when_done, self.max_follow_ups = _session_settings(
            self.ask_user, ask_when_done, follow_up, max_follow_ups)
```

`AsyncSupervisor.__init__`: apply the same three parameter lines after its `memory_write: bool = True,` parameter, the same docstring entries after its `memory_write` entry, and the same two body lines directly after ITS `self._operator: Optional[OperatorChannel] = None` (the second occurrence). Open the async constructor first and read its signature and `Args:` list to place them correctly.

- [ ] **Step 6: Run the tests and the whole suite**

Run: `python -m pytest tests/test_session_settings.py -q`
Expected: PASS.
Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_session_settings.py
git commit -m "feat(supervisor): ask_when_done / follow_up settings, result fields and session helpers"
```

---

### Task 3: The sync session loop

**Files:**
- Modify: `agentx_dev/Supervisor.py` (class `Supervisor`: `stream`)
- Test: `tests/test_session_sync.py` (create)

**Interfaces:**
- Consumes: Task 1 (`OperatorChannel.checkpoint`, `seed_from`, `checkpoint_text`, `is_stop_reply`), Task 2 (`_outcome_line`, `_follow_up_task`, `_cap_text`, the settings and result fields).
- Produces: `Supervisor._stream_round(user_task, *, save_result=True, seed=None)` (the old `stream` body) and the new `Supervisor.stream`. Stream events `{"type": "round", "round": int, "query": str}` and `{"type": "checkpoint", "round": int, "outcome": str}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_sync.py`:

```python
"""Supervisor(ask_when_done=..., follow_up=...): the framework asks what to do next, and can go on."""

import json

import pytest

import agentx_dev.Supervisor as supmod
from agentx_dev import CostBudgetExceeded, Persistence, Supervisor
from agentx_dev.Operator import NoChannel
from tests.conftest import MockModel
from tests.memory_helpers import FakeStore
from tests.subagent_helpers import PLANNER_MARK, ScriptedRunner, plan_json, router, step

PLAN = plan_json(step("s1", "worker", "do it"))
ASK_PLAN = json.dumps({"ask": [{"question": "Which three competitors should I compare?",
                                "why": "the task does not name them"}]})


def asker_of(*replies):
    """An ask_user function that answers from a list (None when it runs out) and records what it saw."""
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
    return Supervisor(model=model, agents={"worker": ("does the work", ScriptedRunner())}, ask_user=ask, **kw)


class TestOff:
    def test_nothing_is_asked_and_the_result_is_unchanged(self):
        ask = asker_of("never used")
        model = router(plans=[PLAN])
        events = list(sup(model, ask).stream("task"))
        assert ask.seen == []
        assert {e["type"] for e in events}.isdisjoint({"round", "checkpoint"})
        result = events[-1]["result"]
        assert result.next_step is None and result.rounds == []


class TestAskWhenDone:
    def test_one_round_then_the_question(self):
        ask = asker_of("write the emails")
        model = router(plans=[PLAN], synth="Final.")
        result = sup(model, ask, ask_when_done=True).run("task")
        assert len(checkpoints(ask)) == 1
        shown = checkpoints(ask)[0]
        assert "Final." in shown and "Outcome: done." in shown and shown.endswith('(blank or "done" to finish)')
        assert result.next_step == "write the emails"
        assert len(model.planner_prompts()) == 1               # nothing ran on the reply
        assert result.rounds == []
        assert [r["source"] for r in result.asked] == ["supervisor"]

    def test_the_prompt_is_labelled_for_the_supervisor(self):
        ask = asker_of("x")
        result = sup(router(plans=[PLAN]), ask, ask_when_done=True).run("task")
        assert result.asked[0]["question"] == "What should I do next?"

    @pytest.mark.parametrize("reply", ["done", "Quit.", "That's all!"])
    def test_a_stop_word_is_still_returned_as_typed(self, reply):
        result = sup(router(plans=[PLAN]), asker_of(reply), follow_up=True).run("task")
        assert result.next_step == reply and result.rounds == []

    @pytest.mark.parametrize("reply", ["", None])
    def test_a_blank_or_missing_reply_is_none(self, reply):
        result = sup(router(plans=[PLAN]), asker_of(reply), follow_up=True).run("task")
        assert result.next_step is None

    @pytest.mark.parametrize("raised,reason", [(RuntimeError("boom"), "error"), (NoChannel(), "no_channel")])
    def test_an_asker_that_cannot_answer_ends_quietly(self, raised, reason):
        result = sup(router(plans=[PLAN]), asker_of(raised), follow_up=True).run("task")
        assert result.next_step is None and result.asked[-1]["reason"] == reason

    def test_it_asks_whatever_the_outcome_and_says_so(self):
        failing = ScriptedRunner(("gave up", "stuck"), ("gave up", "stuck"))
        ask = asker_of("try something else")
        s = Supervisor(model=router(plans=[PLAN]), agents={"worker": ("w", failing)}, verbose=False,
                       ask_user=ask, ask_when_done=True)
        result = s.run("task")
        assert result.outcome != "done" and "Outcome: " + result.outcome in checkpoints(ask)[0]
        assert result.next_step == "try something else"

    def test_a_huge_answer_is_cut_in_the_prompt_but_not_in_the_result(self):
        ask = asker_of("x")
        result = sup(router(plans=[PLAN], synth="A" * 10000), ask, ask_when_done=True).run("task")
        assert len(result.content) == 10000
        assert len(checkpoints(ask)[0]) < 3300 and "(full text in result.content)" in checkpoints(ask)[0]

    def test_it_uses_no_question_slot(self):
        result = sup(router(plans=[PLAN]), asker_of("thanks"), ask_when_done=True, max_questions=0).run("task")
        assert result.next_step == "thanks"


class TestFollowUp:
    def test_two_rounds_then_done(self):
        ask = asker_of("compare pricing", "done")
        model = router(plans=[PLAN, PLAN], synth="Final.")
        result = sup(model, ask, follow_up=True).run("Research X")
        assert len(model.planner_prompts()) == 2
        second = model.planner_prompts()[1]
        assert "Research X" in second and "WHAT HAS HAPPENED SO FAR" in second and "Final." in second
        assert "The operator now asks: compare pricing" in second
        assert result.query == "compare pricing" and result.next_step == "done"
        assert [r["round"] for r in result.rounds] == [1, 2]
        assert result.rounds[0] == {"round": 1, "query": "Research X", "content": "Final.", "outcome": "done"}
        assert len(checkpoints(ask)) == 2

    def test_asked_lists_every_checkpoint_in_order(self):
        result = sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True).run("task")
        assert [r["source"] for r in result.asked] == ["supervisor", "supervisor"]

    def test_events_end_with_one_completion_and_mark_the_rounds(self):
        events = list(sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True).stream("task"))
        types = [e["type"] for e in events]
        assert types.count("completion") == 1 and types[-1] == "completion"
        assert types.count("round") == 1 and types.count("checkpoint") == 2 and types.count("final") == 2
        assert types.index("checkpoint") < types.index("round")
        round_event = next(e for e in events if e["type"] == "round")
        assert round_event["round"] == 2 and round_event["query"] == "more"
        assert [e["source"] for e in events if e["type"] == "question"] == ["supervisor", "supervisor"]

    def test_the_cap_stops_the_questions(self):
        ask = asker_of("a", "b", "c")
        model = router(plans=[PLAN, PLAN, PLAN])
        result = sup(model, ask, follow_up=True, max_follow_ups=2).run("task")
        assert len(model.planner_prompts()) == 3 and len(checkpoints(ask)) == 2
        assert len(result.rounds) == 3 and result.next_step is None

    def test_zero_follow_ups_behaves_like_ask_when_done(self):
        model = router(plans=[PLAN, PLAN])
        result = sup(model, asker_of("do more"), follow_up=True, max_follow_ups=0).run("task")
        assert len(model.planner_prompts()) == 1 and result.next_step == "do more" and result.rounds == []

    def test_a_question_answered_in_round_one_is_not_asked_again(self):
        ask = asker_of("Notion, Obsidian, Coda", "more please", "done")
        model = router(plans=[ASK_PLAN, PLAN, ASK_PLAN, PLAN])
        result = sup(model, ask, follow_up=True).run("Compare our competitors")
        assert len([q for q in ask.seen if "Which three competitors" in q]) == 1
        second = model.planner_prompts()[-1]
        assert "OPERATOR ANSWERS" in second and "Notion, Obsidian, Coda" in second
        assert len(result.rounds) == 2

    def test_only_round_one_is_saved_to_memory(self):
        store = FakeStore()
        sup(router(plans=[PLAN, PLAN], synth="Final."), asker_of("more", "done"), follow_up=True,
            memory=store).run("task")
        assert store.kinds_added().count("run_result") == 1

    def test_each_round_gets_a_fresh_time_budget(self, monkeypatch):
        starts = []
        real = supmod.RunBudget.start
        monkeypatch.setattr(supmod.RunBudget, "start", staticmethod(lambda *a, **k: starts.append(1) or real(*a, **k)))
        sup(router(plans=[PLAN, PLAN]), asker_of("more", "done"), follow_up=True,
            persistence=Persistence(max_minutes=5)).run("task")
        assert len(starts) == 2

    def test_no_round_starts_after_the_cost_budget_is_spent(self):
        def script(messages):
            if PLANNER_MARK in str(messages[0]["content"]):
                raise CostBudgetExceeded(spent_usd=2.0, limit_usd=1.0)
            return "Final."
        model = MockModel(script=script)
        model.planner_prompts = lambda: [str(c[0]["content"]) for c in model.calls
                                         if PLANNER_MARK in str(c[0]["content"])]
        ask = asker_of("try again")
        result = sup(model, ask, follow_up=True, persistence=Persistence(max_minutes=5)).run("task")
        assert result.outcome == "out_of_budget" and "cost limit reached" in checkpoints(ask)[0]
        assert result.next_step == "try again" and result.rounds == []
        assert len(model.planner_prompts()) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_session_sync.py -q`
Expected: FAIL (the checkpoint is never asked: `ask.seen` stays empty, `next_step` is `None`).

- [ ] **Step 3: Turn the old `stream` into the round generator**

In `class Supervisor`, make exactly these edits to the existing method:

1. Change the signature line `    def stream(self, user_task: str):` to:

```python
    def _stream_round(self, user_task: str, *, save_result: bool = True,
                      seed: Optional[OperatorChannel] = None):
```

2. Replace its whole docstring (it starts `"""Yield structured events as the supervisor plans, dispatches,` and ends `the intermediate events.` + `"""`) with this one (the long event list moves to the new public `stream` in Step 4):

```python
        """One plan / dispatch / synthesize round, as a stream of events. ``save_result`` False
        keeps the round's answer out of long-term memory (a follow-up round, whose task is a
        composed one). ``seed`` is the previous round's operator channel, whose answers this
        round's channel starts with. The last event is this round's ``completion``."""
```

3. Directly after the existing lines

```python
        self._operator = self._new_operator_channel()
        self._spawn_run_policy.operator = self._operator
        if self._operator is not None:
            self._operator.budget = budget            # waiting on the operator pauses the deadline
```

add:

```python
        if self._operator is not None and seed is not None:
            self._operator.seed_from(seed)
```

4. Change the memory-save condition. The existing code reads:

```python
        if (self._run_memory is not None and outcome == OUTCOME_DONE
                and not self._synth_fell_back and str(final).strip()):
            self._run_memory.remember_result(user_task, final)
```

Change its first line to `        if (save_result and self._run_memory is not None and outcome == OUTCOME_DONE`.

- [ ] **Step 4: Add the new public `stream`**

Directly above `def run(self, user_task: str) -> SupervisorResult:` in `class Supervisor`, add this method (copy the event list from the old docstring and add the two new lines marked below):

```python
    def stream(self, user_task: str):
        """Yield structured events as the supervisor plans, dispatches,
        and synthesizes.

        Event shapes:
          - {"type": "plan_start"}
          - {"type": "plan",           "plan": <list of steps>}
          - {"type": "spawn",          "name": str, "origin": "plan" | "delegate", "tools": list,
                                        "dropped": list, "reused": str | None, "refused": str | None,
                                        "capabilities": list, "rerouted_from": None}
          - {"type": "delegate_result", "name": str, "outcome": str, "chars": int}
          - {"type": "question",       "source": "planner" | "supervisor" | <agent name>, "question": str, "context": str}
          - {"type": "answer",         "source": str, "answered": bool, "reason": None | str}
          - {"type": "memory",         "stage": "plan" | "step", "hits": int}
          - {"type": "dispatch",       "agent": str, "query": str, "step": int}
          - {"type": "subtask_result", "result": SubtaskResult, "step": int}
          - {"type": "replan",         "round": int, "unresolved": list, "plan": list}  (persistent)
          - {"type": "budget",         "reason": "time" | "cost"}  (persistent; before synthesis)
          - {"type": "synthesize_start"}
          - {"type": "final",          "content": str}   (once per round)
          - {"type": "checkpoint",     "round": int, "outcome": str}  (ask_when_done; before the question)
          - {"type": "round",          "round": int, "query": str}  (follow_up; starts each round after the first)
          - {"type": "completion",     "result": SupervisorResult}   (last)

        The last event is always ``completion`` so ``run()`` can build
        on top of this and callers who want live UI updates can consume
        the intermediate events. With ``ask_when_done`` / ``follow_up`` the
        stream covers every round and ends with one ``completion`` for the
        session.
        """
        if not self.ask_when_done:
            yield from self._stream_round(user_task)
            return
        rounds: List[Dict[str, Any]] = []
        asked: List[Dict[str, Any]] = []
        task, instruction, seed = user_task, user_task, None
        while True:
            number = len(rounds) + 1
            if number > 1:
                yield {"type": "round", "round": number, "query": instruction}
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.round] round {number}: {_cap_text(instruction, 120)}{_C_RESET}")
            result: Optional[SupervisorResult] = None
            for event in self._stream_round(task, save_result=number == 1, seed=seed):
                if event["type"] == "completion":
                    result = event["result"]
                else:
                    yield event
            assert result is not None, "a round must yield a completion event"
            rounds.append({"round": number, "query": instruction, "content": result.content,
                           "outcome": result.outcome})
            asked.extend(result.asked)
            channel = self._operator
            assert channel is not None, "ask_when_done needs the operator channel"
            if self.follow_up and number > 1 and number == 1 + self.max_follow_ups:
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.round] follow-up limit reached{_C_RESET}")
                next_step = None
                break
            yield {"type": "checkpoint", "round": number, "outcome": result.outcome}
            before = len(channel.records)
            reply = channel.checkpoint(checkpoint_text(result.content, _outcome_line(result)))
            asked.extend(channel.records[before:])
            yield from self._drain_operator_events()
            next_step = reply.text
            more = (self.follow_up and number <= self.max_follow_ups
                    and result.outcome != OUTCOME_OUT_OF_BUDGET and not is_stop_reply(reply.text))
            if not more:
                break
            task, instruction, seed = _follow_up_task(user_task, rounds, reply.text), reply.text, channel
        yield {"type": "completion", "result": result.model_copy(update={
            "query": instruction, "asked": asked, "next_step": next_step,
            "rounds": rounds if len(rounds) > 1 else []})}
```

Check that `_C_PLAN` and `_C_RESET` are already defined in this module (they are used by `_log_plan` and the spawn log lines); if the names differ, use the same names those lines use.

- [ ] **Step 5: Run the new tests, then the whole suite**

Run: `python -m pytest tests/test_session_sync.py -q`
Expected: PASS (all). If `test_no_round_starts_after_the_cost_budget_is_spent` fails because the stopped-before-planning result does not have outcome `out_of_budget`, read `_stopped_before_planning` and `_budget_outcome_of` in `agentx_dev/Supervisor.py` and fix the TEST's setup (the exception type or the persistence it needs), not the production code.
Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_session_sync.py
git commit -m "feat(supervisor): ask what to do next when a round is done, and follow up on the reply"
```

---

### Task 4: The async session loop

**Files:**
- Modify: `agentx_dev/Supervisor.py` (class `AsyncSupervisor`: `astream`)
- Test: `tests/test_session_async.py` (create)

**Interfaces:**
- Consumes: everything Task 3 uses, plus `OperatorChannel.acheckpoint`.
- Produces: `AsyncSupervisor._astream_round(user_task, *, save_result=True, seed=None)` and the new `AsyncSupervisor.astream`; `AsyncSupervisor.run` is unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_async.py`:

```python
"""AsyncSupervisor(ask_when_done=..., follow_up=...): the same session, awaited."""

import asyncio
import json

from agentx_dev import AsyncSupervisor, CostBudgetExceeded, Persistence
from agentx_dev.Operator import NoChannel
from tests.conftest import MockModel
from tests.memory_helpers import FakeStore
from tests.subagent_helpers import PLANNER_MARK, ScriptedRunner, plan_json, router, step

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_session_async.py -q`
Expected: FAIL (`next_step` is `None`, nothing asked).

- [ ] **Step 3: Turn `astream` into the round generator**

In `class AsyncSupervisor`, make the same four edits as Task 3 Step 3, adapted:

1. Signature `    async def astream(self, user_task: str):` becomes:

```python
    async def _astream_round(self, user_task: str, *, save_result: bool = True,
                             seed: Optional[OperatorChannel] = None):
```

2. Replace its docstring (it starts `"""Async event stream. Same event shapes as ``Supervisor.stream``.`) with:

```python
        """One plan / dispatch / synthesize round, as an async stream of events. See
        ``Supervisor._stream_round``. Concurrent-dispatch mode (``sequential=False``) yields
        ``subtask_result`` events as each sub-task finishes, so the UI sees whichever
        completes first, not the plan order."""
```

3. After the lines `self._operator = self._new_operator_channel()` / `self._spawn_run_policy.operator = self._operator` / `if self._operator is not None: self._operator.budget = budget ...` add:

```python
        if self._operator is not None and seed is not None:
            self._operator.seed_from(seed)
```

4. The memory-save condition in this method reads `if (self._run_memory is not None and outcome == OUTCOME_DONE` followed by `and not self._synth_fell_back and str(final).strip()):`. Change its first line to `        if (save_result and self._run_memory is not None and outcome == OUTCOME_DONE`.

- [ ] **Step 4: Add the new public `astream`**

Directly above `async def run(self, user_task: str) -> SupervisorResult:` in `class AsyncSupervisor`, add:

```python
    async def astream(self, user_task: str):
        """Async event stream. Same event shapes as ``Supervisor.stream``, including the
        ``checkpoint`` and ``round`` events of ``ask_when_done`` / ``follow_up``.

        Concurrent-dispatch mode (``sequential=False``) yields
        ``subtask_result`` events as each sub-task finishes -- so the
        UI sees whichever completes first, not the plan order.
        """
        if not self.ask_when_done:
            async for event in self._astream_round(user_task):
                yield event
            return
        rounds: List[Dict[str, Any]] = []
        asked: List[Dict[str, Any]] = []
        task, instruction, seed = user_task, user_task, None
        while True:
            number = len(rounds) + 1
            if number > 1:
                yield {"type": "round", "round": number, "query": instruction}
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.round] round {number}: {_cap_text(instruction, 120)}{_C_RESET}")
            result: Optional[SupervisorResult] = None
            async for event in self._astream_round(task, save_result=number == 1, seed=seed):
                if event["type"] == "completion":
                    result = event["result"]
                else:
                    yield event
            assert result is not None, "a round must yield a completion event"
            rounds.append({"round": number, "query": instruction, "content": result.content,
                           "outcome": result.outcome})
            asked.extend(result.asked)
            channel = self._operator
            assert channel is not None, "ask_when_done needs the operator channel"
            if self.follow_up and number > 1 and number == 1 + self.max_follow_ups:
                if self.verbose:
                    print(f"{_C_PLAN}[supervisor.round] follow-up limit reached{_C_RESET}")
                next_step = None
                break
            yield {"type": "checkpoint", "round": number, "outcome": result.outcome}
            before = len(channel.records)
            reply = await channel.acheckpoint(checkpoint_text(result.content, _outcome_line(result)))
            asked.extend(channel.records[before:])
            for event in self._drain_operator_events():
                yield event
            next_step = reply.text
            more = (self.follow_up and number <= self.max_follow_ups
                    and result.outcome != OUTCOME_OUT_OF_BUDGET and not is_stop_reply(reply.text))
            if not more:
                break
            task, instruction, seed = _follow_up_task(user_task, rounds, reply.text), reply.text, channel
        yield {"type": "completion", "result": result.model_copy(update={
            "query": instruction, "asked": asked, "next_step": next_step,
            "rounds": rounds if len(rounds) > 1 else []})}
```

- [ ] **Step 5: Run the new tests, then the whole suite**

Run: `python -m pytest tests/test_session_async.py -q`
Expected: PASS (all). The same note as Task 3 applies to the cost-budget test.
Run: `python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agentx_dev/Supervisor.py tests/test_session_async.py
git commit -m "feat(supervisor): ask_when_done / follow_up on AsyncSupervisor"
```

---

### Task 5: Docs, changelog, spec correction

**Files:**
- Modify: `docs/guides/sub-agents.md`, `docs/cookbook/faq.md`, `docs/reference/api-summary.md`, `README.md`, `CHANGELOG.md`, `docs/superpowers/specs/2026-10-09-ask-when-done-follow-up-design.md`
- Regenerate: `host/data.js`

- [ ] **Step 1: Check the spec's reply cap**

`docs/superpowers/specs/2026-10-09-ask-when-done-follow-up-design.md` sections 6 and 11 already say the reply is capped at 2,000 characters (the channel's existing answer cap, `MAX_ANSWER_CHARS`). Confirm nothing in the docs you write below says 4,000.

- [ ] **Step 2: Guide**

In `docs/guides/sub-agents.md`, in the section `## Asking the operator`, directly after the bullet that starts `- **Asking at a checkpoint.**`, add this block (a new level-3 heading and body, before the bullet `- **An agent may ask once.**`'s following bullets is fine; place it at the end of the section instead if the bullet list is contiguous: the heading must come after the last bullet of the section and before `## Long-term memory`):

````markdown
### Asking when the work is done

A model has to choose to call `ask_user`; the framework does not have to. Two settings make it ask by itself when a round's answer is ready:

```python
sup = Supervisor(model=model, agents=agents, ask_user=True, ask_when_done=True)
result = sup.run("Research veltehub.com and find 10 prospects in Barrie")
print(result.next_step)        # what you replied to "What should I do next?", or None

sup = Supervisor(model=model, agents=agents, ask_user=True, follow_up=True, max_follow_ups=5)
result = sup.run("Research veltehub.com and find 10 prospects in Barrie")   # keeps going on your replies
```

| Setting | Default | What it does |
|---|---|---|
| `ask_when_done` | `False` | After the answer is ready, ask "What should I do next?" through the `ask_user` asker. The prompt shows the answer (cut at 3,000 characters), how the round ended, and the question. The reply goes in `result.next_step`. |
| `follow_up` | `False` | Run another round on your reply, until you reply blank or with a stop word. Implies `ask_when_done`. |
| `max_follow_ups` | `5` | Rounds allowed after the first. After the last one no question is asked. |

- Either setting without `ask_user` raises `ValueError`.
- It asks whatever the outcome (`done`, `partial`, `stuck`, `out_of_time`, `out_of_budget`) and says which in the prompt.
- Stop words: `done`, `stop`, `quit`, `exit`, `no`, `nothing`, `that's all` (any case, trailing `.` or `!` ignored), or a blank reply. `next_step` keeps what you typed, so it can be `"done"`; it is `None` when nobody answered (headless, timed out, empty reply).
- **A follow-up round is a new run.** It plans on your original task, what each earlier round produced (each capped at 3,000 characters) and your new instruction. Your earlier answers carry over, and a question you already answered is not asked again. The question budget, the helpers and the persistent time budget start over each round; a model cost cap does not. No round starts after `out_of_budget`, though you are still asked.
- **The checkpoint is not a question to a helper.** It uses no `max_questions` slot, is not looked up in or saved to memory, and appears in `result.asked` and as `question` / `answer` events with source `supervisor`.
- **What you get back.** `run()` returns the last round's result. `result.rounds` lists every round (`round`, `query`, `content`, `outcome`) once a follow-up has run, and `result.asked` holds every question across rounds. In memory, only round 1's result is saved.
- **Streaming.** `stream()` / `astream()` add `{"type": "checkpoint", "round", "outcome"}` before each question and `{"type": "round", "round", "query"}` when a round after the first starts. Each round's `final` is yielded, and one `completion` ends the stream.
````

Also add two rows to the events table in the same guide (the table near `| {"type": "question", ...` ): 

```
| `{"type": "checkpoint", "round", "outcome"}` | The framework is about to ask what to do next (`ask_when_done`). |
| `{"type": "round", "round", "query"}` | A follow-up round starts (`follow_up`); `query` is your reply. |
```

- [ ] **Step 3: FAQ, API summary, README**

`docs/cookbook/faq.md`: before `## How do I let a Supervisor's helpers write files? *(3.6)*` add:

```markdown
## How do I make the Supervisor ask me what to do next? *(3.6.2)*

Pass `ask_user=True` and `ask_when_done=True`. When the answer is ready the framework asks "What should
I do next?" itself (it does not rely on a model calling the tool) and returns your reply in
`result.next_step`. Add `follow_up=True` to run another round on each reply until you answer blank or
"done" (`max_follow_ups`, default 5). See [Sub-agents](../guides/sub-agents.md#asking-when-the-work-is-done).
```

`docs/reference/api-summary.md`: find the `Supervisor` / `AsyncSupervisor` constructor row or the `ask_user` mention (search for `ask_user`) and append `ask_when_done`, `follow_up`, `max_follow_ups` (3.6.2) to that row's parameter list; add `next_step` and `rounds` to the `SupervisorResult` row.

`README.md`: in the feature table row that mentions `ask_user=True`, append one sentence: "`ask_when_done=True` makes the Supervisor ask what to do next when it finishes, and `follow_up=True` keeps going on your replies."

- [ ] **Step 4: Changelog**

In `CHANGELOG.md`, under `## [3.6.2] - 2026-10-09`, add a new section as the first one (above `### Fixed`):

```markdown
### Added

- **`Supervisor(ask_when_done=True)` / `follow_up=True`** (and the same on `AsyncSupervisor`): when a
  round's answer is ready the framework itself asks the operator "What should I do next?" through the
  `ask_user` asker (the answer, the outcome and the question in one prompt) and returns the reply in
  `SupervisorResult.next_step`. `follow_up=True` runs another round on each reply, with the earlier
  results as context, until the operator replies blank or with a stop word; `max_follow_ups` (default
  5) bounds it and `SupervisorResult.rounds` lists the rounds. Asked whatever the outcome. The
  checkpoint uses no `max_questions` slot and is never saved to memory; only round 1's result is.
  New `checkpoint` and `round` stream events. Either setting needs `ask_user`.
```

- [ ] **Step 5: Regenerate the docs data, run everything, commit**

Run: `python host/build_data.py`
Run: `python -m pytest tests -q`
Expected: PASS.

```bash
git add -A
git commit -m "docs: ask_when_done and follow_up (guide, FAQ, API summary, README, changelog); spec reply cap"
```
