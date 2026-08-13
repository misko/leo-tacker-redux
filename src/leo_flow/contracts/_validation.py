"""Small validation helpers shared by public contract value objects."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def require_token(value: str, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} must be a non-empty portable token")
    return value


def require_nonnegative(value: float, field: str) -> None:
    require_finite(value, field)
    if value < 0:
        raise ValueError(f"{field} must be non-negative")


def require_positive(value: float, field: str) -> None:
    require_finite(value, field)
    if value <= 0:
        raise ValueError(f"{field} must be positive")


def require_finite(value: float, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be finite")


def require_utc_ns(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative UTC nanosecond integer")


def utc_datetime_to_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("datetime must be explicitly UTC")
    return int(value.timestamp() * 1_000_000_000)


def freeze_mapping(value: Mapping[str, Any], field: str) -> tuple[tuple[str, Any], ...]:
    """Return a recursively immutable, key-sorted representation."""
    result: list[tuple[str, Any]] = []
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{field} keys must be strings")
        result.append((key, freeze_value(item, field)))
    result.sort(key=lambda pair: pair[0])
    return tuple(result)


def freeze_value(value: Any, field: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        require_finite(value, field)
        return 0.0 if value == 0.0 else value
    if isinstance(value, Mapping):
        return freeze_mapping(value, field)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_value(item, field) for item in value)
    raise TypeError(f"{field} contains unsupported value {type(value).__name__}")


def thaw_value(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {key: thaw_value(item) for key, item in value}
        return [thaw_value(item) for item in value]
    return value
