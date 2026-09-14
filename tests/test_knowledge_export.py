from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime

from starlette.requests import Request

from crowdcode import server
from tests.test_reputation import FakeConn

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _fake_connect(conn):
    @contextmanager
    def connect():
        yield conn

    return connect


def _service(service_id, name, score, n_eff):
    return {
        "id": service_id,
        "name": name,
        "directory_slug": name.lower(),
        "canonical_endpoint": f"https://{name.lower()}.dev/api",
        "payment_provider": "x402",
        "score": score,
        "n_eff": n_eff,
        "review_summary": None,
    }


def _review(service_id, rating, reason, amount=0.01):
    return {
        "service_id": service_id,
        "rating": rating,
        "reason": reason,
        "task_context": "geocode an address",
        "payment_verified": True,
        "verification_level": "onchain_verified",
        "amount": amount,
        "created_at": NOW,
    }


def test_evidence_payload_groups_and_caps_reviews(monkeypatch):
    conn = FakeConn(
        {
            "from services": [_service("svc_a", "Exa", 4.2, 12.0), _service("svc_b", "New", 3.0, 0.1)],
            "from reviews": [
                _review("svc_a", 5, "worked"),
                _review("svc_a", 4, "ok"),
                _review("svc_a", 1, "failed"),
            ],
        }
    )
    monkeypatch.setattr(server, "connect", _fake_connect(conn))
    monkeypatch.setattr(server, "redact_texts", lambda texts, fail_closed: texts)

    payload = server._knowledge_evidence_payload(max_reviews=2)

    assert payload["ok"] is True
    assert payload["stats"] == {"num_services": 2, "total_reviews": 2}
    exa, new = payload["services"]
    assert exa["service_id"] == "svc_a"
    assert exa["num_reviews"] == 2  # capped
    assert exa["unproven"] is False
    assert [r["rating"] for r in exa["reviews"]] == [5, 4]
    assert exa["reviews"][0]["amount"] == 0.01
    assert "service_id" not in exa["reviews"][0]
    assert new["unproven"] is True
    assert new["reviews"] == []


def test_evidence_payload_drops_text_when_redactor_is_down(monkeypatch):
    conn = FakeConn(
        {
            "from services": [_service("svc_a", "Exa", 4.2, 12.0)],
            "from reviews": [_review("svc_a", 5, "worked")],
        }
    )
    monkeypatch.setattr(server, "connect", _fake_connect(conn))
    monkeypatch.setattr(server, "redact_texts", lambda texts, fail_closed: None)

    payload = server._knowledge_evidence_payload(max_reviews=10)

    review = payload["services"][0]["reviews"][0]
    assert review["reason"] is None
    assert review["task_context"] is None


def _request(headers: dict[str, str], query: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/knowledge/evidence",
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_endpoint_is_disabled_without_token(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: type("S", (), {"knowledge_export_token": None})())
    response = asyncio.run(server.knowledge_evidence(_request({})))
    assert response.status_code == 404


def test_endpoint_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: type("S", (), {"knowledge_export_token": "secret"})())
    response = asyncio.run(server.knowledge_evidence(_request({"authorization": "Bearer nope"})))
    assert response.status_code == 401


def test_endpoint_returns_payload_with_token(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: type("S", (), {"knowledge_export_token": "secret"})())
    seen = {}

    def fake_payload(max_reviews):
        seen["max_reviews"] = max_reviews
        return {"ok": True, "services": [], "stats": {"num_services": 0, "total_reviews": 0}}

    monkeypatch.setattr(server, "_knowledge_evidence_payload", fake_payload)
    response = asyncio.run(
        server.knowledge_evidence(
            _request({"authorization": "Bearer secret"}, query="max_reviews=9999")
        )
    )
    assert response.status_code == 200
    assert seen["max_reviews"] == server.KNOWLEDGE_EXPORT_MAX_REVIEWS
