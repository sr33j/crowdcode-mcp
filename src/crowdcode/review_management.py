"""Ownership proofs and replay keys for user-directed review management."""
from __future__ import annotations

import hashlib
import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.payments import _normalize_evm_address, canonical_payment_reference


def management_message(action: str, wallet: str, expires_at: int,
                       review_id: int = 0, before_id: int = 0, limit: int = 0) -> str:
    return "\n".join([
        "CrowdCode review management v1", f"action:{action}",
        f"wallet:{wallet.lower()}", f"review_id:{review_id}",
        f"before_id:{before_id}", f"limit:{limit}", f"expires_at:{expires_at}",
    ])


def authorize(action: str, reviewer_wallet: str, expires_at: int,
              authorization: str, **scope: int) -> str:
    wallet = _normalize_evm_address(reviewer_wallet)
    now = int(time.time())
    if wallet is None or not now < expires_at <= now + 300:
        raise ValueError("Invalid or expired review management authorization")
    try:
        recovered = Account.recover_message(
            encode_defunct(text=management_message(action, wallet, expires_at, **scope)),
            signature=authorization,
        )
    except Exception as exc:
        raise ValueError("Invalid review management signature") from exc
    if recovered.lower() != wallet.lower():
        raise ValueError("Review management signature does not match wallet")
    return wallet.lower()


def review_replay_key(wallet: str | None, reference: str | None, nonce: str | None) -> str:
    if reference is not None:
        value = "payment:" + canonical_payment_reference(reference)
    else:
        value = f"unpaid:{(wallet or '').lower()}:{nonce or ''}"
    return hashlib.sha256(value.encode()).hexdigest()


def lock_review_writes(conn: Any) -> None:
    # Serialize submissions, deletion, and nightly score replay so deletion
    # cannot race a replay or leave a score based on a removed review.
    conn.execute("select pg_advisory_xact_lock(824761029)")
