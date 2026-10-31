"""Command-line interface (argparse). Parsing and presentation only; the work is in engine.py."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, config, engine, reporting
from .config import UserConfig
from .exceptions import (
    ConfigError,
    FilePumperError,
    InvalidSizeError,
    UsageError,
    ValidationError,
)
from .exitcodes import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    EXIT_NO_SPACE,
    EXIT_NOT_CONFIRMED,
    EXIT_OK,
    exit_code_for,
)
from .patterns import PatternKind, make_pattern, parse_hex_pattern
from .sizes import SUPPORTED_UNITS_TEXT, format_size, parse_size

PROG = "filepumper"

_EXIT_CODES_HELP = """\
exit codes:
  0    every file succeeded or was skipped as a no-op (also: dry run completed)
  1    at least one file failed or was cancelled, or a requested report could not be written
  2    invalid usage, options, configuration or input paths (nothing was modified)
  3    not enough free disk space for the staged copy (nothing was modified)
  4    a large operation was not confirmed (declined, or no --yes in a non-interactive run)
  130  interrupted with Ctrl+C (uncommitted temporary files were removed)
"""

_PAD_EPILOG = f"""\
sizes:
  Binary units only: KB = KiB = 1024 bytes, MB = MiB = 1024**2, GB = GiB = 1024**3,
  TB = TiB = 1024**4. A unit is required. Supported: {SUPPORTED_UNITS_TEXT}.

examples:
  filepumper pad sample.bin --add 100MB
  filepumper pad sample.bin --target 500MB --dry-run
  filepumper pad a.bin "my data/b.bin" --add 50MB --pattern random --verify
  filepumper pad test.bin --add 10MB --pattern repeat --repeat-hex DEADBEEF

For PE/EXE, ZIP/Office, PDF, PNG, JPEG and GIF, File Pumper locates where the file's
own structure ends and places padding after it, so the file still opens/runs the same
as before -- see --mark and 'filepumper detect'. This confirms the existing structure
is undisturbed; it does not guarantee full format-spec validity beyond that. For any
other file, or when the structure can't be confirmed, padding still just appends
generic bytes and does not keep the file working the same way. Use it for test data.

