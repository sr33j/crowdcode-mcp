"""Tests for the board's canonical payloads, verification, and derived state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.board import (
    BoardValidationError,
    aggregate_demand,
    board_decay,
    canonical_bounty_amount,
    canonical_post_payload,
    post_id_from_payload,
    rank_score,
    text_hash,
    verify_board_write,
)
from crowdcode.scoring import TrustRow

ACCOUNT = Account.from_key("0x" + "42" * 32)
WALLET = ACCOUNT.address.lower()
NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=UTC)
TIMESTAMP = "2026-08-19T12:00:00Z"
NONCE = "aabbccddeeff00112233445566778899"


def _sign(message: str) -> str:
    return ACCOUNT.sign_message(encode_defunct(text=message)).signature.hex()


def _signed_write(**overrides):
    kwargs = dict(
        wallet=WALLET,
        text="Resolve citations to papers — worth ~$0.10 per lookup.",
        bounty_amount="5",
        timestamp=TIMESTAMP,
        nonce=NONCE,
        parent_post_id=None,
    )
    kwargs.update(overrides)
    message = canonical_post_payload(
        wallet=kwargs["wallet"],
        text=kwargs["text"],
        bounty_amount=canonical_bounty_amount(kwargs["bounty_amount"]),
        timestamp=kwargs["timestamp"],
        nonce=kwargs["nonce"],
        parent_post_id=kwargs["parent_post_id"],
    )
    return kwargs, message


# --- bounty canonicalization -------------------------------------------------


def test_bounty_canonical_forms():
    assert canonical_bounty_amount(None) is None
    assert canonical_bounty_amount("") is None
    assert canonical_bounty_amount("0") == "0"
    assert canonical_bounty_amount("05") == "5"
    assert canonical_bounty_amount("5.0") == "5"
    assert canonical_bounty_amount("1.50") == "1.5"
    assert canonical_bounty_amount("0.250000") == "0.25"
    assert canonical_bounty_amount(5) == "5"


@pytest.mark.parametrize("bad", ["-1", "1e3", ".5", "5.", "0.1234567", "1000001", "abc"])
def test_bounty_rejects_invalid(bad):
    with pytest.raises(BoardValidationError):
        canonical_bounty_amount(bad)


# --- canonical payload and id ------------------------------------------------


def test_post_and_comment_payload_types():
    post = json.loads(
        canonical_post_payload(
            wallet=WALLET, text="x", bounty_amount=None, timestamp=TIMESTAMP,
            nonce=NONCE,
        )
    )
    assert post["type"] == "crowdcode.post.v1"
    assert "parent_post_id" not in post
    assert post["text_hash"] == text_hash("x")
    comment = json.loads(
        canonical_post_payload(
            wallet=WALLET, text="x", bounty_amount="0", timestamp=TIMESTAMP,
            nonce=NONCE, parent_post_id="post_" + "0" * 20,
        )
    )
    assert comment["type"] == "crowdcode.comment.v1"
    assert comment["parent_post_id"] == "post_" + "0" * 20


def test_post_id_is_content_addressed():
    _, message = _signed_write()
    assert post_id_from_payload(message) == post_id_from_payload(message)
    _, other = _signed_write(nonce="0123456789abcdef")
    assert post_id_from_payload(message) != post_id_from_payload(other)
    assert post_id_from_payload(message).startswith("post_")
    assert len(post_id_from_payload(message)) == len("post_") + 20


# --- signature verification --------------------------------------------------


def test_verify_accepts_valid_signature():
    kwargs, message = _signed_write()
    verified = verify_board_write(signature=_sign(message), now=NOW, **kwargs)
    assert verified.wallet == WALLET
    assert verified.bounty_amount == "5"
    assert verified.post_id == post_id_from_payload(message)


def test_verify_normalizes_bounty_before_signature_check():
    # Client signed the canonical "1.5"; submitting "1.50" must still verify.
    kwargs, message = _signed_write(bounty_amount="1.50")
    verified = verify_board_write(signature=_sign(message), now=NOW, **kwargs)
    assert verified.bounty_amount == "1.5"


def test_verify_rejects_wrong_wallet():
    kwargs, message = _signed_write()
    kwargs["wallet"] = "0x" + "99" * 20
    with pytest.raises(BoardValidationError, match="signature"):
        verify_board_write(signature=_sign(message), now=NOW, **kwargs)


def test_verify_rejects_tampered_text():
    kwargs, message = _signed_write()
    kwargs["text"] = "different text"
    with pytest.raises(BoardValidationError, match="signature"):
        verify_board_write(signature=_sign(message), now=NOW, **kwargs)


def test_verify_rejects_stale_timestamp():
    kwargs, message = _signed_write()
    with pytest.raises(BoardValidationError, match="timestamp"):
        verify_board_write(
            signature=_sign(message), now=NOW + timedelta(hours=1), **kwargs
        )


def test_verify_rejects_oversized_text():
    kwargs, _ = _signed_write(text="x" * 5000)
    with pytest.raises(BoardValidationError, match="at most"):
        verify_board_write(signature="0xdead", now=NOW, **kwargs)


def test_verify_rejects_bad_nonce_and_timestamp_format():
    kwargs, message = _signed_write()
    bad_nonce = dict(kwargs, nonce="xyz")
    with pytest.raises(BoardValidationError, match="nonce"):
        verify_board_write(signature=_sign(message), now=NOW, **bad_nonce)
    bad_ts = dict(kwargs, timestamp="2026-08-19 12:00:00")
    with pytest.raises(BoardValidationError, match="timestamp"):
        verify_board_write(signature=_sign(message), now=NOW, **bad_ts)


# --- demand aggregation ------------------------------------------------------

TRUSTED = TrustRow(raw_trust=1.0)
UNPROVEN = TrustRow(raw_trust=0.0)


def test_demand_takes_max_per_wallet_not_sum():
    summary = aggregate_demand(
        [("0xaa", "50"), ("0xaa", "50"), ("0xaa", "10")], {"0xaa": TRUSTED}
    )
    assert summary.total_stated_usd == 50.0
    assert summary.trusted_stated_usd == 50.0
    assert summary.num_backers == 1


def test_demand_separates_unproven_wallets():
    summary = aggregate_demand(
        [("0xaa", "50"), ("0xbb", "100")], {"0xaa": TRUSTED, "0xbb": UNPROVEN}
    )
    assert summary.total_stated_usd == 150.0
    assert summary.trusted_stated_usd == 50.0
    assert summary.num_backers == 2


def test_demand_unknown_wallet_counts_as_unproven():
    summary = aggregate_demand([("0xcc", "25")], {})
    assert summary.total_stated_usd == 25.0
    assert summary.trusted_stated_usd == 0.0


def test_zero_bounty_is_a_backer():
    summary = aggregate_demand([("0xaa", "0")], {"0xaa": TRUSTED})
    assert summary.total_stated_usd == 0.0
    assert summary.num_backers == 1


# --- ranking -----------------------------------------------------------------


def test_rank_score_orders_by_trusted_demand():
    fresh = NOW - timedelta(days=1)
    low = rank_score(0.5, 0.0, fresh, NOW)
    high = rank_score(0.5, 100.0, fresh, NOW)
    assert high > low > 0


def test_rank_score_decays_with_age():
    assert rank_score(0.5, 10.0, NOW - timedelta(days=60), NOW) < rank_score(
        0.5, 10.0, NOW - timedelta(days=1), NOW
    )
    assert board_decay(NOW - timedelta(days=30), NOW) == pytest.approx(0.5)


# --- cross-language vectors --------------------------------------------------


def test_board_vectors_match_reference():
    vectors = json.loads(
        (Path(__file__).parent.parent / "spec" / "board-payload-vectors.json").read_text()
    )
    for case in vectors["bounty_normalization"]:
        if "error" in case:
            with pytest.raises(BoardValidationError):
                canonical_bounty_amount(case["input"])
        else:
            assert canonical_bounty_amount(case["input"]) == case["expected"]
    for case in vectors["board_payload"]:
        amount = canonical_bounty_amount(case["bounty_amount"])
        message = canonical_post_payload(
            wallet=case["wallet"][:2] + case["wallet"][2:].lower(),
            text=case["text"],
            bounty_amount=amount,
            timestamp=case["timestamp"],
            nonce=case["nonce"],
            parent_post_id=case["parent_post_id"],
        )
        assert amount == case["expected_bounty_amount"], case["name"]
        assert message == case["expected_message"], case["name"]
        assert post_id_from_payload(message) == case["expected_post_id"], case["name"]
