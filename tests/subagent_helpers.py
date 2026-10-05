"""Test doubles for the sub-agent tests: one scripted model that plays planner, synthesizer,
specialist and sub-agent, told apart by what is in the first message."""

import json
from typing import Callable, List, Optional

from tests.conftest import MockModel, make_final

PLANNER_MARK = "You are a Supervisor"
SYNTH_MARK = "answering the user's question directly"
SPAWNED_MARK = "You were spawned by a Supervisor"


def plan_json(*steps) -> str:
    return json.dumps({"plan": list(steps)})


def step(id_: str, agent: str, query: str = "q", deps: Optional[List[str]] = None, **extra) -> dict:
    d = {"id": id_, "agent": agent, "query": query}
    if deps:
        d["depends_on"] = deps
    d.update(extra)
    return d


def new_agent_step(id_: str, name: str, instructions: str, tools=(), query: str = "q",
                   deps: Optional[List[str]] = None, **extra) -> dict:
    d = {"id": id_, "query": query,
         "new_agent": {"name": name, "instructions": instructions, "tools": list(tools)}}
    if deps:
        d["depends_on"] = deps
    d.update(extra)
    return d


def router(plans=(), synth: str = "Final.", agent: Optional[Callable] = None, sub: Optional[Callable] = None):
    """A MockModel whose script routes by role.

    - planner prompt  -> the next item of ``plans`` (a JSON string)
    - synthesis       -> ``synth``
    - an agent whose system prompt carries the spawned-specialist addendum -> ``sub(messages)``
    - any other agent -> ``agent(messages)``; the default answers ``make_final("agent done")``
    """
    queue = list(plans)

    def script(messages):
        first = str(messages[0]["content"])
        if PLANNER_MARK in first:
            return queue.pop(0)
        if SYNTH_MARK in first:
            return synth
        if SPAWNED_MARK in first and sub is not None:
            return sub(messages)
        if agent is not None:
            return agent(messages)
        return make_final("agent done")

    model = MockModel(script=script)
    model.planner_prompts = lambda: [str(c[0]["content"]) for c in model.calls if PLANNER_MARK in str(c[0]["content"])]
    return model
