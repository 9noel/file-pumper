"""Step 2: configure the operation."""

from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..exceptions import UsageError
from ..sizes import format_size
from .app import Preview, Region, Screen
from .canvas import Style
from .form import TextInput
from .keys import BACKTAB, DOWN, ENTER, ESC, LEFT, PGDN, PGUP, RIGHT, SPACE, TAB, UP
from .textutil import fit, safe_text, wrap

LABEL_WIDTH = 20
INPUT_WIDTH = 16
SIDE_WIDTH = 34
WIDE_LAYOUT = 100


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str  # choice | text | toggle | button
    help: str


FIELDS = (
    FieldSpec(
        "mode", "Operation", "choice",
        "Add: append exactly this many bytes to every file. Target: grow every file to exactly "
        "this total size. Files are never shrunk.",
    ),
    FieldSpec(
        "size", "Size", "text",
        "Binary units: KB = KiB = 1024 bytes, MB = MiB = 1024^2, GB = GiB = 1024^3. "
        "Examples: 100MB, 1.5GiB, 4096B.",
    ),
    FieldSpec(
        "pattern", "Pattern", "choice",
        "Zero: 0x00 bytes. Random: from os.urandom. Repeat: your hex bytes, repeated and cut off "
        "exactly at the requested length. Blend: cycled from a sample of the file's own bytes, "
        "so it doesn't read as an obvious flat block next to real content.",
    ),
    FieldSpec(
        "repeat_hex", "Hex bytes", "text",
        "Whole bytes in hex, for example DEADBEEF or 0x00FF. Up to 4096 bytes.",
    ),
    FieldSpec(
        "preset", "Performance", "choice",
        "auto picks balanced, or conservative if this machine cannot be assessed. fast uses "
        "bigger chunks and two files at a time. These are starting points, not benchmarks.",
    ),
    FieldSpec(
        "chunk", "Chunk size", "text",
        "Leave blank for the preset's value. 4KiB to 64MiB, e.g. 4MiB. Memory use stays about "
        "one chunk per file whatever the size.",
    ),
    FieldSpec(
        "jobs", "Files in parallel", "text",
        "Leave blank for the preset's value (usually 1). More workers do not always mean "
        "faster disks. Each file is only ever written by one worker.",
    ),
    FieldSpec(
        "verify", "Verify afterwards", "toggle",
        "Re-read each finished file and check its size, that the original bytes are an unchanged "
        "prefix, and the appended bytes. Takes roughly twice as long.",
    ),
    FieldSpec(
        "backup", "Keep backup (.bak)", "toggle",
        "Keep the original content as a hard link named FILE.bak. The old data stays on disk, "
        "so this needs extra space. Needs a filesystem with hard links.",
    ),
    FieldSpec(
        "allow_large", "Lift size cap", "toggle",
        "Removes the per-file limit on added bytes (default 10 GiB). Free-space checks still "
        "apply and large runs still ask for confirmation.",
    ),
    FieldSpec(
        "follow_symlinks", "Follow symlinks", "toggle",
        "Modify the file a symbolic link points to, instead of refusing the link.",
    ),
    FieldSpec(
        "allow_hardlinks", "Allow hard links", "toggle",
        "Allow files that have other hard links. This name gets a new file and the other "
        "names keep the old content.",
    ),
    FieldSpec(
        "mark", "Add provenance mark", "toggle",
        "Write a small footer into the padding identifying that File Pumper added it, with "
        "the original size and when. Findable with 'filepumper detect'. Needs at least 64 B "
        "of padding; not meant to be hidden.",
    ),
    FieldSpec(
        "next", "Continue to review", "button", "Check everything before anything is written."
    ),
)  # fmt: skip

CHOICES = {
    "mode": (("add", "Add bytes"), ("target", "Reach target size")),
    "pattern": (
        ("zero", "Zero"), ("random", "Random"), ("repeat", "Repeat hex"), ("blend", "Blend"),
    ),
    "preset": tuple((name, name) for name in config.PRESET_NAMES),
}  # fmt: skip
ERROR_FIELD = {"size": "size", "hex": "repeat_hex", "chunk": "chunk", "jobs": "jobs"}
GROUP_STARTS = {"pattern", "preset", "verify", "next"}


