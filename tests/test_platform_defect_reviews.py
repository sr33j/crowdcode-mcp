"""A review of a failure CrowdCode caused is not evidence about the provider.

Two data problems, one cause each, both fixed here:

* A machine-payment service registered with a non-address payee can never be
  reviewed again (`service identity conflict`). The repair reads the payee
  from the service's own 402 and registers every rail it advertises, so a
  purchase settled on either one resolves.
* 1-star reviews our own bugs caused — a gateway that misread a valid 402, an
  agent refused the network the seller wanted — are kept verbatim and stop
  counting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crowdcode import server as server_mod
from crowdcode.identity import (
    build_identity,
    create_service_from_identity,
    register_machine_payment_alias,
    resolve_service,
)
from crowdcode.review_sql import (
    EXCLUSION_PLATFORM_DEFECT,
    WEIGHT_EXCLUSIONS,
    public_reviews,
    review_counts,
)
from scripts import exclude_platform_defect_reviews as exclude_script
from scripts import fix_machine_payees as payee_script
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture

ENDPOINT = "https://parallelmpp.example/api/search"
TEMPO_PAYEE = "0x662AeBe56c6CA013Ef6E6aBD6487c929E9219Ab5"
BASE_PAYEE = "0x3eF12B73391cfbB0a4Cef873dE1e357f0110cE47"
WALLET = "0x" + "77" * 20
NOW = datetime(2026, 10, 28, 23, 0, tzinfo=UTC)


# --- the non-address payee -------------------------------------------------


def _slug_registered_service(conn):
    """Reproduce svc_e91d4c0dfb719d9fefe8: `parallel` where the payee belongs.

    Written straight to the table, because that is the only way it can exist
    now — identity.normalize_payment_target refuses to store it.
    """
    conn.execute(
        """
        insert into services (id, name, directory_slug, canonical_origin,
                              canonical_endpoint, payment_provider,
                              payment_target_ref, created_from_review)
        values ('svc_slugpayee', 'Parallel MPP Search', 'parallel',
                'https://parallelmpp.example', %s, 'mppx', 'parallel', true)
        """,
        (ENDPOINT,),
    )
    for value in (("api_endpoint", ENDPOINT), ("payment_target", "mppx:parallel"),
                  ("directory_slug", "parallel")):
        conn.execute(
            "insert into service_identifiers (service_id, identifier_type, identifier_value)"
            " values ('svc_slugpayee', %s, %s)",
            value,
        )
    # The one real payee it ever collected on, registered by a verified
    # payment before the provider rotated it.
    conn.execute(
        "insert into service_identifiers (service_id, identifier_type, identifier_value)"
        " values ('svc_slugpayee', 'payment_target', %s)",
        ("x402:" + "0x" + "5d" * 20,),
    )


OFFERS = [
    payee_script.Offer("x402", BASE_PAYEE, "eip155:8453"),
    payee_script.Offer("mppx", TEMPO_PAYEE, "tempo:4217"),
]


def _advertise(monkeypatch, offers, *, rotating=False, note="HTTP 402"):
    monkeypatch.setattr(
        payee_script,
        "advertised_payees",
        lambda endpoint: payee_script.Advertised(offers, rotating, note),
    )


def test_a_stable_payee_is_read_from_the_402_and_both_rails_resolve(db, monkeypatch):
    conn, _, _ = db
    _slug_registered_service(conn)

    # Both of the payees the provider actually advertises are refused: the
    # service has no canonical payee of any family, and a stale alias holds
    # the EVM slot.
    for provider, payee in (("x402", BASE_PAYEE), ("mppx", TEMPO_PAYEE)):
        blocked = resolve_service(
            conn,
            build_identity(api_endpoint=ENDPOINT, payment_provider=provider,
                           payment_target_ref=payee),
        )
        assert blocked.error == "service identity conflict"

    _advertise(monkeypatch, OFFERS)
    monkeypatch.setattr(payee_script, "connect", _fake_connect(conn))
    assert payee_script.main_with(["--apply"]) == 0

    row = conn.execute(
        "select payment_provider, payment_target_ref, metadata from services"
        " where id = 'svc_slugpayee'"
    ).fetchone()
    assert (row["payment_provider"], row["payment_target_ref"]) == ("mppx", TEMPO_PAYEE)
    correction = row["metadata"]["payee_corrections"][-1]
    assert correction["from"] == "parallel"
    assert correction["to"] == TEMPO_PAYEE

    # The slug identifier named nothing and is gone; both advertised rails
    # now resolve to this service, and so does the historical payee.
    targets = {
        r["identifier_value"]
        for r in conn.execute(
            "select identifier_value from service_identifiers"
            " where service_id = 'svc_slugpayee' and identifier_type = 'payment_target'"
        ).fetchall()
    }
    assert "mppx:parallel" not in targets
    assert targets == {f"mppx:{TEMPO_PAYEE}", f"x402:{BASE_PAYEE}",
                       "x402:0x" + "5d" * 20}
    for provider, payee in (("x402", BASE_PAYEE), ("mppx", TEMPO_PAYEE),
                            ("x402", "0x" + "5d" * 20)):
        resolved = resolve_service(
            conn,
            build_identity(api_endpoint=ENDPOINT, payment_provider=provider,
                           payment_target_ref=payee),
        )
        assert resolved.error is None, (provider, payee)
        assert resolved.row["id"] == "svc_slugpayee"

    # An unadvertised payee is still a conflict: the repair must not turn the
    # service into one that accepts any destination.
    attacker = resolve_service(
        conn,
        build_identity(api_endpoint=ENDPOINT, payment_provider="x402",
                       payment_target_ref="0x" + "99" * 20),
    )
    assert attacker.error == "service identity conflict"


def test_a_rotating_payee_stores_no_payee_and_still_resolves_every_purchase(
    db, monkeypatch
):
    conn, _, _ = db
    _slug_registered_service(conn)
    # Parallel answers each 402 with a fresh single-use address, so the two
    # probes disagree and there is no payee to store.
    _advertise(monkeypatch, OFFERS, rotating=True,
               note="HTTP 402; mppx 0xaaa | HTTP 402; mppx 0xbbb")
    monkeypatch.setattr(payee_script, "connect", _fake_connect(conn))
    assert payee_script.main_with(["--apply"]) == 0

    row = conn.execute(
        "select payment_target_ref, payee_rotates, metadata from services"
        " where id = 'svc_slugpayee'"
    ).fetchone()
    assert row["payment_target_ref"] is None
    assert row["payee_rotates"] is True
    assert row["metadata"]["payee_corrections"][-1]["payee_rotates"] is True

    # Two consecutive purchases, each to an address the provider had just
    # issued, both resolve — and neither pins a payee or leaves an alias
    # behind, which is what made the service unreviewable in the first place.
    for octet in ("1a", "2b"):
        payee = "0x" + octet * 20
        resolved = resolve_service(
            conn,
            build_identity(api_endpoint=ENDPOINT, payment_provider="x402",
                           payment_target_ref=payee),
        )
        assert resolved.error is None, payee
        assert resolved.row["id"] == "svc_slugpayee"
        with conn.transaction():
            register_machine_payment_alias(
                conn, "svc_slugpayee", resolved.identity
            )
    after = conn.execute(
        "select payment_target_ref from services where id = 'svc_slugpayee'"
    ).fetchone()
    assert after["payment_target_ref"] is None
    assert conn.execute(
        """select count(*) as n from service_identifiers
           where service_id = 'svc_slugpayee' and identifier_type = 'payment_target'"""
    ).fetchone()["n"] == 1  # only the historical verified payee remains

    # A rotating payee is not a blank cheque for another service's payee: the
    # endpoint still decides which service a review lands on.
    other = resolve_service(
        conn,
        build_identity(api_endpoint="https://elsewhere.example/v1",
                       payment_provider="x402", payment_target_ref="0x" + "1a" * 20),
    )
    assert other.row is None and other.error is None


def test_repair_is_idempotent_and_finds_nothing_once_payees_are_addresses(db, monkeypatch):
    conn, _, _ = db
    _slug_registered_service(conn)
    _advertise(monkeypatch, OFFERS)
    monkeypatch.setattr(payee_script, "connect", _fake_connect(conn))
    assert payee_script.main_with(["--apply"]) == 0
    assert payee_script.broken_services(conn) == []
    assert payee_script.main_with(["--apply"]) == 0


def test_a_service_with_no_advertised_payee_is_left_alone(db, monkeypatch):
    conn, _, _ = db
    _slug_registered_service(conn)
    _advertise(monkeypatch, [], note="probe failed: ConnectError")
    monkeypatch.setattr(payee_script, "connect", _fake_connect(conn))
    assert payee_script.main_with(["--apply"]) == 0
    row = conn.execute(
        "select payment_target_ref from services where id = 'svc_slugpayee'"
    ).fetchone()
    assert row["payment_target_ref"] == "parallel"


def test_a_dry_run_writes_nothing(db, monkeypatch):
    conn, _, _ = db
    _slug_registered_service(conn)
    _advertise(monkeypatch, OFFERS)
    monkeypatch.setattr(payee_script, "connect", _fake_connect(conn))
    assert payee_script.main_with([]) == 0
    row = conn.execute(
        "select payment_target_ref from services where id = 'svc_slugpayee'"
    ).fetchone()
    assert row["payment_target_ref"] == "parallel"


X402_402 = {
    "accepts": [
        {"scheme": "exact", "network": "eip155:8453", "amount": "10000",
         "payTo": BASE_PAYEE.lower()},
    ]
}
MPP_HEADER = (
    'Payment id="abc", realm="parallelmpp.example", method="tempo", '
    'intent="charge", request="'
    "eyJhbW91bnQiOiIxMDAwMCIsIm1ldGhvZERldGFpbHMiOnsiY2hhaW5JZCI6NDIxN30sInJl"
    "Y2lwaWVudCI6IjB4NjYyYWViZTU2YzZjYTAxM2VmNmU2YWJkNjQ4N2M5MjllOTIxOWFiNSJ9"
    '", expires="2026-10-28T23:43:53.818Z"'
)


def test_the_402_parser_reads_both_rails_and_checksums_evm_payees():
    assert payee_script._x402_offers(X402_402, None) == [
        payee_script.Offer("x402", BASE_PAYEE, "eip155:8453")
    ]
    assert payee_script._mpp_offers(MPP_HEADER) == [
        payee_script.Offer("mppx", TEMPO_PAYEE, "tempo:4217")
    ]
    assert payee_script._mpp_offers("Bearer realm=\"x\"") == []
    assert payee_script._x402_offers({"accepts": [{"payTo": ""}]}, None) == []


# --- the unfair reviews ----------------------------------------------------


def _review(conn, service_id, rating, *, reason, wallet=WALLET, created_at=NOW,
            level="signature_only", nonce=None):
    return conn.execute(
        """
        insert into reviews (service_id, rating, reason, reviewer_wallet,
                             review_nonce, signature_verified,
                             payment_verification_level, created_at,
                             weight_basis, weight_exclusion)
        values (%s, %s, %s, %s, %s, true, %s, %s, 'none', 'community')
        returning id
        """,
        (service_id, rating, reason, wallet, nonce or reason[:20], level, created_at),
    ).fetchone()["id"]


@pytest.fixture
def reviewed(db):
    conn, _, _ = db
    with conn.transaction():
        service = create_service_from_identity(
            conn,
            build_identity(api_endpoint="https://music.example/v2/generate",
                           payment_provider="x402", payment_target_ref="0x" + "ab" * 20),
        )
    service_id = service.row["id"]
    good = _review(conn, service_id, 5, reason="the mp3 arrived and played",
                   created_at=NOW - timedelta(days=1))
    ours = _review(conn, service_id, 1,
                   reason="the gateway called the payment requirements unsupported")
    return conn, service_id, good, ours


def _counts(conn, service_id):
    return conn.execute(
        f"select {review_counts()} from reviews where service_id = %s", (service_id,)
    ).fetchone()


def test_an_excluded_review_leaves_every_published_count(reviewed, monkeypatch):
    conn, service_id, _, ours = reviewed
    before = _counts(conn, service_id)
    assert (before["num_reviews"], before["avg_rating"]) == (2, 3.0)
    assert before["num_signature_only_reviews"] == 2

    _exclude(monkeypatch, conn, service_id, ours)

    after = _counts(conn, service_id)
    assert (after["num_reviews"], after["avg_rating"]) == (1, 5.0)
    assert after["num_signature_only_reviews"] == 1
    assert after["num_community_reviews"] == 1
    # The row and its rating are still there to audit.
    row = conn.execute(
        "select rating, reason, weight_exclusion, exclusion_note from reviews where id = %s",
        (ours,),
    ).fetchone()
    assert row["rating"] == 1
    assert row["weight_exclusion"] == EXCLUSION_PLATFORM_DEFECT
    assert "crowdcode#157" in row["exclusion_note"]
    assert "unsupported" in row["reason"]


def test_an_excluded_review_is_not_summarized_and_regeneration_is_queued(reviewed, monkeypatch):
    from crowdcode import cron

    conn, service_id, good, ours = reviewed
    monkeypatch.setattr(cron, "connect", _fake_connect(conn))

    # A summary already generated from both reviews, watermarked between them.
    def watermark():
        conn.execute(
            """update services
               set review_summary = %s::jsonb, last_summarized_at = %s
               where id = %s""",
            ('{"version": 2}', NOW - timedelta(hours=12), service_id),
        )

    watermark()
    reasons = [
        r["reason"] for r in cron._summary_input_reviews(conn, service_id, {}, NOW)
    ]
    assert len(reasons) == 2
    # The 1-star review is newer than the watermark, so it asks for a summary.
    assert [s[0] for s in cron._collect_summary_inputs(NOW)] == [service_id]

    _exclude(monkeypatch, conn, service_id, ours)

    reasons = [
        r["reason"] for r in cron._summary_input_reviews(conn, service_id, {}, NOW)
    ]
    assert reasons == ["the mp3 arrived and played"]
    # Clearing the watermark is what makes the next cron run regenerate.
    assert conn.execute(
        "select last_summarized_at from services where id = %s", (service_id,)
    ).fetchone()["last_summarized_at"] is None
    assert [s[0] for s in cron._collect_summary_inputs(NOW)] == [service_id]

    # Once regenerated, the excluded review never asks for one again.
    watermark()
    assert cron._collect_summary_inputs(NOW) == []


def test_the_exclusion_refuses_a_review_that_does_not_match_what_was_audited(
    reviewed, monkeypatch
):
    conn, service_id, good, ours = reviewed
    monkeypatch.setattr(exclude_script, "connect", _fake_connect(conn))
    monkeypatch.setattr(
        exclude_script,
        "EXCLUSIONS",
        (
            exclude_script.Exclusion(
                review_id=ours,
                service_id=service_id,
                rating=4,  # the audited rating was 1
                reviewer_wallet=WALLET,
                created_on=NOW.date().isoformat(),
                note="crowdcode#157: mismatched on purpose",
            ),
        ),
    )
    assert exclude_script.main_with(["--apply"]) == 1
    assert conn.execute(
        "select weight_exclusion from reviews where id = %s", (ours,)
    ).fetchone()["weight_exclusion"] == "community"

    monkeypatch.setattr(
        exclude_script,
        "EXCLUSIONS",
        (
            exclude_script.Exclusion(
                review_id=max(ours, good) + 1000,
                service_id=service_id,
                rating=1,
                reviewer_wallet=WALLET,
                created_on=NOW.date().isoformat(),
                note="crowdcode#157: no such review",
            ),
        ),
    )
    assert exclude_script.main_with(["--apply"]) == 1


def test_platform_defect_is_an_accepted_weight_exclusion_everywhere():
    assert EXCLUSION_PLATFORM_DEFECT in WEIGHT_EXCLUSIONS
    assert EXCLUSION_PLATFORM_DEFECT in public_reviews()
    assert EXCLUSION_PLATFORM_DEFECT in public_reviews("r")
    assert "r.weight_exclusion" in public_reviews("r")
    assert server_mod.PUBLIC_REVIEWS == public_reviews()


# --- helpers ---------------------------------------------------------------


def _fake_connect(conn):
    from contextlib import contextmanager

    @contextmanager
    def connect():
        yield conn

    return connect


def _exclude(monkeypatch, conn, service_id, review_id, rating=1):
    monkeypatch.setattr(exclude_script, "connect", _fake_connect(conn))
    monkeypatch.setattr(
        exclude_script,
        "EXCLUSIONS",
        (
            exclude_script.Exclusion(
                review_id=review_id,
                service_id=service_id,
                rating=rating,
                reviewer_wallet=WALLET,
                created_on=NOW.date().isoformat(),
                note="crowdcode#157: the gateway refused a valid 402 offer",
            ),
        ),
    )
    assert exclude_script.main_with(["--apply"]) == 0
