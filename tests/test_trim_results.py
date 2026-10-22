"""Trimming old tool results from what the model is sent (not from the history that is kept).

Every request re-sends the whole conversation, so a file read at step 2 is paid for again at steps 3, 4, 5 ...
``trim_tool_results`` replaces the big results that are no longer fresh with a one-line stub."""

import json

import pytest

from agentx_dev import AgentRunner, AgentType, AsyncAgentRunner, DefaultTools, Permissions
from agentx_dev.Runner.Trim import TrimPolicy, trim_tool_results
from tests.conftest import MockModel, make_final, make_react_response

BIG = "x" * 3000


def tool_msg(name, content, call_id):
    return {"role": "tool", "name": name, "content": content, "tool_call_id": call_id}


def assistant_call(call_id, name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def conversation(n, name="read_path"):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    for i in range(n):
        msgs.append(assistant_call(f"c{i}", name, {"path": f"f{i}.py"}))
        msgs.append(tool_msg(name, f"result {i} " + BIG, f"c{i}"))
    return msgs


class TestTrimToolResults:
    def test_the_newest_results_stay_whole_and_older_big_ones_become_a_stub(self):
        msgs = conversation(8)
        out = trim_tool_results(msgs, TrimPolicy(keep_last=3, keep_chars=0))
        results = [m for m in out if m["role"] == "tool"]
        assert [len(m["content"]) > 1000 for m in results] == [False] * 5 + [True] * 3
        assert "read_path result for f0.py" in results[0]["content"] and "3,0" in results[0]["content"]
        assert "grep that file or read only the lines you need" in results[0]["content"]
        assert "Run the tool again" not in results[0]["content"]           # it must not invite a re-read

    def test_the_input_is_not_changed_and_pairing_survives(self):
        msgs = conversation(8)
        before = json.dumps(msgs)
        out = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=0))
        assert json.dumps(msgs) == before and len(out) == len(msgs)
        for original, trimmed in zip(msgs, out):
            assert original["role"] == trimmed["role"]
            assert original.get("tool_call_id") == trimmed.get("tool_call_id")
            assert original.get("name") == trimmed.get("name")

    def test_small_results_are_always_kept(self):
        msgs = conversation(6)
        msgs[3]["content"] = "ok, 12 lines"                      # an old but small result
        out = trim_tool_results(msgs, TrimPolicy(keep_last=1, keep_chars=0))
        assert out[3]["content"] == "ok, 12 lines"

    def test_a_skill_is_never_trimmed_and_does_not_use_up_the_window(self):
        msgs = conversation(4)
        msgs.insert(2, assistant_call("s0", "use_skill", {"name": "brainstorming"}))
        msgs.insert(3, tool_msg("use_skill", "SKILL TEXT " + BIG, "s0"))
        out = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=0))
        assert out[3]["content"].startswith("SKILL TEXT")           # kept whole, though it is the oldest
        kept = [m for m in out if m["role"] == "tool" and m["name"] == "read_path" and len(m["content"]) > 1000]
        assert len(kept) == 2                                       # and the window still holds two file reads

    def test_text_mode_observations_are_trimmed_too(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(6):
            msgs.append({"role": "assistant", "content": make_react_response("read_path", {"path": f"f{i}"})})
            msgs.append({"role": "user", "content": f"Observation: result {i} " + BIG})
        out = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=0))
        observations = [m["content"] for m in out if m["content"].startswith("Observation:")]
        assert [len(o) > 1000 for o in observations] == [False] * 4 + [True] * 2
        assert observations[0].startswith("Observation: [Earlier")

    def test_big_arguments_of_old_calls_are_shortened_and_stay_valid_json(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        msgs.append(assistant_call("w0", "write_file", {"path": "a.py", "content": BIG}))
        msgs.append(tool_msg("write_file", "wrote a.py", "w0"))
        for i in range(4):
            msgs.append(assistant_call(f"c{i}", "read_path", {"path": f"f{i}.py"}))
            msgs.append(tool_msg("read_path", f"r{i} " + BIG, f"c{i}"))
        old = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=0))[2]
        arguments = json.loads(old["tool_calls"][0]["function"]["arguments"])
        assert arguments["path"] == "a.py" and len(arguments["content"]) < 300 and "removed" in arguments["content"]
        newest = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=0))[-2]
        assert json.loads(newest["tool_calls"][0]["function"]["arguments"]) == {"path": "f3.py"}

    def test_trimming_twice_changes_nothing_more(self):
        once = trim_tool_results(conversation(8), TrimPolicy(keep_last=2, keep_chars=0))
        assert trim_tool_results(once, TrimPolicy(keep_last=2, keep_chars=0)) == once

    def test_too_few_results_means_no_change(self):
        msgs = conversation(3)
        assert trim_tool_results(msgs, TrimPolicy(keep_last=4, keep_chars=0)) == msgs


