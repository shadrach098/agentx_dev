import pytest

from agentx_dev.ChatModel import CostBudgetExceeded, ModelDeclined, RetryBudgetExceeded
from agentx_dev.Runner.Fallback import (KEY_REJECTED, MODEL_NOT_FOUND, NO_CREDIT_WORDS, OUT_OF_CREDIT,
                                        UNREACHABLE, fails_for_good)


class Fake(Exception):
    def __init__(self, status=None, message=""):
        super().__init__(message)
        if status is not None:
            self.status_code = status


class StatusOnly(Exception):
    status = 401


class Wrapped(Exception):
    class _Response:
        status_code = 404

    response = _Response()


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class ServiceUnavailableError(Exception):
    pass


class InternalServerError(Exception):
    pass


class OverloadedError(Exception):
    pass


class RateLimitError(Exception):
    pass


@pytest.mark.parametrize("error, expected", [
    (Fake(400, "Your credit balance is too low to access the API"), OUT_OF_CREDIT),
    (Fake(429, "Error code: insufficient_quota"), OUT_OF_CREDIT),
    (Fake(None, "You exceeded your current quota"), OUT_OF_CREDIT),
    (Fake(500, "credit balance is too low"), OUT_OF_CREDIT),
    (Fake(401, "bad key"), KEY_REJECTED),
    (Fake(403, "forbidden"), KEY_REJECTED),
    (StatusOnly("x"), KEY_REJECTED),
    (Fake(404, "no such model"), MODEL_NOT_FOUND),
    (Wrapped("x"), MODEL_NOT_FOUND),
    (Fake(500, "boom"), UNREACHABLE),
    (Fake(503, "down"), UNREACHABLE),
    (ConnectionError(), UNREACHABLE),
    (TimeoutError(), UNREACHABLE),
    (APIConnectionError("x"), UNREACHABLE),
    (APITimeoutError("x"), UNREACHABLE),
    (ServiceUnavailableError("x"), UNREACHABLE),
    (InternalServerError("x"), UNREACHABLE),
    (OverloadedError("x"), UNREACHABLE),
    (Fake(429, "rate limit"), None),
    (RateLimitError("slow down"), None),
    (Fake(400, "bad request"), None),
    (Fake(422, "unprocessable"), None),
    (Fake(408, "request timeout"), None),
    (ModelDeclined("no", reason="refusal"), None),
    (CostBudgetExceeded(2.0, 1.0), None),
    (RetryBudgetExceeded("over"), None),
    (KeyboardInterrupt(), None),
    (ValueError("x"), None),
])
def test_fails_for_good(error, expected):
    assert fails_for_good(error) == expected


def test_credit_words_match_the_app():
    from agentx_dev.cli import app
    assert NO_CREDIT_WORDS == ("credit balance is too low", "insufficient_quota", "exceeded your current quota")
    assert app._out_of_credit(Fake(400, "Your Credit Balance Is Too Low"))
    assert app._out_of_credit(Fake(429, "insufficient_quota"))
    assert app._out_of_credit(Fake(429, "You exceeded your current quota"))
    assert not app._out_of_credit(Fake(429, "rate limit"))
