"""Tests for the redaction sidecar client (src/crowdcode/redaction.py)."""

from __future__ import annotations

import pytest

from crowdcode import redaction
from crowdcode.redaction import (
    RedactionUnavailable,
    redact_texts,
    redaction_enabled,
)


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    monkeypatch.delenv("REDACTOR_URL", raising=False)
    monkeypatch.delenv("REDACTOR_TOKEN", raising=False)


def test_disabled_when_unconfigured():
    assert not redaction_enabled()
    assert redact_texts(["raw text", None], fail_closed=True) == ["raw text", None]


def test_enabled_when_configured(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    assert redaction_enabled()


def _fake_post(payload, *, status_ok=True):
    class Response:
        def raise_for_status(self):
            if not status_ok:
                raise RuntimeError("boom")

        def json(self):
            return payload

    def post(url, **kwargs):
        return Response()

    return post


def test_redacts_and_preserves_none_positions(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(
        redaction.httpx,
        "post",
        _fake_post({"texts": ["email [EMAIL_1]", ""]}),
    )
    assert redact_texts(["email a@b.com", None], fail_closed=True) == [
        "email [EMAIL_1]",
        None,
    ]


def test_sends_token_header_when_set(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("REDACTOR_TOKEN", "s3cret")
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"texts": ["ok"]}

    def post(url, **kwargs):
        seen.update(kwargs["headers"])
        return Response()

    monkeypatch.setattr(redaction.httpx, "post", post)
    redact_texts(["x"], fail_closed=True)
    assert seen["x-redactor-token"] == "s3cret"


def test_fail_closed_raises_on_error(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(
        redaction.httpx, "post", _fake_post({}, status_ok=False)
    )
    with pytest.raises(RedactionUnavailable):
        redact_texts(["x"], fail_closed=True)


def test_fail_open_returns_none_on_error(monkeypatch):
    """Egress callers get None and must drop the free text rather than leak."""
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(
        redaction.httpx, "post", _fake_post({}, status_ok=False)
    )
    assert redact_texts(["x"], fail_closed=False) is None


def test_mismatched_batch_size_is_a_failure(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(
        redaction.httpx, "post", _fake_post({"texts": ["only-one"]})
    )
    with pytest.raises(RedactionUnavailable):
        redact_texts(["a", "b"], fail_closed=True)
