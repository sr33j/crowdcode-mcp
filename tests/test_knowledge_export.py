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


def _counts(service_id, num_reviews, **extra):
    return {
        "service_id": service_id,
        "num_reviews": num_reviews,
        "num_verified_reviews": num_reviews,
        "num_weighted_reviews": 0,
        "num_legacy_reviews": num_reviews,
        "r1": 0, "r2": 0, "r3": 0, "r4": 0, "r5": num_reviews,
        "median_paid_amount": 0.01,
        **extra,
    }


def test_evidence_payload_carries_counts_and_summaries_not_reviews(monkeypatch):
    summary = {"version": 2, "pros": ["Fast geocoding"], "cons": [], "watch_out": [],
               "n_reviews": 3, "through_date": "2026-09-13", "generated_at": "x"}
    conn = FakeConn(
        {
            "from services": [
                {**_service("svc_a", "Exa", 4.2, 12.0), "review_summary": summary},
                _service("svc_b", "New", 3.0, 0.1),
            ],
            "from reviews": [_counts("svc_a", 3)],
        }
    )
    monkeypatch.setattr(server, "connect", _fake_connect(conn))
    monkeypatch.setattr(server, "redact_texts", lambda texts, fail_closed, **_: texts)

    payload = server._knowledge_evidence_payload()

    assert payload["ok"] is True
    assert payload["stats"] == {"num_services": 2, "total_reviews": 3}
    exa, new = payload["services"]
    assert exa["service_id"] == "svc_a"
    assert exa["num_reviews"] == 3
    assert exa["histogram"]["5"] == 3
    assert exa["median_paid_amount"] == 0.01
    assert exa["summary"]["pros"] == ["Fast geocoding"]
    assert "reviews" not in exa
    # The export never selects review text at all.
    review_sql = " ".join(sql for sql, _ in conn.executed("from reviews"))
    assert "reason" not in review_sql and "task_context" not in review_sql
    assert new["unproven"] is True
    assert new["num_reviews"] == 0
    assert new["summary"] is None


def test_evidence_payload_drops_summaries_when_redactor_is_down(monkeypatch):
    summary = {"version": 2, "pros": ["Fast"], "cons": [], "watch_out": []}
    conn = FakeConn(
        {
            "from services": [{**_service("svc_a", "Exa", 4.2, 12.0), "review_summary": summary}],
            "from reviews": [_counts("svc_a", 1)],
        }
    )
    monkeypatch.setattr(server, "connect", _fake_connect(conn))
    monkeypatch.setattr(server, "redact_texts", lambda texts, fail_closed, **_: None)

    payload = server._knowledge_evidence_payload()

    assert payload["services"][0]["summary"] is None


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

    def fake_payload():
        return {"ok": True, "services": [], "stats": {"num_services": 0, "total_reviews": 0}}

    monkeypatch.setattr(server, "_knowledge_evidence_payload", fake_payload)
    response = asyncio.run(
        server.knowledge_evidence(_request({"authorization": "Bearer secret"}))
    )
    assert response.status_code == 200
