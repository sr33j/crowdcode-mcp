"""Per-service payment networks for the public services API."""

from __future__ import annotations

from crowdcode.networks import (
    EMPTY_NETWORKS,
    fetch_service_networks,
    payment_identifier_network,
    payment_target_network,
    review_payment_network,
    service_networks,
)
from tests.test_reputation import FakeConn

EVM_TX = "0x" + "ab" * 32
SOLANA_SIG = "5" * 87
SOLANA_PAYEE = "F1AbWuXJcBT9arW9wc6Xr2vom5NBtngWsz6Ht16jRBLM"
EVM_PAYEE = "0x" + "44" * 20


def test_recorded_network_wins():
    assert review_payment_network("x402", "monad", EVM_TX) == "monad"
    assert review_payment_network("x402", "eip155:8453", None) == "base"
    assert review_payment_network("mppx", "tempo", EVM_TX) == "tempo"
    assert review_payment_network("x402", "solana", f"x402:solana:{SOLANA_SIG}") == "solana"


def test_reference_prefix_then_rail_default():
    assert review_payment_network("x402", None, f"x402:monad:{EVM_TX}") == "monad"
    assert review_payment_network("x402", None, f"X402:SOLANA:{SOLANA_SIG}") == "solana"
    assert review_payment_network("mppx", None, f"mppx:tempo:{EVM_TX}") == "tempo"
    # Bare 0x hash: x402 defaults to Base, mppx always settles on Tempo.
    assert review_payment_network("x402", None, EVM_TX) == "base"
    assert review_payment_network("mppx", None, EVM_TX) == "tempo"
    assert review_payment_network("x402", None, SOLANA_SIG) == "solana"


def test_unknown_evidence_names_no_network():
    assert review_payment_network(None, None, None) is None
    assert review_payment_network("manual", None, "receipt-1") is None
    assert review_payment_network("x402", "polygon", "not-a-hash") is None


def test_registered_payees_advertise_only_pinned_networks():
    assert payment_target_network("mppx", EVM_PAYEE) == "tempo"
    assert payment_target_network("x402", SOLANA_PAYEE) == "solana"
    # An EVM x402 payee could be Base or Monad: it advertises nothing.
    assert payment_target_network("x402", EVM_PAYEE) is None
    assert payment_identifier_network(f"x402:{SOLANA_PAYEE}") == "solana"
    assert payment_identifier_network(f"mppx:{EVM_PAYEE}") == "tempo"
    assert payment_identifier_network(f"x402:{EVM_PAYEE}") is None


def test_service_networks_orders_verified_first():
    shape = service_networks({"solana": 1, "base": 5, "monad": 0}, ["tempo", "solana"])
    assert shape["networks"] == ["base", "solana", "tempo"]
    assert shape["verified_networks"] == [
        {"network": "base", "label": "Base", "verified_payments": 5},
        {"network": "solana", "label": "Solana", "verified_payments": 1},
    ]
    assert shape["advertised_networks"] == ["solana", "tempo"]
    assert EMPTY_NETWORKS == {
        "networks": [], "verified_networks": [], "advertised_networks": []
    }


def test_fetch_service_networks_combines_reviews_and_identifiers():
    conn = FakeConn(
        {
            "from reviews": [
                {"service_id": "svc_a", "payment_provider": "x402",
                 "network": None, "reference": EVM_TX},
                {"service_id": "svc_a", "payment_provider": "x402",
                 "network": "solana", "reference": f"x402:solana:{SOLANA_SIG}"},
                {"service_id": "svc_a", "payment_provider": "x402",
                 "network": "base", "reference": EVM_TX},
                {"service_id": "svc_b", "payment_provider": "mppx",
                 "network": None, "reference": f"mppx:tempo:{EVM_TX}"},
                {"service_id": "svc_c", "payment_provider": "manual",
                 "network": None, "reference": None},
            ],
            "from services": [
                {"service_id": "svc_a", "payment_provider": "x402",
                 "payment_target_ref": EVM_PAYEE},
                {"service_id": "svc_d", "payment_provider": "mppx",
                 "payment_target_ref": "parallel"},
            ],
            "from service_identifiers": [
                {"service_id": "svc_a", "identifier_value": f"x402:{SOLANA_PAYEE}"},
            ],
        }
    )
    result = fetch_service_networks(conn)
    assert result["svc_a"]["networks"] == ["base", "solana"]
    assert result["svc_a"]["verified_networks"][0]["verified_payments"] == 2
    assert result["svc_a"]["advertised_networks"] == ["solana"]
    assert result["svc_b"]["networks"] == ["tempo"]
    assert result["svc_d"] == {
        "networks": ["tempo"], "verified_networks": [], "advertised_networks": ["tempo"]
    }
    assert "svc_c" not in result
    # Only verified payments count as evidence.
    assert "where payment_verified" in conn.executed("from reviews")[0][0]


def test_fetch_service_networks_scopes_to_one_service():
    conn = FakeConn({})
    assert fetch_service_networks(conn, "svc_a") == {}
    assert all(params == ("svc_a",) for _, params in conn.queries)
