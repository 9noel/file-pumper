"""Steps 3 and 4: review the plan, watch the run, read the results."""

from __future__ import annotations

import os
import threading
import time

from .. import config, engine, reporting
from ..engine import BatchPlan, BatchResult, FileStatus
from ..reporting import RateMeter, estimate_eta, format_duration, format_rate
from ..sizes import format_size, format_size_exact
from .app import Preview, Region, Screen
from .browser import tilde
from .canvas import Style, Symbols
from .keys import BACKSPACE, DOWN, ENTER, ESC, LEFT, PGDN, PGUP, UP
from .textutil import display_width, fit, safe_text, truncate_left, wrap

COLUMN = 11


def bar(symbols: Symbols, width: int, fraction: float) -> list[tuple[str, Style]]:
    """A progress bar as styled segments, using eighth-block partial cells when available."""
    width = max(1, width)
    fraction = max(0.0, min(1.0, fraction))
    cells = fraction * width
    full = int(cells)
    parts: list[tuple[str, Style]] = []
    used = full
    if full:
        parts.append((symbols.bar_full * full, Style.BAR_FILL))
    if symbols.bar_partial and full < width:
        index = int((cells - full) * len(symbols.bar_partial))
        if index > 0:
            parts.append((symbols.bar_partial[index], Style.BAR_FILL))
            used += 1
    if used < width:
        parts.append((symbols.bar_empty * (width - used), Style.BAR_EMPTY))
    return parts


