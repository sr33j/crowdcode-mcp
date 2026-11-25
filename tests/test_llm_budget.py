"""w111: one daily model budget, through the one function every model call takes.

On 2026-11-25 the bill was $15 a day and nothing capped the total. These tests
call `summaries._chat_json` against a local OpenAI-shaped server over HTTP, with
the ledger in a real PostgreSQL schema.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crowdcode import llm_budget, relevant
from crowdcode import settings as settings_mod
from crowdcode.llm_budget import BudgetExhausted
from crowdcode.settings import get_settings
from crowdcode.summaries import _chat_json
from tests.test_shared_wallet_db import db  # noqa: F401 - fixture


class OpenAIStub:
    """Answers /chat/completions with a fixed usage, and counts the calls."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.usage = {"prompt_tokens": 10_000, "completion_tokens": 1_000}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                stub.calls.append(body)
                system = body["messages"][0]["content"]
                content = ({"queries": ["video generation"]} if "search queries" in system
                           else {"picks": [{"n": 1}], "none_fit": False} if "choose which paid" in system
                           else {"ok": True})
                out = json.dumps({"choices": [{"message": {"content": json.dumps(content)}}],
                                  "usage": stub.usage}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def model(db, monkeypatch):  # noqa: F811
    _, schema, url = db
    monkeypatch.setenv("DATABASE_URL", f"{url}?options=-csearch_path%3D{schema}")
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: None)
    stub = OpenAIStub()
    monkeypatch.setenv("OPENAI_BASE_URL", stub.url)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    for name in ("CROWDCODE_LLM_DAILY_USD", *(f"CROWDCODE_LLM_{job.upper()}_DAILY_USD" for job in llm_budget.JOB_CAPS)):
        monkeypatch.delenv(name, raising=False)
    yield stub
    stub.close()


def ask(job: str, model_name: str = "gpt-5-mini") -> dict:
    return _chat_json(get_settings(), "system", {"q": 1}, model=model_name, job=job)


def test_every_call_is_recorded_by_job_and_model(model):
    ask("summaries")
    ask("search")
    status = llm_budget.status()
    # gpt-5-mini: 10k in at $0.25/M and 1k out at $2/M.
    assert status["jobs"]["summaries"]["usd"] == pytest.approx(0.0045)
    assert status["jobs"]["summaries"]["calls"] == 1
    assert status["spent_usd"] == pytest.approx(0.009)
    assert status["daily_usd"] == llm_budget.DEFAULT_DAILY_USD
    assert list(status["recent_days_usd"].values()) == [pytest.approx(0.009)]


def test_a_job_past_its_share_is_refused_before_the_call(model, monkeypatch):
    monkeypatch.setenv("CROWDCODE_LLM_SUMMARIES_DAILY_USD", "0.004")
    ask("summaries")  # $0.0045: a call that fits is made, and its answer may overshoot
    with pytest.raises(BudgetExhausted):
        ask("summaries")
    assert len(model.calls) == 1  # refused before anything was sent
    ask("screen")  # another job's share is untouched
    assert len(model.calls) == 2


def test_the_hourly_gpt55_regroup_no_longer_fits_in_a_day(model, monkeypatch):
    """What cost $13.30 a day: a 31k-token, 13k-token-answer gpt-5.5 call every hour."""
    model.usage = {"prompt_tokens": 31_388, "completion_tokens": 13_273}
    monkeypatch.setenv("CROWDCODE_LLM_PROPOSALS_DAILY_USD", "1")
    ask("proposals", "gpt-5.5")
    assert llm_budget.status()["spent_usd"] == pytest.approx(0.555, abs=0.001)
    with pytest.raises(BudgetExhausted):
        ask("proposals", "gpt-5.5")
    assert len(model.calls) == 1


def test_batch_jobs_never_spend_searchs_share_and_search_falls_back_to_retrieval(model, monkeypatch):
    monkeypatch.setenv("CROWDCODE_LLM_DAILY_USD", "0.02")
    monkeypatch.setenv("CROWDCODE_LLM_SEARCH_DAILY_USD", "0.01")
    monkeypatch.setenv("CROWDCODE_LLM_PROPOSALS_DAILY_USD", "0.02")
    model.usage = {"prompt_tokens": 1_000, "completion_tokens": 100}  # gpt-5.5: $0.008 a call
    ask("proposals", "gpt-5.5")
    ask("proposals", "gpt-5.5")
    with pytest.raises(BudgetExhausted, match="reserved_for_search"):
        ask("proposals", "gpt-5.5")
    model.usage = {"prompt_tokens": 10_000, "completion_tokens": 1_000}  # gpt-5-mini: $0.0045
    ask("search")
    with pytest.raises(BudgetExhausted):
        ask("search")
    sent = len(model.calls)

    def search(query, *, limit, **_):
        return {"ok": True, "services": [{"endpoint": "https://video.example/generate", "method": "POST"}]}

    out = relevant.find_relevant("make a short video of ocean waves at sunset", search=search,
                                 cache=relevant.AnswerCache())
    assert out["ok"] is True and out["reranked"] is False
    assert [s["endpoint"] for s in out["services"]] == ["https://video.example/generate"]
    assert len(model.calls) == sent


def test_the_owner_hears_at_80_percent_and_at_100_once_each(model, monkeypatch, capsys):
    monkeypatch.setenv("CROWDCODE_LLM_DAILY_USD", "0.01")
    monkeypatch.setenv("CROWDCODE_LLM_SEARCH_DAILY_USD", "0")
    monkeypatch.setenv("CROWDCODE_LLM_SUMMARIES_DAILY_USD", "1")

    def lines():
        return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]

    ask("summaries")
    assert lines() == []
    ask("summaries")  # $0.009 of $0.01
    [warning] = lines()
    assert warning["event"] == "llm.budget" and warning["kind"] == "warning"
    assert warning["spent_usd"] == pytest.approx(0.009) and warning["jobs"] == {"summaries": pytest.approx(0.009)}
    ask("summaries")  # past the budget by one call's answer, then refused
    assert [line["kind"] for line in lines()] == ["exhausted"]
    with pytest.raises(BudgetExhausted):
        ask("summaries")
    assert lines() == []
    # Tomorrow is a new day.
    llm_budget.check("summaries", now=datetime.now(UTC) + timedelta(days=1))


def test_a_ledger_that_cannot_be_read_refuses_the_call(model, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:1/none")
    with pytest.raises(BudgetExhausted):
        ask("search")
    assert model.calls == []


@pytest.mark.parametrize("model_name,usage,usd", [
    ("gpt-5-mini-2025-08-07", {"prompt_tokens": 1_000_000, "completion_tokens": 0}, 0.25),
    ("gpt-5.5", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
                 "prompt_tokens_details": {"cached_tokens": 1_000_000}}, 30.5),
    ("a-model-nobody-priced", {"prompt_tokens": 0, "completion_tokens": 1_000_000}, 30.0),
])
def test_cost_follows_the_price_list_and_errs_high_on_an_unknown_model(model_name, usage, usd):
    assert llm_budget.cost(model_name, usage) == pytest.approx(usd)
