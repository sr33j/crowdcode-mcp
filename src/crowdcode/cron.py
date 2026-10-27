"""crowdcode-cron: nightly batch entrypoint (docs/SCORING.md §8.2).

Jobs run in dependency order, each isolated so one failure does not block the
rest; the process exits nonzero if any job failed:

1. Payment re-verification — reviews whose tx-hash on-chain check failed only
   because the RPC was unreachable at submit time are re-checked and upgraded
   to onchain_verified when the transfer now verifies (a network flake must
   not permanently cost the verified multiplier).
2. Consistency sweep — recompute all owner trust and service scores from
   scratch by replaying one owner/service event at a time, log drift, and
   write the authoritative values. The first run after applying the schema
   DDL doubles as the data migration that backfills owners and stored scores.
3. Per-service review summaries (LLM), watermarked on last_summarized_at.
   Summaries written under an older SUMMARY_VERSION are regenerated.
4. Service proposals (LLM): service requests mapped and grouped into the
   proposals the website publishes (crowdcode.proposals), in app_cache.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from crowdcode.db import connect
from crowdcode.payments import (
    FAILURE_RPC_UNREACHABLE,
    LEVEL_ONCHAIN_VERIFIED,
    LEVEL_SIGNATURE_ONLY,
    TX_HASH_RE,
    check_payment_reference_onchain,
    utc_now,
)
from crowdcode.redaction import BATCH_TIMEOUT as REDACTION_BATCH_TIMEOUT
from crowdcode.redaction import RedactionUnavailable, redact_texts, redaction_enabled
from crowdcode.reputation import (
    ensure_owner,
    ensure_user,
    load_legacy_trust,
    recompute_service_score,
    sync_seed_owners,
)
from crowdcode.scoring import (
    ReviewRow,
    TrustRow,
    aggregate_owner_reviews,
    compute_score,
    compute_trust_consensus,
    review_weight,
    updated_raw_trust,
)
from crowdcode.settings import get_settings
from crowdcode import proposals
from crowdcode.summaries import SUMMARY_VERSION, summarize_service_reviews

DRIFT_TOLERANCE = 0.01
SUMMARY_MAX_REVIEWS = 50

# rpc_unreachable re-verification window: retry nightly for up to 14 days /
# 5 attempts, then give up (the review keeps its signature_only weight).
REVERIFY_MAX_AGE_DAYS = 14
REVERIFY_MAX_ATTEMPTS = 5


def run_payment_reverification(now: datetime) -> None:
    """Upgrade reviews stuck at signature_only by an unreachable RPC.

    Only rows whose recorded verification_failure is rpc_unreachable are
    candidates — every other failure means the chain was consulted and
    definitively said no.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            select id, service_id, payment_provider, payment_target_ref,
                   reviewer_wallet, payment_reference_canonical,
                   payment_verification_metadata
            from reviews
            where payment_verification_level = %s
              and payment_verification_metadata->>'verification_failure' = %s
              and coalesce((payment_verification_metadata->>'reverify_attempts')::int, 0) < %s
              and created_at > %s - make_interval(days => %s)
              and reviewer_wallet is not null
            order by id
            """,
            (
                LEVEL_SIGNATURE_ONLY,
                FAILURE_RPC_UNREACHABLE,
                REVERIFY_MAX_ATTEMPTS,
                now,
                REVERIFY_MAX_AGE_DAYS,
            ),
        ).fetchall()

        from crowdcode.review_management import lock_review_writes
        lock_review_writes(conn)
        upgraded = 0
        touched_services: set[str] = set()
        for row in rows:
            tx_hash = (row["payment_reference_canonical"] or "").strip()
            if not TX_HASH_RE.match(tx_hash):
                continue
            meta = dict(row["payment_verification_metadata"] or {})
            # Re-check on the chain recorded at submit time (x402 Base or
            # Monad); rows from before multi-chain x402 default to Base.
            check, chain_meta = check_payment_reference_onchain(
                provider=row["payment_provider"],
                payment_target_ref=row["payment_target_ref"],
                reviewer_wallet=row["reviewer_wallet"],
                tx_hash=tx_hash,
                network=meta.get("network"),
            )
            attempts = int(meta.get("reverify_attempts") or 0) + 1
            meta["reverify_attempts"] = attempts
            meta["last_reverified_at"] = now.isoformat()

            if check.ok and check.transfer is not None:
                meta["verification_failure"] = None
                meta["upgraded_by"] = "cron_reverification"
                conn.execute(
                    """
                    update reviews
                    set payment_verification_level = %s,
                        payment_verified = true,
                        payment_verified_at = %s,
                        amount = coalesce(amount, %s),
                        payment_verification_metadata = %s
                    where id = %s
                    """,
                    (
                        LEVEL_ONCHAIN_VERIFIED,
                        now,
                        check.transfer["value"],
                        Jsonb(meta),
                        row["id"],
                    ),
                )
                touched_services.add(row["service_id"])
                upgraded += 1
            else:
                # A definitive chain answer replaces rpc_unreachable so the
                # row stops being a retry candidate; another unreachable
                # attempt just burns one of the capped retries.
                meta["verification_failure"] = chain_meta["verification_failure"]
                conn.execute(
                    "update reviews set payment_verification_metadata = %s where id = %s",
                    (Jsonb(meta), row["id"]),
                )

        for service_id in sorted(touched_services):
            recompute_service_score(conn, service_id, now)
        conn.commit()
    print(
        f"reverify: {len(rows)} candidate(s), {upgraded} upgraded, "
        f"{len(touched_services)} service score(s) refreshed"
    )


def run_consistency_sweep(now: datetime) -> None:
    from crowdcode.review_management import lock_review_writes
    with connect() as conn:
        lock_review_writes(conn)
        replay_scores(conn, now)
        conn.commit()


def replay_scores(conn: Any, now: datetime) -> None:
    """Replay trust and scores inside the caller's transaction, including deletion."""
    rows = conn.execute(
        """
        select id, service_id, reviewer_wallet, rating, payment_verified,
               signature_verified, payment_verification_level,
               created_at, user_id, owner_ref, weight_exclusion, weight_basis
        from reviews
        order by created_at asc, id asc
        """
    ).fetchall()

    # Backfill users + reviews.user_id from history (idempotent).
    wallet_user_ids: dict[str, int] = {}
    for row in rows:
        wallet = row["reviewer_wallet"]
        if wallet and wallet not in wallet_user_ids:
            wallet_user_ids[wallet] = ensure_user(conn, wallet)["user_id"]
    backfilled = 0
    for row in rows:
        wallet = row["reviewer_wallet"]
        if wallet and row["user_id"] is None:
            conn.execute(
                "update reviews set user_id = %s where id = %s",
                (wallet_user_ids[wallet], row["id"]),
            )
            backfilled += 1
    if backfilled:
        logging.getLogger(__name__).info(f"sweep: backfilled user_id on {backfilled} review(s)")

    # Every owner named by a weighted review exists in review_owners (idempotent).
    for row in rows:
        if row["owner_ref"] and row["weight_exclusion"] is None:
            ensure_owner(conn, row["owner_ref"])

    # The frozen pre-cutover wallet trust. The sweep reads it and never writes
    # it: legacy scores have to reproduce, so the snapshot cannot move.
    legacy_trust = load_legacy_trust(conn)

    owners = conn.execute(
        "select owner_ref, raw_trust, is_seed, slashed_at from review_owners"
    ).fetchall()
    pinned = {
        o["owner_ref"]: TrustRow(
            raw_trust=float(o["raw_trust"]),
            is_seed=bool(o["is_seed"]),
            slashed=o["slashed_at"] is not None,
        )
        for o in owners
    }

    # Replay: recompute every non-seed owner's raw trust from scratch. One
    # owner's weighted reviews of one service form a single trust event,
    # matching the one-vote-per-owner scoring cap.
    raw: dict[str, float] = {}
    trust: dict[str, TrustRow] = {}
    for owner_ref, row in pinned.items():
        if row.is_seed or row.slashed:
            trust[owner_ref] = row
        else:
            raw[owner_ref] = 0.0
            trust[owner_ref] = TrustRow(raw_trust=0.0)

    events: dict[tuple[str, str], list[ReviewRow]] = {}
    event_order: dict[tuple[str, str], tuple[datetime, int]] = {}
    per_service: dict[str, list[ReviewRow]] = {}
    for row in rows:
        review = ReviewRow(
            wallet=row["reviewer_wallet"],
            rating=int(row["rating"]),
            payment_verified=bool(row["payment_verified"]),
            signature_verified=bool(row["signature_verified"]),
            created_at=row["created_at"],
            payment_verification_level=row.get("payment_verification_level"),
            owner_ref=row.get("owner_ref"),
            weight_exclusion=row.get("weight_exclusion"),
            weight_basis=row.get("weight_basis") or "none",
        )
        # Legacy rows and unweighted rows alike belong to the service's stored
        # history but create no trust event. Legacy rows still carry their
        # frozen weight into every score computed below; unweighted ones carry
        # nothing.
        if not review.weighted:
            per_service.setdefault(row["service_id"], []).append(review)
            continue
        key = (row["service_id"], review.owner_ref)
        events.setdefault(key, []).append(review)
        event_order[key] = max(
            event_order.get(key, (row["created_at"], int(row["id"]))),
            (row["created_at"], int(row["id"])),
        )

    for key in sorted(events, key=lambda item: event_order[item]):
        service_id, owner_ref = key
        event_reviews = events[key]
        event_at = event_order[key][0]
        bucket = aggregate_owner_reviews(event_reviews, event_at)[0]
        if owner_ref in raw:
            loo = compute_trust_consensus(
                per_service.get(service_id, []),
                trust,
                event_at,
                exclude_owner=owner_ref,
                legacy_trust=legacy_trust,
            )
            raw[owner_ref] = updated_raw_trust(
                raw[owner_ref], loo.score, bucket.rating
            )
            trust[owner_ref] = TrustRow(raw_trust=raw[owner_ref])
        per_service.setdefault(service_id, []).extend(event_reviews)

    for owner in owners:
        owner_ref = owner["owner_ref"]
        if owner_ref not in raw:
            continue
        stored = float(owner["raw_trust"])
        if abs(raw[owner_ref] - stored) > DRIFT_TOLERANCE:
            logging.getLogger(__name__).info(
                f"sweep: trust drift owner={owner_ref} "
                f"stored={stored:.4f} recomputed={raw[owner_ref]:.4f}"
            )
        conn.execute(
            "update review_owners set raw_trust = %s, trust_updated_at = %s where owner_ref = %s",
            (raw[owner_ref], now, owner_ref),
        )

    # Recompute every service's stored score with decay at `now` — this is
    # also what keeps decay staleness bounded between reviews.
    services = conn.execute("select id, score, n_eff from services").fetchall()
    for service in services:
        result = compute_score(
            per_service.get(service["id"], []), trust, now, legacy_trust=legacy_trust
        )
        if abs(result.score - float(service["score"])) > DRIFT_TOLERANCE:
            logging.getLogger(__name__).info(
                f"sweep: score drift service={service['id']} "
                f"stored={float(service['score']):.4f} recomputed={result.score:.4f}"
            )
        conn.execute(
            """
            update services
            set score = %s, n_eff = %s, n_eff_legacy = %s, n_eff_hosted = %s,
                score_updated_at = %s
            where id = %s
            """,
            (result.score, result.n_eff, result.n_eff_legacy, result.n_eff_hosted,
             now, service["id"]),
        )


