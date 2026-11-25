"""The browser tools the main agent gets (``browser_open``, ``browser_click``, ... ``browser_console``), each behind
the gate, over the process's ``BrowserHost`` and one page per conversation.

Every text result starts with ``WEB_CAUTION``: a page, a console line or an error that quotes a page was written by
strangers. The acting tools are judged with the address of the page they act on (``page_url``), read from the host
here and handed to the gate only: it is not a model argument. While the user drives the page the acting tools say so
and do nothing; reading still works.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, Dict, List

from pydantic import BaseModel, Field

from agentx_dev.Pictures import picture_result
from agentx_dev.Tools import StructuredTool
from agentx_dev.cli.approval import Gate
from agentx_dev.cli.browser import BrowserHost, BrowserUnavailable
from agentx_dev.cli.tools import WEB_CAUTION, gate_tool

HELD_BY_USER = "The user is using the browser right now; wait or ask them."
NO_CONSOLE = "(no console messages)"


class _OpenArgs(BaseModel):
    url: str = Field(..., description="http or https address (localhost is fine).")


class _TargetArgs(BaseModel):
    target: str = Field(..., description="Element ref from the page text (e3), or its visible text.")


class _TypeArgs(BaseModel):
    target: str = Field(..., description="Element ref (e3) or its visible text.")
    text: str = Field(..., description="Text to type. Never a password or payment detail.")
    submit: bool = Field(False, description="Press Enter afterwards.")


class _PressArgs(BaseModel):
    key: str = Field(..., description="Enter, Escape, Tab, ArrowDown, ...")


class _NoArgs(BaseModel):
    pass


class _ShotArgs(BaseModel):
    full_page: bool = Field(False, description="The whole page, not just what shows.")


OPEN_DESCRIPTION = ("Open a web page in agentx's own browser (also a local dev server) and read it: title, text and "
                    "numbered elements.")
CLICK_DESCRIPTION = "Click an element of the open page; returns the page after it settles."
TYPE_DESCRIPTION = "Type into a field of the open page; returns the page after."
PRESS_DESCRIPTION = "Press a key on the open page; returns the page after."
BACK_DESCRIPTION = "Go back one page; returns the page."
READ_DESCRIPTION = "Read the open page again: text and numbered elements."
SHOT_DESCRIPTION = "A picture of the open page, for checking how it looks."
CONSOLE_DESCRIPTION = "The newest console messages and page errors of the open page (at most 50)."


def build_browser_tools(host: BrowserHost, owner: Callable[[], str], gate: Gate) -> List[StructuredTool]:
    """The eight browser tools over ``host``, each behind ``gate``. ``owner()`` is the calling conversation's id now
    (it changes with /clear and /resume): each conversation has its own page."""

    def me() -> str:
        try:
            return str(owner() or "")
        except Exception:
            return ""

    def page_url() -> Dict[str, Any]:
        return {"page_url": str(host.state(me()).get("url") or "")}

    def held() -> bool:
        try:
            return bool(host.held_by_user(me()))
        except Exception:
            return False

    def safely(work: Callable[[], Any]) -> str:
        """``work()``'s text, or its failure as text; always starts with the caution."""
        try:
            result = work()
        except BrowserUnavailable as error:
            return WEB_CAUTION + str(error)
        except ValueError as error:
            return WEB_CAUTION + str(error)
        except Exception as error:
            return WEB_CAUTION + f"ERROR: the browser failed: {type(error).__name__}: {error}"
        return result if isinstance(result, str) and result.startswith(WEB_CAUTION) else WEB_CAUTION + str(result)

    def acting(work: Callable[[], Any]) -> str:
        if held():
            return HELD_BY_USER
        return safely(lambda: work().render())

    def browser_open(url: str) -> str:
        return acting(lambda: host.open(me(), url))

    def browser_click(target: str) -> str:
        return acting(lambda: host.click(me(), target))

    def browser_type(target: str, text: str, submit: bool = False) -> str:
        return acting(lambda: host.type(me(), target, text, submit=submit))

    def browser_press(key: str) -> str:
        return acting(lambda: host.press(me(), key))

    def browser_back() -> str:
        return acting(lambda: host.back(me()))

    def browser_read() -> str:
        return safely(lambda: host.snapshot(me()).render())

    def browser_console() -> str:
        return safely(lambda: "\n".join(host.console(me())) or NO_CONSOLE)

    def browser_screenshot(full_page: bool = False) -> Any:
        try:
            png = host.screenshot(me(), full_page=full_page)
        except BrowserUnavailable as error:
            return WEB_CAUTION + str(error)
        except ValueError as error:
            return WEB_CAUTION + str(error)
        except Exception as error:
            return WEB_CAUTION + f"ERROR: the browser failed: {type(error).__name__}: {error}"
        handle, path = tempfile.mkstemp(prefix="agentx-shot-", suffix=".png")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(png)
            return picture_result(path, "the browser page")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def make(func, schema, name, description, extra=False):
        raw = StructuredTool(func=func, args_schema=schema, name=name, description=description)
        return gate_tool(raw, gate, gate_extra=page_url if extra else None)

    return [
        make(browser_open, _OpenArgs, "browser_open", OPEN_DESCRIPTION),
        make(browser_click, _TargetArgs, "browser_click", CLICK_DESCRIPTION, extra=True),
        make(browser_type, _TypeArgs, "browser_type", TYPE_DESCRIPTION, extra=True),
        make(browser_press, _PressArgs, "browser_press", PRESS_DESCRIPTION, extra=True),
        make(browser_back, _NoArgs, "browser_back", BACK_DESCRIPTION, extra=True),
        make(browser_read, _NoArgs, "browser_read", READ_DESCRIPTION),
        make(browser_screenshot, _ShotArgs, "browser_screenshot", SHOT_DESCRIPTION),
        make(browser_console, _NoArgs, "browser_console", CONSOLE_DESCRIPTION),
    ]
