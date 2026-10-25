"""The terminal app keeps requests small: capped reads, trimmed old results, skills read once, summarising sooner."""

from agentx_dev.cli import compact as compact_module
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.team import build_helpers
from agentx_dev.cli.tools import READ_MAX_CHARS, build_tools
from agentx_dev.Runner.Trim import TrimPolicy
from agentx_dev.Session import Session
from agentx_dev.Skills import Skills
from tests.conftest import MockModel, make_final
from tests.test_cli_app import Lines, make_app
from tests.test_cli_memory_app import summarising_model

BIG = "x" * 10_000


def tool_turns(turns, size=10_000):
    """FC-style history: a question, a read_path call, its big result, an answer; ``turns`` times."""
    out = []
    for i in range(turns):
        out += [{"role": "user", "content": f"question {i}"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": f"c{i}", "type": "function",
                                 "function": {"name": "read_path", "arguments": f'{{"path": "f{i}.py"}}'}}]},
                {"role": "tool", "name": "read_path", "content": f"file {i} " + "y" * size, "tool_call_id": f"c{i}"},
                {"role": "assistant", "content": f"answer {i}"}]
    return out


def app_with_history(tmp_path, history, model):
    app, out, cwd, home = make_app(tmp_path, Lines(), model, width=300)
    app.model = model
    app.session = Session.start(app._make_runner(model))
    app.session.history = history
    return app, out


class TestSummarisingSooner:
    def test_the_threshold_is_about_fifteen_thousand_tokens(self):
        assert compact_module.THRESHOLD_CHARS == 60_000

    def test_a_long_plain_conversation_is_summarised(self, tmp_path):
        model = summarising_model()
        history = []
        for i in range(12):
            history += [{"role": "user", "content": f"question {i} " + "x" * 6_000}, {"role": "assistant", "content": f"a{i}"}]
        app, out = app_with_history(tmp_path, history, model)
        app._maybe_compact()
        assert model.calls_made["summary"] == 1

    def test_big_old_tool_results_do_not_count_because_the_model_is_not_sent_them(self, tmp_path):
        model = summarising_model()
        history = tool_turns(12)                       # 120 KB in the history, much less in what is sent
        assert sum(len(str(m.get("content", ""))) for m in history) > 60_000
        app, out = app_with_history(tmp_path, history, model)
        app._maybe_compact()
        assert model.calls_made["summary"] == 0 and app.session.history is history

    def test_summarising_makes_the_skill_texts_readable_again(self, tmp_path):
        model = summarising_model()
        history = []
        for i in range(12):
            history += [{"role": "user", "content": f"question {i} " + "x" * 6_000}, {"role": "assistant", "content": f"a{i}"}]
        app, out = app_with_history(tmp_path, history, model)
        use_skill = next(t for t in app.session.runner.tools if t.name == "use_skill")
        assert "list_directory" in use_skill.func("exploring-a-project")
        assert "already read" in use_skill.func("exploring-a-project")
        app._maybe_compact()
        assert "list_directory" in use_skill.func("exploring-a-project")      # its text is gone from the history, so it comes again


class TestRunnerSettings:
    def test_the_chat_runner_trims_old_results_and_remembers_skill_reads(self, tmp_path):
        app, out, cwd, home = make_app(tmp_path, Lines(), MockModel())
        runner = app._make_runner(MockModel())
        assert runner._trim_policy == TrimPolicy()
        use_skill = next(t for t in runner.tools if t.name == "use_skill")
        use_skill.func("exploring-a-project")
        assert "already read" in use_skill.func("exploring-a-project")

    def test_team_helpers_trim_too(self, tmp_path):
        tools = build_tools(tmp_path, [], Gate(cwd=tmp_path))
        helpers = build_helpers(MockModel(), tools, "BASE", skills=Skills(tmp_path / "skills"))
        assert all(runner._trim_policy == TrimPolicy(keep_last=1, keep_chars=40_000, step_chars=20_000)
                   for desc, runner in helpers.values())        # the chat's window: a smaller one caused re-reads

    def test_read_path_in_the_app_cuts_a_big_file_and_says_how_to_go_on(self, tmp_path):
        (tmp_path / "big.txt").write_text("\n".join(f"line {i} " + "z" * 60 for i in range(2_000)), encoding="utf-8")
        tools = {t.name: t for t in build_tools(tmp_path, [], Gate(cwd=tmp_path))}
        result = tools["read_path"].func(path="big.txt")
        assert READ_MAX_CHARS == 30_000 and len(result) < READ_MAX_CHARS + 400
        assert result.startswith("1\tline 0") and "offset=" in result
