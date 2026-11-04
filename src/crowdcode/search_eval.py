"""Scoring the directory search against its evaluation set (SRI-86).

`tests/search_eval/cases.json` holds real intents and the services that should
answer them. A case is scored on the ranked endpoints a search returned:

- **recall@3**: the share of ``min(3, len(expected))`` expected services that
  appear in the top three;
- **reciprocal rank**: ``1 / rank`` of the first expected service (0 if none
  is returned at all), averaged into MRR;
- **hit@3**: whether any expected service is in the top three.

`tests/test_search_eval.py` runs the set offline over a directory snapshot
in CI; `scripts/search_eval.py --live` runs it against production nightly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

CASES_PATH = Path(__file__).resolve().parents[2] / "tests" / "search_eval" / "cases.json"
K = 3


@dataclass
class CaseResult:
    id: str
    query: str
    expected: list[str]
    ranked: list[str]

    @property
    def first_rank(self) -> int | None:
        for position, endpoint in enumerate(self.ranked, start=1):
            if endpoint in self.expected:
                return position
        return None

    @property
    def recall(self) -> float:
        found = sum(1 for endpoint in self.ranked[:K] if endpoint in self.expected)
        return found / min(K, len(self.expected))

    @property
    def reciprocal_rank(self) -> float:
        rank = self.first_rank
        return 1.0 / rank if rank else 0.0

    @property
    def hit(self) -> bool:
        rank = self.first_rank
        return rank is not None and rank <= K


@dataclass
class Summary:
    results: list[CaseResult]

    @property
    def recall_at_3(self) -> float:
        return sum(r.recall for r in self.results) / len(self.results)

    @property
    def mrr(self) -> float:
        return sum(r.reciprocal_rank for r in self.results) / len(self.results)

    @property
    def hit_at_3(self) -> float:
        return sum(1 for r in self.results if r.hit) / len(self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cases": len(self.results),
            "recall_at_3": round(self.recall_at_3, 4),
            "mrr": round(self.mrr, 4),
            "hit_at_3": round(self.hit_at_3, 4),
            "per_case": [
                {"id": r.id, "first_rank": r.first_rank, "recall": round(r.recall, 3), "top3": r.ranked[:K]}
                for r in self.results
            ],
        }

    def table(self) -> str:
        lines = [f"{'case':28} {'rank':>4} {'r@3':>5}  top 3"]
        for r in self.results:
            lines.append(f"{r.id[:28]:28} {r.first_rank or '-':>4} {r.recall:5.2f}  "
                         + " | ".join(e.split('://', 1)[-1][:40] for e in r.ranked[:K]))
        lines.append(f"recall@3 {self.recall_at_3:.3f}   MRR {self.mrr:.3f}   hit@3 {self.hit_at_3:.3f}   "
                     f"({len(self.results)} cases)")
        return "\n".join(lines)


def load_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def evaluate(cases: Iterable[dict[str, Any]], search: Callable[[str], list[str]]) -> Summary:
    """``search(query)`` returns ranked endpoints, best first."""
    return Summary([
        CaseResult(case["id"], case["query"], list(case["expected"]), list(search(case["query"])))
        for case in cases
    ])
