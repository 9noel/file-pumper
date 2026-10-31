"""Pure-logic tests for the terminal UI: text helpers, canvas, form and file browser."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import is_root
from filepumper import config
from filepumper.config import UserConfig
from filepumper.exceptions import UsageError
from filepumper.sizes import KIB, MIB, parse_size
from filepumper.tui.browser import FileBrowser, Kind, list_directory, tilde
from filepumper.tui.canvas import ASCII, UNICODE, BufferCanvas, Style
from filepumper.tui.form import Form, TextInput, size_text
from filepumper.tui.keys import ScriptedKeys, is_text, text_of
from filepumper.tui.textutil import (
    char_width,
    display_width,
    fit,
    safe_text,
    truncate,
    truncate_left,
    wrap,
)

# ------------------------------------------------------------------------ text utils


def test_widths() -> None:
    assert char_width("a") == 1
    assert char_width("日") == 2
    assert char_width("́") == 0  # combining accent
    assert display_width("a日b") == 4


def test_truncate_respects_cells_not_characters() -> None:
    assert truncate("hello world", 8) == "hello w…"
    assert display_width(truncate("日本語日本語", 7)) <= 7
    assert truncate("abc", 10) == "abc"
    assert truncate("abc", 0) == ""
    assert truncate("hello", 3, "...") == "..."
    assert truncate("hello", 5, "") == "hello"


def test_truncate_left_keeps_the_end() -> None:
    out = truncate_left("/very/long/path/to/file.bin", 14)
    assert out.endswith("file.bin") and out.startswith("…")
    assert display_width(out) <= 14


@pytest.mark.parametrize(("align", "expected"), [("<", "ab   "), (">", "   ab"), ("^", " ab  ")])
def test_fit_alignment(align: str, expected: str) -> None:
    assert fit("ab", 5, align) == expected


def test_fit_truncates_and_pads_to_exact_width() -> None:
    assert display_width(fit("日本語日本語", 9)) == 9
    assert display_width(fit("x", 9)) == 9


def test_safe_text_removes_control_and_surrogates() -> None:
    assert safe_text("a\x00b\nc\x1b[0m") == "a?b?c?[0m"
    assert safe_text("bad\udcffname") == "bad?name"
    assert safe_text("ünï 日本") == "ünï 日本"


def test_wrap() -> None:
    assert wrap("hello world foo", 11) == ["hello world", "foo"]
    assert wrap("abcdefghij", 4) == ["abcd", "efgh", "ij"]
    assert wrap("", 5) == [""]
    assert wrap("a\nb", 5) == ["a", "b"]
    assert all(display_width(line) <= 10 for line in wrap("word " * 40, 10))


# ---------------------------------------------------------------------------- canvas


def test_buffer_canvas_draws_and_clips() -> None:
    canvas = BufferCanvas(10, 3)
    canvas.put(0, 0, "hello")
    canvas.put(1, 7, "overflowing")
    canvas.put(5, 0, "off-screen")
    canvas.put(2, -2, "abcd")
    assert canvas.line(0) == "hello"
    assert canvas.line(1) == "       ove"
    assert canvas.line(2) == "cd"
    assert canvas.find("hello") == (0, 0)
    assert canvas.find("nope") is None


def test_buffer_canvas_wide_characters_take_two_cells() -> None:
    canvas = BufferCanvas(6, 1)
    canvas.put(0, 0, "日本", Style.GOOD)
    canvas.put(0, 4, "x")
    assert canvas.line(0) == "日本x"
    assert canvas.style_at(0, 1) is Style.GOOD
    canvas.put(0, 5, "日")  # does not fit in the last cell: dropped, not split
    assert canvas.line(0) == "日本x"


def test_buffer_canvas_records_styles_and_refreshes() -> None:
    canvas = BufferCanvas(5, 1)
    canvas.put(0, 1, "ab", Style.BAD)
    assert canvas.style_at(0, 0) is Style.NORMAL and canvas.style_at(0, 1) is Style.BAD
    canvas.refresh()
    assert canvas.refreshes == 1
    canvas.resize(8, 2)
    assert canvas.size() == (8, 2) and canvas.text() == ""


def test_symbol_sets_are_complete_and_ascii_is_ascii() -> None:
    from dataclasses import fields

    for name in (f.name for f in fields(ASCII)):
        assert getattr(ASCII, name).isascii(), name
        assert getattr(UNICODE, name) != "" or name == "bar_partial"


# ------------------------------------------------------------------------------ keys


def test_key_helpers() -> None:
    assert is_text("a") and is_text("SPACE") and is_text("é")
    assert not is_text("ENTER") and not is_text("UP")
    assert text_of("SPACE") == " " and text_of("x") == "x"


def test_scripted_keys_separates_run_keys() -> None:
    keys = ScriptedKeys(["a", "b"], run_keys=["ESC"])
    assert keys.poll() == "ESC" and keys.poll() is None
    assert keys.get() == "a" and keys.remaining == 1


# ------------------------------------------------------------------------- TextInput


def test_text_input_editing() -> None:
    field = TextInput("abc")
    assert field.cursor == 3
    field.handle("d")
    field.handle("LEFT")
    field.handle("LEFT")
    field.handle("X")
    assert field.value == "abXcd" and field.cursor == 3
    field.handle("BACKSPACE")
    assert field.value == "abcd"
    field.handle("DELETE")
    assert field.value == "abd"
    field.handle("HOME")
    field.handle("BACKSPACE")  # nothing before the cursor
    assert field.value == "abd" and field.cursor == 0
    field.handle("END")
    field.handle("SPACE")
    assert field.value == "abd "


def test_text_input_ignores_other_keys_and_clamps_cursor() -> None:
    field = TextInput("x")
    assert field.handle("UP") is False and field.handle("ENTER") is False
    field.handle("RIGHT")
    field.handle("RIGHT")
    assert field.cursor == 1
    field.set("hello")
    assert field.cursor == 5
    assert TextInput("ab", cursor=99).cursor == 2


# ---------------------------------------------------------------------------- Form


def test_default_form_is_valid_and_matches_cli_defaults() -> None:
    form = Form()
    assert form.errors() == {}
    options = form.to_options()
    assert options.size == 10 * MIB and options.mode.value == "add"
    assert options.max_add == config.DEFAULT_MAX_ADD
    assert options.settings.jobs == 1 and not options.verify and not options.backup


@pytest.mark.parametrize("text", ["", "10", "-5MB", "5XB", "inf", "1e3MB", "0.5B"])
def test_invalid_sizes_are_reported(text: str) -> None:
    form = Form()
    form.size.set(text)
    assert "size" in form.errors()
    assert form.size_bytes() is None
    with pytest.raises(UsageError):
        form.to_options()


def test_hex_is_only_checked_for_the_repeat_pattern() -> None:
    form = Form()
    form.repeat_hex.set("ZZ")
    assert form.errors() == {}
    form.pattern = "repeat"
    assert "hex" in form.errors()
    form.repeat_hex.set("0xCAFE")
    assert form.errors() == {}
    assert form.to_options().pattern.pattern == bytes.fromhex("CAFE")  # type: ignore[attr-defined]


def test_zero_pattern_ignores_the_hex_field() -> None:
    form = Form()
    form.repeat_hex.set("not hex")
    assert form.to_options().pattern.kind.value == "zero"


@pytest.mark.parametrize(
    ("text", "valid"),
    [("", True), ("4KiB", True), ("64MiB", True), ("4095B", False), ("65MiB", False), ("x", False)],
)
def test_chunk_validation(text: str, valid: bool) -> None:
    form = Form()
    form.chunk.set(text)
    assert (form.errors() == {}) is valid


@pytest.mark.parametrize(
    ("text", "valid"),
    [
        ("", True),
        ("1", True),
        ("32", True),
        ("0", False),
        ("33", False),
        ("two", False),
        ("1.5", False),
    ],
)
def test_jobs_validation(text: str, valid: bool) -> None:
    form = Form()
    form.jobs.set(text)
    assert (form.errors() == {}) is valid


def test_overrides_reach_the_options() -> None:
    form = Form(mode="target", pattern="random", preset="fast", verify=True, backup=True)
    form.size.set("1GiB")
    form.chunk.set("2MiB")
    form.jobs.set("2")
    form.follow_symlinks = True
    form.allow_hardlinks = True
    options = form.to_options()
    assert options.mode.value == "target" and options.size == 1024**3
    assert options.settings.chunk_size == 2 * MIB and options.settings.jobs == 2
    assert options.verify and options.backup and options.follow_symlinks and options.allow_hardlinks


def test_allow_large_lifts_the_cap_and_config_cap_applies() -> None:
    form = Form()
    assert form.to_options().max_add == config.DEFAULT_MAX_ADD
    form.max_add = 5 * MIB
    assert form.to_options().max_add == 5 * MIB
    form.allow_large = True
    assert form.to_options().max_add is None


def test_form_from_config_prefills() -> None:
    user = UserConfig(
        preset="fast", chunk_size=4 * MIB, jobs=2, pattern="repeat", repeat_hex="AB",
        verify=True, max_add=2048,
    )  # fmt: skip
    form = Form.from_config(user)
    assert (form.preset, form.pattern, form.verify, form.max_add) == ("fast", "repeat", True, 2048)
    assert form.chunk.value == "4MiB" and form.jobs.value == "2" and form.repeat_hex.value == "AB"
    assert form.errors() == {}


def test_form_signature_changes_with_every_field() -> None:
    form = Form()
    seen = {form.signature()}
    form.size.set("1KB")
    seen.add(form.signature())
    form.verify = True
    seen.add(form.signature())
    form.pattern = "random"
    seen.add(form.signature())
    assert len(seen) == 4


@pytest.mark.parametrize(
    "value", [0, 1, 512, 1023, 1024, 1536, 2 * KIB, MIB, 3 * MIB, 5 * 1024**3 + 7]
)
def test_size_text_round_trips(value: int) -> None:
    assert parse_size(size_text(value)) == value


# ---------------------------------------------------------------------- FileBrowser


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "A_dir").mkdir()
    (tmp_path / "z.txt").write_bytes(b"z" * 10)
    (tmp_path / "a.txt").write_bytes(b"a" * 5)
    (tmp_path / "Big.bin").write_bytes(b"b" * 2000)
    (tmp_path / ".hidden").write_bytes(b"h")
    (tmp_path / "b_dir" / "inner.bin").write_bytes(b"i" * 7)
    return tmp_path


def names(browser: FileBrowser) -> list[str]:
    return [entry.name for entry in browser.entries]


def test_listing_order_folders_first_case_insensitive(tree: Path) -> None:
    browser = FileBrowser(tree)
    assert names(browser) == ["..", "A_dir", "b_dir", "a.txt", "Big.bin", "z.txt"]
    assert browser.entries[0].kind is Kind.PARENT
    assert browser.entries[3].size == 5


def test_hidden_files_toggle(tree: Path) -> None:
    browser = FileBrowser(tree)
    assert ".hidden" not in names(browser)
    browser.toggle_hidden()
    assert ".hidden" in names(browser)
    browser.toggle_hidden()
    assert ".hidden" not in names(browser)


def test_filter_is_case_insensitive_and_keeps_parent(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.set_filter("TXT")
    assert names(browser) == ["..", "a.txt", "z.txt"]
    browser.set_filter("")
    assert len(browser.entries) == 6


def test_navigation_into_and_out_of_folders(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.move(2)  # b_dir
    assert browser.current.name == "b_dir"
    assert browser.open_current() is None
    assert browser.cwd == str(tree / "b_dir") and "inner.bin" in names(browser)
    browser.go_up()
    assert browser.cwd == str(tree)
    assert browser.current.name == "b_dir"  # cursor returns to the folder we came from


def test_open_parent_entry_goes_up(tree: Path) -> None:
    browser = FileBrowser(tree / "b_dir")
    assert browser.current.kind is Kind.PARENT
    browser.open_current()
    assert browser.cwd == str(tree)


def test_cursor_is_clamped_and_scrolls(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.move(-5)
    assert browser.cursor == 0
    browser.move(99)
    assert browser.cursor == len(browser.entries) - 1
    browser.ensure_visible(3)
    assert browser.top == browser.cursor - 2
    browser.home()
    browser.ensure_visible(3)
    assert browser.top == 0
    browser.end()
    assert browser.cursor == 5


def test_selection_toggle_and_order(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.cursor = 5  # z.txt
    assert browser.toggle_current() is None
    browser.cursor = 3  # a.txt
    browser.toggle_current()
    assert browser.selection == [str(tree / "z.txt"), str(tree / "a.txt")]  # in selection order
    assert browser.is_selected(str(tree / "a.txt"))
    browser.toggle_current()
    assert browser.selection == [str(tree / "z.txt")]
    assert browser.selected_bytes() == 10


def test_folders_cannot_be_selected(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.cursor = 1
    assert "folders cannot be selected" in (browser.toggle_current() or "")
    browser.cursor = 0
    assert "folders cannot be selected" in (browser.toggle_current() or "")
    assert browser.selection == []


def test_select_all_visible_and_clear(tree: Path) -> None:
    browser = FileBrowser(tree)
    assert browser.select_all_visible() == 3
    assert browser.select_all_visible() == 0
    browser.remove(str(tree / "a.txt"))
    assert len(browser.selection) == 2
    browser.clear_selection()
    assert browser.selection == []


def test_select_all_respects_the_filter(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.set_filter(".txt")
    assert browser.select_all_visible() == 2
    assert all(path.endswith(".txt") for path in browser.selection)


def test_selection_survives_changing_folder(tree: Path) -> None:
    browser = FileBrowser(tree)
    browser.cursor = 3
    browser.toggle_current()
    browser.cursor = 2
    browser.open_current()
    browser.cursor = 1  # inner.bin
    browser.toggle_current()
    assert len(browser.selection) == 2


def test_preselected_paths(tree: Path) -> None:
    browser = FileBrowser(tree, selected=[str(tree / "a.txt")])
    assert browser.is_selected(str(tree / "a.txt"))


def test_goto_folder_file_and_errors(tree: Path) -> None:
    browser = FileBrowser(tree)
    assert browser.goto("b_dir") is None and browser.cwd == str(tree / "b_dir")
    assert browser.goto("..") is None and browser.cwd == str(tree)
    assert browser.goto("a.txt") is None
    assert browser.selection == [str(tree / "a.txt")] and browser.current.name == "a.txt"
    assert "no such file" in (browser.goto("missing.bin") or "")
    assert browser.goto("   ") is None
    assert browser.goto(str(tree / "b_dir" / "inner.bin")) is None
    assert browser.cwd == str(tree / "b_dir") and len(browser.selection) == 2


def test_goto_expands_home(monkeypatch: pytest.MonkeyPatch, tree: Path) -> None:
    monkeypatch.setenv("HOME", str(tree))
    monkeypatch.setenv("USERPROFILE", str(tree))
    browser = FileBrowser(tree / "b_dir")
    assert browser.goto("~") is None and browser.cwd == str(tree)
    assert tilde(str(tree / "a.txt")) == "~" + os.sep + "a.txt"


def test_symlinks_are_described(tree: Path) -> None:
    try:
        os.symlink(tree / "a.txt", tree / "link_to_file")
        os.symlink(tree / "b_dir", tree / "link_to_dir")
        os.symlink(tree / "nowhere", tree / "dangling")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links unavailable")
    entries = {e.name: e for e in FileBrowser(tree).entries}
    assert entries["link_to_file"].kind is Kind.LINK and entries["link_to_file"].size == 5
    assert entries["link_to_file"].selectable
    assert entries["link_to_dir"].kind is Kind.LINK_DIR and entries["link_to_dir"].is_dir_like
    assert entries["dangling"].kind is Kind.BROKEN and not entries["dangling"].selectable


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX FIFO")
def test_special_files_are_not_selectable(tree: Path) -> None:
    os.mkfifo(tree / "pipe")
    entry = next(e for e in FileBrowser(tree).entries if e.name == "pipe")
    assert entry.kind is Kind.SPECIAL and not entry.selectable


@pytest.mark.skipif(os.name == "nt" or is_root(), reason="needs permissions and a non-root user")
def test_unreadable_folder_reports_an_error(tree: Path) -> None:
    locked = tree / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        entries, error = list_directory(str(locked))
        assert error and "cannot read" in error
        assert [e.kind for e in entries] == [Kind.PARENT]
        browser = FileBrowser(tree)
        browser.cursor = next(i for i, e in enumerate(browser.entries) if e.name == "locked")
        assert "permission denied" in (browser.open_current() or "")
    finally:
        locked.chmod(0o755)


def test_names_with_odd_characters(tree: Path) -> None:
    (tree / "sp ace & ünï 日本.bin").write_bytes(b"x")
    assert "sp ace & ünï 日本.bin" in names(FileBrowser(tree))


def test_empty_folder_has_only_the_parent_entry(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    browser = FileBrowser(tmp_path / "empty")
    assert names(browser) == [".."]
    assert browser.toggle_current() is not None
    assert browser.select_all_visible() == 0
