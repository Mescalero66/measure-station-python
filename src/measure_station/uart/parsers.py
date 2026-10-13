"""Line protocols for data arriving from external microcontrollers."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


class ParseError(ValueError):
    """A line could not be decoded into measurement values."""


def parse_json(line: str) -> dict[str, Any]:
    """``{"temp_c": 21.4, "rh_pct": 40.1}`` -> dict."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc}") from None
    if not isinstance(obj, dict) or not obj:
        raise ParseError("expected a non-empty JSON object")
    return obj


def parse_key_value(line: str) -> dict[str, Any]:
    """``temp_c=21.4,rh_pct=40.1,state=ok`` -> dict, coercing numeric values."""
    values: dict[str, Any] = {}
    for part in line.split(","):
        key, sep, raw = part.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key:
            raise ParseError(f"malformed pair {part!r}")
        values[key] = _coerce(raw)
    if not values:
        raise ParseError("no key=value pairs")
    return values


def _coerce(raw: str) -> int | float | str:
    for convert in (int, float):
        try:
            return convert(raw)
        except ValueError:
            pass
    return raw


PARSERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "json": parse_json,
    "kv": parse_key_value,
}
