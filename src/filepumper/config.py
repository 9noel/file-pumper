"""Performance settings, safety limits and the optional user configuration file.

Precedence for every setting is: command-line option > configuration file >
built-in default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import system_info
from .exceptions import ConfigError, InvalidSizeError, UsageError
from .sizes import GIB, KIB, MIB, format_size, parse_size

MIN_CHUNK_SIZE = 4 * KIB
MAX_CHUNK_SIZE = 64 * MIB
MAX_JOBS = 32

#: Default per-file cap on the amount of data added (override with --max-add / --allow-large).
DEFAULT_MAX_ADD = 10 * GIB

#: Operations adding at least this much in total need confirmation (or --yes).
CONFIRM_THRESHOLD = 1 * GIB

PRESET_NAMES = ("auto", "conservative", "balanced", "fast")
PATTERN_NAMES = ("zero", "random", "repeat")

_PRESETS: dict[str, tuple[int, int]] = {
    "conservative": (256 * KIB, 1),
    "balanced": (1 * MIB, 1),
    "fast": (8 * MIB, 2),
}

CONFIG_ENV_VAR = "FILEPUMPER_CONFIG"


@dataclass(frozen=True)
class PerformanceSettings:
    """Resolved performance configuration."""

    preset: str
    resolved_preset: str
    chunk_size: int
    jobs: int

    def describe(self) -> str:
        name = (
            self.preset
            if self.preset == self.resolved_preset
            else (f"{self.preset} -> {self.resolved_preset}")
        )
        return f"preset {name}, chunk size {format_size(self.chunk_size)}, jobs {self.jobs}"


def resolve_performance(
    preset: str | None = None,
    chunk_size: int | None = None,
    jobs: int | None = None,
) -> PerformanceSettings:
    """Combine a preset with optional overrides and validate the ranges."""
    name = preset or "auto"
    if name not in PRESET_NAMES:
        raise UsageError(f"unknown preset {name!r}; choose one of: {', '.join(PRESET_NAMES)}")
    cpus = system_info.cpu_count()
    resolved = (
        system_info.recommend_preset(cpus, system_info.total_memory()) if name == "auto" else name
    )
    preset_chunk, preset_jobs = _PRESETS[resolved]
    if resolved == "fast":
        preset_jobs = min(preset_jobs, cpus or 1)

    final_chunk = preset_chunk if chunk_size is None else chunk_size
    final_jobs = preset_jobs if jobs is None else jobs
    if not MIN_CHUNK_SIZE <= final_chunk <= MAX_CHUNK_SIZE:
        raise UsageError(
            f"chunk size {format_size(final_chunk)} is outside the allowed range "
            f"{format_size(MIN_CHUNK_SIZE)} to {format_size(MAX_CHUNK_SIZE)}"
        )
    if not 1 <= final_jobs <= MAX_JOBS:
        raise UsageError(f"jobs must be between 1 and {MAX_JOBS}, got {final_jobs}")
    return PerformanceSettings(name, resolved, final_chunk, final_jobs)


@dataclass(frozen=True)
class UserConfig:
    """Preferences loaded from the optional TOML configuration file."""

    preset: str | None = None
    chunk_size: int | None = None
    jobs: int | None = None
    pattern: str | None = None
    repeat_hex: str | None = None
    verify: bool | None = None
    max_add: int | None = None
    source: Path | None = None


def default_config_path() -> Path | None:
    """Return the default configuration path, or ``None`` if it cannot be determined."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    try:
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        else:
            xdg = os.environ.get("XDG_CONFIG_HOME")
            base = Path(xdg) if xdg else Path.home() / ".config"
    except RuntimeError:
        return None
    return base / "filepumper" / "config.toml"


def load_user_config(path: Path | None = None, *, explicit: bool = False) -> UserConfig:
    """Load preferences from ``path`` (or the default location).

    A missing default file is not an error. A missing *explicit* file is.
    """
    if path is None:
        path = default_config_path()
        explicit = False
    if path is None:
        return UserConfig()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"configuration file not found: {path}") from None
        return UserConfig()
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid configuration file {path}: {exc}") from exc
    return _validate_config(data, path)


def _validate_config(data: dict[str, Any], path: Path) -> UserConfig:
    allowed = {"preset", "chunk_size", "jobs", "pattern", "repeat_hex", "verify", "max_add"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s): {', '.join(unknown)}; allowed keys: "
            f"{', '.join(sorted(allowed))}"
        )

    def fail(key: str, expected: str) -> ConfigError:
        return ConfigError(f"{path}: '{key}' must be {expected}, got {data[key]!r}")

    values: dict[str, Any] = {}
    if "preset" in data:
        if data["preset"] not in PRESET_NAMES:
            raise fail("preset", "one of " + ", ".join(PRESET_NAMES))
        values["preset"] = data["preset"]
    for key in ("chunk_size", "max_add"):
        if key in data:
            values[key] = _config_size(data, key, path)
    if "jobs" in data:
        jobs = data["jobs"]
        if isinstance(jobs, bool) or not isinstance(jobs, int):
            raise fail("jobs", "an integer")
        values["jobs"] = jobs
    if "pattern" in data:
        if data["pattern"] not in PATTERN_NAMES:
            raise fail("pattern", "one of " + ", ".join(PATTERN_NAMES))
        values["pattern"] = data["pattern"]
    if "repeat_hex" in data:
        if not isinstance(data["repeat_hex"], str):
            raise fail("repeat_hex", "a hexadecimal string")
        values["repeat_hex"] = data["repeat_hex"]
    if "verify" in data:
        if not isinstance(data["verify"], bool):
            raise fail("verify", "true or false")
        values["verify"] = data["verify"]
    return UserConfig(source=path, **values)


def _config_size(data: dict[str, Any], key: str, path: Path) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(
            f"{path}: '{key}' must be a size such as \"4MiB\" or an integer byte count"
        )
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{path}: '{key}' must not be negative")
        return value
    try:
        return parse_size(value)
    except InvalidSizeError as exc:
        raise ConfigError(f"{path}: '{key}': {exc}") from exc
