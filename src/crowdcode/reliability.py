"""reliability-v1: one state per tracked service, with its reasons (SRI-115 C3).

The rule, as the parent repository's design-docs/RELIABLE_SERVICES_BUILD.md
(contract C3) states it:

=============  =============================================================
``unreliable``  blocked by an operator; or the last 3 probes all failed; or
                the payee changed in the last 7 days to one the service's own
                402 had not advertised; or >= 4 judged purchases in 30 days
                with < 50% delivered
``reliable``    not unreliable; and a valid 402 in >= 2 of the last 3
                probes; and the latest price <= 2x the 7-day median; and
                >= 3 judged purchases in 30 days with >= 80% delivered
``unverified``  everything else, including services the index does not track
=============  =============================================================

Every threshold is a constant below, and nothing else in the codebase
restates one. :func:`evaluate` is pure: it reads only the ``inputs`` document
that is stored and published beside each state, so anyone can recompute a
state from what ``GET /api/services/{id}/reliability`` returns.

How the evidence is read:

- **Probes** are the index's daily unpaid requests (``index_resource_signals``
  ``last_probes``). A probe is *valid* when it got a payment challenge. A probe
  whose request body the seller refused (``probe_body_rejected``) is
  inconclusive: neither a failure nor a valid 402.
- **Liveness from paid calls:** a successful paid call (a gateway purchase or
  a crowdcode-mcp observation, C5) at or after the oldest of three failed
  probes means the service answered a buyer, so it is not counted as down.
  Paid calls never stand in for a valid 402 and never count as delivery.
- **Payee changes** come from ``index_changes``. Only addresses that appear in
  a change count, and only if the service had not advertised them: neither
  in the 402 answer before the change nor among the payees CrowdCode has
  registered for it before the change (learned from its own 402,
  ``crowdcode.advertised``).
  Services an operator marked ``payee_rotates`` issue a fresh payee per
  request, so their payee changes are not counted.
- **Judged purchases** are reviews carrying a valid gateway attestation, from
  any owner, whose attestation names the judge's ``delivery``. ``partial``
  counts as not delivered. CrowdCode's own test purchases count and are
  labelled (the attestation's ``traffic``); attestations issued before
  ``traffic`` existed are reported as unlabelled. Reviews excluded as a
  CrowdCode platform defect are not evidence about the provider and are not
  counted.
- **A service with several tracked endpoints** is reliable only when every
  endpoint passes the probe and price conditions, and is down only when every
  endpoint is down. A payee change on any endpoint counts.

Results are stored in ``service_reliability`` (supabase/reliability.sql) by
the hourly cron, after the probe sweep (cron.jobs), and for one service right
after it receives a judged review.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

import psycopg
from psycopg.types.json import Jsonb

RULE_VERSION = "reliability-v1"
STATES = ("reliable", "unverified", "unreliable")

# --- The rule's constants: the only copy -------------------------------------------
PROBE_WINDOW = 3  # the probes the rule reads, newest first
DOWN_AFTER_FAILED = 3  # unreliable: this many failed probes in a row (the whole window)
RELIABLE_VALID_PROBES = 2  # reliable: at least this many valid 402s in the window
INCONCLUSIVE_PROBE_ERRORS = frozenset({"probe_body_rejected"})
PAYEE_WINDOW = timedelta(days=7)
PRICE_MAX_MULTIPLE = Decimal(2)
JUDGED_WINDOW = timedelta(days=30)
UNRELIABLE_MIN_JUDGED = 4
UNRELIABLE_DELIVERED_BELOW = Decimal("0.5")
RELIABLE_MIN_JUDGED = 3
RELIABLE_DELIVERED_AT_LEAST = Decimal("0.8")
PAYERS_WINDOW = timedelta(days=30)
# What the gateway shows an owner (C4 caps reasons at 8).
MAX_REASONS = 8

NOT_INDEXED = "not in CrowdCode's index"
NOT_TRACKED = "not tracked by CrowdCode's index, so CrowdCode has not checked it"

# USDC, the only asset hosted agents hold (index_feeds.USDC), shown in dollars.
_USDC = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "epjfwdd5aufqssqem2qn1xzybapc8g4wewggkzwytdt1v",
    "0x20c000000000000000000000b9537d11c60e8b50",
}
_DELIVERY = ("delivered", "partial", "not_delivered")
STATUS_CACHE_KEY = "reliability_status"


# --- Formatting ----------------------------------------------------------------------


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _when(value: Any) -> str:
    at = _ts(value)
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if at else "unknown"


def _day(value: Any) -> str:
    at = _ts(value)
    return at.astimezone(UTC).strftime("%Y-%m-%d") if at else "unknown"


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _price(amount: Decimal, asset: Any) -> str:
    if isinstance(asset, str) and asset.lower() in _USDC:
        dollars = amount / Decimal(10**6)
        text = format(dollars.normalize(), "f")
        if "." not in text:
            text += ".00"
        elif len(text.split(".")[1]) < 2:
            text += "0"
        return f"${text}"
    return f"{format(amount.normalize(), 'f')} base units of {asset or 'an unknown asset'}"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _norm_payee(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if ":" in value and not value.lower().startswith("0x"):
        value = value.split(":", 1)[1]  # service_identifiers' "x402:0x…" form
    return value.lower() if value.lower().startswith("0x") else value


def _payees(values: Any) -> set[str]:
    if isinstance(values, str):
        values = values.split(",")
    return {p for p in (_norm_payee(v) for v in values or []) if p}


# --- The rule ------------------------------------------------------------------------


def evaluate(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    """reliability-v1 over one service's ``inputs``: ``(state, reasons)``.

    Reasons come in order: what decided the state first, then the conditions
    that held, then context (payers). Each is a sentence a reader can check
    against ``inputs``.
    """
    as_of = _ts(inputs.get("as_of")) or datetime.now(UTC)
    endpoints = list(inputs.get("endpoints") or [])
    multi = len(endpoints) > 1
    triggers: list[str] = []  # unreliable
    unmet: list[str] = []  # a reliable condition that does not hold
    met: list[str] = []
    context: list[str] = []

    blocked = inputs.get("blocked")
    if blocked:
        triggers.append(f"blocked by a CrowdCode operator on {_day(blocked.get('at'))}: {blocked.get('note')}")
    if not inputs.get("tracked"):
        state = "unreliable" if triggers else "unverified"
        return state, [*triggers, NOT_TRACKED][:MAX_REASONS]

    # Probes: liveness and a valid 402.
    down: list[str] = []
    for ep in endpoints:
        label = f"{ep.get('method')} {ep.get('url')}: " if multi else ""
        probes = list(ep.get("last_probes") or [])[:PROBE_WINDOW]
        valid = sum(1 for p in probes if p.get("ok"))
        failed = [p for p in probes if not p.get("ok") and p.get("error_code") not in INCONCLUSIVE_PROBE_ERRORS]
        inconclusive = len(probes) - valid - len(failed)
        latest = _when(probes[0].get("at")) if probes else None
        note = (f"; {_plural(inconclusive, 'probe')} inconclusive (the seller refused the request body CrowdCode built)"
                if inconclusive else "")
        if not probes:
            unmet.append(f"{label}not probed yet; reliable needs a valid 402 in "
                         f"{RELIABLE_VALID_PROBES} of the last {PROBE_WINDOW} daily probes")
        elif len(failed) >= DOWN_AFTER_FAILED:
            codes = ", ".join(str(p.get("error_code") or p.get("http_status") or "failed") for p in failed)
            oldest = _ts(failed[-1].get("at"))
            paid = _ts(ep.get("last_paid_ok_at"))
            if paid is not None and oldest is not None and paid >= oldest:
                unmet.append(f"{label}the last {len(failed)} daily probes failed ({codes}; latest {latest}), "
                             f"but a paid call succeeded on {_when(paid)}, so it is not counted as down")
            else:
                down.append(f"{label}the last {len(failed)} daily probes all failed ({codes}; latest {latest})")
        elif valid >= RELIABLE_VALID_PROBES:
            met.append(f"{label}valid 402 in {valid} of the last {len(probes)} daily probes (latest {latest}){note}")
        else:
            unmet.append(f"{label}valid 402 in {valid} of the last {_plural(len(probes), 'daily probe')} "
                         f"(latest {latest}); reliable needs {RELIABLE_VALID_PROBES} of the last {PROBE_WINDOW}{note}")

        # Price: only meaningful once the endpoint has quoted one.
        if valid:
            price = _dec(ep.get("latest_price_atomic"))
            median = _dec(ep.get("median_price_7d_atomic"))
            asset = ep.get("latest_price_asset")
            if price is None or median is None:
                unmet.append(f"{label}no price quoted in a valid probe in the last 7 days")
            elif price <= PRICE_MAX_MULTIPLE * median:
                met.append(f"{label}latest price {_price(price, asset)} is within {PRICE_MAX_MULTIPLE}× "
                           f"its 7-day median of {_price(median, asset)}")
            else:
                unmet.append(f"{label}latest price {_price(price, asset)} is more than {PRICE_MAX_MULTIPLE}× "
                             f"its 7-day median of {_price(median, asset)}")
    if down and len(down) == len(endpoints):
        triggers.extend(down)
    else:
        unmet.extend(down)
    if not endpoints:
        unmet.append("no tracked endpoint to probe")

    # Payee changes in the last 7 days.
    registered = [(_norm_payee(r.get("payee")), _ts(r.get("since"))) for r in inputs.get("registered_payees") or []
                  if isinstance(r, dict)]
    rotates = bool(inputs.get("payee_rotates"))
    if rotates:
        context.append("the payee rotates per request (confirmed by an operator), so payee changes are not counted")
    else:
        for ep in endpoints:
            label = f"{ep.get('method')} {ep.get('url')}: " if multi else ""
            changes = [c for c in ep.get("payee_changes_7d") or []
                       if (_ts(c.get("at")) or as_of) > as_of - PAYEE_WINDOW]
            counted = False
            for change in changes:
                at = _ts(change.get("at")) or as_of
                known = {p for p, since in registered if p and (since is None or since <= at)}
                new = _payees(change.get("to")) - _payees(change.get("from"))
                unadvertised = sorted(new - known)
                if unadvertised:
                    counted = True
                    triggers.append(f"{label}payee changed on {_when(change.get('at'))} to "
                                    f"{', '.join(unadvertised)}, which the service's own 402 had not advertised before")
            if counted:
                continue
            if changes:
                met.append(f"{label}payee changed on {_when(changes[0].get('at'))} only to addresses the service "
                           "already advertised; not counted as a change")
            elif ep.get("payees"):
                since = ep.get("payee_since")
                met.append(f"{label}payee unchanged in the last 7 days" + (f" (current since {_day(since)})" if since else ""))

    # Judged purchases.
    judged = inputs.get("judged_30d") or {}
    total = int(judged.get("total") or 0)
    delivered = int(judged.get("delivered") or 0)
    partial = int(judged.get("partial") or 0)
    not_delivered = int(judged.get("not_delivered") or 0)
    if total == 0:
        unmet.append(f"no judged purchases in the last 30 days; reliable needs at least {RELIABLE_MIN_JUDGED}, "
                     f"with {RELIABLE_DELIVERED_AT_LEAST:.0%} delivered")
    else:
        rate = Decimal(delivered) / Decimal(total)
        details = []
        if partial:
            details.append(f"{partial} partial, counted as not delivered")
        if not_delivered:
            details.append(f"{not_delivered} not delivered")
        test = int(judged.get("test") or 0)
        unlabelled = int(judged.get("unlabelled") or 0)
        if test:
            details.append(f"{test} of them CrowdCode test purchases")
        if unlabelled:
            details.append(f"{unlabelled} from before test purchases were labelled")
        if judged.get("last_at"):
            details.append(f"last {_day(judged['last_at'])}")
        sentence = (f"{delivered} of {_plural(total, 'judged purchase')} in the last 30 days delivered "
                    f"({rate:.0%}{'; ' + '; '.join(details) if details else ''})")
        if total >= UNRELIABLE_MIN_JUDGED and rate < UNRELIABLE_DELIVERED_BELOW:
            triggers.append(f"{sentence}; below {UNRELIABLE_DELIVERED_BELOW:.0%} once there are "
                            f"{UNRELIABLE_MIN_JUDGED} or more")
        elif total >= RELIABLE_MIN_JUDGED and rate >= RELIABLE_DELIVERED_AT_LEAST:
            met.append(sentence)
        elif total < RELIABLE_MIN_JUDGED:
            unmet.append(f"{sentence}; reliable needs at least {RELIABLE_MIN_JUDGED}")
        else:
            unmet.append(f"{sentence}; reliable needs {RELIABLE_DELIVERED_AT_LEAST:.0%} delivered")

    payers = []
    if inputs.get("bazaar_payers_30d"):
        payers.append(f"{inputs['bazaar_payers_30d']} on the x402 Bazaar")
    if inputs.get("external_payers_30d"):
        payers.append(f"{inputs['external_payers_30d']} through crowdcode-mcp")
    if judged.get("owner_buyers"):
        payers.append(f"{_plural(int(judged['owner_buyers']), 'hosted-agent owner')} in judged purchases")
    if payers:
        context.append("payers in the last 30 days: " + ", ".join(payers))

    if triggers:
        return "unreliable", [*triggers, *unmet, *met, *context][:MAX_REASONS]
    if not unmet:
        return "reliable", [*met, *context][:MAX_REASONS]
    return "unverified", [*unmet, *met, *context][:MAX_REASONS]


# --- The evidence --------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, float):
        return _jsonable(Decimal(str(value)))
    return value


def _probe(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in ("at", "ok", "http_status", "error_code", "quoted_amount_atomic", "asset")}


def gather(conn: psycopg.Connection, as_of: datetime, service_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every input reliability-v1 reads, per service: the tracked services plus
    any blocked one (``service_ids`` limits it to those services)."""
    ids = None if service_ids is None else sorted(set(service_ids))
    only = "" if ids is None else "and service_id = any(%(ids)s)"
    params = {"ids": ids, "payee_since": as_of - PAYEE_WINDOW, "judged_since": as_of - JUDGED_WINDOW,
              "payers_since": as_of - PAYERS_WINDOW}

    signals = conn.execute(
        f"""
        select resource_id, service_id, method, probe_url, last_probes, latest_price_atomic, latest_price_asset,
               median_price_7d_atomic, payees, payee_since, bazaar_payers_30d
        from index_resource_signals
        where tracked and service_id is not null {only}
        order by service_id, resource_id
        """,
        params,
    ).fetchall()
    blocks = {r["service_id"]: r for r in conn.execute(
        f"select service_id, note, blocked_by, blocked_at from service_blocks where true {only}", params).fetchall()}
    services = sorted({r["service_id"] for r in signals} | set(blocks))
    if not services:
        return {}
    params["services"] = services
    resources = [r["resource_id"] for r in signals]
    params["resources"] = resources

    flags = {r["id"]: r for r in conn.execute(
        "select id, payee_rotates, payment_target_ref, created_at from services where id = any(%(services)s)",
        params).fetchall()}
    # Each registered payee with when CrowdCode first knew it: only a payee known
    # before a change can excuse that change.
    registered: dict[str, dict[str, datetime]] = {}

    def remember(sid: str, value: Any, since: datetime) -> None:
        for payee in _payees([value]):
            known = registered.setdefault(sid, {})
            known[payee] = min(known.get(payee, since), since)

    for sid, row in flags.items():
        remember(sid, row.get("payment_target_ref"), row["created_at"])
    for row in conn.execute(
        """
        select service_id, identifier_value, created_at from service_identifiers
        where identifier_type = 'payment_target' and service_id = any(%(services)s)
        """,
        params,
    ).fetchall():
        remember(row["service_id"], row["identifier_value"], row["created_at"])

    paid = {r["resource_id"]: r["at"] for r in conn.execute(
        """
        select resource_id, max(at) as at from index_observations
        where kind in ('purchase', 'observation') and ok and resource_id = any(%(resources)s)
        group by 1
        """,
        params,
    ).fetchall()}
    changes: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        """
        select resource_id, at, old_value, new_value from index_changes
        where kind = 'payee' and at > %(payee_since)s and resource_id = any(%(resources)s)
        order by at desc, id desc
        """,
        params,
    ).fetchall():
        changes.setdefault(row["resource_id"], []).append({
            "at": row["at"], "from": sorted(_payees(row["old_value"])), "to": sorted(_payees(row["new_value"])),
        })
    judged = {r["service_id"]: r for r in conn.execute(
        f"""
        select service_id,
               count(*)::int as total,
               count(*) filter (where delivery = 'delivered')::int as delivered,
               count(*) filter (where delivery = 'partial')::int as partial,
               count(*) filter (where delivery = 'not_delivered')::int as not_delivered,
               count(*) filter (where traffic = 'test')::int as test,
               count(*) filter (where traffic = 'test' and delivery = 'delivered')::int as test_delivered,
               count(*) filter (where traffic is null)::int as unlabelled,
               count(distinct owner_ref) filter (where traffic is distinct from 'test')::int as owner_buyers,
               max(created_at) as last_at
        from (
          select service_id, created_at, owner_ref,
                 gateway_attestation->'review'->>'delivery' as delivery,
                 gateway_attestation->'review'->>'traffic' as traffic
          from reviews
          where receipt_key_id is not null
            and created_at > %(judged_since)s
            and service_id = any(%(services)s)
            and weight_exclusion is distinct from 'platform_defect'
        ) j
        where delivery in ('delivered', 'partial', 'not_delivered')
        group by 1
        """,
        params,
    ).fetchall()}
    external = {r["service_id"]: r["n"] for r in conn.execute(
        """
        select r.service_id, count(distinct o.observer_wallet)::int as n
        from index_observations o join index_resources r on r.id = o.resource_id
        where o.kind = 'observation' and o.at > %(payers_since)s and r.service_id = any(%(services)s)
        group by 1
        """,
        params,
    ).fetchall()}

    by_service: dict[str, list[dict[str, Any]]] = {}
    for r in signals:
        by_service.setdefault(r["service_id"], []).append({
            "resource_id": r["resource_id"],
            "method": r["method"],
            "url": r["probe_url"],
            "last_probes": [_probe(p) for p in (r["last_probes"] or [])][:PROBE_WINDOW],
            "last_paid_ok_at": paid.get(r["resource_id"]),
            "latest_price_atomic": r["latest_price_atomic"],
            "latest_price_asset": r["latest_price_asset"],
            "median_price_7d_atomic": r["median_price_7d_atomic"],
            "payees": sorted(_payees(r["payees"])),
            "payee_since": r["payee_since"],
            "payee_changes_7d": changes.get(r["resource_id"], []),
            "bazaar_payers_30d": r["bazaar_payers_30d"],
        })

    out: dict[str, dict[str, Any]] = {}
    for sid in services:
        endpoints = by_service.get(sid, [])
        block = blocks.get(sid)
        j = judged.get(sid) or {}
        bazaar = [e["bazaar_payers_30d"] for e in endpoints if e["bazaar_payers_30d"] is not None]
        out[sid] = _jsonable({
            "as_of": as_of,
            "tracked": bool(endpoints),
            "blocked": {"note": block["note"], "by": block["blocked_by"], "at": block["blocked_at"]} if block else None,
            "payee_rotates": bool((flags.get(sid) or {}).get("payee_rotates")),
            "registered_payees": [{"payee": p, "since": at} for p, at in sorted(registered.get(sid, {}).items())],
            "endpoints": endpoints,
            "judged_30d": {
                "total": j.get("total", 0), "delivered": j.get("delivered", 0), "partial": j.get("partial", 0),
                "not_delivered": j.get("not_delivered", 0), "test": j.get("test", 0),
                "test_delivered": j.get("test_delivered", 0), "unlabelled": j.get("unlabelled", 0),
                "owner_buyers": j.get("owner_buyers", 0), "last_at": j.get("last_at"),
            },
            # Several endpoints of one service can share payers; the largest is the honest floor.
            "bazaar_payers_30d": max(bazaar) if bazaar else None,
            "external_payers_30d": external.get(sid, 0),
        })
    return out


