from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SAMPLE, directory_state, temp_files
from filepumper import __version__, cli, config, engine, system_info
from filepumper.cli import main

SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ------------------------------------------------------------------ help / version


def test_version(capsys) -> None:
    code, out, _ = run_cli(capsys, "--version")
    assert code == 0
    assert out.strip() == f"filepumper {__version__}"


def test_help_lists_commands(capsys) -> None:
    code, out, _ = run_cli(capsys, "--help")
    assert code == 0
    for word in ("pad", "info", "interactive", "--version"):
        assert word in out


def test_pad_help_documents_every_option(capsys) -> None:
    code, out, _ = run_cli(capsys, "pad", "--help")
    assert code == 0
    for option in (
        "--add", "--target", "--pattern", "--repeat-hex", "--preset", "--chunk-size", "--jobs",
        "--dry-run", "--verify", "--backup", "--mark", "--max-add", "--allow-large", "--yes",
        "--follow-symlinks", "--allow-hardlinks", "--report", "--no-progress", "--verbose",
        "--config", "--no-config",
    ):  # fmt: skip
        assert option in out, option
    for fragment in ("exit codes", "1024", "does not keep the file working the same way"):
        assert fragment in out


def test_no_command_prints_help_and_fails(capsys) -> None:
    code, _, err = run_cli(capsys)
    assert code == 2
    assert "COMMAND" in err


def test_module_entry_point_runs() -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    completed = subprocess.run(
        [sys.executable, "-m", "filepumper", "--version"],
        capture_output=True, text=True, env=env, check=False, timeout=60,
    )  # fmt: skip
    assert completed.returncode == 0
    assert __version__ in completed.stdout


# ------------------------------------------------------------------ pad: success


def test_add_success(make_file, capsys) -> None:
    path = make_file()
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "2KB", "--no-progress")
    assert code == 0
    assert path.stat().st_size == len(SAMPLE) + 2048
    assert path.read_bytes()[: len(SAMPLE)] == SAMPLE
    assert "succeeded     : 1" in out
    assert "[OK]" in out
    assert "Total bytes added: 2.00 KiB (2,048 bytes)" in out


def test_target_success(make_file, capsys) -> None:
    path = make_file()
    code, out, _ = run_cli(capsys, "pad", str(path), "--target", "10KiB", "--no-progress")
    assert code == 0
    assert path.stat().st_size == 10 * 1024
    assert "expand each file" in out


@pytest.mark.parametrize(
    ("args", "expected_tail"),
    [
        (["--pattern", "zero"], bytes(8)),
        (["--pattern", "repeat", "--repeat-hex", "DEADBEEF"], b"\xde\xad\xbe\xef\xde\xad\xbe\xef"),
        (["--pattern", "blend"], bytes(range(8))),  # cycled from the file's own leading bytes
    ],
)
def test_patterns_via_cli(make_file, capsys, args: list[str], expected_tail: bytes) -> None:
    path = make_file()
    code, _, _ = run_cli(capsys, "pad", str(path), "--add", "8B", "--no-progress", *args)
    assert code == 0
    assert path.read_bytes()[len(SAMPLE) :] == expected_tail


def test_random_pattern_with_verify(make_file, capsys) -> None:
    path = make_file()
    code, out, _ = run_cli(
        capsys,
        "pad",
        str(path),
        "--add",
        "64KiB",
        "--pattern",
        "random",
        "--verify",
        "--no-progress",
    )
    assert code == 0
    assert "[verified]" in out
    assert path.stat().st_size == len(SAMPLE) + 65536


def test_multiple_paths_with_spaces(make_file, capsys) -> None:
    a = make_file("a b.bin", subdir="test data")
    b = make_file("b.bin", subdir="test data")
    code, out, _ = run_cli(capsys, "pad", str(a), str(b), "--add", "50B", "--no-progress")
    assert code == 0
    assert a.stat().st_size == b.stat().st_size == len(SAMPLE) + 50
    assert out.count("[OK]") == 2


def test_tuning_options(make_file, capsys) -> None:
    paths = [make_file(f"{i}.bin") for i in range(3)]
    code, out, _ = run_cli(
        capsys, "pad", *map(str, paths), "--add", "1MiB", "--jobs", "2", "--chunk-size", "64KiB",
        "--preset", "fast", "--no-progress",
    )  # fmt: skip
    assert code == 0
    assert "chunk size 64.00 KiB, jobs 2" in out
    assert all(p.stat().st_size == len(SAMPLE) + 2**20 for p in paths)


