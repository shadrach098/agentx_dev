"""YAML / dict-based agent configuration loader.

Lets users define an agent declaratively and instantiate it without
hand-wiring constructors. Useful for:

  - Swapping models / prompts / tool sets across environments without
    touching code.
  - Hot-reloading an agent definition during development.
  - Storing canonical agent configurations in version control alongside
    prompts, separate from the application code that consumes them.

Example::

    # agent.yaml
    model:
      provider: gpt
      name: gpt-4o-mini
      temperature: 0
    agent_type: ReAct
    tools:
      - module: my_app.tools
        attr: search_tool
      - module: my_app.tools
        attr: calculator_tool
    use_function_calling: true
    verbose: false
    max_iterations: 6

    # code
    from agentx_dev import load_agent_from_yaml
    runner = load_agent_from_yaml("agent.yaml")
    result = runner.invoke("What is 2 + 2?")

Scope:
  - Builds a sync AgentRunner (use ``load_async_agent_from_yaml`` for
    AsyncAgentRunner).
  - Model providers: ``gpt`` / ``claude`` / ``custom`` (the latter takes
    ``module``+``attr`` of a BaseChatModel subclass + factory kwargs).
  - Tools loaded by ``module``+``attr`` reference — the module is
    imported and the attribute looked up at config-load time. The
    attribute must already be a StandardTool / StructuredTool /
    AsyncStandardTool / AsyncStructuredTool instance.
  - AgentType referenced by name (``ReAct`` / ``Chain_of_Thought`` /
    ``Zero_Shot`` / ``Few_Shot`` / ``Instruction_Tuned``) or by a
    free-form prompt string passed in ``agent.prompt``.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentx_dev.Agents.Agent import AgentType, AgentFormattor
from agentx_dev.ChatModel import BaseChatModel
from agentx_dev.Runner.AgentRun import AgentRunner
from agentx_dev.Runner.AsyncAgentRun import AsyncAgentRunner


class AgentConfigError(ValueError):
    """Raised when an agent config file is malformed or references a
    module/attr that can't be resolved. Wraps the underlying cause so
    callers can surface a useful error to the user."""


def _resolve_dotted(module_name: str, attr_name: str) -> Any:
    """Import ``module_name`` and pull ``attr_name`` off it.

    Raises ``AgentConfigError`` with a helpful message on ImportError
    / AttributeError so the user knows which line of the config failed.
    """
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        raise AgentConfigError(
            f"Could not import module {module_name!r}: {e}"
        ) from e
    if not hasattr(mod, attr_name):
        raise AgentConfigError(
            f"Module {module_name!r} has no attribute {attr_name!r}"
        )
    return getattr(mod, attr_name)


def _build_model(model_cfg: Dict[str, Any]) -> BaseChatModel:
    """Instantiate a BaseChatModel from the ``model:`` block of the config."""
    if not isinstance(model_cfg, dict):
        raise AgentConfigError("'model' must be a mapping")

    provider = model_cfg.get("provider")
    if not provider:
        raise AgentConfigError("'model.provider' is required (gpt / claude / custom)")

    provider = provider.lower()
    # Strip the structural keys; everything else becomes kwargs.
    kwargs = {k: v for k, v in model_cfg.items() if k not in ("provider", "name", "module", "attr")}
    name = model_cfg.get("name")

    if provider == "gpt":
        from agentx_dev.ChatModel import GPT
        if name:
            kwargs["model"] = name
        return GPT(**kwargs)

    if provider == "claude":
        from agentx_dev.ChatModel import Claude
        if name:
            kwargs["model"] = name
        return Claude(**kwargs)

    if provider == "custom":
        module = model_cfg.get("module")
        attr = model_cfg.get("attr")
        if not (module and attr):
            raise AgentConfigError(
                "'model.provider: custom' requires 'module' and 'attr' fields"
            )
        cls = _resolve_dotted(module, attr)
        try:
            instance = cls(**kwargs)
        except Exception as e:
            raise AgentConfigError(
                f"Failed to instantiate custom model {module}.{attr}: {e}"
            ) from e
        if not isinstance(instance, BaseChatModel):
            raise AgentConfigError(
                f"{module}.{attr} did not produce a BaseChatModel instance "
                f"(got {type(instance).__name__})"
            )
        return instance

    raise AgentConfigError(
        f"Unknown model.provider {provider!r}. Use 'gpt', 'claude', or 'custom'."
    )


def _build_tools(tools_cfg: Optional[List[Dict[str, Any]]]) -> List[Any]:
    """Resolve each ``- module: …, attr: …`` entry to the actual tool object."""
    if tools_cfg is None:
        return []
    if not isinstance(tools_cfg, list):
        raise AgentConfigError("'tools' must be a list")

    tools: List[Any] = []
    for i, entry in enumerate(tools_cfg):
        if not isinstance(entry, dict):
            raise AgentConfigError(f"tools[{i}] must be a mapping with 'module' + 'attr'")
        module = entry.get("module")
        attr = entry.get("attr")
        if not (module and attr):
            raise AgentConfigError(
                f"tools[{i}] requires both 'module' and 'attr' fields; got {entry!r}"
            )
        tool = _resolve_dotted(module, attr)
        tools.append(tool)
    return tools


def _resolve_agent_type(agent_cfg: Any):
    """Map the ``agent_type:`` config string to an AgentFormattor instance.

    Accepts:
      - A bare string naming one of AgentType's attributes
        ("ReAct", "Chain_of_Thought", "Zero_Shot", "Few_Shot",
        "Instruction_Tuned").
      - A mapping with ``prompt: <template string>`` for a custom prompt.
    """
    if isinstance(agent_cfg, str):
        if not hasattr(AgentType, agent_cfg):
            valid = [n for n in dir(AgentType) if not n.startswith("_")]
            raise AgentConfigError(
                f"Unknown agent_type {agent_cfg!r}. Valid: {valid}"
            )
        return getattr(AgentType, agent_cfg)
    if isinstance(agent_cfg, dict):
        prompt = agent_cfg.get("prompt")
        if not prompt:
            raise AgentConfigError(
                "When 'agent_type' is a mapping it must contain a 'prompt' string"
            )
        # Free-form prompt — caller takes responsibility for {tools} /
        # {tool_names} / {user_input} placeholders.
        from agentx_dev.Agents.Agent import StandardParser
        return AgentFormattor(prompt=prompt, Agent=StandardParser)
    raise AgentConfigError(
        f"'agent_type' must be a string or a mapping, got {type(agent_cfg).__name__}"
    )


def build_runner_from_config(
    config: Dict[str, Any],
    *,
    async_runner: bool = False,
):
    """Instantiate an AgentRunner (or AsyncAgentRunner) from a parsed config dict.

    Lower-level than ``load_agent_from_yaml`` — useful when you have the
    config in memory already (e.g. fetched from a database) and don't
    want to round-trip it through a YAML file.
    """
    if not isinstance(config, dict):
        raise AgentConfigError("Top-level config must be a mapping")

    if "model" not in config:
        raise AgentConfigError("'model' block is required")
    if "agent_type" not in config:
        raise AgentConfigError("'agent_type' is required")

    model = _build_model(config["model"])
    agent = _resolve_agent_type(config["agent_type"])
    tools = _build_tools(config.get("tools"))

    runner_kwargs: Dict[str, Any] = {
        "model": model,
        "agent": agent,
        "tools": tools,
    }
    for key in ("max_iterations", "auto_cache", "auto_memory",
                "use_function_calling", "verbose", "include_denied_tools"):
        if key in config:
            runner_kwargs[key] = config[key]

    # YAML-declared permissions get materialized into the runner's
    # built-in tools alongside any tools= block — same merge semantics
    # the runner itself uses.
    if "permissions" in config:
        from agentx_dev.DefaultTools import Permissions
        perm_cfg = config["permissions"]
        if not isinstance(perm_cfg, dict):
            raise AgentConfigError("'permissions' must be a mapping of capability flags")
        # Translate plain YAML dict into Permissions kwargs. Unknown keys
        # raise so a typo (read_filez=True) doesn't silently grant nothing.
        valid_fields = {f for f in Permissions.__dataclass_fields__}
        unknown = set(perm_cfg) - valid_fields
        if unknown:
            raise AgentConfigError(
                f"Unknown permissions field(s): {sorted(unknown)}. "
                f"Valid: {sorted(valid_fields)}"
            )
        runner_kwargs["permissions"] = Permissions(**perm_cfg)

    if async_runner:
        # AsyncAgentRunner accepts everything sync does plus bind_tools_natively.
        if "bind_tools_natively" in config:
            runner_kwargs["bind_tools_natively"] = config["bind_tools_natively"]
        return AsyncAgentRunner(**runner_kwargs)

    if "bind_tools_natively" in config:
        raise AgentConfigError(
            "'bind_tools_natively' is only available on AsyncAgentRunner. "
            "Pass async_runner=True (or use load_async_agent_from_yaml)."
        )
    return AgentRunner(**runner_kwargs)


def _load_yaml(path) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as e:
        raise AgentConfigError(
            "Loading an agent from YAML requires PyYAML. "
            "Install with: pip install PyYAML"
        ) from e
    with open(path, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    if loaded is None:
        raise AgentConfigError(f"YAML file {path} is empty")
    return loaded


def load_agent_from_yaml(path) -> AgentRunner:
    """Read ``path``, parse as YAML, build a sync AgentRunner.

    See module docstring for the expected config shape.
    """
    config = _load_yaml(Path(path))
    return build_runner_from_config(config, async_runner=False)


def load_async_agent_from_yaml(path) -> AsyncAgentRunner:
    """Read ``path``, parse as YAML, build an AsyncAgentRunner.

    Use this when your tools include AsyncStandardTool / AsyncStructuredTool
    or when you want bind_tools_natively=True for parallel dispatch.
    """
    config = _load_yaml(Path(path))
    return build_runner_from_config(config, async_runner=True)
