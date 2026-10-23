"""Write-path trust and score persistence, against a scripted fake connection.

Mirrors the FakeConn/FakeCursor pattern in tests/test_rate_limit.py: no
database, just assertions that the right SQL runs with the right values.
"""

from __future__ import annotations

from datetime import UTC, datetime
import pytest

from crowdcode.reputation import (
    apply_review_trust_update,
    ensure_user,
    recompute_service_score,
    sync_seed_owners,
)
from crowdcode.scoring import ETA, KAPPA, MU0

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
SEED_OWNER = "1" * 32
HONEST_OWNER = "2" * 32
SEED_WALLET = "0x" + "11" * 20
HONEST_WALLET = "0x" + "22" * 20


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """Answers each query by the first matching fragment in `responses`."""

    def __init__(self, responses):
        self.responses = responses
        self.queries: list[tuple[str, tuple]] = []
        self.commits = 0

    def commit(self):
        self.commits += 1

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        for fragment, rows in self.responses.items():
            if fragment in sql:
                return FakeCursor(rows)
        return FakeCursor([])

    def executed(self, fragment):
        return [q for q in self.queries if fragment in q[0]]


def _review_row(owner, rating, *, exclusion=None, basis="hosted", wallet=None):
    """A weighted hosted review by default."""
    return {
        "reviewer_wallet": wallet or f"0xwallet-of-{owner[:8] if owner else 'x'}",
        "rating": rating,
        "payment_verified": True,
        "signature_verified": True,
        "payment_verification_level": "response_attested",
        "created_at": NOW,
        "owner_ref": owner,
        "weight_exclusion": exclusion,
        "weight_basis": basis,
    }


def _legacy_row(wallet, rating, *, level="onchain_verified"):
    """A pre-cutover review, weighed by the frozen wallet snapshot."""
    return {
        "reviewer_wallet": wallet,
        "rating": rating,
        "payment_verified": True,
        "signature_verified": True,
        "payment_verification_level": level,
        "created_at": NOW,
        "owner_ref": None,
        "weight_exclusion": "community",
        "weight_basis": "legacy",
    }


def test_recompute_stores_the_canonical_score():
    conn = FakeConn(
        {
            "from reviews": [_review_row(SEED_OWNER, 5)],
            "from review_owners": [
                {
                    "owner_ref": SEED_OWNER,
                    "raw_trust": 1.0,
                    "is_seed": True,
                    "slashed_at": None,
                }
            ],
        }
    )
    result = recompute_service_score(conn, "svc_1", NOW)
    # one trusted owner vote
    assert result.n_eff == 1.0
    assert result.score == (1.0 * 5 + KAPPA * MU0) / (1.0 + KAPPA)

    update = conn.executed("update services")[0]
    assert update[1] == (result.score, result.n_eff, 0.0, result.n_eff, NOW, "svc_1")


def test_untrusted_owners_leave_the_service_at_the_prior():
    conn = FakeConn(
        {
            "from reviews": [_review_row(HONEST_OWNER, 5)] * 20,
            "from review_owners": [
                {
                    "owner_ref": HONEST_OWNER,
                    "raw_trust": 0.0,
                    "is_seed": False,
                    "slashed_at": None,
                }
            ],
        }
    )
    result = recompute_service_score(conn, "svc_1", NOW)
    assert result.score == MU0
    assert result.n_eff == 0.0


def test_recompute_restores_a_pre_cutover_score_from_frozen_wallet_trust():
    """The grandfathering regression: a service whose only evidence predates
    the cutover still scores, out of wallet_users rather than review_owners."""
    conn = FakeConn(
        {
            "from reviews": [_legacy_row(SEED_WALLET, 5)],
            "from wallet_users": [
                {"wallet_address": SEED_WALLET, "raw_trust": 1.0, "is_seed": True, "slashed_at": None}
            ],
            "from review_owners": [],
        }
    )
    result = recompute_service_score(conn, "svc_1", NOW)
    # seed 1.0 x onchain 2.0 — v2's number, unchanged.
    assert result.n_eff_legacy == 2.0
    assert result.n_eff_hosted == 0.0
    assert result.score == (2.0 * 5 + KAPPA * MU0) / (2.0 + KAPPA) == 4.0


