"""File-browser model: directory listing, cursor, filter and multi-file selection.

Pure logic with no drawing, so it can be tested without a terminal. Nothing here
modifies the file system.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

HIDDEN_ATTRIBUTE = 0x2  # FILE_ATTRIBUTE_HIDDEN on Windows


class Kind(StrEnum):
    PARENT = "parent"
    DIR = "dir"
    FILE = "file"
    LINK = "link"  # symbolic link to a regular file
    LINK_DIR = "linkdir"  # symbolic link to a directory
    BROKEN = "broken"  # dangling or unreadable link
    SPECIAL = "special"  # device, pipe, socket, ...


@dataclass(frozen=True)
class Entry:
    """One row in the listing."""

    name: str
    path: str
    kind: Kind
    size: int | None = None
    target: str | None = None

    @property
    def selectable(self) -> bool:
        return self.kind in (Kind.FILE, Kind.LINK)

    @property
    def is_dir_like(self) -> bool:
        return self.kind in (Kind.PARENT, Kind.DIR, Kind.LINK_DIR)


def tilde(path: str) -> str:
    """Shorten the home directory to ``~`` for display."""
    home = os.path.expanduser("~")
    if home and home != os.sep and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def _is_hidden(name: str, st: os.stat_result | None) -> bool:
    if name.startswith("."):
        return True
    attributes = getattr(st, "st_file_attributes", 0) if st is not None else 0
    return bool(attributes & HIDDEN_ATTRIBUTE)


def classify(directory: str, name: str) -> tuple[Entry, bool]:
    """Describe one directory entry; also return whether it is hidden."""
    path = os.path.join(directory, name)
    try:
        link_stat = os.lstat(path)
    except OSError:
        return Entry(name, path, Kind.BROKEN), name.startswith(".")
    hidden = _is_hidden(name, link_stat)
    if stat.S_ISLNK(link_stat.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = None
        try:
            real = os.stat(path)
        except OSError:
            return Entry(name, path, Kind.BROKEN, target=target), hidden
        if stat.S_ISDIR(real.st_mode):
            return Entry(name, path, Kind.LINK_DIR, target=target), hidden
        if stat.S_ISREG(real.st_mode):
            return Entry(name, path, Kind.LINK, real.st_size, target), hidden
        return Entry(name, path, Kind.SPECIAL, target=target), hidden
    if stat.S_ISDIR(link_stat.st_mode):
        return Entry(name, path, Kind.DIR), hidden
    if stat.S_ISREG(link_stat.st_mode):
        return Entry(name, path, Kind.FILE, link_stat.st_size), hidden
    return Entry(name, path, Kind.SPECIAL), hidden


def list_directory(
    directory: str, *, show_hidden: bool = False, text_filter: str = ""
) -> tuple[list[Entry], str | None]:
    """List ``directory``: parent link, then folders, then files, each sorted by name.

    Returns the entries and an error message (``None`` if the folder was readable).
    """
    entries: list[Entry] = []
    error: str | None = None
    try:
        names = os.listdir(directory)
    except OSError as exc:
        names = []
        error = f"cannot read this folder: {exc.strerror or exc}"
    needle = text_filter.casefold()
    for name in names:
        entry, hidden = classify(directory, name)
        if hidden and not show_hidden:
            continue
        if needle and needle not in name.casefold():
            continue
        entries.append(entry)
    entries.sort(key=lambda e: (not e.is_dir_like, e.name.casefold(), e.name))
    parent = os.path.dirname(directory)
    if parent and parent != directory:
        entries.insert(0, Entry("..", parent, Kind.PARENT))
    return entries, error


class FileBrowser:
    """Browsing state: current folder, cursor, scroll position, filter and selection."""

    def __init__(self, start: str | os.PathLike[str] = ".", selected: Iterable[str] = ()) -> None:
        self.cwd = os.path.abspath(start)
        self.show_hidden = False
        self.text_filter = ""
        self.entries: list[Entry] = []
        self.error: str | None = None
        self.cursor = 0
        self.top = 0
        self._selection: dict[str, None] = {}
        for path in selected:
            self._selection[os.path.abspath(path)] = None
        self.reload()

    # -- listing ------------------------------------------------------------------

    def reload(self, focus: str | None = None) -> None:
        """Re-read the folder, keeping the cursor on ``focus`` (a name) when possible."""
        previous = focus if focus is not None else (self.current.name if self.current else None)
        self.entries, self.error = list_directory(
            self.cwd, show_hidden=self.show_hidden, text_filter=self.text_filter
        )
        self.cursor = 0
        if previous is not None:
            for index, entry in enumerate(self.entries):
                if entry.name == previous:
                    self.cursor = index
                    break
        self.top = min(self.top, self.cursor)

    @property
    def current(self) -> Entry | None:
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    # -- movement -----------------------------------------------------------------

    def move(self, delta: int) -> None:
        if self.entries:
            self.cursor = max(0, min(len(self.entries) - 1, self.cursor + delta))

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = max(0, len(self.entries) - 1)

    def ensure_visible(self, height: int) -> None:
        """Scroll so that the cursor row is inside a window of ``height`` rows."""
        height = max(1, height)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + height:
            self.top = self.cursor - height + 1
        self.top = max(0, min(self.top, max(0, len(self.entries) - height)))

    def _enter(self, path: str, focus: str | None = None) -> None:
        self.cwd = path
        self.text_filter = ""
        self.top = 0
        self.reload(focus=focus if focus is not None else "")

    def go_up(self) -> None:
        parent = os.path.dirname(self.cwd)
        if parent and parent != self.cwd:
            self._enter(parent, focus=os.path.basename(self.cwd))

    def open_current(self) -> str | None:
        """Open the folder under the cursor. Returns a message if that is not possible."""
        entry = self.current
        if entry is None:
            return None
        if entry.kind is Kind.PARENT:
            self.go_up()
            return None
        if entry.is_dir_like:
            if not os.access(entry.path, os.R_OK | os.X_OK):
                return f"cannot open {entry.name}: permission denied"
            self._enter(entry.path)
            return None
        return None

    def toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.reload()

    def set_filter(self, text: str) -> None:
        self.text_filter = text
        self.reload(focus="")

    def goto(self, text: str) -> str | None:
        """Jump to a typed folder, or select a typed file. Returns an error message."""
        raw = text.strip()
        if not raw:
            return None
        path = os.path.abspath(os.path.join(self.cwd, os.path.expanduser(raw)))
        if os.path.isdir(path):
            if not os.access(path, os.R_OK | os.X_OK):
                return f"cannot open {raw}: permission denied"
            self._enter(path)
            return None
        entry, _ = classify(os.path.dirname(path), os.path.basename(path))
        if entry.selectable:
            self._enter(os.path.dirname(path), focus=entry.name)
            self._selection[path] = None
            return None
        if entry.kind in (Kind.BROKEN,) and not os.path.lexists(path):
            return f"no such file or folder: {raw}"
        return f"{raw} is not a regular file or folder"

    # -- selection ----------------------------------------------------------------

    @property
    def selection(self) -> list[str]:
        return list(self._selection)

    def is_selected(self, path: str) -> bool:
        return os.path.abspath(path) in self._selection

    def toggle_current(self) -> str | None:
        entry = self.current
        if entry is None:
            return None
        if not entry.selectable:
            if entry.is_dir_like:
                return "folders cannot be selected; press Enter to open one"
            return f"{entry.name} is not a regular file and cannot be selected"
        path = os.path.abspath(entry.path)
        if path in self._selection:
            del self._selection[path]
        else:
            self._selection[path] = None
        return None

    def select_all_visible(self) -> int:
        count = 0
        for entry in self.entries:
            if entry.selectable and os.path.abspath(entry.path) not in self._selection:
                self._selection[os.path.abspath(entry.path)] = None
                count += 1
        return count

    def remove(self, path: str) -> None:
        self._selection.pop(os.path.abspath(path), None)

    def clear_selection(self) -> None:
        self._selection.clear()

    def selected_bytes(self) -> int:
        """Combined current size of the selected files (missing files count as 0)."""
        total = 0
        for path in self._selection:
            try:
                total += os.stat(path).st_size
            except OSError:
                continue
        return total
