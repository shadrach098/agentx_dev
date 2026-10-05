"""Specs, the SpawnConfig ceiling, and SpawnPolicy: the one place a sub-agent is built."""

import asyncio

import pytest

from agentx_dev import AgentRunner, AsyncAgentRunner, Persistence, StandardTool
from agentx_dev.SubAgents import (
    AgentSpec, SpawnConfig, SpawnPolicy, SpawnRefused, SpecError, parse_agent_spec,
    spawn_instruction, spec_from_legacy_spawn,
)
from tests.conftest import MockModel


def tool_names(runner):
    return sorted(runner.registry.names)


def policy(**cfg):
    cfg.setdefault("enabled", True)
    return SpawnPolicy(SpawnConfig(**cfg), MockModel(), verbose=False)


CEILING = dict(capabilities={"web", "files_read"}, allowed_paths=["./workspace"])


class TestSpecs:
    def test_parse_agent_spec_normalizes(self):
        s = parse_agent_spec({"name": " analyst ", "instructions": "  Compare prices.  ",
                              "tools": ["web", "web", " web_fetch "]})
        assert s == AgentSpec("analyst", "Compare prices.", ("web", "web_fetch"), "plan")

    def test_instructions_are_capped(self):
        s = parse_agent_spec({"name": "a", "instructions": "x" * 9000})
        assert len(s.instructions) == 4000

    @pytest.mark.parametrize("raw", [
        "nope", None, [],
        {"instructions": "i"},                                # no name
        {"name": "has space", "instructions": "i"},
        {"name": "x" * 41, "instructions": "i"},
        {"name": "a"},                                        # no instructions
        {"name": "a", "instructions": "   "},
        {"name": "a", "instructions": "i", "tools": "web"},   # not a list
        {"name": "a", "instructions": "i", "tools": [1]},
    ])
    def test_invalid_specs_raise(self, raw):
        with pytest.raises(SpecError):
            parse_agent_spec(raw)

    @pytest.mark.parametrize("name", ["__spawn__", "__x", "__", "delegate_1", "delegate_12"])
    def test_reserved_names_are_rejected(self, name):
        with pytest.raises(SpecError, match="name is reserved"):
            parse_agent_spec({"name": name, "instructions": "i"})

    @pytest.mark.parametrize("name", ["delegate", "delegate_x", "delegate_1a", "my__agent", "_private"])
    def test_names_close_to_the_reserved_ones_are_fine(self, name):
        assert parse_agent_spec({"name": name, "instructions": "i"}).name == name

    def test_a_legacy_spawn_name_is_only_checked_against_the_delegate_pattern(self):
        with pytest.raises(SpecError, match="name is reserved"):
            spec_from_legacy_spawn({"agent": "__spawn__", "name": "delegate_2", "description": "d"})
        assert spec_from_legacy_spawn({"agent": "__spawn__", "name": "__scout", "description": "d"}).name == "__scout"

    def test_legacy_spawn_step_becomes_a_spec(self):
        s = spec_from_legacy_spawn({"agent": "__spawn__", "name": "analyst",
                                    "description": "runs python", "capabilities": ["code", "files"]})
        assert s == AgentSpec("analyst", "runs python", ("code", "files"), "plan")
        with pytest.raises(SpecError):
            spec_from_legacy_spawn({"agent": "__spawn__", "name": "", "description": "d"})


class TestSpawnConfig:
    def test_modes_and_defaults(self):
        legacy = SpawnConfig(enabled=True)
        assert not legacy.ceiling_mode and legacy.effective_max_spawns == 3 and legacy.max_depth == 1
        assert SpawnConfig(enabled=True, capabilities=set()).ceiling_mode
        assert SpawnConfig(enabled=True, tools=[]).ceiling_mode
        assert SpawnConfig(enabled=True, capabilities={"web"}).effective_max_spawns == 6
        assert SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=2).effective_max_spawns == 2
        assert SpawnConfig(enabled=True, max_spawns=9).effective_max_spawns == 9

    def test_still_importable_from_the_old_places(self):
        import agentx_dev
        from agentx_dev.Supervisor import SpawnConfig as S2, SpawnRequest as R2
        assert S2 is SpawnConfig and agentx_dev.SpawnConfig is SpawnConfig and R2 is agentx_dev.SpawnRequest


