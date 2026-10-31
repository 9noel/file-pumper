"""Drawing surfaces for the terminal UI.

The screens draw onto a :class:`Canvas`. :class:`CursesCanvas` is the real terminal;
:class:`BufferCanvas` is an in-memory grid used by the tests, so every screen can be
rendered and inspected without a terminal.
"""

from __future__ import annotations

import contextlib
import locale
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .textutil import char_width


class Style(Enum):
    """Semantic text styles; each canvas maps them to colors and attributes."""

    NORMAL = auto()
    DIM = auto()
    BOLD = auto()
    ACCENT = auto()
    TITLE = auto()
    GOOD = auto()
    WARN = auto()
    BAD = auto()
    SELECT = auto()  # the row under the cursor
    KEYCAP = auto()  # a key name in the footer
    INPUT = auto()  # an editable field
    INPUT_ACTIVE = auto()  # the editable field that has focus
    BAR_FILL = auto()
    BAR_EMPTY = auto()


@dataclass(frozen=True)
class Symbols:
    """Glyphs used by the screens, with a plain-ASCII twin for limited terminals."""

    h: str
    v: str
    tl: str
    tr: str
    bl: str
    br: str
    ellipsis: str
    bullet: str
    radio_on: str
    radio_off: str
    box_on: str
    box_off: str
    check: str
    cross: str
    dash: str
    warn: str
    arrow: str
    up: str
    down: str
    folder: str
    file: str
    link: str
    chevron: str
    cursor: str
    bar_full: str
    bar_empty: str
    bar_partial: str  # eight fill levels, lightest first (may be empty -> no partials)
    spinner: str
    logo: str


UNICODE = Symbols(
    h="─", v="│", tl="╭", tr="╮", bl="╰", br="╯", ellipsis="…", bullet="•",
    radio_on="●", radio_off="○", box_on="◼", box_off="◻", check="✔", cross="✘",
    dash="–", warn="▲", arrow="→", up="↑", down="↓", folder="▸", file=" ", link="↪",
    chevron="›", cursor="▌", bar_full="█", bar_empty="░", bar_partial=" ▏▎▍▌▋▊▉",
    spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", logo="▟█▙",
)  # fmt: skip

ASCII = Symbols(
    h="-", v="|", tl="+", tr="+", bl="+", br="+", ellipsis="...", bullet="*",
    radio_on="(*)", radio_off="( )", box_on="[x]", box_off="[ ]", check="+", cross="x",
    dash="-", warn="!", arrow="->", up="^", down="v", folder=">", file=" ", link="@",
    chevron=">", cursor="_", bar_full="#", bar_empty="-", bar_partial="",
    spinner="|/-\\", logo="[#]",
)  # fmt: skip


def detect_symbols() -> Symbols:
    """Pick Unicode glyphs when the terminal can show them, else ASCII.

    Set ``FILEPUMPER_ASCII=1`` to force ASCII.
    """
    if os.environ.get("FILEPUMPER_ASCII"):
        return ASCII
    if os.environ.get("TERM") == "linux":  # the bare Linux console lacks many glyphs
        return ASCII
    encoding = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
    return UNICODE if encoding.startswith("utf") else ASCII


