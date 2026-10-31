"""Settings-form model: field values, validation, and conversion to engine options.

The form reuses the same parsing and validation as the command line (``parse_size``,
``make_pattern``, ``resolve_performance``), so the UI can never accept something the
CLI would reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import config
from ..config import UserConfig
from ..engine import Mode, PadOptions
from ..exceptions import InvalidSizeError, UsageError, ValidationError
from ..patterns import make_pattern, parse_hex_pattern
from ..sizes import GIB, KIB, MIB, parse_size
from .keys import BACKSPACE, DELETE, END, HOME, LEFT, RIGHT, is_text, text_of


def size_text(num_bytes: int) -> str:
    """Render a byte count as input text that :func:`parse_size` reads back exactly."""
    for unit, factor in (("GiB", GIB), ("MiB", MIB), ("KiB", KIB)):
        if num_bytes >= factor and num_bytes % factor == 0:
            return f"{num_bytes // factor}{unit}"
    return f"{num_bytes}B"


@dataclass
class TextInput:
    """A single-line editable text value with a cursor."""

    value: str = ""
    cursor: int = -1

    def __post_init__(self) -> None:
        if self.cursor < 0 or self.cursor > len(self.value):
            self.cursor = len(self.value)

    def set(self, value: str) -> None:
        self.value = value
        self.cursor = len(value)

    def handle(self, key: str) -> bool:
        """Apply an editing key. Returns ``True`` if the key was consumed."""
        if is_text(key):
            self.value = self.value[: self.cursor] + text_of(key) + self.value[self.cursor :]
            self.cursor += 1
        elif key == BACKSPACE:
            if self.cursor:
                self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
                self.cursor -= 1
        elif key == DELETE:
            self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
        elif key == LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == RIGHT:
            self.cursor = min(len(self.value), self.cursor + 1)
        elif key == HOME:
            self.cursor = 0
        elif key == END:
            self.cursor = len(self.value)
        else:
            return False
        return True


@dataclass
class Form:
    """Everything the settings screen edits."""

    mode: str = "add"
    pattern: str = "zero"
    preset: str = "auto"
    verify: bool = False
    backup: bool = False
    allow_large: bool = False
    follow_symlinks: bool = False
    allow_hardlinks: bool = False
    mark: bool = False
    size: TextInput = field(default_factory=lambda: TextInput("10MB"))
    repeat_hex: TextInput = field(default_factory=lambda: TextInput("DEADBEEF"))
    chunk: TextInput = field(default_factory=TextInput)  # blank = use the preset
    jobs: TextInput = field(default_factory=TextInput)  # blank = use the preset
    max_add: int | None = None  # from the configuration file; None = built-in default

    @classmethod
    def from_config(cls, user: UserConfig) -> Form:
        """Start from the user's configuration file preferences (if any)."""
        form = cls()
        if user.preset:
            form.preset = user.preset
        if user.pattern:
            form.pattern = user.pattern
        if user.repeat_hex:
            form.repeat_hex.set(user.repeat_hex)
        if user.chunk_size:
            form.chunk.set(size_text(user.chunk_size))
        if user.jobs:
            form.jobs.set(str(user.jobs))
        if user.verify is not None:
            form.verify = user.verify
        form.max_add = user.max_add
        return form

    # -- validation -----------------------------------------------------------------

    def errors(self) -> dict[str, str]:
        """Problems per field name (``size``, ``hex``, ``chunk``, ``jobs``); empty if valid."""
        problems: dict[str, str] = {}
        try:
            parse_size(self.size.value)
        except InvalidSizeError as exc:
            problems["size"] = str(exc)
        if self.pattern == "repeat":
            try:
                parse_hex_pattern(self.repeat_hex.value)
            except ValidationError as exc:
                problems["hex"] = str(exc)
        if self.chunk.value.strip():
            try:
                chunk = parse_size(self.chunk.value)
            except InvalidSizeError as exc:
                problems["chunk"] = str(exc)
            else:
                if not config.MIN_CHUNK_SIZE <= chunk <= config.MAX_CHUNK_SIZE:
                    problems["chunk"] = (
                        f"must be between {size_text(config.MIN_CHUNK_SIZE)} "
                        f"and {size_text(config.MAX_CHUNK_SIZE)}"
                    )
        if self.jobs.value.strip():
            try:
                jobs = int(self.jobs.value)
            except ValueError:
                problems["jobs"] = "must be a whole number"
            else:
                if not 1 <= jobs <= config.MAX_JOBS:
                    problems["jobs"] = f"must be between 1 and {config.MAX_JOBS}"
        return problems

    def signature(self) -> tuple[object, ...]:
        """A hashable snapshot of every value, used to cache the live preview."""
        return (
            self.mode, self.pattern, self.preset, self.verify, self.backup, self.allow_large,
            self.follow_symlinks, self.allow_hardlinks, self.mark, self.size.value,
            self.repeat_hex.value, self.chunk.value, self.jobs.value, self.max_add,
        )  # fmt: skip

    def size_bytes(self) -> int | None:
        """The parsed size, or ``None`` while the text is not valid."""
        try:
            return parse_size(self.size.value)
        except InvalidSizeError:
            return None

    def to_options(self) -> PadOptions:
        """Build engine options. Raises ``UsageError`` if any field is invalid."""
        problems = self.errors()
        if problems:
            raise UsageError("; ".join(f"{name}: {text}" for name, text in problems.items()))
        chunk = parse_size(self.chunk.value) if self.chunk.value.strip() else None
        jobs = int(self.jobs.value) if self.jobs.value.strip() else None
        hex_text = self.repeat_hex.value if self.pattern == "repeat" else None
        if self.allow_large:
            max_add = None
        else:
            max_add = self.max_add if self.max_add is not None else config.DEFAULT_MAX_ADD
        return PadOptions(
            mode=Mode(self.mode),
            size=parse_size(self.size.value),
            pattern=make_pattern(self.pattern, hex_text),
            settings=config.resolve_performance(self.preset, chunk, jobs),
            verify=self.verify,
            backup=self.backup,
            follow_symlinks=self.follow_symlinks,
            allow_hardlinks=self.allow_hardlinks,
            max_add=max_add,
            mark=self.mark,
        )
