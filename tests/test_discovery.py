from contextlib import contextmanager

from crowdcode import discovery
from tests.test_shared_wallet_db import db  # noqa: F401  isolated PostgreSQL schema fixture
from crowdcode.discovery import (
    BASE_NETWORK,
    NETWORK_TOKENS,
    SOLANA_NETWORK,
    TRANSFER_COST_USD,
    find_services,
)


def accept(network, amount, asset=None, scheme="exact"):
    name = discovery.network_name(network)
    return {"network": network, "amount": str(amount), "asset": asset or NETWORK_TOKENS[name],
            "scheme": scheme, "payTo": "payee"}


def resource(url, *accepts, payers=None, method="POST"):
    item = {"resource": url, "description": f"desc {url}", "accepts": list(accepts),
            "extensions": {"bazaar": {"info": {"input": {"method": method}}}}}
    if payers is not None:
        item["quality"] = {"l30DaysUniquePayers": payers}
    return item


class FakeCatalogs:
    def __init__(self, cdp=None, mpp=None, fail=()):
        self.cdp = cdp or {"resources": []}
        self.mpp = mpp or {"services": []}
        self.fail = fail

    def cdp_search(self, query):
        if "cdp" in self.fail:
            raise RuntimeError("down")
        return self.cdp

    def mpp_catalog(self):
        if "mpp" in self.fail:
            raise RuntimeError("down")
        return self.mpp


class FakeConn:
    def __init__(self, directory, evidence):
        self.directory = directory
        self.evidence = evidence
        self.last = None

    def execute(self, sql, params=None):
        self.last = sql
        return self

    def fetchall(self):
        if self.last.strip().startswith("select id, name"):
            return self.directory
        return self.evidence


def conn_factory(directory=(), evidence=()):
    @contextmanager
    def factory():
        yield FakeConn(list(directory), list(evidence))
    return factory


def mpp_service(url, path, amount, description="Upload a file to object storage"):
    return {"name": "Object Storage", "serviceUrl": url, "description": "S3 storage", "status": "active",
            "tags": ["storage", "files"], "categories": ["storage"],
            "endpoints": [{"method": "PUT", "path": path, "description": description,
                           "payment": {"method": "tempo", "intent": "charge", "currency": NETWORK_TOKENS["tempo"],
                                       "decimals": 6, "amount": str(amount)}}]}


def test_solana_only_service_from_bazaar_is_payable_and_ranked():
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://tick.example/price", accept(SOLANA_NETWORK, 5000)),
        resource("https://base.example/price", accept(BASE_NETWORK, 9000)),
    ]})
    result = find_services("token price", catalogs=catalogs)
    first = result["services"][0]
    assert first["endpoint"] == "https://tick.example/price"
    assert first["best"]["network"] == "solana"
    assert first["best"]["price_usd"] == 0.005
    assert first["sources"] == ["cdp-bazaar"]


def test_quality_outranks_price_across_chains():
    catalogs = FakeCatalogs(
        cdp={"resources": [resource("https://cheap.example/upload", accept(BASE_NETWORK, 1000))]},
        mpp={"services": [mpp_service("https://storage.example", "/upload", 20000)]},
    )
    evidence = [{"endpoint": "https://storage.example/upload", "service_id": "svc_1", "score": 4.4,
                 "n_eff": 6.0, "num_reviews": 7, "num_verified_reviews": 6}]
    result = find_services("upload file storage", catalogs=catalogs, conn_factory=conn_factory(evidence=evidence))
    ranked = [s["endpoint"] for s in result["services"]]
    assert ranked == ["https://storage.example/upload", "https://cheap.example/upload"]
    assert result["services"][0]["best"]["network"] == "tempo"
    assert result["services"][0]["quality"]["service_id"] == "svc_1"


def test_transfer_cost_applies_when_network_balance_is_short():
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://sol.example/a", accept(SOLANA_NETWORK, 10000)),
        resource("https://base.example/a", accept(BASE_NETWORK, 30000)),
    ]})
    result = find_services("thing", catalogs=catalogs, balances={"base": 2, "solana": 0, "tempo": 0})
    by_endpoint = {s["endpoint"]: s for s in result["services"]}
    sol = by_endpoint["https://sol.example/a"]["best"]
    assert sol["transfer_needed"] is True and sol["fundable"] is True
    assert sol["effective_price_usd"] == round(0.01 + TRANSFER_COST_USD, 6)
    # 0.01 + transfer (0.04) is still cheaper than 0.03 on Base? No: 0.04 > 0.03, so Base wins.
    assert result["services"][0]["endpoint"] == "https://base.example/a"


