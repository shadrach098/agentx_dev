from typing import Optional, Union, Dict, List, Iterable, Literal, Any
from openai import OpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from openai.types.chat.completion_create_params import (
    FunctionCall, Function, ResponseFormat
)
from openai._types import NOT_GIVEN, NotGiven
import logging,os

logger = logging.getLogger(__name__)
from abc import ABC, abstractmethod
class BaseChatModel(ABC):
    """Abstract base class. All chat model integrations must implement Initialize()."""

    @abstractmethod
    def Initialize(self, messages) -> str:
        """Send messages to the LLM and return the response string."""
        ...

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
            ChatCompletion: The OpenAI ChatCompletion response object.
        """
        try:
           

            logger.debug(f"Calling OpenAI chat.completions.create ")

            return self.extract_content(self.client.chat.completions.create(
                messages=messages,
                **self.defaults,
                extra_headers=extra_headers,
                extra_query=extra_query,
                extra_body=extra_body,
                timeout=timeout or self.timeout,
            ))
        except Exception as e:
            logger.error(f"Error during chat completion: {str(e)}")
            raise
    
    def create_stream(self, messages: Iterable[ChatCompletionMessageParam], **kwargs):
        """
        Create a streamed chat completion.

        Args:
            messages (Iterable[ChatCompletionMessageParam]): List of chat messages.
            **kwargs: Additional keyword arguments passed to `create`.

        Returns:
            ChatCompletion: The streamed completion response.
        """
        self.defaults["stream"] = True
        return self.create(messages=messages, **kwargs)

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
    
    
    
