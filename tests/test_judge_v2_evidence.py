"""C11 and C10 (SRI-115 Phase 2): re-reading judge.v1 `partial` verdicts, and
the hosted gateway's signed records of paid calls the seller broke before
settlement. The review texts are CrowdCode's own published (redacted) verdicts."""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.rows import dict_row

from crowdcode import purchase_failures
from crowdcode.receipts import canonical_payload
from crowdcode.rubric import endpoint_defect, reread_partial

ROOT = Path(__file__).resolve().parents[1]

# Graded `partial` by judge.v1 for falling short of the buyer's whole task.
SHORT_OF_THE_TASK = {
    1506: "The request searched for [GIVEN_NAME_3][GIVEN_NAME_1] [SURNAME_3]'s official phone number, and the response returned relevant official-page search results. No phone number was included, so the requested contact detail was not delivered.",
    1503: "The search returned a California-specific Rocket Mortgage 30-year fixed quote with APR, points, and assumptions. The quoted rate was current as of November 16, 2026, not published today as requested, and the response does not establish 10 qualifying November 17 lender quotes.",
    1505: "The search returned relevant official Australian and European sources with publication dates and summaries. However, several embedded primary-source references were truncated or pointed to incomplete or broader paths, and the response does not clearly provide all requested sources, including the SEC item.",
    1462: "The search returned relevant US product and roundup pages with several candidate keyboards, including layouts and some prices. It did not provide a complete three-keyboard comparison with switch details, confirmed prices, and review evidence for each model.",
    1467: "The search returned three relevant 2026 guides with several keyboards, layouts, switch features, and example prices under $100. It did not provide a consolidated comparison of recent positive reviews or US shipping availability, and some results were truncated.",
    1356: "The request sought the two biggest U.S. stablecoin regulation developments with source URLs, and the provider returned HTTP 200 search results including a relevant Federal Register GENIUS Act implementation document. The response did not explicitly identify or rank two developments, and the displayed payload is truncated.",
    1250: "The search returned economy one-way SFO–JFK offers for one adult on 2026-12-10, including a nonstop American Airlines itinerary priced at USD 140.49. However, the response status was still \"enriching\" and explicitly required polling before it could be treated as a final all-seller comparison, so the cheapest option was not established.",
    1396: "The request sought selection of the best remaining recent deep dive, an attributed summary, and a PDF report. The provider returned HTTP 200 with one article’s metadata and a synthesized summary, but no PDF and no evidence that it selected among multiple remaining articles.",
}
# Each names a defect of the endpoint itself.
ENDPOINT_DEFECTS = {
    1272: ("The search returned CP timetable material confirming [GIVEN_NAME_17] do Douro services and useful times for [GIVEN_NAME_16] and Régua, plus information that [GIVEN_NAME_13]s and [SURNAME_2][SURNAME_6] [SURNAME_1] lack railway stations. It did not provide a clean nearest-station plan or reliable travel time for all six stops, and the timetable text was garbled with missing or unclear journey details.", "a malformed body"),
    1498: ("No task or request was recorded, and the provider returned HTTP 400 with an unknown query parameter error instead of flight data.", "an error status"),
    1246: ("The request failed in transport and returned no streamflow value; payment state remained unknown.", "a failed call"),
    1140: ("Charged $0.02 for an outbound SMS request but returned status queued with _gateway.live=false and note 'SMS queued. Production integrates with Twilio/Vonage.' No evidence of actual SMS dispatch or delivery; response appears non-live and did not establish completion of the user's task. No duplicate message or fallback purchase attempted.", "not a real result"),
}


@pytest.mark.parametrize("review_id", sorted(SHORT_OF_THE_TASK))
def test_partial_short_of_the_task_becomes_delivered(review_id):
    delivery, note = reread_partial(SHORT_OF_THE_TASK[review_id])
    assert delivery == "delivered", (review_id, endpoint_defect(SHORT_OF_THE_TASK[review_id]))
    assert "no endpoint defect named" in note


@pytest.mark.parametrize("review_id", sorted(ENDPOINT_DEFECTS))
def test_partial_naming_an_endpoint_defect_stays_not_delivered(review_id):
    text, defect = ENDPOINT_DEFECTS[review_id]
    assert reread_partial(text) == ("not_delivered", f"judge.v1 partial re-read under C11: the judge's text names {defect}")


def test_what_is_not_a_defect():
    # Our own 4,000-character cut, a status line with 200, an async status.
    for text in ("The displayed payload is truncated.", "Returned HTTP 200 with three results.",
                 "The job is queued; poll the status URL.", "Fewer than the requested six results were returned."):
        assert endpoint_defect(text) is None, text
    for text in ("The provider returned HTTP 503.", "An empty body came back.", "Invalid JSON in the response.",
                 "It returned the wrong content type for an image request.", "Charged $0.02 with no result."):
        assert endpoint_defect(text), text


# --- PostgreSQL ------------------------------------------------------------

KEY = Ed25519PrivateKey.generate()
KID = "oc-receipt-test"
KEYS = {KID: KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)}
NOW = datetime.now(UTC).replace(microsecond=0)


def failure(**overrides):
    payload = {
        "type": "opencrowd.purchase_failure.v1", "attempt_id": str(uuid4()), "owner_ref": "ab" * 32,
        "endpoint": "https://seller.example/api/run", "method": "POST", "network": "base",
        "payee": "0x1111111111111111111111111111111111111111", "amount_atomic": "5000",
        "asset": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "http_status": 500, "failure": "seller_error",
        "observed_at": NOW.isoformat().replace("+00:00", "Z"), "traffic": "test", "kid": KID,
    }
    payload.update(overrides)
    return payload