def test_multi_network_service_picks_the_network_with_balance():
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://both.example/a", accept(BASE_NETWORK, 10000), accept(SOLANA_NETWORK, 10000)),
    ]})
    result = find_services("thing", catalogs=catalogs, balances={"base": 0, "solana": 1})
    best = result["services"][0]["best"]
    assert best["network"] == "solana" and best["transfer_needed"] is False


def test_unpayable_offers_are_excluded():
    other_token = "0x0000000000000000000000000000000000000001"
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://polygon.example/a", accept("eip155:137", 1000, asset=other_token)),
        resource("https://wrong-token.example/a", accept(BASE_NETWORK, 1000, asset=other_token)),
        resource("https://upto.example/a", accept(BASE_NETWORK, 1000, scheme="upto")),
    ]})
    result = find_services("thing", catalogs=catalogs)
    assert result["services"] == []


def test_payer_networks_limit_payability():
    catalogs = FakeCatalogs(cdp={"resources": [resource("https://sol.example/a", accept(SOLANA_NETWORK, 1000))]})
    assert find_services("thing", catalogs=catalogs, networks=["base"])["services"] == []
    assert find_services("thing", catalogs=catalogs, networks="base,solana")["services"]


def test_sources_merge_with_provenance_and_caller_candidates():
    catalogs = FakeCatalogs(
        cdp={"resources": [resource("https://dual.example/upload/", accept(BASE_NETWORK, 5000), payers=40)]},
        mpp={"services": [mpp_service("https://dual.example", "/upload", 5000)]},
    )
    caller = [{"endpoint": "https://DUAL.example/upload", "price_usd": "0.005 USD", "protocols": ["x402"],
               "source": "agentcash"},
              {"url": "https://only-agentcash.example/x", "price": 0.002, "protocols": ["x402"]}]
    result = find_services("upload file storage", catalogs=catalogs, candidates=caller)
    dual = next(s for s in result["services"] if s["endpoint"] == "https://dual.example/upload")
    assert sorted(dual["sources"]) == ["agentcash", "cdp-bazaar", "mpp"]
    assert {(o["network"], o["protocol"]) for o in dual["offers"]} == {("base", "x402"), ("tempo", "mpp")}
    assert dual["quality"]["unique_payers_30d"] == 40
    unknown = next(s for s in result["services"] if s["endpoint"] == "https://only-agentcash.example/x")
    assert unknown["best"]["network"] is None and unknown["sources"] == ["caller"]


def test_adopted_services_rank_before_cheaper_unused_ones():
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://spam.example/search", accept(BASE_NETWORK, 100), payers=0),
        resource("https://used.example/search", accept(BASE_NETWORK, 3000), payers=25),
    ]})
    result = find_services("search", catalogs=catalogs)
    assert result["services"][0]["endpoint"] == "https://used.example/search"


def test_poorly_rated_services_rank_last():
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://bad.example/a", accept(BASE_NETWORK, 100)),
        resource("https://unknown.example/a", accept(BASE_NETWORK, 9000)),
    ]})
    evidence = [{"endpoint": "https://bad.example/a", "service_id": "svc_bad", "score": 1.4,
                 "n_eff": 5.0, "num_reviews": 6, "num_verified_reviews": 6}]
    result = find_services("thing", catalogs=catalogs, conn_factory=conn_factory(evidence=evidence))
    assert [s["endpoint"] for s in result["services"]] == ["https://unknown.example/a", "https://bad.example/a"]
    assert result["services"][1]["quality"]["below_floor"] is True


def test_reviewed_directory_services_join_the_search():
    directory = [{"id": "svc_p", "name": "Parallel MPP Search", "directory_slug": "parallel",
                  "canonical_endpoint": "https://parallelmpp.dev/api/search", "payment_provider": "mppx"}]
    evidence = [{"endpoint": "https://parallelmpp.dev/api/search", "service_id": "svc_p", "score": 4.25,
                 "n_eff": 3.3, "num_reviews": 4, "num_verified_reviews": 3}]
    catalogs = FakeCatalogs(cdp={"resources": [resource("https://x.example/search", accept(BASE_NETWORK, 1000))]})
    result = find_services("web search", catalogs=catalogs, conn_factory=conn_factory(directory, evidence))
    first = result["services"][0]
    assert first["endpoint"] == "https://parallelmpp.dev/api/search"
    assert first["sources"] == ["crowdcode"] and first["best"]["network"] == "tempo"


def test_one_failing_catalog_does_not_hide_the_others():
    catalogs = FakeCatalogs(mpp={"services": [mpp_service("https://s.example", "/upload", 1000)]}, fail=("cdp",))
    result = find_services("upload file storage", catalogs=catalogs)
    assert result["sources"]["cdp-bazaar"]["ok"] is False
    assert result["services"][0]["endpoint"] == "https://s.example/upload"