class TestResolve:
    def test_clips_to_the_ceiling_and_reports_what_was_dropped(self):
        p = policy(capabilities={"web"})
        granted, pool, dropped = p.resolve(["web", "code", "bogus"])
        assert granted == ["web"] and pool == [] and dropped == ["code", "bogus"]

    def test_single_tool_names_map_to_the_narrowest_allowed_preset(self):
        p = policy(**CEILING)
        assert p.resolve(["read_path"])[0] == ["files_read"]
        assert p.resolve(["web_fetch"])[0] == ["web"]
        granted, _, dropped = p.resolve(["write_file", "run_python"])
        assert granted == [] and dropped == ["write_file", "run_python"]

    def test_files_subsumes_files_read(self):
        p = policy(capabilities={"files"})
        assert p.resolve(["files_read", "files"])[0] == ["files"]
        assert p.resolve(["files_read"])[0] == ["files_read"]      # "files" allows the read-only preset

    def test_pool_tools_resolve_by_name(self):
        mine = StandardTool(func=lambda x: x, name="lookup", description="look something up")
        p = policy(tools=[mine])
        granted, pool, dropped = p.resolve(["lookup", "web"])
        assert granted == [] and pool == [mine] and dropped == ["web"]      # no presets allowed in this ceiling

    def test_a_pool_tool_named_like_a_preset_word_is_granted_as_the_pool_tool(self):
        mine = StandardTool(func=lambda x: x, name="web", description="my own web tool")
        granted, pool, dropped = policy(tools=[mine], capabilities={"files_read"}).resolve(["web"])
        assert granted == [] and pool == [mine] and dropped == []
        granted, pool, dropped = policy(tools=[mine], capabilities={"web"}).resolve(["web"])
        assert granted == [] and pool == [mine] and dropped == []     # the pool wins over the preset

    def test_legacy_mode_allows_every_preset_word(self):
        granted, _, dropped = policy().resolve(["web", "files", "code", "delete", "nope"])
        assert granted == ["web", "files", "code", "delete"] and dropped == ["nope"]


