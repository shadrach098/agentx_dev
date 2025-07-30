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
class BaseChatModel:
    """
    An abstract base class defining the standard interface for chat models.

    This class establishes a contract for all model integrations, ensuring they
    can be used interchangeably within the framework. It defines both standard
    invocation and streaming, with asynchronous methods as the core implementation
    and synchronous wrappers provided for convenience.
    """

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
    
    
    


import os
import logging
from typing import Optional, Dict, Any, List, Union, Iterable, Generator

# --- Preliminaries (to make the code runnable) ---
# Assuming a similar BaseChatModel and NotGiven structure
# In a real application, these would be properly defined.



class _NotGiven:
    def __repr__(self):
        return "NOT_GIVEN"

NOT_GIVEN = _NotGiven()

# Import the Google Generative AI library
try:
    import google.generativeai as genai
    # CORRECTED IMPORT: GenerateContentResponse is imported from the 'types' module
    from google.generativeai.types import (
        GenerationConfig, 
        SafetySettingDict, 
        ContentDict,
        GenerateContentResponse
    )
except ImportError:
    raise ImportError(
        "The 'google-generativeai' library is required to use the Gemini class. "
        "Please install it with 'pip install google-generativeai'.")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# --- Gemini Wrapper Class ---

class Gemini(BaseChatModel):
    """
    Wrapper class for interacting with Google's Gemini API using configurable defaults.

    Attributes:
        api_key (Optional[str]): API key for Google AI. Defaults to 'GOOGLE_API_KEY' environment variable.
        client (genai.GenerativeModel): An instance of the Google GenerativeModel client.
        defaults (dict): Default parameters for generation requests.
    """
    def __init__(
        self,
        # API config
        api_key: Optional[str] = None,
        
        # Default generation params
        model: str = "gemini-1.5-pro-latest",
        temperature: float | _NotGiven = NOT_GIVEN,
        top_p: float | _NotGiven = NOT_GIVEN,
        top_k: int | _NotGiven = NOT_GIVEN,
        candidate_count: int | _NotGiven = NOT_GIVEN,
        max_output_tokens: int | _NotGiven = NOT_GIVEN,
        stop_sequences: Union[str, List[str]] | _NotGiven = NOT_GIVEN,
        safety_settings: Optional[Iterable[SafetySettingDict]] = None,
        tools: Optional[Iterable[Any]] = None,
        tool_config: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize the Gemini client with configurable API and model parameters.

        Args:
            api_key (Optional[str]): Your Google AI API key.
            
            model (str): Default model to use (e.g., "gemini-1.5-pro-latest").
            temperature (float | NotGiven): Sampling temperature.
            top_p (float | NotGiven): Nucleus sampling parameter.
            top_k (int | NotGiven): Top-k sampling parameter.
            candidate_count (int | NotGiven): Number of generated responses to return.
            max_output_tokens (int | NotGiven): Maximum number of tokens in the response.
            stop_sequences (Union[str, List[str]] | NotGiven): Stop sequences.
            safety_settings (Optional[Iterable[SafetySettingDict]]): Safety settings for the request.
            tools (Optional[Iterable[Any]]): List of available tools for the model.
            tool_config (Optional[Dict[str, Any]]): Configuration for how the model should use tools.
        """
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("Google API key must be provided or set in the GOOGLE_API_KEY environment variable.")
            
        genai.configure(api_key=self.api_key)

        # Store generation config separately as it's a distinct object in the Google SDK
        self.generation_config = {
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "candidate_count": candidate_count,
            "max_output_tokens": max_output_tokens,
            "stop_sequences": stop_sequences,
        }
        
        # Remove NOT_GIVEN values so they don't get passed to the API
        self.generation_config = {k: v for k, v in self.generation_config.items() if v is not NOT_GIVEN}

        self.defaults = {
            "tools": tools,
            "tool_config": tool_config,
        }
        self.defaults = {k: v for k, v in self.defaults.items() if v is not NOT_GIVEN}

        # System instructions are handled at the model initialization level
        self.client = genai.GenerativeModel(
            model_name=model,
            safety_settings=safety_settings,
            # system_instruction can be added here if needed
        )
        
    def _convert_messages(self, messages: Iterable[Dict[str, str]]) -> List[ContentDict]:
        """Converts OpenAI-style messages to Gemini's format."""
        gemini_messages = []
        for msg in messages:
            # Gemini uses 'model' for the assistant's role
            role = "model" if msg["role"] == "assistant" else msg["role"]
            gemini_messages.append({'role': role, 'parts': [msg['content']]})
        return gemini_messages

    def Initialize(
        self,
        messages: Iterable[Dict[str, str]],
        stream: bool = False,
    ) -> Union[str, Generator[str, None, None]]:
        """
        Generate content using the Gemini API.

        Args:
            messages (Iterable[Dict[str, str]]): The list of messages for the conversation.
                Expected format is a list of dicts, e.g., [{"role": "user", "content": "Hello"}].
            stream (bool): Whether to stream the response.

        Returns:
            Union[str, Generator[str, None, None]]: The generated text content as a string,
            or a generator yielding text chunks if streaming.
        """
        try:
            logger.debug("Calling Gemini generate_content")
            
            gemini_messages = self._convert_messages(messages)

            response_generator = self.client.generate_content(
                contents=gemini_messages,
                generation_config=self.generation_config,
                stream=stream,
                **self.defaults
            )

            if stream:
                # Return a generator that yields the text from each chunk
                return (self.extract_content(chunk) for chunk in response_generator)
            else:
                return self.extract_content(response_generator)
                
        except Exception as e:
            logger.error(f"Error during Gemini content generation: {str(e)}")
            raise

    def create_stream(self, messages: Iterable[Dict[str, str]], **kwargs) -> Generator[str, None, None]:
        """
        Create a streamed chat completion.

        Args:
            messages (Iterable[Dict[str, str]]): List of chat messages.
            **kwargs: Additional keyword arguments passed to `Initialize`.

        Returns:
            Generator[str, None, None]: A generator of response text chunks.
        """
        return self.Initialize(messages=messages, stream=True, **kwargs)

    def update_defaults(self, **kwargs):
        """Updates default generation parameters."""
        for key, value in kwargs.items():
            if key in self.generation_config:
                self.generation_config[key] = value
                logger.info(f"Updated generation_config parameter {key} = {value}")
            elif key in self.defaults:
                self.defaults[key] = value
                logger.info(f"Updated default parameter {key} = {value}")
            else:
                logger.warning(f"Parameter '{key}' is not a recognized default for this Gemini wrapper.")
    
    def extract_content(self, response: GenerateContentResponse) -> str:
        """
        Extract text content from a Gemini response object.

        Args:
            response: The GenerateContentResponse object from the API.

        Returns:
            The content string from the response. Returns an empty string if blocked or no content.
        """
        try:
            return response.text
        except ValueError:
            # This can happen if the response is blocked due to safety settings
            # or if there are no candidates.
            logger.warning("Could not extract text from response. It may have been blocked or empty.")
            logger.debug(f"Full response details: {response}")
            return ""
        except AttributeError:
             logger.warning("Response object has no 'text' attribute. It might be an empty stream chunk.")
             return ""


# --- Example Usage ---
if __name__ == '__main__':
    # Make sure to set your GOOGLE_API_KEY environment variable
    # export GOOGLE_API_KEY='Your-API-Key'
    
    if not os.getenv("GOOGLE_API_KEY"):
        print("Please set the GOOGLE_API_KEY environment variable to run the example.")
    else:
        # 1. Initialize the client
        gemini_chat = Gemini(
            model="gemini-1.5-flash-latest",
            temperature=0.7,
            max_output_tokens=250
        )

        
        chat_messages = [
            {"role": "system", "content": "you are a system that reasons"}
            ,
            {"role": "user", "content": "Explain the importance of the transformer architecture in large language models in one paragraph."}
        ]

        # 3. Get a standard response
        print("--- Standard Response ---")
        response_text = gemini_chat.Initialize(messages=chat_messages)
        print(response_text)
        print("\n" + "="*50 + "\n")

        # # 4. Get a streamed response
        # print("--- Streamed Response ---")
        # stream_generator = gemini_chat.create_stream(messages=chat_messages)
        # for chunk in stream_generator:
        #     print(chunk, end="", flush=True)
        # print("\n")    