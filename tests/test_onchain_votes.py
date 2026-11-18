"""Paid reviews verified on chain are votes (docs/SCORING.md v4, SRI-118).

Owner decision, 2026-11-17: a crowdcode-mcp review of a paid call whose
settlement CrowdCode verified on chain weighs the same as a hosted judged
review, with guards: the payer is not the payee nor a wallet of the payee's
own account, one vote per paying wallet per service, and at least $0.001.
"""
from __future__ import annotations

import pytest

from crowdcode import server as server_mod
from tests.test_hosted_attestation import attestation, receipt  # noqa: F401
from tests.test_hosted_attestation import hosted, tx_hash, wallet  # noqa: F401 - fixtures
from tests.test_review_payee_from_index import ENDPOINT, SELLER, SERVICE_ID, _review, indexed  # noqa: F401
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture


def _stored(conn):
    return conn.execute(
        "select score, score_mean, n_eff, n_eff_onchain from services where id = %s", (SERVICE_ID,)
    ).fetchone()


def test_a_paid_mcp_review_is_a_full_vote(indexed, monkeypatch):
    conn, _ = indexed
    result = _review(conn, monkeypatch, seed="61", rating=5)
    assert result["accepted"], result
    assert result["counts_towards_ranking"] is True
    assert result["weight_basis"] == "onchain"
    row = conn.execute("select weight_basis, weight_exclusion from reviews").fetchone()
    assert row == {"weight_basis": "onchain", "weight_exclusion": None}
    stored = _stored(conn)
    assert stored["n_eff_onchain"] == pytest.approx(1.0)
    assert stored["score"] == pytest.approx(3.75)
    assert stored["score_mean"] == pytest.approx(11 / 3)
    score = server_mod.get_service_score(service_id=SERVICE_ID)
    assert score["num_weighted_reviews"] == 1
    assert score["p_above_3"] == pytest.approx(0.75)


def test_one_wallet_is_one_vote(indexed, monkeypatch):
    conn, _ = indexed
    # The same wallet pays twice (two settlements) and reviews both.
    agent_seed = "62"
    first = _review(conn, monkeypatch, seed=agent_seed, rating=5)
    assert first["accepted"], first
    from crowdcode import payments as payments_mod
    from tests.test_hosted_attestation import _transfer_receipt
    from crowdcode.payments import canonical_review_payload
    from crowdcode.identity import build_identity, resolve_service
    from eth_account.messages import encode_defunct
    agent = wallet(agent_seed)
    tx = "0x" + "77" * 32
    monkeypatch.setattr(payments_mod, "_rpc_transaction_receipt",
                        lambda rpc, h: _transfer_receipt(payer=agent.address, payee=SELLER, value=20000))
    signed = resolve_service(conn, build_identity(api_endpoint=ENDPOINT, payment_provider="x402")).identity
    reason = "Second purchase, same answer quality."
    message = canonical_review_payload(identity=signed, rating=5, reason=reason, payment_reference=tx)
    second = server_mod.review_service(
        rating=5, reason=reason, payment_reference=tx, service_id=signed.service_id,
        api_endpoint=signed.api_endpoint, payment_provider=signed.payment_provider,
        payment_target_ref=signed.payment_target_ref, reviewer_wallet=agent.address,
        review_signature="0x" + agent.sign_message(encode_defunct(text=message)).signature.hex(),
    )
    assert second["accepted"], second
    assert _stored(conn)["n_eff"] == pytest.approx(1.0)
    # A second wallet is a second vote.
    assert _review(conn, monkeypatch, seed="63", rating=5)["accepted"]
    assert _stored(conn)["n_eff"] == pytest.approx(2.0)


def test_a_dust_payment_is_no_vote(indexed, monkeypatch):
    conn, _ = indexed
    from crowdcode import payments as payments_mod
    from tests.test_hosted_attestation import _transfer_receipt
    original = _transfer_receipt
    monkeypatch.setattr("tests.test_review_payee_from_index._transfer_receipt",
                        lambda **kw: original(**{**kw, "value": 999}))
    result = _review(conn, monkeypatch, seed="64")
    assert result["accepted"], result
    assert result["weight_exclusion"] == "dust_payment"
    assert result["counts_towards_ranking"] is False
    assert _stored(conn)["n_eff"] == 0.0
    del payments_mod


def test_paying_yourself_is_no_vote(indexed, monkeypatch):
    conn, _ = indexed
    payer = wallet("65")
    conn.execute("update index_resources set payees = %s, pay_to = %s",
                 ([payer.address.lower()], [payer.address.lower()]))
    result = _review(conn, monkeypatch, seed="65", paid_to=payer.address)
    assert result["accepted"], result
    assert result["weight_exclusion"] == "self_dealing"
    assert _stored(conn)["n_eff"] == 0.0


def test_a_wallet_of_the_payees_own_account_is_no_vote(indexed, monkeypatch):
    conn, _ = indexed
    payer = wallet("66")
    # Gateway receipts already tie this wallet, and this payee, to one owner.
    conn.execute(
        """insert into reviews (service_id, rating, reason, reviewer_wallet, owner_ref, payee_owner_ref,
                                payment_target_ref, verified_source, weight_exclusion, signature_verified,
                                payment_verified, reviewer_id)
           values (%s, 4, 'x', %s, %s, %s, %s, 'hosted_receipt', 'self_dealing', true, true, 'r-66')""",
        (SERVICE_ID, payer.address.lower(), "6" * 32, "6" * 32, SELLER.lower()),
    )
    result = _review(conn, monkeypatch, seed="66")
    assert result["accepted"], result
    assert result["weight_exclusion"] == "self_dealing"


def test_an_endpoint_reports_its_average_and_its_providers_pooled_score(indexed, monkeypatch):
    conn, _ = indexed
    assert _review(conn, monkeypatch, seed="71", rating=5)["accepted"]
    assert _review(conn, monkeypatch, seed="72", rating=4)["accepted"]
    score = server_mod.get_service_score(service_id=SERVICE_ID)
    assert score["endpoint_avg_rating"] == pytest.approx(4.5, abs=0.01)
    provider = score["provider"]
    assert provider["provider_key"] == "api.financialdatasets.example"
    assert provider["num_endpoints"] == 1 and provider["endpoint_ids"] == [SERVICE_ID]
    assert provider["score"] == pytest.approx(score["score"])
    payload, status = server_mod._service_detail_payload(SERVICE_ID)
    assert status == 200
    assert payload["endpoint_avg_rating"] == pytest.approx(4.5, abs=0.01)
    assert payload["provider"]["score"] == pytest.approx(provider["score"])
