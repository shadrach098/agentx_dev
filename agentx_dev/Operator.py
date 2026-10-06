"""
Ask the operator: let a Supervisor, and the agents it runs, put a question to the person
who set the task, and carry the answer through the run.

``ask_user=True`` on a Supervisor uses the built-in asker below, which picks the channel by
where the code runs (notebook, terminal, IDE console) and never blocks in a headless
process. ``ask_user=<function>`` routes questions through the developer's own channel (a
chat UI, a websocket, a queue).
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import inspect
import sys
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from agentx_dev.AsyncTools import AsyncStructuredTool
from agentx_dev.Tools import StructuredTool, logger

ASK_TOOL_NAME = "ask_user"
MAX_ANSWER_CHARS = 2000
CONTROLLING_TTY_TIMEOUT = 300.0            # seconds; the prompt can land in a window nobody sees

REASON_NO_CHANNEL = "no_channel"
REASON_DECLINED = "declined"
REASON_TIMEOUT = "timeout"
REASON_LIMIT = "limit"
REASON_ERROR = "error"

NO_ANSWER_TEXT = (
    "[no operator answer] Proceed on a stated assumption and say plainly what you assumed "
    "in your answer."
)


class NoChannel(Exception):
    """There is no way to reach an operator here (headless process, a kernel without stdin)."""


class AskTimeout(Exception):
    """The operator did not answer in time."""


# ----------------------------------------------------------------------------
# Where are we running?
# ----------------------------------------------------------------------------

def _ipython_shell() -> Any:
    getter = getattr(sys.modules.get("IPython"), "get_ipython", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _in_notebook() -> bool:
    """A Jupyter-style kernel (Jupyter, VS Code, Colab). Colab's shell subclasses
    ``ZMQInteractiveShell`` under another name, so walk the MRO."""
    shell = _ipython_shell()
    return shell is not None and any(c.__name__ == "ZMQInteractiveShell" for c in type(shell).__mro__)


def _notebook_can_prompt() -> bool:
    """False under papermill / nbconvert, where the kernel has stdin switched off."""
    kernel = getattr(_ipython_shell(), "kernel", None)
    return bool(getattr(kernel, "_allow_stdin", False))


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and bool(sys.stdin.isatty())
    except Exception:
        return False


def _open_controlling_tty():
    """The process's controlling terminal: ``CONIN$``/``CONOUT$`` on Windows, ``/dev/tty``
    elsewhere. ``(None, None)`` when there is none (cron, Docker without ``-it``, a server)."""
    names = ("CONIN$", "CONOUT$") if sys.platform == "win32" else ("/dev/tty", "/dev/tty")
    tty_in = tty_out = None
    try:
        tty_in = open(names[0], "r")
        tty_out = open(names[1], "w")
        return tty_in, tty_out
    except OSError:
        for f in (tty_in, tty_out):
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass
        return None, None


# ----------------------------------------------------------------------------
# Reading an answer
# ----------------------------------------------------------------------------

def _call_with_timeout(fn: Callable[[], Any], timeout: float) -> Any:
    """Run ``fn()`` in a daemon thread (with the caller's contextvars) and wait at most
    ``timeout`` seconds. A thread stuck in a blocking read is abandoned: it ends with the
    process. Whatever ``fn`` raised is re-raised here."""
    box: dict = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            box["value"] = ctx.run(fn)
        except BaseException as e:                         # re-raised in the caller
            box["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AskTimeout(f"no answer within {timeout:g} seconds")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# One blocking read at a time. A reader abandoned by a timeout is still waiting on the
# terminal and would take the operator's next keystrokes, so while one is outstanding a
# later ask is refused (NoChannel) rather than started behind it.
_reader_lock = threading.Lock()
_reader_busy = False


def _refuse_if_reader_outstanding() -> None:
    with _reader_lock:
        if _reader_busy:
            raise NoChannel("an earlier prompt is still waiting for input")


def _claim_reader() -> None:
    global _reader_busy
    with _reader_lock:
        if _reader_busy:
            raise NoChannel("an earlier prompt is still waiting for input")
        _reader_busy = True


def _release_reader() -> None:
    global _reader_busy
    with _reader_lock:
        _reader_busy = False


def _read_input(prompt: str) -> Optional[str]:
    try:
        return input(prompt)
    except EOFError:
        return None
    except Exception as e:                                  # StdinNotImplementedError is a RuntimeError
        if type(e).__name__ == "StdinNotImplementedError":
            raise NoChannel("this kernel cannot take input") from e
        raise


def _ask_controlling_tty(question: str, prefix: str, timeout: Optional[float]) -> Optional[str]:
    _refuse_if_reader_outstanding()
    tty_in, tty_out = _open_controlling_tty()
    if tty_in is None:
        raise NoChannel("no terminal to ask on")

    def read() -> Optional[str]:
        claimed = False
        try:
            _claim_reader()
            claimed = True
            tty_out.write(f"\n{prefix} needs your input\n  question: {question}\n  > ")
            tty_out.flush()
            line = tty_in.readline()
        except EOFError:
            return None
        finally:
            for f in (tty_in, tty_out):
                try:
                    f.close()
                except Exception:
                    pass
            if claimed:
                _release_reader()
        return line.strip() if line else None

    return _call_with_timeout(read, CONTROLLING_TTY_TIMEOUT if timeout is None else timeout)


def builtin_asker(question: str, *, prefix: str = "[agent]", timeout: Optional[float] = None) -> Optional[str]:
    """Ask the operator ``question`` on the channel this process has, in order:

    1. a notebook kernel: ``input()`` (the input box under the cell; ``timeout`` is not
       enforced here, a person is looking at the box and Interrupt works);
    2. a terminal: ``input()``;
    3. a controlling terminal when stdin is not one (IDE run panel, wrapper subprocess),
       with a 300 s default timeout because the prompt can land in a window nobody sees;
    4. nothing (headless): :class:`NoChannel`.

    Returns the typed answer, or ``None`` on EOF. ``KeyboardInterrupt`` is not swallowed:
    pressing Interrupt means stop the run. Raises :class:`NoChannel` / :class:`AskTimeout`.
    """
    if _in_notebook():
        if not _notebook_can_prompt():
            raise NoChannel("this notebook kernel cannot take input")
        return _read_input(f"{question}\n> ")
    if _stdin_is_tty():
        prompt = f"\n{prefix} needs your input\n  question: {question}\n> "
        if timeout is None:
            return _read_input(prompt)
        _refuse_if_reader_outstanding()

        def read() -> Optional[str]:
            _claim_reader()
            try:
                return _read_input(prompt)
            finally:
                _release_reader()
        return _call_with_timeout(read, timeout)
    return _ask_controlling_tty(question, prefix, timeout)


# ----------------------------------------------------------------------------
# Replies
# ----------------------------------------------------------------------------

@dataclass
class Reply:
    """What the operator said, or why nothing came back."""

    text: Optional[str] = None
    reason: Optional[str] = None          # None when answered; else one of the REASON_* words
    deduped: bool = False

    @property
    def answered(self) -> bool:
        return self.text is not None


def _clean_answer(raw: Any) -> Reply:
    if raw is None:
        return Reply(None, REASON_DECLINED)
    text = str(raw).strip()
    if not text:
        return Reply(None, REASON_DECLINED)
    return Reply(text[:MAX_ANSWER_CHARS])


def _normalize(question: str) -> str:
    return " ".join(str(question).split()).casefold()


def _shown(question: str, context: str = "") -> str:
    """What the asker receives: the question, with the agent's one-line context appended."""
    return f"{question}\n(context: {context})" if context else question


def reply_text(reply: Reply) -> str:
    """The tool result an agent sees. Never an error, so its stuck logic is not triggered."""
    return f"[operator] {reply.text}" if reply.answered else NO_ANSWER_TEXT


def validate_ask_user(ask_user: Any, is_async: bool) -> Any:
    """Check an ``ask_user=`` value at construction time and return it unchanged."""
    if ask_user is None or ask_user is False or ask_user is True:
        return ask_user
    if not callable(ask_user):
        raise TypeError("ask_user must be True, False/None, or a function (question) -> answer")
    if not is_async and asyncio.iscoroutinefunction(ask_user):
        raise TypeError("ask_user is an async function: use AsyncSupervisor, or pass a plain function")
    return ask_user


# ----------------------------------------------------------------------------
# The channel: the one place that knows about the operator
# ----------------------------------------------------------------------------

class OperatorChannel:
    """One per Supervisor run. Owns the asker, the question budget, de-duplication,
    one-at-a-time asking, timeouts, the event buffer and the records.

    ``ask`` / ``aask`` never raise because of the operator channel (a callback that raises or
    times out is a no-answer); only ``KeyboardInterrupt`` and task cancellation propagate.
    """

    def __init__(self, asker: Optional[Callable[[str], Any]], *, builtin: bool = False,
                 max_questions: int = 3, timeout: Optional[float] = None,
                 is_async: bool = False, verbose: bool = False, prefix: str = "[agent]"):
        self._asker = asker
        self._builtin = builtin
        self.max_questions = max(0, int(max_questions))
        self.timeout = timeout
        self.is_async = is_async
        self.verbose = verbose
        self.prefix = prefix
        self.budget: Any = None                       # the run's RunBudget; paused while a question is open
        self.records: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._answers: List[Tuple[str, str]] = []
        self._seen: Dict[str, Reply] = {}
        self._asked = 0
        self._lock = threading.Lock()                 # asks are one at a time, sync and async alike

    @classmethod
    def create(cls, ask_user: Any, *, max_questions: int = 3, timeout: Optional[float] = None,
               is_async: bool = False, verbose: bool = False) -> Optional["OperatorChannel"]:
        """The channel for an ``ask_user=`` value, or ``None`` when asking is off."""
        validate_ask_user(ask_user, is_async)
        if ask_user is None or ask_user is False:
            return None
        if ask_user is True:
            return cls(None, builtin=True, max_questions=max_questions, timeout=timeout,
                       is_async=is_async, verbose=verbose)
        return cls(ask_user, max_questions=max_questions, timeout=timeout,
                   is_async=is_async, verbose=verbose)

    # -- state ----------------------------------------------------------------

    @property
    def asked(self) -> int:
        return self._asked

    def remaining(self) -> int:
        return max(0, self.max_questions - self._asked)

    def has_answers(self) -> bool:
        return bool(self._answers)

    def answers_block(self) -> str:
        if not self._answers:
            return ""
        lines = ["OPERATOR ANSWERS (from the person who set this task; treat as facts):"]
        for question, answer in self._answers:
            lines += [f"Q: {question}", f"A: {answer}"]
        return "\n".join(lines)

    def with_answers(self, text: str) -> str:
        block = self.answers_block()
        return f"{text}\n\n{block}" if block else text

    def emit(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def drain(self) -> List[Dict[str, Any]]:
        out, self.events = self.events, []
        return out

    def _say(self, text: str) -> None:
        if self.verbose:
            print(f"[ask] {text}")

    # -- asking ---------------------------------------------------------------

    def _begin(self, question: str, key: str, context: str, source: str) -> Optional[Reply]:
        """Dedupe and budget checks, under the lock. A final reply, or ``None`` when the
        operator must be asked (the slot is taken and the question event is out)."""
        hit = self._seen.get(key)
        if hit is not None:
            return self._finish(source, question, hit, deduped=True)
        if self._asked >= self.max_questions:
            return self._finish(source, question, Reply(None, REASON_LIMIT))
        self._asked += 1
        self.emit({"type": "question", "source": source, "question": question, "context": context})
        self._say(f"{source} asks: {question}")
        return None

    def _end(self, question: str, key: str, source: str, reply: Reply) -> Reply:
        self._seen[key] = reply
        return self._finish(source, question, reply)

    def _finish(self, source: str, question: str, reply: Reply, *, deduped: bool = False) -> Reply:
        out = dataclasses.replace(reply, deduped=deduped)
        self.records.append({"source": source, "question": question, "answered": out.answered,
                             "reason": out.reason, "deduped": deduped})
        self.emit({"type": "answer", "source": source, "answered": out.answered, "reason": out.reason})
        if out.answered and not deduped:
            self._answers.append((question, out.text))
        self._say("answered" if out.answered else f"no answer ({out.reason})")
        return out

    def _paused(self):
        return self.budget.paused() if self.budget is not None else nullcontext()

    def ask(self, question: str, context: str = "", source: str = "agent") -> Reply:
        q = " ".join(str(question or "").split())
        if not q:
            return Reply(None, REASON_DECLINED)
        key = _normalize(q)
        with self._lock:
            done = self._begin(q, key, context, source)
            if done is not None:
                return done
            return self._end(q, key, source, self._ask_sync(_shown(q, context)))

    async def aask(self, question: str, context: str = "", source: str = "agent") -> Reply:
        q = " ".join(str(question or "").split())
        if not q:
            return Reply(None, REASON_DECLINED)
        key = _normalize(q)
        while not self._lock.acquire(blocking=False):      # never blocks the loop; a cancel cannot leak the lock
            await asyncio.sleep(0.02)
        try:
            done = self._begin(q, key, context, source)
            if done is not None:
                return done
            return self._end(q, key, source, await self._ask_async(_shown(q, context)))
        finally:
            self._lock.release()

    def _ask_sync(self, shown: str) -> Reply:
        try:
            with self._paused():
                if self._builtin:
                    raw = builtin_asker(shown, prefix=self.prefix, timeout=self.timeout)
                elif self.timeout is not None:
                    raw = _call_with_timeout(lambda: self._asker(shown), self.timeout)
                else:
                    raw = self._asker(shown)
        except NoChannel:
            return Reply(None, REASON_NO_CHANNEL)
        except AskTimeout:
            return Reply(None, REASON_TIMEOUT)
        except Exception as e:                             # not KeyboardInterrupt / CancelledError
            logger.warning(f"ask_user raised; treating as no answer: {e}")
            return Reply(None, REASON_ERROR)
        if inspect.isawaitable(raw):
            getattr(raw, "close", lambda: None)()
            logger.warning("ask_user returned an awaitable on a sync Supervisor; treating as no answer")
            return Reply(None, REASON_ERROR)
        return _clean_answer(raw)

    async def _ask_async(self, shown: str) -> Reply:
        async def go() -> Any:
            if asyncio.iscoroutinefunction(self._asker):
                raw = await self._asker(shown)
            else:
                raw = await asyncio.to_thread(self._asker, shown)
            return await raw if inspect.isawaitable(raw) else raw

        try:
            with self._paused():
                if self._builtin:
                    if _in_notebook():
                        # ipykernel's input() is not safe from a worker thread, and a person is
                        # looking at the input box (Interrupt works): ask on the loop thread.
                        raw = builtin_asker(shown, prefix=self.prefix, timeout=self.timeout)
                    else:
                        # A terminal read can wait minutes: keep the loop (other tasks, timers,
                        # cancellation) running while the person types.
                        raw = await asyncio.to_thread(builtin_asker, shown, prefix=self.prefix,
                                                      timeout=self.timeout)
                else:
                    raw = await asyncio.wait_for(go(), self.timeout)
        except NoChannel:
            return Reply(None, REASON_NO_CHANNEL)
        except (AskTimeout, asyncio.TimeoutError):
            return Reply(None, REASON_TIMEOUT)
        except Exception as e:
            logger.warning(f"ask_user raised; treating as no answer: {e}")
            return Reply(None, REASON_ERROR)
        return _clean_answer(raw)


# ----------------------------------------------------------------------------
# The planner may ask before it plans
# ----------------------------------------------------------------------------

_ASK_INSTRUCTION = """

── ASKING THE OPERATOR ──────────────────────────────────────────────
You may ask the operator instead of planning, but ONLY when the task leaves out a fact you cannot reasonably assume and a wrong guess would waste the whole run (for example: which three competitors, which file, which account). Then reply with {"ask": [{"question": "...", "why": "..."}]} and no plan: at most <<N>> questions, each specific and self-contained. If a sensible assumption lets you proceed, write the plan instead.
────────────────────────────────────────────────────────────────────
"""

NO_ANSWER_PLAN_NOTE = (
    "\n\nNo operator answered your questions. Plan on stated assumptions and make each "
    "assumption explicit in the step queries."
)


def ask_instruction(limit: int) -> str:
    """The planner-prompt block that offers the ``ask`` reply."""
    return _ASK_INSTRUCTION.replace("<<N>>", str(limit))


def parse_ask_request(raw: Any, limit: int) -> List[Dict[str, str]]:
    """The planner's ``ask`` value as ``[{"question", "why"}]``, at most ``limit`` long.
    Anything that is not a list of questions gives ``[]``."""
    if not isinstance(raw, list) or limit <= 0:
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            question, why = item, ""
        elif isinstance(item, dict) and isinstance(item.get("question"), str):
            question, why = item["question"], str(item.get("why") or "")
        else:
            continue
        question = " ".join(question.split())
        if question:
            out.append({"question": question, "why": " ".join(why.split())})
    return out[:limit]


# ----------------------------------------------------------------------------
# The tool agents use to ask
# ----------------------------------------------------------------------------

class AskUserArgs(BaseModel):
    question: str = Field(
        ...,
        description=("The question to ask the operator. Be specific and self-contained: they "
                     "cannot see your conversation. Ask ONE thing at a time."),
    )
    context: str = Field(
        "",
        description=("Optional one line of context shown with the question so the operator "
                     "knows what stage of the task you are at."),
    )


ASK_USER_DESCRIPTION = (
    "Ask the operator ONE question when a fact you need is missing and your tools cannot find "
    "it. Good: which three competitors, which file, which account. Bad: asking permission for "
    "each step, tone or audience, or anything you can look up. Returns the operator's reply, or "
    "a note that nobody answered (then proceed on a stated assumption)."
)

ASK_ADDENDUM_LINE = (
    "\n\n- If a fact you need is missing and your tools cannot find it, call ask_user with one "
    "specific question. Do not guess."
)


def _is_async_runner(runner: Any) -> bool:
    return asyncio.iscoroutinefunction(getattr(runner, "Initialize", None))


def make_ask_tool(parent: Any, channel: OperatorChannel, source: str) -> Any:
    """The ``ask_user`` tool for ``parent``. An async parent gets an async tool that awaits the
    channel; a sync parent a sync tool. ``source`` (the agent's name) labels events and records."""
    if _is_async_runner(parent):
        async def ask_user(question: str, context: str = "") -> str:
            return reply_text(await channel.aask(question, context, source))

        tool = AsyncStructuredTool(func=ask_user, args_schema=AskUserArgs,
                                   name=ASK_TOOL_NAME, description=ASK_USER_DESCRIPTION)
    else:
        def ask_user(question: str, context: str = "") -> str:
            return reply_text(channel.ask(question, context, source))

        tool = StructuredTool(func=ask_user, args_schema=AskUserArgs,
                              name=ASK_TOOL_NAME, description=ASK_USER_DESCRIPTION)
    tool.cacheable = False            # asking has side effects: never answer it from the cache
    return tool


@contextmanager
def attach_ask_user(agents: Dict[str, Any], channel: Optional[OperatorChannel]):
    """Give every runner in ``agents`` (``name -> runner``) the ``ask_user`` tool for the block.

    Nothing is attached when ``channel`` is ``None``. Objects that cannot take a tool (no
    ``add_tool``), or that already have a tool named ``ask_user`` (the developer's wins), are
    left alone. The tool is removed on exit, even if the block raises, so the developer's
    runners are exactly as they were."""
    attached: List[Any] = []
    if channel is not None:
        for name, r in agents.items():
            if not callable(getattr(r, "add_tool", None)) or not hasattr(r, "registry"):
                continue
            if r.registry.has(ASK_TOOL_NAME):
                continue
            r.add_tool(make_ask_tool(r, channel, name))
            attached.append(r)
    try:
        yield
    finally:
        for r in attached:
            r.remove_tool(ASK_TOOL_NAME)


def ask_human_tool(*, prompt_prefix: str = "[agent]", ask_timeout: Optional[float] = None) -> Any:
    """A standalone ``ask_human`` tool for any ``AgentRunner``, built on the built-in asker:
    it prompts in the notebook's input box or the terminal, and never blocks in a headless
    process. Returns the operator's reply, or a note that nobody answered."""

    def _ask(question: str, context: str = "") -> str:
        try:
            raw = builtin_asker(_shown(question, context), prefix=prompt_prefix, timeout=ask_timeout)
        except (NoChannel, AskTimeout):
            return NO_ANSWER_TEXT
        return reply_text(_clean_answer(raw))

    return StructuredTool(
        func=_ask, args_schema=AskUserArgs, name="ask_human",
        description=(
            "Ask the operator ONE clarifying question when the task is genuinely ambiguous and a "
            "guess would waste a whole run. Good uses: acronym disambiguation, which file or "
            "account is meant. BAD uses: tone or audience, permission for every step, or asking "
            "the operator to do your research. Returns the operator's typed reply, or a note "
            "that nobody answered."
        ),
    )