def test_zero_add_is_a_noop_with_exit_zero(make_file, capsys, tmp_path) -> None:
    path = make_file()
    before = directory_state(tmp_path)
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "0B", "--no-progress")
    assert code == 0
    assert "skipped       : 1" in out
    assert "Nothing to do" in out
    assert directory_state(tmp_path) == before


def test_target_equal_to_size_is_skipped(make_file, capsys) -> None:
    path = make_file()
    code, out, _ = run_cli(capsys, "pad", str(path), "--target", f"{len(SAMPLE)}B", "--no-progress")
    assert code == 0 and "[SKIPPED]" in out


def test_progress_is_plain_when_redirected(make_file, capsys) -> None:
    path = make_file()
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "1MiB")
    assert code == 0
    assert "\r" not in err
    assert "[1/1]" in err and "done" in err


def test_no_progress_flag_silences_progress(make_file, capsys) -> None:
    path = make_file()
    _, _, err = run_cli(capsys, "pad", str(path), "--add", "1KiB", "--no-progress")
    assert err == ""


def test_verbose_logging_goes_to_stderr(make_file, capsys) -> None:
    path = make_file()
    _, _, err = run_cli(capsys, "pad", str(path), "--add", "1KiB", "--no-progress", "-v")
    assert "staging" in err and "committed" in err


# -------------------------------------------------------------------- pad: dry run


def test_dry_run_changes_nothing_and_needs_no_prompt(
    make_file, capsys, tmp_path, monkeypatch
) -> None:
    path = make_file()
    before = directory_state(tmp_path)
    atime = path.stat().st_atime_ns

    def no_prompt(*_a, **_k):
        raise AssertionError("dry run must not prompt")

    monkeypatch.setattr(cli, "_prompt", no_prompt)
    monkeypatch.setattr(engine, "run_batch", no_prompt)
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "2GB", "--dry-run")
    assert code == 0
    assert "DRY RUN" in out
    for expected in (
        "current size",
        "additional",
        "final size",
        "temp space",
        "Pattern",
        "Performance",
    ):
        assert expected in out
    assert "2.00 GiB (2,147,483,648 bytes)" in out
    assert directory_state(tmp_path) == before
    assert path.stat().st_atime_ns == atime


def test_dry_run_shows_warnings(make_file, capsys) -> None:
    path = make_file("document.pdf", b"%PDF-1.4")
    _, out, _ = run_cli(capsys, "pad", str(path), "--add", "1KB", "--dry-run")
    assert "WARNING" in out and "PDF" in out


def test_dry_run_predicts_space_failure(make_file, capsys, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(system_info, "free_space", lambda _p: 100)
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "1MB", "--dry-run")
    assert code == 3
    assert "INSUFFICIENT" in out and "Dry run predicts a failure" in out
    assert path.read_bytes() == SAMPLE


def test_dry_run_with_report(make_file, capsys, tmp_path) -> None:
    path = make_file()
    report = tmp_path / "out" / "plan.json"
    report.parent.mkdir()
    code, _, _ = run_cli(
        capsys, "pad", str(path), "--add", "1KB", "--dry-run", "--report", str(report)
    )
    assert code == 0
    data = json.loads(report.read_text())
    assert data["dry_run"] is True
    assert data["summary"]["would_modify"] == 1
    assert data["files"][0]["status"] == "planned"
    assert path.read_bytes() == SAMPLE


# --------------------------------------------------------------- pad: invalid usage


