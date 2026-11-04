#!/usr/bin/env python3
"""Run the directory search evaluation set (SRI-86) and report recall@3 and MRR.

Live, against a deployed backend (what the nightly workflow runs; free, one
search per case):

    python scripts/search_eval.py --live https://crowdcode-backend.onrender.com
    python scripts/search_eval.py --live URL --min-recall 0.6 --min-mrr 0.6 --json out.json

Offline, over the committed directory snapshot (what CI runs, through
tests/test_search_eval.py):

    python scripts/search_eval.py --offline

Rebuilding the snapshot after the directory or the listing text changes
(needs DATABASE_URL and OPENAI_API_KEY; costs well under a cent):

    python scripts/search_eval.py --snapshot

Exits 1 when a threshold is given and missed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from crowdcode.search_eval import evaluate, load_cases


def live_search(base: str, timeout: float):
    def search(query: str) -> list[str]:
        for attempt in range(3):
            try:
                response = httpx.post(f"{base.rstrip('/')}/api/services/find", json={"query": query, "limit": 10},
                                      timeout=timeout)
                response.raise_for_status()
                return [s["endpoint"] for s in response.json().get("services") or []]
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        return []
    return search


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", metavar="URL", help="a deployed backend, e.g. https://crowdcode-backend.onrender.com")
    mode.add_argument("--offline", action="store_true", help="the committed directory snapshot")
    mode.add_argument("--snapshot", action="store_true", help="rebuild the snapshot from DATABASE_URL")
    parser.add_argument("--min-recall", type=float)
    parser.add_argument("--min-mrr", type=float)
    parser.add_argument("--json", help="write the per-case results here")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep-catalogs", action="store_true", help="--snapshot: refresh only the directory")
    parser.add_argument("--overlay", help="--snapshot: a fill_listing_descriptions.py --report file to use for listings not yet described")
    args = parser.parse_args()

    if args.snapshot:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        from search_eval.snapshot import build_snapshot

        print(json.dumps(build_snapshot(overlay=args.overlay, keep_catalogs=args.keep_catalogs)))
        return 0

    cases = load_cases()
    if args.live:
        search = live_search(args.live, args.timeout)
        started = time.monotonic()
        # A few at a time: the nightly run should not look like load.
        with ThreadPoolExecutor(max_workers=3) as pool:
            ranked = dict(zip((c["query"] for c in cases), pool.map(search, (c["query"] for c in cases))))
        summary = evaluate(cases, lambda q: ranked[q])
        print(f"live: {args.live} in {time.monotonic() - started:.1f}s")
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        from search_eval.snapshot import offline_search

        summary = evaluate(cases, offline_search())

    print(summary.table())
    if args.json:
        Path(args.json).write_text(json.dumps(summary.as_dict(), indent=2), encoding="utf-8")
    failed = []
    if args.min_recall is not None and summary.recall_at_3 < args.min_recall:
        failed.append(f"recall@3 {summary.recall_at_3:.3f} < {args.min_recall}")
    if args.min_mrr is not None and summary.mrr < args.min_mrr:
        failed.append(f"MRR {summary.mrr:.3f} < {args.min_mrr}")
    if failed:
        print("search_eval.regression " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
