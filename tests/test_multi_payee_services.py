"""A service that collects at two Base payees can be reviewed at either (SRI-69).

Exa (`https://api.exa.ai/search`) advertises two Base payees in one 402. It was
registered from an mppx payment to one of them; the hosted gateway pays the
other, so 62 hosted reviews in a week were refused as
`service identity conflict`. The fixture below is that service: registered on
PAYEE_B, advertising PAYEE_A and PAYEE_B on Base, PAYEE_B on Tempo and a
Solana payee.

What must still hold: a payee joins a service's set only when the 402 of an
endpoint registered to that service advertises it, the probe never runs while
a connection is open, and nothing a caller supplies chooses the URL probed.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from crowdcode import advertised
from crowdcode import server as server_mod
from crowdcode.advertised import Offer
from crowdcode.identity import build_identity, create_service_from_identity, resolve_service
from tests.test_hosted_attestation import (  # noqa: F401 - fixtures
    ENDPOINT,
    attestation,
    hosted,
    receipt,
    tx_hash,
    wallet,
)
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture

PAYEE_A = "0x6d6E695b09861467c7d462f5AAF31cF3540B9192"
PAYEE_B = "0xB98eF29eb2be19Ae646A8FC0248255B90A332dbC"
SOLANA_PAYEE = "12Ec2cJmfR1C9uwejzxcuMhUgEC7wDrLgm1wBvvR5w9E"
STRANGER = "0x" + "5e" * 20
OFFERS = [
    Offer("mppx", PAYEE_B, "tempo:4217"),
    Offer("x402", PAYEE_A, "eip155:8453"),
    Offer("x402", SOLANA_PAYEE, "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"),
    Offer("x402", PAYEE_B, "eip155:8453"),
]


@pytest.fixture
def exa(hosted, monkeypatch):  # noqa: F811
    conn, review = hosted
    with conn.transaction():
        created = create_service_from_identity(
            conn,
            build_identity(api_endpoint=ENDPOINT, payment_provider="mppx",
                           payment_target_ref=PAYEE_B),
        )
    probes: list[str] = []
    open_connections = [0]
    connect = server_mod.connect

    @contextmanager
    def counting_connect():
        with connect() as c:
            open_connections[0] += 1
            try:
                yield c
            finally:
                open_connections[0] -= 1

    def fake_probe(endpoint):
        # An unpaid call to a third party must never hold a connection.
        assert open_connections[0] == 0
        probes.append(endpoint)
        return OFFERS, "HTTP 402; fixture"

    monkeypatch.setattr(server_mod, "connect", counting_connect)
    monkeypatch.setattr(advertised, "probe", fake_probe)
    monkeypatch.setattr(advertised, "public_endpoint", lambda endpoint: True)
    monkeypatch.setattr(advertised, "_cooldown", {})
    return conn, review, created.row["id"], probes


def _payees(conn, service_id):
    return {
        row["identifier_value"]
        for row in conn.execute(
            "select identifier_value from service_identifiers"
            " where service_id = %s and identifier_type = 'payment_target'",
            (service_id,),
        ).fetchall()
    }


def _hosted_review(review, *, payee, seed):
    agent = wallet(seed)
    tx = tx_hash(seed)
    att = attestation(receipt(tx=tx, payee=payee), reviewer_wallet=agent.address)
    # The gateway asks for the score first, then submits.
    score = server_mod.get_service_score(
        api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=payee
    )
    return score, review(wallet=agent, tx=tx, att=att, payee=payee)


def test_hosted_reviews_paid_to_either_base_payee_are_accepted(exa):
    conn, review, service_id, probes = exa
    for payee, seed in ((PAYEE_A, "41"), (PAYEE_B, "42")):
        score, result = _hosted_review(review, payee=payee, seed=seed)
        assert score["found"] is True, score
        assert result["accepted"], result
        assert result["verified_source"] == "hosted_receipt"
        assert result["counts_towards_ranking"] is True
        assert result["service_id"] == service_id
    # One probe, of the registered endpoint, learned the whole advertised set.
    assert probes == [ENDPOINT]
    # The 402 added PAYEE_A and the Solana payee; the verified Base payment to
    # PAYEE_B registered its x402 rail, as any verified payment does.
    assert _payees(conn, service_id) == {
        f"mppx:{PAYEE_B}", f"x402:{PAYEE_A}", f"x402:{SOLANA_PAYEE}", f"x402:{PAYEE_B}",
    }
    learned = conn.execute(
        "select metadata from services where id = %s", (service_id,)
    ).fetchone()["metadata"]["advertised_payees"][-1]
    assert learned["endpoint"] == ENDPOINT
    assert learned["registered"] == [f"x402:{PAYEE_A}", f"x402:{SOLANA_PAYEE}"]
    assert learned["source"] == "advertised 402 offer"
    rows = conn.execute("select payment_target_ref from reviews order by id").fetchall()
    assert [r["payment_target_ref"] for r in rows] == [PAYEE_A, PAYEE_B]


def test_without_the_402_the_second_payee_is_still_a_conflict(exa, monkeypatch):
    conn, _, service_id, _ = exa
    monkeypatch.setattr(advertised, "probe", lambda endpoint: ([], "HTTP 200; no payee advertised"))
    score = server_mod.get_service_score(
        api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=PAYEE_A
    )
    assert score["found"] is False
    assert score["reason"] == "service identity conflict"
    assert _payees(conn, service_id) == {f"mppx:{PAYEE_B}"}


def test_a_strangers_payee_is_never_added_to_the_set(exa):
    conn, review, service_id, probes = exa
    agent = wallet("43")
    result = review(wallet=agent, tx=tx_hash("43"), payee=STRANGER)
    assert result["accepted"] is False
    assert result["reason"] == "service identity conflict"
    assert probes == [ENDPOINT]
    # The service's own payees were learned; the stranger's was not, and the
    # review stored nothing.
    payees = _payees(conn, service_id)
    assert not any(STRANGER.lower() in value.lower() for value in payees)
    assert f"x402:{PAYEE_A}" in payees
    assert conn.execute("select count(*) as n from reviews").fetchone()["n"] == 0
    # Naming more payees within the cooldown does not call the service again.
    for other in (STRANGER, "0x" + "6f" * 20):
        server_mod.get_service_score(
            api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=other
        )
    assert probes == [ENDPOINT]
    # And the real second payee, asked after the stranger, still resolves.
    _, result = _hosted_review(review, payee=PAYEE_A, seed="44")
    assert result["accepted"], result


def test_the_caller_never_chooses_the_url_that_is_probed(exa):
    conn, _, service_id, probes = exa
    # The victim's service_id with an endpoint the caller controls: the
    # conflict is the endpoint, not the payee, so nothing is asked.
    score = server_mod.get_service_score(
        service_id=service_id, api_endpoint="https://attacker.example/pay",
        payment_provider="x402", payment_target_ref=PAYEE_A,
    )
    assert score["found"] is False
    # Wallet-only lookups name no service, so there is nothing to ask either.
    server_mod.get_service_score(payment_provider="x402", payment_target_ref=PAYEE_A)
    assert probes == []
    assert _payees(conn, service_id) == {f"mppx:{PAYEE_B}"}


def test_a_rotating_service_is_not_probed(exa):
    conn, _, service_id, probes = exa
    conn.execute("update services set payee_rotates = true where id = %s", (service_id,))
    assert resolve_service(
        conn, build_identity(api_endpoint=ENDPOINT, payment_provider="x402",
                             payment_target_ref=PAYEE_A)
    ).error is None
    server_mod.get_service_score(
        api_endpoint=ENDPOINT, payment_provider="x402", payment_target_ref=PAYEE_A
    )
    assert probes == []


@pytest.mark.parametrize("endpoint", [
    "http://api.exa.ai/search",
    "https://localhost/pay",
    "https://127.0.0.1/pay",
    "https://10.0.0.7/pay",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/pay",
])
def test_only_public_https_endpoints_are_probed(endpoint):
    assert advertised.public_endpoint(endpoint) is False


def test_the_parser_reads_every_payee_of_a_multi_payee_402():
    body = {"accepts": [
        {"network": "eip155:8453", "payTo": PAYEE_A.lower()},
        {"network": "eip155:8453", "payTo": PAYEE_B},
        {"network": "eip155:480", "payTo": PAYEE_B},
    ]}
    assert advertised.x402_offers(body, None) == [
        Offer("x402", PAYEE_A, "eip155:8453"),
        Offer("x402", PAYEE_B, "eip155:8453"),
        Offer("x402", PAYEE_B, "eip155:480"),
    ]
    assert advertised.advertises(advertised.x402_offers(body, None), PAYEE_A.lower())
    assert not advertised.advertises(advertised.x402_offers(body, None), STRANGER)
