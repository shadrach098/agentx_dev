"""A team job's block is replayed under the ``start_team_job`` call that ran it: kept under that call's id when
the app knows it, else at the newest call of the same task whose result did not fail. A declined or refused call
gets no block, an edit or a summary that removes the call removes its record, and a reload while the second job
of a message runs shows the first one too."""
import json
import types

from agentx_dev.cli.team_panel import TeamPanel, assign_team_blocks, prune_team_blocks, task_key
from agentx_dev.cli.webui import WebFront


def call(i, task, *, text_mode=False):
    if text_mode:
        return [{"role": "user", "content": f"go {i}"},
                {"role": "assistant", "content": json.dumps({"Thought": "t", "action": "start_team_job",
                                                              "action_input": {"task": task}})}]
    return [{"role": "user", "content": f"go {i}"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {
                "name": "start_team_job", "arguments": json.dumps({"task": task})}}]}]


def res(i, text, *, text_mode=False):
    if text_mode:
        return [{"role": "user", "content": f"Observation: {text}"}, {"role": "assistant", "content": "ok"}]
    return [{"role": "tool", "tool_call_id": f"c{i}", "content": text}, {"role": "assistant", "content": "ok"}]


def rec(job, task="A", call_id=None):
    out = {"job": job, "taskKey": task_key(task), "rows": [
        {"id": "d1", "helper": "h", "label": "l", "state": "done", "mark": "✓", "word": "Done"}]}
    if call_id:
        out["call"] = call_id
    return out


def shown(history, kept):
    front = WebFront.__new__(WebFront)
    front.app = types.SimpleNamespace(session=types.SimpleNamespace(history=history, metadata={"team_blocks": kept}))
    front.cwd = "C:/proj"
    out = []
    for item in front.history():
        if item["type"] == "tool_call":
            out.append("call")
        elif item["type"] == "tool_result":
            out.append("result(" + ("failed" if item.get("failed") else "ok") + ")")
        elif item["type"] == "team_block":
            out.append("block:" + item["job"])
    return out


RAN = "Team job finished (outcome: done)."


def test_a_declined_call_gets_no_block_the_call_that_ran_does():
    history = call(1, "A") + res(1, "The user declined this.") + call(2, "A") + res(2, RAN)
    want = ["call", "result(failed)", "call", "block:RUN", "result(ok)"]
    assert shown(history, [rec("RUN", call_id="c2")]) == want          # kept under its call's id
    assert shown(history, [rec("RUN")]) == want                        # no id known: the call that did not fail


def test_a_refused_call_gets_no_block():
    for refusal in ("Refused: 2 team jobs already ran for this message.", "Refused: no step of the last team job"):
        history = call(1, "A") + res(1, RAN) + call(2, "A") + res(2, refusal)
        assert shown(history, [rec("RUN")]) == ["call", "block:RUN", "result(ok)", "call", "result(failed)"]


def test_after_an_edit_and_a_rerun_the_new_job_shows_not_the_old():
    history = call(2, "A") + res(2, RAN)
    assert shown(history, [rec("OLD", call_id="c1"), rec("NEW", call_id="c2")]) == ["call", "block:NEW", "result(ok)"]
    assert shown(history, [rec("OLD"), rec("NEW")]) == ["call", "block:NEW", "result(ok)"]


def test_the_same_task_twice_each_under_its_own_call():
    history = call(1, "A") + res(1, RAN) + call(2, "A") + res(2, RAN)
    want = ["call", "block:J1", "result(ok)", "call", "block:J2", "result(ok)"]
    assert shown(history, [rec("J1", call_id="c1"), rec("J2", call_id="c2")]) == want
    assert shown(history, [rec("J1"), rec("J2")]) == want
    text = call(1, "A", text_mode=True) + res(1, RAN, text_mode=True) + call(2, "A", text_mode=True) + res(
        2, RAN, text_mode=True)
    assert [s for s in shown(text, [rec("J1"), rec("J2")]) if s.startswith("block")] == ["block:J1", "block:J2"]


