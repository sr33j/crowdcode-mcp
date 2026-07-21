"""Backfill redaction for rows written before server-side enforcement.

Streams reviews and service_requests rows with redacted_at IS NULL through
the redaction sidecar (REDACTOR_URL must be set) and rewrites their free-text
columns in place, stamping redacted_at for idempotency. For reviews, the
original reason hash is preserved in payment_proof metadata as
original_reason_hash so historical signatures remain auditable.

Usage:
  python scripts/backfill_redaction.py [--dry-run] [--batch-size 50]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from psycopg.types.json import Jsonb  # noqa: E402

from crowdcode.db import connect  # noqa: E402
from crowdcode.payments import reason_hash  # noqa: E402
from crowdcode.redaction import RedactionUnavailable, redact_texts  # noqa: E402


def backfill_reviews(batch_size: int, dry_run: bool) -> int:
    total = 0
    while True:
        with connect() as conn:
            rows = conn.execute(
                """
                select id, reason, task_context, payment_proof
                from reviews
                where redacted_at is null
                order by id
                limit %s
                """,
                (batch_size,),
            ).fetchall()
            if not rows:
                return total

            texts: list[str | None] = []
            for row in rows:
                texts.append(row["reason"])
                texts.append(row["task_context"])
            redacted = redact_texts(texts, fail_closed=True)
            assert redacted is not None

            for index, row in enumerate(rows):
                new_reason = redacted[index * 2] or row["reason"]
                new_context = redacted[index * 2 + 1]
                if dry_run:
                    if new_reason != row["reason"]:
                        print(f"reviews #{row['id']}: {row['reason']!r} -> {new_reason!r}")
                    continue
                proof = dict(row["payment_proof"] or {})
                proof.setdefault("original_reason_hash", reason_hash(row["reason"]))
                conn.execute(
                    """
                    update reviews
                    set reason = %s, task_context = %s, payment_proof = %s,
                        redacted_at = now()
                    where id = %s
                    """,
                    (new_reason, new_context, Jsonb(proof), row["id"]),
                )
            if not dry_run:
                conn.commit()
            total += len(rows)
            if dry_run:
                return total


def backfill_requests(batch_size: int, dry_run: bool) -> int:
    total = 0
    while True:
        with connect() as conn:
            rows = conn.execute(
                """
                select id, service_description, task_context
                from service_requests
                where redacted_at is null
                order by id
                limit %s
                """,
                (batch_size,),
            ).fetchall()
            if not rows:
                return total

            texts: list[str | None] = []
            for row in rows:
                texts.append(row["service_description"])
                texts.append(row["task_context"])
            redacted = redact_texts(texts, fail_closed=True)
            assert redacted is not None

            for index, row in enumerate(rows):
                new_description = redacted[index * 2] or row["service_description"]
                new_context = redacted[index * 2 + 1]
                if dry_run:
                    if new_description != row["service_description"]:
                        print(
                            f"service_requests #{row['id']}: "
                            f"{row['service_description']!r} -> {new_description!r}"
                        )
                    continue
                conn.execute(
                    """
                    update service_requests
                    set service_description = %s, task_context = %s,
                        redacted_at = now()
                    where id = %s
                    """,
                    (new_description, new_context, row["id"]),
                )
            if not dry_run:
                conn.commit()
            total += len(rows)
            if dry_run:
                return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    try:
        reviews = backfill_reviews(args.batch_size, args.dry_run)
        requests = backfill_requests(args.batch_size, args.dry_run)
    except RedactionUnavailable as exc:
        print(f"redaction sidecar unavailable, aborting without changes: {exc}")
        sys.exit(1)

    mode = "would process" if args.dry_run else "processed"
    print(f"{mode} {reviews} reviews and {requests} service_requests")


if __name__ == "__main__":
    main()