@pytest.mark.parametrize(
    "argv",
    [
        ["pad", "f.bin"],  # neither --add nor --target
        ["pad", "f.bin", "--add", "1KB", "--target", "2KB"],  # mutually exclusive
        ["pad", "--add", "1KB"],  # no paths
        ["pad", "f.bin", "--add", "100"],  # no unit
        ["pad", "f.bin", "--add", "-5MB"],
        ["pad", "f.bin", "--add", "5XB"],
        ["pad", "f.bin", "--add", "inf"],
        ["pad", "f.bin", "--add", "1KB", "--pattern", "sparse"],
        ["pad", "f.bin", "--add", "1KB", "--preset", "turbo"],
        ["pad", "f.bin", "--add", "1KB", "--jobs", "0"],
        ["pad", "f.bin", "--add", "1KB", "--jobs", "abc"],
        ["pad", "f.bin", "--add", "1KB", "--max-add", "1KB", "--allow-large"],
        ["pad", "f.bin", "--add", "1KB", "--config", "x.toml", "--no-config"],
        ["pad", "f.bin", "--add", "1KB", "--bogus"],
    ],
)
def test_parser_rejects_invalid_arguments(make_file, capsys, argv: list[str]) -> None:
    make_file("f.bin")
    code, _, err = run_cli(capsys, *argv)
    assert code == 2
    assert "error" in err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--pattern", "repeat"], "requires --repeat-hex"),
        (
            ["--pattern", "zero", "--repeat-hex", "AB"],
            "only be used together with --pattern repeat",
        ),
        (["--pattern", "repeat", "--repeat-hex", "XYZ"], "non-hexadecimal"),
        (["--pattern", "repeat", "--repeat-hex", "ABC"], "odd number"),
        (["--pattern", "repeat", "--repeat-hex", ""], "empty"),
        (["--chunk-size", "1KB"], "outside the allowed range"),
        (["--chunk-size", "1GB"], "outside the allowed range"),
        (["--jobs", "999"], "jobs must be between"),
    ],
)
def test_invalid_options_are_rejected_before_touching_files(
    make_file, capsys, tmp_path, extra, message
) -> None:
    path = make_file()
    before = directory_state(tmp_path)
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "1KB", "--no-progress", *extra)
    assert code == 2
    assert message in err
    assert directory_state(tmp_path) == before


def test_missing_file_rejects_the_whole_batch(make_file, capsys, tmp_path) -> None:
    good = make_file()
    before = directory_state(tmp_path)
    code, _, err = run_cli(capsys, "pad", str(good), str(tmp_path / "nope.bin"), "--add", "1KB")
    assert code == 2
    assert "nope.bin: does not exist" in err
    assert "nothing was modified" in err
    assert directory_state(tmp_path) == before


def test_directory_rejected(capsys, tmp_path) -> None:
    code, _, err = run_cli(capsys, "pad", str(tmp_path), "--add", "1KB")
    assert code == 2 and "is a directory" in err


def test_shrink_rejected_with_clear_message(make_file, capsys) -> None:
    path = make_file()
    code, _, err = run_cli(capsys, "pad", str(path), "--target", "10B")
    assert code == 2
    assert "never truncates" in err
    assert path.read_bytes() == SAMPLE


def test_symlink_and_hardlink_refusals_are_explained(make_file, capsys, tmp_path) -> None:
    path = make_file()
    try:
        os.symlink(path, tmp_path / "link")
        os.link(path, tmp_path / "hard")
    except (OSError, NotImplementedError):
        pytest.skip("links unavailable")
    code, _, err = run_cli(capsys, "pad", str(tmp_path / "link"), "--add", "1KB")
    assert code == 2 and "--follow-symlinks" in err
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "1KB")
    assert code == 2 and "--allow-hardlinks" in err


# ------------------------------------------------------- size limit vs confirmation


def test_default_cap_blocks_large_additions_and_yes_does_not_lift_it(
    make_file, capsys, plenty_of_space
) -> None:
    path = make_file()
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "20GiB", "--yes", "--dry-run")
    assert code == 2
    assert "per-file limit" in err and "--allow-large" in err


def test_allow_large_lifts_the_cap(make_file, capsys, plenty_of_space) -> None:
    path = make_file()
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "20GiB", "--allow-large", "--dry-run")
    assert code == 0
    assert "20.00 GiB" in out
    assert path.read_bytes() == SAMPLE


def test_max_add_sets_a_custom_cap(make_file, capsys) -> None:
    path = make_file()
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "2KB", "--max-add", "1KB")
    assert code == 2 and "per-file limit of 1.00 KiB" in err


