"""Pre-flight validation, file identity tracking, space estimation and backups.

Nothing in this module modifies file contents. The only filesystem change it
can make is :func:`create_backup_link`, which the engine calls just before the
atomic replacement.
"""

from __future__ import annotations

import logging
import os
import stat
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import system_info
from .exceptions import InsufficientSpaceError, ValidationError, WriteError
from .sizes import format_size

log = logging.getLogger(__name__)

#: Maximum number of ``.bak`` names tried before giving up.
MAX_BACKUP_ATTEMPTS = 1000


# --------------------------------------------------------------------------- identity


@dataclass(frozen=True)
class FileIdentity:
    """Metadata used to notice that a file was changed or swapped underneath us."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int

    def differences(self, other: FileIdentity) -> list[str]:
        """Name the fields that differ between two snapshots."""
        names = ("device", "inode", "size", "mtime_ns", "ctime_ns", "mode")
        labels = {
            "device": "device",
            "inode": "file identity",
            "size": "size",
            "mtime_ns": "modification time",
            "ctime_ns": "change time",
            "mode": "permissions",
        }
        return [labels[n] for n in names if getattr(self, n) != getattr(other, n)]


def snapshot(path: str | os.PathLike[str]) -> FileIdentity:
    """Record the identity of ``path`` (following symbolic links)."""
    st = os.stat(path)
    return identity_from_stat(st)


def identity_from_stat(st: os.stat_result) -> FileIdentity:
    return FileIdentity(
        device=st.st_dev,
        inode=st.st_ino,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        ctime_ns=st.st_ctime_ns,
        mode=st.st_mode,
    )


# ------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class ValidatedFile:
    """A path that passed validation, with the real file that will be replaced."""

    path: str
    dest: str
    identity: FileIdentity
    warnings: tuple[str, ...] = ()


_STRUCTURED_EXTENSIONS: dict[str, str] = {
    ".pdf": "PDF document",
    ".zip": "ZIP archive",
    ".docx": "ZIP-based Word document",
    ".xlsx": "ZIP-based Excel workbook",
    ".pptx": "ZIP-based PowerPoint presentation",
    ".odt": "ZIP-based OpenDocument file",
    ".jar": "Java archive",
    ".apk": "Android package",
    ".gz": "gzip archive",
    ".7z": "7-Zip archive",
    ".rar": "RAR archive",
    ".iso": "disk image",
    ".json": "JSON text",
    ".xml": "XML text",
    ".csv": "CSV text",
    ".txt": "text file",
    ".sqlite": "SQLite database",
    ".db": "database file",
    ".png": "PNG image",
    ".jpg": "JPEG image",
    ".jpeg": "JPEG image",
    ".gif": "GIF image",
    ".mp3": "MP3 audio",
    ".mp4": "MP4 media container",
    ".mkv": "Matroska media container",
    ".avi": "AVI media container",
    ".exe": "Windows executable",
    ".dll": "Windows library",
    ".so": "shared library",
    ".dylib": "macOS library",
}


def format_warning(path: str) -> str | None:
    """Return a factual warning if the extension suggests a structured format.

    This is a naive, extension-only guess, used as a fallback for formats that
    :mod:`filepumper.formats` does not recognize from the file's actual content. The
    caller (``engine.build_plan``) prefers that content-based analysis when it
    recognizes the file, since it is far more reliable than an extension.
    """
    extension = os.path.splitext(path)[1].lower()
    kind = _STRUCTURED_EXTENSIONS.get(extension)
    if kind is None:
        return None
    return (
        f"'{extension}' suggests a {kind}. File Pumper appends generic bytes only and "
        "knows nothing about this format, so applications may reject the padded file."
    )


def validate_file(
    path: str | os.PathLike[str],
    *,
    follow_symlinks: bool = False,
    allow_hardlinks: bool = False,
) -> ValidatedFile:
    """Validate one path; raise :class:`ValidationError` describing the first problem."""
    given = os.fspath(path)
    try:
        link_stat = os.lstat(given)
    except FileNotFoundError:
        raise ValidationError(f"{given}: does not exist") from None
    except PermissionError:
        raise ValidationError(f"{given}: permission denied while inspecting it") from None
    except OSError as exc:
        raise ValidationError(f"{given}: cannot be inspected ({exc.strerror or exc})") from exc

    warnings: list[str] = []
    dest = given
    st = link_stat
    if stat.S_ISLNK(link_stat.st_mode):
        if not follow_symlinks:
            raise ValidationError(
                f"{given}: is a symbolic link; refused by default "
                "(use --follow-symlinks to modify the link target instead)"
            )
        try:
            dest = os.path.realpath(given, strict=True)
            st = os.stat(dest)
        except OSError as exc:
            raise ValidationError(
                f"{given}: symbolic link cannot be resolved ({exc.strerror or exc})"
            ) from exc
        warnings.append(f"symbolic link: the target '{dest}' will be replaced; the link is kept")

    if stat.S_ISDIR(st.st_mode):
        raise ValidationError(
            f"{given}: is a directory; File Pumper only modifies files that are named "
            "explicitly (recursive selection is not implemented)"
        )
    if not stat.S_ISREG(st.st_mode):
        raise ValidationError(
            f"{given}: is not a regular file (devices, pipes, sockets and other special "
            "files are not supported)"
        )

    if st.st_nlink > 1:
        if not allow_hardlinks:
            raise ValidationError(
                f"{given}: has {st.st_nlink} hard links; replacing it would detach this name "
                "from the others, so it is refused by default (use --allow-hardlinks to proceed)"
            )
        warnings.append(
            f"hard link: this name will get a new file; the other {st.st_nlink - 1} link(s) "
            "keep the old content"
        )

    if not os.access(dest, os.R_OK):
        raise ValidationError(f"{given}: is not readable")
    if not os.access(dest, os.W_OK):
        raise ValidationError(
            f"{given}: is not writable; refusing to replace a file the current user "
            "could not modify directly"
        )
    directory = os.path.dirname(os.path.abspath(dest))
    if not os.access(directory, os.W_OK | os.X_OK):
        raise ValidationError(
            f"{given}: directory '{directory}' is not writable, so a temporary file cannot "
            "be created next to it"
        )

    return ValidatedFile(
        path=given,
        dest=dest,
        identity=identity_from_stat(st),
        warnings=tuple(warnings),
    )


def inspect_files(
    paths: Iterable[str | os.PathLike[str]],
    *,
    follow_symlinks: bool = False,
    allow_hardlinks: bool = False,
) -> tuple[list[ValidatedFile], list[str]]:
    """Validate every path without stopping at the first problem.

    Returns the files that passed and the list of problems for the ones that did
    not. Files that resolve to the same underlying file are reported as
    duplicates, so two workers can never target the same file.
    """
    validated: list[ValidatedFile] = []
    problems: list[str] = []
    seen: dict[tuple[int, int], str] = {}
    for path in paths:
        try:
            item = validate_file(
                path, follow_symlinks=follow_symlinks, allow_hardlinks=allow_hardlinks
            )
        except ValidationError as exc:
            problems.extend(exc.problems)
            continue
        key = (item.identity.device, item.identity.inode)
        if key in seen:
            problems.append(f"{item.path}: refers to the same file as {seen[key]}")
            continue
        seen[key] = item.path
        validated.append(item)
    return validated, problems


# ------------------------------------------------------------------------ free space


@dataclass(frozen=True)
class SpaceEntry:
    """One file that needs staging space."""

    directory: str
    original_size: int
    add_bytes: int

    @property
    def final_size(self) -> int:
        return self.original_size + self.add_bytes


@dataclass(frozen=True)
class DeviceSpace:
    """Space requirement versus availability for one filesystem."""

    directory: str
    required: int
    free: int | None

    @property
    def sufficient(self) -> bool:
        return self.free is None or self.required <= self.free

    @property
    def shortfall(self) -> int:
        if self.free is None:
            return 0
        return max(0, self.required - self.free)


@dataclass(frozen=True)
class SpaceReport:
    """Result of the free-space preflight."""

    devices: tuple[DeviceSpace, ...]

    @property
    def sufficient(self) -> bool:
        return all(device.sufficient for device in self.devices)

    @property
    def total_required(self) -> int:
        return sum(device.required for device in self.devices)

    def describe_problems(self) -> str:
        parts = [
            f"'{d.directory}' needs about {format_size(d.required)} but only "
            f"{format_size(d.free or 0)} is free (short by {format_size(d.shortfall)})"
            for d in self.devices
            if not d.sufficient
        ]
        return "insufficient free space: " + "; ".join(parts)

    def raise_if_insufficient(self) -> None:
        if not self.sufficient:
            raise InsufficientSpaceError(self.describe_problems(), self)


def required_space(entries: Sequence[SpaceEntry], *, jobs: int, keep_backup: bool) -> int:
    """Estimate peak extra space for files that live on one filesystem.

    Each file is staged as a complete new copy (original bytes plus payload) next to
    the original. Without a backup, the old copy is released after each commit, so the
    peak is every committed addition plus the original size of the files in flight.
    With a backup the old data stays, so every new copy stays too.
    """
    if keep_backup:
        return sum(entry.final_size for entry in entries)
    in_flight = sorted((entry.original_size for entry in entries), reverse=True)[: max(1, jobs)]
    return sum(entry.add_bytes for entry in entries) + sum(in_flight)


def check_free_space(
    entries: Sequence[SpaceEntry], *, jobs: int, keep_backup: bool = False
) -> SpaceReport:
    """Compare the estimated staging need with free space, per filesystem.

    Estimates cannot rule out failures caused by other programs, quotas or
    filesystem overhead. If free space cannot be determined it is treated as
    unknown and does not block the operation.
    """
    groups: dict[int, list[SpaceEntry]] = defaultdict(list)
    for entry in entries:
        try:
            device = os.stat(entry.directory).st_dev
        except OSError:
            device = -1
        groups[device].append(entry)

    devices = []
    for items in groups.values():
        directory = items[0].directory
        devices.append(
            DeviceSpace(
                directory=directory,
                required=required_space(items, jobs=jobs, keep_backup=keep_backup),
                free=system_info.free_space(directory),
            )
        )
    return SpaceReport(devices=tuple(devices))


# ----------------------------------------------------------------------------- backup


def create_backup_link(dest: str) -> str:
    """Create a hard-link backup of ``dest`` under a collision-safe name.

    A hard link keeps the old data under a second name without copying it, and the
    atomic replacement that follows only re-points ``dest``. The name is claimed with
    an exclusive ``link`` call, so an existing file is never overwritten.
    """
    for attempt in range(MAX_BACKUP_ATTEMPTS):
        candidate = f"{dest}.bak" if attempt == 0 else f"{dest}.bak.{attempt}"
        try:
            os.link(dest, candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise WriteError(
                f"cannot create a backup of {dest}: {exc.strerror or exc} "
                "(backups need a filesystem that supports hard links)"
            ) from exc
        log.info("created backup link %s", candidate)
        return candidate
    raise WriteError(f"cannot create a backup of {dest}: no unused backup name found")
