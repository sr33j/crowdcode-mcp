"""The reconciler's redaction call for the public home-page feed (SRI-117)."""

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from crowdcode import server as server_mod
from crowdcode.redaction import RedactionUnavailable

PATH = "/api/internal/feed/redact"
AUTH = {"authorization": "Bearer miner-token"}


def _client(monkeypatch, token="miner-token"):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    if token is None:
        monkeypatch.delenv("CROWDCODE_DEMAND_MINER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CROWDCODE_DEMAND_MINER_TOKEN", token)
    return TestClient(Starlette(routes=[Route(PATH, server_mod.feed_redact, methods=["POST"])]))


def test_it_needs_the_configured_token(monkeypatch):
    assert _client(monkeypatch, token=None).post(PATH, json={"texts": []}).status_code == 404
    client = _client(monkeypatch)
    assert client.post(PATH, json={"texts": []}).status_code == 401
    assert client.post(PATH, json={"texts": []}, headers={"authorization": "Bearer wrong"}).status_code == 401


def test_it_bounds_the_batch(monkeypatch):
    client = _client(monkeypatch)
    assert client.post(PATH, json={"texts": "x"}, headers=AUTH).status_code == 400
    assert client.post(PATH, json={"texts": ["x"] * 41}, headers=AUTH).status_code == 400
    assert client.post(PATH, json={"texts": ["x" * 4001]}, headers=AUTH).status_code == 400
    assert client.post(PATH, json={"texts": [1]}, headers=AUTH).status_code == 400


def test_it_returns_what_the_redactor_returned(monkeypatch):
    client = _client(monkeypatch)
    seen = {}

    def fake(texts, *, fail_closed, timeout):
        seen.update(texts=texts, fail_closed=fail_closed)
        return [t.replace("ada@example.com", "[EMAIL_1]") for t in texts]

    monkeypatch.setattr(server_mod, "redact_texts", fake)
    monkeypatch.setattr(server_mod, "redaction_enabled", lambda: True)
    answer = client.post(PATH, json={"texts": ["mail ada@example.com", ""]}, headers=AUTH)
    assert answer.status_code == 200
    assert answer.json() == {"ok": True, "texts": ["mail [EMAIL_1]", ""], "redacted": True}
    assert seen["fail_closed"] is True


def test_it_says_when_nothing_redacted_it(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(server_mod, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(server_mod, "redaction_enabled", lambda: False)
    assert client.post(PATH, json={"texts": ["a"]}, headers=AUTH).json()["redacted"] is False


def test_an_unavailable_redactor_is_a_503(monkeypatch):
    client = _client(monkeypatch)

    def down(texts, **_):
        raise RedactionUnavailable("down")

    monkeypatch.setattr(server_mod, "redact_texts", down)
    answer = client.post(PATH, json={"texts": ["a"]}, headers=AUTH)
    assert answer.status_code == 503
    monkeypatch.setattr(server_mod, "redact_texts", lambda texts, **_: None)
    assert client.post(PATH, json={"texts": ["a"]}, headers=AUTH).status_code == 503