# --- Storing results -----------------------------------------------------------------


def _same(row: dict[str, Any], state: str, reasons: list[str], inputs: dict[str, Any]) -> bool:
    old = dict(row["inputs"] or {})
    new = dict(inputs)
    old.pop("as_of", None)
    new.pop("as_of", None)
    return (row["state"] == state and row["rule_version"] == RULE_VERSION and list(row["reasons"] or []) == reasons
            and json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True))


def recompute(connect: Callable[[], Any], now: datetime, service_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the rule over every tracked or blocked service (or just ``service_ids``).

    A row is rewritten only when its answer changed (state, reasons or any
    input besides ``as_of``), so ``computed_at`` is when the current answer was
    first produced. Rows of services that are no longer tracked or blocked are
    removed: those read ``unverified`` (not tracked) without one.
    """
    ids = None if service_ids is None else sorted(set(service_ids))
    with connect() as conn:
        gathered = gather(conn, now, ids)
        only = "" if ids is None else "where service_id = any(%s)"
        existing = {r["service_id"]: r for r in conn.execute(
            f"select service_id, state, rule_version, reasons, inputs from service_reliability {only}",
            () if ids is None else (ids,)).fetchall()}
        counts = {s: 0 for s in STATES}
        writes = []
        for sid, inputs in gathered.items():
            state, reasons = evaluate(inputs)
            counts[state] += 1
            row = existing.get(sid)
            if row is None or not _same(row, state, reasons, inputs):
                writes.append((sid, state, RULE_VERSION, _ts(inputs["as_of"]), Jsonb(reasons), Jsonb(inputs)))
        if writes:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    insert into service_reliability (service_id, state, rule_version, computed_at, reasons, inputs)
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (service_id) do update set state = excluded.state,
                      rule_version = excluded.rule_version, computed_at = excluded.computed_at,
                      reasons = excluded.reasons, inputs = excluded.inputs
                    """,
                    writes,
                )
        gone = [sid for sid in existing if sid not in gathered]
        if gone:
            conn.execute("delete from service_reliability where service_id = any(%s)", (gone,))
        stats = {"services": len(gathered), "written": len(writes), "removed": len(gone), **counts}
        if ids is None:
            conn.execute(
                """
                insert into app_cache (key, payload, generated_at) values (%s, %s, now())
                on conflict (key) do update set payload = excluded.payload, generated_at = excluded.generated_at
                """,
                (STATUS_CACHE_KEY, Jsonb({"rule_version": RULE_VERSION, "checked_at": now.isoformat(), **stats})),
            )
        conn.commit()
    return stats


