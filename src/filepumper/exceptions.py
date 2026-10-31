"""Exception hierarchy for File Pumper.

Every error that File Pumper raises on purpose derives from
:class:`FilePumperError`, so callers can catch the family without hiding
unrelated programming errors.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class FilePumperError(Exception):
    """Base class for all expected File Pumper errors."""


class InvalidSizeError(FilePumperError, ValueError):
    """A human-readable size string could not be parsed or is out of range."""


class UsageError(FilePumperError):
    """Command-line or configuration options are invalid or incompatible."""


class ConfigError(FilePumperError):
    """The user configuration file is missing, unreadable or invalid."""


class ValidationError(FilePumperError):
    """Input paths or the requested operation failed pre-flight validation.

    ``problems`` holds one human-readable message per problem so that all
    problems can be reported at once.
    """

    def __init__(self, problems: str | Sequence[str]) -> None:
        self.problems: list[str] = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class InsufficientSpaceError(FilePumperError):
    """The filesystem does not have enough free space for the operation."""

    def __init__(self, message: str, report: Any = None) -> None:
        super().__init__(message)
        self.report = report


class SourceModifiedError(FilePumperError):
    """The source file changed while it was being processed."""


class VerificationError(FilePumperError):
    """Post-write verification found a mismatch."""


class WriteError(FilePumperError):
    """Writing, flushing or committing the result failed."""


class OperationCancelledError(FilePumperError):
    """The operation was cancelled before it was committed."""
