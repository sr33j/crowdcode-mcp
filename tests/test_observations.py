"""POST /api/observations (C5): a signed record of one paid call, verified on
chain, stored as `kind = 'observation'`, and never carrying content."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.testclient import TestClient

from crowdcode import advertised, observations
from crowdcode import server as server_mod
from crowdcode.advertised import Offer
from crowdcode.solana import SOLANA_USDC_MINT, SPL_TOKEN_PROGRAM, b58encode
from tests.test_hosted_attestation import _transfer_receipt, db, hosted  # noqa: F401 - fixtures

NOW = datetime.now(UTC).replace(microsecond=0)
PAYER = Account.create()
PAYEE = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
TX = "0x" + "12" * 32
ENDPOINT = "https://api.provider.example/v1/search"


def record(**overrides):
    base = {
        "endpoint": ENDPOINT, "method": "POST", "network": "eip155:8453",
        "payment_reference": TX, "amount_atomic": 7000, "status": 200,
        "response_bytes": 5120, "observed_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    base.update(overrides)
    return base


def signed(rec, account=PAYER):
    message = observations.observation_message(rec)
    return {"observation": rec,
            "signature": account.sign_message(encode_defunct(text=message)).signature.hex()}


@pytest.fixture
def chain(hosted, monkeypatch):  # noqa: F811
    """A Base receipt for TX from PAYER, and an endpoint whose 402 advertises PAYEE."""
    conn, _ = hosted
    receipts = {TX: _transfer_receipt(payer=PAYER.address, payee=PAYEE, value=7000)}
    monkeypatch.setattr(observations, "_rpc_transaction_receipt", lambda _url, tx: receipts.get(tx))
    probes = []

    def fake_advertised(key, endpoint):
        probes.append(endpoint)
        return [Offer("x402", PAYEE, "eip155:8453")], "HTTP 402"

    monkeypatch.setattr(advertised, "_advertised", fake_advertised)
    return conn, receipts, probes


def post(body):
    client = TestClient(server_mod.create_app())
    return client.post("/api/observations", content=json.dumps(body),
                       headers={"content-type": "application/json"})


def rows(conn):
    return conn.execute("select * from index_observations order by id").fetchall()


def test_a_verified_paid_call_is_stored_as_an_observation(chain):
    conn, _, probes = chain
    response = post(signed(record()))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True and body["payee"] == PAYEE and body["amount_atomic"] == 7000
    [row] = rows(conn)
    assert row["kind"] == "observation" and row["ok"] is True
    assert row["endpoint"] == ENDPOINT and row["method"] == "POST"
    assert row["network"] == "eip155:8453" and row["payee"] == PAYEE
    assert row["observer_wallet"] == PAYER.address.lower()
    assert row["payment_reference"] == TX and row["response_bytes"] == 5120
    assert int(row["quoted_amount_atomic"]) == 7000 and row["http_status"] == 200
    assert probes == [ENDPOINT]


def test_resubmitting_is_idempotent_and_another_wallet_cannot_claim_it(chain):
    conn, _, _ = chain
    assert post(signed(record())).json()["accepted"]
    again = post(signed(record()))
    assert again.status_code == 200 and again.json()["duplicate"] is True
    stolen = post(signed(record(), account=Account.create()))
    assert stolen.status_code == 409
    assert len(rows(conn)) == 1


def test_a_failed_call_is_recorded_as_not_ok(chain):
    conn, _, _ = chain
    assert post(signed(record(status=502))).json()["accepted"]
    [row] = rows(conn)
    assert row["ok"] is False and row["error_code"] == "http_502"


def test_content_and_query_strings_are_refused(chain):
    conn, _, _ = chain
    with_content = signed(record())
    with_content["observation"]["response_body"] = "the user's search results"
    assert post(with_content).status_code == 400
    extra_top = {**signed(record()), "request": {"query": "private"}}
    assert post(extra_top).status_code == 400
    assert post(signed(record(endpoint=ENDPOINT + "?q=private"))).status_code == 400
    assert rows(conn) == []


def test_bad_signature_wrong_payer_and_unknown_payee_are_rejected(chain):
    conn, receipts, _ = chain
    tampered = signed(record())
    tampered["observation"]["status"] = 201  # signed 200
    response = post(tampered)
    # Recovers a different (random) signer, which did not pay.
    assert response.status_code == 422 and response.json()["error_code"] == "payment_transfer_mismatch"
    assert post({"observation": record(), "signature": "0x00"}).status_code == 401

    other_tx = "0x" + "34" * 32
    receipts[other_tx] = _transfer_receipt(payer=PAYER.address, payee=OTHER, value=7000)
    response = post(signed(record(payment_reference=other_tx)))
    assert response.status_code == 422 and response.json()["error_code"] == "payee_not_advertised"
    assert rows(conn) == []


def test_a_transaction_not_yet_visible_is_retryable(chain):
    response = post(signed(record(payment_reference="0x" + "56" * 32)))
    assert response.status_code == 503 and response.json()["retryable"] is True


def test_old_or_future_observations_are_refused(chain):
    old = (NOW - timedelta(days=8)).isoformat()
    assert post(signed(record(observed_at=old))).status_code == 400
    future = (NOW + timedelta(hours=1)).isoformat()
    assert post(signed(record(observed_at=future))).status_code == 400


def test_rate_limited_per_wallet(chain, monkeypatch):
    conn, receipts, _ = chain
    monkeypatch.setenv("CROWDCODE_OBSERVATION_LIMIT_PER_DAY", "1")
    assert post(signed(record())).json()["accepted"]
    second = "0x" + "78" * 32
    receipts[second] = _transfer_receipt(payer=PAYER.address, payee=PAYEE)
    response = post(signed(record(payment_reference=second)))
    assert response.status_code == 429 and response.json()["retryable"] is True


def test_a_registered_payee_needs_no_probe(chain):
    conn, _, probes = chain
    server_mod.create_service_from_identity(conn, server_mod.build_identity(
        api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=PAYEE))
    conn.commit()
    body = post(signed(record())).json()
    assert body["accepted"] and body["service_id"]
    assert probes == []


def test_solana_needs_the_payer_to_sign_the_same_message(chain, monkeypatch):
    conn, _, _ = chain
    key = Ed25519PrivateKey.generate()
    payer = b58encode(key.public_key().public_bytes_raw())
    payee = b58encode(bytes(range(1, 33)))
    signature = b58encode(bytes(range(64)))
    keys = ["facilitator1111111111111111111111111111111", "src", "dst"]
    tx = {
        "slot": 1, "meta": {
            "err": None,
            "preTokenBalances": [
                {"accountIndex": 1, "mint": SOLANA_USDC_MINT, "owner": payer, "uiTokenAmount": {"amount": "9000"}},
                {"accountIndex": 2, "mint": SOLANA_USDC_MINT, "owner": payee, "uiTokenAmount": {"amount": "0"}}],
            "postTokenBalances": [
                {"accountIndex": 1, "mint": SOLANA_USDC_MINT, "owner": payer, "uiTokenAmount": {"amount": "2000"}},
                {"accountIndex": 2, "mint": SOLANA_USDC_MINT, "owner": payee, "uiTokenAmount": {"amount": "7000"}}],
        },
        "transaction": {"message": {"accountKeys": keys, "instructions": [{
            "programId": SPL_TOKEN_PROGRAM,
            "parsed": {"type": "transferChecked", "info": {
                "source": "src", "destination": "dst", "mint": SOLANA_USDC_MINT,
                "authority": payer, "tokenAmount": {"amount": "7000"}}}}]}},
    }
    monkeypatch.setattr(observations, "rpc_get_transaction", lambda _url, sig: tx if sig == signature else None)
    monkeypatch.setattr(advertised, "_advertised",
                        lambda key, endpoint: ([Offer("x402", payee, "solana")], "HTTP 402"))
    rec = record(network="solana", payment_reference=signature)
    body = signed(rec)
    assert post(body).json()["error_code"] == "payer_signature_required"
    message = observations.observation_message(rec).encode()
    body.update(payer_wallet=payer, payer_signature=b58encode(key.sign(message)))
    response = post(body)
    assert response.status_code == 200, response.text
    [row] = rows(conn)
    assert row["network"] == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    assert row["payee"] == payee and int(row["quoted_amount_atomic"]) == 7000
