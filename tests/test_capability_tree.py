"""The capability tree (crowdcode.capability_tree, contract C7).

Pure tests cover the split rule, coverage of reliable services, demand on empty
leaves and the token budgets. The integration test runs the real job against
PostgreSQL with a scripted model and a scripted reliability rule.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from crowdcode import capability_tree as ct

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def resource(rid: int, sid: str, *, payers: int = 10, price: int = 10_000, name: str | None = None,
             liveness: str = "alive", payable: bool = True) -> dict:
    return {
        "id": rid, "method": "POST", "origin": f"https://{sid}.example",
        "route_template": f"https://{sid}.example/v1/r{rid}", "probe_url": f"https://{sid}.example/v1/r{rid}",
        "service_id": sid, "service_name": None, "service_label": name or f"{sid} r{rid}",
        "description": f"Endpoint {rid}", "input_schema": None, "payable": payable, "networks": ["eip155:8453"],
        "liveness": liveness, "bazaar_payers_30d": payers, "price_atomic": price, "price_asset": USDC,
        "quoted_price_atomic": None, "quoted_asset": None,
    }


def reliable(sid: str, judged: int = 5, delivered: int = 5) -> dict:
    return {"service_id": sid, "state": "reliable", "rule_version": "reliability-v1",
            "reasons": [f"{delivered} of {judged} judged purchases delivered in 30 days",
                        "valid 402 in 3 of the last 3 daily probes (latest 2026-11-17 06:00 UTC)"],
            "inputs": {"judged_30d": {"total": judged, "delivered": delivered, "partial": 0, "not_delivered": judged - delivered}}}


def leaf(node: str, name: str, *facets: str) -> dict:
    return {"id": f"{node}.{name}", "node": node, "slug": name, "label": name.replace("-", " ").title(),
            "when_to_look": f"When the task needs {name}.", "facets": list(facets)}


def assign(node: str, facet: str) -> list[dict]:
    return [{"node": node, "facet": facet}]


def node_of(tree: dict, node_id: str) -> dict:
    return next(n for n in tree["nodes"] if n["id"] == node_id)


def video_fixture(states: dict) -> dict:
    """Video with four candidate leaves: X wins short-form and animated, Y wins
    long-form, nothing reliable for editing (which has demand)."""
    resources = [resource(1, "svc_x", payers=50), resource(2, "svc_x", payers=40), resource(3, "svc_y", payers=30),
                 resource(4, "svc_u", payers=90), resource(5, "svc_v", payers=5), resource(6, "svc_bad", payers=500)]
    assignments = {"r:1": assign("video", "text-to-video"), "r:2": assign("video", "animation"),
                   "r:3": assign("video", "long-video"), "r:4": assign("video", "video-editing"),
                   "r:5": assign("video", "text-to-video"), "r:6": assign("video", "video-editing"),
                   "p:p_edit": assign("video", "video-editing")}
    leaves = [leaf("video", "short-form", "text-to-video"), leaf("video", "animated", "animation"),
              leaf("video", "long-form", "long-video"), leaf("video", "editing", "video-editing")]
    proposals = [{"id": "p_edit", "title": "Template Video Renderer", "demand": {"requests": 5, "requesters": 3},
                  "price": {"median": 0.5}}]
    return ct.assemble(resources=resources, assignments=assignments, leaves=leaves, proposals=proposals,
                       reliability=ct.ReliabilityView(states=states, available=True), built_at=NOW, version=3)


# --- The split rule ----------------------------------------------------------------


def test_a_leaf_splits_only_where_a_different_reliable_service_wins():
    states = {"svc_x": reliable("svc_x"), "svc_y": reliable("svc_y", 4, 4),
              "svc_bad": {"service_id": "svc_bad", "state": "unreliable", "reasons": ["3 failed probes"]}}
    video = node_of(video_fixture(states), "video")
    served = {leaf["id"]: leaf for leaf in video["leaves"]}
    # short-form and animated share a winner (X): one leaf, covering both.
    assert set(served) == {"video.short-form", "video.long-form", "video.other"}
    assert {c["id"] for c in served["video.short-form"]["covers"]} == {"video.short-form", "video.animated"}
    assert served["video.short-form"]["picks"][0]["service_id"] == "svc_x"
    assert served["video.long-form"]["picks"][0]["service_id"] == "svc_y"
    # Every non-remainder leaf has its own winner.
    winners = [leaf["picks"][0]["service_id"] for leaf in video["leaves"] if not leaf["remainder"]]
    assert len(winners) == len(set(winners))
    # Editing has no reliable winner: merged up into the remainder, empty, with its demand.
    other = served["video.other"]
    assert other["remainder"] and other["empty"] and other["picks"] == []
    assert [c["id"] for c in other["covers"]] == ["video.editing"]
    assert other["demand"][0]["title"] == "Template Video Renderer" and other["demand_requests"] == 5
    # Candidates are unverified only: never the unreliable service, never a pick.
    assert [c["service_id"] for c in other["candidates"]] == ["svc_u"]
    assert video["best_pick"]["service_id"] == "svc_x"


def test_one_winner_or_none_keeps_the_node_whole():
    video = node_of(video_fixture({"svc_x": reliable("svc_x")}), "video")
    assert [leaf["id"] for leaf in video["leaves"]] == ["video.all"]
    only = video["leaves"][0]
    assert {c["id"] for c in only["covers"]} == {"video.short-form", "video.animated", "video.long-form", "video.editing"}
    assert [p["service_id"] for p in only["picks"]] == ["svc_x"]

    nothing = node_of(video_fixture({}), "video")
    assert [leaf["id"] for leaf in nothing["leaves"]] == ["video.all"]
    assert nothing["empty"] and nothing["leaves"][0]["empty"]
    # Nothing reliable yet still carries the demand and the candidates to verify next.
    assert nothing["leaves"][0]["demand_count"] == 1
    assert [c["service_id"] for c in nothing["leaves"][0]["candidates"]][:2] == ["svc_bad", "svc_u"]


def test_every_reliable_service_sits_on_a_leaf_even_before_it_is_classified():
    resources = [resource(1, "svc_a"), resource(2, "svc_new")]
    tree = ct.assemble(resources=resources, assignments={"r:1": assign("search", "web-search")},
                       leaves=[leaf("search", "general", "web-search")], proposals=[],
                       reliability=ct.ReliabilityView(states={"svc_a": reliable("svc_a"), "svc_new": reliable("svc_new")},
                                                      available=True), built_at=NOW)
    on_tree = {p["service_id"] for n in tree["nodes"] for l in n["leaves"] for p in l["picks"]}
    assert on_tree == {"svc_a", "svc_new"}
    assert tree["stats"]["reliable_on_tree"] == 2
    assert node_of(tree, "developer")["leaves"][0]["picks"][0]["service_id"] == "svc_new"


def test_without_reliability_everything_is_unverified_and_the_tree_still_builds():
    tree = video_fixture({})
    tree["reliability"] = {"available": False}
    assert all(n["empty"] for n in tree["nodes"] if n["kind"] != "policy")
    l0 = ct.render_l0(tree)
    assert "nothing reliable yet" in l0 and "unavailable" in l0


def test_a_covered_candidate_leaf_id_still_resolves():
    tree = video_fixture({"svc_x": reliable("svc_x"), "svc_y": reliable("svc_y")})
    node, found = ct.find_entry(tree, "video.animated")
    assert node["id"] == "video" and found["id"] == "video.short-form"
    assert ct.find_entry(tree, "video.editing")[1]["id"] == "video.other"
    assert ct.find_entry(tree, "video")[1] is None
    assert ct.find_entry(tree, "nope") is None


def test_explicit_capability_on_a_proposal_wins():
    resources = [resource(1, "svc_a")]
    proposals = [{"id": "p1", "title": "Flights", "capability": "travel", "demand": {"requests": 2}}]
    tree = ct.assemble(resources=resources, assignments={"r:1": assign("search", "web-search"),
                                                         "p:p1": assign("search", "web-search")},
                       leaves=[leaf("search", "general", "web-search")], proposals=proposals,
                       reliability=ct.ReliabilityView(), built_at=NOW)
    assert node_of(tree, "travel")["leaves"][0]["demand"][0]["id"] == "p1"
    assert node_of(tree, "search")["leaves"][0]["demand"] == []


# --- Token budgets -----------------------------------------------------------------


def big_tree() -> dict:
    """Every node with eight long-named leaves, each with several reliable picks."""
    resources, assignments, leaves, states, proposals = [], {}, [], {}, []
    rid = 0
    for node in ct.NODES:
        if not node.hint:
            continue
        for k in range(ct.MAX_LEAVES_PER_NODE):
            name = f"situation-number-{k}-with-a-long-name"
            leaves.append({**leaf(node.id, name, f"facet-{k}"),
                           "label": f"A rather long leaf label for situation {k}"})
            for j in range(4):
                rid += 1
                sid = f"svc_{node.id}_{k}_{j}"
                resources.append(resource(rid, sid, payers=1000 - j,
                                          name=f"An Extremely Verbose Service Name Number {rid} /api/v1/{'x' * 30}"))
                assignments[f"r:{rid}"] = assign(node.id, f"facet-{k}")
                if j < 3:
                    # Distinct winners in every leaf: nothing merges.
                    states[sid] = reliable(sid, 7, 6 if j else 7)
            proposals.append({"id": f"p_{node.id}_{k}", "title": "A long proposal title " * 3,
                              "demand": {"requests": 9, "requesters": 4}})
            assignments[f"p:p_{node.id}_{k}"] = assign(node.id, f"facet-{k}")
    return ct.assemble(resources=resources, assignments=assignments, leaves=leaves, proposals=proposals,
                       reliability=ct.ReliabilityView(states=states, available=True), built_at=NOW, version=12)


def _tiktoken():
    try:
        import tiktoken
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def test_l0_and_every_entry_fit_their_token_budgets():
    tree = big_tree()
    row = {"version": 12, "published": True, "tree": tree}
    enc = _tiktoken()
    l0 = ct.render_l0(tree)
    assert ct.token_bound(l0) <= ct.L0_TOKENS
    assert all(f"- {n.id}:" in l0 for n in ct.NODES if n.hint)  # one line per node, none dropped
    if enc:
        assert len(enc.encode(l0)) <= ct.L0_TOKENS
    ids = [n["id"] for n in tree["nodes"]] + [l["id"] for n in tree["nodes"] for l in n["leaves"]]
    assert len(ids) > 100
    for entry in ids:
        # As served: Starlette's JSONResponse writes UTF-8, not \\u escapes.
        body = json.dumps(ct.entry_payload(row, entry), ensure_ascii=False)
        assert ct.token_bound(body) <= ct.ENTRY_TOKENS, entry
        if enc:
            assert len(enc.encode(body)) <= ct.ENTRY_TOKENS, entry


def test_the_token_bound_is_an_upper_bound_on_this_kind_of_text():
    enc = _tiktoken()
    if enc is None:
        pytest.skip("tiktoken is not installed")
    tree = big_tree()
    for text in [ct.render_l0(tree), *(ct.render_entry(tree, n["id"]) for n in tree["nodes"])]:
        assert len(enc.encode(text)) <= ct.token_bound(text)


def test_a_leaf_rendering_names_picks_reasons_candidates_and_demand():
    tree = video_fixture({"svc_x": reliable("svc_x"), "svc_y": reliable("svc_y", 4, 3)})
    text = ct.render_entry(tree, "video.long-form")
    assert "Pick: svc_y r3, $0.01, 75% delivered of 4, 30 payers" in text
    assert "Why: 3 of 4 judged purchases delivered in 30 days" in text
    other = ct.render_entry(tree, "video.other")
    assert "Nothing reliable yet" in other and "Demand (1): Template Video Renderer (5 requests)" in other
    assert "Unverified candidates (2): svc_bad r6 ($0.01, 500 payers); svc_u r4" in other


# --- Classification ----------------------------------------------------------------


class ScriptedModel:
    """Answers like the model would: classifies by keywords, groups facets per node."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, system: str, payload: dict, usage: dict) -> dict:
        self.calls.append(payload)
        if self.fail:
            raise httpx.ConnectError("down")
        usage.update({"prompt_tokens": 1000, "completion_tokens": 200})
        if "items" in payload:
            out = {}
            for item in payload["items"]:
                text = json.dumps(item).lower()
                if "video" in text:
                    out[str(item["i"])] = "video:text-to-video"
                elif "search" in text:
                    out[str(item["i"])] = "search:web-search|nonsense:x"
                elif "skip" in text:
                    continue
                else:
                    out[str(item["i"])] = "developer:misc-tools"
            return {"r": out}
        facets = [f["f"] for f in payload["facets"]]
        return {"new_leaves": [{"slug": "general", "label": "General", "when_to_look": "When anything."}]
                if not payload["existing_leaves"] else [],
                "map": {f: "general" for f in facets}}


