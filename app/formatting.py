"""Lossless text splitting for Telegram HTML messages."""
from __future__ import annotations

import html


def split_html_text(text: str, max_escaped_length: int = 3400) -> list[str]:
    """Return escaped chunks whose concatenated unescaped text equals the input.

    HTML entities are always kept intact and Telegram's character limit is
    checked *after* escaping. Newlines, spaces and repeated words are preserved.
    """
    if max_escaped_length < 5:
        raise ValueError("max_escaped_length must be at least 5")
    if not text:
        return [""]

    result: list[str] = []
    buffer: list[str] = []
    escaped_length = 0
    for char in text:
        cost = len(html.escape(char, quote=False))
        if buffer and escaped_length + cost > max_escaped_length:
            result.append(html.escape("".join(buffer), quote=False))
            buffer = []
            escaped_length = 0
        buffer.append(char)
        escaped_length += cost
    if buffer:
        result.append(html.escape("".join(buffer), quote=False))
    return result


def format_duration(seconds: float | int) -> str:
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