class SettingsScreen(Screen):
    step = 1

    def __init__(self, app) -> None:
        super().__init__(app)
        self.focus = next(i for i, spec in enumerate(FIELDS) if spec.key == "size")

    # -- helpers ----------------------------------------------------------------------

    @property
    def form(self):
        return self.app.form

    def _inputs(self) -> dict[str, TextInput]:
        form = self.form
        return {
            "size": form.size,
            "repeat_hex": form.repeat_hex,
            "chunk": form.chunk,
            "jobs": form.jobs,
        }

    def _enabled(self, spec: FieldSpec) -> bool:
        return spec.key != "repeat_hex" or self.form.pattern == "repeat"

    @property
    def current(self) -> FieldSpec:
        return FIELDS[self.focus]

    def _label(self, spec: FieldSpec) -> str:
        if spec.key == "size":
            return "Bytes to add" if self.form.mode == "add" else "Target total size"
        return spec.label

    def _move(self, delta: int) -> None:
        index = self.focus
        for _ in range(len(FIELDS)):
            index = (index + delta) % len(FIELDS)
            if self._enabled(FIELDS[index]):
                self.focus = index
                return

    # -- drawing ----------------------------------------------------------------------

    def draw(self, body: Region) -> None:
        wide = body.width >= WIDE_LAYOUT
        form_width = body.width - SIDE_WIDTH - 1 if wide else body.width
        area = body.sub(0, 0, form_width, body.height)
        preview = self.app.preview()
        roomy = body.height >= 22
        row = 0
        for spec in FIELDS:
            if roomy and spec.key in GROUP_STARTS:
                row += 1
            self._draw_field(area, row, spec, preview)
            row += 1
        self._draw_help(area, row + 1, preview)
        self._draw_summary(area, area.height - 1, preview)
        if wide:
            for r in range(body.height):
                body.put(r, form_width, self.app.canvas.sym.v, Style.DIM)
            self._draw_side(body.sub(form_width + 2, 0, SIDE_WIDTH - 1, body.height), preview)

    def _draw_field(self, area: Region, row: int, spec: FieldSpec, preview: Preview) -> None:
        sym = area.sym
        focused = FIELDS[self.focus] is spec
        enabled = self._enabled(spec)
        if spec.kind == "button":
            if focused:
                area.fill(row, Style.SELECT)
            style = Style.SELECT if focused else Style.ACCENT
            area.put(row, 1, f"{sym.chevron}{sym.chevron} {spec.label}", style)
            return
        label_style = Style.SELECT if focused else (Style.NORMAL if enabled else Style.DIM)
        area.put(row, 0, " " + fit(self._label(spec), LABEL_WIDTH - 1), label_style)
        col = LABEL_WIDTH + 1
        form = self.form
        if not enabled:
            area.put(row, col, "(only for the Repeat pattern)", Style.DIM)
        elif spec.kind == "choice":
            current = getattr(form, spec.key)
            for value, text in CHOICES[spec.key]:
                chosen = value == current
                glyph = sym.radio_on if chosen else sym.radio_off
                col = area.put(row, col, f"{glyph} {text}", Style.ACCENT if chosen else Style.DIM)
                col += 2
        elif spec.kind == "toggle":
            on = getattr(form, spec.key)
            text = f"{sym.box_on} on" if on else f"{sym.box_off} off"
            area.put(row, col, text, Style.GOOD if on else Style.DIM)
        elif spec.kind == "text":
            self._draw_text(area, row, col, spec, focused, preview)

    def _draw_text(
        self, area: Region, row: int, col: int, spec: FieldSpec, focused: bool, preview: Preview
    ) -> None:
        field = self._inputs()[spec.key]
        style = Style.INPUT_ACTIVE if focused else Style.INPUT
        start = max(0, field.cursor - INPUT_WIDTH + 2)
        visible = field.value[start : start + INPUT_WIDTH]
        area.put(row, col, " " * INPUT_WIDTH, style)
        area.put(row, col, visible, style)
        if focused:
            at = col + field.cursor - start
            char = field.value[field.cursor] if field.cursor < len(field.value) else " "
            area.put(row, at, char, Style.SELECT)
        note_col = col + INPUT_WIDTH + 2
        room = area.width - note_col - 1
        if room < 6:
            return
        error_key = {v: k for k, v in ERROR_FIELD.items()}[spec.key]
        if error_key in preview.errors:
            area.put(row, note_col, fit("invalid - see below", room), Style.BAD)
            return
        text, style = self._note(spec.key, field)
        area.put(row, note_col, fit(text, room), style)

    def _note(self, key: str, field: TextInput) -> tuple[str, Style]:
        form = self.form
        if key == "size":
            size = form.size_bytes()
            return (f"= {size:,} bytes" if size is not None else ""), Style.GOOD
        if key == "repeat_hex":
            text = field.value.strip()
            text = text[2:] if text[:2].lower() == "0x" else text
            return f"= {len(text) // 2} byte(s)", Style.GOOD
        try:
            settings = config.resolve_performance(form.preset)
        except UsageError:
            return "", Style.DIM
        if key == "chunk":
            if not field.value.strip():
                return f"preset: {format_size(settings.chunk_size)}", Style.DIM
            return "ok", Style.GOOD
        if not field.value.strip():
            return f"preset: {settings.jobs}", Style.DIM
        return "ok", Style.GOOD

    def _draw_help(self, area: Region, row: int, preview: Preview) -> None:
        spec = self.current
        width = area.width - 4
        for key, message in preview.errors.items():
            if ERROR_FIELD[key] == spec.key:
                for offset, line in enumerate(wrap(message, width)[:2]):
                    area.put(row + offset, 2, line, Style.BAD)
                return
        for offset, line in enumerate(wrap(spec.help, width)[:3]):
            area.put(row + offset, 2, line, Style.DIM)

    def _draw_summary(self, area: Region, row: int, preview: Preview) -> None:
        if preview.errors:
            area.put(row, 1, "Fix the highlighted fields to continue.", Style.WARN)
        elif preview.problems:
            extra = f" (+{len(preview.problems) - 1} more)" if len(preview.problems) > 1 else ""
            area.put(row, 1, fit(safe_text(preview.problems[0]) + extra, area.width - 2), Style.BAD)
        elif preview.plan is not None:
            plan, space = preview.plan, preview.space
            files = len(plan.files)
            parts = [
                f"{files} file{'s' if files != 1 else ''}",
                f"+{format_size(plan.total_add)} in total",
            ]
            style = Style.GOOD
            if space is not None and not space.sufficient:
                parts.append("NOT ENOUGH DISK SPACE")
                style = Style.BAD
            elif space is not None:
                parts.append(f"needs ~{format_size(space.total_required)} free")
            area.put(row, 1, fit(", ".join(parts), area.width - 2), style)

    def _draw_side(self, side: Region, preview: Preview) -> None:
        sym = side.sym
        side.put(0, 0, "Preview", Style.ACCENT)
        side.put(0, 9, "(nothing is written yet)", Style.DIM)
        if preview.errors:
            side.put(2, 0, "Waiting for valid settings...", Style.DIM)
            return
        if preview.plan is None:
            for offset, problem in enumerate(preview.problems[: side.height // 3]):
                for sub, line in enumerate(wrap(safe_text(problem), side.width)[:3]):
                    side.put(2 + offset * 3 + sub, 0, line, Style.BAD)
            return
        row = 2
        for item in preview.plan.files:
            if row >= side.height - 1:
                side.put(row, 0, "...", Style.DIM)
                break
            name = safe_text(item.path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
            side.put(row, 0, fit(name, side.width), Style.NORMAL)
            flow = f"{format_size(item.original_size)} {sym.arrow} {format_size(item.final_size)}"
            if item.skip_reason:
                flow = "skipped: " + item.skip_reason
            side.put(
                row + 1, 2, fit(flow, side.width - 2), Style.DIM if item.skip_reason else Style.GOOD
            )
            row += 2

    # -- keys -------------------------------------------------------------------------

    def hints(self) -> list[tuple[str, str]]:
        sym = self.app.canvas.sym
        spec = self.current
        first = {
            "choice": [("←→" if sym.up != "^" else "Left/Right", "change")],
            "toggle": [("Space", "toggle")],
            "text": [("type", "edit")],
            "button": [("Enter", "review")],
        }[spec.kind]
        return [
            *first,
            (sym.up + sym.down, "field"),
            ("Tab", "next field"),
            ("Esc", "back to files"),
        ]

    def on_key(self, key: str) -> None:
        spec = self.current
        if key in (UP, BACKTAB):
            self._move(-1)
        elif key in (DOWN, TAB):
            self._move(1)
        elif key == ESC:
            self.app.goto("files")
        elif key == "CTRL+N":
            self._continue()
        elif key == PGDN:
            self.focus = len(FIELDS) - 1
        elif key == PGUP:
            self.focus = 0
        elif spec.kind == "choice":
            self._choice_key(spec, key)
        elif spec.kind == "toggle":
            if key in (SPACE, ENTER, LEFT, RIGHT):
                setattr(self.form, spec.key, not getattr(self.form, spec.key))
        elif spec.kind == "text":
            if key == ENTER:
                self._move(1)
            else:
                self._inputs()[spec.key].handle(key)
        elif spec.kind == "button" and key in (ENTER, SPACE):
            self._continue()

    def _choice_key(self, spec: FieldSpec, key: str) -> None:
        values = [value for value, _ in CHOICES[spec.key]]
        index = values.index(getattr(self.form, spec.key))
        if key == LEFT:
            index = (index - 1) % len(values)
        elif key in (RIGHT, SPACE):
            index = (index + 1) % len(values)
        elif key == ENTER:
            self._move(1)
            return
        else:
            return
        setattr(self.form, spec.key, values[index])

    def _continue(self) -> None:
        preview = self.app.preview(force=True)
        if preview.errors:
            first = next(iter(preview.errors))
            target = ERROR_FIELD[first]
            self.focus = next(i for i, spec in enumerate(FIELDS) if spec.key == target)
            self.app.say("Fix the highlighted field first.", Style.BAD)
            return
        self.app.goto("review")