def test_records_whose_call_is_gone_are_pruned():
    history = call(2, "A") + res(2, RAN)
    kept = [rec("OLD", call_id="c1"), rec("NEW", call_id="c2"), rec("LOOSE-OLD", "B"), rec("OTHER", "C")]
    assert [r["job"] for r in prune_team_blocks(history, kept)] == ["NEW"]
    assert set(assign_team_blocks(history, kept)) == {"c2"}


def test_a_kept_record_is_small():
    panel = TeamPanel()
    panel.notify("plan", {"round": 1, "steps": [{"agent": "a", "query": "q", "step_id": "s1"}]})
    from agentx_dev.cli.team import StepStatus
    status = StepStatus(0, "a", "a", "q")
    panel.notify("dispatch", {"round": 1, "step": 0, "step_id": "s1", "status": status})
    for n in range(8):
        status.note_call("read_path", {"path": "x" * 5000 + str(n)}, "y" * 5000)
    kept = panel.record("T" * 12_000)
    assert "task" not in kept or len(kept["task"]) <= 80
    assert kept["taskKey"] == task_key("T" * 12_000)
    assert len(json.dumps(kept)) < 6000, len(json.dumps(kept))


def test_subjects_are_worked_out_once_per_call(monkeypatch):
    import agentx_dev.cli.team_panel as team_panel
    from agentx_dev.cli.team import StepStatus
    counted = []
    real = team_panel.subject_of
    monkeypatch.setattr(team_panel, "subject_of", lambda name, args: counted.append(name) or real(name, args))
    panel = TeamPanel()
    panel.notify("plan", {"round": 1, "steps": [{"agent": "a", "query": "q", "step_id": "s1"}]})
    status = StepStatus(0, "a", "a", "q")
    panel.notify("dispatch", {"round": 1, "step": 0, "step_id": "s1", "status": status})
    for n in range(3):
        status.note_call("read_path", {"path": f"f{n}.py"}, "text")
    for _ in range(20):                                  # the status asks four times a second
        panel.snapshot()
    assert len(counted) == 3
    assert [a["subject"] for a in panel.snapshot()[0]["actions"]] == ["f0.py", "f1.py", "f2.py"]


# -- the app: the call id, the edit, the summary, the second job, a step with no row ----------------------------

def test_a_step_the_panel_has_no_row_for_is_printed_in_the_window():
    from rich.console import Console
    from agentx_dev.cli.webui import Bus, WebScreen
    import io
    bus = Bus()
    screen = WebScreen(Console(file=io.StringIO(), force_terminal=True), bus)
    screen.team_done("explorer", "look", True, "report", in_panel=True)
    assert not [e for e in bus.after(0) if e["type"] == "print"]
    screen.console = Console(file=_BusFile(bus), force_terminal=True, width=100)
    screen.team_done("explorer", "look", True, "the report", in_panel=False)
    printed = "".join(e.get("ansi", "") for e in bus.after(0) if e["type"] == "print")
    assert "explorer · look" in printed and "Done" in printed and "the report" in printed