def items(*texts: str) -> list[ct.Item]:
    return [ct.Item(f"r:{i}", {"d": t}, f"fp{i}") for i, t in enumerate(texts)]


def test_an_item_the_model_skips_twice_is_stored_unclassified_not_resent(db):
    seed(db)
    with db() as conn:
        conn.execute("update index_resources set description = 'skip this one' where service_id = 'svc_dns'")
        conn.commit()
    model = ScriptedModel()
    report = ct.run(db, NOW, chat=model, reliability=ct.ReliabilityView(), settings=Settings())
    assert report["classify"]["unclassifiable"] == 1
    # One batch, then the retry of the skipped item alone.
    assert [len(c["items"]) for c in model.calls if "items" in c] == [4, 1]
    calls = len(model.calls)
    assert ct.run(db, NOW + timedelta(hours=1), chat=model, reliability=ct.ReliabilityView(),
                  settings=Settings())["pending"] == 0
    assert len(model.calls) == calls


def test_classify_batch_keeps_valid_pairs_and_leaves_the_rest_pending():
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=1.0)
    result = ct.classify_batch(ScriptedModel(), spend, items("make a video", "web search", "skip me"), {})
    assert result == {"r:0": [{"node": "video", "facet": "text-to-video"}],
                      "r:1": [{"node": "search", "facet": "web-search"}]}  # the unknown node is dropped
    assert spend.calls == 1 and spend.usd == pytest.approx((1000 * 0.25 + 200 * 2.0) / 1e6)