def test_community_reviews_from_a_seed_owner_still_weigh_nothing():
    """Trust is not a bypass: an owner's unattested review carries no weight
    however trusted the owner is."""
    conn = FakeConn(
        {
            "from reviews": [_review_row(SEED_OWNER, 5, exclusion="community")] * 5,
            "from review_owners": [
                {"owner_ref": SEED_OWNER, "raw_trust": 1.0, "is_seed": True, "slashed_at": None}
            ],
        }
    )
    result = recompute_service_score(conn, "svc_1", NOW)
    assert result.score == MU0
    assert result.n_eff == 0.0


def test_trust_update_uses_the_leave_one_out_consensus():
    # The owner's own 20 reviews are excluded, so the consensus it is scored
    # against is the seed's alone; agreeing with it earns trust.
    conn = FakeConn(
        {
            "from review_owners\n        where owner_ref": [
                {
                    "owner_ref": HONEST_OWNER,
                    "raw_trust": 0.5,
                    "is_seed": False,
                    "slashed_at": None,
                }
            ],
            "from reviews": [_review_row(SEED_OWNER, 5)]
            + [_review_row(HONEST_OWNER, 1)] * 20,
            "from review_owners": [
                {
                    "owner_ref": SEED_OWNER,
                    "raw_trust": 1.0,
                    "is_seed": True,
                    "slashed_at": None,
                },
                {
                    "owner_ref": HONEST_OWNER,
                    "raw_trust": 0.5,
                    "is_seed": False,
                    "slashed_at": None,
                },
            ],
        }
    )
    new_raw = apply_review_trust_update(conn, HONEST_OWNER, "svc_1", 5, NOW)
    assert new_raw > 0.5
    assert new_raw <= 0.5 + ETA
    assert conn.executed("update review_owners set raw_trust")[0][1] == (new_raw, NOW, HONEST_OWNER)


def test_seeds_and_slashed_owners_never_move():
    for row in (
        {"owner_ref": SEED_OWNER, "raw_trust": 1.0, "is_seed": True, "slashed_at": None},
        {"owner_ref": SEED_OWNER, "raw_trust": 0.4, "is_seed": False, "slashed_at": NOW},
    ):
        conn = FakeConn({"from review_owners\n        where owner_ref": [row]})
        assert apply_review_trust_update(conn, SEED_OWNER, "svc_1", 5, NOW) is None
        assert conn.executed("update review_owners set raw_trust") == []


def test_unknown_owner_produces_no_trust_update():
    conn = FakeConn({})
    assert apply_review_trust_update(conn, HONEST_OWNER, "svc_1", 5, NOW) is None


def test_seed_sync_pins_listed_owners_and_demotes_the_rest():
    conn = FakeConn({})
    sync_seed_owners(conn, [SEED_OWNER.upper(), "", "  "])
    inserts = conn.executed("insert into review_owners")
    assert len(inserts) == 1
    assert inserts[0][1] == (SEED_OWNER,)
    demote = conn.executed("set is_seed = false")[0]
    assert demote[1] == ([SEED_OWNER],)


def test_empty_seed_list_is_a_no_op_not_a_mass_demotion():
    conn = FakeConn({})
    sync_seed_owners(conn, [])
    assert conn.executed("insert into review_owners") == []
    assert conn.executed("set is_seed = false") == []


def test_no_wallet_registry_can_grant_trust_any_more():
    """The regression this change exists for: registering a hosted mainnet
    wallet used to pin it at trust 1.0. A wallet now earns nothing, ever, and
    the backend no longer reads the hosted registry at all."""
    conn = FakeConn({
        "to_regclass": [{"relation": "opencrowd_seed_wallets"}],
        "from opencrowd_seed_wallets": [{"wallet_address": HONEST_WALLET}],
        "insert into wallet_users": [
            {"user_id": 3, "wallet_address": HONEST_WALLET, "is_seed": False,
             "raw_trust": 0.0, "slashed_at": None}
        ],
    })
    assert ensure_user(conn, HONEST_WALLET)["is_seed"] is False
    assert conn.executed("opencrowd_seed_wallets") == []
    assert conn.executed("to_regclass") == []
    sync_seed_owners(conn, [SEED_OWNER])
    assert {q[1][0] for q in conn.executed("insert into review_owners")} == {SEED_OWNER}


def test_invalid_seed_configuration_fails_before_any_writes():
    conn = FakeConn({})
    with pytest.raises(ValueError, match="hex owner references"):
        sync_seed_owners(conn, [SEED_OWNER + "\nMPPX_TEMPO_TOKEN_ADDRESS=0x123"])
    assert conn.queries == []