def test_query_is_required_and_limit_is_bounded():
    assert find_services("  ", catalogs=FakeCatalogs())["ok"] is False
    many = {"resources": [resource(f"https://s{i}.example/a", accept(BASE_NETWORK, 1000 + i)) for i in range(30)]}
    assert len(find_services("a", catalogs=FakeCatalogs(cdp=many), limit=100)["services"]) == discovery.MAX_LIMIT


def test_evidence_and_directory_queries_against_postgres(db):  # noqa: F811
    from crowdcode.identity import build_identity, create_service_from_identity

    conn, _, _ = db
    with conn.transaction():
        service = create_service_from_identity(conn, build_identity(
            api_endpoint="https://storage.example/upload", payment_provider="mppx",
            payment_target_ref="0x" + "ab" * 20)).row
    conn.execute("update services set score = 4.2, n_eff = 3.0 where id = %s", (service["id"],))
    rows = discovery.directory_rows(conn)
    assert any(r["canonical_endpoint"] == "https://storage.example/upload" for r in rows)
    evidence = discovery.evidence_by_endpoint(conn, ["https://storage.example/upload", "https://nothing.example/x"])
    assert set(evidence) == {"https://storage.example/upload"}
    row = evidence["https://storage.example/upload"]
    assert row["service_id"] == service["id"] and float(row["score"]) == 4.2 and row["num_reviews"] == 0
    # The mppx payee pins Tempo, so the directory entry carries that network.
    assert discovery.fetch_service_networks(conn)[service["id"]]["networks"] == ["tempo"]


def test_generic_request_words_do_not_hide_specific_catalog_matches():
    drugs = {"name": "RxAtlas", "serviceUrl": "https://api.rxatlas.dev", "status": "active",
             "description": "Search normalized US drug products with FDA enrichments.", "tags": ["drugs", "fda"],
             "categories": ["data"], "endpoints": [{"method": "GET", "path": "/v1/drugs", "description": "Search drug products",
                                                    "payment": {"method": "tempo", "intent": "charge",
                                                                "currency": NETWORK_TOKENS["tempo"], "amount": "100000"}}]}
    result = find_services("look up drug information by drug name", catalogs=FakeCatalogs(mpp={"services": [drugs]}))
    assert [s["endpoint"] for s in result["services"]] == ["https://api.rxatlas.dev/v1/drugs"]


def test_a_clearly_better_score_beats_a_cheaper_service():
    catalogs = FakeCatalogs(cdp={"resources": [
        resource("https://cheap.example/search", accept(BASE_NETWORK, 2000)),
        resource("https://better.example/search", accept(BASE_NETWORK, 10000)),
        resource("https://tie.example/search", accept(BASE_NETWORK, 9000)),
    ]})
    evidence = [
        {"endpoint": "https://cheap.example/search", "service_id": "a", "score": 3.84, "n_eff": 1.7, "num_reviews": 1, "num_verified_reviews": 1},
        {"endpoint": "https://better.example/search", "service_id": "b", "score": 4.249, "n_eff": 3.3, "num_reviews": 4, "num_verified_reviews": 3},
        {"endpoint": "https://tie.example/search", "service_id": "c", "score": 4.18, "n_eff": 2.0, "num_reviews": 2, "num_verified_reviews": 2},
    ]
    result = find_services("search", catalogs=catalogs, conn_factory=conn_factory(evidence=evidence))
    # 4.249 and 4.18 share the 4.25 band, where the cheaper one wins; 3.84 is a band below.
    assert [s["endpoint"] for s in result["services"]] == [
        "https://tie.example/search", "https://better.example/search", "https://cheap.example/search"]


def test_word_forms_match_and_directory_entries_carry_seen_networks():
    rows = [{"id": "svc_ics", "name": "mwork.dev", "directory_slug": None,
             "canonical_endpoint": "https://mwork.dev/v1/generate-ics", "payment_provider": "mppx"},
            {"id": "svc_sol", "name": "Tick", "directory_slug": None,
             "canonical_endpoint": "https://tick.example/sku/calendar-ics", "payment_provider": "x402"}]
    networks = {"svc_sol": {"networks": ["solana"]}}
    found = discovery.directory_candidates(rows, "ics calendar file generation", networks=networks)
    assert [c.endpoint for c in found] == ["https://mwork.dev/v1/generate-ics", "https://tick.example/sku/calendar-ics"]
    assert [(o.network, o.payable) for o in found[0].offers] == [("tempo", True)]
    assert [(o.network, o.protocol, o.payable) for o in found[1].offers] == [("solana", "x402", True)]
    assert discovery.relevance(["transcription"], "transform json") == 0