def test_the_budget_stops_calls_before_they_are_made():
    model = ScriptedModel()
    spend = ct.Spend(model="gpt-5-mini", remaining_usd=0.0000001)
    with pytest.raises(ct.BudgetExhausted):
        ct.classify_batch(model, spend, items("a video"), {})
    assert model.calls == []
    # An unknown model is costed at the most expensive rate.
    assert ct.Spend(model="mystery", remaining_usd=1).price() == ct.UNKNOWN_MODEL_PRICE
    assert ct.Spend(model="gpt-5-mini-2025-08-07", remaining_usd=1).price() == ct.MODEL_PRICES["gpt-5-mini"]
    # A failed call is None (its items stay pending), not an empty answer.
    assert ct.classify_batch(ScriptedModel(fail=True), ct.Spend(model="gpt-5-mini", remaining_usd=1), items("x"), {}) is None


def test_slugs_and_schema_keys():
    assert ct.slug("Risk, compliance & verification checks", 30) == "risk-compliance-verification"
    assert ct._input_keys({"info": {"queryParams": {"q": "x"}}, "schema": {"properties": {
        "body": {"properties": {"prompt": {}, "seconds": {}}}}}}) == ["prompt", "seconds", "q"]


# --- Reliability (C3) --------------------------------------------------------------


