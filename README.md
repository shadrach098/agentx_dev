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
```plaintext
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

Use pip to install the framework:
```bash
pip install AgentX-Dev
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
from AgentXL import AgentRunner, AgentType,ChatModel
from pydantic import BaseModel
from AgentXL.Tools import StructuredTool,StandardTool
# Define a sample Stuctured tool
class MultiplyTool(BaseModel):
    a: int
    b: int

def multiply(a: int, b: int) -> int:
    return a * b

# Define a sample Standard tool
def Weather(weather:str):
    return f"{weather} is currently at 28 degree with a high of 32 and a low of 18 "

# Create chat model and agent
ReAct=AgentType.ReAct
chat_model = ChatModel.GPT(model="gpt-4", temperature=0.7)
tools = [StructuredTool(name="MultiplyTool",
                        description="useful when you need to add two numbers",
                        func=multiply,
                        args_schema=MultiplyTool
                        ),
         StandardTool(name="Weather",
                      description="when you need to check the weather of a location, input should be the str of the location",
                      func=Weather)]
# Create an AgentRunner instance
agent = AgentRunner(model=chat_model,Agent=ReAct, tools=tools)

# Initialize the agent with user input
response = agent.Initialize("What is 5 times 8?")

# Print the result
print(response.content)


# output the Agent completion
agent.Initialize("i need the weather in Barrie")


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


