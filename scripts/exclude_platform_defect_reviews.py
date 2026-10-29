#!/usr/bin/env python3
"""Mark reviews that rate a failure CrowdCode caused, not the provider's.

Three of our own bugs made hosted agents file 1-star reviews against
providers that were never given the chance to succeed or fail. The ratings are
kept verbatim — the mistake was ours and deleting the record of it would hide
it — but they stop counting: `weight_exclusion = 'platform_defect'` takes the
row out of every published count, histogram, average and generated summary
(crowdcode.review_sql), and `exclusion_note` records which defect and why.

Each entry below is checked against the stored row before anything is written.
A review id alone is not enough of a key to retire somebody's rating on: if
the service, rating, reviewing wallet or day does not match what was audited,
the script refuses rather than silently excluding an innocent review.

Dry run by default:

    python scripts/exclude_platform_defect_reviews.py
    python scripts/exclude_platform_defect_reviews.py --apply

Applying clears `services.last_summarized_at` for every affected service, so
the next crowdcode-cron run regenerates their summaries from the reviews that
remain, and recomputes each stored score (a no-op for rows that already
carried no weight, correct if one ever did).

Requires DATABASE_URL and supabase/platform-defect-reviews.sql applied.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from crowdcode.db import connect
from crowdcode.payments import utc_now
from crowdcode.reputation import recompute_service_score
from crowdcode.review_sql import EXCLUSION_PLATFORM_DEFECT


@dataclass(frozen=True)
class Exclusion:
    review_id: int
    service_id: str
    rating: int
    reviewer_wallet: str
    created_on: str  # UTC date, as audited
    note: str


# crowdcode#157 ("Pay the sellers whose 402s this gateway was refusing") and
# crowdcode#160 ("stop refusing listings nobody classified"): the gateway
# misread valid x402/MPP offers and refused to pay, or refused the listing
# outright, so the provider's endpoint was never called. crowdcode#156 ("Let
# an agent spend the money it has on the network the seller wants"): the agent
# held USDC on another network and was not allowed to move it to the one the
# seller asked for, so a Tempo-only service was rated for a wallet balance of
# our own making.
EXCLUSIONS = (
    Exclusion(
        review_id=1251,
        service_id="svc_32473aa6a21e5dc8002a",
        rating=1,
        reviewer_wallet="0x640c38f6366f7c1f9e4e2b8099ad8baaaabb47f4",
        created_on="2026-10-28",
        note="crowdcode#157: the gateway rejected this provider's valid x402 "
             "offer as invalid payment requirements, so the request never ran",
    ),
    Exclusion(
        review_id=1257,
        service_id="svc_32473aa6a21e5dc8002a",
        rating=1,
        reviewer_wallet="0x640c38f6366f7c1f9e4e2b8099ad8baaaabb47f4",
        created_on="2026-10-28",
        note="crowdcode#157: the purchase failed inside our gateway while it "
             "could not settle this provider's valid x402 offer; the provider "
             "returned no response to rate",
    ),
    Exclusion(
        review_id=1252,
        service_id="svc_07f727230e5563b1ac19",
        rating=1,
        reviewer_wallet="0x640c38f6366f7c1f9e4e2b8099ad8baaaabb47f4",
        created_on="2026-10-28",
        note="crowdcode#157: the gateway called this provider's payment "
             "requirements unsupported and refused to pay, so no music was "
             "ever requested",
    ),
    Exclusion(
        review_id=1258,
        service_id="svc_07f727230e5563b1ac19",
        rating=1,
        reviewer_wallet="0x640c38f6366f7c1f9e4e2b8099ad8baaaabb47f4",
        created_on="2026-10-28",
        note="crowdcode#160: our own reputation floor refused the listing "
             "before payment because nobody had classified it; the provider "
             "was never called",
    ),
    Exclusion(
        review_id=1256,
        service_id="svc_f71c1dbf0f78a9fe118b",
        rating=1,
        reviewer_wallet="0xe841f8ff1502164b57fdd46810cdc4b3d5fec881",
        created_on="2026-10-28",
        note="crowdcode#156: the agent held USDC on Base and Solana but could "
             "not spend it on Tempo, the only network this provider accepts; "
             "the empty Tempo balance was ours, not a provider fault",
    ),
)


def main_with(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the exclusions (default: report what would change)",
    )
    args = parser.parse_args(argv)

    now = utc_now()
    with connect() as conn:
        rows = {
            row["id"]: row
            for row in conn.execute(
                """
                select id, service_id, rating, reviewer_wallet, created_at,
                       weight_exclusion, exclusion_note
                from reviews
                where id = any(%s)
                """,
                ([e.review_id for e in EXCLUSIONS],),
            ).fetchall()
        }

        planned: list[Exclusion] = []
        mismatched: list[str] = []
        for entry in EXCLUSIONS:
            row = rows.get(entry.review_id)
            if row is None:
                mismatched.append(f"review {entry.review_id}: not found")
                continue
            actual = (
                row["service_id"],
                int(row["rating"]),
                (row["reviewer_wallet"] or "").lower(),
                row["created_at"].date().isoformat(),
            )
            expected = (
                entry.service_id,
                entry.rating,
                entry.reviewer_wallet.lower(),
                entry.created_on,
            )
            if actual != expected:
                mismatched.append(
                    f"review {entry.review_id}: stored {actual} does not match "
                    f"the audited {expected}"
                )
                continue
            if row["weight_exclusion"] == EXCLUSION_PLATFORM_DEFECT:
                print(f"review {entry.review_id}: already excluded; skipping")
                continue
            planned.append(entry)
            print(
                f"review {entry.review_id} ({entry.service_id}, {entry.rating}*, "
                f"{entry.created_on}): {row['weight_exclusion']} -> "
                f"{EXCLUSION_PLATFORM_DEFECT}\n    {entry.note}"
            )

        if mismatched:
            for problem in mismatched:
                print(f"REFUSING: {problem}", file=sys.stderr)
            return 1

        if not planned:
            print("nothing to do")
            return 0
        if not args.apply:
            print(f"\ndry run: {len(planned)} review(s) would be excluded; "
                  "rerun with --apply")
            return 0

        for entry in planned:
            conn.execute(
                """
                update reviews
                set weight_exclusion = %s, exclusion_note = %s
                where id = %s
                """,
                (EXCLUSION_PLATFORM_DEFECT, entry.note, entry.review_id),
            )
        services = sorted({entry.service_id for entry in planned})
        # The summary is generated from the reviews that count, so it has to be
        # regenerated rather than edited: clearing the watermark is what makes
        # the next cron run pick these services up.
        conn.execute(
            "update services set last_summarized_at = null where id = any(%s)",
            (services,),
        )
        for service_id in services:
            result = recompute_service_score(conn, service_id, now)
            print(
                f"{service_id}: score {result.score:.6f} n_eff {result.n_eff:.6f} "
                "(summary queued for regeneration)"
            )
        conn.commit()
        print(f"\nexcluded {len(planned)} review(s) across {len(services)} service(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_with())