class _BusFile:
    def __init__(self, bus):
        self.bus = bus

    def write(self, text):
        self.bus.emit("print", ansi=text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True


def test_the_panel_says_whether_a_result_has_its_row():
    panel = TeamPanel()
    panel.notify("plan", {"round": 1, "steps": [{"agent": "a", "query": "q", "step_id": "s1"}]})
    assert panel.notify("result", {"round": 1, "step": 0, "step_id": "s1", "agent": "a", "ok": True}) is True
    assert panel.notify("result", {"round": 1, "step": 5, "step_id": "s6", "agent": "b", "ok": True}) is False


def _app_job(tmp_path, monkeypatch=None, call_id=None):
    from tests.subagent_helpers import step
    from tests.test_cli_app import Lines, make_app
    from tests.test_cli_team_app import team_model
    if monkeypatch is not None:
        import agentx_dev.cli.app as app_module
        monkeypatch.setattr(app_module, "current_tool_call_id", lambda: call_id)
    app, _, _, _ = make_app(tmp_path, Lines("do job", "/exit"), team_model([step("s1", "explorer", "look")]),
                            mode="auto")
    app.start()
    return app


def test_the_app_keeps_a_job_under_the_call_that_ran_it(tmp_path, monkeypatch):
    app = _app_job(tmp_path, monkeypatch, call_id="call_7")
    kept = app.session.metadata["team_blocks"]
    assert len(kept) == 1 and kept[0]["call"] == "call_7"
    assert kept[0]["turnAt"] == 0                       # the conversation was empty as its first turn began


def test_a_summary_that_removes_the_call_removes_its_record(tmp_path, monkeypatch):
    import agentx_dev.cli.app_usage as app_usage
    app = _app_job(tmp_path)
    assert app.session.metadata["team_blocks"]
    summary = [app.session.history[0], {"role": "user", "content": "[summary of earlier work]"}]
    monkeypatch.setattr(app_usage, "needs_compaction", lambda *a, **k: True)
    monkeypatch.setattr(app_usage, "compact", lambda history, call, **k: types.SimpleNamespace(
        status="done", history=list(summary), replaced=len(history) - 1, summary="earlier work"))
    app._compact_wait = 0
    app._maybe_compact()
    assert app.session.metadata["team_blocks"] == []


def test_an_edit_keeps_the_earlier_job_of_the_same_task_also_without_call_ids():
    from agentx_dev.cli.app_team import TeamCommands
    history = ([{"role": "system", "content": "s"}] + call(1, "A", text_mode=True) + res(1, RAN, text_mode=True)
               + call(2, "A", text_mode=True) + res(2, RAN, text_mode=True))
    first, second = rec("J1"), rec("J2")
    first["turnAt"], second["turnAt"] = 0, 5               # the second turn began at its message, index 5
    session = types.SimpleNamespace(history=history[:5], metadata={"team_blocks": [first, second]})
    TeamCommands._prune_team_blocks(types.SimpleNamespace(session=session), upto=5)
    assert [r["job"] for r in session.metadata["team_blocks"]] == ["J1"]


# -- a stopped turn, a summary, a later run that kept nothing, two calls in one batch ---------------------------

def _stopped_job(tmp_path, monkeypatch, call_id):
    """A turn whose team job Esc / Ctrl+C stopped: the turn keeps only your message and a short note."""
    import agentx_dev.cli.app as app_module
    from tests.subagent_helpers import step
    from tests.test_cli_app import Lines, make_app
    from tests.test_cli_team_app import team_model

    def helper(messages, n):
        raise KeyboardInterrupt

    monkeypatch.setattr(app_module, "current_tool_call_id", lambda: call_id)
    app, _, _, _ = make_app(tmp_path, Lines("do the big job", "/exit"), team_model(
        [step("s1", "explorer", "look around")], helper=helper), mode="auto")
    app.start()
    return app


def test_a_stopped_turn_keeps_its_job_under_its_note(tmp_path, monkeypatch):
    from agentx_dev.cli.app_team import TeamCommands
    for n, call_id in enumerate((None, "call_9")):              # text mode, then function calling
        (tmp_path / str(n)).mkdir()
        app = _stopped_job(tmp_path / str(n), monkeypatch, call_id)
        history, kept = app.session.history, app.session.metadata["team_blocks"]
        assert [m["role"] for m in history[-2:]] == ["user", "assistant"]
        assert "stopped before it finished" in history[-1]["content"]
        assert len(kept) == 1
        assert shown(history, kept) == ["block:" + kept[0]["job"]]          # a reload draws it again
        app._prune_team_blocks()                                             # and it is not an orphan
        assert len(app.session.metadata["team_blocks"]) == 1
        TeamCommands._prune_team_blocks(app, upto=len(history) - 2)          # an edit of that message drops it
        assert app.session.metadata["team_blocks"] == []


def test_after_a_summary_an_edit_keeps_a_text_mode_job_whose_call_is_still_there(tmp_path, monkeypatch):
    import agentx_dev.cli.app_usage as app_usage
    app = _app_job(tmp_path)
    filler = [m for i in range(3) for m in ({"role": "user", "content": f"more {i}"},
                                            {"role": "assistant", "content": "fine"})]
    history = ([{"role": "system", "content": "s"}] + call(1, "A", text_mode=True) + res(1, RAN, text_mode=True)
               + filler + call(2, "B", text_mode=True) + res(2, RAN, text_mode=True)
               + [{"role": "user", "content": "go 3"}, {"role": "assistant", "content": "done"}])
    assert history[11]["content"] == "go 2"
    job = rec("JB", "B")
    job["turnAt"] = 11
    app.session.history, app.session.metadata["team_blocks"] = history, [job]
    summary = [{"role": "user", "content": "[summary]"}, {"role": "assistant", "content": "ok"}]
    monkeypatch.setattr(app_usage, "needs_compaction", lambda *a, **k: True)
    monkeypatch.setattr(app_usage, "compact", lambda h, c, **k: types.SimpleNamespace(
        status="done", history=h[:1] + summary + h[11:], replaced=10, summary="earlier work"))
    app._compact_wait = 0
    app._maybe_compact()
    assert app.session.history[3]["content"] == "go 2"
    assert app.session.metadata["team_blocks"][0]["turnAt"] == 3           # re-based with the conversation
    app._prune_team_blocks(upto=7)                                         # an edit of "go 3"
    assert [r["job"] for r in app.session.metadata["team_blocks"]] == ["JB"]
    assert shown(app.session.history[:7], app.session.metadata["team_blocks"]) == ["call", "block:JB", "result(ok)"]


def test_a_later_run_of_the_same_task_that_kept_nothing_gets_no_block(tmp_path):
    """Text mode: the second job's plan made no rows, so it kept no record; the first job's stays under its own
    call instead of moving to the newest one."""
    from tests.subagent_helpers import step
    from tests.test_cli_app import Lines, make_app
    from tests.test_cli_team_app import team_model
    plans = []

    def steps(prompt):
        plans.append(prompt)
        return [step("s1", "explorer", "look")] if len(plans) == 1 else []

    model = team_model(steps, main=("call", "final", "call", "final"))
    original = model._script_fn
    model._script_fn = lambda messages: original(messages).replace(" (batch 3)", "")   # the same task again
    app, _, _, _ = make_app(tmp_path, Lines("do job", "again", "/exit"), model, mode="auto")
    app.start()
    kept = app.session.metadata["team_blocks"]
    assert len(kept) == 1 and len(plans) >= 2
    block = "block:" + kept[0]["job"]
    out = shown(app.session.history, kept)
    assert out.count("call") == 2 and out.count(block) == 1
    assert out[out.index("call") + 1] == block                            # under the first call, not the second


def test_two_team_calls_in_one_batch_each_keep_their_own_call(tmp_path, monkeypatch):
    """A natively bound model may send two ``start_team_job`` calls in one parallel batch: the jobs run one after
    the other (they share the window's live block), and each record is kept under its own call."""
    import threading
    import agentx_dev.cli.app as app_module
    from agentx_dev.Runner.AgentRun import _TOOL_CALL_ID
    app = _app_job(tmp_path)
    app.session.metadata["team_blocks"] = []
    running, most, second_in = [], [0], threading.Event()

    def fake_job(task, *, notify, **kwargs):
        running.append(task)
        most[0] = max(most[0], len(running))
        notify("plan", {"round": 1, "steps": [{"agent": "explorer", "query": task, "step_id": "s1"}]})
        if task == "job A":
            second_in.wait(0.5)                 # without the one-at-a-time rule job B starts meanwhile
        else:
            second_in.set()
        running.remove(task)
        return "Team job finished (outcome: done)."

    monkeypatch.setattr(app_module, "run_team_job", fake_job)

    def run(call_id, task):
        _TOOL_CALL_ID.set(call_id)
        app._start_team_job(task, "why")

    app._team_jobs, app._team_stalled = 0, False
    first = threading.Thread(target=run, args=("cA", "job A"))
    first.start()
    while not running:
        pass
    second = threading.Thread(target=run, args=("cB", "job B"))
    second.start()
    first.join(10)
    second.join(10)
    assert most[0] == 1
    assert {r["task"]: r["call"] for r in app.session.metadata["team_blocks"]} == {"job A": "cA", "job B": "cB"}