def draw_box(body: Region, lines: list[tuple[str, Style]], title: str = "") -> None:
    """Draw a centered bordered message box over the body."""
    sym = body.sym
    inner = min(body.width - 6, max([display_width(text) for text, _ in lines] + [30]) + 2)
    height = len(lines) + 2
    left = (body.width - inner - 2) // 2
    top = max(0, (body.height - height) // 2)
    box = body.sub(left, top, inner + 2, height)
    box.put(0, 0, sym.tl + sym.h * inner + sym.tr, Style.ACCENT)
    for row in range(1, height - 1):
        box.put(row, 0, sym.v + " " * inner + sym.v, Style.ACCENT)
    box.put(height - 1, 0, sym.bl + sym.h * inner + sym.br, Style.ACCENT)
    if title:
        box.put(0, 2, f" {title} ", Style.TITLE)
    for row, (text, style) in enumerate(lines, start=1):
        box.put(row, 2, fit(text, inner - 2), style)


class ReviewScreen(Screen):
    """Shows exactly what will happen. Nothing has been written at this point."""

    step = 2

    def __init__(self, app) -> None:
        super().__init__(app)
        self.scroll = 0
        self.confirming = False
        self.preview = Preview()
        self.rows = 5

    def on_enter(self) -> None:
        self.preview = self.app.preview(force=True)

    # -- drawing ----------------------------------------------------------------------

    def draw(self, body: Region) -> None:
        preview = self.preview
        body.put(0, 1, "Nothing has been written yet.", Style.ACCENT)
        if not preview.ok:
            self._draw_problems(body, preview)
            return
        plan = preview.plan
        assert plan is not None
        info = self._info_lines(plan, preview, body.width - 4)
        table_rows = max(3, body.height - 4 - len(info))
        self._draw_table(body, plan, table_rows)
        used = min(len(plan.files), table_rows)  # keep the details right under a short table
        for offset, (text, style) in enumerate(info):
            body.put(3 + used + 1 + offset, 1, fit(text, body.width - 2), style)
        if self.confirming:
            self._draw_confirm(body, plan)

    def _draw_problems(self, body: Region, preview: Preview) -> None:
        body.put(2, 1, "This cannot run yet:", Style.BAD)
        problems = [f"{name}: {text}" for name, text in preview.errors.items()] + preview.problems
        row = 3
        for problem in problems:
            lines = wrap(safe_text(problem), body.width - 6)
            for index, line in enumerate(lines):
                body.put(row, 3, ("- " if index == 0 else "  ") + line, Style.BAD)
                row += 1
            if row >= body.height - 1:
                break
        body.put(min(row + 1, body.height - 1), 1, "Press Esc to go back and fix this.", Style.DIM)

    def _draw_table(self, body: Region, plan: BatchPlan, table_rows: int) -> None:
        sym = body.sym
        name_w = body.width - 2 - 3 * (COLUMN + 1)
        header_style = Style.DIM
        body.put(2, 1, fit("File", name_w), header_style)
        for index, title in enumerate(("Current", "Add", "Final")):
            body.put(
                2, 1 + name_w + 1 + index * (COLUMN + 1), fit(title, COLUMN, ">"), header_style
            )
        self.rows = table_rows
        self.scroll = max(0, min(self.scroll, len(plan.files) - table_rows))
        for offset, item in enumerate(plan.files[self.scroll : self.scroll + table_rows]):
            row = 3 + offset
            skipped = item.skip_reason is not None
            name = truncate_left(safe_text(tilde(item.path)), name_w)
            body.put(row, 1, fit(name, name_w), Style.DIM if skipped else Style.NORMAL)
            cur = fit(format_size(item.original_size), COLUMN, ">")
            add = fit("skipped" if skipped else "+" + format_size(item.add_bytes), COLUMN, ">")
            final = fit(format_size(item.final_size), COLUMN, ">")
            base = 1 + name_w + 1
            body.put(row, base, cur, Style.DIM)
            body.put(row, base + COLUMN + 1, add, Style.WARN if skipped else Style.GOOD)
            body.put(row, base + 2 * (COLUMN + 1), final, Style.NORMAL)
        if len(plan.files) > table_rows:
            end = min(len(plan.files), self.scroll + table_rows)
            note = f"{self.scroll + 1}-{end} of {len(plan.files)} {sym.up}{sym.down}"
            body.put(2, body.width - len(note) - 1, note, Style.DIM)

    def _info_lines(self, plan: BatchPlan, preview: Preview, width: int) -> list[tuple[str, Style]]:
        sym = self.app.canvas.sym
        options = plan.options
        lines: list[tuple[str, Style]] = [
            (f"Pattern      {options.pattern.describe()}", Style.NORMAL),
            (f"Performance  {options.settings.describe()}", Style.NORMAL),
            (
                "Options      "
                f"verify {'on' if options.verify else 'off'}, "
                f"backup {'on' if options.backup else 'off'}, "
                f"mark {'on' if options.mark else 'off'}, "
                f"size cap {format_size(options.max_add) if options.max_add else 'lifted'}",
                Style.NORMAL,
            ),
        ]
        recognized = {
            item.format.label for item in plan.writes if item.format.kind.value != "unknown"
        }
        if recognized:
            shown = ", ".join(sorted(recognized)[:3])
            more = f" (+{len(recognized) - 3} more)" if len(recognized) > 3 else ""
            lines.append((f"Format       recognized: {shown}{more}", Style.NORMAL))
        space = preview.space
        if space is not None and space.devices:
            device = space.devices[0]
            free = "unknown" if device.free is None else format_size(device.free)
            extra = f" (+{len(space.devices) - 1} more disks)" if len(space.devices) > 1 else ""
            verdict = (
                f"{sym.check} enough" if device.sufficient else f"{sym.cross} NOT ENOUGH SPACE"
            )
            lines.append(
                (
                    f"Disk space   needs ~{format_size(device.required)} while working, "
                    f"{free} free  {verdict}{extra}",
                    Style.GOOD if space.sufficient else Style.BAD,
                )
            )
        warnings = []
        for item in plan.files:
            for text in item.warnings:
                if text not in warnings:
                    warnings.append(text)
        for text in warnings[:2]:
            for index, line in enumerate(wrap(text, width)[:2]):
                lines.append((f"{sym.warn} {line}" if index == 0 else f"  {line}", Style.WARN))
        if len(warnings) > 2:
            lines.append((f"{sym.warn} ...and {len(warnings) - 2} more warning(s)", Style.WARN))
        if not plan.writes:
            lines.append(("Nothing to do: every file would be skipped.", Style.WARN))
        elif len(plan.writes) > 1:
            lines.append(
                (
                    "Files are committed one at a time; if a later file fails, earlier ones "
                    "stay modified.",
                    Style.DIM,
                )
            )
        return lines

    def _draw_confirm(self, body: Region, plan: BatchPlan) -> None:
        draw_box(
            body,
            [
                (f"This will write about {format_size(plan.total_add)} of new data", Style.BOLD),
                (f"to {len(plan.writes)} file(s), plus temporary staging copies.", Style.NORMAL),
                ("Free-space estimates cannot guarantee success;", Style.DIM),
                ("if it fails, originals are kept.", Style.DIM),
                ("", Style.NORMAL),
                ("Press Y to start, any other key to go back.", Style.ACCENT),
            ],
            title="Large operation",
        )

    # -- keys -------------------------------------------------------------------------

    def hints(self) -> list[tuple[str, str]]:
        sym = self.app.canvas.sym
        if self.confirming:
            return [("y", "start"), ("any other key", "go back")]
        hints = []
        if self.preview.can_run:
            hints.append(("Enter", "START"))
        hints += [("Esc", "back to settings"), (sym.up + sym.down, "scroll"), ("q", "quit")]
        return hints

    def on_key(self, key: str) -> None:
        app = self.app
        if self.confirming:
            self.confirming = False
            if key in ("y", "Y"):
                self._start()
            return
        if key in (ESC, "b", LEFT, BACKSPACE):
            app.goto("settings")
        elif key == UP:
            self.scroll = max(0, self.scroll - 1)
        elif key == DOWN:
            self.scroll += 1
        elif key == PGUP:
            self.scroll = max(0, self.scroll - self.rows)
        elif key == PGDN:
            self.scroll += self.rows
        elif key == "q":
            app.quit()
        elif key in (ENTER, "r"):
            self._request_start()

    def _request_start(self) -> None:
        preview, app = self.preview, self.app
        if not preview.ok:
            app.say("Fix the problems first (Esc goes back).", Style.BAD)
        elif preview.plan is None or not preview.plan.writes:
            app.say("Nothing to do: every file would be skipped.", Style.WARN)
        elif preview.space is not None and not preview.space.sufficient:
            app.say("Not enough free disk space for the temporary copy.", Style.BAD)
        elif preview.plan.total_add >= config.CONFIRM_THRESHOLD:
            self.confirming = True
        else:
            self._start()

    def _start(self) -> None:
        app, preview = self.app, self.preview
        assert preview.plan is not None
        app.last_plan, app.last_space = preview.plan, preview.space
        run = RunScreen(app, preview.plan)
        app.screen = run
        result = run.execute()
        app.record(result)
        app.screen = ResultScreen(app, result)
        app.screen.on_enter()


class RunScreen(Screen):
    """Live progress. The engine does the work; this screen only draws and listens for cancel."""

    step = 3

    PHASE_TEXT = {
        engine.PHASE_QUEUED: "queued",
        engine.PHASE_COPYING: "copying",
        engine.PHASE_APPENDING: "padding",
        engine.PHASE_VERIFYING: "verifying",
        engine.PHASE_COMMITTING: "committing",
        engine.PHASE_DONE: "done",
        engine.PHASE_SKIPPED: "skipped",
        engine.PHASE_FAILED: "failed",
        engine.PHASE_CANCELLED: "cancelled",
    }

    def __init__(self, app, plan: BatchPlan) -> None:
        super().__init__(app)
        self.plan = plan
        self.tracker = engine.ProgressTracker(plan)
        self.cancel = threading.Event()
        self.meter = RateMeter()
        self.rate: float | None = None
        self.started = time.monotonic()

    def execute(self) -> BatchResult:
        """Run the batch, redrawing about ten times a second. Returns when it has finished."""

        def on_tick() -> None:
            key = self.app.keys.poll()
            if key in (ESC, "c", "q") and not self.cancel.is_set():
                self.cancel.set()
            snapshot = self.tracker.snapshot()
            self.rate = self.meter.update(time.monotonic(), snapshot.done)
            self.app.draw()

        return engine.run_batch(
            self.plan,
            tracker=self.tracker,
            cancel=self.cancel,
            on_tick=on_tick,
            tick_interval=0.1,
        )

    # -- drawing ----------------------------------------------------------------------

    def draw(self, body: Region) -> None:
        sym = body.sym
        snapshot = self.tracker.snapshot()
        spin = sym.spinner[int((time.monotonic() - self.started) * 10) % len(sym.spinner)]
        if self.cancel.is_set():
            body.put(0, 1, f"{spin} Cancelling - removing temporary files...", Style.WARN)
        else:
            count = len(snapshot.files)
            body.put(0, 1, f"{spin} Padding {count} file{'s' if count != 1 else ''}", Style.ACCENT)

        total, done = snapshot.total, snapshot.done
        fraction = done / total if total else 1.0
        width = body.width - 9
        col = body.segments(2, 1, bar(sym, width, fraction))
        body.put(2, col + 1, f"{fraction * 100:5.1f}%", Style.BOLD)
        eta = estimate_eta(done, total, self.rate, snapshot.elapsed)
        body.put(
            3,
            1,
            f"{format_size(done)} of {format_size(total)}   {format_rate(self.rate)}   "
            f"elapsed {format_duration(snapshot.elapsed)}   ETA {format_duration(eta)}",
            Style.DIM,
        )

        rows = body.height - 6
        files = snapshot.files
        active = next((i for i, f in enumerate(files) if f.phase in self._live_phases()), 0)
        top = max(0, min(active - rows // 2, len(files) - rows))
        bar_w = 16
        name_w = max(8, body.width - 4 - 12 - bar_w - 8)
        for offset, item in enumerate(files[top : top + rows]):
            row = 5 + offset
            icon, style = self._icon(sym, item.phase, spin)
            body.put(row, 1, icon, style)
            name = truncate_left(safe_text(os.path.basename(item.path) or item.path), name_w)
            body.put(row, 3, fit(name, name_w), Style.NORMAL)
            phase = self.PHASE_TEXT.get(item.phase, item.phase)
            body.put(row, 4 + name_w, fit(phase, 11), style)
            if item.total:
                frac = item.done / item.total
                col = body.segments(row, 16 + name_w, bar(sym, bar_w, frac))
                body.put(row, col + 1, f"{frac * 100:3.0f}%", Style.DIM)

    @staticmethod
    def _live_phases() -> set[str]:
        return {
            engine.PHASE_COPYING,
            engine.PHASE_APPENDING,
            engine.PHASE_VERIFYING,
            engine.PHASE_COMMITTING,
        }

    @staticmethod
    def _icon(sym: Symbols, phase: str, spin: str) -> tuple[str, Style]:
        if phase == engine.PHASE_DONE:
            return sym.check, Style.GOOD
        if phase == engine.PHASE_FAILED:
            return sym.cross, Style.BAD
        if phase == engine.PHASE_CANCELLED:
            return sym.warn, Style.WARN
        if phase == engine.PHASE_SKIPPED:
            return sym.dash, Style.DIM
        if phase == engine.PHASE_QUEUED:
            return sym.bullet, Style.DIM
        return spin, Style.ACCENT

    def hints(self) -> list[tuple[str, str]]:
        return [] if self.cancel.is_set() else [("Esc", "cancel (originals stay untouched)")]

    def on_key(self, key: str) -> None:  # input is read by execute(); nothing to do here
        return


class ResultScreen(Screen):
    """What happened, per file."""

    step = 3

    def __init__(self, app, result: BatchResult) -> None:
        super().__init__(app)
        self.result = result
        self.scroll = 0
        self.rows = 5

    def on_enter(self) -> None:
        result = self.result
        if result.interrupted:
            self.app.say("Cancelled - unfinished files were left unchanged.", Style.WARN)
        elif result.failed:
            self.app.say("Some files failed - originals of those are unchanged.", Style.BAD)
        else:
            self.app.say("Done.", Style.GOOD)

    def _lines(self, width: int, sym: Symbols) -> list[tuple[str, Style]]:
        result = self.result
        lines: list[tuple[str, Style]] = []
        for item in result.results:
            name = safe_text(tilde(item.path))
            if item.status is FileStatus.SUCCESS:
                tail = (
                    f"{format_size(item.original_size)} {sym.arrow} "
                    f"{format_size(item.final_size or 0)}  (+{format_size(item.bytes_added)})"
                )
                if item.verified:
                    tail += "  verified"
                if item.backup_path:
                    tail += f"  backup: {os.path.basename(item.backup_path)}"
                lines.append((f"{sym.check} {name}", Style.GOOD))
                lines.append((f"    {tail}", Style.DIM))
            elif item.status is FileStatus.SKIPPED:
                lines.append((f"{sym.dash} {name}", Style.DIM))
                lines.append((f"    skipped: {item.error or 'nothing to do'}", Style.DIM))
            else:
                bad = item.status is FileStatus.FAILED
                lines.append(
                    (f"{sym.cross if bad else sym.warn} {name}", Style.BAD if bad else Style.WARN)
                )
                for text in wrap(safe_text(item.error or ""), max(10, width - 6)):
                    lines.append((f"    {text}", Style.BAD if bad else Style.WARN))
            for note in item.warnings:
                for text in wrap(note, max(10, width - 6)):
                    lines.append((f"    {sym.warn} {text}", Style.WARN))
        if result.leftover_temps:
            lines.append(("", Style.NORMAL))
            lines.append(
                ("These temporary files could not be removed - delete them manually:", Style.BAD)
            )
            lines.extend((f"    {safe_text(p)}", Style.BAD) for p in result.leftover_temps)
        if result.succeeded and (result.failed or result.cancelled):
            lines.append(("", Style.NORMAL))
            for text in wrap(
                f"Batch only partly completed: {result.succeeded} file(s) were already "
                "committed and are NOT rolled back.",
                width - 2,
            ):
                lines.append((text, Style.WARN))
        return lines

    def draw(self, body: Region) -> None:
        sym = body.sym
        result = self.result
        body.segments(
            0,
            1,
            [
                (f"{sym.check} {result.succeeded} succeeded   ", Style.GOOD),
                (f"{sym.dash} {result.skipped} skipped   ", Style.DIM),
                (
                    f"{sym.cross} {result.failed} failed   ",
                    Style.BAD if result.failed else Style.DIM,
                ),
                (
                    f"{sym.warn} {result.cancelled} cancelled",
                    Style.WARN if result.cancelled else Style.DIM,
                ),
            ],
        )
        body.put(
            1,
            1,
            f"{format_size_exact(result.total_added)} added in {format_duration(result.elapsed)}",
            Style.DIM,
        )
        body.hline(2)
        lines = self._lines(body.width - 2, sym)
        self.rows = max(1, body.height - 3)
        self.scroll = max(0, min(self.scroll, len(lines) - self.rows))
        for offset, (text, style) in enumerate(lines[self.scroll : self.scroll + self.rows]):
            body.put(3 + offset, 1, text, style)
        if len(lines) > self.rows:
            note = f"{sym.up}{sym.down} more"
            body.put(1, body.width - len(note) - 1, note, Style.DIM)

    def hints(self) -> list[tuple[str, str]]:
        return [("n", "new operation"), ("s", "save JSON report"), ("q", "quit")]

    def on_key(self, key: str) -> None:
        app = self.app
        if key == UP:
            self.scroll = max(0, self.scroll - 1)
        elif key == DOWN:
            self.scroll += 1
        elif key == PGUP:
            self.scroll = max(0, self.scroll - self.rows)
        elif key == PGDN:
            self.scroll += self.rows
        elif key == "n":
            app.browser.clear_selection()
            app.goto("files")
        elif key == "s":
            self._save_report()
        elif key in ("q", ESC, ENTER):
            app.quit()

    def _save_report(self) -> None:
        app = self.app
        if app.last_plan is None:
            return
        name = time.strftime("filepumper-report-%Y%m%d-%H%M%S.json")
        path = os.path.join(app.report_dir, name)
        report = reporting.build_report(app.last_plan, self.result, app.last_space, dry_run=False)
        try:
            reporting.write_report(path, report)
        except OSError as exc:
            app.say(f"Could not save the report: {exc.strerror or exc}", Style.BAD)
        else:
            app.say(f"Report saved: {tilde(path)}", Style.GOOD)
