"""A Supervisor's fallback_model reaches the helpers it runs: registered runners that have none, and the
specialists it spawns. A runner with its own fallback keeps it, and an object that is not an AgentRunner is
left alone."""

from agentx_dev import AgentRunner, AgentType, Supervisor
from agentx_dev.SubAgents import AgentSpec, SpawnConfig
from tests.subagent_helpers import ScriptedRunner, new_agent_step, plan_json, router, step
from tests.test_runner_fallback import KeyRejected, Local, Paid, respond

SWITCH = {"from": "paid-model", "to": "local-model", "reason": "key rejected"}


def runner(model, fallback=None):
    return AgentRunner(model=model, agent=AgentType.ReAct, tools=[], fallback_model=fallback,
                       verbose=False, auto_cache=False)


class Recording(AgentRunner):
    """An AgentRunner that keeps the completion of each step it ran."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.completions = []

    def Initialize(self, *a, **kw):
        completion = super().Initialize(*a, **kw)
        self.completions.append(completion)
        return completion


def one_step(agent="worker"):
    return router(plans=[plan_json(step("a", agent, "do it"))])


def test_registered_runner_gets_fallback():
    fallback = Local([])
    worker = runner(Paid([]))
    seen = []

    def spy(*a, **kw):
        seen.append(worker.fallback_model)
        return type("C", (), {"content": "ok", "outcome": "done", "output": None, "progress": None})()

    worker.Initialize = spy
    Supervisor(model=one_step(), agents={"worker": ("Works.", worker)}, verbose=False,
               fallback_model=fallback).run("job")
    assert seen == [fallback]


def test_own_fallback_kept():
    own, shared = Local([]), Local([])
    worker = runner(Paid([]), own)
    seen = []

    def spy(*a, **kw):
        seen.append(worker.fallback_model)
        return type("C", (), {"content": "ok", "outcome": "done", "output": None, "progress": None})()

    worker.Initialize = spy
    Supervisor(model=one_step(), agents={"worker": ("Works.", worker)}, verbose=False,
               fallback_model=shared).run("job")
    assert seen == [own]
    assert worker.fallback_model is own


def test_runner_without_the_attribute_is_left_alone():
    scripted = ScriptedRunner(("ok", "done"))
    Supervisor(model=one_step(), agents={"worker": ("Works.", scripted)}, verbose=False,
               fallback_model=Local([])).run("job")
    assert not hasattr(scripted, "fallback_model")


def test_spawned_helper_gets_fallback():
    fallback = Local([])
    model = router(plans=[plan_json(new_agent_step("a", "helper", "Help."))])
    sup = Supervisor(model=model, agents={}, verbose=False, fallback_model=fallback,
                     spawn_config=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=2))
    policy = sup._new_spawn_policy()
    built = policy.build(AgentSpec(name="helper", instructions="Help.", tools=[]))
    assert built.runner.fallback_model is fallback


def test_no_fallback_by_default():
    sup = Supervisor(model=one_step(), agents={}, verbose=False)
    assert sup.fallback_model is None


def test_step_result_carries_fallback_mark():
    worker = Recording(model=Paid([KeyRejected()]), agent=AgentType.ReAct, tools=[], verbose=False,
                       auto_cache=False)
    result = Supervisor(model=one_step(), agents={"worker": ("Works.", worker)}, verbose=False,
                        fallback_model=Local([respond("finished on the fallback")])).run("job")
    assert result.subtasks[0].content == "finished on the fallback"
    assert worker.completions[0].fallback == SWITCH


def test_spawned_helper_step_finishes_on_the_fallback():
    def helper_fails(messages):
        raise KeyRejected()

    model = router(plans=[plan_json(new_agent_step("a", "helper", "Help.", query="go"))], sub=helper_fails)
    model.model_name = "paid-model"
    result = Supervisor(model=model, agents={}, verbose=False,
                        fallback_model=Local([respond("helped on the fallback")]),
                        spawn_config=SpawnConfig(enabled=True, capabilities={"web"}, max_spawns=2)).run("job")
    assert result.subtasks[0].content == "helped on the fallback"
