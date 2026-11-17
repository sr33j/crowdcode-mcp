"""A paid review that names no payee is verified against the payees CrowdCode
knows for the service (SRI-118).

Found on 2026-11-17: eight paid Financial Datasets calls, each settled on
Base, were all refused as `payment_payee_unknown` until the reviewer added
the x402 `payTo` by hand. The services index had created the endpoint's
`services` row without a payee, while knowing the payee from its own probe.
The fixture below is that seller: indexed, tracked, probed, never registered.

What must still hold: the reviewer never chooses the payee, a payee only a
third-party listing names is used only if the endpoint's own 402 still
advertises it, and a transfer to anyone else stores nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_account.messages import encode_defunct

from crowdcode import advertised
from crowdcode import payments as payments_mod
from crowdcode import server as server_mod
from crowdcode.advertised import Offer
from crowdcode.identity import build_identity, resolve_service
from crowdcode.payments import canonical_review_payload
from tests.test_hosted_attestation import (  # noqa: F401 - fixtures
    _transfer_receipt,
    hosted,
    tx_hash,
    wallet,
)
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture

ROOT = Path(__file__).parents[1]
ENDPOINT = "https://api.financialdatasets.example/financials"
SELLER = "0xC45E70793c01C6ae4336DD5a5503bAc958d32403"
STRANGER = "0x" + "5e" * 20
SERVICE_ID = "svc_index_financials"


@pytest.fixture
def indexed(hosted, monkeypatch):  # noqa: F811
    """An indexed, tracked, probed seller whose services row has no payee."""
    conn, _ = hosted
    conn.execute((ROOT / "supabase/services-index.sql").read_text())
    conn.execute(
        """insert into services (id, name, canonical_origin, canonical_endpoint, created_from_review, metadata)
           values (%s, 'api.financialdatasets.example /financials', 'https://api.financialdatasets.example',
                   %s, false, '{"listing_source": "index"}')""",
        (SERVICE_ID, ENDPOINT),
    )
    conn.execute(
        "insert into service_identifiers (service_id, identifier_type, identifier_value) values (%s, 'api_endpoint', %s)",
        (SERVICE_ID, ENDPOINT),
    )
    conn.execute(
        """insert into index_resources (resource_key, method, origin, route_template, probe_url, protocol,
                                        networks, payable, pay_to, payees, tracked, tracked_reason, service_id)
           values (%s, 'GET', 'https://api.financialdatasets.example', %s, %s, 'x402', '{eip155:8453}', true,
                   %s, %s, true, 'bazaar_payers', %s)""",
        (f"GET {ENDPOINT}", ENDPOINT, ENDPOINT, [SELLER.lower()], [SELLER.lower()], SERVICE_ID),
    )
    probes: list[str] = []

    def fake_probe(endpoint):
        probes.append(endpoint)
        return [Offer("x402", SELLER, "eip155:8453")], "HTTP 402; fixture"

    monkeypatch.setattr(advertised, "probe", fake_probe)
    monkeypatch.setattr(advertised, "public_endpoint", lambda endpoint: True)
    monkeypatch.setattr(advertised, "_cooldown", {})
    return conn, probes


def _review(conn, monkeypatch, *, seed, paid_to=SELLER, endpoint=ENDPOINT, provider="x402", rating=5,
            payee=None):
    """What a plain crowdcode-mcp install sends: the identity get_service_score
    resolved, signed, with no payment_target_ref unless `payee` is given."""
    agent = wallet(seed)
    tx = tx_hash(seed)
    monkeypatch.setattr(
        payments_mod, "_rpc_transaction_receipt",
        lambda rpc, h: _transfer_receipt(payer=agent.address, payee=paid_to, value=20000),
    )
    supplied = build_identity(api_endpoint=endpoint, payment_provider=provider, payment_target_ref=payee)
    signed = resolve_service(conn, supplied).identity or supplied
    reason = "Returned three years of income statements for AAPL as JSON."
    message = canonical_review_payload(identity=signed, rating=rating, reason=reason, payment_reference=tx)
    return server_mod.review_service(
        rating=rating, reason=reason, payment_reference=tx,
        service_id=signed.service_id, api_endpoint=signed.api_endpoint,
        payment_provider=signed.payment_provider, payment_target_ref=signed.payment_target_ref,
        reviewer_wallet=agent.address,
        review_signature="0x" + agent.sign_message(encode_defunct(text=message)).signature.hex(),
    )


def _service(conn):
    return conn.execute(
        "select payment_provider, payment_target_ref from services where id = %s", (SERVICE_ID,)
    ).fetchone()


def test_a_review_without_a_payee_verifies_against_the_indexed_payee(indexed, monkeypatch):
    conn, probes = indexed
    result = _review(conn, monkeypatch, seed="51")
    assert result["accepted"], result
    assert result["service_id"] == SERVICE_ID
    assert result["payment_verified"] is True
    assert result["payment_verification_level"] == "onchain_verified"
    row = conn.execute("select payment_target_ref, payment_provider, amount from reviews").fetchone()
    assert row["payment_target_ref"].lower() == SELLER.lower()
    assert row["payment_provider"] == "x402"
    assert row["amount"] == 20000
    # The payee the chain proved is now the service's, so the next lookup
    # (and the next reviewer's signing payload) names it.
    assert _service(conn)["payment_target_ref"].lower() == SELLER.lower()
    # The index's own probe was enough: the seller was not called.
    assert probes == []
    # A second reviewer, signing the identity that now resolves with the payee.
    second = _review(conn, monkeypatch, seed="52", rating=4)
    assert second["accepted"], second


def test_the_protocol_comes_from_the_index_when_the_review_names_none(indexed, monkeypatch):
    conn, _ = indexed
    result = _review(conn, monkeypatch, seed="53", provider=None)
    assert result["accepted"], result
    assert _service(conn)["payment_provider"] == "x402"


def test_a_transfer_to_anyone_else_stores_nothing(indexed, monkeypatch):
    conn, _ = indexed
    result = _review(conn, monkeypatch, seed="54", paid_to=STRANGER)
    assert result["accepted"] is False
    assert result["error_code"] == "payment_transfer_mismatch"
    assert [p.lower() for p in result["checked_payees"]] == [SELLER.lower()]
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 0
    assert _service(conn)["payment_target_ref"] is None


def test_a_listing_only_payee_needs_the_endpoints_own_402(indexed, monkeypatch):
    conn, probes = indexed
    # Never probed by the index: only a third-party listing names the payee.
    conn.execute("update index_resources set payees = '{}'")
    monkeypatch.setattr(advertised, "probe", lambda e: (probes.append(e) or [], "HTTP 200; no payee advertised"))
    refused = _review(conn, monkeypatch, seed="55")
    assert refused["accepted"] is False
    assert refused["error_code"] == "payment_payee_unknown"
    assert "payTo" in refused["reason"]
    assert refused["next_step"]["action"] == "name_payee"
    assert probes == [ENDPOINT]
    # The same listing, confirmed by the 402 the seller serves now.
    monkeypatch.setattr(advertised, "_cooldown", {})
    monkeypatch.setattr(advertised, "probe", lambda e: ([Offer("x402", SELLER, "eip155:8453")], "HTTP 402"))
    accepted = _review(conn, monkeypatch, seed="56")
    assert accepted["accepted"], accepted


def test_an_endpoint_known_only_to_the_index_is_registered_with_its_payee(indexed, monkeypatch):
    conn, _ = indexed
    other = "https://api.financialdatasets.example/earnings"
    # Listed and probed, but untracked: the index made no services row for it.
    conn.execute(
        """insert into index_resources (resource_key, method, origin, route_template, probe_url, protocol,
                                        payable, pay_to, payees)
           values (%s, 'GET', 'https://api.financialdatasets.example', %s, %s, 'x402', true, %s, %s)""",
        (f"GET {other}", other, other, [SELLER.lower()], [SELLER.lower()]),
    )
    result = _review(conn, monkeypatch, seed="57", endpoint=other)
    assert result["accepted"], result
    assert result["service_created"] is True
    created = conn.execute(
        "select payment_target_ref from services where canonical_endpoint = %s", (other,)
    ).fetchone()
    assert created["payment_target_ref"].lower() == SELLER.lower()


def test_naming_the_payee_still_works_and_wins(indexed, monkeypatch):
    conn, _ = indexed
    result = _review(conn, monkeypatch, seed="58", payee=SELLER)
    assert result["accepted"], result


def test_a_rejection_is_logged_for_counting(indexed, monkeypatch, caplog):
    conn, _ = indexed
    caplog.set_level("INFO", logger="crowdcode.server")
    _review(conn, monkeypatch, seed="59", paid_to=STRANGER)
    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    rejected = [e for e in events if e.get("event") == "review_rejected"]
    assert rejected and rejected[0]["error_code"] == "payment_transfer_mismatch"
    assert rejected[0]["payee_named"] is False and rejected[0]["payees_known"] == 1
