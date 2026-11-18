"""The nightly demand miner's backend half (demand_miner.py, SRI-115 C8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from crowdcode import demand_miner, proposals
from crowdcode import server as server_mod
from crowdcode.demand import SourceIndex
from crowdcode.request_screen import ScreenResult
from tests.test_hosted_attestation import db, hosted  # noqa: F401 - fixtures

NOW = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
REFS = ["a" * 32, "b" * 32, "c" * 32]


def _run(ref, prompt, **extra):
    return {"ref": ref, "prompt": prompt, "turns": 9, "verdict": "failed", **extra}


def _body(night="2026-11-17"):
    return {"night": night, "runs": [
        _run(REFS[0], "Read the article from a newspaper archive and summarise it for me please",
             failed_tools=[{"tool": "fetch_url", "error": "403 paywall"}]),
        _run(REFS[1], "Get me the full text of a magazine story the film was based on",
             final="I could not access the publisher's archive"),
        _run(REFS[2], "Post our daily update to our social account every morning at nine",
             paid=[{"service": "social.example/post", "outcome": "failed", "usd": 0.02}]),
    ]}


LICENSED = {
    "input": "a publication name, a headline and an approximate date for a news article",
    "output": "the licensed full text of that article with its byline and publication date",
    "problem": "agents hit paywalls and cannot read the articles owners ask them to summarise",
    "builds_on": "news archive licensing APIs from major aggregators and publisher syndication feeds",
    "frequency": "per_task_like_this", "price_per_call": 0.4, "capability": "search", "runs": [0, 1],
}
SCHEDULER = {
    "input": "post text, optional media and a recurring schedule for a social account",
    "output": "a confirmation of each scheduled post as it goes out, posted exactly once",
    "problem": "agents run once and cannot come back every morning to publish a recurring post",
    "builds_on": "official social platform posting APIs plus a hosted scheduler with retries",
    "frequency": "recurring", "price_per_call": 0.05, "capability": "social", "runs": [1, 2],
}


class Recorder:
    """Counts open connections so a test can assert none is open during a call."""

    def __init__(self, connect):
        self.inner, self.open = connect, 0

    def __call__(self):
        recorder = self

        class _Ctx:
            def __enter__(self):
                recorder.open += 1
                self.cm = recorder.inner()
                return self.cm.__enter__()

            def __exit__(self, *exc):
                recorder.open -= 1
                return self.cm.__exit__(*exc)

        return _Ctx()


@pytest.fixture
def miner(hosted, monkeypatch):  # noqa: F811
    monkeypatch.setattr(demand_miner, "redact_texts", lambda texts, **_: texts)
    monkeypatch.setattr(demand_miner, "redaction_enabled", lambda: True)
    monkeypatch.setenv("CROWDCODE_PROPOSAL_MODEL", "gpt-5.5")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return Recorder(server_mod.connect)


def _chat(services, recorder, usage=None):
    calls = []

    def chat(system, payload, out_usage):
        assert recorder.open == 0, "a connection was held across the model call"
        calls.append(payload)
        out_usage.update(usage or {"prompt_tokens": 4000, "completion_tokens": 1500})
        return {"services": services}

    chat.calls = calls
    return chat


def _accept(fields, usage):
    usage.update({"prompt_tokens": 1200, "completion_tokens": 200})
    return ScreenResult("accept")


def test_a_batch_is_mined_into_miner_requests_with_salted_refs_only(miner):
    chat = _chat([LICENSED, SCHEDULER], miner)
    result = demand_miner.mine(_body(), miner, NOW, chat=chat, screen=_accept)
    assert result["stored"] == 2 and result["rejected"] == {}
    assert 0 < result["usd"] < 0.25 and result["spent_today_usd"] == result["usd"]
    # The model saw summaries, by batch index, never the refs.
    sent = chat.calls[0]["runs"]
    assert [r["i"] for r in sent] == [0, 1, 2] and "ref" not in sent[0]
    with miner() as conn:
        rows = conn.execute("select * from service_requests order by id").fetchall()
    assert [r["source"] for r in rows] == ["miner", "miner"]
    assert rows[0]["requester_id"] is None and rows[0]["requester_wallet"] is None
    assert rows[0]["miner_run_count"] == 2 and rows[0]["miner_run_refs"] == REFS[:2]
    assert rows[0]["fields_source"] == "miner" and rows[0]["screen_verdict"] == "accept"
    assert rows[1]["frequency"] == "recurring" and rows[1]["capability"] == "social"
    # Mined needs have the same fields as agent requests: no kind, no unlocks.
    assert all(r["request_kind"] is None and r["unlocks"] is None for r in rows)
    schema = chat.calls[0]["schema"]["services"][0]
    assert "kind" not in schema and "unlocks" not in schema
    assert not any("leverage" in rule or "unlocks" in rule for rule in chat.calls[0]["requirements"])


def test_a_retried_batch_is_not_mined_twice(miner):
    demand_miner.mine(_body(), miner, NOW, chat=_chat([LICENSED], miner), screen=_accept)
    again = demand_miner.mine(_body(), miner, NOW, chat=_chat([LICENSED], miner), screen=_accept)
    assert again["duplicate"] is True
    with miner() as conn:
        assert conn.execute("select count(*) as n from service_requests").fetchone()["n"] == 1


@pytest.mark.parametrize("change, reason", [
    ({"output": "the article text from https://news.example.com/story with its byline"}, "traceable"),
    ({"input": "an article id like 12345678 and a publication date"}, "traceable"),
    ({"builds_on": "news licensing APIs used by [PERSON_1] and syndication feeds"}, "traceable"),
    ({"problem": "Get me the full text of a magazine story the film was based on"}, "copies_run"),
    ({"builds_on": "N/A"}, "invalid"),
    ({"runs": [7]}, "no_runs"),
    ({"runs": [0]}, "single_run"),
    ({"runs": [0, 0, 9]}, "single_run"),
    ({"frequency": "sometimes"}, "invalid"),
    ({"builds_on": "nothing exists for this yet"}, "invalid"),
])
def test_candidates_that_fail_a_check_are_not_stored(miner, change, reason):
    result = demand_miner.mine(_body(), miner, NOW, chat=_chat([{**LICENSED, **change}], miner),
                               screen=_accept)
    assert result["stored"] == 0 and result["rejected"] == {reason: 1}


def test_the_screen_fails_closed_for_the_miner(miner):
    def screen(fields, usage):
        return ScreenResult("unchecked", "model unavailable")

    result = demand_miner.mine(_body(), miner, NOW, chat=_chat([LICENSED], miner), screen=screen)
    assert result["stored"] == 0 and result["rejected"] == {"screen_unchecked": 1}
    restated = demand_miner.mine(_body("2026-11-18"), miner, NOW,
                                 chat=_chat([LICENSED], miner),
                                 screen=lambda f, u: ScreenResult("restates_task"))
    assert restated["rejected"] == {"screen_restates_task": 1}


def test_the_daily_cap_stops_calls_before_they_are_made(miner, monkeypatch):
    monkeypatch.setenv("CROWDCODE_DEMAND_MINER_DAILY_USD", "0.01")
    chat = _chat([LICENSED], miner)
    result = demand_miner.mine(_body(), miner, NOW, chat=chat, screen=_accept)
    assert result["skipped"] == "budget" and chat.calls == []


def test_spend_is_recorded_per_day_and_counts_against_the_cap(miner, monkeypatch):
    monkeypatch.setenv("CROWDCODE_DEMAND_MINER_DAILY_USD", "0.24")
    big = {"prompt_tokens": 10000, "completion_tokens": 5000}  # $0.20 at gpt-5.5
    first = demand_miner.mine(_body(), miner, NOW, chat=_chat([LICENSED], miner, big), screen=_accept)
    assert first["usd"] == pytest.approx(0.2 + 0.012, abs=1e-6)
    second = demand_miner.mine(_body("2026-11-18"), miner, NOW, chat=_chat([LICENSED], miner, big),
                               screen=_accept)
    assert second["skipped"] == "budget"
    tomorrow = demand_miner.mine(_body("2026-11-18"), miner, NOW + timedelta(days=1),
                                 chat=_chat([LICENSED], miner), screen=_accept)
    assert tomorrow["stored"] == 1


def test_parse_batch_enforces_summary_limits():
    long = _run("d" * 32, "x " * 2000, final="y " * 2000,
                failed_tools=[{"tool": "t", "error": "e " * 500}] * 10)
    batch = demand_miner.parse_batch({"night": "2026-11-17", "runs": [long, _run("e" * 32, "   ")]})
    assert len(batch.runs) == 1  # an empty prompt teaches nothing
    run = batch.runs[0]
    assert len(run.prompt) <= demand_miner.PROMPT_CHARS and len(run.final) <= demand_miner.FINAL_CHARS
    assert len(run.failed_tools) == demand_miner.MAX_FAILED_TOOLS
    for bad in ({"night": "today", "runs": [long]},
                {"night": "2026-11-17", "runs": []},
                {"night": "2026-11-17", "runs": [{**long, "ref": "not-hex"}]},
                {"night": "2026-11-17", "runs": [long, long]},
                {"night": "2026-11-17", "runs": [long] * 61}):
        with pytest.raises(demand_miner.MinerError):
            demand_miner.parse_batch(bad)


@pytest.mark.parametrize("text", [
    "see https://a.example/x", "on www.example.org", "acme.com's API", "mail jo@ex.co",
    "post as @crowdcodeapp", "wallet 0xabcdef12", "run 1f0c3a4e-1111-4222-8333-123456789abc",
    "order 123456", "the file pachinko.pdf", "for [PERSON_1]",
])
def test_traceable_text(text):
    assert demand_miner.traceable(text)


@pytest.mark.parametrize("text", [
    "licensed full text of a news article with its byline",
    "official social platform posting APIs plus a hosted scheduler",
    "a price per call of 0.40 USD for 30 days of monitoring",
])
def test_ordinary_text_is_not_traceable(text):
    assert not demand_miner.traceable(text)


def test_the_endpoint_needs_the_configured_token(monkeypatch):
    app = Starlette(routes=[Route("/api/internal/demand-miner/batch", server_mod.demand_miner_batch,
                                  methods=["POST"])])
    client = TestClient(app)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("CROWDCODE_DEMAND_MINER_TOKEN", raising=False)
    assert client.post("/api/internal/demand-miner/batch", json={}).status_code == 404
    monkeypatch.setenv("CROWDCODE_DEMAND_MINER_TOKEN", "miner-token")
    assert client.post("/api/internal/demand-miner/batch", json={}).status_code == 401
    assert client.post("/api/internal/demand-miner/batch", json={},
                       headers={"authorization": "Bearer wrong"}).status_code == 401
    bad = client.post("/api/internal/demand-miner/batch", json={"night": "x"},
                      headers={"authorization": "Bearer miner-token"})
    assert bad.status_code == 400


# --- the proposals pipeline -------------------------------------------------

def _row(request_id, *, source="agent", runs=None, **text):
    return {
        "id": request_id, "created_at": NOW - timedelta(days=1),
        "requester_id": None if source == "miner" else f"r{request_id}",
        "service_description": None, "task_context": None,
        "request_input": text.get("input", f"input text for request number {request_id}"),
        "request_output": text.get("output", f"output text for request number {request_id}"),
        "request_problem": text.get("problem", f"problem text for request number {request_id}"),
        "price_per_call_usd": 0.1, "fields_source": "miner" if source == "miner" else "agent",
        "inference_confidence": "low" if source == "miner" else None,
        "request_kind": "leverage", "source": source, "miner_run_count": runs,
    }


def _proposal(title, ids, **fields):
    return {"title": title, "input": fields.get("input", "a publication and a headline"),
            "output": fields.get("output", "the licensed full text of the article"),
            "problem": "agents hit paywalls on articles owners ask about",
            "builds_on": "news licensing APIs", "unlocks": "grounded summaries",
            "request_ids": ids}


def test_proposals_say_who_found_the_demand_and_in_how_many_runs():
    rows = [_row(1, source="miner", runs=3), _row(2, source="miner", runs=2), _row(3), _row(4),
            _row(5, source="miner", runs=1)]
    parsed = {"proposals": [_proposal("Licensed articles", [1, 2]),
                            _proposal("DNS checks", [3]),
                            _proposal("Mixed", [4, 5])]}
    payload = proposals.build_payload(parsed, rows, SourceIndex([]), NOW, "gpt-5.5")
    by_title = {p["title"]: p for p in payload["proposals"]}
    assert by_title["Licensed articles"]["source"] == "miner"
    assert by_title["Licensed articles"]["demand"]["runs"] == 5
    assert by_title["DNS checks"]["source"] == "agents" and by_title["DNS checks"]["demand"]["runs"] == 0
    assert by_title["Mixed"]["source"] == "both"
    assert by_title["Mixed"]["demand"]["mined"] == 1 and by_title["Mixed"]["demand"]["requesters"] == 2
    assert by_title["Licensed articles"]["inferred_requests"] == 0
    assert by_title["Licensed articles"]["confidence"] == "low"  # nobody stated a price
    assert payload["listed_by_source"] == {"miner": 3, "agent": 2}
    items = proposals._model_items(rows)
    assert items[0]["source"] == "miner" and items[0]["runs"] == 3 and not items[0]["low_confidence"]
    assert "source" not in items[2]


def test_a_mined_proposal_that_names_something_traceable_is_not_published():
    rows = [_row(1, source="miner", runs=2)]
    parsed = {"proposals": [_proposal("Articles", [1], output="the article from nytimes.com")]}
    payload = proposals.build_payload(parsed, rows, SourceIndex([]), NOW, "gpt-5.5")
    assert payload["proposals"] == [] and payload["hidden_reasons"]["private"] == 1


def test_a_proposal_the_redactor_changes_is_not_published(monkeypatch):
    rows = [_row(1, source="miner", runs=4), _row(2), _row(3)]
    parsed = {"proposals": [_proposal("Full text", [1, 2]), _proposal("DNS checks", [3])]}
    payload = proposals.build_payload(parsed, rows, SourceIndex([]), NOW, "gpt-5.5")
    assert payload["listed_by_source"] == {"miner": 1, "agent": 2}

    def redactor(texts, **_):
        return [t.replace("a publication", "[SURNAME_3]") if t and "Full" not in t and i < len(proposals.PUBLIC_KEYS) else t
                for i, t in enumerate(texts)]

    monkeypatch.setattr(proposals, "redact_texts", redactor)
    out = proposals.redact_payload(payload)
    assert [p["title"] for p in out["proposals"]] == ["DNS checks"]
    assert "_tally" not in out["proposals"][0] and "_member_ids" not in out["proposals"][0]
    assert out["listed_request_count"] == 1 and out["hidden_reasons"]["private"] == 2
    assert {out["_triage"][i]["reason"] for i in (1, 2)} == {"private"} and out["_triage"][3]["listed"]
    assert out["listed_by_source"] == {"agent": 1}


def test_the_miner_is_told_what_is_already_published(miner):
    with miner() as conn:
        conn.execute(
            "insert into app_cache (key, payload, generated_at) values ('service_proposals', %s::jsonb, now())",
            ('{"proposals": [{"id": "p_1", "title": "Social Post Publisher", '
             '"problem": "agents cannot post to social accounts reliably", "input": "x"}]}',))
        conn.commit()
    chat = _chat([LICENSED], miner)
    demand_miner.mine(_body(), miner, NOW, chat=chat, screen=_accept)
    prompt = chat.calls[0]
    assert prompt["already_published"] == [
        {"title": "Social Post Publisher", "problem": "agents cannot post to social accounts reliably"}]
    assert any("already_published" in rule and "none of them covers" in rule for rule in prompt["requirements"])


def test_an_unknown_capability_is_left_for_the_tree_to_map(miner):
    result = demand_miner.mine(_body(), miner, NOW, chat=_chat([{**LICENSED, "capability": "made-up"}], miner),
                               screen=_accept)
    assert result["stored"] == 1
    with miner() as conn:
        assert conn.execute("select capability from service_requests").fetchone()["capability"] is None
