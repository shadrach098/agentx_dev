"""A runner's log (``log=``): one event per step while it runs, to stderr, a file, a logger or a function."""

import asyncio
import io
import json
import logging

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, Persistence, RunLog, StandardTool
from tests.conftest import MockModel, make_final, make_react_response

SECRET = "sk-ant-" + "a" * 30


def ok(x):
    return "fine " + str(x)


def bad(x):
    raise ValueError(f"boom {SECRET}")


def tools():
    return [StandardTool(name="ok_tool", func=ok, description="works"),
            StandardTool(name="bad_tool", func=bad, description="fails")]


def script():
    return [make_react_response("ok_tool", {"input": "a"}), make_react_response("bad_tool", {"input": "b"}),
            make_final("all done")]


def runner(log, cls=AgentRunner, **kw):
    return cls(model=MockModel(script=script()), agent=AgentType.ReAct, tools=tools(), verbose=False, log=log, **kw)


def kinds(events):
    return [(e["event"], e.get("tool"), e.get("ok")) for e in events]


EXPECTED = [("run.start", None, None), ("model.call", None, None), ("tool", "ok_tool", True),
            ("model.call", None, None), ("tool", "bad_tool", False), ("model.call", None, None),
            ("run.end", None, None)]


class TestWhatIsLogged:
    def test_a_sync_run_logs_its_start_model_calls_tools_and_end(self):
        events = []
        assert runner(events.append).invoke("do it").content == "all done"
        assert kinds(events) == EXPECTED
        assert len({e["run"] for e in events}) == 1                         # one run id for the whole run
        end = events[-1]
        assert end["outcome"] == "done" and end["model_calls"] == 3 and end["tool_calls"] == 2
        assert end["tool_errors"] == 1 and end["answer"] == "all done" and end["tokens_in"] == 30

    def test_an_async_run_logs_the_same(self):
        events = []
        assert asyncio.run(runner(events.append, cls=AsyncAgentRunner).ainvoke("do it")).content == "all done"
        assert kinds(events) == EXPECTED

    def test_stream_logs_too(self):
        events = []
        assert [e["type"] for e in runner(events.append).stream("do it")][-1] == "completion"
        assert kinds(events) == EXPECTED

    def test_a_persistent_run_logs_too(self):
        events = []
        r = runner(events.append, persistence=Persistence(max_minutes=5))
        r.invoke("do it")
        assert events[0]["event"] == "run.start" and events[-1]["event"] == "run.end"

    def test_secrets_are_redacted_and_long_text_is_cut(self):
        events = []
        runner(events.append).invoke("do it")
        assert SECRET not in json.dumps(events) and "[REDACTED]" in json.dumps(events)
        short = []
        runner(RunLog(short.append, preview=40)).invoke("x" * 500)
        assert len(short[0]["query"]) == 40 and short[0]["query"].endswith("...")

    def test_a_run_that_raises_logs_the_error_and_still_raises(self):
        events = []

        def broken(messages):
            raise RuntimeError("provider down")
        r = AgentRunner(model=MockModel(script=broken), agent=AgentType.ReAct, tools=[], verbose=False,
                        log=events.append)
        with pytest.raises(RuntimeError):
            r.invoke("hi")
        assert events[-1]["event"] == "run.error" and "provider down" in events[-1]["error"]
        assert any(e["event"] == "model.call" and "error" in e for e in events)

    def test_no_log_by_default_and_a_failing_log_never_stops_the_run(self):
        assert runner(None).log is None and runner(None).invoke("do it").content == "all done"

        def explode(event):
            raise OSError("disk full")
        assert runner(explode).invoke("do it").content == "all done"


class TestWhereItGoes:
    def test_true_prints_readable_lines_to_stderr(self, capsys):
        runner(True).invoke("do it")
        err = capsys.readouterr().err
        assert "start MockModel: do it" in err and "tool bad_tool ERROR" in err and "end done" in err

    def test_a_jsonl_file_gets_one_json_object_per_event(self, tmp_path):
        path = tmp_path / "logs" / "run.jsonl"
        runner(str(path)).invoke("do it")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert kinds(rows) == EXPECTED

    def test_another_file_gets_readable_lines(self, tmp_path):
        path = tmp_path / "run.log"
        runner(path).invoke("do it")
        assert "tool ok_tool ok" in path.read_text(encoding="utf-8")

    def test_a_logger_gets_lines_and_the_event(self):
        stream = io.StringIO()
        log = logging.getLogger("test.agentx.runlog")
        log.setLevel(logging.INFO)
        handler = logging.StreamHandler(stream)
        seen = []
        handler.addFilter(lambda record: seen.append(record.agentx) or True)
        log.addHandler(handler)
        try:
            runner(log).invoke("do it")
        finally:
            log.removeHandler(handler)
        assert "end done" in stream.getvalue() and kinds(seen) == EXPECTED

    def test_chain_writes_to_each(self):
        a, b = [], []
        runner(RunLog.chain(RunLog.to(a.append), None, RunLog.to(b.append))).invoke("do it")
        assert kinds(a) == kinds(b) == EXPECTED

    def test_anything_else_is_refused(self):
        with pytest.raises(TypeError):
            RunLog.to(42)