def _summary_input_reviews(
    conn: Any,
    service_id: str,
    trust: dict[str, TrustRow],
    now: datetime,
    legacy_trust: dict[str, TrustRow] | None = None,
) -> list[dict[str, Any]]:
    """Trust-weighted summarizer input (docs/SCORING.md §8.2): reviews that
    carry no weight don't get to write the narrative. A review qualifies under
    whichever era weighs it — a trusted owner's judged purchase, or a
    pre-cutover review from a wallet the frozen snapshot trusted. Cold-start
    fallback: with neither, fall back to authenticated community reviews.
    Unsigned placeholder rows never appear — they are not publishable. Payment
    status is preserved in the summarizer input."""
    rows = conn.execute(
        """
        select rating, reason, task_context, payment_verified,
               signature_verified, payment_verification_level,
               reviewer_wallet, owner_ref, weight_exclusion, weight_basis,
               verified_source, created_at
        from reviews
        where service_id = %s
          and (signature_verified or verified_source = 'hosted_receipt')
        order by (weight_exclusion is null) desc, created_at desc
        limit %s
        """,
        (service_id, SUMMARY_MAX_REVIEWS),
    ).fetchall()

    def carries_weight(row: dict[str, Any]) -> bool:
        review = ReviewRow(
            wallet=row["reviewer_wallet"],
            rating=int(row["rating"]),
            payment_verified=bool(row["payment_verified"]),
            signature_verified=bool(row["signature_verified"]),
            created_at=row["created_at"],
            payment_verification_level=row.get("payment_verification_level"),
            owner_ref=row.get("owner_ref"),
            weight_exclusion=row.get("weight_exclusion"),
            weight_basis=row.get("weight_basis") or "none",
        )
        holder = (
            (legacy_trust or {}).get(review.wallet)
            if review.legacy
            else trust.get(row["owner_ref"])
        )
        return review_weight(review, holder, now) > 0

    weighted = [row for row in rows if carries_weight(row)]
    return weighted or [row for row in rows if row["payment_verified"] or row["signature_verified"]]