class TestBuild:
    def test_builds_a_sync_runner_with_the_clipped_tools_and_the_instructions(self):
        p = policy(**CEILING)
        built = p.build(AgentSpec("analyst", "Compare prices. Return a table.", ("web", "files", "code")))
        r = built.runner
        assert isinstance(r, AgentRunner) and not isinstance(r, AsyncAgentRunner)
        assert built.granted == ["web"] and built.dropped == ["files", "code"]   # preset words must be allowed exactly
        assert "web_search" in tool_names(r) and "run_python" not in tool_names(r)
        assert "write_file" not in tool_names(r)
        assert "Compare prices. Return a table." in r.system_addendum
        assert "You were spawned by a Supervisor" in r.system_addendum
        assert "delegate" not in tool_names(r)                     # depth 1 == max_depth 1

    def test_the_read_only_preset_grants_read_tools_only(self):
        built = policy(**CEILING).build(AgentSpec("reader", "Read.", ("files_read",)))
        assert built.granted == ["files_read"]
        names = tool_names(built.runner)
        assert "read_path" in names and "grep" in names
        assert "write_file" not in names and "edit_file" not in names

    def test_async_policy_builds_an_async_runner(self):
        p = SpawnPolicy(SpawnConfig(enabled=True, **CEILING), MockModel(), is_async=True)
        built = p.build(AgentSpec("a", "Be useful.", ("web",)))
        assert isinstance(built.runner, AsyncAgentRunner) and "Be useful." in built.runner.system_addendum
        sync = p.build(AgentSpec("b", "Be useful.", ("web",)), is_async=False)
        assert type(sync.runner) is AgentRunner

    def test_persistence_is_inherited(self):
        persistence = Persistence(max_minutes=5)
        p = SpawnPolicy(SpawnConfig(enabled=True, **CEILING), MockModel(), persistence=persistence)
        runner = p.build(AgentSpec("a", "x", ("web",))).runner
        assert runner.persistence is persistence and runner.max_iterations == persistence.max_turns

    def test_a_spec_with_no_tools_gets_none(self):
        runner = policy(**CEILING).build(AgentSpec("thinker", "Just reason.")).runner
        assert tool_names(runner) == []

    def test_requested_tools_none_usable_is_a_refusal(self):
        with pytest.raises(SpawnRefused) as e:
            policy(**CEILING).build(AgentSpec("a", "x", ("code", "delete")))
        assert e.value.reason == "no usable tools"

    def test_disabled_and_limit_refusals(self):
        with pytest.raises(SpawnRefused, match="disabled"):
            policy(enabled=False).build(AgentSpec("a", "x"))
        p = policy(max_spawns=2, **CEILING)
        p.build(AgentSpec("a", "x", ("web",)))
        p.build(AgentSpec("b", "x", ("web",)))
        with pytest.raises(SpawnRefused, match="spawn limit reached"):
            p.build(AgentSpec("c", "x", ("web",)))
        assert p.run.count == 2

    def test_the_description_says_which_tools_were_not_granted(self):
        p = policy(capabilities={"web"})
        clipped = p.build(AgentSpec("a", "Find prices.\nmore", ("web", "code")))
        assert clipped.description == "Find prices. (tools: web; not granted: code)"
        clean = p.build(AgentSpec("b", "Find prices.", ("web",)))
        assert clean.description == "Find prices. (tools: web)" and "not granted" not in clean.description

    def test_concurrent_builds_never_exceed_the_spawn_limit(self):
        import threading
        n, cap = 6, 2
        p = policy(max_spawns=cap, **CEILING)
        cond = threading.Condition()
        state = {"approving": 0, "refused": 0}
        real_approve = p._approve

        def approve(spec):
            # Hold every build that got past the limit check until each thread has either
            # reached this point or been refused, so a check-then-increment race shows up.
            with cond:
                state["approving"] += 1
                cond.notify_all()
                cond.wait_for(lambda: state["approving"] + state["refused"] >= n, timeout=5)
            return real_approve(spec)

        p._approve = approve
        start = threading.Barrier(n)
        built, errors = [], []

        def worker(i):
            start.wait(timeout=5)
            try:
                built.append(p.build(AgentSpec(f"a{i}", "x", ("web",))))
            except SpawnRefused as e:
                errors.append(e.reason)
                with cond:
                    state["refused"] += 1
                    cond.notify_all()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(built) == cap and p.run.count == cap
        assert errors == ["spawn limit reached"] * (n - cap)
        assert len(p.run.records) == cap

    def test_a_refused_build_frees_its_slot(self):
        p = policy(max_spawns=1, approver=lambda r: r.name != "no", **CEILING)
        with pytest.raises(SpawnRefused, match="not approved"):
            p.build(AgentSpec("no", "x", ("web",)))
        with pytest.raises(SpawnRefused, match="no usable tools"):
            p.build(AgentSpec("c", "x", ("code",)))
        assert p.build(AgentSpec("yes", "x", ("web",))).spec.name == "yes" and p.run.count == 1

    def test_pool_tool_colliding_with_a_default_tool_is_refused(self):
        clash = StandardTool(func=lambda x: x, name="read_path", description="clashes")
        p = policy(tools=[clash], capabilities={"files_read"})
        with pytest.raises(SpawnRefused, match="could not build"):
            p.build(AgentSpec("a", "x", ("read_path", "files_read")))

    def test_pool_tools_reach_the_runner(self):
        mine = StandardTool(func=lambda x: x, name="lookup", description="look something up")
        runner = policy(tools=[mine]).build(AgentSpec("a", "x", ("lookup",))).runner
        assert "lookup" in tool_names(runner)


class TestApproval:
    def test_ceiling_mode_needs_no_approval(self):
        policy(**CEILING).build(AgentSpec("a", "x", ("web",)))           # no approver, no prompt

    def test_ceiling_mode_still_consults_an_approver_when_one_is_set(self):
        asked = []
        p = policy(approver=lambda req: asked.append(req) or False, **CEILING)
        with pytest.raises(SpawnRefused, match="not approved"):
            p.build(AgentSpec("a", "why", ("web",)))
        assert asked[0].name == "a" and asked[0].capabilities == ["web"] and asked[0].description == "why"

    def test_an_approver_that_raises_counts_as_a_refusal(self):
        def boom(req):
            raise RuntimeError("no")
        with pytest.raises(SpawnRefused, match="not approved"):
            policy(approver=boom, **CEILING).build(AgentSpec("a", "x", ("web",)))

    def test_legacy_auto_spawn_with_an_allowed_caps_gate(self):
        p = policy(auto_spawn=True, auto_spawn_allowed_caps={"web"})
        p.build(AgentSpec("ok", "x", ("web",)))
        with pytest.raises(SpawnRefused, match="not approved"):            # code is outside the gate, no approver
            p.build(AgentSpec("bad", "x", ("code",)))

    def test_legacy_gate_falls_through_to_the_approver(self):
        p = policy(auto_spawn=True, auto_spawn_allowed_caps={"web"}, approver=lambda r: True)
        assert p.build(AgentSpec("code_agent", "x", ("code",))).granted == ["code"]

    def test_legacy_without_auto_spawn_uses_the_approver(self):
        assert policy(approver=lambda r: True).build(AgentSpec("a", "x", ("web",))).granted == ["web"]
        with pytest.raises(SpawnRefused):
            policy(approver=lambda r: False).build(AgentSpec("a", "x", ("web",)))


