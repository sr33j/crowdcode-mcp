"""request_service v2: four concrete fields in, private storage, and only
generated, aggregated proposals out."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from crowdcode import cron, proposals
from crowdcode import server as server_mod
from crowdcode import summaries
from crowdcode.demand import SourceIndex, copies_source, price_distribution, validate_request_fields
from tests.test_hosted_attestation import db, hosted  # noqa: F401 - fixtures

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
WALLET = "0x" + "5a" * 20
GOOD = {
    "input": "a public MP4 URL (up to 60 s) and the post text",
    "output": "the published post ID and URL, posted exactly once",
    "problem": "agents publishing video to X fail on chunked uploads and cannot tell whether it posted",
    "price_per_call": 0.05,
}
EXTRAS = {
    "builds_on": "the X API v2 media upload endpoint with resumable chunk retries",
    "frequency": "per_task_like_this",
}


# --- validation ------------------------------------------------------------

def test_valid_fields_are_normalized():
    fields, error = validate_request_fields(
        "  a public MP4 URL\n(up to 60 s)   and the post text ", GOOD["output"], GOOD["problem"], "0.05")
    assert error is None
    assert fields.input == "a public MP4 URL (up to 60 s) and the post text"
    assert fields.price_per_call == 0.05


@pytest.mark.parametrize("field,value,expected", [
    ("input", None, "input is required"),
    ("output", "", "output is required"),
    ("problem", "it breaks", "problem is too vague"),
    ("input", "anything", "input is too vague"),
    ("input", "word " * 150, "at most 600"),
    ("output", GOOD["input"], "input and output must differ"),
    ("price_per_call", None, "price_per_call is required"),
    ("price_per_call", 0, "more than 0"),
    ("price_per_call", -1, "more than 0"),
    ("price_per_call", float("nan"), "more than 0"),
    ("price_per_call", True, "price_per_call is required"),
    ("price_per_call", "cheap", "must be a number"),
    ("price_per_call", 250, "at most 100"),
])
def test_invalid_fields_say_what_to_fix(field, value, expected):
    args = {**GOOD, field: value}
    fields, error = validate_request_fields(**args)
    assert fields is None
    assert expected in error


def test_a_vague_field_is_taught_by_example():
    _, error = validate_request_fields(**{**GOOD, "output": "results"})
    assert "for example" in error


# --- privacy guard and price statistics --------------------------------------

def test_copy_guard_flags_eight_shared_words_only():
    source = "The paid DNS checker returned success false on a 200 and charged anyway for it"
    assert copies_source("so the paid DNS checker returned success false on a 200 again", [source])
    assert not copies_source("A DNS checker that reports resolver errors honestly", [source])
    assert not SourceIndex([None, ""]).copies("anything at all here and more words than eight")


def test_price_distribution():
    dist = price_distribution([0.01, 0.05, 0.05, 0.2, 2.0])
    assert dist["median"] == 0.05 and dist["n"] == 5
    assert dist["min"] == 0.01 and dist["max"] == 2.0
    assert [b["n"] for b in dist["buckets"]] == [0, 1, 3, 0, 1]
    assert price_distribution([]) is None
    assert price_distribution([0.3])["p25"] == 0.3


# --- the tool ----------------------------------------------------------------

def test_request_service_stores_the_four_fields(hosted):  # noqa: F811
    conn, _ = hosted
    result = server_mod.request_service(**GOOD, **EXTRAS, requester_wallet=WALLET)
    assert result["accepted"], result
    assert result["recorded"]["price_per_call"] == 0.05
    row = conn.execute("select * from service_requests").fetchone()
    assert row["request_input"] == GOOD["input"]
    assert row["request_output"] == GOOD["output"]
    assert row["request_problem"] == GOOD["problem"]
    assert float(row["price_per_call_usd"]) == 0.05
    assert row["fields_source"] == "agent"
    assert row["service_description"] is None


def test_request_service_rejects_vague_requests_before_storing(hosted):  # noqa: F811
    conn, _ = hosted
    result = server_mod.request_service(**{**GOOD, "output": "results"}, **EXTRAS, requester_wallet=WALLET)
    assert result["accepted"] is False
    assert result["error_code"] == "invalid_request_fields"
    assert "output is too vague" in result["reason"]
    assert conn.execute("select count(*) as n from service_requests").fetchone()["n"] == 0


def test_request_service_needs_a_wallet(hosted):  # noqa: F811
    result = server_mod.request_service(**GOOD, **EXTRAS)
    assert result["accepted"] is False and result["next_step"]["action"] == "install_wallet"


# --- proposals -----------------------------------------------------------------

def _row(request_id, price, *, source="agent", confidence=None, requester="r1", days=1, **text):
    return {
        "id": request_id, "created_at": NOW - timedelta(days=days), "requester_id": requester,
        "service_description": text.get("description"), "task_context": None,
        "request_input": text.get("input", f"input text for request number {request_id}"),
        "request_output": text.get("output", f"output text for request number {request_id}"),
        "request_problem": text.get("problem", f"problem text for request number {request_id}"),
        "price_per_call_usd": price, "fields_source": source, "inference_confidence": confidence,
    }


def _proposal(title, ids, **fields):
    return {"title": title, "input": fields.get("input", "a hostname and a DNS record type"),
            "output": fields.get("output", "answers, errors and latency from each resolver"),
            "problem": fields.get("problem", "agents cannot tell whether DNS changes propagated"),
            "request_ids": ids}


def test_build_payload_computes_demand_and_price_from_rows():
    rows = [_row(1, 0.01, requester="a"), _row(2, 0.05, requester="b"), _row(3, 0.2, requester="b"),
            _row(4, 0.5), _row(5, 0.02), _row(6, 1.0), _row(7, None, source="inferred", confidence="low")]
    parsed = {
        "proposals": [
            _proposal("DNS propagation check", [1, 2, 3, 3, 99]),  # dup and unknown ids ignored
            _proposal("No output", [4], output=""),
            _proposal("Guess-priced idea", [7]),
        ],
        "not_listed": [{"request_id": 6, "reason": "internal", "note": "Fixes its own wallet."},
                       {"request_id": 5, "reason": "unrealistic", "note": "Nobody would pay."},
                       {"request_id": 1, "reason": "vague"}],
    }
    payload = proposals.build_payload(parsed, rows, SourceIndex([]), NOW, "m")
    assert [p["title"] for p in payload["proposals"]] == ["DNS propagation check", "Guess-priced idea"]
    dns, guess = payload["proposals"]
    assert dns["demand"]["requests"] == 3 and dns["demand"]["requesters"] == 2
    assert dns["price"]["median"] == 0.05
    assert dns["confidence"] == "high"
    assert guess["confidence"] == "low" and guess["price"] is None
    # A reason the model is no longer asked for (realism) counts as vague, never "unrealistic".
    assert payload["hidden_reasons"] == {"vague": 2, "internal": 1, "not_a_service": 0,
                                         "private": 0, "unplaced": 0}
    assert payload["listed_request_count"] == 4 and payload["hidden_request_count"] == 3
    triage = payload["_triage"]
    assert triage[1] == {"listed": True, "reason": None, "note": None,
                         "proposal_id": dns["id"], "proposal_title": "DNS propagation check"}
    assert triage[6]["reason"] == "internal" and triage[6]["note"] == "Fixes its own wallet."
    assert triage[4]["reason"] == "vague" and triage[4]["proposal_title"] == "No output"
    assert set(triage) == {1, 2, 3, 4, 5, 6, 7}


def test_no_judgement_of_whether_anyone_would_pay():
    """SRI-122: one requester, a big price, an ambitious service: listed. The
    prompt asks only for tractability and the payload carries no pay score."""
    rows = [_row(202, 2.5, requester="solo")]
    gpu = _proposal("GPU Training Jobs", [202], input="A container image, a dataset URL and a GPU type",
                    output="A job ID with live logs, per-second billing and the trained weights",
                    problem="Agents have no GPUs to fine-tune or train models")
    payload = proposals.build_payload({"proposals": [gpu]}, rows, SourceIndex([]), NOW, "m")
    [listed] = payload["proposals"]
    assert listed["title"] == "GPU Training Jobs" and payload["hidden_request_count"] == 0
    for key in ("would_pay", "realism", "clarity", "rationale"):
        assert key not in listed
    assert "unrealistic" not in payload["hidden_reasons"]
    captured = {}
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", "postgresql://unused")
        mp.setattr(proposals, "_chat_json", lambda settings, system, prompt, **_: captured.update(
            system=system, prompt=prompt) or {})
        proposals.propose_services([])
    text = json.dumps(captured).lower()
    assert "tractab" in text
    for banned in ("would_pay", "realism", "unrealistic", "skeptical"):
        assert banned not in text


def test_build_payload_drops_a_proposal_that_repeats_a_request():
    private = "the paid DNS checker returned success false on a 200 and charged anyway"
    rows = [_row(1, 0.01, problem=private)]
    parsed = {"proposals": [_proposal("DNS", [1], problem="Because " + private)]}
    payload = proposals.build_payload(parsed, rows, SourceIndex([private]), NOW, "m")
    assert payload["proposals"] == []
    assert payload["hidden_request_count"] == 1
    assert payload["hidden_reasons"]["private"] == 1
    assert payload["_triage"][1]["reason"] == "private"


def test_unassigned_requests_are_hidden_as_unplaced():
    payload = proposals.build_payload({"proposals": []}, [_row(1, 0.1)], SourceIndex([]), NOW, "m")
    assert payload["hidden_reasons"]["unplaced"] == 1
    assert payload["_triage"][1]["reason"] == "unplaced"


LEGACY_TEXT = ("Upload of the Q3 board deck for Zyxwv Holdings to X failed at finalize; "
               "worth about $0.02 per successful post for Priya Raman")


def test_cron_maps_legacy_rows_and_publishes_only_proposals(hosted, monkeypatch):  # noqa: F811
    conn, _ = hosted
    conn.execute("insert into service_requests (service_description, task_context, requester_id) "
                 "values (%s, %s, 'legacy-1')", (LEGACY_TEXT, "daily posting run for Priya Raman"))
    server_mod.request_service(**GOOD, **EXTRAS, requester_wallet=WALLET)
    conn.execute("insert into app_cache (key, payload) values ('project_ideas', %s::jsonb)",
                 (json.dumps({"ideas": [{"example_requests": [LEGACY_TEXT]}]}),))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cron, "connect", server_mod.connect)
    monkeypatch.setattr(proposals, "redact_texts", lambda texts, **_: texts)
    calls = []

    def fake_model(settings, system, prompt, timeout=None, model=None):
        calls.append(model)
        if "requests" in prompt and "text" in prompt["requests"][0]:
            legacy_id = prompt["requests"][0]["id"]
            return {"requests": [{"id": legacy_id, "input": "an MP4 file and the post text",
                                  "output": "the post ID and URL", "problem": "chunked uploads fail",
                                  "price_per_call": 0.02, "price_stated": True,
                                  "confidence": "low"}]}
        ids = [item["id"] for item in prompt["requests"]]
        assert all("Zyxwv" not in json.dumps(item) for item in prompt["requests"])
        return {"proposals": [{
            "title": "Video posting to X with a verified post ID",
            "input": "A video file and the post text for a connected X account",
            "output": "The post ID and URL, published exactly once",
            "problem": "Chunked video uploads to X fail midway and leave agents unsure whether it posted",
            "request_ids": ids}]}

    monkeypatch.setattr(proposals, "_chat_json", fake_model)
    cron.run_service_proposals(NOW)

    legacy = conn.execute("select * from service_requests where requester_id = 'legacy-1'").fetchone()
    assert legacy["fields_source"] == "inferred" and legacy["inference_confidence"] == "low"
    assert float(legacy["price_per_call_usd"]) == 0.02
    assert set(calls) == {summaries.get_settings().proposal_model}

    payload = server_mod._service_proposals_payload()
    assert payload["version"] == 2 and payload["source_request_count"] == 2
    [proposal] = payload["proposals"]
    assert proposal["demand"] == {"requests": 2, "requesters": 2, "runs": 0, "mined": 0,
                                  "last_requested_at": proposal["demand"]["last_requested_at"]}
    assert proposal["source"] == "agents"
    assert proposal["price"]["median"] == pytest.approx(0.035)
    assert proposal["confidence"] == "high"  # one of two requests stated its price as an agent
    ideas = server_mod._project_ideas_payload()
    assert ideas["ideas"][0]["title"] == proposal["title"]
    # No raw request text anywhere public, and the old cache with examples is gone.
    public = json.dumps([payload, ideas])
    for secret in ("Zyxwv", "Priya Raman", "board deck", GOOD["problem"]):
        assert secret not in public
    assert conn.execute("select count(*) as n from app_cache where key = 'project_ideas'").fetchone()["n"] == 0
    # Each request's outcome is stored privately, never in the public payload (SRI-122).
    assert "_triage" not in payload
    triage = conn.execute("select listed, reason, proposal_title from service_request_triage").fetchall()
    assert [dict(t) for t in triage] == [{"listed": True, "reason": None, "proposal_title": proposal["title"]}] * 2


def test_no_requests_publishes_an_empty_payload(hosted, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cron, "connect", server_mod.connect)
    cron.run_service_proposals(NOW)
    payload = server_mod._service_proposals_payload()
    assert payload["proposals"] == [] and payload["source_request_count"] == 0


# --- requests v2 fields (C6) ---------------------------------------------------

from crowdcode import request_screen  # noqa: E402
from crowdcode.demand import is_filler, validate_request_extras  # noqa: E402


@pytest.mark.parametrize("field,value,expected", [
    ("frequency", "sometimes", "frequency is required"),
    ("builds_on", None, "builds_on is required"),
    ("builds_on", "N/A", "builds_on cannot be N/A"),
    ("builds_on", "nothing exists that could do this today", "must name something that exists"),
    ("builds_on", "more", "builds_on is too vague"),
])
def test_invalid_extras_say_what_to_fix(field, value, expected):
    extras, error = validate_request_extras(**{**EXTRAS, field: value})
    assert extras is None and expected in error


def test_extras_normalize_and_keep_a_capability_id():
    extras, error = validate_request_extras(**{**EXTRAS, "frequency": "per task like this",
                                               "capability": "Act/Booking.travel"})
    assert error is None
    assert extras.frequency == "per_task_like_this"
    assert extras.capability == "act/booking.travel"
    assert validate_request_extras(**{**EXTRAS, "capability": "not a node id!"})[0].capability is None


def test_na_filler_is_rejected_in_the_four_fields_too():
    assert is_filler("N/A (no service needed)") and is_filler(" none. ")
    assert not is_filler("a CSV of every table")
    _, error = validate_request_fields(**{**GOOD, "problem": "N/A - nothing needed"})
    assert "cannot be N/A" in error


def test_request_service_stores_the_new_fields(hosted, monkeypatch):  # noqa: F811
    conn, _ = hosted
    seen = []
    monkeypatch.setattr(server_mod, "screen_request",
                        lambda fields: seen.append(fields) or request_screen.ScreenResult("accept"))
    result = server_mod.request_service(**GOOD, **{**EXTRAS, "capability": "social.post"},
                                        requester_wallet=WALLET)
    assert result["accepted"], result
    assert "kind" not in result["recorded"] and result["screen"] == "accept"
    row = conn.execute("select * from service_requests").fetchone()
    assert row["request_kind"] is None and row["unlocks"] is None
    assert row["builds_on"] == EXTRAS["builds_on"] and row["frequency"] == "per_task_like_this"
    assert row["capability"] == "social.post" and row["screen_verdict"] == "accept"
    # The screen sees the request, never the wallet.
    assert WALLET not in json.dumps(seen)


def test_a_task_restatement_is_rejected_and_not_stored(hosted, monkeypatch):  # noqa: F811
    conn, _ = hosted
    monkeypatch.setattr(server_mod, "screen_request",
                        lambda fields: request_screen.ScreenResult("restates_task", "an explanation"))
    result = server_mod.request_service(**GOOD, **EXTRAS, requester_wallet=WALLET)
    assert result["accepted"] is False and result["error_code"] == "request_restates_task"
    assert "reusable service" in result["reason"]
    assert conn.execute("select count(*) as n from service_requests").fetchone()["n"] == 0


def test_requests_without_builds_on_are_rejected_before_the_model(hosted, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server_mod, "screen_request", lambda fields: pytest.fail("model called"))
    result = server_mod.request_service(**GOOD, **{**EXTRAS, "builds_on": None}, requester_wallet=WALLET)
    assert result["accepted"] is False and "builds_on is required" in result["reason"]


def test_the_screen_fails_open(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def down(*args, **kwargs):
        raise TimeoutError("slow")

    monkeypatch.setattr(request_screen, "_chat_json", down)
    assert request_screen.screen_request({}).verdict == "unchecked"
    monkeypatch.setattr(request_screen, "_chat_json", lambda *a, **k: {"verdict": "restates_task"})
    assert request_screen.screen_request({}).rejected
    monkeypatch.setattr(request_screen, "_chat_json", lambda *a, **k: {"verdict": "maybe?"})
    assert request_screen.screen_request({}).verdict == "unchecked"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert request_screen.screen_request({}).verdict == "unchecked"


def test_proposals_take_frequency_and_capability_from_the_requests():
    rows = [_row(1, 2.0, requester="a"), _row(2, 1.5, requester="b"), _row(3, 0.05, requester="c")]
    # A row stored before kind/unlocks were dropped keeps them; nothing reads them.
    rows[0].update(request_kind="leverage", frequency="recurring", capability="docs.ocr",
                   builds_on="a layout-aware OCR API", unlocks="old field")
    rows[1].update(frequency="per_task_like_this", capability="docs.ocr")
    rows[2].update(frequency="one_off")
    parsed = {"proposals": [
        {**_proposal("Table OCR", [1, 2]), "builds_on": "a layout-aware OCR API with table reconstruction"},
        _proposal("DNS propagation check", [3]),
    ]}
    payload = proposals.build_payload(parsed, rows, SourceIndex([]), NOW, "m")
    ocr = next(p for p in payload["proposals"] if p["title"] == "Table OCR")
    assert "kind" not in ocr and "unlocks" not in ocr and ocr["capability"] == "docs.ocr"
    assert ocr["frequency"] == {"recurring": 1, "per_task_like_this": 1}
    assert ocr["builds_on"] == "a layout-aware OCR API with table reconstruction"
    assert "listed_by_kind" not in payload
    items = proposals._model_items(rows)
    assert items[0]["builds_on"] and "kind" not in items[0] and "unlocks" not in items[0]
    assert "builds_on" not in items[2]


def test_an_old_client_sending_kind_and_unlocks_is_not_rejected(hosted, monkeypatch):  # noqa: F811
    import asyncio
    conn, _ = hosted
    monkeypatch.setattr(server_mod, "screen_request", lambda fields: request_screen.ScreenResult("accept"))
    asyncio.run(server_mod.mcp.call_tool("request_service", {
        **GOOD, **EXTRAS, "kind": "leverage", "unlocks": "book it directly", "requester_wallet": WALLET}))
    row = conn.execute("select * from service_requests").fetchone()
    assert row is not None and row["builds_on"] == EXTRAS["builds_on"]
    assert row["request_kind"] is None and row["unlocks"] is None


def test_rollout_mode_accepts_a_pre_c6_client_but_still_screens_it(hosted, monkeypatch):  # noqa: F811
    conn, _ = hosted
    screened = []
    monkeypatch.setattr(server_mod, "screen_request",
                        lambda fields: screened.append(fields) or request_screen.ScreenResult("accept"))
    assert server_mod.request_service(**GOOD, requester_wallet=WALLET)["accepted"] is False
    monkeypatch.setenv("CROWDCODE_REQUESTS_V2_STRICT", "0")
    result = server_mod.request_service(**GOOD, requester_wallet=WALLET)
    assert result["accepted"], result
    assert "builds_on" not in screened[-1]
    row = conn.execute("select * from service_requests").fetchone()
    assert row["builds_on"] is None
    # A client that sends some of the new fields is validated in full.
    partial = server_mod.request_service(**GOOD, builds_on=EXTRAS["builds_on"], requester_wallet=WALLET)
    assert partial["accepted"] is False and "frequency is required" in partial["reason"]


def _gpu_payload():
    rows = [_row(202, 2.5, requester="solo"), _row(203, 0.1, requester="b")]
    parsed = {"proposals": [
        _proposal("GPU Training Runner", [202], input="A container image, a dataset and a GPU type",
                  output="A run handle that resolves to trained weights"),
        _proposal("DNS propagation check", [203]),
    ]}
    return proposals.build_payload(parsed, rows, SourceIndex([]), NOW, "m")


def _surname_redactor(texts, **_):
    """The production redactor read "Runner" in a product name as a surname (2026-11-18)."""
    return [t.replace("Runner", "[SURNAME_3]") if t else t for t in texts]


def test_a_proposal_the_redactor_misreads_is_reworded_and_listed(monkeypatch):
    monkeypatch.setattr(proposals, "redact_texts", _surname_redactor)
    seen = []

    def reword(flagged):
        seen.extend(p["title"] for p in flagged)
        return {p["id"]: {"title": "GPU Training Jobs", "input": p["input"], "output": p["output"],
                          "problem": p["problem"], "builds_on": None} for p in flagged}

    out = proposals.redact_payload(_gpu_payload(), SourceIndex([]), reword)
    # Only the flagged proposal is reworded, from its redacted text.
    assert seen == ["GPU Training [SURNAME_3]"]
    titles = [p["title"] for p in out["proposals"]]
    assert titles == ["GPU Training Jobs", "DNS propagation check"]
    gpu = out["proposals"][0]
    assert out["_triage"][202] == {"listed": True, "reason": None, "note": None,
                                   "proposal_id": gpu["id"], "proposal_title": "GPU Training Jobs"}
    assert out["listed_request_count"] == 2 and out["hidden_reasons"]["private"] == 0


def test_a_rewording_still_flagged_stays_hidden(monkeypatch):
    monkeypatch.setattr(proposals, "redact_texts", _surname_redactor)
    reword = lambda flagged: {p["id"]: {**{k: p[k] for k in proposals.PUBLIC_KEYS}, "title": "Runner"}  # noqa: E731
                              for p in flagged}
    out = proposals.redact_payload(_gpu_payload(), SourceIndex([]), reword)
    assert [p["title"] for p in out["proposals"]] == ["DNS propagation check"]
    assert out["_triage"][202]["reason"] == "private"
    assert out["_triage"][202]["proposal_title"] == "GPU Training [SURNAME_3]"
    # A failed or missing rewrite hides it too.
    out = proposals.redact_payload(_gpu_payload(), SourceIndex([]), lambda flagged: {})
    assert out["hidden_reasons"]["private"] == 1
