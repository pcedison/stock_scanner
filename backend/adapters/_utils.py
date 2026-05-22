from __future__ import annotations

from typing import Any

_NULL_TOKENS = frozenset({"-", "--", "NA", "N/A", "null", "None"})


def to_float(value: Any, *, parenthesized_negative: bool = False) -> float | None:
    """Parse a loosely-typed numeric string to float.

    Args:
        parenthesized_negative: if True, treat ``(123)`` as ``-123``.
            Used for MOPS financial statements which encode negatives this way.
    """
    text = "" if value is None else str(value).replace(",", "").strip()
    if not text or text in _NULL_TOKENS:
        return None
    if parenthesized_negative and text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None