{_EXIT_CODES_HELP}"""


# ------------------------------------------------------------------------- parsing


def _size_arg(value: str) -> int:
    try:
        return parse_size(value)
    except InvalidSizeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "File Pumper grows existing files to an exact size by appending generic padding "
            "bytes. Writes are transactional: the original is only replaced once the new "
            "file is complete."
        ),
        epilog="Run 'filepumper' with no command in a terminal to open the full-screen UI. "
        "Run 'filepumper COMMAND --help' for command details.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    pad = commands.add_parser(
        "pad",
        help="append padding to one or more existing files",
        description="Append padding bytes to existing files, or expand them to a target size.",
        epilog=_PAD_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pad.add_argument("paths", nargs="+", metavar="PATH", help="existing file(s) to pad")
    amount = pad.add_mutually_exclusive_group(required=True)
    amount.add_argument(
        "--add",
        type=_size_arg,
        metavar="SIZE",
        help="append exactly SIZE bytes to each file (e.g. 100MB); 0B is a reported no-op",
    )
    amount.add_argument(
        "--target",
        type=_size_arg,
        metavar="SIZE",
        help="grow each file to exactly SIZE bytes in total; never shrinks a file",
    )
    pad.add_argument(
        "--pattern",
        choices=[kind.value for kind in PatternKind],
        help="padding bytes: zero, random (os.urandom), repeat (needs --repeat-hex), or "
        "blend (cycled from a sample of each file's own content); default zero",
    )
    pad.add_argument(
        "--repeat-hex",
        metavar="HEX",
        help="byte pattern for --pattern repeat, e.g. DEADBEEF (max 4096 bytes)",
    )
    pad.add_argument(
        "--preset",
        choices=config.PRESET_NAMES,
        help="performance preset (default auto: a conservative suggestion from detected hardware)",
    )
    pad.add_argument(
        "--chunk-size",
        type=_size_arg,
        metavar="SIZE",
        help=f"I/O chunk size, {format_size(config.MIN_CHUNK_SIZE)} to "
        f"{format_size(config.MAX_CHUNK_SIZE)}; overrides the preset",
    )
    pad.add_argument(
        "--jobs",
        type=_positive_int,
        metavar="N",
        help=f"files processed concurrently, 1 to {config.MAX_JOBS}; overrides the preset "
        "(default one worker)",
    )
    pad.add_argument(
        "--dry-run", action="store_true", help="show the plan and estimates; change nothing"
    )
    pad.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="read each result back and check size, original prefix and appended bytes "
        "(adds a full extra read of every file)",
    )
    pad.add_argument(
        "--backup",
        action="store_true",
        help="keep the original content as a hard link named FILE.bak (or .bak.N); "
        "the old data stays on disk",
    )
    pad.add_argument(
        "--mark",
        action="store_true",
        help="write a small provenance footer into the padding (findable with "
        "'filepumper detect'); consumes part of the requested size, so --add still "
        f"adds exactly that many bytes (needs at least {format_size(64)} of padding)",
    )
    large = pad.add_mutually_exclusive_group()
    large.add_argument(
        "--max-add",
        type=_size_arg,
        metavar="SIZE",
        help=f"per-file limit on the added bytes (default {format_size(config.DEFAULT_MAX_ADD)})",
    )
    large.add_argument(
        "--allow-large",
        action="store_true",
        help="remove the per-file size limit (free-space checks still apply)",
    )
    pad.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the confirmation prompt for operations adding "
        f"{format_size(config.CONFIRM_THRESHOLD)} or more in total "
        "(does not lift the size limit)",
    )
    pad.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="modify the target of a symbolic link instead of refusing it",
    )
    pad.add_argument(
        "--allow-hardlinks",
        action="store_true",
        help="allow files that have other hard links; this name gets a new file and the other "
        "links keep the old content",
    )
    pad.add_argument("--report", metavar="FILE", help="write a JSON report to FILE")
    pad.add_argument("--no-progress", action="store_true", help="do not show progress output")
    pad.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="diagnostic logging on stderr (-v info, -vv debug)",
    )
    source = pad.add_mutually_exclusive_group()
    source.add_argument("--config", metavar="FILE", help="read preferences from this TOML file")
    source.add_argument(
        "--no-config", action="store_true", help="ignore the user configuration file"
    )

    info = commands.add_parser(
        "info",
        help="show platform, CPU and storage information",
        description="Show platform, CPU count, memory, limits and free storage. "
        "Anything that cannot be determined is shown as 'unknown'.",
    )
    info.add_argument(
        "paths", nargs="*", metavar="PATH", help="paths whose storage to inspect (default: .)"
    )
    info.add_argument("--json", action="store_true", help="print machine-readable JSON")

    ui = commands.add_parser(
        "ui",
        help="open the full-screen terminal UI (also: run 'filepumper' with no command)",
        description="Open a full-screen terminal UI: browse and select files, configure the "
        "settings with a form, review the plan, then watch the run. It uses the same engine, "
        "safety checks and limits as 'filepumper pad'.",
    )
    ui.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="files to pre-select, or a folder to start in (default: the current folder)",
    )
    ui_config = ui.add_mutually_exclusive_group()
    ui_config.add_argument("--config", metavar="FILE", help="read preferences from this TOML file")
    ui_config.add_argument(
        "--no-config", action="store_true", help="ignore the user configuration file"
    )

    detect = commands.add_parser(
        "detect",
        help="check whether a file carries trailing data beyond its own format's content",
        description="For each PATH, compare what the file's own format says its content "
        "ends at to the file's actual size, and report any trailing/overlay data found -- "
        "the same region File Pumper's own padding goes into. Works on any file, not only "
        "ones padded by this tool. The check is structural (declared end vs. actual size "
        "for PE, ZIP/Office, PDF, PNG, JPEG and GIF), plus a look for File Pumper's own "
        "'--mark' footer; it is not a search for any particular byte pattern, and for an "
        "unrecognized format with no mark, there is no basis to report anything either way.",
    )
    detect.add_argument("paths", nargs="+", metavar="PATH", help="file(s) to inspect")
    detect.add_argument("--json", action="store_true", help="print machine-readable JSON")

    commands.add_parser(
        "interactive",
        help="guided prompts that build a 'pad' command",
        description="Answer a few questions; File Pumper shows the equivalent 'pad' command "
        "and runs it. Defaults to a dry run.",
    )
    return parser


# ------------------------------------------------------------------------ helpers


def _error(message: str) -> None:
    print(f"{PROG}: error: {message}", file=sys.stderr)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt(question: str) -> str:
    try:
        return input(question)
    except EOFError:
        raise UsageError("input ended before the questions were answered") from None


def _confirm(question: str) -> bool:
    return _prompt(question).strip().lower() in {"y", "yes"}


def _configure_logging(verbosity: int) -> tuple[logging.Logger, logging.Handler, int]:
    logger = logging.getLogger("filepumper")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(
        logging.DEBUG if verbosity >= 2 else logging.INFO if verbosity == 1 else logging.WARNING
    )
    return logger, handler, previous


def _harden_streams() -> None:
    """Never crash on unencodable characters in file names."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="backslashreplace")


