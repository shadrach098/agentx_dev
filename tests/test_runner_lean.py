"""A lean request for a small window: the short skill index, ``compact_prompt=`` and ``fallback_tools=``, the tools a
run switches to when it moves to its fallback model."""

import json

from agentx_dev import AgentRunner, AgentType, StandardTool
from agentx_dev.Skills import Skills, attach_skills
from agentx_dev.Skills.Store import INDEX_HEADER, SHORT_INDEX_SKILLS, Skill, index_block
from tests.test_runner_fallback import KeyRejected, Local, Paid, respond
from tests.test_skills_store import make_skill


# -- the short skill index ----------------------------------------------------------------------------------------

def _skills(count, source):
    return [Skill(f"{source[:3]}-{i:02d}", f"Description number {i} " + "word " * 30, "body", source=source)
            for i in range(count)]


def test_the_short_index_lists_at_most_20_built_in_first_with_short_descriptions():
    skills = _skills(30, "own") + _skills(22, "bundled")
    text = index_block(skills, short=True)
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == SHORT_INDEX_SKILLS == 20
    assert all(line.startswith("- bun-") for line in lines)              # the built-in ones first: 22 of them
    assert all(len(line.split(": ", 1)[1]) <= 60 for line in lines)
    assert text.splitlines()[-1] == ("(32 more skills are not listed here. use_skill opens any skill by name; a name "
                                     "it does not know gets the list of all of them.)")
    assert INDEX_HEADER not in text


def test_the_short_index_puts_the_users_own_after_the_built_in_ones_in_their_order():
    installed = Skill("zz-installed", "Installed one.", "b", source="installed")
    skills = _skills(3, "own") + _skills(2, "bundled") + [installed]
    lines = [line for line in index_block(skills, short=True).splitlines() if line.startswith("- ")]
    assert [line.split(":")[0] for line in lines] == ["- bun-00", "- bun-01", "- own-00", "- own-01", "- own-02",
                                                     "- zz-installed"]
    assert "more skills" not in index_block(skills, short=True)         # all listed: no closing line


def test_the_full_index_is_unchanged_by_the_new_parameter():
    skills = _skills(30, "own") + _skills(22, "bundled")
    assert index_block(skills) == index_block(skills, short=False)
    assert index_block(skills).startswith(INDEX_HEADER)


def test_use_skill_opens_a_skill_the_short_index_leaves_out(tmp_path):
    root = tmp_path / "skills"
    for i in range(25):
        make_skill(root, f"s{i:02d}", name=f"s{i:02d}", description=f"Skill {i}.", body=f"Body of {i}.")
    tools, index = attach_skills(Skills(root, seed=False), [], None, short=True)
    assert "- s24:" not in index and "5 more skills" in index
    use_skill = next(t for t in tools if t.name == "use_skill")
    assert "Body of 24." in use_skill.func(name="s24")


def test_the_short_index_still_obeys_the_trust_check(tmp_path):
    root = tmp_path / "skills"
    for i in range(25):
        make_skill(root, f"s{i:02d}", name=f"s{i:02d}", description=f"Skill {i}.", body=f"Body of {i}.")
    held = Skills(root, seed=False, check=lambda skill: "changed since you approved it" if skill.name == "s24" else "")
    tools, index = attach_skills(held, [], None, short=True)
    use_skill = next(t for t in tools if t.name == "use_skill")
    assert use_skill.func(name="s24").startswith("ERROR: no skill named 's24'")
    assert "4 more skills" in index


# -- compact_prompt and fallback_tools ----------------------------------------------------------------------------

def tool(name, seen=None):
    def run(x):
        if seen is not None:
            seen.append((name, x))
        return f"{name} did {x}"
    return StandardTool(func=run, name=name, description=f"The {name} tool. " + "Long description. " * 20)


class PaidNative(Paid):
    prefers_native_tools = True


def native_calls(*calls):
    return {"type": "tool_use", "tool_calls": [{"name": name, "input": {"input": arg}, "id": call_id}
                                               for name, arg, call_id in calls]}


def skill_folder(tmp_path, count=25):
    root = tmp_path / "skills"
    for i in range(count):
        make_skill(root, f"s{i:02d}", name=f"s{i:02d}", description=f"Skill {i} " + "word " * 30)
    return Skills(root, seed=False)


def build(primary, fallback, tools, **kw):
    return AgentRunner(model=primary, agent=AgentType.ReAct, tools=tools, fallback_model=fallback, verbose=False,
                       auto_cache=False, **kw)