def reading_runner(tmp_path, steps, **kw):
    """A runner over real read_path with a scripted text-mode model; returns (runner, sizes of each request)."""
    for i in range(steps):
        (tmp_path / f"f{i}.py").write_text(f"# file {i}\n" + "y = 2\n" * 700, encoding="utf-8")   # 4.2 KB, each different
    sizes = []

    def script(messages):
        sizes.append(sum(len(str(m["content"])) for m in messages))
        step = len(sizes) - 1
        if step < steps:
            return make_react_response("read_path", {"path": str(tmp_path / f"f{step}.py")})
        return make_final("done")

    runner = AgentRunner(model=MockModel(script=script), agent=AgentType.ReAct, verbose=False, auto_cache=False,
                         max_iterations=steps + 2, permissions=Permissions.read_only([str(tmp_path)]), **kw)
    return runner, sizes


class TestRunnerOption:
    def test_default_sends_everything_every_time(self, tmp_path):
        runner, sizes = reading_runner(tmp_path, 10)
        runner.invoke("read them")
        assert all(b > a for a, b in zip(sizes, sizes[1:]))
        assert sizes[-1] - sizes[0] > 9 * 4000                   # ten whole files by the last request

    def test_with_trimming_the_requests_stop_growing(self, tmp_path):
        runner, sizes = reading_runner(tmp_path, 10, trim_results=TrimPolicy(keep_last=2, keep_chars=0))
        completion = runner.invoke("read them")
        assert sizes[-1] - sizes[2] < 3000                       # about two files plus short stubs
        assert "done" in str(getattr(completion, "content", completion))

    def test_true_means_the_default_policy(self, tmp_path):
        runner, sizes = reading_runner(tmp_path, 14, trim_results=True)
        runner.invoke("read them")
        assert sizes[-1] < sizes[0] + 40_000 + 14 * 300          # about 40,000 characters of results stay whole
        assert sizes[-1] > sizes[0] + 4 * 4000                   # and more than the four newest

    def test_the_stored_history_is_not_trimmed(self, tmp_path):
        runner, sizes = reading_runner(tmp_path, 8, trim_results=TrimPolicy(keep_last=2, keep_chars=0))
        completion = runner.invoke("read them")
        results = [m for m in completion.history if "y = 2" in str(m.get("content", ""))]
        assert len(results) == 8                                 # all eight kept whole in what is saved

    def test_the_async_runner_takes_the_option_too(self, tmp_path):
        runner = AsyncAgentRunner(model=MockModel(script=[]), agent=AgentType.ReAct, verbose=False, auto_cache=False,
                                  permissions=Permissions.read_only([str(tmp_path)]), trim_results=True)
        assert runner._trim_policy == TrimPolicy()

    def test_wrong_values_are_refused(self, tmp_path):
        with pytest.raises(TypeError):
            AgentRunner(model=MockModel(script=[]), agent=AgentType.ReAct, trim_results="yes")


