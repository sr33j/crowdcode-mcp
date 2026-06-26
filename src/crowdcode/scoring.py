from __future__ import annotations

from decimal import Decimal
from typing import Any


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def confidence_for_count(num_reviews: int) -> str:
    return "high" if num_reviews >= 5 else "low"

