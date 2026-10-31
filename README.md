# File Pumper

[![CI](https://github.com/9noel/file-pumper/actions/workflows/ci.yml/badge.svg)](https://github.com/9noel/file-pumper/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![No runtime dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![Linted with Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

File Pumper grows **existing files** to an exact size by appending padding bytes. It is a small,
dependency-free Python tool for creating test fixtures, exercising code that handles files of
different sizes, and benchmarking storage writes. Use it from the command line, or just run
`filepumper` and pick files and settings in a full-screen [terminal UI](#terminal-ui).

For PE/EXE, ZIP/Office, PDF, PNG, JPEG and GIF files, padding is placed **after** the format's own
content so the file keeps opening, running or rendering the same as before — see
[Safety and file-format limitations](#safety-and-file-format-limitations). A `blend` fill pattern
and an honest [`detect`](#detecting-padding-detect) command round out the format-aware side of the
tool.

Writes are **transactional**: the original file is only replaced after a complete, fsynced new copy
exists, so a failure or Ctrl+C before that point leaves your file untouched.

> **Important:** For most file types, File Pumper appends arbitrary bytes with no guarantee the
> file still works. For PE/EXE, ZIP/Office, PDF, PNG, JPEG and GIF it can place padding
> structurally, after the format's own content, so the file still opens the same — but this is not
> a general format-validity guarantee. See
> [Safety and file-format limitations](#safety-and-file-format-limitations).

## Contents

- [Features](#features)
- [Safety and file-format limitations](#safety-and-file-format-limitations)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Terminal UI](#terminal-ui)
- [Examples](#examples)
- [Command reference](#command-reference)
- [Detecting padding (`detect`)](#detecting-padding-detect)
- [Size units](#size-units)
- [How writes work (and what they cost in disk space)](#how-writes-work-and-what-they-cost-in-disk-space)
- [Performance: presets, chunk size and workers](#performance-presets-chunk-size-and-workers)
- [Configuration file](#configuration-file)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [Development and testing](#development-and-testing)
- [Architecture](#architecture)
- [Not implemented](#not-implemented)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Exact amounts:** `--add SIZE` appends exactly `SIZE` bytes; `--target SIZE` grows a file to
  exactly `SIZE` bytes in total. The two are mutually exclusive. Files are never truncated.
- **Four fill patterns:** `zero`, `random` (`os.urandom`), `repeat` (your hex bytes, repeated and
  cut off exactly at the requested length), and `blend` (bytes drawn from each file's own existing
  content, cycled to length — the appended region doesn't read as an obvious flat block of filler
  in a hex/text viewer the way `zero` or `repeat` do).
- **Format-safe placement** for PE/EXE, ZIP/Office, PDF, PNG, JPEG and GIF: File Pumper locates
  where the file's own structure ends and appends after it, so the file still opens, runs or
  renders the same as before. For any other file, or when the structure can't be confidently
  parsed, it falls back to plain appending and says so. See
  [Safety and file-format limitations](#safety-and-file-format-limitations).
- **`detect` command:** checks whether a file carries trailing data beyond what its own format (or
  an optional `--mark` footer) accounts for — on any file, not only ones padded by File Pumper.
- **Optional provenance mark** (`--mark`): writes a small, clearly-labeled 64-byte footer into the
  padding recording the original size, how much was added, when, and with which pattern. It is
  never hidden and is what makes `detect` fully reliable even on unrecognized formats.
- **Multiple files** per run, given as separate arguments (quoting handles spaces). Each is
  validated *before* anything is modified and reported separately.
- **Dry run:** shows current and final sizes, pattern, performance settings, temporary disk-space
  needs and warnings, and changes nothing.
- **Transactional writes** with an `fsync`, a check that the source did not change meanwhile, and
  an atomic replacement. Temporary files are removed on every failure and on Ctrl+C.
- **Free-space preflight** that accounts for the temporary copy and for concurrent files.
- **Optional verification** (`--verify`): re-reads the result and checks the size, that the original
  bytes are an unchanged prefix, and that the appended bytes are the ones that were written.
- **Bounded memory:** data is streamed in chunks; memory use does not depend on the payload size.
- **Safety limits:** a per-file cap on added data (default 10 GiB) and a confirmation prompt for
  large operations, controlled by two separate options.
- **Link policy:** symbolic links and hard-linked files are refused unless you opt in.
- **Optional backup** of the original (`--backup`), progress display, JSON reports, `-v` logging,
  an optional TOML configuration file, and an `info` command.
- **Terminal UI** (`filepumper` or `filepumper ui`): browse folders, tick files, edit settings in a
  form, review the exact plan, watch live progress and cancel safely. It drives the same engine as
  the commands, so every safety guarantee is unchanged.
- Standard library only. No runtime dependencies.

## Safety and file-format limitations

Please read this before pointing the tool at anything you care about.

- **Padding can break files.** Extra bytes can invalidate text encodings and syntax (JSON, XML,
  CSV), databases, media containers, archives, signed or checksummed files, and executables. A PDF
  with bytes after `%%EOF`, or a DOCX/XLSX/PPTX (ZIP) with trailing data, is not guaranteed to open
  as it did before.
- **Format-safe placement, for six recognized formats only.** File Pumper can parse PE/EXE,
  ZIP-family (including Office and other ZIP-based formats), PDF, PNG, JPEG and GIF well enough to
  find where the format's own declared content ends, and appends padding strictly after that point
  — the same region viewers, loaders and `zipfile`/`PIL`-style libraries already treat as optional
  trailing data (an EXE's certificate table, a ZIP comment, data after a PDF's last `%%EOF`, and so
  on). This means the file keeps opening, running or rendering the way it did before, because its
  own structure is untouched. **It is not a guarantee of full format-spec validity** — File Pumper
  does not validate every field of these formats, and a file that was already inconsistent, or an
  edge case the parser doesn't recognize, falls back to "not confirmed safe" rather than a false
  promise; the dry-run plan and `--mark` report show which case applied to each file. Any other
  format, or an unrecognized/malformed file, gets plain appending with no safety claim at all —
  exactly the old behavior. File Pumper makes **no** claim about antivirus, EDR or other
  security-product detection of a padded file either way; see [`detect`](#detecting-padding-detect)
  for what it does check.
- **Test on copies.** Even with transactional writes, a successfully padded file is a different
  file. Keep your originals or use `--backup`.
- **Atomic replacement is not a power-loss guarantee.** The final step uses the operating system's
  atomic rename (`os.replace`), which is atomic for the file name on supported filesystems. The
  new data is fsynced first and, on POSIX, the directory is fsynced afterwards on a best-effort
  basis, but File Pumper does not promise durability across every filesystem, disk cache or crash.
- **Metadata that is and is not kept.** The replacement is a *new file* with your original
  permissions (and ownership when run with enough privilege). The modification time becomes the
  time of the operation. File Pumper does **not** copy ACLs, extended attributes, Windows alternate
  data streams, SELinux labels or hard-link relationships.
- **Remaining race windows.** Before committing, File Pumper compares the source file's identity,
  size, permissions, modification and change times with what it recorded at the start, and aborts
  if anything differs. A change made in the instant between that final check and the rename cannot
  be detected by any portable method.
- **Batches are not atomic.** Files are committed one at a time. If a later file fails, files that
  were already committed stay modified. The summary says so explicitly.
- **Disk-space estimates are estimates.** Other programs, quotas and filesystem overhead can still
  cause a write to fail partway. If that happens the original is kept and the temporary file is
  removed.
- **Platforms.** The automated tests were run on Linux (Python 3.11, 3.12 and 3.13). A CI workflow
  for Linux, Windows and macOS is included in `.github/workflows/ci.yml`. On Windows, replacing a
  file that another program has open fails; File Pumper then reports the failure and keeps the
  original.

## Requirements

- Python **3.11 or newer**.
- Windows, Linux or macOS.
- For the terminal UI: Python's standard `curses` module. It ships with Python on Linux and macOS.
  On Windows run `python -m pip install windows-curses` (or install with the `ui` extra, below).
  The command-line commands work without it.
- No third-party runtime dependencies. `pytest` and `ruff` are optional development tools.

## Installation

```bash
git clone https://github.com/9noel/file-pumper.git
cd file-pumper
python -m pip install -e .
```

On Windows, include the `ui` extra to get the curses package the terminal UI needs:

```bash
python -m pip install -e ".[ui]"
```

To also get the test and lint tools:

```bash
python -m pip install -e ".[dev]"
```

Then check it works:

```bash
filepumper --version
python -m filepumper --version   # equivalent
```

## Quick start

```bash
# See what would happen (changes nothing)
filepumper pad sample.bin --add 100MB --dry-run

# Append exactly 100 MiB of zero bytes
filepumper pad sample.bin --add 100MB

# Grow a file to exactly 500 MiB
filepumper pad sample.bin --target 500MB
```

`MB` means MiB (1024²) throughout; see [Size units](#size-units).

Prefer menus to options? Run `filepumper` with no arguments (on a terminal) to open the
[terminal UI](#terminal-ui).

## Terminal UI

```bash
filepumper                       # opens the UI when run on a terminal
filepumper ui                    # the same, explicitly
filepumper ui sample.bin other.bin   # start with these files already ticked
filepumper ui ~/projects/data    # start in a folder
```

The UI is a four-step flow; the current step is highlighted at the top of the screen.

| Step | What you do |
| --- | --- |
| **1 Files** | Browse folders and tick files. Selections are kept while you move between folders. |
| **2 Settings** | Choose *add* or *target* mode, the size, pattern (including `blend`), preset, chunk size, workers, and the verify / backup / mark / size-limit / link options. Values are checked as you type, with a live summary. |
| **3 Review** | See exactly which files will grow from what size to what size, the disk space needed, which formats were recognized, and any warnings. **Nothing has been written at this point.** |
| **4 Run** | Live progress with throughput and ETA. `Esc` cancels; unfinished files are left unchanged and their temporary files removed. Then a per-file result, with an optional JSON report. |

Runs that add 1 GiB or more in total ask for an explicit `Y` first, exactly like the command line's
confirmation. The size limit and free-space checks are the same ones the commands use.

**Keys** (the footer always shows the ones that apply):

| Screen | Keys |
| --- | --- |
| Files | `↑` `↓` or `j` `k` move; `PgUp` `PgDn` `Home` `End`; `Space` tick and move on; `Enter` or `→` open a folder (or tick a file); `←` or `Backspace` parent folder; `a` tick every file shown; `x` clear all ticks; `/` filter by name; `g` go to a folder, or add a file by path; `.` show hidden files; `r` reload; `s` manage the selection; `Tab` or `n` next step; `q` quit |
| Settings | `↑` `↓`, `Tab` or `Shift+Tab` move between fields; `←` `→` or `Space` change a choice or toggle; type to edit a value; `Ctrl+N` continue; `PgDn` jump to *Continue*; `Esc` back |
| Review | `Enter` start; `↑` `↓` scroll the table; `Esc` back to settings; `q` quit |
| Run | `Esc` (or `c`) cancel |
| Result | `s` save a JSON report (`filepumper-report-DATE-TIME.json`, in the folder you launched File Pumper from); `n` new operation; `q` quit |

**Display.** It needs a terminal of at least 64 × 20 characters (a message is shown if it is
smaller, and it recovers when you enlarge the window); at 100 columns or more a side panel shows
the selection and a plan preview. Colors are used when available; set `NO_COLOR=1` to turn them
off. Box-drawing and block characters are replaced with plain ASCII on the Linux console, in
non-UTF-8 locales, or when `FILEPUMPER_ASCII=1` is set. Mouse input is not supported.

**Exit code.** The UI exits with the worst outcome of the operations you ran in that session, using
the [exit codes](#exit-codes) below (`0` when you quit without running anything), and `130` on
Ctrl+C. It needs an interactive terminal; in scripts, use `filepumper pad`.

## Examples

**Add an exact amount**

```bash
filepumper pad ./example.bin --add 100MB
```

**Expand to a target size** (refused if the file is already larger)

```bash
filepumper pad ./example.bin --target 500MB
```

**Zero padding** (the default pattern), with a larger chunk size

```bash
filepumper pad ./test.bin --add 1GB --pattern zero --chunk-size 4MB
```

**Random padding** from `os.urandom`, verified afterwards

```bash
filepumper pad ./test.bin --add 20MB --pattern random --verify
```

**Repeated pattern.** The hex bytes repeat until the requested length is reached; a final partial
repetition is cut off exactly. With `--add 10B` and `DEADBEEF` the appended bytes are
`DE AD BE EF DE AD BE EF DE AD`.

```bash
filepumper pad ./test.bin --add 10MB --pattern repeat --repeat-hex DEADBEEF
```

**Blend pattern.** Filler is sampled from each file's own existing content (up to the first 64 KiB)
and cycled to length, independently per file in a batch, so it doesn't stand out in a hex/text
viewer the way `zero` or `repeat` do. It still has no effect on whether `detect` can find it —
detection is structural, not a search for what the filler looks like.

```bash
filepumper pad ./app.exe --add 512KB --pattern blend
```

**Provenance mark.** `--mark` writes a small, clearly-labeled footer into the padding (it consumes
part of the requested size, so `--add`/`--target` still land exactly). It is never hidden, and is
what `detect` reads to report the original size, added size, pattern and timestamp precisely, even
on files whose format File Pumper can't parse.

```bash
filepumper pad ./app.exe --add 512KB --pattern blend --mark
```

**Dry run**

```bash
filepumper pad ./test.bin --add 2GB --dry-run
```

Abbreviated real output:

```text
DRY RUN: no files, names, timestamps or permissions will be changed.

Operation  : add 2.00 GiB (2,147,483,648 bytes) to each file
Pattern    : zero (all bytes 0x00)
Performance: preset auto -> balanced, chunk size 1.00 MiB, jobs 1
Verify     : no
Backup     : no
Files      : 1 (1 to modify)

[1/1] test.bin
    current size : 2.93 KiB (3,000 bytes)
    additional   : 2.00 GiB (2,147,483,648 bytes)
    final size   : 2.00 GiB (2,147,486,648 bytes)
    temp space   : ~2.00 GiB for the staged copy (original bytes + payload)

Disk space (estimate for staging, not a guarantee):
    /data: need ~2.00 GiB, free 29.99 GiB -> OK
```

**Dry run on a recognized format, with `--mark`.** A `format` line appears whenever the file's
format is recognized, and a `Mark` line whenever `--mark` is given:

```bash
filepumper pad ./app.exe --add 512KB --pattern blend --mark --dry-run
```

```text
DRY RUN: no files, names, timestamps or permissions will be changed.

Operation  : add 512.00 KiB (524,288 bytes) to each file
Pattern    : blend (filler drawn from each file's own existing content)
Performance: preset auto -> balanced, chunk size 1.00 MiB, jobs 1
Verify     : no
Backup     : no
Mark       : yes (a provenance footer is written into the padding)
Files      : 1 (1 to modify)

[1/1] app.exe
    current size : 1.40 KiB (1,432 bytes)
    additional   : 512.00 KiB (524,288 bytes)
    final size   : 513.40 KiB (525,720 bytes)
    format       : PE executable/DLL (appending stays within the format's own structure)
    temp space   : ~513.40 KiB for the staged copy (original bytes + payload)

Disk space (estimate for staging, not a guarantee):
    /data: need ~513.40 KiB, free 29.99 GiB -> OK
```

**Verification.** `--verify` reads every finished file back once more, so the operation takes
roughly twice as long for the same data.

```bash
filepumper pad ./test.bin --add 20MB --verify
```

**Multiple files, including paths with spaces**

```bash
filepumper pad "./test data/a.bin" "./test data/b.bin" --add 50MB
```

**Run files in parallel** (see [Performance](#performance-presets-chunk-size-and-workers))

```bash
filepumper pad a.bin b.bin c.bin --add 1GB --jobs 2 --chunk-size 1MB
```

**Keep a backup of each original**

```bash
filepumper pad ./test.bin --add 10MB --backup      # leaves ./test.bin.bak
```

**System information**

```bash
filepumper info                 # storage for the current directory
filepumper info /mnt/data --json
```

**Machine-readable report**

```bash
filepumper pad ./test.bin --add 10MB --verify --report report.json
```

**Check whether a file is padded.** Works on any file, not only ones File Pumper padded, and the
same check applies whether or not `--mark` was used:

```bash
filepumper detect ./app.exe
```

```text
[1/1] app.exe
    size         : 513.40 KiB (525,720 bytes)
    format       : PE executable/DLL (overlay/certificate region)
    verdict      : PUMPED, 512.00 KiB beyond the format's own structure (mixed content, 0.19 bits/byte)
    File Pumper mark: original 1.40 KiB (1,432 bytes), added 512.00 KiB (524,288 bytes), pattern blend, at 2026-10-29T00:08:09+00:00

1/1 file(s) show signs of padding
```

**Guided mode** asks a few questions, shows the equivalent `pad` command and defaults to a dry run:

```bash
filepumper interactive
```

## Command reference

`--help` and `--version` print help and the version.

```text
filepumper [--help] [--version] COMMAND ...

COMMAND:
  pad          append padding to one or more existing files
  detect       check whether a file carries padding beyond its own format's content
  info         show platform, CPU and storage information
  interactive  guided prompts that build a 'pad' command
  ui           full-screen terminal UI (also opens when no command is given on a terminal)
```

With no command, File Pumper opens the UI if it is running on an interactive terminal and `curses`
is available; otherwise (scripts, pipes) it prints this help and exits with code 2.

### `filepumper pad PATH... (--add SIZE | --target SIZE) [options]`

| Option | Meaning |
| --- | --- |
| `PATH...` | One or more existing regular files. Each path is a separate argument. Directories are rejected and never scanned. |
| `--add SIZE` | Append exactly `SIZE` bytes to each file. `0B` is a reported no-op (the file is skipped). |
| `--target SIZE` | Grow each file to exactly `SIZE` bytes in total. A file already at `SIZE` is skipped; a larger file is an error. |
| `--pattern {zero,random,repeat,blend}` | Padding bytes. Default `zero`. `blend` samples up to the first 64 KiB of the file's own existing content and cycles it; see [Features](#features). |
| `--repeat-hex HEX` | Bytes for `--pattern repeat`, e.g. `DEADBEEF` or `0xDEADBEEF`. Whole bytes, 1 to 4096 bytes. Valid only with `--pattern repeat`, which requires it. |
| `--mark` | Write a 64-byte provenance footer into the padding (original size, added size, pattern, timestamp). Consumes part of the requested `--add`/`--target` size, so the final size is still exact. Requires at least 64 bytes of padding. Never hidden; read it back with [`filepumper detect`](#detecting-padding-detect). |
| `--preset {auto,conservative,balanced,fast}` | Performance preset. Default `auto`. |
| `--chunk-size SIZE` | I/O chunk size, 4 KiB to 64 MiB. Overrides the preset. |
| `--jobs N` | Number of files processed at once, 1 to 32. Overrides the preset. Default one worker. |
| `--dry-run` | Show the plan, estimates and warnings; change nothing. Needs no confirmation. |
| `--verify` / `--no-verify` | Read each result back and check size, original prefix and appended bytes. |
| `--backup` | Keep the original content as a hard link `FILE.bak` (or `FILE.bak.1`, `.2`, ... if taken). |
| `--max-add SIZE` | Per-file limit on the bytes added. Default 10 GiB. |
| `--allow-large` | Remove the per-file limit. Free-space checks still apply. Cannot be combined with `--max-add`. |
| `--yes`, `-y` | Skip the confirmation prompt (see below). Does **not** lift the per-file limit. |
| `--follow-symlinks` | Modify the target of a symbolic link instead of refusing it. The link itself is kept. |
| `--allow-hardlinks` | Allow files that have other hard links. This name gets a new file; the other names keep the old content. |
| `--report FILE` | Write a JSON report (also for dry runs). |
| `--no-progress` | Do not show progress. |
| `-v`, `--verbose` | Diagnostic logging on stderr: `-v` for info, `-vv` for debug. |
| `--config FILE` / `--no-config` | Read preferences from `FILE`, or ignore the configuration file. |

`--add` and `--target` are mutually exclusive and one of them is required. Invalid or conflicting
options are rejected with exit code 2 and are never silently ignored.

**Two separate safety switches.** `--allow-large` (or `--max-add`) controls the *size limit*.
`--yes` controls the *confirmation prompt*, which appears when a run adds 1 GiB or more in total.
Without `--yes`, a non-interactive run (a script, a pipe) that needs confirmation exits with code 4
instead of guessing. Neither option disables the free-space check, and a confirmation is never
treated as proof that there is enough disk space.

**Link policy.** Symbolic links and files with more than one hard link are refused by default
because replacing them would change what other names point to. Two arguments that resolve to the
same file are also rejected.

### `filepumper detect PATH... [--json]`

See [Detecting padding](#detecting-padding-detect) below for what the check does and does not
cover. `--json` prints a list of per-file objects instead of text. Exit code is `0` unless a `PATH`
could not be read, in which case it is `1` (per-file errors are still reported; the command does
not stop at the first one). `detect` never modifies anything.

### `filepumper info [PATH...] [--json]`

With `--json` the same information is printed as machine-readable JSON. Otherwise it prints the
File Pumper version, platform, Python version, logical CPU count, physical memory, the
configuration file location, a suggested preset, the built-in limits, and total/free/used space
for each `PATH` (default: the current directory). If a path does not exist yet, its nearest
existing parent is measured. Anything that cannot be determined is shown as `unknown`. The
suggested preset is informational, not a benchmark result.

### `filepumper interactive`

Requires a terminal. Asks for paths, the operation, size, pattern, preset, verification and
whether to dry-run, then prints and runs the equivalent `pad` command.

### `filepumper ui [PATH...] [--config FILE | --no-config]`

Opens the [terminal UI](#terminal-ui). Each `PATH` that is an existing file starts ticked; if the
first `PATH` is a folder, the browser starts there. `--config FILE` / `--no-config` behave as for
`pad` and pre-fill the Settings screen from the configuration file. Fails with exit code 2 and a
clear message when there is no interactive terminal or no `curses` module.

## Detecting padding (`detect`)

`filepumper detect PATH...` checks whether a file carries data beyond what its own format accounts
for. It works on any file — not only ones File Pumper padded — which is the point: it is meant to
answer "has this file been padded" as a question about the file, not about this tool's own history
with it.

**How it decides, in order:**

1. **Structural check.** For the six recognized formats (PE/EXE, ZIP-family, PDF, PNG, JPEG, GIF),
   `detect` parses the file the same way `pad` does, to find where the format's own content ends.
   Any bytes after that point are reported as an overlay, along with their size and a rough content
   classification (all one byte value, high entropy / random-looking, printable text, mixed, and so
   on). **This is deliberately not a byte-pattern or signature search** — it does not look for what
   the filler looks like, only for where the content the format itself declares actually ends. That
   is also why `blend` filler does not make a file invisible to `detect`: changing what the padding
   looks like changes nothing about where it sits.
2. **Mark check.** Independently, `detect` looks for File Pumper's own `--mark` footer (a
   CRC-protected 64-byte record), which if present and valid gives the exact original size, added
   size, pattern and timestamp regardless of file format — including on files `detect` cannot
   otherwise classify at all.
3. **Honest "no data" cases.** An unrecognized or malformed file with no `--mark` footer gives
   `detect` no reference point to compare against, so it correctly reports no verdict either way,
   rather than guessing from the file's content alone.

**What this does and does not claim.** `detect` is a structural/provenance check, not an antivirus,
EDR or other security-product detector, and File Pumper makes no claim about whether any particular
security product would or would not flag a given file — that is outside this tool's scope and
intentionally so. `detect` also cannot prove a file was *not* padded by something else entirely
(another tool, a legitimately large resource section, a deliberately crafted file); it can only
report what its own structural and footer checks find.

## Size units

File Pumper uses **binary units only**. A unit is always required, so `100` on its own is an error.

| Unit | Bytes |
| --- | --- |
| `B` | 1 |
| `KB`, `KiB` | 1,024 |
| `MB`, `MiB` | 1,048,576 (1024²) |
| `GB`, `GiB` | 1,073,741,824 (1024³) |
| `TB`, `TiB` | 1,099,511,627,776 (1024⁴) |

Units are case-insensitive. Decimal values such as `1.5GiB` are accepted if they come to a whole
number of bytes. Negative, empty, malformed, non-finite (`inf`, `nan`), unit-less, unknown-unit and
fractional-byte values are rejected, as is anything larger than 2⁶³−1 bytes.

The amount added is exactly the requested number of payload bytes. No header, marker or delimiter
is written.

## How writes work (and what they cost in disk space)

For each file File Pumper:

1. Records the source file's identity (device, inode, size, times, permissions).
2. Creates a temporary file **in the same directory** (`.NAME.XXXX.filepumper.tmp`) so the final
   rename stays on one filesystem.
3. Streams the original bytes into it, then streams the padding after them.
4. Flushes and `fsync`s the temporary file.
5. If `--verify` is set, reads it back and checks it.
6. Re-checks that the source file did not change; if it did, aborts.
7. Copies permissions, optionally links a backup, then atomically replaces the original name.
8. Reports success only after the replacement has happened.

Any failure before step 7 deletes the temporary file and leaves the original untouched. File
Pumper never falls back to an unsafe in-place write.

**Disk space.** Because the original stays in place until the swap, you need room for a complete
extra copy while a file is being processed. The preflight estimates, per filesystem:

- without `--backup`: the sum of all additions, plus the full original size of the largest files
  being processed at the same time (up to `--jobs` of them);
- with `--backup`: the full final size of every file, because the old data is kept.

A dry run prints this estimate. If it is clearly too large, the run stops with exit code 3 before
anything is written.

## Performance: presets, chunk sizes and workers

| Preset | Chunk size | Jobs | Notes |
| --- | --- | --- | --- |
| `conservative` | 256 KiB | 1 | Smallest footprint. |
| `balanced` | 1 MiB | 1 | Good general default. |
| `fast` | 8 MiB | 2 (at most the CPU count) | Larger chunks, two files at once. |
| `auto` (default) | as chosen | as chosen | Picks `balanced`, or `conservative` if the CPU count is unknown or memory is under 1 GiB. |

These are starting points, not benchmark results. `--chunk-size` (4 KiB to 64 MiB) and `--jobs`
(1 to 32) override the preset.

- **Memory is bounded.** Roughly one chunk per active file is held at a time, regardless of how
  much data is added.
- **One worker by default.** More threads do not necessarily mean faster storage writes; on a
  single disk, parallel writers often compete. Try `--jobs 2` and measure.
- **Parallelism is per file.** Each file is one unit of work, and no two workers ever touch the same
  file. A single file is always written by one worker, and the effective job count is never more
  than the number of files that need writing.
- **Progress** shows the current file, bytes done, percentage, elapsed time, throughput and an
  estimated time remaining once there is enough data to estimate it. On a terminal it refreshes
  one line a few times a second; when output is redirected it prints plain lines instead.

## Configuration file

An optional TOML file can set defaults. Command-line options always win. Settings, in order of
precedence: command line, then configuration file, then built-in defaults.

Location (first match): `--config FILE`, the `FILEPUMPER_CONFIG` environment variable, then
`$XDG_CONFIG_HOME/filepumper/config.toml` (or `~/.config/filepumper/config.toml`) on Linux and
macOS, or `%APPDATA%\filepumper\config.toml` on Windows. A missing default file is fine; a missing
file named with `--config` is an error. `--no-config` ignores it.

```toml
preset = "balanced"        # auto | conservative | balanced | fast
chunk_size = "2MiB"        # size string, or an integer number of bytes
jobs = 1
pattern = "zero"           # zero | random | repeat
repeat_hex = "DEADBEEF"    # used when pattern = "repeat"
verify = false
max_add = "10GiB"          # per-file cap on added bytes
```

Unknown keys and wrong value types are rejected with exit code 2. Safety options such as
`--allow-large`, `--yes`, `--backup`, `--follow-symlinks` and `--allow-hardlinks` are deliberately
command-line only.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every file succeeded or was skipped as a no-op. Also: a dry run completed. |
| `1` | At least one file failed or was cancelled, a temporary file could not be removed, or a requested `--report` could not be written. |
| `2` | Invalid usage, options, configuration or input paths. Nothing was modified. |
| `3` | Not enough free disk space for the staged copy (also predicted by a dry run). Nothing was modified. |
| `4` | A large operation was not confirmed: you answered no, or the run was non-interactive without `--yes`. Nothing was modified. |
| `130` | Interrupted with Ctrl+C (or the UI run was cancelled with `Esc`). Uncommitted temporary files were removed. |

## Troubleshooting

| Message (abridged) | What it means and what to do |
| --- | --- |
| `size '100' has no unit` | A unit is required. Use `100B` or `100MB`. |
| `unsupported unit ...` | Use one of `B, KB, KiB, MB, MiB, GB, GiB, TB, TiB`. |
| `argument --target: not allowed with argument --add` | Choose one of `--add` and `--target`. |
| `current size ... is larger than the target ...; File Pumper never truncates files` | `--target` must be at least the current size. |
| `... is a directory` | Name files explicitly. Directories are never scanned. |
| `... is a symbolic link; refused by default` | Add `--follow-symlinks` to modify the link's target, or pass the real path. |
| `... has N hard links ...` | Replacing the file would detach this name from the others. Add `--allow-hardlinks` only if that is acceptable. |
| `... is not writable` / `directory ... is not writable` | The user needs write access to the file and to its directory (for the temporary file). |
| `adding ... exceeds the per-file limit` | Raise it with `--max-add SIZE` or lift it with `--allow-large`. `--yes` does not do this. |
| `--mark needs at least ... bytes of padding` | The provenance footer is 64 bytes; request at least that much with `--add`/`--target`, or drop `--mark`. |
| `blend pattern has no content to sample from` | `--pattern blend` reads from the file's own existing bytes; an empty source file has none to sample. |
| `refusing to add ... without confirmation in a non-interactive run` | Pass `--yes` once you have checked the plan with `--dry-run`. |
| `insufficient free space: ...` | Free up space on the filesystem holding the file, or pad fewer files at once. The estimate includes the temporary copy. |
| `... changed during the operation ... aborted without replacing it` | Something modified the file while it was being copied. Nothing was replaced; run again when the file is idle. |
| `could not replace ...` | The final rename failed (for example the file is open elsewhere on Windows). The original is intact. |
| `no space left on the device` | The disk filled up during the write. The temporary file was removed; the original is intact. |
| `temporary files could not be removed; delete them manually` | The listed `*.filepumper.tmp` files remain; delete them yourself. |
| `could not write the report` | The padding itself completed and is reported; only the JSON report failed. |

| `the terminal UI needs an interactive terminal` | `filepumper ui` was started from a script or pipe. Use `filepumper pad` there. |
| `the terminal UI needs Python's 'curses' module` | Windows only: `python -m pip install windows-curses`. |
| `Terminal too small` | Enlarge the window to at least 64 × 20 characters. |
| Odd boxes or letters in the UI | Use a UTF-8 terminal, or set `FILEPUMPER_ASCII=1` for plain characters. |

Use `-v` for step-by-step logging and `--dry-run` to preview any command.

## Development and testing

```bash
python -m pip install -e ".[dev]"

python -m pytest                      # run the test suite
ruff check src tests                  # lint
ruff format --check src tests         # formatting (use `ruff format src tests` to apply)
python scripts/readme_check.py        # catch CLI options/patterns undocumented in this README
```

The tests use isolated temporary directories, never write more than a few tens of megabytes, and
cover size parsing, every pattern (including partial final repetitions and bounded memory), exact
sizes and prefixes, dry-run no-write guarantees, failure injection (generator errors, full disk,
replacement failure, source modified or replaced mid-run), cleanup, cancellation, concurrency
limits, link policy, verification, JSON reports, configuration, and CLI behavior and exit codes.
The terminal UI is tested without a terminal (screens draw onto an in-memory grid and read scripted
keys, with the real engine writing real temporary files), plus one smoke test that drives
`filepumper ui` through a pseudo-terminal on POSIX systems. Windows UI behavior depends on the
`windows-curses` package and is not covered by the automated tests.
Tests that need a non-root user (read-only file, unwritable directory) are skipped when run as root.

## Architecture

```text
src/filepumper/
├── __main__.py     python -m filepumper
├── cli.py          argparse commands, option precedence, confirmation, exit codes
├── engine.py       plan (read-only) and transactional execution, workers, cancellation
├── patterns.py     streaming byte generators: zero, random, repeat, blend; hex validation
├── formats.py      stdlib-only structural parsers (PE, ZIP, PDF, PNG, JPEG, GIF): find each
│                   format's own content end, so padding can be placed after it
├── marker.py       the opt-in 64-byte provenance footer: pack/unpack, CRC-protected
├── detect.py       the 'detect' command's engine: structural + marker check, content shape
├── sizes.py        size parsing and formatting (binary units)
├── safety.py       path validation, file identity, free-space estimate, backup links
├── config.py       performance presets, limits, optional TOML preferences
├── system_info.py  CPU, memory and storage inspection (unknown rather than guessed)
├── reporting.py    progress, plan/summary text, JSON reports, info/detect output, rate/ETA meter
├── exitcodes.py    exit codes shared by the commands and the UI
├── exceptions.py   dedicated error types
└── tui/            optional curses UI (never imported by the commands)
    ├── app.py              layout, step header, key loop, cached live preview
    ├── screens_files.py    step 1: folder browser and selection
    ├── screens_settings.py step 2: settings form
    ├── screens_run.py      steps 3-4: review, live run, results
    ├── browser.py / form.py  file-browser and settings models (no drawing code)
    ├── canvas.py / keys.py   terminal abstraction: real curses or in-memory test doubles
    └── textutil.py         width-aware truncation and wrapping
tests/              pytest suite (one module per area)
```

The design keeps responsibilities separate:

- **Planning is read-only.** `engine.build_plan` only inspects metadata. A dry run is a plan that is
  never executed, which is why tests can prove it performs no writes.
- **One future per file.** `engine.run_batch` submits one task per file to a thread pool, never one
  per chunk. The calling thread only waits and refreshes progress, which keeps Ctrl+C responsive.
- **Failure boundary.** Each file's transaction runs inside one function that converts every
  outcome into a result, so one file's failure never hides another's.
- **The UI is a thin client.** The screens call `engine.build_plan`, `engine.plan_space` and
  `engine.run_batch` and contain no file-writing code. Drawing goes through a small `Canvas`
  interface and input through a `KeySource`, so tests substitute an in-memory grid and scripted keys.
- **Generic-first, format-aware where it's been verified.** Pattern generators implement a small
  interface and stay format-agnostic. `formats.py` is deliberately separate and narrow: it never
  guesses, never raises on malformed input, and only ever answers "where does this format's own
  content end" — `engine.py` is the only place that decides what to do with that answer.
- **Detection is independent of padding.** `detect.py` does not import `patterns.py` or know how a
  file was padded; it only looks at the file and an optional `--mark` footer. That separation is
  what keeps `blend` from weakening `detect` — the two features cannot become coupled by accident.

## Not implemented

These were considered and are deliberately absent rather than shipped half-finished:

- **Recursive directory selection.** Files must be named explicitly; the UI browses folders but
  only ever ticks individual files.
- **Mouse support in the UI.** It is keyboard-only.
- **In-place (non-transactional) mode.** Every write is transactional.
- **Format awareness beyond placement.** File Pumper locates the safe append point for six formats
  (see [Safety and file-format limitations](#safety-and-file-format-limitations)) but does not
  validate or repair internal structure, update length/offset fields elsewhere in the format,
  re-sign anything, or support any other format. This is intentionally a narrow, verified subset,
  not general format awareness.
- **Antivirus/EDR-evasion tuning of any kind.** `blend` exists to avoid looking like an obvious
  flat block of filler to a human in a hex/text viewer, and `detect` is a structural/provenance
  check — neither is tuned against, tested against, or makes any claim about real security
  products, and that is a deliberate boundary, not a gap to be filled later.
- **Preserving ACLs, extended attributes or alternate data streams.**

## Contributing

Contributions are welcome.

1. Open an issue or describe the change you want to make.
2. Keep changes focused, with tests: new behavior needs a test, and bug fixes need a test that fails
   without the fix.
3. Run `python -m pytest`, `ruff check src tests` and `ruff format --check src tests` before
   submitting.
4. Do not weaken the safety guarantees (original preserved on pre-commit failure, no implicit
   truncation, no silent in-place fallback) and do not add format-safety claims without format-level
   implementation and tests.
5. Keep this README and the `--help` output accurate; do not document features that do not exist.

## License

File Pumper is released under the [MIT License](LICENSE). The copyright line in `LICENSE` names
"File Pumper contributors" generically; replace it with the actual rights holder if you publish or
redistribute this project.
