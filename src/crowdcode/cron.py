"""crowdcode-cron: nightly batch entrypoint (docs/SCORING.md §8.2).

Jobs run in dependency order, each isolated so one failure does not block the
rest; the process exits nonzero if any job failed:

1. Payment re-verification — reviews whose tx-hash on-chain check failed only
   because the RPC was unreachable at submit time are re-checked and upgraded
   to onchain_verified when the transfer now verifies (a network flake must
   not permanently cost the verified multiplier).
2. Consistency sweep — recompute all reviewer trust and service scores from
   scratch by replaying the review history, log drift vs the incrementally
   maintained values, and write the recomputed values (the sweep is
   authoritative). The first run after applying the schema DDL doubles as the
   data migration that backfills users and stored scores.
3. Per-service review summaries (LLM), watermarked on last_summarized_at.
4. Requested-services summary (LLM), written to app_cache for the website.
"""

from __future__ import annotations

import json
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
from crowdcode.redaction import RedactionUnavailable, redact_texts, redaction_enabled
from crowdcode.reputation import ensure_user, recompute_service_score, sync_seed_wallets
from crowdcode.scoring import (
    ReviewRow,
    TrustRow,
    compute_score,
    review_weight,
    updated_raw_trust,
)
from crowdcode.settings import get_settings
from crowdcode.summaries import (
    BATCH_TIMEOUT,
    fetch_recent_requests,
    summarize_project_ideas,
    summarize_service_reviews,
)

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

        upgraded = 0
        touched_services: set[str] = set()
        for row in rows:
            tx_hash = (row["payment_reference_canonical"] or "").strip()
            if not TX_HASH_RE.match(tx_hash):
                continue
            check, chain_meta = check_payment_reference_onchain(
                provider=row["payment_provider"],
                payment_target_ref=row["payment_target_ref"],
                reviewer_wallet=row["reviewer_wallet"],
                tx_hash=tx_hash,
            )
            meta = dict(row["payment_verification_metadata"] or {})
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
    with connect() as conn:
        rows = conn.execute(
            """
            select id, service_id, reviewer_wallet, rating, payment_verified,
                   signature_verified, payment_verification_level,
                   created_at, user_id
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
            print(f"sweep: backfilled user_id on {backfilled} review(s)")

        users = conn.execute(
            "select user_id, wallet_address, raw_trust, is_seed, slashed_at from wallet_users"
        ).fetchall()
        pinned = {
            u["wallet_address"]: TrustRow(
                raw_trust=float(u["raw_trust"]),
                is_seed=bool(u["is_seed"]),
                slashed=u["slashed_at"] is not None,
            )
            for u in users
        }

        # Replay: recompute every non-seed wallet's raw trust from scratch,
        # applying the same leave-one-out update the write path applies, at
        # each review's own ingest time.
        raw: dict[str, float] = {}
        trust: dict[str, TrustRow] = {}
        for wallet, row in pinned.items():
            if row.is_seed or row.slashed:
                trust[wallet] = row
            else:
                raw[wallet] = 0.0
                trust[wallet] = TrustRow(raw_trust=0.0)

        per_service: dict[str, list[ReviewRow]] = {}
        for row in rows:
            wallet = row["reviewer_wallet"]
            review = ReviewRow(
                wallet=wallet,
                rating=int(row["rating"]),
                payment_verified=bool(row["payment_verified"]),
                signature_verified=bool(row["signature_verified"]),
                created_at=row["created_at"],
                payment_verification_level=row.get("payment_verification_level"),
            )
            if wallet in raw:
                loo = compute_score(
                    per_service.get(row["service_id"], []),
                    trust,
                    row["created_at"],
                    exclude_wallet=wallet,
                )
                raw[wallet] = updated_raw_trust(
                    raw[wallet], loo.score, review.rating
                )
                trust[wallet] = TrustRow(raw_trust=raw[wallet])
            per_service.setdefault(row["service_id"], []).append(review)

        for user in users:
            wallet = user["wallet_address"]
            if wallet not in raw:
                continue
            stored = float(user["raw_trust"])
            if abs(raw[wallet] - stored) > DRIFT_TOLERANCE:
                print(
                    f"sweep: trust drift user_id={user['user_id']} "
                    f"stored={stored:.4f} recomputed={raw[wallet]:.4f}"
                )
            conn.execute(
                "update wallet_users set raw_trust = %s, trust_updated_at = %s where user_id = %s",
                (raw[wallet], now, user["user_id"]),
            )

        # Recompute every service's stored score with decay at `now` — this is
        # also what keeps decay staleness bounded between reviews.
        services = conn.execute("select id, score, n_eff from services").fetchall()
        for service in services:
            result = compute_score(per_service.get(service["id"], []), trust, now)
            if abs(result.score - float(service["score"])) > DRIFT_TOLERANCE:
                print(
                    f"sweep: score drift service={service['id']} "
                    f"stored={float(service['score']):.4f} recomputed={result.score:.4f}"
                )
            conn.execute(
                """
                update services
                set score = %s, n_eff = %s, score_updated_at = %s
                where id = %s
                """,
                (result.score, result.n_eff, now, service["id"]),
            )
        conn.commit()
    print(f"sweep: {len(rows)} review(s), {len(users)} user(s), {len(services)} service(s)")


def _summary_input_reviews(
    conn: Any, service_id: str, trust: dict[str, TrustRow], now: datetime
) -> list[dict[str, Any]]:
    """Trust-weighted summarizer input (docs/SCORING.md §8.2): wallets below
    theta don't get to write the narrative. Cold-start fallback: with no
    weighted reviews yet, fall back to payment-verified ones; else skip."""
    rows = conn.execute(
        """
        select rating, reason, task_context, payment_verified,
               signature_verified, payment_verification_level,
               reviewer_wallet, created_at
        from reviews
        where service_id = %s
        order by created_at desc
        limit %s
        """,
        (service_id, SUMMARY_MAX_REVIEWS),
    ).fetchall()
    weighted = [
        row
        for row in rows
        if row["reviewer_wallet"]
        and review_weight(
            ReviewRow(
                wallet=row["reviewer_wallet"],
                rating=int(row["rating"]),
                payment_verified=bool(row["payment_verified"]),
                signature_verified=bool(row["signature_verified"]),
                created_at=row["created_at"],
                payment_verification_level=row.get("payment_verification_level"),
            ),
            trust.get(row["reviewer_wallet"]),
            now,
        )
        > 0
    ]
    return weighted or [row for row in rows if row["payment_verified"]]


def _collect_summary_inputs(now: datetime) -> list[tuple[str, str, list[dict[str, Any]]]]:
    """Read every stale service's summarizer input in one short-lived
    connection. Model calls happen afterwards with no connection held — the
    pooler drops a connection idled across a slow LLM round trip."""
    with connect() as conn:
        pending = conn.execute(
            """
            select s.id, s.name
            from services s
            where exists (
              select 1 from reviews r
              where r.service_id = s.id
                and (s.last_summarized_at is null
                     or r.created_at > s.last_summarized_at)
            )
            order by s.id
            """
        ).fetchall()
        if not pending:
            return []

        users = conn.execute(
            "select wallet_address, raw_trust, is_seed, slashed_at from wallet_users"
        ).fetchall()
        trust = {
            u["wallet_address"]: TrustRow(
                raw_trust=float(u["raw_trust"]),
                is_seed=bool(u["is_seed"]),
                slashed=u["slashed_at"] is not None,
            )
            for u in users
        }
        return [
            (service["id"], service["name"], _summary_input_reviews(
                conn, service["id"], trust, now
            ))
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
    for service_id, service_name, reviews in inputs:
        if not reviews:
            continue

        texts: list[str | None] = []
        for review in reviews:
            texts.append(review.get("reason"))
            texts.append(review.get("task_context"))
        try:
            redacted = redact_texts(texts, fail_closed=True)
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
                where id = %s
                """,
                (_as_json(stored), now, service_id),
            )
            conn.commit()
        summarized += 1
    print(f"summaries: {summarized}/{len(inputs)} service(s) summarized")


