"""Tests for wallet-keyed rolling-window rate limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from crowdcode.rate_limit import (
    WINDOW_SECONDS,
    check_request_limit,
    identity_id_from_wallet,
    rate_limit_payload,
)

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql, params):
        self.queries.append((sql, params))
        return FakeCursor(self.row)


def test_under_limit_allows_with_remaining():
    conn = FakeConn({"n": 2, "oldest": NOW - timedelta(hours=3)})
    result = check_request_limit(conn, "req-id", 5, NOW)
    assert result.allowed
    assert result.retry_after_seconds is None
    assert result.remaining == 2  # 5 max - 2 used - 1 for this action


def test_at_limit_blocks_with_retry_after():
    oldest = NOW - timedelta(hours=20)
    conn = FakeConn({"n": 5, "oldest": oldest})
    result = check_request_limit(conn, "req-id", 5, NOW)
    assert not result.allowed
    assert result.remaining == 0
    # The window frees up when the oldest hit ages out: 24h - 20h = 4h.
    assert result.retry_after_seconds == 4 * 3600
    assert result.limit == {
        "scope": "requester_daily",
        "max": 5,
        "window_seconds": WINDOW_SECONDS,
    }


def test_zero_limit_disables_without_query():
    conn = FakeConn({"n": 999, "oldest": NOW})
    result = check_request_limit(conn, "req-id", 0, NOW)
    assert result.allowed
    assert result.remaining is None
    assert conn.queries == []


def test_blocked_with_missing_oldest_falls_back_to_full_window():
    conn = FakeConn({"n": 5, "oldest": None})
    result = check_request_limit(conn, "req-id", 5, NOW)
    assert not result.allowed
    assert result.retry_after_seconds == WINDOW_SECONDS


def test_retry_after_is_at_least_one_second():
    oldest = NOW - timedelta(seconds=WINDOW_SECONDS)  # ages out right now
    conn = FakeConn({"n": 5, "oldest": oldest})
    result = check_request_limit(conn, "req-id", 5, NOW)
    assert not result.allowed
    assert result.retry_after_seconds == 1


def test_query_scopes_by_identity_and_window():
    conn = FakeConn({"n": 0, "oldest": None})
    check_request_limit(conn, "req-id", 5, NOW)
    sql, params = conn.queries[0]
    assert "requester_id = %s" in sql
    assert params == ("req-id", NOW - timedelta(seconds=WINDOW_SECONDS))


def test_rate_limit_payload_shape():
    conn = FakeConn({"n": 5, "oldest": NOW - timedelta(hours=23)})
    result = check_request_limit(conn, "req-id", 5, NOW)
    payload = rate_limit_payload(
        result, "5 service requests per wallet per 24 hours",
        retry_tool="request_service",
    )
    assert payload["accepted"] is False
    assert payload["rate_limited"] is True
    assert payload["reason"].startswith("rate limit exceeded:")
    assert payload["retry_after_seconds"] == 3600
    assert payload["limit"]["scope"] == "requester_daily"
    next_step = payload["next_step"]
    assert next_step["action"] == "wait_and_retry"
    assert next_step["retry"] == {
        "tool": "request_service",
        "after_seconds": 3600,
        "with": {},
    }


def test_identity_id_is_salted_and_case_insensitive(monkeypatch):
    monkeypatch.setenv("CROWDCODE_REVIEWER_SALT", "test-salt")
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    wallet = "0x" + "Ab" * 20
    assert identity_id_from_wallet(wallet) == identity_id_from_wallet(wallet.lower())
    assert len(identity_id_from_wallet(wallet)) == 64


def test_board_post_limit_uses_wallet_and_parent_null():
    from crowdcode.rate_limit import check_board_limit

    conn = FakeConn({"n": 4, "oldest": NOW - timedelta(hours=3)})
    result = check_board_limit(conn, "0xabc", 5, NOW, comments=False)
    assert result.allowed
    assert result.remaining == 0
    sql, params = conn.queries[0]
    assert "parent_post_id is null" in sql
    assert params[0] == "0xabc"
    assert result.limit["scope"] == "board_post_daily"


def test_board_comment_limit_blocks_at_cap():
    from crowdcode.rate_limit import check_board_limit

    conn = FakeConn({"n": 20, "oldest": NOW - timedelta(hours=23)})
    result = check_board_limit(conn, "0xabc", 20, NOW, comments=True)
    assert not result.allowed
    assert result.retry_after_seconds == 3600
    sql, _ = conn.queries[0]
    assert "parent_post_id is not null" in sql
    assert result.limit["scope"] == "board_comment_daily"