# --- Matching by meaning (SRI-86) ---------------------------------------------------

from tests.test_semantic import embedder as toy_embedder  # noqa: E402

DRIP = "https://dripstack.xyz/api/v1/publications/{publicationSlug}/{postSlug}"


def listing(service_id, name, endpoint, description=None, listing_text=None, provider="x402"):
    return {"id": service_id, "name": name, "directory_slug": None, "canonical_endpoint": endpoint,
            "payment_provider": provider, "description": description, "listing_text": listing_text}


def reviewed(endpoint, service_id, score, n_eff=1.4):
    return {"endpoint": endpoint, "service_id": service_id, "score": score, "n_eff": n_eff,
            "num_reviews": 1, "num_verified_reviews": 1}


def test_a_listing_that_shares_no_words_with_the_request_is_found_by_meaning():
    directory = [
        listing("svc_drip", "Drip", DRIP, "Premium financial newsletter research and analyst calls."),
        listing("svc_mail", "StableEmail", "https://stableemail.dev/api/send", "Send an email."),
    ]
    evidence = [reviewed(DRIP, "svc_drip", 3.8)]
    catalogs = FakeCatalogs(cdp={"resources": [resource("https://x.example/weather", accept(BASE_NETWORK, 1000))]})
    # No word of "sell-side memo" appears in Drip's listing; keywords alone never offered it.
    keyword_only = find_services("sell-side memo", catalogs=catalogs, conn_factory=conn_factory(directory, evidence))
    assert DRIP not in [s["endpoint"] for s in keyword_only["services"]]
    result = find_services("sell-side memo", catalogs=catalogs, conn_factory=conn_factory(directory, evidence),
                           embedder=toy_embedder())
    assert result["services"][0]["endpoint"] == DRIP
    assert result["services"][0]["match"] > 0
    assert "https://stableemail.dev/api/send" not in [s["endpoint"] for s in result["services"]]
    assert result["ranking"] == discovery.RANKING


def test_on_a_near_tie_in_match_the_reviewed_service_beats_the_unproven_one():
    # Both are speech services, worded alike; the reviewed 3.8 wins over the unproven 3.0 prior.
    catalogs = FakeCatalogs(cdp={"resources": [
        dict(resource("https://new.example/speech", accept(BASE_NETWORK, 1000)), description="Speech audio from text"),
        dict(resource("https://known.example/speech", accept(BASE_NETWORK, 5000)), description="Speech audio from text"),
    ]})
    evidence = [reviewed("https://known.example/speech", "svc_known", 3.8)]
    result = find_services("voice mp3", catalogs=catalogs, conn_factory=conn_factory(evidence=evidence),
                           embedder=toy_embedder())
    assert [s["endpoint"] for s in result["services"]] == ["https://known.example/speech", "https://new.example/speech"]


def test_a_much_better_match_beats_a_better_score():
    catalogs = FakeCatalogs(cdp={"resources": [
        dict(resource("https://search.example/web", accept(BASE_NETWORK, 1000)), description="Search the web"),
        dict(resource("https://voice.example/speech", accept(BASE_NETWORK, 1000)), description="Speech audio voice"),
    ]})
    evidence = [reviewed("https://search.example/web", "svc_search", 4.4, n_eff=6)]
    result = find_services("voice speech audio", catalogs=catalogs, conn_factory=conn_factory(evidence=evidence),
                           embedder=toy_embedder())
    assert result["services"][0]["endpoint"] == "https://voice.example/speech"


def test_without_the_model_ranking_is_quality_first_as_before():
    class Unavailable:
        def similarities(self, query, documents):
            return None

    catalogs = FakeCatalogs(cdp={"resources": [
        dict(resource("https://search.example/web", accept(BASE_NETWORK, 1000)), description="Search the web"),
        dict(resource("https://voice.example/speech", accept(BASE_NETWORK, 1000)), description="Speech audio voice"),
    ]})
    evidence = [reviewed("https://search.example/web", "svc_search", 4.4, n_eff=6)]
    result = find_services("voice speech audio", catalogs=catalogs, conn_factory=conn_factory(evidence=evidence),
                           embedder=Unavailable())
    assert result["services"][0]["endpoint"] == "https://search.example/web"
    assert result["services"][0]["match"] is None
    assert result["ranking"] == discovery.RANKING_BY_QUALITY


def test_directory_listings_match_on_their_descriptions():
    rows = [listing("svc_v", "StableVoice", "https://stablevoice.dev/api/speech", "Convert text to natural speech, MP3 output.")]
    assert [c.endpoint for c in discovery.directory_candidates(rows, "text to mp3")] == ["https://stablevoice.dev/api/speech"]
