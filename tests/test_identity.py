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