def _collect_summary_inputs(now: datetime) -> list[tuple[str, str, list[dict[str, Any]], int]]:
    """Read every stale service's summarizer input in one short-lived
    connection. Model calls happen afterwards with no connection held — the
    pooler drops a connection idled across a slow LLM round trip."""
    with connect() as conn:
        pending = conn.execute(
            """
            select s.id, s.name, s.review_revision
            from services s
            where exists (
              select 1 from reviews r
              where r.service_id = s.id
                and (r.signature_verified or r.verified_source = 'hosted_receipt')
                and (s.last_summarized_at is null
                     or r.created_at > s.last_summarized_at
                     or coalesce(s.review_summary->>'version', '1') <> %s)
            )
            order by s.id
            """,
            (str(SUMMARY_VERSION),),
        ).fetchall()
        if not pending:
            return []

        legacy_trust = load_legacy_trust(conn)
        owners = conn.execute(
            "select owner_ref, raw_trust, is_seed, slashed_at from review_owners"
        ).fetchall()
        trust = {
            o["owner_ref"]: TrustRow(
                raw_trust=float(o["raw_trust"]),
                is_seed=bool(o["is_seed"]),
                slashed=o["slashed_at"] is not None,
            )
            for o in owners
        }
        return [
            (service["id"], service["name"], _summary_input_reviews(
                conn, service["id"], trust, now, legacy_trust
            ), service["review_revision"])
            for service in pending
        ]


