"""Tokens: usage, the token limit question, retries and compaction.

A part of ``ChatApp`` (agentx_dev/cli/app.py), which inherits it."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

from agentx_dev.ChatModel import _is_timeout
from agentx_dev.cli.compact import RETRY_AFTER_MESSAGES, compact, needs_compaction
from agentx_dev.Runner.Trim import TrimPolicy, trim_tool_results
from agentx_dev.cli.memory import summary_entry_text
from agentx_dev.cli.settings import write_user_token_limit
from agentx_dev.cli.team import current_helper, on_main


class UsageCommands:
    """Mixed into ``ChatApp``, whose attributes these methods use."""

    def _track_usage_model(self, label: str, model: Any, role: str) -> None:
        """Remember a model whose token counters ``/usage`` should add up (once per object)."""
        if model is not None and not any(model is known for _, _, known in self._usage_models):
            self._usage_models.append((str(label), role, model))
            try:
                model.on_usage = self._check_token_limit
                model.on_retry = self._show_retry
            except (AttributeError, TypeError):          # an object that takes no attributes keeps no usage either
                pass

    def _show_retry(self, attempt: int, error: BaseException, delay: float) -> None:
        """A tracked model's ``on_retry`` hook: say that a model request failed and is being sent again, so a long
        wait does not look like a hang. (Said on the main thread: a team helper's request runs in a worker.)"""
        on_main(self._say_retry, attempt, error, wait=False)

    def _say_retry(self, attempt: int, error: BaseException) -> None:
        what = "got no answer in time" if _is_timeout(error) else f"failed ({type(error).__name__})"
        helper = current_helper()
        who = f" ({self._safe(helper, 60)})" if helper else ""
        with self.progress.paused():
            self.notice(f"A model request{who} {what}; trying again (attempt {attempt + 1}).")

    def _ask_to_go_on(self, used: int, sent: int = 0) -> str:
        """The limit question: "y" (another limit), "a" (go on, and do not ask again in this message) or "n". Enter,
        end of input, Ctrl+C and a reader that fails are all no; any other answer is asked once more."""
        cache = (f" ({sent:,} sent and received; input from the provider's cache counts as a tenth, about what it "
                 "costs)") if sent > used else ""
        prompt = (f"This message has used {used:,} tokens{cache}; your limit is {self.token_limit:,} a message "
                  f"(/limit changes it). Keep going for up to {self.token_limit:,} more? a = yes, and do not ask "
                  "again in this message. No wraps up with what was found so far. [y/a/N] ")
        with self.progress.paused():
            for attempt in range(2):
                try:
                    answer = str(self.read_short(prompt)).strip().lower()
                except (EOFError, KeyboardInterrupt, Exception):
                    return "n"
                if answer in ("y", "yes"):
                    return "y"
                if answer in ("a", "all", "always"):
                    return "a"
                if answer in ("", "n", "no"):
                    return "n"
                if attempt == 0:
                    self.notice("Please answer y, a or n.")
        return "n"

    def _say_paused(self, text: str) -> None:
        with self.progress.paused():
            self.notice(text)

    def set_token_limit(self, limit: int) -> None:
        """``/limit <tokens>``: use ``limit`` from now on (0 is no limit) and save it in ~/.agentx/settings.json."""
        self.token_limit = max(0, int(limit))
        _, message = write_user_token_limit(self.home, self.token_limit)
        shown = f"{self.token_limit:,} tokens a message" if self.token_limit else "off"
        self.notice(f"Token limit: {shown}. {self._safe(message, 300)}")

    def show_token_limit(self) -> None:
        if self.token_limit:
            self.notice(f"Token limit: {self.token_limit:,} tokens a message. When a message gets there, agentx asks "
                        "before it goes on. /limit <tokens> changes it (300k, 1m, and 0 for no limit also work).")
        else:
            self.notice("Token limit: off. /limit <tokens> sets one (for example /limit 200k).")

    @staticmethod
    def _counters(model: Any) -> Tuple[int, int, int, int]:
        """``(requests, input tokens, output tokens, cached input tokens)`` a model has recorded; zeros when it
        keeps none."""
        usage = getattr(model, "usage", None)
        try:
            return (int(getattr(usage, "total_calls", 0) or 0), int(getattr(usage, "total_input_tokens", 0) or 0),
                    int(getattr(usage, "total_output_tokens", 0) or 0),
                    int(getattr(usage, "total_cache_read_tokens", 0) or 0))
        except (TypeError, ValueError):
            return 0, 0, 0, 0

    def _usage_totals(self) -> Tuple[int, int, int, int]:
        """``(requests, input, output, cached input)`` summed over every model used so far."""
        rows = [self._counters(model) for _, _, model in self._usage_models]
        return tuple(sum(row[i] for row in rows) for i in range(4))              # type: ignore[return-value]

    @staticmethod
    def _cached_note(cached: int, tokens_in: int) -> str:
        if not cached or not tokens_in:
            return ""
        return f" ({cached:,} of the input, {cached * 100 // tokens_in}%, from the provider's cache)"

    def show_usage(self) -> None:
        """``/usage``: the tokens used this session, per model, and what the last turn used. Counts what the
        provider's replies report; a model that reports nothing shows zeros."""
        rows: Dict[Tuple[str, str], List[int]] = {}
        for label, role, model in self._usage_models:
            row = rows.setdefault((label, role), [0, 0, 0, 0])
            for i, value in enumerate(self._counters(model)):
                row[i] += value
        requests, tokens_in, tokens_out, cached_in = self._usage_totals()
        if requests == 0:
            self.notice("No model calls yet in this session.")
            return
        lines = ["Session (all models):",
                 f"  {requests:,} requests, {tokens_in:,} input tokens, {tokens_out:,} output tokens"
                 + self._cached_note(cached_in, tokens_in)]
        if len(rows) > 1 or any(role == "helper" for _, role in rows):
            for (label, role), (calls, inp, out, cached) in rows.items():
                if calls:
                    extra = f", {cached:,} of the input from the provider's cache" if cached else ""
                    lines.append(f"  {self._safe(label, 60)} ({role}): {calls:,} requests, {inp:,} in, {out:,} out{extra}")
        if self._last_turn_usage is not None and self._last_turn_usage[0] > 0:
            calls, inp, out, cached = self._last_turn_usage
            lines.append(f"Last turn: {calls:,} request{'s' if calls != 1 else ''}, {inp:,} input tokens, "
                         f"{out:,} output tokens" + self._cached_note(cached, inp))
        limit = f"{self.token_limit:,} tokens a message" if self.token_limit else "off"
        lines.append(f"Token limit: {limit} (/limit).")
        lines.append("Every request re-sends the conversation, so input grows with the number of steps in a turn. "
                     "Long results are trimmed, big files are read in pieces, and /clear starts fresh. Input the "
                     "provider served from its cache is billed at its cheaper cached rate.")
        self.notice("\n".join(lines))

    def _summary_call(self, prompt: str) -> str:
        """One plain call to the current model (no tools, no agent loop): every model's ``Initialize`` takes the
        message list and returns the reply text."""
        reply = self.model.Initialize(messages=[{"role": "user", "content": prompt}])
        return reply if isinstance(reply, str) else ""

    def _maybe_compact(self) -> None:
        """Summarise a very long conversation before the turn. Nothing is removed unless a summary replaces it;
        after a failure it is not tried again until ``RETRY_AFTER_MESSAGES`` more messages have been added.
        The runner rebuilds its messages from ``session.history`` on every turn, so replacing it is enough."""
        assert self.session is not None
        history = self.session.history
        # Measured on what the model is actually sent (old big tool results are stubs), not on the saved history.
        if len(history) < self._compact_wait or not needs_compaction(trim_tool_results(history, TrimPolicy())):
            return
        self.progress.start("Summarising the conversation")
        try:
            result = compact(history, self._summary_call)
        finally:
            self.progress.stop()
        if result.status == "done":
            self.session.history = result.history
            self._forget_skill_reads()          # the skill texts went into the summary: they may be read again
            self.notice(f"Summarised {result.replaced} earlier messages to keep the conversation short.")
            self._append_log(summary_entry_text(result.summary, datetime.now(), self._hide))
        elif result.status == "failed":
            self._compact_wait = len(history) + RETRY_AFTER_MESSAGES
            self.notice("The conversation is getting long, but it could not be summarised right now; "
                        "it will be tried again later.")

    def _forget_skill_reads(self) -> None:
        runner = getattr(self.session, "runner", None)
        for tool in getattr(runner, "tools", None) or []:
            forget = getattr(tool, "forget_reads", None)
            if callable(forget):
                forget()
