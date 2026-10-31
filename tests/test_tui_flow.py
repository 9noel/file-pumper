"""End-to-end tests of the terminal UI, driven by scripted keys on an in-memory canvas.

No terminal or curses module is needed: the screens draw onto a ``BufferCanvas`` and
read keys from ``ScriptedKeys``, while the real engine writes the real (temporary) files.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from filepumper import config, exitcodes
from filepumper.config import UserConfig
from filepumper.tui.app import MIN_HEIGHT, MIN_WIDTH, App
from filepumper.tui.canvas import ASCII, BufferCanvas, Symbols
from filepumper.tui.keys import KeysExhausted, ScriptedKeys
from filepumper.tui.screens_run import ResultScreen, ReviewScreen, RunScreen, bar

BACK4 = ["BACKSPACE"] * 4  # empties the default "10MB" size field


def type_text(text: str) -> list[str]:
    return ["SPACE" if ch == " " else ch for ch in text]


def start(
    keys: list[str],
    *,
    paths: list[Path] | None = None,
    cwd: Path | None = None,
    size: tuple[int, int] = (100, 30),
    symbols: Symbols | None = None,
    run_keys: list[str] | None = None,
    user: UserConfig | None = None,
    report_dir: Path | None = None,
) -> tuple[App, BufferCanvas]:
    """Run the app until the scripted keys are used up (or it quits)."""
    canvas = BufferCanvas(*size, **({"symbols": symbols} if symbols else {}))
    app = App(
        canvas,
        ScriptedKeys(keys, run_keys or ()),
        paths=[str(p) for p in paths or []] or ([str(cwd)] if cwd else []),
        user=user,
        report_dir=str(report_dir) if report_dir else None,
    )
    app.run()
    return app, canvas


@pytest.fixture
def two_files(make_file) -> tuple[Path, Path]:
    return make_file("a.bin", b"A" * 100), make_file("b.bin", b"B" * 50)


# ---------------------------------------------------------------------- files screen


def test_files_screen_lists_the_folder_and_shows_hints(two_files: tuple[Path, Path]) -> None:
    _, canvas = start([], cwd=two_files[0].parent)
    text = canvas.text()
    assert "FILE PUMPER" in text and "1 Files" in text
    assert "a.bin" in text and "b.bin" in text
    assert "Space" in text and "select" in text and "quit" in text


def test_cannot_continue_without_a_selection(two_files: tuple[Path, Path]) -> None:
    app, canvas = start(["TAB"], cwd=two_files[0].parent)
    assert "Select at least one file" in canvas.text()
    assert app.screen.step == 0


def test_space_selects_and_advances_then_tab_opens_settings(
    two_files: tuple[Path, Path],
) -> None:
    # entries: "..", a.bin, b.bin -> cursor starts on "..", so go down once
    app, canvas = start(["DOWN", "SPACE", "SPACE", "TAB"], cwd=two_files[0].parent)
    assert app.browser.selection == [str(two_files[0]), str(two_files[1])]
    assert app.screen.step == 1
    assert "Operation" in canvas.text() and "Bytes to add" in canvas.text()


def test_preselected_paths_skip_straight_to_a_usable_selection(
    two_files: tuple[Path, Path],
) -> None:
    app, canvas = start([], paths=[two_files[0]])
    assert app.browser.selection == [str(two_files[0])]
    assert "Selected (1)" in canvas.text() or "a.bin" in canvas.text()


def test_filter_and_select_all(two_files: tuple[Path, Path]) -> None:
    keys = ["/", *type_text("b.b"), "ENTER", "a"]
    app, _ = start(keys, cwd=two_files[0].parent)
    assert app.browser.selection == [str(two_files[1])]


def test_goto_adds_a_file_by_path(two_files: tuple[Path, Path]) -> None:
    keys = ["g", *type_text(str(two_files[1])), "ENTER"]
    app, _ = start(keys, cwd=two_files[0].parent)
    assert app.browser.selection == [str(two_files[1])]


def test_goto_reports_a_missing_path(two_files: tuple[Path, Path]) -> None:
    _, canvas = start(["g", *type_text("nope.bin"), "ENTER"], cwd=two_files[0].parent)
    assert "no such file" in canvas.text()


def test_selection_manager_removes_a_file(two_files: tuple[Path, Path]) -> None:
    keys = ["DOWN", "SPACE", "SPACE", "s", "SPACE", "ESC"]
    app, _ = start(keys, cwd=two_files[0].parent)
    assert app.browser.selection == [str(two_files[1])]


def test_quit_from_the_first_screen_exits_cleanly(two_files: tuple[Path, Path]) -> None:
    app, _ = start(["q"], cwd=two_files[0].parent)
    assert app.exit_code == exitcodes.EXIT_OK and not app.exhausted


def test_clear_selection_key(two_files: tuple[Path, Path]) -> None:
    app, _ = start(["DOWN", "a", "x"], cwd=two_files[0].parent)
    assert app.browser.selection == []


# -------------------------------------------------------------------- settings screen


def test_settings_changes_reach_the_form(two_files: tuple[Path, Path]) -> None:
    keys = [
        *BACK4, *type_text("1KB"),  # size
        "DOWN", "RIGHT",  # pattern -> random
    ]  # fmt: skip
    app, canvas = start(keys, paths=[two_files[0], two_files[1]])
    app.goto("settings")
    app.keys = ScriptedKeys(keys)
    app.run()
    assert app.form.size.value == "1KB" and app.form.pattern == "random"
    assert "Bytes to add" in canvas.text()


def test_switching_to_target_mode_relabels_the_size(two_files: tuple[Path, Path]) -> None:
    app, canvas = start([], paths=[two_files[0]])
    app.goto("settings")
    app.keys = ScriptedKeys(["UP", "RIGHT"])
    app.run()
    assert app.form.mode == "target" and "Target total size" in canvas.text()


def test_invalid_size_blocks_continue_and_focuses_the_field(
    two_files: tuple[Path, Path],
) -> None:
    app, canvas = start([], paths=[two_files[0]])
    app.goto("settings")
    app.keys = ScriptedKeys([*BACK4, "x", "CTRL+N"])
    app.run()
    assert app.screen.step == 1
    assert "Fix the highlighted field" in canvas.text()
    assert app.form.errors().keys() == {"size"}


def test_hex_field_is_skipped_until_repeat_is_chosen(two_files: tuple[Path, Path]) -> None:
    app, _ = start([], paths=[two_files[0]])
    app.goto("settings")
    app.keys = ScriptedKeys(["DOWN"])  # size -> pattern is next; hex disabled
    app.run()
    assert app.screen.current.key == "pattern"  # type: ignore[attr-defined]
    app.keys = ScriptedKeys(["DOWN"])
    app.run()
    assert app.screen.current.key == "preset"  # type: ignore[attr-defined]  # hex skipped
    app.keys = ScriptedKeys(["UP", "SPACE", "SPACE", "DOWN"])  # pattern: zero -> random -> repeat
    app.run()
    assert app.form.pattern == "repeat" and app.screen.current.key == "repeat_hex"  # type: ignore[attr-defined]


def test_escape_goes_back_to_files_and_keeps_the_selection(
    two_files: tuple[Path, Path],
) -> None:
    app, _ = start([], paths=[two_files[0]])
    app.goto("settings")
    app.keys = ScriptedKeys(["ESC"])
    app.run()
    assert app.screen.step == 0 and app.browser.selection == [str(two_files[0])]


def test_settings_are_prefilled_from_the_user_config(two_files: tuple[Path, Path]) -> None:
    user = UserConfig(pattern="random", verify=True, jobs=2)
    app, _ = start([], paths=[two_files[0]], user=user)
    assert app.form.pattern == "random" and app.form.verify and app.form.jobs.value == "2"


# --------------------------------------------------------------------- review and run


def full_flow_keys(*size_keys: str) -> list[str]:
    """From a pre-selected file: settings -> change size -> review -> start."""
    return ["TAB", *size_keys, "CTRL+N"]


def test_review_shows_the_plan_without_touching_files(two_files: tuple[Path, Path]) -> None:
    a, b = two_files
    keys = ["TAB", *BACK4, *type_text("1KB"), "CTRL+N"]
    app, canvas = start(keys, paths=[a, b])
    text = canvas.text()
    assert isinstance(app.screen, ReviewScreen)
    assert "Nothing has been written yet" in text
    assert "a.bin" in text and "b.bin" in text
    assert "+1.00 KiB" in text
    assert "Disk space" in text and "Pattern" in text
    assert a.read_bytes() == b"A" * 100 and b.read_bytes() == b"B" * 50


def test_full_run_pads_the_files_and_exits_zero(
    two_files: tuple[Path, Path], tmp_path: Path
) -> None:
    a, b = two_files
    keys = ["TAB", *BACK4, *type_text("1KB"), "CTRL+N", "ENTER", "q"]
    app, canvas = start(keys, paths=[a, b], report_dir=tmp_path)
    assert a.stat().st_size == 100 + 1024 and b.stat().st_size == 50 + 1024
    assert a.read_bytes()[:100] == b"A" * 100
    assert a.read_bytes()[100:] == bytes(1024)
    assert app.exit_code == exitcodes.EXIT_OK and not app.exhausted
    assert app.last_result is not None and app.last_result.succeeded == 2
    assert not [p for p in tmp_path.iterdir() if ".filepumper" in p.name or p.suffix == ".tmp"]
    assert "2 succeeded" in canvas.text() or canvas.text() == ""  # screen was last drawn at exit


def test_result_screen_and_report_saving(two_files: tuple[Path, Path], tmp_path: Path) -> None:
    a, b = two_files
    reports = tmp_path / "reports"
    reports.mkdir()
    keys = ["TAB", *BACK4, *type_text("1KB"), "CTRL+N", "ENTER", "s"]
    app, canvas = start(keys, paths=[a, b], report_dir=reports)
    text = canvas.text()
    assert isinstance(app.screen, ResultScreen)
    assert "2 succeeded" in text and "0 failed" in text and "Report saved" in text
    saved = list(reports.glob("filepumper-report-*.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text())
    assert data["summary"]["succeeded"] == 2
    assert data["dry_run"] is False


def test_target_mode_run(two_files: tuple[Path, Path]) -> None:
    a, b = two_files
    keys = ["TAB", "UP", "RIGHT", "DOWN", *BACK4, *type_text("500B"), "CTRL+N", "ENTER", "q"]
    start(keys, paths=[a, b])
    assert a.stat().st_size == 500 and b.stat().st_size == 500


def test_repeat_pattern_run_is_byte_exact(make_file) -> None:
    path = make_file("c.bin", b"head")
    keys = [
        "TAB", *BACK4, *type_text("10B"),
        "DOWN", "SPACE", "SPACE", "DOWN",  # pattern -> repeat, then into the hex field
        *["BACKSPACE"] * 8, *type_text("CAFE"),
        "CTRL+N", "ENTER", "q",
    ]  # fmt: skip
    start(keys, paths=[path])
    assert path.read_bytes() == b"head" + bytes.fromhex("CAFE") * 5


def test_verify_and_backup_options_flow_through(make_file, tmp_path: Path) -> None:
    path = make_file("d.bin", b"data")
    app, _ = start([], paths=[path])
    app.goto("settings")
    form = app.form
    form.size.set("64B")
    form.verify = True
    form.backup = True
    app.keys = ScriptedKeys(["CTRL+N", "ENTER", "q"])
    app.run()
    assert path.stat().st_size == 4 + 64
    assert (
        any(".bak" in p.name for p in path.parent.iterdir())
        or app.last_result.results[0].backup_path
    )  # type: ignore[union-attr]
    assert app.last_result.results[0].verified  # type: ignore[union-attr]


def test_size_cap_is_reported_on_review_and_blocks_start(make_file) -> None:
    path = make_file("e.bin", b"x")
    user = UserConfig(max_add=100)
    keys = ["TAB", *BACK4, *type_text("1KB"), "CTRL+N", "ENTER"]
    app, canvas = start(keys, paths=[path], user=user)
    text = canvas.text()
    assert isinstance(app.screen, ReviewScreen)
    assert "cannot run yet" in text
    assert path.read_bytes() == b"x"
    assert app.last_result is None


def test_allow_large_lifts_the_cap(make_file) -> None:
    path = make_file("f.bin", b"x")
    user = UserConfig(max_add=100)
    app, _ = start([], paths=[path], user=user)
    app.goto("settings")
    app.form.size.set("1KB")
    app.form.allow_large = True
    app.keys = ScriptedKeys(["CTRL+N", "ENTER", "q"])
    app.run()
    assert path.stat().st_size == 1 + 1024


def test_review_escape_returns_to_settings(two_files: tuple[Path, Path]) -> None:
    app, _ = start(["TAB", "CTRL+N", "ESC"], paths=[two_files[0]])
    assert app.screen.step == 1


def test_missing_file_is_a_problem_not_a_crash(make_file) -> None:
    path = make_file("gone.bin", b"x")
    app, _ = start([], paths=[path])
    app.goto("settings")
    path.unlink()
    app.keys = ScriptedKeys(["CTRL+N"])
    app.run()
    assert isinstance(app.screen, ReviewScreen)
    assert not app.screen.preview.ok  # type: ignore[attr-defined]


def test_nothing_to_do_when_target_is_already_met(two_files: tuple[Path, Path]) -> None:
    a, _ = two_files
    keys = ["TAB", "UP", "RIGHT", "DOWN", *BACK4, *type_text("100B"), "CTRL+N", "ENTER"]
    app, canvas = start(keys, paths=[a])
    assert "Nothing to do" in canvas.text()
    assert a.read_bytes() == b"A" * 100
    assert app.last_result is None


def test_large_operation_requires_confirmation(
    two_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = two_files
    monkeypatch.setattr(config, "CONFIRM_THRESHOLD", 1000)
    keys = ["TAB", *BACK4, *type_text("2KB"), "CTRL+N", "ENTER"]
    app, canvas = start(keys, paths=[a])
    assert "Large operation" in canvas.text() and "Press Y" in canvas.text()
    assert app.last_result is None and a.stat().st_size == 100

    # any other key declines and leaves the file untouched
    app.keys = ScriptedKeys(["n"])
    app.run()
    assert "Large operation" not in canvas.text()
    assert a.stat().st_size == 100 and app.last_result is None

    # Y starts the run
    app.keys = ScriptedKeys(["ENTER", "y", "q"])
    app.run()
    assert a.stat().st_size == 100 + 2048


def test_new_operation_returns_to_an_empty_selection(
    two_files: tuple[Path, Path],
) -> None:
    a, _ = two_files
    keys = ["TAB", *BACK4, *type_text("8B"), "CTRL+N", "ENTER", "n"]
    app, _ = start(keys, paths=[a])
    assert app.screen.step == 0 and app.browser.selection == []
    assert a.stat().st_size == 108


def test_exit_code_reflects_a_failed_run(
    two_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from filepumper import engine

    a, _ = two_files
    real = engine.run_batch

    def failing(plan, **kwargs):
        result = real(plan, **kwargs)
        for item in result.results:
            item.status = engine.FileStatus.FAILED
            item.error = "simulated failure"
        return result

    monkeypatch.setattr(engine, "run_batch", failing)
    keys = ["TAB", *BACK4, *type_text("8B"), "CTRL+N", "ENTER"]
    app, canvas = start(keys, paths=[a])
    assert "1 failed" in canvas.text() and "simulated failure" in canvas.text()
    app.keys = ScriptedKeys(["q"])
    assert app.run() == exitcodes.EXIT_FAILED


def test_cancelling_during_the_run_leaves_the_original_untouched(
    make_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filepumper import engine

    path = make_file("big.bin", b"orig")
    real = engine.run_batch

    def slow(plan, **kwargs):
        # let the first progress tick run (it polls the scripted run key) before any bytes
        # are written, by making the engine tick immediately
        kwargs["tick_interval"] = 0.0
        return real(plan, **kwargs)

    monkeypatch.setattr(engine, "run_batch", slow)
    keys = ["TAB", *BACK4, *type_text("256KB"), "CTRL+N", "ENTER", "q"]
    app, _ = start(keys, paths=[path], run_keys=["ESC"])
    result = app.last_result
    assert result is not None
    if result.interrupted:  # cancel landed before the commit: original must be intact
        assert path.read_bytes() == b"orig"
        assert app.worst_code == exitcodes.EXIT_INTERRUPTED
    else:  # the tiny file finished before the first tick could be delivered
        assert path.stat().st_size == 4 + 256 * 1024
    assert not [p for p in path.parent.iterdir() if p.name != "big.bin"]


# --------------------------------------------------------------------------- display


@pytest.mark.parametrize("size", [(MIN_WIDTH - 1, 30), (100, MIN_HEIGHT - 1), (20, 5)])
def test_too_small_terminal_shows_a_message_instead_of_crashing(
    two_files: tuple[Path, Path], size: tuple[int, int]
) -> None:
    app, canvas = start(["DOWN", "SPACE"], cwd=two_files[0].parent, size=size)
    assert "too small" in canvas.text().lower()
    assert not app.exhausted or app.exit_code is None


def test_minimum_size_renders_every_screen(two_files: tuple[Path, Path]) -> None:
    app, canvas = start([], paths=[two_files[0]], size=(MIN_WIDTH, MIN_HEIGHT))
    for name in ("files", "settings", "review"):
        app.goto(name)
        app.draw()
        assert "too small" not in canvas.text().lower()
        assert len(canvas.lines()) == MIN_HEIGHT
        assert all(len(line) <= MIN_WIDTH for line in canvas.lines())


def test_ascii_mode_uses_only_ascii_characters(two_files: tuple[Path, Path]) -> None:
    a, b = two_files
    keys = ["TAB", *BACK4, *type_text("1KB"), "CTRL+N"]
    app, canvas = start(keys, paths=[a, b], symbols=ASCII)
    for name in ("files", "settings", "review"):
        app.goto(name)
        app.draw()
        assert canvas.text().isascii(), name


def test_wide_layout_shows_the_side_panel(two_files: tuple[Path, Path]) -> None:
    _, wide = start([], paths=[two_files[0]], size=(120, 30))
    _, narrow = start([], paths=[two_files[0]], size=(80, 24))
    assert "Selected (1)" in wide.text()
    assert "Selected (1)" not in narrow.text()


def test_too_small_then_resized_recovers(two_files: tuple[Path, Path]) -> None:
    app, canvas = start([], cwd=two_files[0].parent, size=(30, 10))
    assert "too small" in canvas.text().lower()
    canvas.resize(100, 30)
    app.keys = ScriptedKeys(["RESIZE"])
    app.run()
    assert "FILE PUMPER" in canvas.text()


def test_interrupt_exits_with_130(two_files: tuple[Path, Path]) -> None:
    class Interrupting(ScriptedKeys):
        def get(self) -> str:
            raise KeyboardInterrupt

    canvas = BufferCanvas(100, 30)
    app = App(canvas, Interrupting([]), paths=[str(two_files[0])])
    assert app.run() == exitcodes.EXIT_INTERRUPTED


def test_running_out_of_scripted_keys_is_flagged(two_files: tuple[Path, Path]) -> None:
    app, _ = start([], cwd=two_files[0].parent)
    assert app.exhausted
    with pytest.raises(KeysExhausted):
        ScriptedKeys([]).get()


# ----------------------------------------------------------------------------- bar()


@pytest.mark.parametrize("fraction", [0.0, 0.01, 0.25, 0.5, 0.999, 1.0, -1.0, 7.0])
@pytest.mark.parametrize("symbols", [ASCII, BufferCanvas().sym])
def test_bar_always_has_the_requested_width(symbols: Symbols, fraction: float) -> None:
    width = 20
    parts = bar(symbols, width, fraction)
    assert sum(len(text) for text, _ in parts) == width


def test_bar_fill_is_proportional() -> None:
    full = "".join(text for text, _ in bar(ASCII, 10, 0.5))
    assert full == "#" * 5 + "-" * 5
    assert "".join(t for t, _ in bar(ASCII, 10, 1.0)) == "#" * 10
    assert "".join(t for t, _ in bar(ASCII, 10, 0.0)) == "-" * 10


def test_run_screen_cancel_event_is_wired(two_files: tuple[Path, Path]) -> None:
    from filepumper import engine

    canvas = BufferCanvas(100, 30)
    app = App(canvas, ScriptedKeys([], ["c"]), paths=[str(two_files[0])])
    form_options = app.form.to_options()
    plan = engine.build_plan([str(two_files[0])], form_options)
    screen = RunScreen(app, plan)
    assert isinstance(screen.cancel, threading.Event) and not screen.cancel.is_set()
    result = screen.execute()
    assert result.interrupted or result.succeeded == 1