def test_mark_adds_a_findable_footer_within_the_exact_requested_size(make_file, capsys) -> None:
    from filepumper import marker

    path = make_file()
    original_size = len(SAMPLE)
    code, _, _ = run_cli(capsys, "pad", str(path), "--add", "500B", "--mark", "--no-progress")
    assert code == 0
    assert path.stat().st_size == original_size + 500
    with path.open("rb") as handle:
        handle.seek(-marker.SIZE, 2)
        found = marker.unpack(handle.read())
    assert found is not None
    assert found.original_size == original_size and found.added_bytes == 500


def test_mark_too_small_is_rejected(make_file, capsys) -> None:
    path = make_file()
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "10B", "--mark")
    assert code == 2 and "--mark needs at least" in err
    assert path.read_bytes() == SAMPLE


def test_mark_shown_in_dry_run_plan(make_file, capsys) -> None:
    path = make_file()
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "200B", "--mark", "--dry-run")
    assert code == 0
    assert "Mark       : yes" in out
    assert path.read_bytes() == SAMPLE


def test_allow_large_keeps_the_free_space_check(make_file, capsys, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(system_info, "free_space", lambda _p: 1000)
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "20GiB", "--allow-large", "--yes")
    assert code == 3
    assert "insufficient free space" in err
    assert path.read_bytes() == SAMPLE


@pytest.fixture
def low_threshold(monkeypatch):
    monkeypatch.setattr(config, "CONFIRM_THRESHOLD", 1000)


def test_large_operation_needs_confirmation_in_scripts(
    make_file, capsys, low_threshold, monkeypatch
) -> None:
    path = make_file()
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "2KB", "--no-progress")
    assert code == 4
    assert "--yes" in err
    assert path.read_bytes() == SAMPLE


def test_yes_skips_confirmation(make_file, capsys, low_threshold, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(cli, "_prompt", lambda _q: pytest.fail("must not prompt with --yes"))
    code, _, _ = run_cli(capsys, "pad", str(path), "--add", "2KB", "--yes", "--no-progress")
    assert code == 0
    assert path.stat().st_size == len(SAMPLE) + 2048


@pytest.mark.parametrize(
    ("answer", "code", "grew"), [("y", 0, True), ("YES", 0, True), ("n", 4, False), ("", 4, False)]
)
def test_interactive_confirmation(
    make_file, capsys, low_threshold, monkeypatch, answer, code, grew
) -> None:
    path = make_file()
    questions: list[str] = []
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda q: questions.append(q) or answer)
    result, out, _ = run_cli(capsys, "pad", str(path), "--add", "2KB", "--no-progress")
    assert result == code
    assert len(questions) == 1 and "cannot guarantee" in questions[0]
    assert (path.stat().st_size > len(SAMPLE)) is grew
    if not grew:
        assert "Aborted" in out


# ----------------------------------------------------------- failures and exit codes


def test_insufficient_space_exits_3_and_changes_nothing(
    make_file, capsys, monkeypatch, tmp_path
) -> None:
    path = make_file()
    before = directory_state(tmp_path)
    monkeypatch.setattr(system_info, "free_space", lambda _p: 10)
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "1MB", "--no-progress")
    assert code == 3
    assert "insufficient free space" in err
    assert directory_state(tmp_path) == before


def test_partial_failure_summary_and_exit_code(make_file, capsys, monkeypatch) -> None:
    a, b = make_file("a.bin"), make_file("b.bin")
    real = engine._atomic_replace

    def fail_b(source: str, destination: str) -> None:
        if destination.endswith("b.bin"):
            raise OSError(5, "Input/output error")
        real(source, destination)

    monkeypatch.setattr(engine, "_atomic_replace", fail_b)
    code, out, _ = run_cli(capsys, "pad", str(a), str(b), "--add", "100B", "--no-progress")
    assert code == 1
    assert "succeeded     : 1" in out and "failed        : 1" in out
    assert "[FAILED]" in out and "b.bin" in out
    assert "partly completed" in out and "NOT rolled back" in out
    assert a.stat().st_size == len(SAMPLE) + 100
    assert b.read_bytes() == SAMPLE
    assert temp_files(a.parent) == []


def test_single_failure_exit_code(make_file, capsys, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(engine, "_fsync", lambda _h: (_ for _ in ()).throw(OSError(28, "full")))
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "100B", "--no-progress")
    assert code == 1
    assert "original file is unchanged" in out
    assert path.read_bytes() == SAMPLE


