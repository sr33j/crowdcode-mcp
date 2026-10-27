"""Service demand: the request_service v2 contract and the privacy guards
shared by everything CrowdCode publishes from stored free text.

A request is four fields an agent fills in concretely: the input the user will
give, the exact output they want back, the problem it solves, and what they
would pay per call in USD. Raw requests and raw reviews stay private; what is
published is generated from them (service proposals, review summaries), and
`copies_source` is the check that generated text did not just repeat them.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any

# Field bounds. Twenty characters is roughly the shortest input or output that
# still names a concrete thing ("a public MP4 URL"); the upper bounds keep a
# request to what a builder can read at a glance.
TEXT_MIN_CHARS = 20
TEXT_MIN_WORDS = 4
TEXT_MAX_CHARS = {"input": 600, "output": 600, "problem": 800}
PRICE_MAX_USD = 100.0

FIELD_HINTS = {
    "input": (
        "input is the clear input the user will give the service, for example "
        "'a public MP4 URL (up to 60 s) and the post text'"
    ),
    "output": (
        "output is the exact deliverable they want back, for example "
        "'the published post ID and URL, posted exactly once'"
    ),
    "problem": (
        "problem is what it solves, for example 'agents publishing video to X "
        "fail on chunked uploads and cannot tell whether the post went out'"
    ),
}

_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class DemandFields:
    input: str
    output: str
    problem: str
    price_per_call: float


def _clean_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", value).strip() if isinstance(value, str) else ""


def validate_request_fields(
    input: Any, output: Any, problem: Any, price_per_call: Any
) -> tuple[DemandFields | None, str | None]:
    """Normalize the four request fields, or say exactly what to fix."""
    texts = {"input": _clean_text(input), "output": _clean_text(output),
             "problem": _clean_text(problem)}
    for field, text in texts.items():
        if not text:
            return None, f"{field} is required: {FIELD_HINTS[field]}"
        if len(text) < TEXT_MIN_CHARS or len(_WORD_RE.findall(text.lower())) < TEXT_MIN_WORDS:
            return None, (
                f"{field} is too vague ({len(text)} characters): {FIELD_HINTS[field]}"
            )
        if len(text) > TEXT_MAX_CHARS[field]:
            return None, f"{field} must be at most {TEXT_MAX_CHARS[field]} characters"
    if texts["input"].lower() == texts["output"].lower():
        return None, "input and output must differ: say what goes in and what comes back"

    if isinstance(price_per_call, bool) or not isinstance(price_per_call, (int, float, str)):
        return None, "price_per_call is required: what you would pay per call in USD, for example 0.05"
    try:
        price = float(price_per_call)
    except ValueError:
        return None, "price_per_call must be a number of USD per call, for example 0.05"
    if not math.isfinite(price) or price <= 0:
        return None, "price_per_call must be more than 0 USD"
    if price > PRICE_MAX_USD:
        return None, f"price_per_call must be at most {PRICE_MAX_USD:g} USD per call"
    return DemandFields(price_per_call=round(price, 6), **texts), None


# --- privacy guard ---------------------------------------------------------

# Eight consecutive words in common is a copied sentence, not a shared idea:
# generated text that shares that much with any private source is discarded.
COPY_SHINGLE_WORDS = 8


def _shingles(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


class SourceIndex:
    """The private texts generated output must not repeat."""

    def __init__(self, sources: list[str | None], n: int = COPY_SHINGLE_WORDS):
        self.n = n
        self._shingles: set[tuple[str, ...]] = set()
        for source in sources:
            if source:
                self._shingles |= _shingles(source, n)

    def copies(self, text: str | None) -> bool:
        return bool(text) and not self._shingles.isdisjoint(_shingles(text, self.n))


def copies_source(text: str | None, sources: list[str | None]) -> bool:
    return SourceIndex(sources).copies(text)


# --- willingness to pay ----------------------------------------------------

PRICE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<$0.01", 0.0, 0.01),
    ("$0.01–0.05", 0.01, 0.05),
    ("$0.05–0.25", 0.05, 0.25),
    ("$0.25–1", 0.25, 1.0),
    ("$1+", 1.0, math.inf),
)


def price_distribution(prices: list[float]) -> dict[str, Any] | None:
    """Median willingness to pay and its spread, over stated per-call prices."""
    values = sorted(float(p) for p in prices if p is not None and float(p) > 0)
    if not values:
        return None
    if len(values) >= 2:
        p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
    else:
        p25 = p75 = values[0]
    return {
        "n": len(values),
        "median": round(statistics.median(values), 6),
        "p25": round(p25, 6),
        "p75": round(p75, 6),
        "min": values[0],
        "max": values[-1],
        "buckets": [
            {"label": label, "n": sum(1 for v in values if lo <= v < hi)}
            for label, lo, hi in PRICE_BUCKETS
        ],
    }
