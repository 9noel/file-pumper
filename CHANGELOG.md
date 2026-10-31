# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [2.2.0] - 2026-10-29

### Added
- **Format-safe placement** for PE/EXE, ZIP-family (including Office and other ZIP-based formats),
  PDF, PNG, JPEG and GIF: `pad` now locates where a recognized file's own structure ends and
  appends padding strictly after it, so the file still opens, runs or renders the same as before.
  Implemented as stdlib-only structural parsers in the new `filepumper.formats` module; it never
  raises on truncated or adversarial input, and when a file's structure can't be confidently
  parsed, padding falls back to plain appending with a warning rather than a false safety claim.
- **`blend` fill pattern**: filler sampled from each file's own existing content (up to 64 KiB) and
  cycled to length, independently per file in a batch. Addresses padding that is trivially obvious
  as a flat block of filler in a hex/text viewer; it has no effect on structural detection (see
  `detect`, below) by design.
- **`--mark`**: an opt-in, never-hidden 64-byte provenance footer written into the padding,
  recording the original size, bytes added, pattern and timestamp (CRC-protected). It consumes
  part of the requested `--add`/`--target` size, so the final file size is still exact. New
  `filepumper.marker` module.
- **`filepumper detect PATH... [--json]`**: checks whether a file carries data beyond what its own
  format (or a `--mark` footer) accounts for, on any file -- not only ones padded by File Pumper.
  Structural, not a byte-pattern search: it compares where a format's own content ends to the
  file's actual size, plus an independent check for the `--mark` footer. Honestly reports no
  verdict for an unrecognized-format file with no mark, rather than guessing. New
  `filepumper.detect` module and `reporting.build_pump_reports`/`format_pump_reports`.
- Dry-run and run output now show a `format` line for files whose format is recognized, and a
  `Mark` line when `--mark` is set. The terminal UI's Settings screen gains a `blend` pattern
  choice and a `Mark` toggle; the Review screen summarizes recognized formats.
- `scripts/readme_check.py`: a dev-only script that cross-checks `README.md` against the actual
  CLI options, pattern kinds and documented constants.

### Changed
- The dry-run "Pattern" line no longer appends a blanket "(generic bytes, not format-aware)" note,
  since that is no longer true for recognized formats; the per-file `format` line now carries the
  safety information instead.
- The extension-based "applications may reject the padded file" warning now only appears when a
  file's content could not be structurally recognized at all (it previously fired on any
  suggestively-named file, even ones File Pumper has now verified it can pad safely).
- `pad --help`'s epilog wording is updated to describe the new, more nuanced safety picture instead
  of a blanket "does not keep structured files valid" statement.

### Notes
- This is an explicitly scoped, defensive feature set: format-safe *placement* and a genuine
  *detector*. It deliberately does **not** include anything tuned toward, tested against, or that
  makes any claim about antivirus, EDR or other security-product detection -- see "Not implemented"
  in the README.
- Format awareness covers locating the safe append point for six formats only; it does not
  validate, repair or re-sign internal structure, and does not extend to any other format.

## [2.1.0] - 2026-10-25

### Added
- Full-screen terminal UI (`filepumper ui`, or just `filepumper` on a terminal): a four-step flow
  (files, settings, review, run) with a folder browser, a validated settings form, an exact plan
  preview, live progress with throughput and ETA, safe cancellation with `Esc`, a per-file result
  screen and optional JSON report. Built on the standard-library `curses` module; it uses the same
  engine as `pad`, so every safety guarantee is unchanged.
- Large-operation confirmation, size cap and free-space checks in the UI, matching the commands.
- Plain-ASCII fallback (Linux console, non-UTF-8 locales, `FILEPUMPER_ASCII=1`), `NO_COLOR`
  support and a "terminal too small" screen.
- `ui` optional dependency (`windows-curses`, Windows only).
- `filepumper.exitcodes` and `RateMeter`/`estimate_eta` helpers shared by the commands and the UI.

### Changed
- Running `filepumper` with no command on an interactive terminal opens the UI; in scripts it still
  prints help and exits with code 2.
- The Ctrl+C summary line now states that unfinished files were left unchanged.

### Fixed
- A Ctrl+C arriving during the final progress refresh could escape the batch runner as a raw
  `KeyboardInterrupt`; it is now handled like any other interruption.
- Verification no longer relies on an `assert` that is skipped under `python -O`.

### Notes
- The UI is keyboard-only. It is tested without a terminal and through a POSIX pseudo-terminal;
  Windows behavior (via `windows-curses`) is not covered by automated tests.

## [2.0.0] - 2026-10-23

First release of the rebuilt File Pumper.

### Added
- `filepumper pad` with `--add SIZE` and `--target SIZE` (mutually exclusive).
- Generic padding patterns: `zero`, `random` (`os.urandom`) and `repeat` (`--repeat-hex`).
- Transactional writes: temporary file in the destination directory, `fsync`,
  concurrent-modification check, atomic replacement, cleanup on every failure.
- Free-space preflight that accounts for the temporary copy and concurrency.
- Dry-run mode (`--dry-run`) that performs no filesystem writes.
- Optional streamed verification (`--verify`).
- Performance presets (`auto`, `conservative`, `balanced`, `fast`) with
  `--chunk-size` and `--jobs` overrides; one worker by default.
- Safety limits: per-file addition cap (`--max-add`, `--allow-large`) and
  confirmation for large operations (`--yes`).
- Symbolic-link and hard-link policies (`--follow-symlinks`, `--allow-hardlinks`).
- Optional hard-link based backup of the original (`--backup`).
- Progress display, per-file summary, JSON reports (`--report`), structured
  logging (`-v`, `-vv`) and an optional TOML configuration file.
- `filepumper info` and a guided `filepumper interactive` mode.
- Ctrl+C handling that stops scheduling work and removes uncommitted temporary files.

### Deferred
- Recursive directory selection.
- In-place (non-transactional) write mode.
- Format-aware padding extensions.