def _load_config(args: argparse.Namespace) -> UserConfig:
    if args.no_config:
        return UserConfig()
    if args.config:
        return config.load_user_config(Path(args.config), explicit=True)
    return config.load_user_config()


def resolve_options(args: argparse.Namespace, user: UserConfig) -> engine.PadOptions:
    """Merge command line, configuration file and defaults (in that order of precedence)."""
    kind = args.pattern or user.pattern or PatternKind.ZERO.value
    if kind == PatternKind.REPEAT.value:
        repeat_hex = args.repeat_hex if args.repeat_hex is not None else user.repeat_hex
        if repeat_hex is None:
            raise UsageError("--pattern repeat requires --repeat-hex")
    else:
        if args.repeat_hex is not None:
            raise UsageError("--repeat-hex can only be used together with --pattern repeat")
        repeat_hex = None
    if repeat_hex is not None:
        parse_hex_pattern(repeat_hex)  # validate before anything else happens
    pattern = make_pattern(kind, repeat_hex)

    settings = config.resolve_performance(
        args.preset or user.preset,
        args.chunk_size if args.chunk_size is not None else user.chunk_size,
        args.jobs if args.jobs is not None else user.jobs,
    )

    if args.allow_large:
        max_add = None
    elif args.max_add is not None:
        max_add = args.max_add
    elif user.max_add is not None:
        max_add = user.max_add
    else:
        max_add = config.DEFAULT_MAX_ADD

    if args.add is not None:
        mode, size = engine.Mode.ADD, args.add
    else:
        mode, size = engine.Mode.TARGET, args.target
    verify = args.verify if args.verify is not None else bool(user.verify)
    return engine.PadOptions(
        mode=mode,
        size=size,
        pattern=pattern,
        settings=settings,
        verify=verify,
        backup=args.backup,
        follow_symlinks=args.follow_symlinks,
        allow_hardlinks=args.allow_hardlinks,
        max_add=max_add,
        mark=args.mark,
    )


def _write_report_file(path: str, report: dict) -> bool:
    try:
        reporting.write_report(path, report)
    except OSError as exc:
        _error(f"could not write the report to {path}: {exc.strerror or exc}")
        return False
    print(f"Report written to {path}")
    return True


# ------------------------------------------------------------------------ commands


def _cmd_pad(args: argparse.Namespace) -> int:
    try:
        user = _load_config(args)
        options = resolve_options(args, user)
        if args.report and os.path.isdir(args.report):
            raise UsageError(f"--report {args.report!r} is a directory; give a file name")
        plan = engine.build_plan(list(args.paths), options)
    except ValidationError as exc:
        _error("invalid input; nothing was modified:")
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_INVALID
    except (UsageError, ConfigError) as exc:
        _error(str(exc))
        return EXIT_INVALID

    space = engine.plan_space(plan)
    print(reporting.format_plan(plan, space, dry_run=args.dry_run))

    if args.dry_run:
        code = EXIT_OK
        if not space.sufficient:
            print(f"\nDry run predicts a failure: {space.describe_problems()}")
            code = EXIT_NO_SPACE
        if args.report:
            report = reporting.build_report(plan, None, space, dry_run=True)
            if not _write_report_file(args.report, report) and code == EXIT_OK:
                code = EXIT_FAILED
        return code

    if not space.sufficient:
        _error(space.describe_problems() + "; nothing was modified")
        return EXIT_NO_SPACE
    if not plan.writes:
        print("\nNothing to do: every file was skipped.")

    if plan.total_add >= config.CONFIRM_THRESHOLD and not args.yes:
        question = (
            f"\nThis will write about {format_size(plan.total_add)} of new data "
            f"(plus staging copies) to {len(plan.writes)} file(s). Disk space estimates cannot "
            "guarantee success. Proceed? [y/N] "
        )
        if not _is_interactive():
            _error(
                f"refusing to add {format_size(plan.total_add)} without confirmation in a "
                "non-interactive run; pass --yes to proceed"
            )
            return EXIT_NOT_CONFIRMED
        if not _confirm(question):
            print("Aborted; no files were modified.")
            return EXIT_NOT_CONFIRMED

    tracker = engine.ProgressTracker(plan)
    renderer = reporting.ProgressRenderer(tracker, sys.stderr, enabled=not args.no_progress)
    result = engine.run_batch(plan, tracker=tracker, on_tick=renderer.tick)
    renderer.finish()
    print(reporting.format_summary(result))

    code = exit_code_for(result)
    if args.report:
        report = reporting.build_report(plan, result, space, dry_run=False)
        if not _write_report_file(args.report, report) and code == EXIT_OK:
            code = EXIT_FAILED
    return code


