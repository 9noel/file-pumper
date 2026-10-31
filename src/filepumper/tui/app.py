"""Application shell for the terminal UI: layout, main loop and shared state.

The UI is a thin layer over the same engine the command line uses
(``engine.build_plan``, ``engine.plan_space``, ``engine.run_batch``); it contains no
file-writing logic of its own.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .. import __version__, engine
from ..config import UserConfig
from ..engine import BatchPlan, BatchResult, PadOptions
from ..exceptions import FilePumperError, ValidationError
from ..exitcodes import EXIT_INTERRUPTED, EXIT_OK, exit_code_for
from ..safety import SpaceReport
from .browser import FileBrowser
from .canvas import Canvas, Style
from .form import Form
from .keys import RESIZE, KeysExhausted, KeySource
from .textutil import display_width, set_ellipsis, truncate

MIN_WIDTH = 64
MIN_HEIGHT = 20
STEPS = ("Files", "Settings", "Review", "Run")
HEADER_ROWS = 2
FOOTER_ROWS = 2


@dataclass
class Region:
    """A rectangle of the canvas; drawing is clipped to it and uses local coordinates."""

    canvas: Canvas
    x: int
    y: int
    width: int
    height: int

    @property
    def sym(self):
        return self.canvas.sym

    def put(self, row: int, col: int, text: str, style: Style = Style.NORMAL) -> int:
        """Draw ``text`` clipped to the region. Returns the column after the text."""
        if not 0 <= row < self.height or col >= self.width or not text:
            return col
        text = truncate(text, self.width - col, "")
        self.canvas.put(self.y + row, self.x + col, text, style)
        return col + display_width(text)

    def segments(self, row: int, col: int, parts: Iterable[tuple[str, Style]]) -> int:
        for text, style in parts:
            col = self.put(row, col, text, style)
        return col

    def fill(self, row: int, style: Style, col: int = 0, width: int | None = None) -> None:
        span = (self.width - col) if width is None else width
        if span > 0:
            self.put(row, col, " " * span, style)

    def hline(self, row: int, style: Style = Style.DIM) -> None:
        self.put(row, 0, self.sym.h * self.width, style)

    def sub(self, x: int, y: int, width: int, height: int) -> Region:
        return Region(self.canvas, self.x + x, self.y + y, width, height)


@dataclass
class Preview:
    """What the current selection and settings would do, computed with the real engine."""

    errors: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    options: PadOptions | None = None
    plan: BatchPlan | None = None
    space: SpaceReport | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and not self.problems and self.plan is not None

    @property
    def can_run(self) -> bool:
        return (
            self.ok
            and self.plan is not None
            and bool(self.plan.writes)
            and (self.space is None or self.space.sufficient)
        )


class Screen:
    """Base class for the five screens."""

    step = 0

    def __init__(self, app: App) -> None:
        self.app = app

    def on_enter(self) -> None:
        """Called when the screen becomes active."""

    def draw(self, body: Region) -> None:
        raise NotImplementedError

    def hints(self) -> list[tuple[str, str]]:
        """Footer key hints, most important first."""
        return []

    def on_key(self, key: str) -> None:
        raise NotImplementedError


class App:
    """Owns the shared state and runs the draw/read/handle loop."""

    def __init__(
        self,
        canvas: Canvas,
        keys: KeySource,
        *,
        paths: Sequence[str] = (),
        user: UserConfig | None = None,
        report_dir: str | None = None,
    ) -> None:
        self.canvas = canvas
        self.keys = keys
        self.user = user or UserConfig()
        self.form = Form.from_config(self.user)
        existing = [os.path.abspath(p) for p in paths if os.path.isfile(p)]
        if existing:
            start = os.path.dirname(existing[0])
        elif paths and os.path.isdir(paths[0]):
            start = os.path.abspath(paths[0])
        else:
            start = os.getcwd()
        self.browser = FileBrowser(start, selected=existing)
        if existing:
            self.browser.reload(focus=os.path.basename(existing[0]))
        self.report_dir = report_dir or os.getcwd()
        self.screen: Screen
        self.message: tuple[str, Style] | None = None
        self.exit_code: int | None = None
        self.exhausted = False
        self.worst_code = EXIT_OK
        self.last_plan: BatchPlan | None = None
        self.last_space: SpaceReport | None = None
        self.last_result: BatchResult | None = None
        self._preview_key: tuple[object, ...] | None = None
        self._preview: Preview | None = None
        self.goto("files")

    # -- navigation -----------------------------------------------------------------

    def goto(self, name: str) -> None:
        from .screens_files import FilesScreen
        from .screens_run import ReviewScreen
        from .screens_settings import SettingsScreen

        screens = {"files": FilesScreen, "settings": SettingsScreen, "review": ReviewScreen}
        self.screen = screens[name](self)
        self.screen.on_enter()

    def say(self, text: str, style: Style = Style.WARN) -> None:
        self.message = (text, style)

    def quit(self) -> None:
        self.exit_code = self.worst_code

    def record(self, result: BatchResult) -> None:
        self.last_result = result
        self.worst_code = max(self.worst_code, exit_code_for(result))

    # -- live preview -----------------------------------------------------------------

    def preview(self, *, force: bool = False) -> Preview:
        """Plan the current selection with the current settings (cached between key presses)."""
        paths = self.browser.selection
        key = (tuple(paths), self.form.signature())
        if not force and key == self._preview_key and self._preview is not None:
            return self._preview
        self._preview_key = key
        self._preview = self._compute_preview(paths)
        return self._preview

    def _compute_preview(self, paths: list[str]) -> Preview:
        errors = self.form.errors()
        if errors:
            return Preview(errors=errors)
        try:
            options = self.form.to_options()
        except FilePumperError as exc:
            return Preview(problems=[str(exc)])
        if not paths:
            return Preview(options=options, problems=["no files selected"])
        try:
            plan = engine.build_plan(paths, options)
        except ValidationError as exc:
            return Preview(options=options, problems=list(exc.problems))
        return Preview(options=options, plan=plan, space=engine.plan_space(plan))

    # -- main loop --------------------------------------------------------------------

    def run(self) -> int:
        """Run until the user quits. Returns the process exit code."""
        try:
            while self.exit_code is None:
                self.draw()
                key = self.keys.get()
                if key == RESIZE:
                    continue
                self.message = None
                self.screen.on_key(key)
        except KeysExhausted:
            self.exhausted = True
            return self.worst_code
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
        return self.exit_code

    # -- drawing ----------------------------------------------------------------------

    def draw(self) -> None:
        canvas = self.canvas
        set_ellipsis(canvas.sym.ellipsis)
        canvas.clear()
        width, height = canvas.size()
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            self._draw_too_small(width, height)
        else:
            root = Region(canvas, 0, 0, width, height)
            self._draw_header(root)
            body = root.sub(0, HEADER_ROWS, width, height - HEADER_ROWS - FOOTER_ROWS)
            self.screen.draw(body)
            self._draw_footer(root)
        canvas.refresh()

    def _draw_too_small(self, width: int, height: int) -> None:
        lines = [
            "Terminal too small",
            f"{width}x{height} - File Pumper needs at least {MIN_WIDTH}x{MIN_HEIGHT}.",
            "Enlarge the window to continue (Ctrl+C quits).",
        ]
        for row, text in enumerate(lines):
            text = truncate(text, width)
            style = Style.BAD if row == 0 else Style.DIM
            self.canvas.put(row, 0, text, style)

    def _draw_header(self, root: Region) -> None:
        sym = root.sym
        col = root.put(0, 1, f"{sym.logo} FILE PUMPER", Style.TITLE)
        root.put(0, col + 1, f"v{__version__}", Style.DIM)
        parts: list[tuple[str, Style]] = []
        for index, name in enumerate(STEPS):
            if index:
                parts.append((f" {sym.chevron} ", Style.DIM))
            if index == self.screen.step:
                parts.append((f" {index + 1} {name} ", Style.SELECT))
            elif index < self.screen.step:
                parts.append((f"{sym.check} {name}", Style.GOOD))
            else:
                parts.append((f"{index + 1} {name}", Style.DIM))
        used = sum(display_width(text) for text, _ in parts)
        root.segments(0, max(col + 12, root.width - used - 1), parts)
        root.hline(1)

    def _draw_footer(self, root: Region) -> None:
        status_row = root.height - 2
        if self.message:
            text, style = self.message
            root.put(status_row, 1, truncate(text, root.width - 2), style)
        col = 1
        for key, label in self.screen.hints():
            need = display_width(key) + 2 + display_width(label) + 2
            if col + need > root.width:
                break
            col = root.put(root.height - 1, col, f" {key} ", Style.KEYCAP)
            col = root.put(root.height - 1, col, f" {label}  ", Style.DIM)
