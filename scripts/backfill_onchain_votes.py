#!/usr/bin/env python3
"""Scoring v4 (docs/SCORING.md, SRI-118): weigh the paid reviews already
verified on chain, and republish every score.

Run once after supabase/scoring-v4.sql is applied and the v4 backend and cron
are deployed. Dry run by default:

    python scripts/backfill_onchain_votes.py
    python scripts/backfill_onchain_votes.py --apply

--apply:

1. gives every community review that verified a payment on chain the weight
   the write path now gives a new one: `weight_basis = 'onchain'`, or
   `weight_exclusion = 'self_dealing' / 'dust_payment'` under the same guards
   (crowdcode.reputation.onchain_weight_exclusion);
2. resets the services nobody has reviewed and nothing has failed for to the
   v4 "unrated" values in one statement (score 2.5, the sort key), rather than
   leaving tens of thousands of single-row updates to the sweep;
3. runs the consistency sweep, which republishes every score from history;
4. recomputes reliability-v2, whose one score is now the review score.

Requires DATABASE_URL.
"""

from __future__ import annotations

import argparse

from crowdcode import cron, reliability
from crowdcode.db import connect
from crowdcode.payments import utc_now
from crowdcode.reputation import onchain_weight_exclusion
from crowdcode.review_management import lock_review_writes
from crowdcode.settings import get_settings

CANDIDATES = """
    select id, service_id, reviewer_wallet, payment_target_ref, amount, rating
    from reviews
    where weight_basis = 'none' and verified_source = 'community'
      and weight_exclusion = 'community'
      and payment_verification_level = 'onchain_verified'
      and payment_verified and signature_verified and reviewer_wallet is not null
    order by id
"""

UNRATED = """
    update services s
    set score = 2.5, score_mean = 3.0, p_above_3 = 0.5, avg_rating_weighted = null,
        score_alpha = 1, score_beta = 1, n_eff = 0, n_eff_legacy = 0, n_eff_hosted = 0,
        n_eff_onchain = 0, n_eff_failures = 0, score_updated_at = %s
    where s.score_mean is null
      and not exists (select 1 from reviews r where r.service_id = s.id)
      and not exists (select 1 from index_resources ir join index_observations o on o.resource_id = ir.id
                      where ir.service_id = s.id and o.kind = 'purchase' and not o.ok)
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is changed")
    args = parser.parse_args()
    minimum = get_settings().min_weighted_amount_atomic
    now = utc_now()
    with connect() as conn:
        lock_review_writes(conn)
        rows = conn.execute(CANDIDATES).fetchall()
        changes = []
        for row in rows:
            amount = int(row["amount"]) if row["amount"] is not None else None
            exclusion = onchain_weight_exclusion(
                conn, wallet=row["reviewer_wallet"], payee=row["payment_target_ref"],
                amount=amount, min_amount_atomic=minimum,
            )
            changes.append((row, exclusion))
            print(f"review {row['id']} {row['service_id']} rating {row['rating']} "
                  f"-> {'onchain vote' if exclusion is None else exclusion}")
        if not args.apply:
            print(f"{len(changes)} review(s) would change; dry run, nothing written")
            conn.rollback()
            return
        for row, exclusion in changes:
            if exclusion is None:
                conn.execute("update reviews set weight_basis = 'onchain', weight_exclusion = null where id = %s",
                             (row["id"],))
            else:
                conn.execute("update reviews set weight_exclusion = %s where id = %s", (exclusion, row["id"]))
        reset = conn.execute(UNRATED, (now,)).rowcount
        conn.commit()
        print(f"{len(changes)} review(s) reclassified; {reset} unreviewed service(s) set to unrated")
    cron.run_consistency_sweep(utc_now())
    print("consistency sweep done")
    print("reliability:", reliability.recompute(connect, utc_now()))


if __name__ == "__main__":
    main()