def signed(payload, key=KEY):
    signature = base64.urlsafe_b64encode(key.sign(canonical_payload(payload).encode())).decode().rstrip("=")
    return {"payload": payload, "signature": signature}


@pytest.fixture
def connect():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "judge_v2_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"create schema {schema}")
        admin.execute(f"set search_path to {schema}")
        for name in ("schema.sql", "hosted-only-reviews.sql", "services-index.sql", "reliability.sql",
                     "judge-v2-evidence.sql", "judge-v2-evidence.sql"):
            admin.execute((ROOT / "supabase" / name).read_text())

    @contextmanager
    def conn():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as c:
            yield c

    try:
        yield conn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"drop schema {schema} cascade")


def test_a_signed_failure_is_stored_once_as_seller_evidence(connect):
    body = signed(failure())
    first = purchase_failures.submit_failure(body, connect, keys=KEYS, now=NOW)
    assert first.http_status == 200 and first.body["accepted"] is True
    again = purchase_failures.submit_failure(body, connect, keys=KEYS, now=NOW)
    assert again.body == {"accepted": True, "duplicate": True}
    with connect() as c:
        rows = c.execute("select kind, ok, review_id, detail, attempt_ref, traffic, http_status, network, protocol from index_observations").fetchall()
    assert rows == [{"kind": "purchase", "ok": False, "review_id": None, "detail": "seller_error",
                     "attempt_ref": body["payload"]["attempt_id"], "traffic": "test", "http_status": 500,
                     "network": "eip155:8453", "protocol": "x402"}]


def test_refuses_what_it_cannot_verify_or_count(connect):
    def outcome(body):
        return purchase_failures.submit_failure(body, connect, keys=KEYS, now=NOW)
    assert outcome(signed(failure(), key=Ed25519PrivateKey.generate())).http_status == 401
    tampered = signed(failure())
    tampered["payload"]["failure"] = "no_receipt"
    assert outcome(tampered).http_status == 401
    assert outcome(signed(failure(failure="seller_slow"))).body["error_code"] == "invalid_failure"
    assert outcome(signed(failure(network="monad"))).http_status == 400
    assert outcome(signed(failure(observed_at=(NOW - timedelta(days=40)).isoformat()))).http_status == 400
    assert outcome({"payload": failure(), "signature": "x", "extra": 1}).http_status == 400
    with connect() as c:
        assert c.execute("select count(*)::int as n from index_observations").fetchone()["n"] == 0


def test_partials_are_reread_once_beside_the_signed_attestation(connect):
    with connect() as c:
        c.execute("insert into services (id, name) values ('svc_exa', 'Exa')")
        for review_id, text in ((1, SHORT_OF_THE_TASK[1506]), (2, ENDPOINT_DEFECTS[1498][0])):
            c.execute(
                "insert into reviews (id, service_id, rating, reason, receipt_key_id, gateway_attestation) values (%s, 'svc_exa', 3, %s, %s, %s)",
                (review_id, text, KID, psycopg.types.json.Jsonb({"review": {"delivery": "partial"}})),
            )
        c.execute("insert into reviews (id, service_id, rating, reason, receipt_key_id, gateway_attestation) values (3, 'svc_exa', 5, 'ok', %s, %s)",
                  (KID, psycopg.types.json.Jsonb({"review": {"delivery": "delivered"}})))
        c.commit()
    assert purchase_failures.reread_partials(connect) == {"delivered": 1, "not_delivered": 1}
    assert purchase_failures.reread_partials(connect) == {"delivered": 0, "not_delivered": 0}
    with connect() as c:
        rows = {r["id"]: r for r in c.execute("select id, delivery_reread, gateway_attestation->'review'->>'delivery' as signed from reviews")}
    assert (rows[1]["delivery_reread"], rows[2]["delivery_reread"], rows[3]["delivery_reread"]) == ("delivered", "not_delivered", None)
    # The signed attestation is never rewritten.
    assert rows[1]["signed"] == "partial"


def test_reliability_v2_counts_both(connect):
    """W4's rule reads what this writes: a failure is beta+1, a re-read partial is resolved."""
    from crowdcode import reliability

    url = "https://seller.example/api/run"
    with connect() as c:
        c.execute("insert into services (id, name, canonical_endpoint) values ('svc_x', 'Seller', %s)", (url,))
        c.execute(
            """insert into index_resources (id, resource_key, method, origin, route_template, probe_url, tracked, service_id, sources)
               values (1, %s, 'POST', 'https://seller.example', %s, %s, true, 'svc_x', array['bazaar'])""",
            (f"POST {url}", url, url),
        )
        c.execute("insert into index_resource_urls (source, method, url, resource_id) values ('bazaar', 'POST', %s, 1)", (url,))
        c.execute(
            "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, created_at) values ('svc_x', 3, %s, %s, %s, %s)",
            (SHORT_OF_THE_TASK[1462], KID, psycopg.types.json.Jsonb({"review": {"delivery": "partial"}}), NOW - timedelta(days=1)),
        )
        c.commit()
    assert purchase_failures.submit_failure(signed(failure(endpoint=url)), connect, keys=KEYS, now=NOW).body["accepted"]
    purchase_failures.reread_partials(connect)
    with connect() as c:
        inputs = reliability.gather(c, NOW, ["svc_x"])["svc_x"]
    assert inputs["failures_30d"] == {"total": 1, "test": 1, "by": {"seller_error": 1}}
    assert inputs["judged_30d"]["delivered"] == 1 and inputs["judged_30d"]["reread"] == 1 and inputs["judged_30d"]["unresolved"] == 0
