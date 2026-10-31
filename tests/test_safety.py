from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import SAMPLE, is_root
from filepumper import engine, safety, system_info
from filepumper.exceptions import InsufficientSpaceError, ValidationError, WriteError
from filepumper.safety import SpaceEntry


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")


# ------------------------------------------------------------------------ identity


def test_snapshot_detects_changes(make_file) -> None:
    path = make_file()
    before = safety.snapshot(path)
    assert before.differences(safety.snapshot(path)) == []
    with open(path, "ab") as handle:
        handle.write(b"x")
    changed = before.differences(safety.snapshot(path))
    assert "size" in changed


def test_snapshot_detects_mtime_only_change(make_file) -> None:
    path = make_file()
    before = safety.snapshot(path)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert "modification time" in before.differences(safety.snapshot(path))


# --------------------------------------------------------------------- validation


def test_valid_regular_file(make_file) -> None:
    path = make_file()
    item = safety.validate_file(path)
    assert item.dest == str(path)
    assert item.identity.size == len(SAMPLE)
    assert item.warnings == ()


def test_missing_path(tmp_path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        safety.validate_file(tmp_path / "missing")


def test_directory_refused(tmp_path) -> None:
    with pytest.raises(ValidationError, match="directory"):
        safety.validate_file(tmp_path)


def test_symlink_refused_by_default(make_file, tmp_path) -> None:
    target = make_file()
    link = tmp_path / "link.bin"
    symlink_or_skip(link, target)
    with pytest.raises(ValidationError, match="symbolic link"):
        safety.validate_file(link)


def test_symlink_followed_on_request_modifies_target_and_keeps_link(
    make_file, tmp_path, run
) -> None:
    target = make_file("real.bin")
    link = tmp_path / "link.bin"
    symlink_or_skip(link, target)
    result = run([link], add=100, follow_symlinks=True)
    assert result.succeeded == 1
    assert link.is_symlink()
    assert target.stat().st_size == len(SAMPLE) + 100
    assert link.read_bytes() == target.read_bytes()


def test_followed_symlink_carries_a_warning(make_file, tmp_path) -> None:
    target = make_file("real.bin")
    link = tmp_path / "link.bin"
    symlink_or_skip(link, target)
    item = safety.validate_file(link, follow_symlinks=True)
    assert item.dest == os.path.realpath(target)
    assert any("symbolic link" in w for w in item.warnings)


def test_dangling_symlink_is_rejected(tmp_path) -> None:
    link = tmp_path / "dangling"
    symlink_or_skip(link, tmp_path / "nowhere")
    with pytest.raises(ValidationError):
        safety.validate_file(link, follow_symlinks=True)


def test_symlink_to_directory_is_rejected(tmp_path) -> None:
    (tmp_path / "dir").mkdir()
    link = tmp_path / "dirlink"
    symlink_or_skip(link, tmp_path / "dir")
    with pytest.raises(ValidationError, match="directory"):
        safety.validate_file(link, follow_symlinks=True)


def test_two_symlinks_to_one_file_are_duplicates(make_file, tmp_path) -> None:
    target = make_file("real.bin")
    one, two = tmp_path / "one", tmp_path / "two"
    symlink_or_skip(one, target)
    symlink_or_skip(two, target)
    _, problems = safety.inspect_files([one, two], follow_symlinks=True)
    assert len(problems) == 1 and "same file" in problems[0]


def _hardlink_or_skip(source: Path, link: Path) -> None:
    try:
        os.link(source, link)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are not available here")


def test_hardlinked_file_refused_by_default(make_file, tmp_path) -> None:
    path = make_file()
    _hardlink_or_skip(path, tmp_path / "second-name.bin")
    with pytest.raises(ValidationError, match="hard link"):
        safety.validate_file(path)


def test_hardlink_allowed_on_request_warns_and_detaches(make_file, tmp_path, run) -> None:
    path = make_file()
    other = tmp_path / "second-name.bin"
    _hardlink_or_skip(path, other)
    item = safety.validate_file(path, allow_hardlinks=True)
    assert any("hard link" in w for w in item.warnings)
    assert run([path], add=100, allow_hardlinks=True).succeeded == 1
    assert path.stat().st_size == len(SAMPLE) + 100
    assert other.read_bytes() == SAMPLE  # the other name keeps the old content, as warned


@pytest.mark.skipif(
    os.name == "nt" or is_root(), reason="needs POSIX permissions and a non-root user"
)
def test_read_only_file_refused(make_file) -> None:
    path = make_file()
    path.chmod(0o444)
    try:
        with pytest.raises(ValidationError, match="not writable"):
            safety.validate_file(path)
    finally:
        path.chmod(0o644)


@pytest.mark.skipif(
    os.name == "nt" or is_root(), reason="needs POSIX permissions and a non-root user"
)
def test_unwritable_directory_refused_and_nothing_changes(make_file, run, tmp_path) -> None:
    path = make_file(subdir="locked")
    path.parent.chmod(0o555)
    try:
        with pytest.raises(ValidationError, match="not writable"):
            engine.build_plan([str(path)], _options())
        assert path.read_bytes() == SAMPLE
    finally:
        path.parent.chmod(0o755)


def _options():
    from filepumper import config
    from filepumper.patterns import make_pattern

    return engine.PadOptions(
        mode=engine.Mode.ADD,
        size=10,
        pattern=make_pattern("zero"),
        settings=config.resolve_performance("conservative"),
    )


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("doc.pdf", "PDF"),
        ("book.docx", "ZIP-based"),
        ("data.JSON", "JSON"),
        ("app.exe", "executable"),
    ],
)
def test_format_warnings_are_factual(name: str, fragment: str) -> None:
    warning = safety.format_warning(name)
    assert warning is not None and fragment in warning
    assert "generic bytes" in warning
    assert "safe" not in warning.lower().replace("not", "")