def test_keyboard_interrupt_exit_code(capsys, monkeypatch) -> None:
    def boom(_args):
        raise KeyboardInterrupt

    monkeypatch.setitem(cli._COMMANDS, "pad", boom)
    code, _, err = run_cli(capsys, "pad", "x", "--add", "1B")
    assert code == 130
    assert "Interrupted" in err


def test_interrupted_run_reports_and_exits_130(make_file, capsys, monkeypatch) -> None:
    path = make_file()
    real = engine.run_batch

    def interrupted(plan, **kwargs):
        def raising() -> None:
            raise KeyboardInterrupt

        kwargs["on_tick"] = raising
        kwargs["tick_interval"] = 0.001
        return real(plan, **kwargs)

    monkeypatch.setattr(engine, "run_batch", interrupted)
    code, out, _ = run_cli(capsys, "pad", str(path), "--add", "1MiB", "--no-progress")
    assert code == 130
    assert "cancelled" in out
    assert path.read_bytes() in (SAMPLE, SAMPLE + bytes(2**20))  # untouched or fully committed
    assert temp_files(path.parent) == []


# ------------------------------------------------------------------------ reports


def test_json_report_for_a_real_run(make_file, capsys, tmp_path) -> None:
    a, b = make_file("a.bin"), make_file("b.bin")
    report = tmp_path / "report.json"
    code, out, _ = run_cli(
        capsys, "pad", str(a), str(b), "--add", "1KiB", "--pattern", "repeat", "--repeat-hex", "AB",
        "--verify", "--no-progress", "--report", str(report),
    )  # fmt: skip
    assert code == 0
    assert f"Report written to {report}" in out
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["tool"] == "filepumper" and data["version"] == __version__
    assert data["dry_run"] is False
    assert data["operation"] == {"mode": "add", "size_bytes": 1024}
    assert data["pattern"]["kind"] == "repeat"
    assert data["options"]["verify"] is True
    assert data["summary"]["succeeded"] == 2 and data["summary"]["total_bytes_added"] == 2048
    first = data["files"][0]
    assert first["original_size_bytes"] == len(SAMPLE)
    assert first["final_size_bytes"] == len(SAMPLE) + 1024
    assert first["status"] == "success" and first["verified"] is True
    assert first["elapsed_seconds"] >= 0
    assert data["space_check"][0]["sufficient"] is True


def test_json_report_records_failures(make_file, capsys, tmp_path, monkeypatch) -> None:
    path = make_file()
    report = tmp_path / "r.json"
    monkeypatch.setattr(engine, "_fsync", lambda _h: (_ for _ in ()).throw(OSError(28, "full")))
    code, _, _ = run_cli(
        capsys, "pad", str(path), "--add", "1KiB", "--no-progress", "--report", str(report)
    )
    assert code == 1
    entry = json.loads(report.read_text())["files"][0]
    assert entry["status"] == "failed" and "no space" in entry["error"]


def test_report_path_that_is_a_directory_is_rejected_up_front(make_file, capsys, tmp_path) -> None:
    path = make_file()
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "1KB", "--report", str(tmp_path))
    assert code == 2 and "is a directory" in err
    assert path.read_bytes() == SAMPLE


def test_report_write_failure_changes_exit_code(make_file, capsys, tmp_path) -> None:
    path = make_file()
    code, out, err = run_cli(
        capsys, "pad", str(path), "--add", "1KB", "--no-progress",
        "--report", str(tmp_path / "missing-dir" / "r.json"),
    )  # fmt: skip
    assert code == 1
    assert "could not write the report" in err
    assert path.stat().st_size == len(SAMPLE) + 1024  # the work itself was done and is reported
    assert "succeeded     : 1" in out


# ------------------------------------------------------------------- config file


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "prefs.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_supplies_defaults(make_file, capsys, tmp_path) -> None:
    path = make_file()
    cfg = write_config(
        tmp_path,
        'pattern = "repeat"\nrepeat_hex = "AB"\nchunk_size = "8KiB"\njobs = 1\nverify = true\n',
    )
    code, out, _ = run_cli(
        capsys, "pad", str(path), "--add", "5B", "--no-progress", "--config", str(cfg)
    )
    assert code == 0
    assert path.read_bytes()[len(SAMPLE) :] == b"ABABA".replace(b"A", b"\xab").replace(
        b"B", b"\xab"
    )
    assert "chunk size 8.00 KiB" in out and "[verified]" in out


