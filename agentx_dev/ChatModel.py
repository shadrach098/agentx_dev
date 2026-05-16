from typing import Optional, Union, Dict, List, Iterable, Literal, Any
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.chat.completion_create_params import (
    FunctionCall, Function, ResponseFormat
)
from openai._types import NOT_GIVEN, NotGiven
import asyncio
import logging, os

logger = logging.getLogger(__name__)
from abc import ABC, abstractmethod
class BaseChatModel(ABC):
    """Abstract base class. All chat model integrations must implement Initialize()."""

    @abstractmethod
    def Initialize(self, messages) -> str:
        """Send messages to the LLM and return the response string."""
        ...

    async def async_initialize(self, messages) -> str:
        """
        Async wrapper around Initialize(). Runs in a thread pool so it
        does not block the event loop. Override for a native async implementation.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.Initialize, messages)

    def _with_retry(self, fn, max_retries: int = 3, base_delay: float = 0.1):
        """
        Call fn() with exponential backoff on failure.
        base_delay is short by default for test speed; production code passes a larger value.
        """
        import time
        last_exc = None
        for attempt in range(max_retries):
            try:
                return fn()
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"LLM call attempt {attempt + 1} failed ({e}), retrying in {delay:.2f}s")
                    time.sleep(delay)
        raise last_exc

class GPT(BaseChatModel):
    """
    Wrapper class for interacting with OpenAI's Chat Completions API using configurable defaults.

    Attributes:
        api_key (Optional[str]): API key for OpenAI. Defaults to the 'OPENAI_API_KEY' environment variable.
        client (OpenAI): An instance of the OpenAI client.
        defaults (dict): Default parameters for chat completion requests.
        timeout (float): Timeout in seconds for requests.
    """
    def __init__(
        
        self,
        # API config
        api_key: Optional[str] = None,
        organization: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        max_retries: int = 3,

        # Default chat params
        model: str = "gpt-4o",
        temperature: float | NotGiven = NOT_GIVEN,
        max_tokens: int | NotGiven = NOT_GIVEN,
        top_p: float | NotGiven = NOT_GIVEN,
        n: int | NotGiven = NOT_GIVEN,
        stream_options: Any | NotGiven = NOT_GIVEN,
        stop: Union[str, List[str]] | NotGiven = NOT_GIVEN,
        presence_penalty: float | NotGiven = NOT_GIVEN,
        frequency_penalty: float | NotGiven = NOT_GIVEN,
        logit_bias: Dict[str, int] | NotGiven = NOT_GIVEN,
        user: str | NotGiven = NOT_GIVEN,
        functions: Iterable[Function] | NotGiven = NOT_GIVEN,
        function_call: FunctionCall | NotGiven = NOT_GIVEN,
        tool_choice: Any | NotGiven = NOT_GIVEN,
        tools: Iterable[Any] | NotGiven = NOT_GIVEN,
        logprobs: bool | NotGiven = NOT_GIVEN,
        top_logprobs: int | NotGiven = NOT_GIVEN,
        response_format: ResponseFormat | NotGiven = NOT_GIVEN,
        seed: int | NotGiven = NOT_GIVEN,
        service_tier: Literal["auto", "default"] | NotGiven = NOT_GIVEN,
        metadata: Dict[str, str] | NotGiven = NOT_GIVEN,
        store: bool | NotGiven = NOT_GIVEN,
        parallel_tool_calls: bool | NotGiven = NOT_GIVEN,
    ):
        """
        Initialize the GPT client with configurable API and model parameters.

        Args:
            api_key (Optional[str]): Your OpenAI API key.
            organization (Optional[str]): OpenAI organization ID (if applicable).
            base_url (Optional[str]): Base URL for the OpenAI API.
            timeout (float): Request timeout in seconds. Default is 60.0.
            max_retries (int): Maximum number of retries on failed requests.

            model (str): Default model to use (e.g., "gpt-4o").
            temperature (float | NotGiven): Sampling temperature.
            max_tokens (int | NotGiven): Maximum number of tokens in the response.
            top_p (float | NotGiven): Nucleus sampling parameter.
            n (int | NotGiven): Number of completions to generate.
            stream_options (Any | NotGiven): Streaming config.
            stop (Union[str, List[str]] | NotGiven): Stop sequences.
            presence_penalty (float | NotGiven): Penalty for introducing new topics.
            frequency_penalty (float | NotGiven): Penalty for repeating tokens.
            logit_bias (Dict[str, int] | NotGiven): Biasing the logits for certain tokens.
            user (str | NotGiven): Unique identifier for the end user.
            functions (Iterable[Function] | NotGiven): Callable functions for the model.
            function_call (FunctionCall | NotGiven): Control how functions are called.
            tool_choice (Any | NotGiven): Tool selection strategy.
            tools (Iterable[Any] | NotGiven): List of available tools.
            logprobs (bool | NotGiven): Whether to return log probabilities.
            top_logprobs (int | NotGiven): Number of most likely tokens to return.
            response_format (ResponseFormat | NotGiven): Format of the response.
            seed (int | NotGiven): Seed for reproducibility.
            service_tier (Literal["auto", "default"] | NotGiven): API service tier.
            metadata (Dict[str, str] | NotGiven): Custom metadata.
            store (bool | NotGiven): Whether to store the request.
            parallel_tool_calls (bool | NotGiven): Whether to allow parallel tool calls.
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(
            api_key=self.api_key,
            organization=organization,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries
        )
        

        self.defaults = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "n": n,
            "stream_options": stream_options,
            "stop": stop,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "logit_bias": logit_bias,
            "user": user,
            "functions": functions,
            "function_call": function_call,
            "tool_choice": tool_choice,
            "tools": tools,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
            "response_format": response_format,
            "seed": seed,
            "service_tier": service_tier,
            "metadata": metadata,
            "store": store,
            "parallel_tool_calls": parallel_tool_calls,
        }

        self.timeout = timeout  # Keep around for later usage

    def Initialize(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        stream: Optional[bool] | NotGiven = NOT_GIVEN,
        extra_headers: Optional[Dict[str, str]] = None,
        extra_query: Optional[Dict[str, Any]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        
    ) :
        """
        Create a chat completion using OpenAI API.

        Args:
            messages (Iterable[ChatCompletionMessageParam]): The list of messages for the conversation.
            stream (Optional[bool] | NotGiven): Whether to stream the response.
            extra_headers (Optional[Dict[str, str]]): Additional request headers.
            extra_query (Optional[Dict[str, Any]]): Additional query parameters.
            extra_body (Optional[Dict[str, Any]]): Additional body parameters.
            timeout (Optional[float]): Override the default timeout.

        Returns:
            str: The content string from the first choice in the response.
        """
        logger.debug(f"Calling OpenAI chat.completions.create ")
        try:
            return self._with_retry(
                lambda: self.extract_content(
                    self.client.chat.completions.create(
                        messages=messages,
                        **self.defaults,
                        extra_headers=extra_headers,
                        extra_query=extra_query,
                        extra_body=extra_body,
                        timeout=timeout or self.timeout,
                    )
                ),
                max_retries=3,
                base_delay=0.1,
            )
        except Exception as e:
            logger.error(f"Error during chat completion: {str(e)}")
            raise
    
    def update_defaults(self, **kwargs):
        for key, value in kwargs.items():
            if key in self.defaults:
                self.defaults[key] = value
                logger.info(f"Updated default parameter {key} = {value}")
    
    def extract_content(self, completion: ChatCompletion) -> str:
        """
        Extract content from a chat completion response

        Args:
            completion: ChatCompletion object

        Returns:
            The content string from the first choice
        """

        if completion.choices and len(completion.choices) > 0:
            return completion.choices[0].message.content or ""
        return ""




class Claude(BaseChatModel):
    """
    Chat model wrapper for Anthropic's Claude API.

    Usage:
        model = Claude(model="claude-sonnet-4-6")
        response = model.Initialize([{"role": "user", "content": "Hello"}])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        temperature: float = 1.0,
        timeout: float = 60.0,
        max_retries: int = 3,
    ):
        import anthropic as _anthropic
        self._anthropic = _anthropic
        self.model_name = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._timeout = timeout
        self._max_retries = max_retries
        self.client = _anthropic.Anthropic(
            api_key=self._api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    def Initialize(self, messages) -> str:
        """
        Send messages to Claude and return the text response.

        Converts OpenAI-style message dicts to Anthropic format:
        - System messages are extracted and passed as the `system` parameter
        - User and assistant messages form the conversation
        """
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        conversation = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        system_prompt = "\n\n".join(system_parts) if system_parts else self._anthropic.NOT_GIVEN

        response = self.client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=conversation,
        )
        return response.content[0].text if response.content else ""

    async def async_initialize(self, messages) -> str:
        """Native async Claude call using the AsyncAnthropic client."""
        import anthropic as _anthropic
        async_client = _anthropic.AsyncAnthropic(
            api_key=self._api_key,
            timeout=self._timeout,
        )
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        conversation = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        system_prompt = "\n\n".join(system_parts) if system_parts else _anthropic.NOT_GIVEN

        response = await async_client.messages.create(
            model=self.model_name,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=system_prompt,
            messages=conversation,
        )
        return response.content[0].text if response.content else ""