def test_at_the_switch_the_fallback_gets_its_own_tools_and_a_compact_prompt(tmp_path):
    seen = []
    core, web = tool("core", seen), tool("web", seen)
    fallback = Local([native_calls(("web", "again", "f1")), respond("done")])
    primary = PaidNative([native_calls(("core", "a", "c1"), ("web", "b", "c2")), KeyRejected()])
    runner = build(primary, fallback, [core, web], skills=skill_folder(tmp_path),
                   compact_prompt=lambda model: getattr(model, "is_local", False),
                   fallback_tools=lambda model: [core])
    assert runner.invoke("go").content == "done"
    assert {spec["name"] for spec in primary.tools_seen[0]} == {"core", "web", "use_skill", "respond"}
    assert {spec["name"] for spec in fallback.tools_seen[0]} == {"core", "use_skill", "respond"}
    messages = fallback.tool_calls_made[0]
    system = str(messages[0]["content"])
    assert "Long description" not in system                                # the specs carry the descriptions
    assert "core, use_skill" in system and "more skills are not listed" in system
    # the call to the tool the fallback does not have is text now; the kept one stays a call
    calls = [c["function"]["name"] for m in messages for c in (m.get("tool_calls") or [])]
    assert calls == ["core"]
    texts = " ".join(str(m.get("content")) for m in messages)
    assert "[earlier step] called web with " in texts and "[earlier result] web did b" in texts
    # the fallback cannot run the tool it was not given
    # (the primary's two calls of one step run side by side: either may be seen first)
    assert ("web", "again") not in seen and sorted(seen) == [("core", "a"), ("web", "b")]
    # the primary's request was the full one
    primary_system = str(primary.tool_calls_made[0][0]["content"])
    assert "Long description" in primary_system and "more skills are not listed" not in primary_system


def test_without_the_new_settings_a_runner_sends_exactly_what_it_did(tmp_path):
    def requests(**kw):
        primary = PaidNative([respond("done")])
        build(primary, Local([]), [tool("core"), tool("web")], skills=skill_folder(tmp_path), **kw).invoke("go")
        return json.dumps(primary.tool_calls_made[0]), json.dumps(primary.tools_seen[0])

    plain = requests()
    assert requests(compact_prompt=lambda model: getattr(model, "is_local", False),
                    fallback_tools=lambda model: []) == plain
    assert requests(compact_prompt=False) == plain


def test_a_compact_runner_on_a_local_model_from_the_start(tmp_path):
    model = Local([respond("done")])
    runner = AgentRunner(model=model, agent=AgentType.ReAct, tools=[tool("core")], verbose=False, auto_cache=False,
                         skills=skill_folder(tmp_path), compact_prompt=True)
    runner.invoke("go")
    system = str(model.tool_calls_made[0][0]["content"])
    assert "Long description" not in system and "6 more skills are not listed" in system     # 25 and custom-tools
    assert runner.fixed_request_chars() < AgentRunner(
        model=Local([]), agent=AgentType.ReAct, tools=[tool("core")], verbose=False, auto_cache=False,
        skills=skill_folder(tmp_path)).fixed_request_chars()


def test_reset_fallback_puts_the_primary_tools_back(tmp_path):
    core, web = tool("core"), tool("web")
    runner = build(PaidNative([KeyRejected()]), Local([respond("done")]), [core, web], skills=skill_folder(tmp_path),
                   compact_prompt=lambda model: getattr(model, "is_local", False), fallback_tools=[core],
                   system_addendum="RULES")
    before = (runner.registry.names, runner.system_addendum, runner._system_prompt(""))
    runner.invoke("go")
    assert runner.registry.names == ["core", "use_skill"]
    runner.reset_fallback()
    assert (runner.registry.names, runner.system_addendum, runner._system_prompt("")) == before


def test_a_failing_fallback_tools_function_keeps_the_tools(tmp_path):
    def broken(model):
        raise RuntimeError("no")

    fallback = Local([respond("done")])
    runner = build(PaidNative([KeyRejected()]), fallback, [tool("core"), tool("web")], fallback_tools=broken)
    assert runner.invoke("go").content == "done"
    assert {spec["name"] for spec in fallback.tools_seen[0]} == {"core", "web", "respond"}


def test_fallback_tools_none_keeps_the_tools_and_the_fallback_addendum_still_applies(tmp_path):
    fallback = Local([respond("done")])
    runner = build(PaidNative([KeyRejected()]), fallback, [tool("core"), tool("web")], skills=skill_folder(tmp_path),
                   fallback_tools=lambda model: None, compact_prompt=lambda model: getattr(model, "is_local", False),
                   system_addendum="LONG", fallback_addendum="SHORT")
    runner.invoke("go")
    system = str(fallback.tool_calls_made[0][0]["content"])
    assert {spec["name"] for spec in fallback.tools_seen[0]} == {"core", "web", "use_skill", "respond"}
    assert "SHORT" in system and "LONG" not in system and "more skills are not listed" in system
