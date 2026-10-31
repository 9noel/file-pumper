"""Keyboard input for the terminal UI.

Keys are plain strings: a printable character stands for itself, and special keys use
names such as ``UP``, ``ENTER``, ``ESC`` and ``SPACE``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable
from typing import Any

UP, DOWN, LEFT, RIGHT = "UP", "DOWN", "LEFT", "RIGHT"
HOME, END, PGUP, PGDN = "HOME", "END", "PGUP", "PGDN"
ENTER, ESC, TAB, BACKTAB = "ENTER", "ESC", "TAB", "BACKTAB"
BACKSPACE, DELETE, SPACE, RESIZE = "BACKSPACE", "DELETE", "SPACE", "RESIZE"


class KeysExhausted(Exception):
    """Raised by scripted input when there are no keys left (tests only)."""


def is_text(key: str) -> bool:
    """True for a key that types a character (including a space)."""
    return key == SPACE or (len(key) == 1 and key.isprintable())


def text_of(key: str) -> str:
    """The character a text key types."""
    return " " if key == SPACE else key


class KeySource(ABC):
    """Where key presses come from."""

    @abstractmethod
    def get(self) -> str:
        """Wait for and return the next key."""

    @abstractmethod
    def poll(self) -> str | None:
        """Return a pending key without waiting, or ``None``."""


class ScriptedKeys(KeySource):
    """A fixed list of keys (tests). ``run_keys`` are only returned by :meth:`poll`."""

    def __init__(self, keys: Iterable[str], run_keys: Iterable[str] = ()) -> None:
        self._keys = deque(keys)
        self._run_keys = deque(run_keys)

    def get(self) -> str:
        if not self._keys:
            raise KeysExhausted
        return self._keys.popleft()

    def poll(self) -> str | None:
        return self._run_keys.popleft() if self._run_keys else None

    @property
    def remaining(self) -> int:
        return len(self._keys)


class CursesKeys(KeySource):
    """Reads keys from a curses window and translates them to the names above."""

    def __init__(self, stdscr: Any) -> None:
        import curses

        self._curses = curses
        self._scr = stdscr
        self._names = {
            curses.KEY_UP: UP,
            curses.KEY_DOWN: DOWN,
            curses.KEY_LEFT: LEFT,
            curses.KEY_RIGHT: RIGHT,
            curses.KEY_HOME: HOME,
            curses.KEY_END: END,
            curses.KEY_PPAGE: PGUP,
            curses.KEY_NPAGE: PGDN,
            curses.KEY_BACKSPACE: BACKSPACE,
            curses.KEY_DC: DELETE,
            curses.KEY_BTAB: BACKTAB,
            curses.KEY_ENTER: ENTER,
            curses.KEY_RESIZE: RESIZE,
        }

    def _translate(self, raw: int | str) -> str | None:
        if isinstance(raw, int):
            return self._names.get(raw)
        if raw in ("\n", "\r"):
            return ENTER
        if raw == "\t":
            return TAB
        if raw == "\x1b":
            return ESC
        if raw in ("\x7f", "\b"):
            return BACKSPACE
        if raw == " ":
            return SPACE
        if raw.isprintable():
            return raw
        if len(raw) == 1 and ord(raw) < 32:
            return "CTRL+" + chr(ord(raw) + 64)
        return None

    def _read(self) -> str | None:
        try:
            raw = self._scr.get_wch()
        except self._curses.error:
            return None  # timed out
        return self._translate(raw)

    def get(self) -> str:
        self._scr.timeout(-1)
        while True:
            key = self._read()
            if key is not None:
                return key

    def poll(self) -> str | None:
        self._scr.timeout(0)
        try:
            return self._read()
        finally:
            self._scr.timeout(-1)
