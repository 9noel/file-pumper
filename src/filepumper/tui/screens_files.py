"""Step 1: choose files."""

from __future__ import annotations

import os

from ..sizes import format_size
from .app import Region, Screen
from .browser import Entry, Kind, tilde
from .canvas import Style
from .form import TextInput
from .keys import (
    BACKSPACE,
    DELETE,
    DOWN,
    END,
    ENTER,
    ESC,
    HOME,
    LEFT,
    PGDN,
    PGUP,
    RIGHT,
    SPACE,
    TAB,
    UP,
)
from .textutil import fit, safe_text, truncate_left

SIDE_WIDTH = 38
WIDE_LAYOUT = 100
SIZE_COLUMN = 11


class FilesScreen(Screen):
    """A folder browser with multi-file selection."""

    step = 0

    def __init__(self, app) -> None:
        super().__init__(app)
        self.mode = "browse"  # browse | filter | goto | selection
        self.input = TextInput()
        self.sel_cursor = 0
        self.list_height = 8

    def on_enter(self) -> None:
        self.app.browser.reload()

    # -- drawing ----------------------------------------------------------------------

    def draw(self, body: Region) -> None:
        wide = body.width >= WIDE_LAYOUT
        main_width = body.width - SIDE_WIDTH - 1 if wide else body.width
        main = body.sub(0, 0, main_width, body.height)
        if wide:
            for row in range(body.height):
                body.put(row, main_width, self.app.canvas.sym.v, Style.DIM)
            self._draw_side(body.sub(main_width + 2, 0, SIDE_WIDTH - 1, body.height))
        if self.mode == "selection":
            self._draw_selection_list(main)
        else:
            self._draw_browser(main)
        self._draw_bottom(main)

    def _draw_browser(self, main: Region) -> None:
        browser = self.app.browser
        sym = main.sym
        right = []
        if browser.show_hidden:
            right.append("hidden shown")
        if browser.text_filter:
            right.append(f"filter: {browser.text_filter}")
        tag = "  [" + ", ".join(right) + "]" if right else ""
        label = "Folder  "
        room = max(10, main.width - len(label) - len(tag) - 2)
        col = main.put(0, 1, label, Style.DIM)
        col = main.put(0, col, truncate_left(safe_text(tilde(browser.cwd)), room), Style.ACCENT)
        main.put(0, col, tag, Style.WARN)

        main.put(1, 1, "Name", Style.DIM)
        main.put(1, main.width - SIZE_COLUMN - 1, fit("Size", SIZE_COLUMN, ">"), Style.DIM)

        self.list_height = max(1, main.height - 3)
        browser.ensure_visible(self.list_height)
        if browser.error:
            main.put(3, 2, browser.error, Style.BAD)
        window = browser.entries[browser.top : browser.top + self.list_height]
        for offset, entry in enumerate(window):
            index = browser.top + offset
            self._draw_entry(main, 2 + offset, entry, index == browser.cursor)
        if not browser.entries:
            main.put(3, 2, "(this folder is empty)", Style.DIM)
        elif len(browser.entries) == 1 and browser.current and browser.current.kind is Kind.PARENT:
            note = "(nothing matches the filter)" if browser.text_filter else "(no files here)"
            main.put(3, 2, note, Style.DIM)
        if len(browser.entries) > self.list_height:  # scroll indicator
            span = len(browser.entries)
            thumb = int((browser.top / max(1, span - self.list_height)) * (self.list_height - 1))
            main.put(2 + thumb, main.width - 1, sym.cursor if sym.cursor != "_" else "#", Style.DIM)

    def _draw_entry(self, main: Region, row: int, entry: Entry, under_cursor: bool) -> None:
        sym = main.sym
        browser = self.app.browser
        selected = browser.is_selected(entry.path)
        marker_w = max(len(sym.box_on), len(sym.box_off))
        if under_cursor:
            main.fill(row, Style.SELECT)
        base = Style.SELECT if under_cursor else Style.NORMAL

        if entry.selectable:
            mark = sym.box_on if selected else sym.box_off
            mark_style = base if under_cursor else (Style.GOOD if selected else Style.DIM)
        else:
            mark, mark_style = " " * marker_w, base
        col = main.put(row, 1, mark, mark_style)
        icon = {Kind.DIR: sym.folder, Kind.PARENT: sym.up, Kind.LINK_DIR: sym.link}.get(
            entry.kind, sym.link if entry.kind is Kind.LINK else " "
        )
        col = main.put(row, col + 1, icon, base if under_cursor else Style.ACCENT)

        name = safe_text(entry.name)
        if entry.is_dir_like and entry.kind is not Kind.PARENT:
            name += "/"
        if entry.kind is Kind.PARENT:
            name = "..  (parent folder)"
        size_col = main.width - SIZE_COLUMN - 1
        room = size_col - col - 2
        if entry.kind in (Kind.LINK, Kind.LINK_DIR, Kind.BROKEN) and entry.target:
            name = f"{name} {sym.arrow} {safe_text(entry.target)}"
        name = fit(name, room)
        if under_cursor:
            name_style = Style.SELECT
        elif selected:
            name_style = Style.GOOD
        elif entry.is_dir_like:
            name_style = Style.ACCENT
        elif entry.kind in (Kind.SPECIAL, Kind.BROKEN):
            name_style = Style.DIM
        else:
            name_style = Style.NORMAL
        main.put(row, col + 1, name, name_style)

        if entry.size is not None:
            size = format_size(entry.size)
        elif entry.kind in (Kind.SPECIAL, Kind.BROKEN):
            size = entry.kind.value
        else:
            size = ""
        main.put(row, size_col, fit(size, SIZE_COLUMN, ">"), base if under_cursor else Style.DIM)

    def _draw_selection_list(self, main: Region) -> None:
        sym = main.sym
        paths = self.app.browser.selection
        main.put(0, 1, "Selected files", Style.ACCENT)
        main.put(0, 17, "- Space/d removes the highlighted file, Esc goes back", Style.DIM)
        self.list_height = max(1, main.height - 3)
        top = max(0, min(self.sel_cursor - self.list_height + 1, len(paths) - self.list_height))
        top = max(0, top)
        for offset, path in enumerate(paths[top : top + self.list_height]):
            row = 2 + offset
            current = top + offset == self.sel_cursor
            if current:
                main.fill(row, Style.SELECT)
            text = truncate_left(safe_text(tilde(path)), main.width - 4)
            main.put(row, 1, f"{sym.box_on} ", Style.SELECT if current else Style.GOOD)
            main.put(row, 2 + len(sym.box_on), text, Style.SELECT if current else Style.NORMAL)

    def _draw_side(self, side: Region) -> None:
        sym = side.sym
        paths = self.app.browser.selection
        side.put(0, 0, f"Selected ({len(paths)})", Style.ACCENT)
        if paths:
            total = format_size(self.app.browser.selected_bytes())
            side.put(0, side.width - len(total) - 1, total, Style.DIM)
        else:
            side.put(2, 0, "Nothing selected yet.", Style.DIM)
            side.put(3, 0, "Move to a file and press", Style.DIM)
            side.put(4, 0, "Space to add it.", Style.DIM)
            return
        rows = side.height - 2
        shown = paths[: rows - 1] if len(paths) > rows else paths
        for row, path in enumerate(shown, start=2):
            text = truncate_left(safe_text(tilde(path)), side.width - 3)
            side.put(row, 0, f"{sym.check} ", Style.GOOD)
            side.put(row, 2, text, Style.NORMAL)
        if len(shown) < len(paths):
            side.put(2 + len(shown), 0, f"+ {len(paths) - len(shown)} more (press s)", Style.DIM)

    def _draw_bottom(self, main: Region) -> None:
        row = main.height - 1
        if self.mode in ("filter", "goto"):
            prompt = "Filter: " if self.mode == "filter" else "Go to folder, or add a file: "
            col = main.put(row, 1, prompt, Style.ACCENT)
            self._draw_input(main, row, col, main.width - col - 2)
            return
        count = len(self.app.browser.selection)
        if count:
            text = f"{count} file{'s' if count != 1 else ''} selected"
            col = main.put(row, 1, text, Style.GOOD)
            main.put(row, col, "  " + format_size(self.app.browser.selected_bytes()), Style.DIM)
            main.put(row, main.width - 20, "Tab: continue", Style.ACCENT)
        else:
            main.put(row, 1, "Nothing selected yet - Space selects a file", Style.DIM)

    def _draw_input(self, region: Region, row: int, col: int, width: int) -> None:
        width = max(4, width)
        value = self.input.value
        cursor = self.input.cursor
        start = max(0, cursor - width + 2)
        visible = value[start : start + width - 1]
        region.put(row, col, " " * width, Style.INPUT_ACTIVE)
        region.put(row, col, visible, Style.INPUT_ACTIVE)
        at = col + (cursor - start)
        char = value[cursor] if cursor < len(value) else " "
        region.put(row, at, char, Style.SELECT)

    # -- keys -------------------------------------------------------------------------

    def hints(self) -> list[tuple[str, str]]:
        sym = self.app.canvas.sym
        if self.mode in ("filter", "goto"):
            return [("Enter", "apply"), ("Esc", "cancel")]
        if self.mode == "selection":
            return [("Space", "remove"), (sym.up + sym.down, "move"), ("Esc", "back")]
        left = "Left" if sym.up == "^" else "\u2190"
        return [
            ("Space", "select"),
            ("Enter", "open"),
            ("Tab", "next"),
            ("q", "quit"),
            (sym.up + sym.down, "move"),
            (left, "up"),
            ("a", "all"),
            ("/", "filter"),
            ("g", "go to"),
            (".", "hidden"),
            ("s", "selection"),
        ]

    def on_key(self, key: str) -> None:
        if self.mode in ("filter", "goto"):
            self._input_key(key)
        elif self.mode == "selection":
            self._selection_key(key)
        else:
            self._browse_key(key)

    def _browse_key(self, key: str) -> None:
        app, browser = self.app, self.app.browser
        if key in (UP, "k"):
            browser.move(-1)
        elif key in (DOWN, "j"):
            browser.move(1)
        elif key == PGUP:
            browser.move(-max(1, self.list_height - 1))
        elif key == PGDN:
            browser.move(max(1, self.list_height - 1))
        elif key == HOME:
            browser.home()
        elif key == END:
            browser.end()
        elif key in (LEFT, BACKSPACE):
            browser.go_up()
        elif key in (ENTER, RIGHT):
            entry = browser.current
            if entry is not None and entry.is_dir_like:
                message = browser.open_current()
                if message:
                    app.say(message, Style.BAD)
            elif key == ENTER:
                self._toggle(advance=False)
        elif key == SPACE:
            self._toggle(advance=True)
        elif key == "a":
            added = browser.select_all_visible()
            app.say(f"Selected {added} more file(s) in this folder.", Style.GOOD)
        elif key == "x":
            browser.clear_selection()
            app.say("Selection cleared.", Style.WARN)
        elif key == ".":
            browser.toggle_hidden()
        elif key == "r":
            browser.reload()
            app.say("Folder reloaded.", Style.DIM)
        elif key == "/":
            self.mode = "filter"
            self.input = TextInput(browser.text_filter)
        elif key == "g":
            self.mode = "goto"
            self.input = TextInput("")
        elif key == "s":
            if browser.selection:
                self.mode = "selection"
                self.sel_cursor = 0
            else:
                app.say("Nothing is selected yet.", Style.WARN)
        elif key in (TAB, "n"):
            if browser.selection:
                app.goto("settings")
            else:
                app.say("Select at least one file first (Space).", Style.WARN)
        elif key == ESC:
            if browser.text_filter:
                browser.set_filter("")
        elif key == "q":
            app.quit()

    def _toggle(self, *, advance: bool) -> None:
        message = self.app.browser.toggle_current()
        if message:
            self.app.say(message, Style.WARN)
        elif advance:
            self.app.browser.move(1)

    def _input_key(self, key: str) -> None:
        browser = self.app.browser
        if key == ESC:
            if self.mode == "filter":
                browser.set_filter("")
            self.mode = "browse"
        elif key == ENTER:
            if self.mode == "goto":
                message = browser.goto(self.input.value)
                if message:
                    self.app.say(message, Style.BAD)
                elif os.path.isfile(os.path.join(browser.cwd, self.input.value.strip() or "\0")):
                    self.app.say("File added to the selection.", Style.GOOD)
            self.mode = "browse"
        elif self.input.handle(key) and self.mode == "filter":
            browser.set_filter(self.input.value)

    def _selection_key(self, key: str) -> None:
        browser = self.app.browser
        paths = browser.selection
        if key == UP:
            self.sel_cursor = max(0, self.sel_cursor - 1)
        elif key == DOWN:
            self.sel_cursor = min(len(paths) - 1, self.sel_cursor + 1)
        elif key in (SPACE, "d", DELETE, BACKSPACE):
            if paths:
                browser.remove(paths[self.sel_cursor])
                paths = browser.selection
                self.sel_cursor = min(self.sel_cursor, len(paths) - 1)
            if not paths:
                self.mode = "browse"
        elif key in (ESC, "s", ENTER, "q"):
            self.mode = "browse"
