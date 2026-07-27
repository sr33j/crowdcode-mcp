"""Write-path trust and score persistence, against a scripted fake connection.

Mirrors the FakeConn/FakeCursor pattern in tests/test_rate_limit.py: no
database, just assertions that the right SQL runs with the right values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from crowdcode.reputation import (
    apply_review_trust_update,
    recompute_service_score,
    sync_seed_wallets,
)
from crowdcode.scoring import ETA, KAPPA, MU0

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
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


def _review_row(wallet, rating):
    return {
        "reviewer_wallet": wallet,
        "rating": rating,
        "payment_verified": True,
        "signature_verified": True,
        "created_at": NOW,
    }


def test_recompute_stores_the_canonical_score():
    conn = FakeConn(
        {
            "from reviews": [_review_row(SEED_WALLET, 5)],
            "from wallet_users": [
                {
                    "wallet_address": SEED_WALLET,
                    "raw_trust": 1.0,
                    "is_seed": True,
                    "slashed_at": None,
                }
            ],
        }
    )
    result = recompute_service_score(conn, "svc_1", NOW)
    # seed weight 1.0 x verified 2.0 = 2.0
    assert result.n_eff == 2.0
    assert result.score == (2.0 * 5 + KAPPA * MU0) / (2.0 + KAPPA)

    update = conn.executed("update services")[0]
    assert update[1] == (result.score, result.n_eff, NOW, "svc_1")


def test_untrusted_reviews_leave_the_service_at_the_prior():
    conn = FakeConn(
        {
            "from reviews": [_review_row(HONEST_WALLET, 5)] * 20,
            "from wallet_users": [
                {
                    "wallet_address": HONEST_WALLET,
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


def test_trust_update_uses_the_leave_one_out_consensus():
    # The wallet's own 20 reviews are excluded, so the consensus it is scored
    # against is the seed's alone (4.0 => p = 0.75); agreeing earns trust.
    conn = FakeConn(
        {
            "from wallet_users\n        where wallet_address": [
                {
                    "user_id": 7,
                    "raw_trust": 0.5,
                    "is_seed": False,
                    "slashed_at": None,
                }
            ],
            "from reviews": [_review_row(SEED_WALLET, 5)]
            + [_review_row(HONEST_WALLET, 1)] * 20,
            "from wallet_users": [
                {
                    "wallet_address": SEED_WALLET,
                    "raw_trust": 1.0,
                    "is_seed": True,
                    "slashed_at": None,
                },
                {
                    "wallet_address": HONEST_WALLET,
                    "raw_trust": 0.5,
                    "is_seed": False,
                    "slashed_at": None,
                },
            ],
        }
    )
    new_raw = apply_review_trust_update(conn, HONEST_WALLET, "svc_1", 5, NOW)
    assert new_raw > 0.5
    assert new_raw <= 0.5 + ETA
    assert conn.executed("update wallet_users set raw_trust")[0][1] == (new_raw, NOW, 7)


def test_seeds_and_slashed_wallets_never_move():
    for row in (
        {"user_id": 1, "raw_trust": 1.0, "is_seed": True, "slashed_at": None},
        {"user_id": 2, "raw_trust": 0.4, "is_seed": False, "slashed_at": NOW},
    ):
        conn = FakeConn({"from wallet_users\n        where wallet_address": [row]})
        assert apply_review_trust_update(conn, SEED_WALLET, "svc_1", 5, NOW) is None
        assert conn.executed("update wallet_users set raw_trust") == []


def test_unknown_wallet_produces_no_trust_update():
    conn = FakeConn({})
    assert apply_review_trust_update(conn, HONEST_WALLET, "svc_1", 5, NOW) is None


def test_seed_sync_pins_listed_wallets_and_demotes_the_rest():
    conn = FakeConn({})
    sync_seed_wallets(conn, [SEED_WALLET.upper(), "", "  "])
    inserts = conn.executed("insert into wallet_users")
    assert len(inserts) == 1
    assert inserts[0][1] == (SEED_WALLET,)
    demote = conn.executed("set is_seed = false")[0]
    assert demote[1] == ([SEED_WALLET],)


def test_empty_seed_list_is_a_no_op_not_a_mass_demotion():
    conn = FakeConn({})
    sync_seed_wallets(conn, [])
    assert conn.queries == []
