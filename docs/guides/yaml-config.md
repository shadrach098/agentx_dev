# Guide: configure an agent from YAML

When you want to swap models / tools / permissions without changing
code (dev vs. staging vs. prod), define the agent in YAML.

## Minimal config

```yaml
# agent.yaml
model:
  provider: claude          # gpt | claude | custom
  name: claude-sonnet-4-6
  temperature: 0.7

agent_type: ReAct           # ReAct | Chain_of_Thought | Zero_Shot | Few_Shot | Instruction_Tuned

permissions:
  read_files: true
  list_directories: true
  write_files: true
  allowed_paths:
    - ./workspace

tools:
  - module: my_app.tools
    attr: weather_tool

max_iterations: 6
use_function_calling: true
verbose: false
```

Load:

```python
from agentx_dev import load_agent_from_yaml

runner = load_agent_from_yaml("agent.yaml")
result = runner.invoke("What's in the workspace?")
```

Async sibling:

```python
from agentx_dev import load_async_agent_from_yaml
runner = load_async_agent_from_yaml("agent.yaml")
```

## Recognized fields

### `model`

```yaml
model:
  provider: claude              # required: gpt | claude | custom
  name: claude-sonnet-4-6       # required
  temperature: 0.7              # optional
  max_tokens: 4096              # optional
  api_key: ${ANTHROPIC_API_KEY} # optional; env var interpolation supported
  timeout: 60.0                 # optional
  # 3.1 Claude only:
  enable_prompt_cache: true
  cache_history_after: 4
```

For a custom provider:

```yaml
model:
  provider: custom
  module: my_app.models
  attr: OllamaChat
  kwargs:
    model: llama3.1
    host: http://localhost:11434
```

### `agent_type`

One of `ReAct` / `Chain_of_Thought` / `Zero_Shot` / `Few_Shot` /
`Instruction_Tuned`, or a custom formatter:

```yaml
agent_type: custom
custom_agent:
  module: my_app.agents
  attr: my_formatter
```

### `permissions`

Every field on `Permissions` is accepted. `allowed_paths` accepts a
list of strings.

```yaml
permissions:
  read_files: true
  write_files: true
  edit_files: true
  execute_python: true
  execute_shell: false
  allowed_paths: [./workspace, ./data]
  python_timeout_sec: 10.0
  python_max_output_bytes: 100000
  workspace: ./workspace
  python_persistent_state: true
```

Or use a preset:

```yaml
permissions:
  preset: full_access
  allowed_paths: [./workspace]
```

### `tools`

A list of imports. Each entry gives a Python module + attribute name:

```yaml
tools:
  - module: my_app.tools
    attr: weather_tool
  - module: my_app.tools
    attr: calc_tool
  - module: agentx_dev.WebTools
    attr: web_search_tool
    call: true       # if attr is a factory, call it to get the tool
```

`call: true` on the last item invokes `web_search_tool()` to produce the
StructuredTool. Without `call:`, the attribute is expected to be a tool
instance directly.

### `chat_model.configure_limits`

To wire rate limits, retry budget, cost budget:

```yaml
model:
  provider: claude
  name: claude-sonnet-4-6
  configure_limits:
    budget_usd: 5.0
    input_price_per_1k: 0.003
    output_price_per_1k: 0.015
    rate_limit_per_sec: 5
    retry_budget: 10
```

## Environment interpolation

Any string value can reference an env var via `${VAR}`:

```yaml
model:
  api_key: ${ANTHROPIC_API_KEY}
  base_url: ${LITELLM_HOST}
```

## Loud errors

A typo in any field (`read_filez: true`) fails at load time with a
clear message telling you the offending key. No silent defaults.

## Reference from dict directly

Skip the file when you want to build from a dict (tests, dynamic
config):

```python
from agentx_dev import build_runner_from_config

runner = build_runner_from_config({
    "model": {"provider": "claude", "name": "claude-sonnet-4-6"},
    "agent_type": "ReAct",
    "permissions": {"preset": "read_only", "allowed_paths": ["./docs"]},
    "tools": [],
})
```

## Multi-environment pattern

Keep a `base.yaml` and per-env overrides:

```yaml
# base.yaml
model:
  provider: claude
  name: claude-sonnet-4-6

# prod.yaml — merged on top of base
model:
  configure_limits:
    budget_usd: 100.0
    rate_limit_per_sec: 20
permissions:
  preset: read_only
  allowed_paths: [/srv/prod/data]
```

Merge them yourself before calling the loader:

```python
import yaml

def load_env(env: str):
    with open("base.yaml") as f:
        base = yaml.safe_load(f)
    with open(f"{env}.yaml") as f:
        overrides = yaml.safe_load(f)
    merged = {**base, **overrides}
    return build_runner_from_config(merged)
```
