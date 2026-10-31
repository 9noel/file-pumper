"""Full-screen terminal UI built on the standard-library ``curses`` module.

The UI is optional: importing :mod:`filepumper` never imports ``curses``. Use
:func:`launch` (or ``filepumper ui``) to start it.
"""

from __future__ import annotations

import contextlib
import locale
import os
import sys
from collections.abc import Sequence

from ..config import UserConfig
from ..exceptions import FilePumperError

__all__ = ["UiUnavailableError", "curses_available", "launch"]


class UiUnavailableError(FilePumperError):
    """The terminal UI cannot start here (no terminal, or no curses module)."""


def curses_available() -> bool:
    """True if the ``curses`` module can be imported on this platform."""
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


def launch(paths: Sequence[str] = (), *, user: UserConfig | None = None) -> int:
    """Run the UI until the user quits. Returns the process exit code.

    ``paths`` may name files to pre-select (or a folder to start in).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UiUnavailableError(
            "the terminal UI needs an interactive terminal; use 'filepumper pad' in scripts"
        )
    try:
        import curses
    except ImportError:
        raise UiUnavailableError(
            "the terminal UI needs Python's 'curses' module. On Windows install it with: "
            "python -m pip install windows-curses"
        ) from None

    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_ALL, "")  # lets curses draw UTF-8 correctly
    os.environ.setdefault("ESCDELAY", "25")  # make the Esc key react quickly

    from .app import App
    from .canvas import CursesCanvas
    from .keys import CursesKeys

    def run(stdscr) -> int:
        with contextlib.suppress(curses.error):
            curses.curs_set(0)  # hide the hardware cursor; the UI draws its own
        app = App(CursesCanvas(stdscr), CursesKeys(stdscr), paths=paths, user=user)
        return app.run()

    return curses.wrapper(run)