def test_pick_reasons_prefer_delivery_and_stay_one_line():
    row = {"reasons": ["valid 402 in 3 of the last 3 daily probes", "price within 2x of the median",
                       "4 of 4 judged purchases delivered"]}
    assert ct.pick_reason(row) == "4 of 4 judged purchases delivered · valid 402 in 3 of the last 3 daily probes"
    assert len(ct.pick_reason({"reasons": ["x" * 400]})) <= 160
    assert ct.delivered_rate({"inputs": {"judged_30d": {"total": 5, "delivered": 4, "partial": 1}}}) == (0.8, 5)
    assert ct.delivered_rate({"inputs": {"judged_30d": {"total": 0}}}) == (None, 0)
    assert ct.delivered_rate({"inputs": {}}) == (None, None)


def test_reliability_is_read_from_service_reliability_and_a_failure_is_unverified(db):
    seed(db)
    with db() as conn:
        conn.execute((ROOT / "supabase/reliability.sql").read_text())
        for sid, state in (("svc_vid", "reliable"), ("svc_find", "unreliable"), ("svc_dns", "unverified")):
            conn.execute("insert into service_reliability (service_id, state, rule_version, computed_at, reasons, inputs) "
                         "values (%s, %s, 'reliability-v1', now(), '[\"r\"]', '{}')", (sid, state))
        conn.commit()
    view = ct.load_reliability(db)
    assert view.available and view.rule_version == "reliability-v1"
    assert (view.state("svc_vid"), view.state("svc_find"), view.state("svc_dns")) == ("reliable", "unreliable", "unverified")

    @contextmanager
    def broken():
        raise psycopg.OperationalError("down")
        yield
    down = ct.load_reliability(broken)
    assert not down.available and down.states == {} and down.error == "OperationalError"


