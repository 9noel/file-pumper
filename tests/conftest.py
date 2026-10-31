"""Shared fixtures. Every test works inside pytest's temporary directories only."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from filepumper import config, engine, system_info
from filepumper.patterns import PatternGenerator, make_pattern

SAMPLE = bytes(range(256)) * 5  # 1280 bytes with a recognisable, non-repeating-looking prefix

HUGE = 2**60


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Never read the developer's real configuration file."""
    missing = tmp_path_factory.mktemp("cfg") / "does-not-exist.toml"
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(missing))


@pytest.fixture
def make_file(tmp_path: Path) -> Callable[..., Path]:
    """Create a file with the given content inside the test's temporary directory."""

    def _make(name: str = "sample.bin", data: bytes = SAMPLE, subdir: str | None = None) -> Path:
        directory = tmp_path / subdir if subdir else tmp_path
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(data)
        return path

    return _make


@pytest.fixture
def make_options() -> Callable[..., engine.PadOptions]:
    """Build engine options with small, fast settings."""

    def _make(
        *,
        add: int | None = None,
        target: int | None = None,
        pattern: PatternGenerator | None = None,
        chunk_size: int = 4096,
        jobs: int = 1,
        **kwargs,
    ) -> engine.PadOptions:
        assert (add is None) != (target is None)
        return engine.PadOptions(
            mode=engine.Mode.ADD if add is not None else engine.Mode.TARGET,
            size=add if add is not None else target,  # type: ignore[arg-type]
            pattern=pattern or make_pattern("zero"),
            settings=config.resolve_performance("conservative", chunk_size, jobs),
            **kwargs,
        )

    return _make


@pytest.fixture
def run(make_options):
    """Plan and execute in one step; returns the BatchResult."""

    def _run(paths, **kwargs) -> engine.BatchResult:
        options = make_options(**kwargs)
        plan = engine.build_plan([str(p) for p in paths], options)
        return engine.run_batch(plan)

    return _run


@pytest.fixture
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend every filesystem has enormous free space (for large dry-run style tests)."""
    monkeypatch.setattr(system_info, "free_space", lambda _path: HUGE)


def temp_files(directory: Path) -> list[Path]:
    """Return leftover File Pumper temporary files in ``directory``."""
    return sorted(directory.glob(f"*{engine.TEMP_SUFFIX}"))


def directory_state(directory: Path) -> dict[str, tuple[int, int, int, bytes]]:
    """Snapshot names, sizes, mtimes, modes and contents so tests can prove nothing changed."""
    state = {}
    for item in sorted(directory.rglob("*")):
        st = item.lstat()
        content = item.read_bytes() if item.is_file() else b""
        state[str(item)] = (st.st_size, st.st_mtime_ns, st.st_mode, content)
    return state


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0
