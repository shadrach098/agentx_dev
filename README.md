# Bruce_framework

## Documentation for Agent Framework

### Overview

The agent framework is a lightweight and modular tool for building LLM-powered agents that can use tools. It provides a structured way to define tools, format them for an agent prompt, and execute them within a loop controlled by an `AgentRunner`.

### Key Components

1.  **`AgentRunner`**: The main engine that orchestrates the agent's execution loop. It connects the LLM, tools, and prompt, managing the agent's state and running the primary "reason-act" cycle.
2.  **`GPT`**: A wrapper class for interacting with OpenAI's Chat Completions API. It provides a convenient interface for creating chat completions and streamed chat completions.
3.  **`StandardTool` and `StructuredTool`**: Represent simple and structured tools, respectively, that can be used by the agent.
4.  **`AgentFormattor` and `AgentPrompt`**: Define the agent's prompt template and provide a way to format it with tools and user input.

### Using the Framework

1.  **Define Tools**: Create instances of `StandardTool` or `StructuredTool` to define the tools available to the agent.
2.  **Create an `AgentRunner`**: Initialize an `AgentRunner` with a `GPT` instance, an `Agent` (either an `AgentFormattor` or `AgentPrompt` instance), a list of tools, and an optional `max_iterations` parameter.
3.  **Initialize the Agent**: Call the `Initialize` method on the `AgentRunner` instance, passing in user input.

### API Documentation

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
# Create a GPT instance
gpt = GPT(api_key="your_api_key")

# Define tools
tools = [StandardTool(name="example_tool", func=example_func, description="An example tool")]

# Create an AgentRunner instance
runner = AgentRunner(model=gpt, Agent=my_agent, tools=tools)

# Initialize the agent with user input
result = runner.Initialize(user_input="Hello, how are you?")

# Print the result
print(result)
```

This documentation provides a comprehensive overview of the agent framework, its key components, and how to use it. It also includes API documentation for the main classes and methods.