def run_service_summaries(now: datetime) -> None:
    if not redaction_enabled():
        print("summaries: redaction disabled; review text passes through unredacted")

    inputs = _collect_summary_inputs(now)
    if not inputs:
        print("summaries: nothing to summarize")
        return

    settings = get_settings()
    summarized = 0
    for service_id, service_name, reviews, revision in inputs:
        if not reviews:
            continue

        texts: list[str | None] = []
        for review in reviews:
            texts.append(review.get("reason"))
            texts.append(review.get("task_context"))
        try:
            redacted = redact_texts(texts, fail_closed=True, timeout=REDACTION_BATCH_TIMEOUT)
        except RedactionUnavailable:
            print("summaries: redactor unavailable; skipping remaining services")
            break
        clean_reviews = []
        for index, review in enumerate(reviews):
            created_at = review.get("created_at")
            clean_reviews.append(
                {
                    "rating": int(review["rating"]),
                    "payment_verified": bool(review["payment_verified"]),
                    "created_at": created_at.isoformat() if created_at else None,
                    "reason": (redacted[index * 2] if redacted else review["reason"]),
                    "task_context": (
                        redacted[index * 2 + 1] if redacted else review["task_context"]
                    ),
                }
            )

        summary = summarize_service_reviews(service_name, clean_reviews)
        if summary is None:
            continue
        through = max(
            (r["created_at"] for r in clean_reviews if r["created_at"]),
            default=None,
        )
        stored = {
            **summary,
            "version": SUMMARY_VERSION,
            "n_reviews": len(clean_reviews),
            "through_date": through[:10] if through else None,
            "model": settings.openai_model,
            "generated_at": now.isoformat(),
        }
        with connect() as conn:
            conn.execute(
                """
                update services
                set review_summary = %s::jsonb, last_summarized_at = %s
                where id = %s and review_revision = %s
                """,
                (_as_json(stored), now, service_id, revision),
            )
            conn.commit()
        summarized += 1
    print(f"summaries: {summarized}/{len(inputs)} service(s) summarized")


def run_service_proposals(now: datetime) -> None:
    payload = proposals.generate(now, connect)
    if payload is None:
        print("proposals: skipped; keeping the last payload")
        return
    with connect() as conn:
        conn.execute(
            """
            insert into app_cache (key, payload, generated_at)
            values (%s, %s::jsonb, %s)
            on conflict (key)
            do update set payload = excluded.payload,
                          generated_at = excluded.generated_at
            """,
            (proposals.CACHE_KEY, _as_json(payload), now),
        )
        # The pre-v2 cache carried example request text; it is never served
        # again and must not linger.
        conn.execute("delete from app_cache where key = 'project_ideas'")
        conn.commit()
    print(
        f"proposals: {len(payload['proposals'])} listed from "
        f"{payload['source_request_count']} request(s), "
        f"{payload['hidden_request_count']} hidden"
    )


def _as_json(value: dict[str, Any]) -> str:
    return json.dumps(value)


def main() -> None:
    settings = get_settings()
    now = utc_now()
    failed = False

    try:
        with connect() as conn:
            sync_seed_owners(conn, settings.seed_owners)
            conn.commit()
        print(f"seeds: synced {len(settings.seed_owners)} explicit root owner(s)")
    except Exception:
        traceback.print_exc()
        failed = True

    for job in (
        run_payment_reverification,
        run_consistency_sweep,
        run_service_summaries,
        run_service_proposals,
    ):
        try:
            job(now)
        except Exception:
            traceback.print_exc()
            failed = True

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
