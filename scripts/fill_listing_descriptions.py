#!/usr/bin/env python3
"""Describe every listing that has no description, from the service's own metadata.

The backend does this by itself: right after a service's first review, at
every start (for listings never looked at), and nightly in the cron
(``crowdcode.listing_metadata``). This is the operator's way to run it over
the whole directory at once and see what it would write.

Dry run by default; ``--apply`` writes. ``--report FILE`` writes one JSON
line per service with what was found.

    python scripts/fill_listing_descriptions.py --report /tmp/listings.jsonl
    python scripts/fill_listing_descriptions.py --apply
    python scripts/fill_listing_descriptions.py --apply --force --id svc_a1f914e1050f89338b32

Requires DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
import sys

from crowdcode.db import connect
from crowdcode.listing_metadata import fill_descriptions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the descriptions (default: dry run)")
    parser.add_argument("--force", action="store_true", help="look again even where a recent check exists")
    parser.add_argument("--id", action="append", dest="ids", help="only this service ID (repeatable)")
    parser.add_argument("--report", help="write one JSON line per service to this file")
    args = parser.parse_args()

    out = open(args.report, "w", encoding="utf-8") if args.report else None

    def report(item: dict) -> None:
        if out:
            out.write(json.dumps(item) + "\n")
        mark = "+" if item["description"] else "-"
        print(f"{mark} {item['service_id']} {item['name']!r}: {(item['description'] or '')[:100]}"
              + (f" [{item['source']}]" if item["source"] else ""))

    with connect() as conn:
        stats = fill_descriptions(conn, service_ids=args.ids, apply=args.apply, force=args.force, report=report)
        conn.commit()
    if out:
        out.close()
    print(("applied: " if args.apply else "dry run: ") + json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