# --- The job, against PostgreSQL ---------------------------------------------------


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "tree_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"create schema {schema}")
        admin.execute(f"set search_path to {schema}")
        admin.execute((ROOT / "supabase/schema.sql").read_text())
        admin.execute((ROOT / "supabase/services-index.sql").read_text())
        # Idempotent: applying it twice changes nothing.
        admin.execute((ROOT / "supabase/capability-tree.sql").read_text())
        admin.execute((ROOT / "supabase/capability-tree.sql").read_text())

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as conn:
            yield conn

    try:
        yield connect
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"drop schema {schema} cascade")


def seed(connect, n: int = 3) -> None:
    with connect() as conn:
        for i, (sid, text) in enumerate([("svc_vid", "Generate a video from a prompt"),
                                         ("svc_find", "Web search for agents"),
                                         ("svc_dns", "DNS lookups")][:n]):
            conn.execute("insert into services (id, name, created_from_review) values (%s, %s, false)",
                         (sid, f"{sid} service"))
            conn.execute(
                """
                insert into index_resources (resource_key, method, origin, route_template, probe_url, payable,
                  networks, price_atomic, price_asset, bazaar_payers_30d, description, tracked, tracked_reason,
                  service_id)
                values (%s, 'POST', %s, %s, %s, true, '{eip155:8453}', 10000, %s, %s, %s, true, 'bazaar_payers', %s)
                """,
                (f"POST https://{sid}.example/v1", f"https://{sid}.example", f"https://{sid}.example/v1",
                 f"https://{sid}.example/v1", USDC, 10 + i, text, sid),
            )
        conn.execute(
            "insert into app_cache (key, payload, generated_at) values ('service_proposals', %s::jsonb, now())",
            (json.dumps({"version": 2, "proposals": [
                {"id": "p_vid", "title": "Validated Prompt Video", "problem": "video", "demand": {"requests": 3}}]}),),
        )
        conn.commit()


class Settings:
    openai_api_key = "test"
    openai_model = "gpt-5-mini"


def test_the_job_classifies_once_builds_versions_and_publishes(db, monkeypatch):
    seed(db)
    model = ScriptedModel()
    view = ct.ReliabilityView(states={"svc_vid": reliable("svc_vid")}, available=True)
    report = ct.run(db, NOW, chat=model, reliability=view, settings=Settings())
    assert report["classify"]["classified"] == 4 and report["built"]["version"] == 1
    assert report["built"]["reliable_on_tree"] == 1 and report["spend"]["usd"] > 0

    with db() as conn:
        current = ct.latest(conn, published_only=True)
        assert current["version"] == 1 and current["published"]
        summary = ct.summary_payload(current)
        video = next(n for n in summary["nodes"] if n["id"] == "video")
        assert video["best_pick"]["service_id"] == "svc_vid"
        assert ct.token_bound(summary["l0"]) <= ct.L0_TOKENS
        # Empty search still carries the candidates W2 should verify.
        search = ct.entry_payload(current, "search")
        assert "svc_find service" in search["text"] and "Nothing reliable yet" in search["text"]
        assert "Validated Prompt Video" in ct.entry_payload(current, "video")["text"]
        spent = ct.spent_today(conn, NOW)
        assert spent == pytest.approx(report["spend"]["usd"])

    # Nothing new an hour later: no model call, no new version.
    calls = len(model.calls)
    later = ct.run(db, NOW + timedelta(hours=1), chat=model, reliability=view, settings=Settings())
    assert len(model.calls) == calls and later["built"] is None

    # A reliability change rebuilds at once.
    changed = ct.ReliabilityView(states={"svc_vid": reliable("svc_vid"), "svc_find": reliable("svc_find")},
                                 available=True)
    assert ct.run(db, NOW + timedelta(hours=2), chat=model, reliability=changed,
                  settings=Settings())["built"]["trigger"] == "changed"

    # C3 unreachable: keep the version with picks rather than publish an empty one.
    down = ct.run(db, NOW + timedelta(hours=3), chat=model, reliability=ct.ReliabilityView(error="ConnectError"),
                  settings=Settings())
    assert down["built"] is None and "keeping" in down["skipped"]

    # The W8 seam: without auto-publish a build is only a candidate until published.
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
    nightly = ct.run(db, NOW + timedelta(hours=26), chat=model, reliability=changed, settings=Settings())
    assert nightly["built"]["trigger"] == "nightly"
    with db() as conn:
        assert ct.latest(conn, published_only=True)["version"] == 2
        candidate = ct.latest(conn, published_only=False)
        assert candidate["version"] == 3 and not candidate["published"]
        assert ct.publish(conn, 3, by="w8 gate", now=NOW)
        conn.commit()
        assert ct.latest(conn, published_only=True)["version"] == 3


