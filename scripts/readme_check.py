#!/usr/bin/env python3
"""Cross-check README.md against the actual CLI so the docs can't silently drift.

Not a pytest test (pytest's testpaths is restricted to tests/), and not shipped as
part of the package. Run it by hand, or from CI, after changing cli.py or README.md:

    python scripts/readme_check.py

It checks that:
  - every `pad` and `detect` CLI option appears somewhere in README.md
  - every PatternGenerator kind (zero/random/repeat/blend/...) is named in README.md
  - the --mark flag, the `detect` command, and the six recognized formats are
    documented
  - the marker footer size documented in README.md matches marker.SIZE

It is deliberately shallow (substring checks, not semantic ones) -- it catches a
removed/renamed option or an undocumented new one, not inaccurate prose.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from filepumper import cli, marker  # noqa: E402
from filepumper.patterns import PatternKind  # noqa: E402

README = (ROOT / "README.md").read_text()


def _help_text(*args: str) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        old_argv = sys.argv
        sys.argv = ["filepumper", *args, "--help"]
        try:
            cli.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv
    return buf.getvalue()


def _options(help_text: str) -> set[str]:
    return {opt for opt in re.findall(r"--[a-z-]+", help_text) if opt != "--help"}


def main() -> int:
    problems: list[str] = []

    if f"{marker.SIZE}-byte" not in README and f"{marker.SIZE} byte" not in README:
        problems.append(f"marker.SIZE ({marker.SIZE} bytes) is not documented in README.md")

    if "filepumper detect" not in README:
        problems.append("the 'detect' command is not mentioned in README.md")

    for kind in PatternKind:
        if kind.value not in README:
            problems.append(f"pattern kind '{kind.value}' is not mentioned in README.md")

    for fmt_name in ("PE", "ZIP", "PDF", "PNG", "JPEG", "GIF"):
        if fmt_name not in README:
            problems.append(f"recognized format '{fmt_name}' is not mentioned in README.md")

    for command in ("pad", "detect"):
        for opt in sorted(_options(_help_text(command))):
            if opt not in README:
                problems.append(f"'{command} {opt}' is not documented in README.md")

    if problems:
        print("README consistency check FAILED:")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("README consistency check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