def run(connect: Callable[[], Any], now: datetime) -> dict[str, Any]:
    stats = recompute(connect, now)
    print(f"reliability: {json.dumps(stats)}")
    return stats


def recompute_in_background(connect: Callable[[], Any], service_id: str) -> None:
    """After a judged review: refresh that one service off the request path. Best effort;
    the hourly cron recomputes everything anyway."""
    def work() -> None:
        try:
            recompute(connect, datetime.now(UTC), [service_id])
        except Exception as error:  # noqa: BLE001 - never fail the review that triggered it
            print(f"reliability: recompute of {service_id} failed ({type(error).__name__})")
    threading.Thread(target=work, name="reliability-recompute", daemon=True).start()


# --- Reading results -----------------------------------------------------------------


def _payload(row: dict[str, Any] | None, service_id: str | None, checked_at: str | None = None) -> dict[str, Any]:
    if row is None:
        return {"service_id": service_id, "state": "unverified", "rule_version": RULE_VERSION, "computed_at": None,
                "checked_at": checked_at,
                "reasons": [NOT_TRACKED if service_id else NOT_INDEXED],
                "inputs": {"tracked": False, "indexed": service_id is not None}}
    return _jsonable({
        "service_id": row["service_id"],
        "state": row["state"],
        "rule_version": row["rule_version"],
        "computed_at": row["computed_at"],
        "checked_at": checked_at,
        "reasons": row["reasons"],
        "inputs": row["inputs"],
        **({"name": row["name"]} if row.get("name") is not None else {}),
    })