def test_a_model_outage_is_reported_and_the_tree_still_builds(db):
    seed(db)
    with pytest.raises(RuntimeError, match="model call"):
        ct.run(db, NOW, chat=ScriptedModel(fail=True), reliability=ct.ReliabilityView(), settings=Settings())
    with db() as conn:
        assert ct.latest(conn, published_only=True)["version"] == 1


def test_the_daily_cap_holds_across_runs(db, monkeypatch):
    seed(db)
    monkeypatch.setenv("CROWDCODE_TREE_DAILY_BUDGET_USD", "0.0001")
    model = ScriptedModel()
    report = ct.run(db, NOW, chat=model, reliability=ct.ReliabilityView(), settings=Settings())
    # The first call fits nowhere in a $0.0001 day: nothing is sent.
    assert model.calls == [] and report["built"]["classified_resources"] == 0


def test_old_versions_are_pruned_but_the_published_one_is_kept(db, monkeypatch):
    seed(db, 1)
    with db() as conn:
        ct.store_version(conn, {"nodes": []}, fingerprint="a", trigger="first", now=NOW, stats={})
        monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
        for i in range(ct.KEEP_VERSIONS + 3):
            ct.store_version(conn, {"nodes": []}, fingerprint=str(i), trigger="changed", now=NOW, stats={})
        versions = [r["version"] for r in conn.execute("select version from capability_tree_versions order by 1")]
        assert 1 in versions and len(versions) == ct.KEEP_VERSIONS + 1


# --- W8: the gate's publish seam and test fixtures ---------------------------------


def _store(conn, n: int, monkeypatch) -> list[int]:
    monkeypatch.setenv("CROWDCODE_TREE_AUTO_PUBLISH", "0")
    tree = video_fixture({"svc_x": reliable("svc_x")})
    return [ct.store_version(conn, tree, fingerprint=f"f{i}", trigger="nightly", now=NOW + timedelta(hours=i),
                             stats={"built": i}) for i in range(n)]


def test_the_gate_publishes_only_a_newer_real_candidate_and_keeps_its_evidence(db, monkeypatch):
    with db() as conn:
        v1, v2, v3 = _store(conn, 3, monkeypatch)
        assert ct.latest(conn, published_only=True) is None
        evidence = {"baseline": None, "candidate": v2, "tasks": [{"task": "free_math", "outcome": "pass"}]}
        assert ct.publish_gated(conn, v2, by="tree-gate 2026-11-18", evidence=evidence, now=NOW) == "published"
        current = ct.latest(conn, published_only=True)
        assert current["version"] == v2 and current["published_by"] == "tree-gate 2026-11-18"
        assert current["stats"]["gate"]["tasks"][0]["task"] == "free_math" and current["stats"]["built"] == 1
        # A retry is not an error; an older version is never served again by the gate.
        assert ct.publish_gated(conn, v2, by="again", evidence={}, now=NOW) == "already_published"
        assert ct.publish_gated(conn, v1, by="gate", evidence={}, now=NOW) == "not_newer"
        assert ct.publish_gated(conn, 99, by="gate", evidence={}, now=NOW) == "not_found"
        # Oversized evidence keeps the comparison and drops the task list.
        huge = {"decision": "publish", "tasks": [{"detail": "x" * 500}] * 40}
        assert ct.publish_gated(conn, v3, by="gate", evidence=huge, now=NOW) == "published"
        gate = ct.get_version(conn, v3)["stats"]["gate"]
        assert gate == {"truncated": True, "decision": "publish"}