class TestBudgetAndDuplicates:
    """Keeping only the newest four results made helpers re-read the same files again and again: the window is
    now a size budget, and only a copy that a newer identical copy makes redundant is stubbed inside it."""

    def test_the_window_holds_results_up_to_the_size_budget(self):
        msgs = conversation(10)                                 # results of about 3,000 characters
        out = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=10_000))
        whole = [len(m["content"]) > 1000 for m in out if m["role"] == "tool"]
        assert whole == [False] * 7 + [True] * 3                # 3 x 3,010 fits in 10,000, a 4th would not

    def test_at_least_keep_last_stay_whole_even_when_they_are_bigger_than_the_budget(self):
        out = trim_tool_results(conversation(6), TrimPolicy(keep_last=2, keep_chars=1_000))
        assert [len(m["content"]) > 1000 for m in out if m["role"] == "tool"] == [False] * 4 + [True] * 2

    def test_the_default_budget(self):
        assert TrimPolicy().keep_chars == 40_000 and TrimPolicy().keep_last == 4

    def test_an_older_identical_copy_is_stubbed_even_inside_the_window(self):
        msgs = conversation(3)
        msgs.append(assistant_call("again", "read_path", {"path": "f0.py"}))
        msgs.append(tool_msg("read_path", msgs[3]["content"], "again"))           # the same file, read again
        out = trim_tool_results(msgs, TrimPolicy())
        assert "same text comes again later" in out[3]["content"]
        assert out[-1]["content"] == msgs[3]["content"]                             # the newest copy stays
        assert [len(m["content"]) > 1000 for m in out if m["role"] == "tool"] == [False, True, True, True]

    def test_identical_small_results_are_left_alone(self):
        msgs = conversation(2)
        msgs[3]["content"] = msgs[5]["content"] = "ok"
        assert trim_tool_results(msgs, TrimPolicy()) == msgs


class TestFileReadStubs:
    """A trimmed file read names the file and its size, so a check needs a grep or a few lines, not a re-read."""

    def test_the_stub_names_the_path_and_line_count(self):
        msgs = conversation(6)
        msgs[3]["content"] = "line\n" * 900                    # 900 lines + an empty last one
        out = trim_tool_results(msgs, TrimPolicy(keep_last=1, keep_chars=0))
        assert out[3]["content"].startswith("[Earlier read_path result for f0.py (4,500 characters, 901 lines)")
        assert "offset and limit" in out[3]["content"]

    def test_a_slice_stub_gives_the_line_range(self):
        msgs = conversation(6)
        msgs[3]["content"] = "\n".join(f"{n}\tcode line {n} " + "y" * 20 for n in range(120, 181)) + \
            "\n[Showing lines 120-180 of 900; the output is cut at 30,000 characters.]"
        out = trim_tool_results(msgs, TrimPolicy(keep_last=1, keep_chars=0))
        assert "lines 120-180)" in out[3]["content"]

    def test_other_tools_keep_the_plain_stub(self):
        msgs = conversation(6, name="grep")
        out = trim_tool_results(msgs, TrimPolicy(keep_last=1, keep_chars=0))
        assert out[3]["content"].startswith("[Earlier grep result (3,")
        assert " for f0.py" not in out[3]["content"] and "only if you need its exact text" in out[3]["content"]

    def test_a_duplicate_names_the_path_too(self):
        msgs = conversation(3)
        msgs[-1]["content"] = msgs[3]["content"]                 # f0.py's text again, as the newest result
        out = trim_tool_results(msgs, TrimPolicy(keep_last=5, keep_chars=0))
        assert out[3]["content"].startswith("[read_path result for f0.py (")
        assert "same text comes again later" in out[3]["content"]

    def test_an_odd_path_is_quoted_safely(self):
        msgs = conversation(4)
        msgs[2] = assistant_call("c0", "read_path", {"path": "a\nb]" + "z" * 400})
        out = trim_tool_results(msgs, TrimPolicy(keep_last=1, keep_chars=0))
        stub = out[3]["content"]
        assert "\n" not in stub and "a?b]" in stub and "..." in stub and len(stub) < 600

    def test_a_call_without_a_path_or_with_bad_arguments_falls_back(self):
        msgs = conversation(4)
        msgs[2]["tool_calls"][0]["function"]["arguments"] = "{not json"
        msgs[4]["tool_calls"][0]["function"]["arguments"] = json.dumps({"file": "x.py"})
        out = trim_tool_results(msgs, TrimPolicy(keep_last=1, keep_chars=0))
        assert out[3]["content"].startswith("[Earlier read_path result (3,")
        assert out[5]["content"].startswith("[Earlier read_path result (3,")

    def test_trimming_its_own_output_changes_nothing(self):
        msgs = conversation(8)
        once = trim_tool_results(msgs, TrimPolicy(keep_last=2, keep_chars=0))
        assert trim_tool_results(once, TrimPolicy(keep_last=2, keep_chars=0)) == once
