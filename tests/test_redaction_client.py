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
    monkeypatch.delenv("REDACTION_REQUIRED", raising=False)


def test_disabled_when_unconfigured():
    assert not redaction_enabled()
    assert redact_texts(["raw text", None], fail_closed=True) == ["raw text", None]


def test_required_but_unconfigured_fails_closed(monkeypatch):
    monkeypatch.setenv("REDACTION_REQUIRED", "1")
    with pytest.raises(RedactionUnavailable):
        redact_texts(["raw text"], fail_closed=True)


def test_required_but_unconfigured_drops_egress_text(monkeypatch):
    monkeypatch.setenv("REDACTION_REQUIRED", "1")
    assert redact_texts(["raw text"], fail_closed=False) is None


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


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(redaction.time, "sleep", slept.append)
    return slept


def test_redacts_and_preserves_none_positions(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(
        redaction.httpx,
        "post",
        _fake_post({"texts": ["email [EMAIL_1]"]}),
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


def _recording_post(calls, *, fail=lambda call, texts: False):
    """Echo-redacting fake that records each chunk and can fail chosen calls."""

    class Response:
        def __init__(self, texts):
            self.texts = texts

        def raise_for_status(self):
            pass

        def json(self):
            return {"texts": [f"R({text})" for text in self.texts]}

    def post(url, **kwargs):
        texts = kwargs["json"]["texts"]
        calls.append(texts)
        if fail(len(calls), texts):
            raise redaction.httpx.ReadTimeout("timed out")
        return Response(texts)

    return post


def test_large_batches_are_sent_in_chunks(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []
    monkeypatch.setattr(redaction.httpx, "post", _recording_post(calls))
    texts = [f"t{i}" for i in range(25)]
    assert redact_texts(texts, fail_closed=False) == [f"R(t{i})" for i in range(25)]
    assert [len(call) for call in calls] == [10, 10, 5]


def test_none_and_empty_texts_are_not_sent(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []
    monkeypatch.setattr(redaction.httpx, "post", _recording_post(calls))
    assert redact_texts([None, "", "a"], fail_closed=True) == [None, "", "R(a)"]
    assert calls == [["a"]]
    assert redact_texts([None, ""], fail_closed=True) == [None, ""]
    assert calls == [["a"]]


def test_a_timed_out_chunk_is_retried(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []
    monkeypatch.setattr(
        redaction.httpx, "post", _recording_post(calls, fail=lambda n, _: n == 1)
    )
    assert redact_texts(["a"], fail_closed=True) == ["R(a)"]
    assert len(calls) == 2


def test_partial_mode_drops_only_the_failed_chunk(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []
    monkeypatch.setattr(
        redaction.httpx,
        "post",
        _recording_post(calls, fail=lambda _, texts: "t12" in texts),
    )
    texts = [f"t{i}" for i in range(15)]
    out = redact_texts(texts, fail_closed=False, allow_partial=True)
    assert out == [f"R(t{i})" for i in range(10)] + [None] * 5
    # Without allow_partial the whole batch is dropped.
    assert redact_texts(texts, fail_closed=False) is None
    with pytest.raises(RedactionUnavailable):
        redact_texts(texts, fail_closed=True)


def test_partial_mode_returns_none_when_every_chunk_fails(monkeypatch):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []
    monkeypatch.setattr(
        redaction.httpx, "post", _recording_post(calls, fail=lambda *_: True)
    )
    assert redact_texts(["a", "b"], fail_closed=False, allow_partial=True) is None
    assert redact_texts(["a"], fail_closed=False, attempts=1) is None
    assert len(calls) == 3


def test_connection_errors_pause_before_retrying(monkeypatch, no_retry_sleep):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"]["texts"])
        if len(calls) == 1:
            raise redaction.httpx.ConnectError("connection refused")
        return type("R", (), {"raise_for_status": lambda self: None,
                              "json": lambda self: {"texts": ["ok"]}})()

    monkeypatch.setattr(redaction.httpx, "post", post)
    assert redact_texts(["a"], fail_closed=True) == ["ok"]
    assert no_retry_sleep == [redaction.RETRY_DELAY]


def test_timeouts_retry_without_pausing(monkeypatch, no_retry_sleep):
    monkeypatch.setenv("REDACTOR_URL", "http://127.0.0.1:8090")
    calls = []
    monkeypatch.setattr(
        redaction.httpx, "post", _recording_post(calls, fail=lambda n, _: n == 1)
    )
    assert redact_texts(["a"], fail_closed=True) == ["R(a)"]
    assert no_retry_sleep == []
