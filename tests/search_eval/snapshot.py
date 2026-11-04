"""The directory and catalogs as they were, so CI can rank the evaluation set offline.

`snapshot.json` holds every listing (name, slug, endpoint, description and
listing text), CrowdCode's evidence for every endpoint the searches return,
the networks each service was seen on, the mpp.dev catalog, and the Coinbase
Bazaar's answer to each case's query. `offline_search()` runs the real
`find_services` over it with the real model, so CI scores exactly the ranking
production would serve for that directory. Everything in it is public: it is
what `/api/services` and the two catalogs publish.

Rebuild it with `python scripts/search_eval.py --snapshot` (DATABASE_URL; the
catalogs are fetched live) when the directory has moved on, and say in the
pull request how recall moved.
"""

from __future__ import annotations

import gzip
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

SNAPSHOT_PATH = Path(__file__).with_name("snapshot.json.gz")


def _compact_cdp(payload: Any) -> dict[str, Any]:
    resources = []
    for resource in (payload or {}).get("resources") or (payload or {}).get("items") or []:
        bazaar = (resource.get("extensions") or {}).get("bazaar") or {}
        info = bazaar.get("info") or {}
        resources.append({
            "resource": resource.get("resource"),
            "description": resource.get("description"),
            "extensions": {"bazaar": {"name": bazaar.get("name"), "description": bazaar.get("description"),
                                      "info": {"input": {"method": (info.get("input") or {}).get("method")}}}},
            "accepts": [{k: a.get(k) for k in ("network", "asset", "scheme", "amount", "maxAmountRequired", "extra")}
                        for a in resource.get("accepts") or [] if isinstance(a, dict)],
            "quality": {"l30DaysUniquePayers": (resource.get("quality") or {}).get("l30DaysUniquePayers")},
        })
    return {"resources": resources}


def _compact_mpp(catalog: Any) -> dict[str, Any]:
    keep = ("name", "serviceUrl", "description", "status", "tags", "categories")
    services = []
    for service in (catalog or {}).get("services") or []:
        item = {k: service.get(k) for k in keep}
        item["endpoints"] = [{
            "method": e.get("method"), "path": e.get("path"), "description": e.get("description"),
            "payment": {k: (e.get("payment") or {}).get(k) for k in ("method", "intent", "currency", "decimals", "amount")},
        } for e in service.get("endpoints") or [] if isinstance(e, dict)]
        services.append(item)
    return {"services": services}


def build_snapshot(path: Path = SNAPSHOT_PATH, overlay: Path | None = None, keep_catalogs: bool = False) -> dict[str, int]:
    """Read the live directory (DATABASE_URL) and catalogs into `snapshot.json.gz`.

    ``overlay``: a `fill_listing_descriptions.py --report` file whose
    descriptions are used where the directory has none yet (a dry run).
    ``keep_catalogs``: keep the snapshot's catalog answers and refresh only
    the directory, so a comparison changes one thing at a time.
    """
    from crowdcode import discovery
    from crowdcode.db import connect
    from crowdcode.networks import fetch_service_networks
    from crowdcode.search_eval import load_cases

    cases = load_cases()
    catalogs = discovery.CatalogClient()
    previous = load_snapshot(path) if keep_catalogs and path.exists() else {"cdp": {}}
    cdp = {}
    for case in cases:
        kept = previous["cdp"].get(case["query"])
        cdp[case["query"]] = kept if kept is not None else _compact_cdp(catalogs.cdp_search(case["query"]))
    mpp = previous.get("mpp") or _compact_mpp(catalogs.mpp_catalog())
    with connect() as conn:
        rows = [dict(r) for r in discovery.directory_rows(conn)]
        networks = fetch_service_networks(conn)
    extra = {}
    if overlay:
        for line in Path(overlay).read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            extra[item["service_id"]] = item
    for row in rows:
        found = extra.get(row["id"])
        if found and not row.get("description"):
            row["description"] = found.get("description")
            row["listing_text"] = found.get("listing_text")
    endpoints = {e for r in rows if (e := discovery._endpoint(r.get("canonical_endpoint")))}
    for payload in cdp.values():
        endpoints |= {c.endpoint for c in discovery.cdp_candidates(payload, discovery.DEFAULT_NETWORKS)}
    for service in mpp["services"]:
        for item in service["endpoints"]:
            if (url := discovery._endpoint(str(service.get("serviceUrl") or "").rstrip("/") + str(item.get("path") or ""))):
                endpoints.add(url)
    with connect() as conn:
        evidence = {k: {f: (float(v) if f in ("score", "n_eff") and v is not None else v) for f, v in row.items()}
                    for k, row in discovery.evidence_by_endpoint(conn, sorted(endpoints)).items()}
    snapshot = {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "directory": [{k: row.get(k) for k in ("id", "name", "directory_slug", "canonical_endpoint", "payment_provider",
                                                "description", "listing_text", "review_pros")} for row in rows],
        "networks": {k: {"networks": v.get("networks", [])} for k, v in networks.items()},
        "evidence": evidence,
        "mpp": mpp,
        "cdp": cdp,
    }
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(snapshot, handle, sort_keys=True, separators=(",", ":"))
    return {"listings": len(rows), "evidence": len(evidence), "cdp_queries": len(cdp),
            "mpp_services": len(mpp["services"]), "bytes": path.stat().st_size}


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class _Conn:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> "_Conn":
        if sql.strip().startswith("select id, name"):
            self.rows = [dict(r) for r in self.snapshot["directory"]]
        elif "with matched as" in sql:
            wanted = set(params[0])
            self.rows = [{"endpoint": e, **v} for e, v in self.snapshot["evidence"].items() if e in wanted]
        else:
            self.rows = []
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _Catalogs:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = snapshot

    def cdp_search(self, query: str) -> Any:
        return self.snapshot["cdp"].get(query, {"resources": []})

    def mpp_catalog(self) -> Any:
        return self.snapshot["mpp"]


def offline_search(snapshot: dict[str, Any] | None = None, embedder: Any = "default",
                   **overrides: Any) -> Callable[[str], list[str]]:
    """``search(query) -> ranked endpoints`` over the snapshot.

    ``embedder=None`` ranks by keywords alone (the model unavailable);
    ``overrides`` patch discovery constants, for tuning experiments.
    """
    from crowdcode import discovery, semantic

    snapshot = snapshot or load_snapshot()
    if embedder == "default":
        semantic.EMBEDDER.warm(wait=True)
        embedder = semantic.EMBEDDER
    networks = snapshot["networks"]

    @contextmanager
    def factory():
        yield _Conn(snapshot)

    def search(query: str) -> list[str]:
        patches = [mock.patch.object(discovery, name, value) for name, value in overrides.items()]
        patches.append(mock.patch.object(discovery, "_service_networks", lambda conn: networks))
        for patch in patches:
            patch.start()
        try:
            result = discovery.find_services(query, limit=10, conn_factory=factory, catalogs=_Catalogs(snapshot),
                                             embedder=embedder if embedder is not None else _NoModel())
        finally:
            for patch in reversed(patches):
                patch.stop()
        return [s["endpoint"] for s in result["services"]]

    return search


class _NoModel:
    """An embedder whose model never loads: keyword ranking only."""

    def similarities(self, query: str, documents: Any) -> None:
        return None
