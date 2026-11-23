"""Tell failures that waiting will not fix from the rest.

A run on a paid model switches to a local fallback only when the paid model
cannot answer at all: no credit, a rejected key, an unknown model, or no
route to the provider. Rate limits, bad requests and the framework's own
stops are not in that group; they are retried or reported as they are today.

Also the errors a run on such a model ends with: ``FallbackFailed`` and ``ContextWindowTooSmall``.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# The words providers use when an account has no credit left (Anthropic, then OpenAI's two forms).
NO_CREDIT_WORDS = ("credit balance is too low", "insufficient_quota", "exceeded your current quota")

OUT_OF_CREDIT = "out of credit"
KEY_REJECTED = "key rejected"
MODEL_NOT_FOUND = "model not found"
UNREACHABLE = "unreachable"

# Errors that reach the runner only after the client's own retries are spent.
_UNREACHABLE_NAMES = re.compile(r"Connection|Timeout|ServiceUnavailable|InternalServer|Overloaded")


def _status(error: BaseException) -> Optional[int]:
    for holder in (error, getattr(error, "response", None)):
        for name in ("status_code", "status"):
            value = getattr(holder, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def fails_for_good(error: BaseException) -> Optional[str]:
    """The reason waiting will not fix this failure, or None when it might."""
    try:
        text = str(error).lower()
    except Exception:
        text = ""
    if any(words in text for words in NO_CREDIT_WORDS):
        return OUT_OF_CREDIT
    status = _status(error)
    if status in (401, 403):
        return KEY_REJECTED
    if status == 404:
        return MODEL_NOT_FOUND
    if status is not None and status >= 500:
        return UNREACHABLE
    if status is not None:
        return None
    if isinstance(error, (ConnectionError, TimeoutError)):
        return UNREACHABLE
    if any(_UNREACHABLE_NAMES.search(cls.__name__) for cls in type(error).__mro__ if cls is not object):
        return UNREACHABLE
    return None


class FallbackFailed(RuntimeError):
    """The fallback model failed too, after it took over from a model that failed for good.

    ``fallback`` is the switch (``{"from", "to", "reason"}``); the fallback's own error is the ``__cause__``.
    """

    def __init__(self, fallback: Dict[str, str], error: BaseException):
        super().__init__(f"{fallback['from']} failed ({fallback['reason']}); "
                         f"the fallback {fallback['to']} failed too: {error}")
        self.fallback = dict(fallback)


class ContextWindowTooSmall(RuntimeError):
    """The model's context window cannot hold even the instructions and the newest message, after old tool results
    were stubbed and the conversation was summarised, so the request was not sent (a server given a longer request
    silently drops its start, which is where the instructions are). ``window`` and ``needed`` are in tokens."""

    def __init__(self, window: int, needed: int):
        super().__init__(f"the request needs about {needed:,} tokens; the model's window is {window:,} tokens "
                         "(a model with a larger window fixes it)")
        self.window = window
        self.needed = needed
