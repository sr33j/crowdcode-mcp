from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import time

import psycopg
from psycopg.rows import dict_row
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode import server, cron
from crowdcode.review_management import authorize, management_message, review_replay_key
from crowdcode.identity import build_identity
from crowdcode.payments import reason_hash
from tests.test_shared_wallet_db import db  # isolated PostgreSQL schema fixture

OWNER = Account.from_key("0x" + "42" * 32)
OTHER = Account.from_key("0x" + "43" * 32)


def proof(action, account=OWNER, **scope):
    expires_at = int(time.time()) + 240
    signature = account.sign_message(encode_defunct(text=management_message(
        action, account.address, expires_at, **scope))).signature.hex()
    return dict(reviewer_wallet=account.address, expires_at=expires_at, authorization=signature)


def test_authorization_binds_action_wallet_review_and_page():
    auth = proof("delete", review_id=7)
    assert authorize("delete", **auth, review_id=7) == OWNER.address.lower()
    for action, overrides, scope in [
        ("delete", {}, {"review_id": 8}),
        ("list", {}, {"review_id": 7}),
        ("delete", {"reviewer_wallet": OTHER.address}, {"review_id": 7}),
        ("delete", {"expires_at": int(time.time()) - 1}, {"review_id": 7}),
        ("delete", {"expires_at": int(time.time()) + 1000}, {"review_id": 7}),
        ("delete", {"authorization": "garbage"}, {"review_id": 7}),
    ]:
        with pytest.raises(ValueError):
            authorize(action, **{**auth, **overrides}, **scope)
    listing = proof("list", before_id=100, limit=25)
    with pytest.raises(ValueError):
        authorize("list", **listing, before_id=0, limit=25)


def test_cross_language_vectors():
    for vector in json.loads((Path(__file__).parents[1] / "spec/review-management-vectors.json").read_text()):
        assert management_message(**vector["args"]) == vector["message"]
        recovered = Account.recover_message(encode_defunct(text=vector["message"]), signature=vector["signature"])
        assert recovered == OWNER.address