def test_command_line_overrides_config(make_file, capsys, tmp_path) -> None:
    path = make_file()
    cfg = write_config(
        tmp_path, 'pattern = "repeat"\nrepeat_hex = "AB"\nverify = true\nchunk_size = "8KiB"\n'
    )
    code, out, _ = run_cli(
        capsys, "pad", str(path), "--add", "5B", "--pattern", "zero", "--no-verify",
        "--chunk-size", "16KiB", "--no-progress", "--config", str(cfg),
    )  # fmt: skip
    assert code == 0
    assert path.read_bytes()[len(SAMPLE) :] == bytes(5)
    assert "chunk size 16.00 KiB" in out and "verified" not in out


def test_config_max_add_applies(make_file, capsys, tmp_path) -> None:
    path = make_file()
    cfg = write_config(tmp_path, 'max_add = "1KB"\n')
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "2KB", "--config", str(cfg))
    assert code == 2 and "per-file limit of 1.00 KiB" in err
    code, _, _ = run_cli(
        capsys,
        "pad",
        str(path),
        "--add",
        "2KB",
        "--config",
        str(cfg),
        "--allow-large",
        "--no-progress",
    )
    assert code == 0


def test_config_via_environment(make_file, capsys, tmp_path, monkeypatch) -> None:
    path = make_file()
    cfg = write_config(tmp_path, 'pattern = "random"\n')
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg))
    _, out, _ = run_cli(capsys, "pad", str(path), "--add", "5B", "--dry-run")
    assert "random (os.urandom)" in out
    _, out, _ = run_cli(capsys, "pad", str(path), "--add", "5B", "--dry-run", "--no-config")
    assert "zero (all bytes 0x00)" in out


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("unknown_key = 1\n", "unknown key"),
        ('pattern = "sparse"\n', "pattern"),
        ('jobs = "two"\n', "jobs"),
        ("jobs = true\n", "jobs"),
        ('verify = "yes"\n', "verify"),
        ('chunk_size = "lots"\n', "chunk_size"),
        ("chunk_size = -5\n", "negative"),
        ("this is = not toml [\n", "invalid configuration"),
        ('preset = "turbo"\n', "preset"),
    ],
)
def test_invalid_config_is_rejected(make_file, capsys, tmp_path, text, message) -> None:
    path = make_file()
    cfg = write_config(tmp_path, text)
    code, _, err = run_cli(capsys, "pad", str(path), "--add", "5B", "--config", str(cfg))
    assert code == 2
    assert message in err
    assert path.read_bytes() == SAMPLE


def test_missing_explicit_config_is_an_error_but_missing_default_is_not(
    make_file, capsys, tmp_path
) -> None:
    path = make_file()
    code, _, err = run_cli(
        capsys, "pad", str(path), "--add", "5B", "--config", str(tmp_path / "none.toml")
    )
    assert code == 2 and "not found" in err
    code, _, _ = run_cli(capsys, "pad", str(path), "--add", "5B", "--no-progress")
    assert code == 0


# ---------------------------------------------------------------------------- info


def test_info_text(capsys, tmp_path) -> None:
    code, out, _ = run_cli(capsys, "info", str(tmp_path))
    assert code == 0
    for label in (
        "Platform",
        "Python",
        "Logical CPUs",
        "Memory",
        "Suggested",
        "Limits",
        "Storage",
        str(tmp_path),
    ):
        assert label in out
    assert "informational, not a benchmark" in out


def test_info_json(capsys, tmp_path) -> None:
    code, out, _ = run_cli(capsys, "info", "--json", str(tmp_path))
    assert code == 0
    data = json.loads(out)
    assert data["storage"][0]["free_bytes"] > 0
    assert data["recommended"]["preset"] in config.PRESET_NAMES
    assert data["limits"]["default_max_add_bytes"] == config.DEFAULT_MAX_ADD