class Canvas(ABC):
    """A character grid that screens draw on."""

    sym: Symbols = UNICODE

    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Return ``(width, height)`` in character cells."""

    @abstractmethod
    def clear(self) -> None:
        """Erase everything."""

    @abstractmethod
    def put(self, y: int, x: int, text: str, style: Style = Style.NORMAL) -> None:
        """Draw ``text`` at row ``y``, column ``x``; anything outside the grid is clipped."""

    @abstractmethod
    def refresh(self) -> None:
        """Show what was drawn."""


class BufferCanvas(Canvas):
    """An in-memory canvas used for tests and snapshots."""

    def __init__(self, width: int = 100, height: int = 30, symbols: Symbols = UNICODE) -> None:
        self.sym = symbols
        self.resize(width, height)
        self.refreshes = 0

    def resize(self, width: int, height: int) -> None:
        self._width, self._height = width, height
        self.clear()

    def size(self) -> tuple[int, int]:
        return self._width, self._height

    def clear(self) -> None:
        self._cells = [[" "] * self._width for _ in range(self._height)]
        self._styles = [[Style.NORMAL] * self._width for _ in range(self._height)]

    def put(self, y: int, x: int, text: str, style: Style = Style.NORMAL) -> None:
        if not 0 <= y < self._height:
            return
        for char in text:
            step = char_width(char)
            if x < 0:
                x += step
                continue
            if x + max(step, 1) > self._width:
                break
            if step == 0:  # combining mark: attach to the previous cell
                if x > 0:
                    self._cells[y][x - 1] += char
                continue
            self._cells[y][x] = char
            self._styles[y][x] = style
            if step == 2:
                self._cells[y][x + 1] = ""
                self._styles[y][x + 1] = style
            x += step

    def refresh(self) -> None:
        self.refreshes += 1

    # -- inspection helpers (tests) --------------------------------------------------

    def line(self, y: int) -> str:
        return "".join(self._cells[y]).rstrip()

    def lines(self) -> list[str]:
        return [self.line(y) for y in range(self._height)]

    def text(self) -> str:
        """The whole screen as text, trailing blanks removed."""
        return "\n".join(self.lines()).rstrip("\n")

    def style_at(self, y: int, x: int) -> Style:
        return self._styles[y][x]

    def find(self, needle: str) -> tuple[int, int] | None:
        """Row and column of the first occurrence of ``needle``."""
        for y, row in enumerate(self.lines()):
            x = row.find(needle)
            if x >= 0:
                return y, x
        return None


class CursesCanvas(Canvas):
    """The real terminal, drawn through the standard-library ``curses`` module."""

    def __init__(self, stdscr: Any, symbols: Symbols | None = None) -> None:
        import curses  # imported here so the rest of the package works without curses

        self._curses = curses
        self._scr = stdscr
        self.sym = symbols or detect_symbols()
        self._attrs = self._build_attributes()

    def _build_attributes(self) -> dict[Style, int]:
        curses = self._curses
        attrs = dict.fromkeys(Style, curses.A_NORMAL)
        attrs[Style.BOLD] = curses.A_BOLD
        attrs[Style.DIM] = curses.A_DIM
        attrs[Style.SELECT] = curses.A_REVERSE
        attrs[Style.KEYCAP] = curses.A_REVERSE | curses.A_BOLD
        attrs[Style.INPUT] = curses.A_UNDERLINE
        attrs[Style.INPUT_ACTIVE] = curses.A_REVERSE
        attrs[Style.TITLE] = curses.A_BOLD
        attrs[Style.BAR_EMPTY] = curses.A_DIM
        if os.environ.get("NO_COLOR"):
            return attrs
        try:
            if not curses.has_colors():
                return attrs
            curses.start_color()
            curses.use_default_colors()
            if curses.COLORS < 8:
                return attrs
        except curses.error:
            return attrs

        def pair(number: int, fg: int, bg: int = -1) -> int:
            curses.init_pair(number, fg, bg)
            return curses.color_pair(number)

        try:
            attrs[Style.ACCENT] = pair(1, curses.COLOR_CYAN) | curses.A_BOLD
            attrs[Style.GOOD] = pair(2, curses.COLOR_GREEN)
            attrs[Style.WARN] = pair(3, curses.COLOR_YELLOW)
            attrs[Style.BAD] = pair(4, curses.COLOR_RED) | curses.A_BOLD
            attrs[Style.TITLE] = pair(5, curses.COLOR_MAGENTA) | curses.A_BOLD
            attrs[Style.SELECT] = pair(6, curses.COLOR_BLACK, curses.COLOR_CYAN)
            attrs[Style.KEYCAP] = pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)
            attrs[Style.INPUT] = pair(8, curses.COLOR_WHITE, curses.COLOR_BLUE)
            attrs[Style.INPUT_ACTIVE] = pair(9, curses.COLOR_BLACK, curses.COLOR_YELLOW)
            attrs[Style.BAR_FILL] = pair(2, curses.COLOR_GREEN)
        except curses.error:
            pass
        return attrs

    def size(self) -> tuple[int, int]:
        height, width = self._scr.getmaxyx()
        return width, height

    def clear(self) -> None:
        self._scr.erase()

    def put(self, y: int, x: int, text: str, style: Style = Style.NORMAL) -> None:
        width, height = self.size()
        if not 0 <= y < height or x >= width or not text:
            return
        if x < 0:
            text, x = text[-x:], 0
        room = width - x
        clipped, used = [], 0
        for char in text:
            step = char_width(char)
            if used + step > room:
                break
            clipped.append(char)
            used += step
        # Writing the bottom-right cell raises after drawing it; that is harmless.
        with contextlib.suppress(self._curses.error):
            self._scr.addstr(y, x, "".join(clipped), self._attrs[style])

    def refresh(self) -> None:
        self._scr.noutrefresh()
        self._curses.doupdate()
