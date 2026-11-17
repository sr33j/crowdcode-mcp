"""reliability-v1 (SRI-115 C3): every branch of the rule, then the stored results,
the url lookup and the block list against PostgreSQL."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from crowdcode import reliability
from crowdcode.reliability import evaluate

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
PAYEE = "0x6d6e695b09861467c7d462f5aaf31cf3540b9192"
OTHER = "0xb98ef29eb2be19ae646a8fc0248255b90a332dbc"
STRANGER = "0x1111111111111111111111111111111111111111"


def iso(dt: datetime) -> str:
    return dt.isoformat()


def probe(days_ago: float, ok: bool = True, error_code: str | None = None, status: int | None = None) -> dict:
    return {"at": iso(NOW - timedelta(days=days_ago)), "ok": ok, "http_status": status or (402 if ok else 503),
            "error_code": None if ok else (error_code or "http_503"), "quoted_amount_atomic": "7000" if ok else None,
            "asset": USDC if ok else None}


def endpoint(**over) -> dict:
    ep = {
        "resource_id": 1, "method": "POST", "url": "https://api.example.com/search",
        "last_probes": [probe(0.1), probe(1.1), probe(2.1)],
        "last_paid_ok_at": None,
        "latest_price_atomic": "7000", "latest_price_asset": USDC, "median_price_7d_atomic": "7000",
        "payees": [PAYEE], "payee_since": iso(NOW - timedelta(days=30)),
        "payee_changes_7d": [], "bazaar_payers_30d": 147,
    }
    ep.update(over)
    return ep


def good(**over) -> dict:
    inputs = {
        "as_of": iso(NOW), "tracked": True, "blocked": None, "payee_rotates": False,
        "registered_payees": [{"payee": PAYEE, "since": iso(NOW - timedelta(days=60))}],
        "endpoints": [endpoint()],
        "judged_30d": {"total": 3, "delivered": 3, "partial": 0, "not_delivered": 0, "test": 0,
                       "test_delivered": 0, "unlabelled": 0, "owner_buyers": 2, "last_at": iso(NOW - timedelta(days=1))},
        "bazaar_payers_30d": 147, "external_payers_30d": 0,
    }
    inputs.update(over)
    return inputs


def judged(total: int, delivered: int, partial: int = 0, **extra) -> dict:
    return {"total": total, "delivered": delivered, "partial": partial,
            "not_delivered": total - delivered - partial, "test": 0, "unlabelled": 0, "owner_buyers": 1,
            "last_at": iso(NOW - timedelta(days=1)), **extra}


def state(inputs: dict) -> str:
    return evaluate(inputs)[0]


# --- reliable --------------------------------------------------------------------------


def test_the_baseline_is_reliable_and_says_why():
    s, reasons = evaluate(good())
    assert s == "reliable"
    assert reasons[0] == "valid 402 in 3 of the last 3 daily probes (latest 2026-11-20 09:36 UTC)"
    assert "latest price $0.007 is within 2× its 7-day median of $0.007" in reasons
    assert "3 of 3 judged purchases in the last 30 days delivered (100%; last 2026-11-19)" in reasons
    assert "payee unchanged in the last 7 days (current since 2026-10-21)" in reasons
    assert reasons[-1] == "payers in the last 30 days: 147 on the x402 Bazaar, 2 hosted-agent owners in judged purchases"


def test_two_valid_probes_of_three_are_enough_and_one_is_not():
    two = good(endpoints=[endpoint(last_probes=[probe(0.1), probe(1.1, ok=False), probe(2.1)])])
    assert state(two) == "reliable"
    # Exa's case: two probes so far, both valid.
    assert state(good(endpoints=[endpoint(last_probes=[probe(0.1), probe(1.1)])])) == "reliable"
    one = good(endpoints=[endpoint(last_probes=[probe(0.1)])])
    s, reasons = evaluate(one)
    assert s == "unverified"
    assert reasons[0] == ("valid 402 in 1 of the last 1 daily probe (latest 2026-11-20 09:36 UTC); "
                          "reliable needs 2 of the last 3")
    s, reasons = evaluate(good(endpoints=[endpoint(last_probes=[])]))
    assert s == "unverified" and reasons[0].startswith("not probed yet")


def test_price_up_to_twice_the_median_is_fine_and_above_it_is_not():
    assert state(good(endpoints=[endpoint(latest_price_atomic="14000")])) == "reliable"
    s, reasons = evaluate(good(endpoints=[endpoint(latest_price_atomic="14001")]))
    assert s == "unverified"
    assert reasons[0] == "latest price $0.014001 is more than 2× its 7-day median of $0.007"
    s, reasons = evaluate(good(endpoints=[endpoint(median_price_7d_atomic=None)]))
    assert s == "unverified" and reasons[0] == "no price quoted in a valid probe in the last 7 days"


def test_judged_thresholds_for_reliable():
    assert state(good(judged_30d=judged(3, 3))) == "reliable"
    assert state(good(judged_30d=judged(5, 4))) == "reliable"  # 80%
    s, reasons = evaluate(good(judged_30d=judged(2, 2)))
    assert s == "unverified"
    assert reasons[0] == ("2 of 2 judged purchases in the last 30 days delivered (100%; last 2026-11-19); "
                          "reliable needs at least 3")
    s, reasons = evaluate(good(judged_30d=judged(0, 0)))
    assert s == "unverified"
    assert reasons[0] == "no judged purchases in the last 30 days; reliable needs at least 3, with 80% delivered"


def test_partial_counts_as_not_delivered():
    # 4 delivered + 1 partial = 80%: reliable. 3 delivered + 1 partial = 75%: not.
    assert state(good(judged_30d=judged(5, 4, partial=1))) == "reliable"
    s, reasons = evaluate(good(judged_30d=judged(4, 3, partial=1)))
    assert s == "unverified"
    assert reasons[0] == ("3 of 4 judged purchases in the last 30 days delivered "
                          "(75%; 1 partial, counted as not delivered; last 2026-11-19); reliable needs 80% delivered")


def test_test_traffic_counts_and_is_labelled():
    s, reasons = evaluate(good(judged_30d=judged(4, 4, test=3, unlabelled=1)))
    assert s == "reliable"
    assert ("4 of 4 judged purchases in the last 30 days delivered (100%; 3 of them CrowdCode test purchases; "
            "1 from before test purchases were labelled; last 2026-11-19)") in reasons


# --- unreliable ----------------------------------------------------------------------


def test_blocked_is_unreliable_with_the_operators_note_first():
    s, reasons = evaluate(good(blocked={"note": "pays out to a wallet reported for fraud", "by": "ops",
                                        "at": iso(NOW - timedelta(hours=2))}))
    assert s == "unreliable"
    assert reasons[0] == "blocked by a CrowdCode operator on 2026-11-20: pays out to a wallet reported for fraud"


def test_three_failed_probes_are_unreliable():
    failed = [probe(0.1, ok=False), probe(1.1, ok=False, error_code="timeout"), probe(2.1, ok=False, error_code="http_404")]
    s, reasons = evaluate(good(endpoints=[endpoint(last_probes=failed)]))
    assert s == "unreliable"
    assert reasons[0] == "the last 3 daily probes all failed (http_503, timeout, http_404; latest 2026-11-20 09:36 UTC)"
    # Two failures are not three.
    assert state(good(endpoints=[endpoint(last_probes=failed[:2])])) == "unverified"


def test_a_successful_paid_call_since_the_failures_means_it_is_not_down():
    failed = [probe(0.1, ok=False), probe(1.1, ok=False), probe(2.1, ok=False)]
    alive = endpoint(last_probes=failed, last_paid_ok_at=iso(NOW - timedelta(days=1)))
    s, reasons = evaluate(good(endpoints=[alive]))
    assert s == "unverified"
    assert "but a paid call succeeded on 2026-11-19 12:00 UTC, so it is not counted as down" in reasons[0]
    stale = endpoint(last_probes=failed, last_paid_ok_at=iso(NOW - timedelta(days=5)))
    assert state(good(endpoints=[stale])) == "unreliable"


def test_a_refused_probe_body_is_inconclusive():
    rejected = probe(0.1, ok=False, error_code="probe_body_rejected", status=400)
    # Neither down ...
    all_rejected = [dict(rejected), probe(1.1, ok=False, error_code="probe_body_rejected", status=422),
                    probe(2.1, ok=False, error_code="probe_body_rejected", status=400)]
    s, reasons = evaluate(good(endpoints=[endpoint(last_probes=all_rejected)]))
    assert s == "unverified"
    assert "3 probes inconclusive (the seller refused the request body CrowdCode built)" in reasons[0]
    # ... nor a valid 402, and it does not break a run of failures into "down" either.
    two_failed = [probe(0.1, ok=False), rejected, probe(2.1, ok=False)]
    assert state(good(endpoints=[endpoint(last_probes=two_failed)])) == "unverified"
    assert state(good(endpoints=[endpoint(last_probes=[probe(0.1), rejected, probe(2.1)])])) == "reliable"


def test_a_payee_change_to_an_unadvertised_address_is_unreliable():
    change = {"at": iso(NOW - timedelta(days=2)), "from": [PAYEE], "to": [STRANGER]}
    s, reasons = evaluate(good(endpoints=[endpoint(payees=[STRANGER], payee_changes_7d=[change])]))
    assert s == "unreliable"
    assert reasons[0] == (f"payee changed on 2026-11-18 12:00 UTC to {STRANGER}, "
                          "which the service's own 402 had not advertised before")
    # Older than 7 days: no longer counted.
    old = dict(change, at=iso(NOW - timedelta(days=8)))
    assert state(good(endpoints=[endpoint(payees=[STRANGER], payee_changes_7d=[old])])) == "reliable"


def test_advertised_payees_and_rotation_do_not_count_as_a_change():
    # Exa advertises two Base payees; adding the second, already registered, is not a change.
    registered = [{"payee": PAYEE, "since": iso(NOW - timedelta(days=60))},
                  {"payee": OTHER.upper().replace("0X", "0x"), "since": iso(NOW - timedelta(days=30))}]
    change = {"at": iso(NOW - timedelta(days=2)), "from": [PAYEE], "to": [OTHER, PAYEE]}
    s, reasons = evaluate(good(registered_payees=registered,
                               endpoints=[endpoint(payees=[OTHER, PAYEE], payee_changes_7d=[change])]))
    assert s == "reliable"
    assert ("payee changed on 2026-11-18 12:00 UTC only to addresses the service already advertised; "
            "not counted as a change") in reasons
    # Dropping a payee is not a change to a new one.
    shrink = {"at": iso(NOW - timedelta(days=2)), "from": [OTHER, PAYEE], "to": [PAYEE]}
    assert state(good(endpoints=[endpoint(payee_changes_7d=[shrink])])) == "reliable"
    # A payee registered only after the change does not excuse it.
    late = [registered[0], {"payee": OTHER, "since": iso(NOW - timedelta(days=1))}]
    assert state(good(registered_payees=late,
                      endpoints=[endpoint(payees=[OTHER, PAYEE], payee_changes_7d=[change])])) == "unreliable"
    # A service that issues a fresh payee per request: changes are not counted.
    rotating = {"at": iso(NOW - timedelta(days=1)), "from": [PAYEE], "to": [STRANGER]}
    s, reasons = evaluate(good(payee_rotates=True, endpoints=[endpoint(payee_changes_7d=[rotating])]))
    assert s == "reliable"
    assert "the payee rotates per request (confirmed by an operator), so payee changes are not counted" in reasons


def test_low_delivery_is_unreliable_once_there_are_four():
    s, reasons = evaluate(good(judged_30d=judged(4, 1, partial=1)))
    assert s == "unreliable"
    assert reasons[0] == ("1 of 4 judged purchases in the last 30 days delivered (25%; 1 partial, counted as not "
                          "delivered; 2 not delivered; last 2026-11-19); below 50% once there are 4 or more")
    # 2 of 4 is 50%: not below, so not unreliable (and not reliable either).
    assert state(good(judged_30d=judged(4, 2))) == "unverified"
    # Three judged, none delivered: too few to call unreliable.
    assert state(good(judged_30d=judged(3, 0))) == "unverified"


# --- untracked, several endpoints, limits ------------------------------------------------


def test_untracked_is_unverified_unless_blocked():
    s, reasons = evaluate({"as_of": iso(NOW), "tracked": False, "endpoints": []})
    assert (s, reasons) == ("unverified", [reliability.NOT_TRACKED])
    s, reasons = evaluate({"as_of": iso(NOW), "tracked": False, "endpoints": [],
                           "blocked": {"note": "fixture", "at": iso(NOW)}})
    assert s == "unreliable" and reasons == ["blocked by a CrowdCode operator on 2026-11-20: fixture", reliability.NOT_TRACKED]


def test_several_endpoints_reliable_only_if_all_pass_and_down_only_if_all_down():
    down = [probe(0.1, ok=False), probe(1.1, ok=False), probe(2.1, ok=False)]
    second = endpoint(resource_id=2, url="https://api.example.com/contents", last_probes=down)
    s, reasons = evaluate(good(endpoints=[endpoint(), second]))
    assert s == "unverified"
    assert reasons[0].startswith("POST https://api.example.com/contents: the last 3 daily probes all failed")
    both = good(endpoints=[endpoint(last_probes=down), second])
    assert state(both) == "unreliable"


def test_reasons_are_bounded_strings_and_the_rule_is_deterministic():
    many = good(endpoints=[endpoint(resource_id=i, url=f"https://api.example.com/{i}", last_probes=[probe(0.1)])
                           for i in range(12)])
    s, reasons = evaluate(many)
    assert s == "unverified" and len(reasons) == reliability.MAX_REASONS
    assert all(isinstance(r, str) and len(r) < 300 for r in reasons)
    copy = deepcopy(many)
    assert evaluate(copy) == evaluate(many) and copy == many  # pure: inputs untouched


# --- PostgreSQL ------------------------------------------------------------------------


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = "reliability_test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"create schema {schema}")
        admin.execute(f"set search_path to {schema}")
        admin.execute((ROOT / "supabase/schema.sql").read_text())
        admin.execute((ROOT / "supabase/services-index.sql").read_text())
        admin.execute((ROOT / "supabase/observations-and-requests-v3.sql").read_text())
        admin.execute((ROOT / "supabase/rotating-payees.sql").read_text())
        # Idempotent: applying it twice changes nothing.
        admin.execute((ROOT / "supabase/reliability.sql").read_text())
        admin.execute((ROOT / "supabase/reliability.sql").read_text())

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, options=f"-c search_path={schema}") as conn:
            yield conn

    try:
        yield connect
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f"drop schema {schema} cascade")


def _seed(conn) -> None:
    conn.execute("""
        insert into services (id, name, canonical_endpoint, canonical_origin, payment_provider, payment_target_ref) values
          ('svc_exa', 'Exa search', 'https://api.exa.ai/search', 'https://api.exa.ai', 'x402', %(payee)s),
          ('svc_new', 'New', 'https://new.example.com/q', 'https://new.example.com', 'x402', %(payee)s),
          ('svc_hosted', 'Renamed hosted', 'https://crowdcode.app/s/new-name', 'https://crowdcode.app', 'x402', %(payee)s),
          ('svc_fixture', 'Blocked fixture', 'https://fixture.example.com/x', 'https://fixture.example.com', 'x402', %(payee)s)
    """, {"payee": PAYEE})
    conn.execute("""
        insert into service_identifiers (service_id, identifier_type, identifier_value) values
          ('svc_exa', 'payment_target', 'x402:' || %(other)s),
          ('svc_hosted', 'api_endpoint', 'https://crowdcode.app/s/old-name')
    """, {"other": OTHER})
    for rid, sid, url, tracked in ((1, "svc_exa", "https://api.exa.ai/search", True),
                                   (2, "svc_new", "https://new.example.com/q", True),
                                   (3, None, "https://quiet.example.com/q", False)):
        conn.execute(
            """
            insert into index_resources (id, resource_key, method, origin, route_template, probe_url, tracked,
              tracked_reason, service_id, quoted_price_atomic, quoted_asset, quoted_network, payees, payee_since,
              bazaar_payers_30d)
            values (%s, %s, 'POST', %s, %s, %s, %s, %s, %s, 7000, %s, 'eip155:8453', %s, %s, 12)
            """,
            (rid, f"POST {url}", url.rsplit("/", 1)[0], url, url, tracked, "gateway_purchase" if tracked else None,
             sid, USDC, [PAYEE, OTHER], NOW - timedelta(days=20)),
        )
        conn.execute("insert into index_resource_urls (source, method, url, resource_id) values ('bazaar', 'POST', %s, %s)",
                     (url, rid))
    for days in (0.2, 1.2):
        conn.execute(
            """
            insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, network,
              payee, quoted_amount_atomic, asset, pay_to)
            values (1, 'https://api.exa.ai/search', 'POST', %s, 'probe', true, 402, 'eip155:8453', %s, 7000, %s, %s)
            """,
            (NOW - timedelta(days=days), PAYEE, USDC, [PAYEE, OTHER]),
        )
    conn.execute("""
        insert into index_changes (resource_id, at, kind, old_value, new_value)
        values (1, %s, 'payee', %s, %s)
    """, (NOW - timedelta(days=1), PAYEE, f"{OTHER},{PAYEE}"))
    attestation = lambda delivery, traffic=None: json.dumps({"review": {"delivery": delivery, **({"traffic": traffic} if traffic else {})},
                                                             "receipt": {"endpoint": "https://api.exa.ai/search"}})
    for i, (delivery, traffic) in enumerate([("delivered", "test"), ("delivered", None), ("partial", None),
                                             ("delivered", "owner"), ("delivered", "owner")]):
        conn.execute(
            "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, owner_ref, created_at) "
            "values ('svc_exa', 5, 'ok', 'k', %s, %s, %s)",
            (attestation(delivery, traffic), f"owner{i % 2}", NOW - timedelta(days=i + 1)),
        )
    # Excluded as CrowdCode's own defect: not evidence about the provider.
    conn.execute(
        "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, weight_exclusion, created_at) "
        "values ('svc_exa', 1, 'ours', 'k', %s, 'platform_defect', %s)",
        (attestation("not_delivered"), NOW - timedelta(days=2)),
    )
    # Too old to count.
    conn.execute(
        "insert into reviews (service_id, rating, reason, receipt_key_id, gateway_attestation, created_at) "
        "values ('svc_exa', 1, 'old', 'k', %s, %s)",
        (attestation("not_delivered"), NOW - timedelta(days=40)),
    )


def test_recompute_stores_states_and_rewrites_only_what_changed(db):
    with db() as conn:
        _seed(conn)
        conn.commit()
    stats = reliability.recompute(db, NOW)
    assert stats == {"services": 2, "written": 2, "removed": 0, "reliable": 1, "unverified": 1, "unreliable": 0}
    with db() as conn:
        exa = reliability.for_service(conn, "svc_exa")
        assert exa["state"] == "reliable" and exa["rule_version"] == "reliability-v1"
        assert exa["checked_at"] == NOW.isoformat()
        judged = exa["inputs"]["judged_30d"]
        assert (judged["total"], judged["delivered"], judged["partial"], judged["test"], judged["unlabelled"]) == (5, 4, 1, 1, 2)
        assert ("4 of 5 judged purchases in the last 30 days delivered (80%; 1 partial, counted as not delivered; "
                "1 of them CrowdCode test purchases; 2 from before test purchases were labelled; last 2026-11-19)") in exa["reasons"]
        assert any("only to addresses the service already advertised" in r for r in exa["reasons"])
        assert exa["inputs"]["endpoints"][0]["median_price_7d_atomic"] == "7000"
        # Anyone can recompute the state from what is published.
        assert evaluate(exa["inputs"]) == (exa["state"], exa["reasons"])
        new = reliability.for_service(conn, "svc_new")
        assert new["state"] == "unverified" and new["reasons"][0].startswith("not probed yet")
        untracked = reliability.for_service(conn, "svc_hosted")
        assert untracked["state"] == "unverified" and untracked["reasons"] == [reliability.NOT_TRACKED]
        assert reliability.for_service(conn, "svc_nope") is None
        listed = reliability.by_state(conn, "reliable")
        assert listed["counts"] == {"reliable": 1, "unverified": 1, "unreliable": 0}
        assert [s["service_id"] for s in listed["services"]] == ["svc_exa"] and listed["services"][0]["name"] == "Exa search"
        first = conn.execute("select computed_at from service_reliability where service_id = 'svc_exa'").fetchone()
    # An hour later nothing changed: nothing is rewritten, and computed_at stays.
    assert reliability.recompute(db, NOW + timedelta(hours=1))["written"] == 0
    with db() as conn:
        again = conn.execute("select computed_at from service_reliability where service_id = 'svc_exa'").fetchone()
        assert again["computed_at"] == first["computed_at"]
        # A third probe fails: the inputs change, so the row does.
        conn.execute(
            "insert into index_observations (resource_id, endpoint, method, at, kind, ok, http_status, error_code) "
            "values (1, 'https://api.exa.ai/search', 'POST', %s, 'probe', false, 503, 'http_503')",
            (NOW + timedelta(hours=1),),
        )
        conn.commit()
    assert reliability.recompute(db, NOW + timedelta(hours=2))["written"] == 1
    with db() as conn:
        exa = reliability.for_service(conn, "svc_exa")
        assert exa["state"] == "reliable"
        assert exa["reasons"][0] == "valid 402 in 2 of the last 3 daily probes (latest 2026-11-20 13:00 UTC)"


def test_url_lookup_resolves_aliases_and_unknown_urls(db):
    with db() as conn:
        _seed(conn)
        conn.commit()
    reliability.recompute(db, NOW)
    with db() as conn:
        exa = reliability.for_url(conn, "https://api.exa.ai/search/", "POST")
        assert exa["state"] == "reliable" and exa["service_id"] == "svc_exa"
        assert exa["resolved"]["resource_id"] == 1
        # An old URL of a renamed hosted service, through the registry's aliases.
        alias = reliability.for_url(conn, "https://crowdcode.app/s/old-name", "POST")
        assert alias["service_id"] == "svc_hosted" and alias["reasons"] == [reliability.NOT_TRACKED]
        unknown = reliability.for_url(conn, "https://nobody.example.org/thing", "POST")
        assert unknown["state"] == "unverified" and unknown["service_id"] is None
        assert unknown["reasons"] == ["not in CrowdCode's index"]
        quiet = reliability.for_url(conn, "https://quiet.example.com/q", "POST")
        assert quiet["service_id"] is None and quiet["reasons"] == [reliability.NOT_TRACKED]
        assert reliability.for_url(conn, "not a url at all", None)["reasons"] == ["not in CrowdCode's index"]


def test_blocking_forces_unreliable_with_the_note_and_is_audited(db, monkeypatch, capsys):
    with db() as conn:
        _seed(conn)
        conn.commit()
    reliability.recompute(db, NOW)
    import scripts.block_service as script
    monkeypatch.setattr(script, "connect", db)
    note = "CrowdCode reliability fixture: blocked on purpose"
    assert script.main(["--block", "svc_exa", "--note", note, "--by", "w4-test"]) == 0
    assert script.main(["--block", "svc_fixture", "--note", note, "--by", "w4-test"]) == 0
    assert script.main(["--block", "svc_missing", "--note", note, "--by", "w4-test"]) == 2
    assert script.main(["--block", "svc_exa", "--note", "x", "--by", "w4-test"]) == 2
    with db() as conn:
        exa = reliability.for_service(conn, "svc_exa")
        assert exa["state"] == "unreliable" and exa["reasons"][0].endswith(note)
        fixture = reliability.for_url(conn, "https://fixture.example.com/x", "POST")
        assert fixture["state"] == "unreliable" and fixture["service_id"] == "svc_fixture"
        assert fixture["reasons"] == [f"blocked by a CrowdCode operator on {datetime.now(UTC):%Y-%m-%d}: {note}",
                                      reliability.NOT_TRACKED]
    assert script.main(["--unblock", "svc_exa", "--note", "fixture done", "--by", "w4-test"]) == 0
    assert script.main(["--unblock", "svc_exa", "--note", "fixture done", "--by", "w4-test"]) == 1
    assert script.main(["--unblock", "svc_fixture", "--note", "fixture done", "--by", "w4-test"]) == 0
    with db() as conn:
        assert reliability.for_service(conn, "svc_exa")["state"] != "unreliable"
        assert reliability.for_service(conn, "svc_fixture")["reasons"] == [reliability.NOT_TRACKED]
        assert conn.execute("select count(*)::int as n from service_reliability where service_id = 'svc_fixture'").fetchone()["n"] == 0
        audit = conn.execute("select service_id, action, actor from service_block_audit order by id").fetchall()
        assert [(a["service_id"], a["action"]) for a in audit] == [
            ("svc_exa", "block"), ("svc_fixture", "block"), ("svc_exa", "unblock"), ("svc_fixture", "unblock")]


def test_the_cron_runs_the_rule_after_the_probes():
    from crowdcode import cron
    names = [job.__name__ for job in cron.jobs()]
    assert names.index("run_reliability") == names.index("run_index_probes") + 1
