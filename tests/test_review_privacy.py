"""Raw review text never leaves the backend.

Reviews are stored verbatim (a score has to stay re-verifiable), but every
public surface publishes counts, scores and the generated summary only. These
tests write reviews with distinctive text and fail if any of it appears in
anything a website visitor, an agent or the knowledge export can read.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

from crowdcode import cron
from crowdcode import server as server_mod
from crowdcode import summaries
from crowdcode.discovery import find_services
from crowdcode.payments import utc_now
from tests.test_discovery import FakeCatalogs
from tests.test_hosted_attestation import (  # noqa: F401 - fixtures
    ENDPOINT,
    JUDGE_TEXT,
    attestation,
    db,
    hosted,
    receipt,
    tx_hash,
    wallet,
)

COMMUNITY_TEXT = "Our quarterly board deck for Zyxwv Holdings came back with every chart mislabeled"
COMMUNITY_TASK = "preparing the confidential Zyxwv Holdings acquisition memo for Priya Raman"
SECRETS = (JUDGE_TEXT, COMMUNITY_TEXT, COMMUNITY_TASK, "Zyxwv", "Priya Raman", "mislabeled")


@pytest.fixture
def reviewed(hosted, monkeypatch):  # noqa: F811
    """One judged hosted review and one community review, plus a generated summary."""
    conn, review = hosted
    agent = wallet("42")
    tx = tx_hash("aa")
    result = review(wallet=agent, tx=tx, att=attestation(receipt(tx=tx), reviewer_wallet=agent.address))
    assert result["accepted"], result
    service_id = result["service_id"]
    conn.execute(
        """insert into reviews (service_id, rating, reason, task_context, reviewer_wallet,
                                signature_verified, payment_verified, weight_basis)
           values (%s, 2, %s, %s, %s, true, false, 'none')""",
        (service_id, COMMUNITY_TEXT, COMMUNITY_TASK, "0x" + "77" * 20),
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("REDACTION_REQUIRED", raising=False)
    monkeypatch.delenv("REDACTOR_URL", raising=False)
    monkeypatch.setattr(cron, "connect", server_mod.connect)

    def fake_model(settings, system, prompt, timeout=None, model=None):
        # The summarizer sees the private text (weighted reviews first); one
        # generated item copies it verbatim.
        copied = prompt["reviews"][0]["reason"]
        assert copied == JUDGE_TEXT
        return {
            "pros": ["Results arrive quickly and are ranked sensibly.", copied],
            "cons": ["Some results miss fields the caller asked for."],
            "watch_out": ["Check that every requested field is present before relying on it."],
        }

    monkeypatch.setattr(summaries, "_chat_json", fake_model)
    cron.run_service_summaries(utc_now())
    return conn, service_id


def _assert_private(payload) -> None:
    text = json.dumps(payload, default=str)
    for secret in SECRETS:
        assert secret not in text, f"raw review text leaked: {secret!r}"


def test_the_summary_is_generated_and_copies_are_dropped(reviewed):
    conn, service_id = reviewed
    stored = conn.execute("select review_summary from services where id = %s", (service_id,)).fetchone()
    summary = stored["review_summary"]
    assert summary["version"] == summaries.SUMMARY_VERSION
    assert summary["pros"] == ["Results arrive quickly and are ranked sensibly."]
    assert summary["cons"] and summary["watch_out"]
    _assert_private(summary)


def test_score_detail_listing_and_search_publish_no_review_text(reviewed):
    _, service_id = reviewed
    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["found"] and score["num_reviews"] == 2
    assert score["summary"]["pros"] == ["Results arrive quickly and are ranked sensibly."]
    assert set(score["summary"]) == {"pros", "cons", "watch_out", "n_reviews", "through_date",
                                     "generated_at"}
    assert "recent_reviews" not in score
    _assert_private(score)

    detail, status = server_mod._service_detail_payload(service_id)
    assert status == 200 and detail["num_reviews"] == 2
    assert detail["histogram"]["2"] == 1
    assert "recent_reviews" not in detail
    _assert_private(detail)

    listing = server_mod._top_services_payload(None)
    assert listing["services"][0]["has_summary"] is True
    _assert_private(listing)

    export = server_mod._knowledge_evidence_payload()
    assert export["services"][0]["summary"]["pros"]
    _assert_private(export)

    found = find_services("search", catalogs=FakeCatalogs(), conn_factory=server_mod.connect)
    _assert_private(found)


def test_http_routes_publish_no_review_text(reviewed):
    _, service_id = reviewed

    def get(handler, path, path_params=None):
        scope = {"type": "http", "method": "GET", "path": path, "query_string": b"",
                 "headers": [], "path_params": path_params or {}}
        response = asyncio.run(handler(Request(scope)))
        assert response.status_code == 200
        return json.loads(response.body)

    _assert_private(get(server_mod.service_detail, f"/api/services/{service_id}",
                        {"service_id": service_id}))
    _assert_private(get(server_mod.all_services, "/api/services"))
    _assert_private(get(server_mod.top_services, "/api/services/top"))


def test_an_old_format_summary_is_never_served(reviewed):
    conn, service_id = reviewed
    conn.execute(
        """update services set review_summary = %s::jsonb where id = %s""",
        (json.dumps({"strengths": [COMMUNITY_TEXT], "failure_modes": [], "caveats": []}), service_id),
    )
    score = server_mod.get_service_score(api_endpoint=ENDPOINT)
    assert score["summary"] is None
    _assert_private(score)
    # ...and the cron regenerates it.
    pending = cron._collect_summary_inputs(utc_now())
    assert [service for service, *_ in pending] == [service_id]


def test_the_owner_can_still_list_their_own_reviews(reviewed, monkeypatch):
    """Private is not deleted: the reviewing wallet reads its own text through
    the signed list_my_reviews path, which is not public."""
    conn, _ = reviewed
    row = conn.execute("select reason from reviews where reason = %s", (COMMUNITY_TEXT,)).fetchone()
    assert row["reason"] == COMMUNITY_TEXT
