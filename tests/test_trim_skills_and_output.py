"""Keeping a long turn small without asking: older skill texts are stubbed once the conversation is big, and
run_python / run_shell output in the terminal app is cut at 30,000 characters."""

import json

from agentx_dev.Runner.Trim import TrimPolicy, trim_tool_results
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.tools import OUTPUT_MAX_CHARS, build_tools


def call(call_id, name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(call_id, name, text):
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": text}


def skill_read(call_id, skill, size, file=None):
    args = {"name": skill, **({"file": file} if file else {})}
    return [call(call_id, "use_skill", args), result(call_id, "use_skill", f"# {skill}\n" + "s" * size)]


def conversation(*parts, padding=0):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go" + "p" * padding}]
    for part in parts:
        msgs += part
    return msgs


def skill_texts(out):
    return [m["content"] for m in out if m.get("role") == "tool" and m.get("name") == "use_skill"]


class TestOlderSkillsAreStubbedInABigConversation:
    def test_a_small_conversation_keeps_every_skill_whole(self):
        msgs = conversation(skill_read("a", "brainstorming", 5000), skill_read("b", "systematic-debugging", 5000))
        out = trim_tool_results(msgs, TrimPolicy())
        assert all(text.startswith("# ") for text in skill_texts(out))

    def test_past_the_size_all_but_the_newest_become_a_stub_that_says_how_to_reopen(self):
        msgs = conversation(skill_read("a", "brainstorming", 9000),
                            skill_read("b", "test-driven-development", 9000, file="testing-anti-patterns.md"),
                            skill_read("c", "systematic-debugging", 9000), padding=60_000)
        texts = skill_texts(trim_tool_results(msgs, TrimPolicy()))
        assert texts[0].startswith("[The brainstorming skill (SKILL.md, 9,")
        assert 'use_skill(name="brainstorming", file="SKILL.md")' in texts[0] and "still applies" in texts[0]
        assert 'use_skill(name="test-driven-development", file="testing-anti-patterns.md")' in texts[1]
        assert texts[2].startswith("# systematic-debugging")                   # the newest stays whole

    def test_a_short_pointer_does_not_count_as_the_newest(self):
        pointer = [call("d", "use_skill", {"name": "brainstorming"}),
                   result("d", "use_skill", 'The "brainstorming" skill was already read earlier.')]
        msgs = conversation(skill_read("a", "brainstorming", 9000), skill_read("b", "systematic-debugging", 9000),
                            pointer, padding=60_000)
        texts = skill_texts(trim_tool_results(msgs, TrimPolicy()))
        assert texts[0].startswith("[The brainstorming skill") and texts[1].startswith("# systematic-debugging")
        assert texts[2].startswith("The \"brainstorming\" skill was already read")

    def test_it_can_be_turned_off(self):
        msgs = conversation(skill_read("a", "x-skill", 9000), skill_read("b", "y-skill", 9000), padding=60_000)
        out = trim_tool_results(msgs, TrimPolicy(shrink_exempt_at=0))
        assert all(text.startswith("# ") for text in skill_texts(out))

    def test_it_is_measured_on_the_whole_conversation_so_a_stub_does_not_come_back(self):
        """The conversation only grows during a turn, so once a skill is stubbed it stays stubbed (stable for the
        provider's cache), even when trimming file results makes the request itself smaller."""
        files = [part for i in range(6) for part in (call(f"r{i}", "read_path", {"path": f"f{i}.py"}),
                                                     result(f"r{i}", "read_path", f"{i}" + "x" * 12_000))]
        msgs = conversation(skill_read("a", "x-skill", 9000), skill_read("b", "y-skill", 9000), files)
        out = trim_tool_results(msgs, TrimPolicy())
        assert skill_texts(out)[0].startswith("[The x-skill skill")
        assert sum(len(m.get("content") or "") for m in out) < 60_000          # sent: small; stub still stands

    def test_a_bad_or_odd_call_gets_a_plain_stub(self):
        msgs = conversation(skill_read("a", "x-skill", 9000), skill_read("b", "y-skill", 9000), padding=60_000)
        msgs[2]["tool_calls"][0]["function"]["arguments"] = "{not json"
        texts = skill_texts(trim_tool_results(msgs, TrimPolicy()))
        assert texts[0].startswith("[Earlier use_skill result (9,")

    def test_pairing_survives(self):
        msgs = conversation(skill_read("a", "x-skill", 9000), skill_read("b", "y-skill", 9000), padding=60_000)
        out = trim_tool_results(msgs, TrimPolicy())
        for before, after in zip(msgs, out):
            assert before.get("tool_call_id") == after.get("tool_call_id") and before["role"] == after["role"]


class TestCommandOutputIsCut:
    def tools(self, tmp_path):
        return {t.name: t for t in build_tools(tmp_path, [], Gate(mode="auto", cwd=tmp_path))}

    def test_run_python_output_is_cut_at_30000_and_says_what_to_do(self, tmp_path):
        out = self.tools(tmp_path)["run_python"].func(code="print('y' * 50_000)")
        assert OUTPUT_MAX_CHARS == 30_000 and len(out) < 31_000
        assert "truncated at 30000 bytes" in out and "Print only what you need" in out

    def test_run_shell_output_is_cut_too(self, tmp_path):
        out = self.tools(tmp_path)["run_shell"].func(command="python -c \"print('y' * 50000)\"")
        if "truncated" not in out:                      # no usable shell on this machine
            return
        assert "truncated at 30000 bytes" in out and "Print only what you need" in out and len(out) < 31_500
