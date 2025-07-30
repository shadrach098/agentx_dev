# 🤖 Bruce_framework Agent Framework

A lightweight, modular framework for building LLM-powered agents that can interact with tools, reason through multi-step problems, and return structured outputs — all without relying on heavyweight libraries like LangChain.

---

## ✨ Features

- 🧱 **Modular Architecture** — Separated into tool definition, LLM abstraction, and reasoning loop.
- 🔍 **Function Calling Support** — Works with OpenAI (Chat Completions) and Gemini (via structured prompting).
- 📦 **StructuredTool and StandardTool** — Support both unstructured (simple) and structured (Pydantic-validated) tools.
- ♻️ **AgentRunner** — Manages the loop: planning, executing tools, handling retries, and assembling final output.
- 🧠 **LLM-Agnostic Design** — Easily swap in OpenAI, Gemini, or your own model wrappers.
- ✅ **Built-in Logging and Debugging** — Tracks reasoning steps, tool results, and final answers.

---

## 🗂️ Project Structure
```
   agent_framework/
   ├── tools.py # StructuredTool, StandardTool, and Pydantic support
   ├── agent_run.py # AgentRunner class to manage reasoning and tool use
   ├── chat_models.py # OpenAI and Gemini abstraction layers
   ├── examples/
   │ ├── weather_tool.py # Example tool using structured Pydantic input
   │ └── run_agent.py # AgentRunner in action with reasoning chain
   ├── README.md
```

### Using the Framework

1.  **Define Tools**: Create instances of `StandardTool` or `StructuredTool` to define the tools available to the agent.
2.  **Create an `AgentRunner`**: Initialize an `AgentRunner` with a `GPT` instance, an `Agent` (either an `AgentFormattor` or `AgentPrompt` instance), a list of tools, and an optional `max_iterations` parameter.
3.  **Initialize the Agent**: Call the `Initialize` method on the `AgentRunner` instance, passing in user input.

### API Documentation


---

## 🛠️ Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/shadrach098/agent-framework.git
cd agent-framework
pip install -r requirements.txt
```

#### Classes

*   `AgentRunner`: The main engine that orchestrates the agent's execution loop.
    *   Methods: `__init__`, `Tool_Runner`, `Initialize`
    *   Attributes: `Query`, `Agent`, `tools`, `max_iterations`, `model`, `history`, `agent_scratchpad`
*   `GPT`: A wrapper class for interacting with OpenAI's Chat Completions API.
    *   Methods: `__init__`, `Initialize`, `create_stream`, `update_defaults`, `extract_content`
    *   Attributes: `api_key`, `client`, `defaults`, `timeout`
*   `StandardTool` and `StructuredTool`: Represent simple and structured tools, respectively.
    *   Methods: `__init__`, `__repr__`
    *   Attributes: `name`, `description`, `func`, `args_schema` (for `StructuredTool`)

#### Methods

*   `AgentRunner.Initialize`: The main agent execution loop.
*   `GPT.Initialize`: Creates a chat completion using the OpenAI API.
*   `GPT.create_stream`: Creates a streamed chat completion.

### Example Usage

```python
`import modules`
from Bruce_Framework.ChatModel import GPT
from Bruce_Framework.Agents.Agent import AgentType,convert_to_json,AgentPrompt
from Bruce_Framework.Tools import StandardTool,StructuredTool
from Bruce_Framework.Runner.AgentRun import AgentRunner
# Create a GPT instance
gpt = GPT(api_key="your_api_key")


class WeatherArgs(BaseModel):
    location: str
    unit: str = "Celsius"

def get_weather(location: str, unit: str):
    return f"The weather in {location} is 22° {unit}."

tools=[weather_tool = StructuredTool(
    name="get_weather",
    description="Get the current weather for a city",
    func=get_weather,
    args_schema=WeatherArgs
)]

# Define tools
tools.append(StandardTool(name="example_tool", func=example_func, description="An example tool"))


# Define the Type of Agent
my_agent=AgentType.ReAct

# Create an AgentRunner instance
runner = AgentRunner(model=gpt, Agent=my_agent, tools=tools)

# Initialize the agent with user input
result = runner.Initialize(user_input="Hello, how are you?")

# Print the result
print(result)
```


### 🔮 Tech Stack
- Python 3.10+
- OpenAI GPT-4 / GPT-3.5
- Google Gemini Pro
- Pydantic
- Requests, Logging

### 📘 Docs
Full API docs coming soon. For now, browse through the structured codebase:

- tools.py – Tool schema and definitions
- agent_run.py – Reasoning engine
- chat_models.py – Model abstraction for OpenAI/Gemini

### 🚀 Author
Bruce-Arhin Shadrach
GitHub • LinkedIn


