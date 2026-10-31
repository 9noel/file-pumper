"""Display-width aware text helpers (wide characters, control characters, truncation)."""

from __future__ import annotations

import unicodedata


def char_width(char: str) -> int:
    """Terminal cells used by one character (0 for combining marks, 2 for wide ones)."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Terminal cells used by ``text``."""
    return sum(char_width(char) for char in text)


def safe_text(text: str) -> str:
    """Make arbitrary file-system text safe to draw: no control characters or lone surrogates."""
    cleaned = []
    for char in text:
        if char.isprintable() and not ("\ud800" <= char <= "\udfff"):
            cleaned.append(char)
        else:
            cleaned.append("?")
    return "".join(cleaned)


_ellipsis = "…"


def set_ellipsis(glyph: str) -> None:
    """Choose the glyph that shortened text ends with (``...`` on ASCII-only terminals).

    The app sets this from the canvas's symbol table before every draw, so the screens do
    not need to pass it around.
    """
    global _ellipsis
    _ellipsis = glyph


def truncate(text: str, width: int, ellipsis: str | None = None) -> str:
    """Cut ``text`` to at most ``width`` cells, ending with ``ellipsis`` when shortened."""
    ellipsis = _ellipsis if ellipsis is None else ellipsis
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    room = width - display_width(ellipsis)
    if room < 0:
        return ellipsis[:width]
    out, used = [], 0
    for char in text:
        step = char_width(char)
        if used + step > room:
            break
        out.append(char)
        used += step
    return "".join(out) + ellipsis


def truncate_left(text: str, width: int, ellipsis: str | None = None) -> str:
    """Like :func:`truncate`, but keeps the end of the text (good for paths)."""
    ellipsis = _ellipsis if ellipsis is None else ellipsis
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    room = width - display_width(ellipsis)
    if room < 0:
        return ellipsis[:width]
    out, used = [], 0
    for char in reversed(text):
        step = char_width(char)
        if used + step > room:
            break
        out.append(char)
        used += step
    return ellipsis + "".join(reversed(out))


def fit(text: str, width: int, align: str = "<", ellipsis: str | None = None) -> str:
    """Truncate and pad ``text`` to exactly ``width`` cells (``<`` left, ``>`` right, ``^``)."""
    text = truncate(text, width, ellipsis)
    gap = max(0, width - display_width(text))
    if align == ">":
        return " " * gap + text
    if align == "^":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def wrap(text: str, width: int) -> list[str]:
    """Wrap ``text`` to ``width`` cells, breaking over-long words. Keeps blank lines."""
    if width < 1:
        return [""]
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            while display_width(word) > width:  # break a word longer than the line
                if current:
                    lines.append(current)
                    current = ""
                piece = truncate(word, width, "")
                lines.append(piece)
                word = word[len(piece) :]
            candidate = word if not current else current + " " + word
            if display_width(candidate) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines
