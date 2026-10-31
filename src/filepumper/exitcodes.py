"""Process exit codes shared by the command line and the terminal UI."""

from __future__ import annotations

from .engine import BatchResult

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_NO_SPACE = 3
EXIT_NOT_CONFIRMED = 4
EXIT_INTERRUPTED = 130


def exit_code_for(result: BatchResult) -> int:
    """Map a finished batch to an exit code (interrupted > failed/cancelled > ok)."""
    if result.interrupted:
        return EXIT_INTERRUPTED
    if result.failed or result.cancelled or result.leftover_temps:
        return EXIT_FAILED
    return EXIT_OK
