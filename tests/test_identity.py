from crowdcode import identity as identity_mod
from crowdcode.identity import build_identity, resolve_service


SERVICE = {
    "id": "svc_victim",
    "name": "Victim service",
    "directory_slug": "victim",
    "canonical_origin": "https://api.victim.example",
    "canonical_endpoint": "https://api.victim.example/v1",
    "payment_provider": "x402",
    "payment_target_ref": "0x" + "11" * 20,
    "created_from_review": False,
}


def _stub_lookups(monkeypatch, identifiers=None):
    identifiers = identifiers or {}
    monkeypatch.setattr(
        identity_mod,
        "_fetch_service",
        lambda _conn, service_id: SERVICE if service_id == SERVICE["id"] else None,
    )
    monkeypatch.setattr(
        identity_mod,
        "_fetch_by_directory_slug",
        lambda _conn, slug: SERVICE if slug == SERVICE["directory_slug"] else None,
    )
    monkeypatch.setattr(
        identity_mod,
        "_fetch_by_identifier",
        lambda _conn, kind, value: identifiers.get((kind, value)),
    )

    def machine_payment_targets(_conn, target):
        rows = []
        for (kind, value), service in identifiers.items():
            if kind != "payment_target":
                continue
            provider, _, registered = value.partition(":")
            if (
                provider in identity_mod.MACHINE_PAYMENT_PROVIDERS
                and registered.lower() == target.lower()
                and service not in rows
            ):
                rows.append(service)
        return rows

    monkeypatch.setattr(
        identity_mod,
        "_fetch_all_by_machine_payment_target",
        machine_payment_targets,
    )


def test_service_id_cannot_override_payment_destination(monkeypatch):
    _stub_lookups(monkeypatch)
    supplied = build_identity(
        service_id=SERVICE["id"],
        api_endpoint=SERVICE["canonical_endpoint"],
        payment_provider="mppx",
        payment_target_ref="0x" + "99" * 20,
    )

    resolved = resolve_service(object(), supplied)

    assert resolved.row is None
    assert resolved.error == "service identity conflict"


def test_service_id_only_resolves_to_canonical_identity(monkeypatch):
    _stub_lookups(monkeypatch)

    resolved = resolve_service(
        object(),
        build_identity(service_id=SERVICE["id"]),
    )

    assert resolved.error is None
    assert resolved.row == SERVICE
    assert resolved.identity is not None
    assert resolved.identity.api_endpoint == SERVICE["canonical_endpoint"]
    assert resolved.identity.payment_provider == SERVICE["payment_provider"]
    assert resolved.identity.payment_target_ref == SERVICE["payment_target_ref"]


def test_registered_alternate_payment_rail_remains_valid(monkeypatch):
    alias_target = "0x" + "22" * 20
    alias = ("payment_target", f"mppx:{alias_target}")
    _stub_lookups(monkeypatch, {alias: SERVICE})
    supplied = build_identity(
        service_id=SERVICE["id"],
        payment_provider="mppx",
        payment_target_ref=alias_target,
    )

    resolved = resolve_service(object(), supplied)

    assert resolved.error is None
    assert resolved.row == SERVICE
    assert resolved.identity is not None
    assert resolved.identity.payment_provider == "mppx"
    assert resolved.identity.payment_target_ref == alias_target


def test_service_id_and_identifier_for_different_services_conflict(monkeypatch):
    other = {**SERVICE, "id": "svc_other", "directory_slug": "other"}
    _stub_lookups(
        monkeypatch,
        {("directory_slug", "other"): other},
    )
    monkeypatch.setattr(
        identity_mod,
        "_fetch_by_directory_slug",
        lambda _conn, slug: other if slug == "other" else None,
    )

    resolved = resolve_service(
        object(),
        build_identity(service_id=SERVICE["id"], directory_slug="other"),
    )

    assert resolved.row is None
    assert resolved.error == "service identity conflict"



# Field note 001 regression: one endpoint, one payee wallet, two machine
# payment rails. A service registered under mppx/Tempo must accept an
# x402/Base identity for the same payee (and vice versa) — the payee
# address, not the protocol label, anchors payment identity.

MPPX_SERVICE = {
    "id": "svc_46e680f0839fdbe8020a",
    "name": "StableEnrich Exa Contents",
    "directory_slug": None,
    "canonical_origin": "https://stableenrich.dev",
    "canonical_endpoint": "https://stableenrich.dev/api/exa/contents",
    "payment_provider": "mppx",
    "payment_target_ref": "0x325bdF6F7efAB24a2210c48c1b64cAb2eAe1d430",
    "created_from_review": True,
}