class TestObtain:
    def test_identical_redefinition_reuses_without_spending_a_spawn(self):
        p = policy(max_spawns=1, **CEILING)
        first = p.obtain(AgentSpec("a", "same", ("web",)))
        again = p.obtain(AgentSpec("a", "same", ("web",)))
        assert again.reused == "spawned" and again.runner is first.runner and p.run.count == 1

    def test_a_differing_redefinition_gets_a_suffix(self):
        p = policy(**CEILING)
        p.obtain(AgentSpec("a", "one", ("web",)))
        other = p.obtain(AgentSpec("a", "two", ("web",)))
        assert other.spec.name == "a_2" and other.reused is None
        assert "a" in p.run.built and "a_2" in p.run.built

    def test_a_name_registered_by_the_developer_is_not_rebuilt(self):
        p = policy(**CEILING)
        got = p.obtain(AgentSpec("researcher", "ignored", ("web",)), registered={"researcher"})
        assert got.reused == "registered" and got.runner is None and p.run.count == 0


class TestEventsAndRecords:
    def test_spawn_event_shape_and_record(self):
        p = policy(**CEILING)
        p.build(AgentSpec("a", "x", ("web", "code")))
        [ev] = p.run.drain()
        assert ev == {"type": "spawn", "name": "a", "origin": "plan", "tools": ["web"],
                      "dropped": ["code"], "reused": None, "refused": None,
                      "capabilities": ["web"], "rerouted_from": None}
        assert p.run.records == [{"name": "a", "origin": "plan", "tools": ["web"],
                                  "dropped": ["code"], "outcome": None, "chars": None}]
        assert p.run.drain() == []
        p.run.finish("a", "done", 12)
        assert p.run.records[0]["outcome"] == "done" and p.run.records[0]["chars"] == 12

    def test_a_refusal_emits_an_event_with_the_reason(self):
        p = policy(**CEILING)
        with pytest.raises(SpawnRefused):
            p.build(AgentSpec("a", "x", ("code",)))
        [ev] = p.run.drain()
        assert ev["refused"] == "no usable tools" and ev["name"] == "a" and ev["reused"] is None

    def test_new_run_resets_counters_and_registry(self):
        p = policy(**CEILING)
        p.build(AgentSpec("a", "x", ("web",)))
        p.new_run()
        assert p.run.count == 0 and p.run.built == {} and p.run.records == []

    def test_the_run_budget_is_held_for_one_run_only(self):
        from agentx_dev.Runner.Persistence import RunBudget
        b = RunBudget.start(5)
        p = SpawnPolicy(SpawnConfig(enabled=True, **CEILING), MockModel(), budget=b)
        assert p.budget is b
        p.new_run()
        assert p.budget is None
        assert policy(**CEILING).budget is None

    def test_delegate_names_are_unique_and_valid(self):
        p = policy(**CEILING)
        assert [p.run.next_delegate_name() for _ in range(3)] == ["delegate_1", "delegate_2", "delegate_3"]


class TestPromptBlock:
    def test_lists_only_what_the_ceiling_allows_and_the_spawn_budget(self):
        mine = StandardTool(func=lambda x: x, name="lookup", description="look something up\nmore")
        text = spawn_instruction(policy(tools=[mine], capabilities={"web"}, max_spawns=4))
        assert "web:" in text and "files:" not in text and "code:" not in text
        assert "lookup: look something up" in text and "more" not in text
        assert "at most 4" in text and "new_agent" in text

    def test_legacy_mode_lists_every_preset(self):
        text = spawn_instruction(policy())
        for word in ("web:", "files:", "code:", "delete:"):
            assert word in text
