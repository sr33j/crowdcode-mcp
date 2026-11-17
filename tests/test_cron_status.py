"""The cron records every job's outcome, and `/api/cron/status` reports it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from crowdcode import cron
from crowdcode.server import cron_status_summary

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/crowdcode_test")
    clear = getattr(cron.get_settings, "cache_clear", None)
    if clear:
        clear()
    yield
    if clear:
        clear()


def _run(minutes_ago: int, ok: bool) -> dict:
    at = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {"started_at": at, "finished_at": at, "ok": ok,
            "failed": [] if ok else ["service_proposals"], "jobs": []}


def test_error_label_names_the_class_and_http_status_but_never_the_message():
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("postgres://user:secret@host/db sk-live-key", request=request, response=response)
    assert cron.error_label(error) == "HTTPStatusError 429"
    assert cron.error_label(RuntimeError("password=hunter2")) == "RuntimeError"


def test_every_job_runs_and_is_recorded_even_when_one_fails(monkeypatch):
    calls: list[str] = []
    recorded: list[dict] = []

    def ok(_now):
        calls.append("ok")

    def boom(_now):
        calls.append("boom")
        raise RuntimeError("summaries: all 23 summary call(s) failed")

    ok.__name__ = "run_first"
    boom.__name__ = "run_second"
    last = lambda _now: calls.append("last")  # noqa: E731
    last.__name__ = "run_third"
    monkeypatch.setattr(cron, "jobs", lambda: (ok, boom, last))
    monkeypatch.setattr(cron, "sync_seed_owners", lambda conn, owners: None)
    monkeypatch.setattr(cron, "record_status", recorded.append)

    class Conn:
        def commit(self):
            pass

    class Ctx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cron, "connect", lambda: Ctx())
    with pytest.raises(SystemExit) as exited:
        cron.main()
    assert exited.value.code == 1
    assert calls == ["ok", "boom", "last"]
    [run] = recorded
    assert run["ok"] is False and run["failed"] == ["second"]
    assert [(job["job"], job["ok"]) for job in run["jobs"]] == [
        ("seed_owners", True), ("first", True), ("second", False), ("third", True)]
    assert run["jobs"][2]["error"] == "RuntimeError"


def test_summaries_raise_when_every_model_call_fails(monkeypatch):
    review = {"rating": 4, "payment_verified": True, "created_at": None,
              "reason": "fine", "task_context": "search"}
    monkeypatch.setattr(cron, "_collect_summary_inputs",
                        lambda now: [("svc_1", "One", [review], 1), ("svc_2", "Two", [review], 1)])
    monkeypatch.setattr(cron, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(cron, "summarize_service_reviews", lambda name, reviews: None)
    with pytest.raises(RuntimeError, match="all 2 summary call"):
        cron.run_service_summaries(NOW)


def test_status_counts_failures_since_the_last_good_run():
    summary = cron_status_summary({"runs": [_run(5, False), _run(65, False), _run(125, True)]}, NOW)
    assert summary["ok"] is False
    assert summary["consecutive_failures"] == 2
    assert summary["last_success_at"] == (NOW - timedelta(minutes=125)).isoformat()
    assert summary["minutes_since_last_run"] == 5.0


def test_status_with_no_runs_is_not_ok():
    summary = cron_status_summary(None, NOW)
    assert summary["ok"] is False and summary["consecutive_failures"] == 0
    assert summary["last_finished_at"] is None