def test_unknown_extension_has_no_warning() -> None:
    assert safety.format_warning("blob.xyz123") is None
    assert safety.format_warning("noextension") is None


def test_warning_does_not_block_the_operation(make_file, run) -> None:
    path = make_file("report.pdf", b"%PDF-1.4\n%%EOF\n")
    result = run([path], add=100)
    assert result.succeeded == 1
    assert path.read_bytes().startswith(b"%PDF-1.4\n%%EOF\n")


def test_inspect_files_returns_good_and_bad(make_file, tmp_path) -> None:
    good = make_file()
    valid, problems = safety.inspect_files([good, tmp_path / "nope", tmp_path])
    assert [v.path for v in valid] == [str(good)]
    assert len(problems) == 2


# --------------------------------------------------------------------- free space


def test_required_space_formula() -> None:
    entries = [SpaceEntry("/d", 100, 10), SpaceEntry("/d", 50, 10)]
    assert safety.required_space(entries, jobs=1, keep_backup=False) == 20 + 100
    assert safety.required_space(entries, jobs=2, keep_backup=False) == 20 + 150
    assert safety.required_space(entries, jobs=8, keep_backup=False) == 20 + 150
    assert safety.required_space(entries, jobs=1, keep_backup=True) == 110 + 60


def test_insufficient_space_is_detected(make_file, make_options, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(system_info, "free_space", lambda _p: 10)
    plan = engine.build_plan([str(path)], make_options(add=5000))
    report = engine.plan_space(plan)
    assert not report.sufficient
    assert report.devices[0].shortfall > 0
    with pytest.raises(InsufficientSpaceError, match="insufficient free space"):
        report.raise_if_insufficient()
    assert path.read_bytes() == SAMPLE


def test_sufficient_space_passes(make_file, make_options, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(system_info, "free_space", lambda _p: 10**12)
    report = engine.plan_space(engine.build_plan([str(path)], make_options(add=5000)))
    assert report.sufficient
    report.raise_if_insufficient()


def test_exactly_enough_space_passes_and_one_less_fails(
    make_file, make_options, monkeypatch
) -> None:
    path = make_file()
    plan = engine.build_plan([str(path)], make_options(add=1000))
    needed = engine.plan_space(plan).total_required
    monkeypatch.setattr(system_info, "free_space", lambda _p: needed)
    assert engine.plan_space(plan).sufficient
    monkeypatch.setattr(system_info, "free_space", lambda _p: needed - 1)
    assert not engine.plan_space(plan).sufficient


def test_unknown_free_space_does_not_block(make_file, make_options, monkeypatch) -> None:
    path = make_file()
    monkeypatch.setattr(system_info, "free_space", lambda _p: None)
    report = engine.plan_space(engine.build_plan([str(path)], make_options(add=5000)))
    assert report.sufficient


def test_space_is_accumulated_per_filesystem_for_concurrent_files(
    make_file, make_options, monkeypatch
) -> None:
    files = [make_file(f"{i}.bin", bytes(1000)) for i in range(3)]
    plan = engine.build_plan([str(p) for p in files], make_options(add=500, jobs=3))
    report = engine.plan_space(plan)
    assert len(report.devices) == 1  # one temp directory on one filesystem
    assert report.devices[0].required == 3 * 500 + 3 * 1000


# ----------------------------------------------------------------------- backups


def test_create_backup_link_is_collision_safe(make_file) -> None:
    path = make_file()
    first = safety.create_backup_link(str(path))
    second = safety.create_backup_link(str(path))
    assert first.endswith(".bak") and second.endswith(".bak.1")
    assert Path(first).read_bytes() == Path(second).read_bytes() == SAMPLE


def test_backup_failure_is_a_write_error(make_file, monkeypatch) -> None:
    path = make_file()

    def nope(*_a, **_k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(safety.os, "link", nope)
    with pytest.raises(WriteError, match="backup"):
        safety.create_backup_link(str(path))


# --------------------------------------------------------------------- system info


def test_storage_info_walks_up_to_existing_parent(tmp_path) -> None:
    info = system_info.storage_info(tmp_path / "does" / "not" / "exist")
    assert info.free is not None and info.total is not None
    assert Path(info.probed or "") == tmp_path
    assert info.used == info.total - info.free


def test_storage_info_unknown_when_probe_fails(tmp_path, monkeypatch) -> None:
    def broken(_path):
        raise OSError("boom")

    monkeypatch.setattr(system_info.shutil, "disk_usage", broken)
    info = system_info.storage_info(tmp_path)
    assert info.free is None and info.total is None and info.used is None
    assert system_info.free_space(tmp_path) is None


def test_recommendation_falls_back_to_conservative() -> None:
    assert system_info.recommend_preset(None, None) == "conservative"
    assert system_info.recommend_preset(8, 512 * 1024**2) == "conservative"
    assert system_info.recommend_preset(8, None) == "balanced"
    assert system_info.recommend_preset(8, 16 * 1024**3) == "balanced"


def test_total_memory_is_unknown_without_sysconf(monkeypatch) -> None:
    def broken(_name):
        raise ValueError("unsupported")

    monkeypatch.setattr(system_info.os, "sysconf", broken, raising=False)
    assert system_info.total_memory() is None
