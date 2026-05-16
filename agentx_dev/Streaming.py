"""
Streaming support for LLM responses in AgentX framework.

This module provides utilities for streaming responses from language models,
enabling real-time output display and improved user experience for long-running operations.
"""

from typing import AsyncIterator, Iterator, Dict, Any, Optional, Callable
from dataclasses import dataclass
import asyncio
import json
import logging

logger = logging.getLogger(__name__)


@dataclass
class StreamChunk:
    """Represents a single chunk in a streaming response."""
    content: str
    delta: str
    is_complete: bool = False
    metadata: Optional[Dict[str, Any]] = None


class StreamBuffer:
    """
    Manages buffering and processing of streaming responses.

    This class accumulates streaming chunks and provides utilities
    for parsing partial JSON responses and managing state.
    """

    def __init__(self):
        self.buffer = ""
        self.complete_content = ""
        self.chunks_received = 0

    def add_chunk(self, chunk: str) -> StreamChunk:
        """
        Add a chunk to the buffer.

        Args:
            chunk: String content to add.

        Returns:
            StreamChunk object with delta and accumulated content.
        """
        self.buffer += chunk
        self.complete_content += chunk
        self.chunks_received += 1

        return StreamChunk(
            content=self.complete_content,
            delta=chunk,
            is_complete=False,
            metadata={"chunks_count": self.chunks_received}
        )

    def finalize(self) -> StreamChunk:
        """
        Mark the stream as complete.

        Returns:
            Final StreamChunk with complete content.
        """
        return StreamChunk(
            content=self.complete_content,
            delta="",
            is_complete=True,
            metadata={"chunks_count": self.chunks_received, "total_length": len(self.complete_content)}
        )

    def try_parse_json(self) -> Optional[Dict[str, Any]]:
        """
        Attempt to parse accumulated buffer as JSON.

        Returns:
            Parsed JSON dict if valid, None otherwise.
        """
        try:
            # Try to extract JSON from markdown code blocks
            content = self.buffer.strip()
            if content.startswith("```json"):
                content = content.split("```json")[-1].split("```")[0].strip()
            elif content.startswith("```"):
                content = content.split("```")[-1].split("```")[0].strip()

            return json.loads(content)
        except json.JSONDecodeError:
            return None

    def clear(self):
        """Clear the buffer while preserving complete content."""
        self.buffer = ""


class StreamProcessor:
    """
    Processes streaming responses with callback support.

    Enables custom processing of stream chunks via callbacks,
    useful for real-time display, logging, or custom handling.
    """

    def __init__(
        self,
        on_chunk: Optional[Callable[[StreamChunk], None]] = None,
        on_complete: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None
    ):
        """
        Initialize the stream processor.

        Args:
            on_chunk: Callback invoked for each chunk received.
            on_complete: Callback invoked when stream completes.
            on_error: Callback invoked if an error occurs.
        """
        self.on_chunk = on_chunk or self._default_chunk_handler
        self.on_complete = on_complete or self._default_complete_handler
        self.on_error = on_error or self._default_error_handler
        self.buffer = StreamBuffer()

    def _default_chunk_handler(self, chunk: StreamChunk):
        """Default handler that prints chunks to console."""
        print(chunk.delta, end="", flush=True)

    def _default_complete_handler(self, content: str):
        """Default handler for completion."""
        logger.info(f"\nStream complete. Total length: {len(content)}")

    def _default_error_handler(self, error: Exception):
        """Default error handler."""
        logger.error(f"Stream error: {error}", exc_info=True)

    async def process_async_stream(self, stream: AsyncIterator[str]) -> str:
        """
        Process an async iterator stream.

        Args:
            stream: AsyncIterator yielding string chunks.

        Returns:
            Complete accumulated content.
        """
        try:
            async for chunk in stream:
                stream_chunk = self.buffer.add_chunk(chunk)
                self.on_chunk(stream_chunk)

            final_chunk = self.buffer.finalize()
            self.on_complete(final_chunk.content)
            return final_chunk.content

        except Exception as e:
            self.on_error(e)
            raise

    def process_sync_stream(self, stream: Iterator[str]) -> str:
        """
        Process a synchronous iterator stream.

        Args:
            stream: Iterator yielding string chunks.

        Returns:
            Complete accumulated content.
        """
        try:
            for chunk in stream:
                stream_chunk = self.buffer.add_chunk(chunk)
                self.on_chunk(stream_chunk)

            final_chunk = self.buffer.finalize()
            self.on_complete(final_chunk.content)
            return final_chunk.content

        except Exception as e:
            self.on_error(e)
            raise


async def stream_with_parser(
    stream: AsyncIterator[str],
    parser: Callable[[str], Any],
    yield_partial: bool = False
) -> AsyncIterator[Any]:
    """
    Stream content and attempt to parse it progressively.

    Args:
        stream: Async iterator of string chunks.
        parser: Function to parse accumulated content.
        yield_partial: If True, yield partial parse results.

    Yields:
        Parsed objects as they become available.
    """
    buffer = StreamBuffer()

    async for chunk in stream:
        buffer.add_chunk(chunk)

        if yield_partial:
            try:
                parsed = parser(buffer.complete_content)
                if parsed:
                    yield parsed
            except Exception:
                # Parsing failed, continue accumulating
                pass

    # Final parse attempt
    try:
        final_parsed = parser(buffer.complete_content)
        yield final_parsed
    except Exception as e:
        logger.error(f"Failed to parse final stream content: {e}")
        yield buffer.complete_content


class OpenAIStreamAdapter:
    """
    Adapter for OpenAI streaming responses.

    Converts OpenAI's streaming format to AgentX StreamChunk format.
    """

    @staticmethod
    async def adapt_async(openai_stream) -> AsyncIterator[StreamChunk]:
        """
        Adapt OpenAI async stream to StreamChunk format.

        Args:
            openai_stream: OpenAI streaming response object.

        Yields:
            StreamChunk objects.
        """
        buffer = StreamBuffer()

        async for chunk in openai_stream:
            if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                content = delta.content if hasattr(delta, 'content') and delta.content else ""

                if content:
                    stream_chunk = buffer.add_chunk(content)
                    yield stream_chunk

        yield buffer.finalize()

    @staticmethod
    def adapt_sync(openai_stream) -> Iterator[StreamChunk]:
        """
        Adapt OpenAI sync stream to StreamChunk format.

        Args:
            openai_stream: OpenAI streaming response object.

        Yields:
            StreamChunk objects.
        """
        buffer = StreamBuffer()

        for chunk in openai_stream:
            if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                content = delta.content if hasattr(delta, 'content') and delta.content else ""

                if content:
                    stream_chunk = buffer.add_chunk(content)
                    yield stream_chunk

        yield buffer.finalize()


# Convenience function for simple streaming
async def simple_stream(
    stream: AsyncIterator[str],
    print_chunks: bool = True
) -> str:
    """
    Simple streaming with optional console output.

    Args:
        stream: Async iterator of string chunks.
        print_chunks: Whether to print chunks to console.

    Returns:
        Complete accumulated content.
    """
    processor = StreamProcessor(
        on_chunk=lambda chunk: print(chunk.delta, end="", flush=True) if print_chunks else None
    )
    return await processor.process_async_stream(stream)