def run_request_summary(now: datetime) -> None:
    requests = fetch_recent_requests(limit=100)
    payload = summarize_project_ideas(requests, timeout=BATCH_TIMEOUT)
    with connect() as conn:
        conn.execute(
            """
            insert into app_cache (key, payload, generated_at)
            values ('project_ideas', %s::jsonb, %s)
            on conflict (key)
            do update set payload = excluded.payload,
                          generated_at = excluded.generated_at
            """,
            (_as_json(payload), now),
        )
        conn.commit()
    print(
        f"request summary: {payload.get('source')} over "
        f"{payload.get('source_request_count', 0)} request(s)"
    )


def _as_json(value: dict[str, Any]) -> str:
    return json.dumps(value)


def main() -> None:
    settings = get_settings()
    now = utc_now()
    failed = False

    if settings.seed_wallets:
        try:
            with connect() as conn:
                sync_seed_wallets(conn, settings.seed_wallets)
                conn.commit()
            print(f"seeds: synced {len(settings.seed_wallets)} wallet(s)")
        except Exception:
            traceback.print_exc()
            failed = True
    else:
        print("seeds: CROWDCODE_SEED_WALLETS not set; skipping sync")

    for job in (
        run_payment_reverification,
        run_consistency_sweep,
        run_service_summaries,
        run_request_summary,
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