def test_info_defaults_to_current_directory(capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli(capsys, "info")
    assert code == 0 and str(tmp_path) in out


def test_info_reports_unknown_instead_of_guessing(capsys, tmp_path, monkeypatch) -> None:
    def broken(_path):
        raise OSError("no")

    monkeypatch.setattr(system_info.shutil, "disk_usage", broken)
    monkeypatch.setattr(system_info, "cpu_count", lambda: None)
    monkeypatch.setattr(system_info, "total_memory", lambda: None)
    code, out, _ = run_cli(capsys, "info", str(tmp_path))
    assert code == 0
    assert "Logical CPUs : unknown" in out
    assert "Memory       : unknown" in out
    assert f"{tmp_path}: unknown" in out
    assert "preset conservative" in out


def test_info_for_nonexistent_path_measures_parent(capsys, tmp_path) -> None:
    code, out, _ = run_cli(capsys, "info", str(tmp_path / "later" / "file.bin"))
    assert code == 0 and "measured at" in out


# -------------------------------------------------------------------- interactive


def test_interactive_requires_a_terminal(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    code, _, err = run_cli(capsys, "interactive")
    assert code == 2 and "needs a terminal" in err


def test_interactive_builds_and_runs_a_dry_run(make_file, capsys, monkeypatch) -> None:
    path = make_file("my file.bin")
    answers = iter([str(path), "", "a", "10x", "1KB", "repeat", "zz", "DEADBEEF", "auto", "y", "y"])
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt", lambda _q: next(answers))
    code, out, _ = run_cli(capsys, "interactive")
    assert code == 0
    assert "Equivalent command" in out
    assert "--repeat-hex DEADBEEF" in out and "--dry-run" in out
    assert "'" in out  # the path with a space is shell-quoted in the shown command
    assert path.read_bytes() == SAMPLE


def test_interactive_eof_is_a_usage_error(capsys, monkeypatch) -> None:
    def eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", eof)  # exercise the real EOF handling in _prompt
    code, _, err = run_cli(capsys, "interactive")
    assert code == 2 and "input ended" in err


# -------------------------------------------------------------------------- detect


def test_detect_help(capsys) -> None:
    code, out, _ = run_cli(capsys, "detect", "--help")
    assert code == 0
    assert "--json" in out and "overlay" in out and "structural" in out


def test_detect_clean_file_reports_no_padding(make_file, capsys) -> None:
    path = make_file()
    code, out, _ = run_cli(capsys, "detect", str(path))
    assert code == 0
    assert "no sign of padding found" in out
    assert "0/1 file(s)" in out


def test_detect_after_pad_reports_overlay(tmp_path, capsys) -> None:
    from _format_samples import SAMPLES

    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    code, _, _ = run_cli(capsys, "pad", str(path), "--add", "1KB", "--no-progress")
    assert code == 0
    code, out, _ = run_cli(capsys, "detect", str(path))
    assert code == 0
    assert "PUMPED" in out and "PNG image" in out
    assert "1/1 file(s)" in out


def test_detect_finds_the_mark(tmp_path, capsys) -> None:
    from _format_samples import SAMPLES

    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    run_cli(capsys, "pad", str(path), "--add", "1KB", "--mark", "--no-progress")
    code, out, _ = run_cli(capsys, "detect", str(path))
    assert code == 0
    assert "File Pumper mark" in out and "pattern zero" in out


def test_detect_json(tmp_path, capsys) -> None:
    from _format_samples import SAMPLES

    path = tmp_path / "a.png"
    path.write_bytes(SAMPLES["png"])
    run_cli(capsys, "pad", str(path), "--add", "1KB", "--mark", "--no-progress")
    code, out, _ = run_cli(capsys, "detect", str(path), "--json")
    assert code == 0
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["looks_pumped"] is True
    assert data[0]["marker"]["pattern"] == "zero"


def test_detect_multiple_paths_and_a_missing_one(make_file, capsys, tmp_path) -> None:
    good = make_file()
    missing = tmp_path / "nope.bin"
    code, out, _ = run_cli(capsys, "detect", str(good), str(missing))
    assert code == 1
    assert "could not be inspected" in out
    assert "[1/2]" in out and "[2/2]" in out


def test_detect_changes_nothing(make_file, capsys) -> None:
    path = make_file()
    before = path.read_bytes()
    run_cli(capsys, "detect", str(path))
    assert path.read_bytes() == before