def checked_at(conn: psycopg.Connection) -> str | None:
    """When the last full recompute ran."""
    row = conn.execute("select payload from app_cache where key = %s", (STATUS_CACHE_KEY,)).fetchone()
    payload = row["payload"] if row else None
    return payload.get("checked_at") if isinstance(payload, dict) else None


def for_service(conn: psycopg.Connection, service_id: str) -> dict[str, Any] | None:
    """``GET /api/services/{id}/reliability``. None when no such service exists."""
    row = conn.execute(
        """
        select s.id as sid, s.name, r.service_id, r.state, r.rule_version, r.computed_at, r.reasons, r.inputs
        from services s left join service_reliability r on r.service_id = s.id
        where s.id = %s
        """,
        (service_id,),
    ).fetchone()
    if row is None:
        return None
    payload = _payload(row if row["service_id"] else None, row["sid"], checked_at(conn))
    payload["name"] = row["name"]
    return payload


def _service_for_url(conn: psycopg.Connection, url: str, method: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """The service an endpoint belongs to: the index's identity first (listed
    URLs, route templates), then the service registry's endpoints and aliases
    (old URLs of renamed hosted services, reviewed endpoints)."""
    from crowdcode import index as services_index
    from crowdcode.identity import normalize_api_endpoint

    resource = services_index.resolve_resource(conn, url, method)
    if resource is not None and resource.get("service_id"):
        return resource["service_id"], resource
    try:
        endpoint = normalize_api_endpoint(url)
    except ValueError:
        endpoint = None
    if endpoint:
        row = conn.execute(
            """
            select service_id from service_identifiers where identifier_type = 'api_endpoint' and identifier_value = %s
            union all
            select id from services where canonical_endpoint = %s
            limit 1
            """,
            (endpoint, endpoint),
        ).fetchone()
        if row:
            return row["service_id"], resource
    return None, resource


def for_url(conn: psycopg.Connection, url: str, method: str | None) -> dict[str, Any]:
    """``GET /api/reliability?url=&method=``: always an answer; unknown is ``unverified``."""
    service_id, resource = _service_for_url(conn, url, method)
    if service_id is None:
        payload = _payload(None, None)
        if resource is not None:  # listed in the index, but too little traffic to track
            payload["reasons"] = [NOT_TRACKED]
            payload["inputs"]["indexed"] = True
        payload["resolved"] = None
        return payload
    payload = for_service(conn, service_id) or _payload(None, service_id)
    payload["resolved"] = {
        "service_id": service_id,
        "resource_id": resource["id"] if resource else None,
        "method": resource["method"] if resource else method,
        "route_template": resource["route_template"] if resource else None,
    }
    return payload


def by_state(conn: psycopg.Connection, state: str, limit: int = 500, offset: int = 0) -> dict[str, Any]:
    """``GET /api/reliability?state=…``: the services in one state, and the counts of all three."""
    counts = {s: 0 for s in STATES}
    for row in conn.execute("select state, count(*)::int as n from service_reliability group by 1").fetchall():
        counts[row["state"]] = row["n"]
    rows = conn.execute(
        """
        select r.service_id, s.name, r.state, r.rule_version, r.computed_at, r.reasons, r.inputs
        from service_reliability r join services s on s.id = r.service_id
        where r.state = %s
        order by coalesce((r.inputs->'judged_30d'->>'total')::int, 0) desc,
                 coalesce((r.inputs->>'bazaar_payers_30d')::int, 0) desc, r.service_id
        limit %s offset %s
        """,
        (state, limit, offset),
    ).fetchall()
    at = checked_at(conn)
    return {"ok": True, "rule_version": RULE_VERSION, "checked_at": at, "state": state, "counts": counts,
            "limit": limit, "offset": offset, "services": [_payload(r, r["service_id"], at) for r in rows]}


# --- Blocking ------------------------------------------------------------------------


class BlockError(ValueError):
    pass


def block(conn: psycopg.Connection, service_id: str, note: str, actor: str) -> None:
    """Block a service: it reads ``unreliable`` with ``note`` as a public reason. Audited."""
    note = " ".join((note or "").split())
    if not 3 <= len(note) <= 240:
        raise BlockError("the note must be 3 to 240 characters; it is shown publicly as the reason")
    if not actor.strip():
        raise BlockError("say who is blocking it")
    if conn.execute("select 1 from services where id = %s", (service_id,)).fetchone() is None:
        raise BlockError(f"no service {service_id}")
    conn.execute(
        """
        insert into service_blocks (service_id, note, blocked_by) values (%s, %s, %s)
        on conflict (service_id) do update set note = excluded.note, blocked_by = excluded.blocked_by,
          blocked_at = now()
        """,
        (service_id, note, actor),
    )
    conn.execute("insert into service_block_audit (service_id, action, note, actor) values (%s, 'block', %s, %s)",
                 (service_id, note, actor))


def unblock(conn: psycopg.Connection, service_id: str, note: str, actor: str) -> bool:
    """Clear a block, with a note saying why. Audited. False when it was not blocked."""
    note = " ".join((note or "").split())
    if not 3 <= len(note) <= 240:
        raise BlockError("say why the block is cleared (3 to 240 characters)")
    if not actor.strip():
        raise BlockError("say who is clearing it")
    removed = conn.execute("delete from service_blocks where service_id = %s", (service_id,)).rowcount
    if removed:
        conn.execute("insert into service_block_audit (service_id, action, note, actor) values (%s, 'unblock', %s, %s)",
                     (service_id, note, actor))
    return bool(removed)


# --- A short in-process cache for the url lookup ---------------------------------------

_URL_CACHE: dict[tuple[str, str | None], tuple[float, dict[str, Any]]] = {}
_URL_CACHE_SECONDS = 30.0
_URL_CACHE_LIMIT = 5000
_lock = threading.Lock()


def cached_for_url(connect: Callable[[], Any], url: str, method: str | None) -> dict[str, Any]:
    """``for_url`` behind a 30-second cache: the gateway asks before purchases, and a
    state changes at most once per recompute."""
    key = (url, method)
    now = time.monotonic()
    with _lock:
        hit = _URL_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    with connect() as conn:
        payload = for_url(conn, url, method)
    with _lock:
        if len(_URL_CACHE) >= _URL_CACHE_LIMIT:
            for k in [k for k, (exp, _) in _URL_CACHE.items() if exp <= now] or list(_URL_CACHE)[: _URL_CACHE_LIMIT // 2]:
                _URL_CACHE.pop(k, None)
        _URL_CACHE[key] = (now + _URL_CACHE_SECONDS, payload)
    return payload