def test_a_fixture_is_worse_advice_on_the_same_shape_and_is_never_published(db, monkeypatch):
    with db() as conn:
        [v1] = _store(conn, 1, monkeypatch)
        base = ct.get_version(conn, v1)
        fixture = ct.store_fixture(conn, v1, "buy_first", now=NOW + timedelta(days=1))
        row = ct.get_version(conn, fixture)
        assert fixture == v1 + 1 and not row["published"]
        assert row["trigger"] == f"fixture:buy_first:v{v1}"
        # Same fingerprint and build time: the hourly cron neither rebuilds for it nor counts it as fresh.
        assert row["fingerprint"] == base["fingerprint"] and row["built_at"] == base["built_at"]
        # Every node and leaf is still there; only the policy changed.
        assert [n["id"] for n in row["tree"]["nodes"]] == [n["id"] for n in base["tree"]["nodes"]]
        l0 = ct.render_l0(row["tree"])
        assert l0.splitlines()[1].startswith("Buy first") and "Free first" not in l0
        assert row["tree"]["version"] == fixture
        assert ct.summary_payload(row)["trigger"].startswith("fixture:")
        assert ct.latest(conn, published_only=False)["version"] == fixture
        assert ct.publish_gated(conn, fixture, by="gate", evidence={}, now=NOW) == "fixture"
        assert ct.latest(conn, published_only=True) is None
    dropped = ct.degrade(base["tree"], "drop_free_first")
    assert "free-first" not in [n["id"] for n in dropped["nodes"]]
    assert "Free first" not in ct.render_l0(dropped) and len(dropped["nodes"]) == len(base["tree"]["nodes"]) - 1
    with pytest.raises(ValueError, match="unknown fixture"):
        ct.degrade({"nodes": []}, "nonsense")


def test_the_publish_endpoint_needs_the_gate_token_and_a_well_formed_body(db, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from crowdcode import server

    with db() as conn:
        [v1, v2] = _store(conn, 2, monkeypatch)
        conn.commit()
    monkeypatch.setattr(server, "connect", db)
    client = TestClient(Starlette(routes=[Route("/api/internal/tree/publish", server.tree_publish, methods=["POST"])]))
    body = {"version": v2, "by": "tree-gate test", "evidence": {"decision": "publish"}}
    monkeypatch.delenv("CROWDCODE_TREE_GATE_TOKEN", raising=False)
    assert client.post("/api/internal/tree/publish", json=body).status_code == 404
    monkeypatch.setenv("CROWDCODE_TREE_GATE_TOKEN", "gate-token-0123456789abcdef0123456789")
    auth = {"authorization": "Bearer gate-token-0123456789abcdef0123456789"}
    assert client.post("/api/internal/tree/publish", json=body).status_code == 401
    assert client.post("/api/internal/tree/publish", json=body, headers={"authorization": "Bearer nope"}).status_code == 401
    for bad in [{**body, "version": "2"}, {**body, "version": True}, {**body, "by": " "}, {**body, "evidence": []}, []]:
        assert client.post("/api/internal/tree/publish", json=bad, headers=auth).status_code == 400
    ok = client.post("/api/internal/tree/publish", json=body, headers=auth)
    assert ok.status_code == 200 and ok.json() == {"ok": True, "result": "published", "version": v2}
    older = client.post("/api/internal/tree/publish", json={**body, "version": v1}, headers=auth)
    assert older.status_code == 409 and older.json()["result"] == "not_newer"
    with db() as conn:
        assert ct.latest(conn, published_only=True)["version"] == v2


# --- The standing media rule (C14) ---------------------------------------------------


def test_the_media_rule_is_in_l0_and_on_every_media_node_and_leaf():
    tree = video_fixture({"svc_x": reliable("svc_x"), "svc_y": reliable("svc_y")})
    rule = "deliver a real file from a model. Never substitute SVG, HTML, CSS or canvas animation, or a slideshow"
    assert rule in ct.render_l0(tree)
    assert tree["hard_rules"] == [ct.MEDIA_RULE]
    for entry in ("video", "video.long-form", "video.other", "image", "audio"):
        assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_entry(tree, entry), entry
    assert "Hard rule" not in ct.render_entry(tree, "search")
    summary = ct.summary_payload({"version": 3, "published": True, "tree": tree})
    assert {n["id"] for n in summary["nodes"] if n.get("hard_rule")} == set(ct.MEDIA_NODES)


def test_a_version_stored_before_the_rule_still_shows_it():
    tree = video_fixture({})
    del tree["hard_rules"]
    for node in tree["nodes"]:
        node.pop("hard_rule", None)
    assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_l0(tree)
    assert f"Hard rule: {ct.MEDIA_RULE}" in ct.render_entry(tree, "video")
    assert "Hard rule" not in ct.render_entry(tree, "finance")
