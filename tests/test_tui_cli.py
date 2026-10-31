"""How the terminal UI is reached from the command line, plus a real-terminal smoke test."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from filepumper import __version__, cli, exitcodes, tui
from filepumper.cli import main
from filepumper.tui import textutil

SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_ui_is_listed_in_help(capsys) -> None:
    code, out, _ = run_cli(capsys, "--help")
    assert code == 0 and "ui" in out


def test_ui_help(capsys) -> None:
    code, out, _ = run_cli(capsys, "ui", "--help")
    assert code == 0
    assert "--config" in out and "--no-config" in out


def test_ui_without_a_terminal_fails_clearly(capsys, make_file) -> None:
    path = make_file()
    code, out, err = run_cli(capsys, "ui", str(path))  # pytest's stdin/stdout are not a TTY
    assert code == exitcodes.EXIT_INVALID
    assert "interactive terminal" in err and "filepumper pad" in err
    assert out == ""
    assert path.stat().st_size == 1280  # nothing was touched


def test_ui_rejects_conflicting_config_flags(capsys) -> None:
    code, _, err = run_cli(capsys, "ui", "--config", "x.toml", "--no-config")
    assert code == exitcodes.EXIT_INVALID and "not allowed" in err


def test_ui_reads_the_config_file_before_launching(
    capsys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {}

    def fake_launch(paths=(), *, user=None) -> int:
        seen["paths"], seen["user"] = list(paths), user
        return 0

    monkeypatch.setattr(tui, "launch", fake_launch)
    cfg = tmp_path / "c.toml"
    cfg.write_text('pattern = "random"\nverify = true\n')
    assert main(["ui", "--config", str(cfg), "a.bin", "b.bin"]) == 0
    assert seen["paths"] == ["a.bin", "b.bin"]
    assert seen["user"].pattern == "random" and seen["user"].verify is True


def test_invalid_config_is_reported_for_ui(capsys, tmp_path: Path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("this is = = not toml")
    code, _, err = run_cli(capsys, "ui", "--config", str(cfg))
    assert code == exitcodes.EXIT_INVALID and err.strip()


def test_no_command_in_a_script_prints_help_and_fails(capsys) -> None:
    code, out, err = run_cli(capsys)
    assert code == exitcodes.EXIT_INVALID
    assert out == "" and "usage" in err.lower() and "ui" in err


def test_no_command_on_a_terminal_opens_the_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(tui, "curses_available", lambda: True)
    monkeypatch.setattr(tui, "launch", lambda *a, **k: calls.append(k) or 0)
    assert main([]) == 0 and len(calls) == 1


def test_no_command_on_a_terminal_without_curses_prints_help(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(tui, "curses_available", lambda: False)
    code, _, err = run_cli(capsys)
    assert code == exitcodes.EXIT_INVALID and "usage" in err.lower()


def test_launch_error_is_reported_without_a_traceback(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **k):
        raise tui.UiUnavailableError("no curses here")

    monkeypatch.setattr(tui, "launch", boom)
    code, _, err = run_cli(capsys, "ui")
    assert code == exitcodes.EXIT_INVALID and "no curses here" in err


def test_interrupt_in_the_ui_exits_130(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "launch", interrupted)
    assert main(["ui"]) == exitcodes.EXIT_INTERRUPTED


def test_curses_available_reflects_the_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "curses", None)  # makes `import curses` raise ImportError
    assert tui.curses_available() is False


def test_importing_the_package_never_imports_curses() -> None:
    code = "import sys, filepumper, filepumper.cli; print('curses' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "False"


def test_ellipsis_follows_the_symbol_set() -> None:
    try:
        textutil.set_ellipsis("...")
        assert textutil.truncate("abcdefghij", 6) == "abc..."
        assert textutil.truncate_left("abcdefghij", 6) == "...hij"
        assert textutil.fit("abcdefghij", 6) == "abc..."
        assert textutil.truncate("abcdefghij", 6, "~") == "abcde~"
    finally:
        textutil.set_ellipsis("…")
    assert textutil.truncate("abcdefghij", 6) == "abcde…"


# ------------------------------------------------------------- real pseudo-terminal


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pseudo-terminal")
def test_ui_runs_in_a_real_terminal(tmp_path: Path) -> None:
    """Start `python -m filepumper ui` on a pty, pad a file with the keyboard, and quit."""
    pty = pytest.importorskip("pty")
    import fcntl
    import select
    import struct
    import termios

    try:
        import curses  # noqa: F401
    except ImportError:
        pytest.skip("curses is not available")

    target = tmp_path / "data.bin"
    target.write_bytes(b"x" * 10)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "TERM": "xterm",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "FILEPUMPER_CONFIG": str(tmp_path / "none.toml"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "filepumper", "ui", str(target)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=tmp_path,
        close_fds=True,
    )
    os.close(slave)
    screen = bytearray()

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                screen.extend(chunk)

    def wait_for(text: bytes, timeout: float = 10.0) -> None:
        end = time.monotonic() + timeout
        while text not in screen and time.monotonic() < end and proc.poll() is None:
            pump(0.1)
        assert text in screen, screen.decode("utf-8", "replace")[-2000:]

    try:
        wait_for(b"FILE PUMPER")
        os.write(master, b"\t")  # Files -> Settings (the file is pre-selected)
        wait_for(b"Operation")
        os.write(master, b"\x7f\x7f\x7f\x7f64B")  # size field: replace "10MB"
        pump(0.3)
        os.write(master, b"\x0e")  # Ctrl+N: continue to review
        wait_for(b"Nothing has been written yet")
        assert target.stat().st_size == 10  # review changes nothing
        os.write(master, b"\r")  # start
        wait_for(b"1 succeeded")
        os.write(master, b"q")
        pump(0.5)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)

    assert target.read_bytes() == b"x" * 10 + bytes(64)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["data.bin"]
    assert __version__.encode() in screen