def test_endpoint_accepts_both_machine_rails_to_same_payee(monkeypatch):
    _stub_lookups(
        monkeypatch,
        {
            ("api_endpoint", MPPX_SERVICE["canonical_endpoint"]): MPPX_SERVICE,
            (
                "payment_target",
                f"mppx:{MPPX_SERVICE['payment_target_ref']}",
            ): MPPX_SERVICE,
        },
    )

    x402_payee = MPPX_SERVICE["payment_target_ref"].lower()
    for provider, target in [
        ("mppx", MPPX_SERVICE["payment_target_ref"]),
        ("x402", x402_payee),
    ]:
        resolved = resolve_service(
            object(),
            build_identity(
                api_endpoint=MPPX_SERVICE["canonical_endpoint"],
                payment_provider=provider,
                payment_target_ref=target,
            ),
        )
        assert resolved.error is None, (provider, resolved.error)
        assert resolved.row == MPPX_SERVICE
        assert resolved.identity is not None
        assert resolved.identity.payment_provider == provider
        assert resolved.identity.payment_target_ref == target


def test_machine_rail_pair_only_lookup_resolves_across_protocols(monkeypatch):
    _stub_lookups(
        monkeypatch,
        {
            (
                "payment_target",
                f"mppx:{MPPX_SERVICE['payment_target_ref']}",
            ): MPPX_SERVICE,
        },
    )

    resolved = resolve_service(
        object(),
        build_identity(
            payment_provider="x402",
            payment_target_ref=MPPX_SERVICE["payment_target_ref"].lower(),
        ),
    )

    assert resolved.error is None
    assert resolved.row == MPPX_SERVICE
    assert resolved.identity is not None
    assert resolved.identity.payment_provider == "x402"


def test_other_machine_rail_with_different_payee_still_conflicts(monkeypatch):
    _stub_lookups(
        monkeypatch,
        {("api_endpoint", MPPX_SERVICE["canonical_endpoint"]): MPPX_SERVICE},
    )

    resolved = resolve_service(
        object(),
        build_identity(
            api_endpoint=MPPX_SERVICE["canonical_endpoint"],
            payment_provider="x402",
            payment_target_ref="0x" + "99" * 20,
        ),
    )

    assert resolved.row is None
    assert resolved.error == "service identity conflict"


def test_provider_only_machine_variant_keeps_registered_payee(monkeypatch):
    _stub_lookups(monkeypatch)

    resolved = resolve_service(
        object(),
        build_identity(service_id=SERVICE["id"], payment_provider="mppx"),
    )

    assert resolved.error is None
    assert resolved.identity is not None
    assert resolved.identity.payment_provider == "mppx"
    assert resolved.identity.payment_target_ref == SERVICE["payment_target_ref"]


def test_provider_only_non_machine_variant_still_conflicts(monkeypatch):
    _stub_lookups(monkeypatch)

    resolved = resolve_service(
        object(),
        build_identity(service_id=SERVICE["id"], payment_provider="stripe"),
    )

    assert resolved.row is None
    assert resolved.error == "service identity conflict"


class _RecordingConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return self


def test_register_machine_payment_alias_inserts_identifier():
    conn = _RecordingConn()
    identity_mod.register_machine_payment_alias(
        conn,
        MPPX_SERVICE["id"],
        build_identity(
            api_endpoint=MPPX_SERVICE["canonical_endpoint"],
            payment_provider="x402",
            payment_target_ref=MPPX_SERVICE["payment_target_ref"],
        ),
    )
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "insert into service_identifiers" in sql
    assert params == (
        MPPX_SERVICE["id"],
        "payment_target",
        f"x402:{MPPX_SERVICE['payment_target_ref']}",
    )


def test_register_machine_payment_alias_ignores_non_machine_pairs():
    conn = _RecordingConn()
    identity_mod.register_machine_payment_alias(
        conn,
        SERVICE["id"],
        build_identity(
            api_endpoint=SERVICE["canonical_endpoint"],
            payment_provider="stripe",
            payment_target_ref="acct_123",
        ),
    )
    identity_mod.register_machine_payment_alias(
        conn,
        SERVICE["id"],
        build_identity(
            api_endpoint=SERVICE["canonical_endpoint"],
            payment_provider="x402",
            payment_target_ref="not-an-address",
        ),
    )
    assert conn.calls == []