def _cmd_info(args: argparse.Namespace) -> int:
    paths = list(args.paths) or [os.getcwd()]
    info = reporting.build_info(paths)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(reporting.format_info(info))
    return EXIT_OK


def _cmd_detect(args: argparse.Namespace) -> int:
    reports = reporting.build_pump_reports(list(args.paths))
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        print(reporting.format_pump_reports(reports))
    if any("error" in r for r in reports):
        return EXIT_FAILED
    return EXIT_OK


def _ask(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = _prompt(f"{question}{suffix}: ").strip()
    return answer or (default or "")


def _cmd_interactive(_args: argparse.Namespace) -> int:
    if not _is_interactive():
        _error("interactive mode needs a terminal; use 'filepumper pad' in scripts")
        return EXIT_INVALID
    print("File Pumper guided mode. Press Ctrl+C at any time to cancel.\n")
    paths: list[str] = []
    while True:
        entry = _ask("File path (leave empty to finish)" if paths else "File path")
        if entry:
            paths.append(entry)
        elif paths:
            break
    argv = ["pad", *paths]

    choice = ""
    while choice not in {"a", "t"}:
        choice = _ask("Add a fixed amount or expand to a target size? (a/t)", "a").lower()
    while True:
        try:
            size_text = _ask("Size (e.g. 100MB)")
            parse_size(size_text)
            break
        except InvalidSizeError as exc:
            print(f"  {exc}")
    argv += ["--add" if choice == "a" else "--target", size_text]

    pattern = ""
    while pattern not in {kind.value for kind in PatternKind}:
        pattern = _ask("Pattern (zero/random/repeat)", "zero").lower()
    argv += ["--pattern", pattern]
    if pattern == "repeat":
        while True:
            hex_text = _ask("Hex pattern (e.g. DEADBEEF)")
            try:
                parse_hex_pattern(hex_text)
                break
            except ValidationError as exc:
                print(f"  {exc}")
        argv += ["--repeat-hex", hex_text]

    preset = ""
    while preset not in config.PRESET_NAMES:
        preset = _ask("Performance preset (auto/conservative/balanced/fast)", "auto").lower()
    argv += ["--preset", preset]
    if _ask("Verify the result afterwards? (y/n)", "n").lower().startswith("y"):
        argv.append("--verify")
    if _ask("Dry run only, changing nothing? (y/n)", "y").lower().startswith("y"):
        argv.append("--dry-run")

    print(f"\nEquivalent command:\n  {PROG} {shlex.join(argv)}\n")
    return main(argv)


def _cmd_ui(args: argparse.Namespace) -> int:
    from . import tui

    return tui.launch(list(args.paths), user=_load_config(args))


_COMMANDS = {
    "pad": _cmd_pad,
    "info": _cmd_info,
    "detect": _cmd_detect,
    "interactive": _cmd_interactive,
    "ui": _cmd_ui,
}


def _no_command(parser: argparse.ArgumentParser) -> int:
    """With no command: open the UI on a real terminal, otherwise print help (for scripts)."""
    if _is_interactive():
        from . import tui

        if tui.curses_available():
            _harden_streams()
            try:
                return tui.launch(user=config.load_user_config())
            except KeyboardInterrupt:
                return EXIT_INTERRUPTED
            except FilePumperError as exc:
                _error(str(exc))
                return EXIT_INVALID
    parser.print_help(sys.stderr)
    return EXIT_INVALID


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits for --help, --version and usage errors
        if exc.code is None:
            return EXIT_OK
        return exc.code if isinstance(exc.code, int) else EXIT_INVALID
    if args.command is None:
        return _no_command(parser)

    _harden_streams()
    logger = handler = None
    previous_level = logging.NOTSET
    if args.command == "pad":
        logger, handler, previous_level = _configure_logging(args.verbose)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except FilePumperError as exc:
        _error(str(exc))
        return EXIT_INVALID
    finally:
        if logger is not None and handler is not None:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