@pytest.fixture
def managed_db(db, monkeypatch):
    conn, schema, url = db
    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as connection:
            yield connection
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(server, "connect", connect)
    monkeypatch.setattr(cron, "connect", connect)
    monkeypatch.setattr(server, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(cron, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(server, "redaction_enabled", lambda: True)
    return conn


def submit(nonce, account=OWNER):
    identity = build_identity(api_endpoint="https://example.com/ocr")
    args = dict(api_endpoint=identity.api_endpoint, rating=2, reason="Dropped a table column",
                task_context="Comparing annual reports", review_nonce=nonce,
                reviewer_wallet=account.address)
    message = server.get_review_signing_payload(api_endpoint=identity.api_endpoint, rating=2,
        reason_hash=reason_hash(args["reason"]), review_nonce=nonce)["message"]
    args["review_signature"] = account.sign_message(encode_defunct(text=message)).signature.hex()
    return args, server.review_service(**args)


def test_pagination_and_ownership(managed_db):
    ids = []
    for n in range(3):
        _, result = submit(f"attempt_{n}")
        assert result["accepted"], result
        ids.append(result["review_id"])
    _, foreign = submit("other_123", OTHER)
    assert foreign["accepted"]
    page = server.list_my_reviews(**proof("list", limit=2), limit=2)
    assert [r["review_id"] for r in page["reviews"]] == ids[:0:-1]
    assert page["next_before_id"] == ids[1]
    page2 = server.list_my_reviews(**proof("list", before_id=ids[1], limit=2), before_id=ids[1], limit=2)
    assert [r["review_id"] for r in page2["reviews"]] == [ids[0]]
    assert page2["next_before_id"] is None
    assert not server.delete_my_review(review_id=foreign["review_id"],
        **proof("delete", review_id=foreign["review_id"]))["deleted"]
    assert server.delete_my_review(review_id=ids[0],
        **proof("delete", review_id=ids[1]))["error_code"] == "invalid_authorization"


def test_delete_removes_content_replays_scores_and_rejects_old_submission(managed_db):
    conn = managed_db
    args, result = submit("delete_123")
    assert result["accepted"], result
    review_id, service_id = result["review_id"], result["service_id"]
    conn.execute("update services set review_summary = '{\"strengths\":[\"private old text\"]}', last_summarized_at=now()")
    conn.execute("update wallet_users set raw_trust=9")
    deleted = server.delete_my_review(review_id=review_id, **proof("delete", review_id=review_id))
    assert deleted["deleted"], deleted
    assert conn.execute("select * from reviews where id=%s", (review_id,)).fetchone() is None
    service = conn.execute("select score,n_eff,review_summary,last_summarized_at,review_revision from services where id=%s", (service_id,)).fetchone()
    assert service == dict(score=3.0, n_eff=0.0, review_summary=None, last_summarized_at=None, review_revision=1)
    assert conn.execute("select raw_trust from wallet_users").fetchone()["raw_trust"] == 0
    keys = conn.execute("select * from deleted_review_keys").fetchall()
    assert keys == [{"replay_key": review_replay_key(OWNER.address, None, "delete_123")}]
    _, retry = submit("delete_123")
    assert retry["error_code"] == "review_deleted"
    assert not server.delete_my_review(review_id=review_id, **proof("delete", review_id=review_id))["deleted"]


def test_deletion_cannot_be_undone_by_inflight_summary(managed_db, monkeypatch):
    conn = managed_db
    _, result = submit("summary_123")
    assert result["accepted"], result
    review_id = result["review_id"]
    def summarize(*args, **kwargs):
        assert server.delete_my_review(review_id=review_id, **proof("delete", review_id=review_id))["deleted"]
        return {"strengths": ["This text must never reappear"]}
    monkeypatch.setattr(cron, "summarize_service_reviews", summarize)
    cron.run_service_summaries(datetime.now(UTC))
    assert conn.execute("select review_summary from services").fetchone()["review_summary"] is None


def test_failed_recompute_rolls_back_deletion_and_replay_key(managed_db, monkeypatch):
    _, result = submit("rollback_123")
    assert result["accepted"], result
    review_id = result["review_id"]
    def fail(*_):
        raise RuntimeError("simulated replay failure")
    monkeypatch.setattr(cron, "replay_scores", fail)
    result = server.delete_my_review(review_id=review_id, **proof("delete", review_id=review_id))
    assert result["status"] == "unavailable"
    assert managed_db.execute("select id from reviews where id=%s", (review_id,)).fetchone()
    assert managed_db.execute("select * from deleted_review_keys").fetchall() == []


def test_paid_review_deletion_preserves_other_reviews_and_payment_replay_key(managed_db, monkeypatch):
    from crowdcode import payments
    from crowdcode.payments import BASE_USDC_ADDRESS, ERC20_TRANSFER_TOPIC
    conn = managed_db
    _, existing = submit("keep_this_review")
    payee = "0x" + "ab" * 20
    topic = lambda address: "0x" + "0" * 24 + address[2:].lower()
    monkeypatch.setattr(payments, "_rpc_transaction_receipt", lambda *_: {
        "status": "0x1", "blockNumber": "0x10", "logs": [{
            "address": BASE_USDC_ADDRESS,
            "topics": [ERC20_TRANSFER_TOPIC, topic(OWNER.address), topic(payee)],
            "data": hex(200000),
        }],
    })
    args = dict(api_endpoint="https://example.com/paid", payment_provider="x402",
                payment_target_ref=payee, payment_reference="0x" + "cd" * 32,
                rating=4, reason="Useful tables", reviewer_wallet=OWNER.address)
    def send():
        message = server.get_review_signing_payload(**{k: v for k, v in args.items()
            if k not in ("reason", "reviewer_wallet")}, reason_hash=reason_hash(args["reason"]))["message"]
        signature = OWNER.sign_message(encode_defunct(text=message)).signature.hex()
        return server.review_service(**args, review_signature=signature)
    paid = send()
    assert paid["accepted"] and paid["payment_verified"], paid
    assert server.delete_my_review(review_id=paid["review_id"], **proof("delete", review_id=paid["review_id"]))["deleted"]
    assert send()["error_code"] == "review_deleted"
    assert conn.execute("select id from reviews").fetchall() == [{"id": existing["review_id"]}]
    assert conn.execute("select * from deleted_review_keys").fetchall() == [
        {"replay_key": review_replay_key(OWNER.address, args["payment_reference"], None)}]
