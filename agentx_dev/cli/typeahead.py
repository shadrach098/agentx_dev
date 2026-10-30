"""Typing while the agent works, on the normal screen.

While the spinner runs, nothing reads the keyboard, so what the user typed used to wait for the next prompt (or was
lost). ``TypeAhead`` reads keys on a background thread only while it is *resumed* (the app resumes it with the
spinner and pauses it whenever the spinner stops, so a question or an approval prompt gets every key). The line being
typed is drawn under the spinner (``line``); Enter hands it to ``on_submit``.

Windows reads keys with ``msvcrt``; elsewhere stdin is put in cbreak mode (Ctrl+C still interrupts) while resumed.
Nothing runs when stdin is not a terminal."""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Any, Callable, Optional

POLL_SEC = 0.03
MAX_CHARS = 4000
_ESCAPES = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.|.)?")      # arrow keys and other terminal sequences


class TypeAhead:
    def __init__(self, on_submit: Callable[[str], None], on_change: Optional[Callable[[], None]] = None,
                 keys: Optional[Any] = None) -> None:
        self.on_submit = on_submit
        self.on_change = on_change
        self.text = ""
        self._keys = keys if keys is not None else _platform_keys()
        self._lock = threading.Lock()
        self._active = False
        self._holds = 0
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        # While set (``/voice`` is recording), Enter on an empty line is handed over too, as "", to stop it.
        self.submit_empty = False

    @property
    def available(self) -> bool:
        return self._keys is not None

    def line(self) -> str:
        return self.text

    @property
    def active(self) -> bool:
        """Whether keys are being read now (the spinner is on and no question is open)."""
        with self._lock:
            return self._active and self._holds == 0 and not self._closed

    def insert(self, words: str) -> None:
        """Add ``words`` to the line being typed (the words ``/voice`` wrote out), to check before Enter."""
        with self._lock:
            self.text = (self.text.rstrip() + " " + str(words).strip()).strip()[:MAX_CHARS]
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    # -- on and off ------------------------------------------------------------------------------------------

    def resume(self) -> None:
        """Start reading keys (the spinner is on)."""
        if self._keys is None or self._closed:
            return
        with self._lock:
            if self._active:
                return
            self._active = True
            if self._holds == 0:
                self._keys.enter()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="agentx-typeahead", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        """Stop reading keys; returns once no key is being read, so a prompt opened next gets every key."""
        if self._keys is None:
            return
        with self._lock:
            if self._active and self._holds == 0:
                self._keys.leave()
            self._active = False

    def hold(self):
        """``with typeahead.hold():`` no key is read inside the block, even if the spinner starts (a prompt)."""
        from contextlib import contextmanager

        @contextmanager
        def block():
            with self._lock:
                if self._holds == 0 and self._active and self._keys is not None:
                    self._keys.leave()
                self._holds += 1
            try:
                yield
            finally:
                with self._lock:
                    self._holds -= 1
                    if self._holds == 0 and self._active and self._keys is not None:
                        self._keys.enter()
        return block()

    def close(self) -> None:
        self.pause()
        self._closed = True

    # -- keys ------------------------------------------------------------------------------------------------

    def _run(self) -> None:
        while not self._closed:
            submitted = None
            changed = False
            with self._lock:
                if self._active and self._holds == 0:
                    try:
                        chunk = self._keys.read()
                    except Exception:                    # a terminal that cannot be read: stop quietly
                        chunk = None
                        self._closed = True
                    if chunk:
                        submitted, changed = self._feed(chunk)
            if changed and self.on_change is not None:
                try:
                    self.on_change()
                except Exception:
                    pass
            if submitted is not None:
                try:
                    self.on_submit(submitted)
                except Exception:                        # a broken handler must not stop the reader
                    pass
            time.sleep(POLL_SEC)

    def feed(self, chunk: str) -> Optional[str]:
        """Apply typed characters (also used by tests); returns a submitted line, if Enter was pressed."""
        with self._lock:
            submitted, _ = self._feed(chunk)
        if submitted is not None:
            self.on_submit(submitted)
        return submitted

    def _feed(self, chunk: str):
        submitted = None
        before = self.text
        for ch in _ESCAPES.sub(lambda m: "" if m.group(1) else "\x1b", chunk):
            if ch in ("\r", "\n"):
                if self.text.strip():
                    submitted = (submitted + "\n" if submitted else "") + self.text.strip()
                elif self.submit_empty and submitted is None:
                    submitted = ""
                self.text = ""
            elif ch in ("\x08", "\x7f"):
                self.text = self.text[:-1]
            elif ch in ("\x1b", "\x15"):                 # Esc or Ctrl+U: clear the line
                self.text = ""
            elif ch == "\x17":                           # Ctrl+W: drop the last word
                self.text = self.text.rstrip().rsplit(" ", 1)[0] if " " in self.text.strip() else ""
            elif ch == "\t":
                self.text += " "
            elif ch.isprintable() and len(self.text) < MAX_CHARS:
                self.text += ch
        return submitted, self.text != before


class _WindowsKeys:
    def __init__(self, msvcrt: Any) -> None:
        self._m = msvcrt

    def enter(self) -> None:
        pass

    def leave(self) -> None:
        pass

    def read(self) -> str:
        out = []
        while self._m.kbhit() and len(out) < 256:
            ch = self._m.getwch()
            if ch in ("\x00", "\xe0"):                   # an arrow or function key: its code follows
                self._m.getwch()
                continue
            out.append(ch)
        return "".join(out)


class _PosixKeys:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._saved = None

    def enter(self) -> None:
        import termios
        import tty
        try:
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            self._saved = None

    def leave(self) -> None:
        import termios
        if self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
            self._saved = None

    def read(self) -> str:
        import select
        ready, _, _ = select.select([self._fd], [], [], 0)
        if not ready:
            return ""
        return os.read(self._fd, 1024).decode("utf-8", errors="ignore")


def _platform_keys() -> Optional[Any]:
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
    except Exception:
        return None
    if sys.platform == "win32":
        try:
            import msvcrt
            return _WindowsKeys(msvcrt)
        except Exception:
            return None
    try:
        import termios  # noqa: F401
        return _PosixKeys(sys.stdin.fileno())
    except Exception:
        return None
