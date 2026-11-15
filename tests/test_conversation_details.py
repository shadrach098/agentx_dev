"""Details for a conversation opened again: its tokens add up across every start (the model's own counters start
at zero again), and the team jobs it ran are read back from disk."""

from datetime import datetime
from types import SimpleNamespace as NS

from agentx_dev.Session import Session
from agentx_dev.cli.team_record import TeamRecord, read_jobs


def model_with(calls=0, tokens_in=0, tokens_out=0, cached=0):
    usage = NS(total_calls=calls, total_input_tokens=tokens_in, total_output_tokens=tokens_out,
               total_cache_read_tokens=cached)
    return NS(usage=usage)


def run(model, calls, tokens_in, tokens_out, cached=0):
    u = model.usage
    u.total_calls += calls
    u.total_input_tokens += tokens_in
    u.total_output_tokens += tokens_out
    u.total_cache_read_tokens += cached


class TestTokensAddUpAcrossStarts:
    def test_a_reopened_conversation_adds_to_its_total_instead_of_replacing_it(self, tmp_path):
        first = model_with()
        session = Session.start(NS(model=first))
        run(first, 4, 1000, 80, 600)
        session._count_usage(first)
        path = tmp_path / "s.json"
        session.save(path)
        again = Session.load(path)
        second = model_with()                        # a new start: the model counts from zero
        again.attach(NS(model=second))
        run(second, 2, 300, 20, 100)
        assert again.usage_now() == {"calls": 6, "input_tokens": 1300, "output_tokens": 100, "cached_input_tokens": 700}
        again._count_usage(second)
        assert again.usage == {"calls": 6, "input_tokens": 1300, "output_tokens": 100, "cached_input_tokens": 700}

    def test_a_model_used_before_counts_only_what_this_conversation_used(self):
        shared = model_with(10, 5000, 500)           # already used by another conversation in this run
        session = Session.start(NS(model=shared))
        run(shared, 1, 200, 10)
        session._count_usage(shared)
        assert session.usage["calls"] == 1 and session.usage["input_tokens"] == 200

    def test_switching_model_mid_conversation_adds_to_the_total(self):
        a, b = model_with(), model_with()
        session = Session.start(NS(model=a))
        run(a, 1, 100, 10)
        session._count_usage(a)
        session.attach(NS(model=b))
        run(b, 1, 50, 5)
        session._count_usage(b)
        assert session.usage["calls"] == 2 and session.usage["input_tokens"] == 150

    def test_the_counters_are_not_written_to_the_file(self, tmp_path):
        session = Session.start(NS(model=model_with(1, 1, 1)))
        assert "usage_seen" not in session.model_dump_json()


class TestTeamJobsReadBack:
    def test_a_job_names_its_conversation_and_is_read_back_with_its_steps(self, tmp_path):
        record = TeamRecord(tmp_path, "Fix the invite link", conversation="abc-123",
                            when=datetime(2026, 11, 1, 10, 0, 0))
        record.plan([{"agent": "fixer", "label": "repair", "query": "repair it"}])
        step = NS(slot=1, helper="fixer@gpt", label="repair", query="Repair the invite link", files={}, calls={},
                  tool_uses=7, tokens=12_345)
        record.started(step)
        record.finished(step, NS(content="Fixed.", error=""), ok=True)
        assert "Conversation: abc-123" in (record.folder / "job.md").read_text(encoding="utf-8")
        jobs = read_jobs(tmp_path, "abc-123")
        assert len(jobs) == 1 and jobs[0]["title"] == "Fix the invite link"
        assert jobs[0]["steps"] == [{"helper": "fixer@gpt", "label": "repair", "state": "done", "toolUses": 7,
                                     "tokens": 12_345, "query": "Repair the invite link"}]
        assert read_jobs(tmp_path, "another-conversation") == []

    def test_an_older_job_is_matched_by_its_task_then_by_when_it_ran(self, tmp_path):
        TeamRecord(tmp_path, "Deploy the site to the VPS", when=datetime(2026, 10, 29, 21, 45, 51))
        TeamRecord(tmp_path, "Audit the repo", when=datetime(2026, 10, 29, 21, 46, 10))
        window = dict(since=datetime(2026, 10, 29, 21, 0), until=datetime(2026, 10, 29, 22, 0))
        by_task = read_jobs(tmp_path, "x", tasks=["Deploy the site to the VPS"], **window)
        assert [j["title"] for j in by_task] == ["Deploy the site to the VPS"]
        by_time = read_jobs(tmp_path, "x", **window)
        assert len(by_time) == 2
        assert read_jobs(tmp_path, "x", since=datetime(2026, 10, 30), until=datetime(2026, 10, 31)) == []

    def test_details_show_a_reopened_conversations_jobs_and_tokens(self, tmp_path):
        from agentx_dev.cli.webui import WebFront
        cwd = tmp_path / "proj"
        record = TeamRecord(cwd / ".agentx" / "team", "Ship it", conversation="sess-1")
        record.started(NS(slot=1, helper="coder@gpt", label="build", query="Build it"))
        session = Session.start(NS(model=model_with()))
        session.session_id = "sess-1"
        session.usage = {"calls": 9, "input_tokens": 290_000, "output_tokens": 1_200, "cached_input_tokens": 5}
        front = WebFront(cwd=cwd, home=tmp_path / "home", open_it=False)
        front.app = NS(session=session, _team_log=[], _memory_on=True, _usage_totals=lambda: (0, 0, 0, 0),
                       model=None, token_limit=0, max_steps=0, mcp=None, project_memory=None)
        details = front.details()
        assert details["usage"]["requests"] == 9 and details["usage"]["input"] == 290_000
        team = details["team"]
        assert team["saved"] and team["steps"][0]["helper"] == "coder@gpt" and team["steps"][0]["job"] == "Ship it"
        assert team["steps"][0]["state"] == "running" and not team["steps"][0]["done"]
