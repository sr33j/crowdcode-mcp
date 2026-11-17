"""The capability tree's policy as data, and the evolution proposer and judge (C14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from crowdcode import capability_tree as ct
from crowdcode import tree_policy as tp
from tests.test_capability_tree import NOW, Settings, ScriptedModel, big_tree, db, judged, seed, video_fixture, view  # noqa: F401


def evolved(**overrides) -> dict:
    policy = tp.default_policy()
    policy["policy"] = "Free first, and stop once a free path answers. Buy when it saves real work."
    policy["rules"] = [*tp.STANDING_RULES, "Say what you bought and what it cost."]
    policy["nodes"]["video"]["when_to_look"] = "The owner asks for a new clip: buy from a video model."
    return {**policy, **overrides}


def test_the_default_policy_is_valid_and_the_l0_carries_the_standing_rule_within_budget():
    tree = tp.inherit(big_tree(), None)
    assert tp.validate_policy(tp.default_policy(), base_tree=tree) == []
    l0 = ct.render_l0(tree)
    assert f"Hard rule: {tp.STANDING_RULES[0]}" in l0 and ct.token_bound(l0) <= ct.L0_TOKENS
    assert all(f"- {n['id']}:" in l0 for n in tree["nodes"] if n["kind"] != "policy")
    assert tree["policy_id"] == "default"
    # The media nodes carry the rule on their own entries too.
    assert tp.STANDING_RULES[0] in ct.render_entry(tree, "video")


def test_apply_policy_rewrites_the_text_and_never_the_picks():
    base = video_fixture()
    out = tp.apply_policy(base, evolved(id="g1-c0"))
    video = next(n for n in out["nodes"] if n["id"] == "video")
    assert video["when_to_look"].startswith("The owner asks for a new clip")
    assert all(leaf.get("when_not_to_buy", video["when_not_to_buy"]) == video["when_not_to_buy"] for leaf in video["leaves"])
    strip = lambda leaves: [{k: v for k, v in leaf.items() if k != "when_not_to_buy"} for leaf in leaves]  # noqa: E731
    assert strip(video["leaves"]) == strip(next(n for n in base["nodes"] if n["id"] == "video")["leaves"])
    assert out["policy_id"] == "g1-c0" and out["hard_rules"][0] == tp.STANDING_RULES[0]
    assert tp.policy_of(out)["nodes"]["video"]["when_to_look"] == video["when_to_look"]


def test_validation_names_every_problem():
    tree = tp.inherit(big_tree(), None)
    bad = evolved()
    bad["rules"] = ["Use whatever is cheapest."]
    del bad["nodes"]["phone"]
    bad["nodes"]["image"]["when_not_to_buy"] = "Buy from https://img.example first."
    bad["nodes"]["travel"]["when_to_look"] = "x" * 300
    errors = tp.validate_policy(bad)
    assert "the standing media rule must be kept verbatim" in errors
    assert "nodes missing: phone" in errors
    assert any("image.when_not_to_buy names a URL" in e for e in errors)
    assert any("travel.when_to_look is over 200" in e for e in errors)
    long = evolved(rules=[*tp.STANDING_RULES, *["A long rule that keeps going " * 8][:1] * 7])
    assert any("L0 would" in e for e in tp.validate_policy(long, base_tree=tree))
    assert tp.validate_policy("nope") == ["the policy is not an object"]


def test_a_candidate_is_stored_on_its_base_published_through_the_gate_and_inherited_by_the_next_build(db, monkeypatch):
    seed(db)
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
    reliability = view(judged("svc_vid", 6, 6))
    ct.run(db, NOW, chat=ScriptedModel(), reliability=reliability, settings=Settings())
    with db() as conn:
        assert ct.publish_gated(conn, 1, by="test", evidence={}, now=NOW) == "published"
        first = ct.get_version(conn, 1)
        assert first["tree"]["policy_id"] == "default" and first["tree"]["hard_rules"] == list(tp.STANDING_RULES)
        version = tp.store_candidate(conn, 1, evolved(), label="g1-c0", now=NOW, meta={"parent": 1})
        row = ct.get_version(conn, version)
        assert row["trigger"] == "evolve:g1-c0:v1" and not row["published"]
        assert row["fingerprint"] == first["fingerprint"] and row["stats"]["evolve"]["parent"] == 1
        assert ct.summary_payload(row)["policy_id"] == "g1-c0"
        with pytest.raises(ValueError, match="label"):
            tp.store_candidate(conn, 1, evolved(), label="Bad Label", now=NOW)
        with pytest.raises(ValueError, match="nodes missing"):
            tp.store_candidate(conn, 1, {**evolved(), "nodes": {}}, label="x", now=NOW)
        # Unlike a fixture, the gate may publish it.
        assert ct.publish_gated(conn, version, by="tree-gate", evidence={"decision": "publish"}, now=NOW) == "published"
        conn.commit()
    # The next nightly build keeps the evolved policy and the fresh picks.
    built = ct.run(db, NOW + timedelta(hours=26), chat=ScriptedModel(), reliability=reliability, settings=Settings())
    with db() as conn:
        latest = ct.get_version(conn, built["built"]["version"])["tree"]
        assert latest["policy_id"] == "g1-c0" and latest["policy"] == evolved()["policy"]
        assert next(n for n in latest["nodes"] if n["id"] == "video")["when_to_look"].startswith("The owner asks")


class Proposer:
    """A scripted proposer: the first answer drops the standing rule, the second is valid."""

    def __init__(self, answers: list[dict]):
        self.answers, self.payloads = list(answers), []

    def __call__(self, system, payload, usage):
        self.payloads.append(payload)
        usage.update({"prompt_tokens": 4000, "completion_tokens": 1500})
        return self.answers.pop(0)


def test_the_proposer_retries_once_with_the_validators_errors_and_keeps_the_standing_rule():
    tree = tp.inherit(big_tree(), None)
    good = evolved()
    bad = {**good, "nodes": {k: v for k, v in good["nodes"].items() if k != "email"}}
    chat = Proposer([{**bad, "rationale": "x"}, {**good, "rules": ["Say what you bought and what it cost."], "rationale": "stop early"}])
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=1.0)
    cases = [{"task": "video_clip", "node": "video", "outcome": "fail", "cost_usd": 0.01, "turns": 3,
              "services": [], "detail": "answered with an SVG animation", "category": "substitute"}]
    policy, rationale = tp.propose_policy(chat, spend, parent=tp.default_policy(), tree=tree, cases=cases, directive="fix media")
    assert rationale == "stop early" and policy["rules"][0] == tp.STANDING_RULES[0]
    assert chat.payloads[1]["previous_attempt_rejected"] == ["nodes missing: email"]
    assert chat.payloads[0]["held_in_results"][0]["detail"] == "answered with an SVG animation"
    assert spend.calls == 2 and spend.usd > 0
    with pytest.raises(ValueError, match="nodes missing"):
        tp.propose_policy(Proposer([bad, bad]), ct.Spend(model="gpt-5-mini", remaining_usd=1.0), parent=tp.default_policy(),
                          tree=tree, cases=[], directive=None)
    with pytest.raises(ct.BudgetExhausted):
        tp.propose_policy(Proposer([good]), ct.Spend(model="gpt-5-mini", remaining_usd=0.0), parent=tp.default_policy(),
                          tree=tree, cases=[], directive=None)


def test_the_judge_bounds_its_score_and_needs_one():
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=1.0)
    verdict = tp.judge_answer(lambda s, p, u: {"score": 1.4, "pass": True, "reason": "all three points"}, spend,
                              task="Summarize", rubric="three points", answer="…")
    assert verdict == {"score": 1.0, "pass": True, "reason": "all three points"}
    assert tp.judge_answer(lambda s, p, u: {"score": 0.3, "pass": True}, spend, task="t", rubric="r", answer="a")["pass"] is False
    with pytest.raises(ValueError, match="no score"):
        tp.judge_answer(lambda s, p, u: {"pass": True}, spend, task="t", rubric="r", answer="a")


def test_the_evolution_endpoints_need_the_gate_token_and_store_what_they_propose(db, monkeypatch):
    from crowdcode import server

    seed(db)
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
    ct.run(db, NOW, chat=ScriptedModel(), reliability=ct.ReliabilityView(available=True), settings=Settings())
    monkeypatch.setattr(server, "connect", db)
    good = evolved()
    monkeypatch.setattr(server, "_evolve_chat", lambda model: ("gpt-5-mini", Proposer([{**good, "rationale": "tighter"}])))
    client = TestClient(Starlette(routes=[
        Route("/api/internal/tree/candidate", server.tree_candidate, methods=["POST"]),
        Route("/api/internal/tree/propose", server.tree_propose, methods=["POST"]),
        Route("/api/internal/eval/judge", server.eval_judge, methods=["POST"]),
    ]))
    monkeypatch.delenv("CROWDCODE_TREE_GATE_TOKEN", raising=False)
    assert client.post("/api/internal/tree/propose", json={}).status_code == 404
    monkeypatch.setenv("CROWDCODE_TREE_GATE_TOKEN", "gate-token-0123456789abcdef0123456789")
    auth = {"authorization": "Bearer gate-token-0123456789abcdef0123456789"}
    assert client.post("/api/internal/tree/propose", json={"base_version": 1, "label": "g0-c0", "cases": []}).status_code == 401
    assert client.post("/api/internal/tree/propose", json={"label": "g0-c0"}, headers=auth).status_code == 400

    proposed = client.post("/api/internal/tree/propose", headers=auth,
                           json={"base_version": 1, "label": "g0-c0", "cases": [{"task": "t", "outcome": "fail"}], "directive": "cheaper"})
    assert proposed.status_code == 200, proposed.text
    body = proposed.json()
    assert body["version"] == 2 and body["rationale"] == "tighter" and body["spend"]["calls"] == 1
    stored = client.post("/api/internal/tree/candidate", headers=auth, json={"base_version": 1, "label": "dry-c1", "policy": good})
    assert stored.json() == {"ok": True, "version": 3}
    rejected = client.post("/api/internal/tree/candidate", headers=auth, json={"base_version": 1, "label": "dry-c2", "policy": {"policy": "x"}})
    assert rejected.status_code == 422 and "rules must be" in rejected.json()["error"]
    with db() as conn:
        assert ct.get_version(conn, 2)["trigger"] == "evolve:g0-c0:v1"
        # The proposer's call was costed against the evolution budget, not the tree's.
        assert ct.spent_today(conn, datetime.now(UTC), key=tp.EVOLVE_SPEND_KEY) > 0

    monkeypatch.setattr(server, "_evolve_chat", lambda model: ("gpt-5-mini", lambda s, p, u: {"score": 0.9, "pass": True, "reason": "ok"}))
    judged = client.post("/api/internal/eval/judge", headers=auth, json={"task": "t", "rubric": "r", "answer": "a"})
    assert judged.status_code == 200 and judged.json()["pass"] is True
