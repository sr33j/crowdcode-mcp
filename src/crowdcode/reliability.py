"""reliability-v2: a delivery estimate, a quality estimate and a tier per offering (SRI-115 C10).

Contract C10 in the parent repository's design-docs/RELIABLE_SERVICES_BUILD.md.
An *offering* is one paid endpoint of one seller; in the index every tracked
resource links to its own ``services`` row, so the result is stored per
service and keyed by ``service_id``.

**Delivery** is ``Beta(alpha, beta)``:

- the prior comes from outside evidence: >= 10 Bazaar payers in 30 days with
  >= 1.5 calls per payer gives ``Beta(8, 1)``; >= 3 payers ``Beta(3, 1)``; a
  curated mpp.dev listing with no payer data ``Beta(3, 1)``; otherwise
  ``Beta(1, 1)``;
- each judged purchase in 30 days adds 1 to alpha (delivered) or beta (not
  delivered). A pre-rubric ``partial`` verdict counts once C11's re-read has
  resolved it (``reviews.delivery_reread``), and not before;
- each paid call that failed before settlement through the seller's fault
  (W2's signed failure records) adds 1 to beta.

**Quality** is the judge ratings behind the v3.1 review score
(crowdcode.scoring), re-shrunk to the offering's quality prior: the
leaderboard prior of the model it sells (``model_quality_priors``, C9), or 3.0.
Because v3.1 is ``(sum(w*r) + 2*3.0) / (n_eff + 2)``, that is exactly
``v3.1 + 2*(prior - 3.0) / (n_eff + 2)``.

**One score:** ``score = delivery mean x quality``, the expected judge rating of
one call, on the 1-5 scale. It replaces the review score on CrowdCode's pages.

**Tiers:**

==========  ==============================================================
``bad``     blocked; dead (the last 3 probes failed, no paid call since);
            payee changed in 7 days to one not advertised; latest price
            above 2x its 7-day median; or >= 4 judged with mean < 0.5
``proven``  >= 5 judged purchases and posterior mean >= 0.85
``popular`` a ``Beta(8, 1)`` or curated prior, and healthy probes (the latest
            conclusive probe was a valid 402, on every endpoint)
``new``     everything else, including untracked and unknown endpoints
==========  ==============================================================

For readers of v1, ``state`` stays: ``bad`` is ``unreliable``, ``proven`` and
``popular`` are ``reliable``, ``new`` is ``unverified``.

:func:`evaluate` is pure and reads only the published ``inputs``, so anyone can
recompute a tier from what ``GET /api/services/{id}/reliability`` returns.
The evidence rules carried over from v1 (inconclusive probes, liveness from
paid calls, which payee changes count, several endpoints) are described where
they are applied below.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

import psycopg
from psycopg.types.json import Jsonb

from crowdcode.review_sql import public_reviews

RULE_VERSION = "reliability-v2"
STATES = ("reliable", "unverified", "unreliable")
TIERS = ("bad", "proven", "popular", "new")
TIER_STATE = {"bad": "unreliable", "proven": "reliable", "popular": "reliable", "new": "unverified"}

# --- The rule's constants: the only copy -------------------------------------------
# Probes
PROBE_WINDOW = 3  # the probes the rule reads, newest first
DOWN_AFTER_FAILED = 3  # bad: this many failed probes in a row (the whole window)
INCONCLUSIVE_PROBE_ERRORS = frozenset({"probe_body_rejected"})
PAYEE_WINDOW = timedelta(days=7)
PRICE_MAX_MULTIPLE = Decimal(2)
# The delivery prior from outside evidence: (alpha, beta)
PRIOR_STRONG = (8, 1)  # >= STRONG_MIN_PAYERS payers and >= STRONG_MIN_REPEAT calls per payer
PRIOR_SOME = (3, 1)  # >= SOME_MIN_PAYERS payers
PRIOR_CURATED = (3, 1)  # mpp.dev's curated catalog, no payer data
PRIOR_NONE = (1, 1)
STRONG_MIN_PAYERS = 10
STRONG_MIN_REPEAT = Decimal("1.5")
SOME_MIN_PAYERS = 3
# Evidence
EVIDENCE_WINDOW = timedelta(days=30)
SELLER_FAILURES = ("seller_error", "seller_rejected", "no_receipt", "authorization_expired")
# Tiers
BAD_MIN_JUDGED = 4
BAD_MEAN_BELOW = Decimal("0.5")
PROVEN_MIN_JUDGED = 5
PROVEN_MEAN_AT_LEAST = Decimal("0.85")
# Quality: v3.1's shrinkage (crowdcode.scoring.KAPPA, MU0), re-centred on the offering's prior
QUALITY_KAPPA = Decimal(2)
QUALITY_V31_PRIOR = Decimal(3)
QUALITY_DEFAULT_PRIOR = Decimal(3)
# C12's allowance reference: what one standard call of a model costs, where a
# seller prices by duration (the C13 amendment's video minimum is 4 s at 720p).
REFERENCE_VIDEO_SECONDS = Decimal(5)
REFERENCE_TIERS = ("proven", "popular")
# What the gateway shows an owner (C4 caps reasons at 8).
MAX_REASONS = 8
MAX_MODELS = 12

NOT_INDEXED = "not in CrowdCode's index"
NOT_TRACKED = "not tracked by CrowdCode's index, so it is not probed"

# USDC, the only asset hosted agents hold (index_feeds.USDC), shown in dollars.
_USDC = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "epjfwdd5aufqssqem2qn1xzybapc8g4wewggkzwytdt1v",
    "0x20c000000000000000000000b9537d11c60e8b50",
}
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


def prior_for(outside: dict[str, Any]) -> dict[str, Any]:
    """The delivery prior from outside evidence: ``{alpha, beta, kind, source}``."""
    payers = int(outside.get("bazaar_payers_30d") or 0)
    calls = _dec(outside.get("bazaar_calls_30d"))
    repeat = (calls / payers) if payers and calls is not None else None
    if payers >= STRONG_MIN_PAYERS and repeat is not None and repeat >= STRONG_MIN_REPEAT:
        (a, b), kind = PRIOR_STRONG, "bazaar_strong"
        source = f"{payers} payers on the x402 Bazaar in 30 days, {repeat:.1f} calls per payer"
    elif payers >= SOME_MIN_PAYERS:
        (a, b), kind = PRIOR_SOME, "bazaar"
        source = f"{payers} payers on the x402 Bazaar in 30 days" + (
            f", {repeat:.1f} calls per payer" if repeat is not None else "")
    elif outside.get("curated"):
        (a, b), kind = PRIOR_CURATED, "curated"
        source = "listed in mpp.dev's curated catalog"
    else:
        (a, b), kind = PRIOR_NONE, "none"
        source = "no outside evidence"
    return {"alpha": a, "beta": b, "kind": kind, "source": source}


def quality_for(q: dict[str, Any]) -> Decimal:
    """Judge ratings (v3.1, weighted) shrunk to the offering's quality prior."""
    prior = _dec(q.get("prior_rating")) or QUALITY_DEFAULT_PRIOR
    v31 = _dec(q.get("v31_score"))
    n_eff = _dec(q.get("n_eff")) or Decimal(0)
    if v31 is None:
        return prior
    return v31 + QUALITY_KAPPA * (prior - QUALITY_V31_PRIOR) / (QUALITY_KAPPA + n_eff)


def _r(value: Decimal, places: int = 2) -> str:
    return f"{value:.{places}f}"


def evaluate(inputs: dict[str, Any]) -> dict[str, Any]:
    """reliability-v2 over one offering's ``inputs``.

    Returns ``{state, tier, reasons, posterior, quality, score}``. Reasons come
    in order: what decided the tier first, then the estimate, the probes, price
    and payee, the score, and context. Each is a sentence a reader can check
    against ``inputs``.
    """
    as_of = _ts(inputs.get("as_of")) or datetime.now(UTC)
    endpoints = list(inputs.get("endpoints") or [])
    multi = len(endpoints) > 1
    tracked = bool(inputs.get("tracked"))
    bad: list[str] = []
    health: list[str] = []
    context: list[str] = []

    blocked = inputs.get("blocked")
    if blocked:
        bad.append(f"blocked by a CrowdCode operator on {_day(blocked.get('at'))}: {blocked.get('note')}")

    # Probes. A probe whose request body the seller refused is inconclusive:
    # neither a failure nor a valid 402. A successful paid call since the oldest
    # of three failures means the service answered a buyer, so it is not down.
    healthy = tracked and bool(endpoints)
    down: list[str] = []
    for ep in endpoints:
        label = f"{ep.get('method')} {ep.get('url')}: " if multi else ""
        probes = list(ep.get("last_probes") or [])[:PROBE_WINDOW]
        valid = sum(1 for p in probes if p.get("ok"))
        failed = [p for p in probes if not p.get("ok") and p.get("error_code") not in INCONCLUSIVE_PROBE_ERRORS]
        conclusive = [p for p in probes if p.get("ok") or p.get("error_code") not in INCONCLUSIVE_PROBE_ERRORS]
        latest = _when(probes[0].get("at")) if probes else None
        if not (conclusive and conclusive[0].get("ok")):
            healthy = False
        if not probes:
            health.append(f"{label}not probed yet")
        elif len(failed) >= DOWN_AFTER_FAILED:
            codes = ", ".join(str(p.get("error_code") or p.get("http_status") or "failed") for p in failed)
            oldest = _ts(failed[-1].get("at"))
            paid = _ts(ep.get("last_paid_ok_at"))
            if paid is not None and oldest is not None and paid >= oldest:
                health.append(f"{label}the last {len(failed)} daily probes failed ({codes}; latest {latest}), "
                              f"but a paid call succeeded on {_when(paid)}, so it is not counted as down")
            else:
                down.append(f"{label}the last {len(failed)} daily probes all failed ({codes}; latest {latest})")
        elif conclusive and conclusive[0].get("ok"):
            health.append(f"{label}probes healthy: valid 402 in {valid} of the last "
                          f"{_plural(len(probes), 'daily probe')} (latest {latest})")
        elif conclusive:
            p = conclusive[0]
            health.append(f"{label}probes not healthy: the latest conclusive probe failed "
                          f"({p.get('error_code') or p.get('http_status')}, {_when(p.get('at'))}); "
                          f"valid 402 in {valid} of the last {_plural(len(probes), 'daily probe')}")
        else:
            health.append(f"{label}probes inconclusive: the seller refused the request body CrowdCode built "
                          f"in the last {_plural(len(probes), 'daily probe')}")
        price = _dec(ep.get("latest_price_atomic"))
        median = _dec(ep.get("median_price_7d_atomic"))
        asset = ep.get("latest_price_asset")
        if valid and price is not None and median is not None:
            if price > PRICE_MAX_MULTIPLE * median:
                bad.append(f"{label}latest price {_price(price, asset)} is more than {PRICE_MAX_MULTIPLE}× "
                           f"its 7-day median of {_price(median, asset)}")
            else:
                health.append(f"{label}latest price {_price(price, asset)} is within {PRICE_MAX_MULTIPLE}× "
                              f"its 7-day median of {_price(median, asset)}")
    # A service is dead only when every tracked endpoint is.
    if down and len(down) == len(endpoints):
        bad.extend(down)
    else:
        health.extend(down)
    if not tracked:
        health.insert(0, NOT_TRACKED)

    # Payee changes in the last 7 days. Only addresses the service had not
    # advertised before the change count: neither in the previous 402 nor among
    # the payees CrowdCode had registered for it by then. Rotating payees are exempt.
    registered = [(_norm_payee(r.get("payee")), _ts(r.get("since"))) for r in inputs.get("registered_payees") or []
                  if isinstance(r, dict)]
    if inputs.get("payee_rotates"):
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
                unadvertised = sorted(_payees(change.get("to")) - _payees(change.get("from")) - known)
                if unadvertised:
                    counted = True
                    bad.append(f"{label}payee changed on {_when(change.get('at'))} to {', '.join(unadvertised)}, "
                               "which the service's own 402 had not advertised before")
            if counted:
                continue
            if changes:
                health.append(f"{label}payee changed on {_when(changes[0].get('at'))} only to addresses the "
                              "service already advertised; not counted as a change")
            elif ep.get("payees"):
                since = ep.get("payee_since")
                health.append(f"{label}payee unchanged in the last 7 days"
                              + (f" (current since {_day(since)})" if since else ""))

    # The delivery estimate.
    prior = prior_for(inputs.get("outside") or {})
    judged = inputs.get("judged_30d") or {}
    failures = inputs.get("failures_30d") or {}
    delivered = int(judged.get("delivered") or 0)
    not_delivered = int(judged.get("not_delivered") or 0)
    failed_calls = int(failures.get("total") or 0)
    n_judged = delivered + not_delivered
    alpha = prior["alpha"] + delivered
    beta = prior["beta"] + not_delivered + failed_calls
    mean = Decimal(alpha) / Decimal(alpha + beta)
    details = []
    if judged.get("test"):
        details.append(f"{judged['test']} of the judged purchases were CrowdCode test purchases")
    if judged.get("unlabelled"):
        details.append(f"{judged['unlabelled']} from before test purchases were labelled")
    if judged.get("reread"):
        details.append(f"{judged['reread']} old partial verdicts re-read under the new rubric")
    if judged.get("unresolved"):
        details.append(f"{judged['unresolved']} partial verdicts not counted until they are re-read")
    if failures.get("test"):
        details.append(f"{failures['test']} of the failed calls were CrowdCode test purchases")
    estimate = (f"delivery estimate Beta({alpha}, {beta}), mean {_r(mean)}: prior Beta({prior['alpha']}, "
                f"{prior['beta']}) ({prior['source']}), plus {delivered} delivered and {not_delivered} not "
                f"delivered judged purchases and {_plural(failed_calls, 'paid call')} that failed before "
                f"settlement in 30 days" + (f" ({'; '.join(details)})" if details else ""))

    # Quality and the one score.
    q = inputs.get("quality") or {}
    quality = quality_for(q)
    prior_rating = _dec(q.get("prior_rating")) or QUALITY_DEFAULT_PRIOR
    v31 = _dec(q.get("v31_score"))
    n_eff = _dec(q.get("n_eff")) or Decimal(0)
    review_score = _dec(q.get("review_score"))
    if review_score is not None:
        # Scoring v4 (SRI-118, owner 2026-11-17): the one score shown is the
        # review score, min(weighted average, 5 × P(better than a 3)), with
        # failed purchases as 1-star observations. Outside popularity and the
        # delivery estimate decide the tier, never the score.
        score = review_score
        average = _dec(q.get("avg_rating_weighted"))
        p = _dec(q.get("p_above_3"))
        if average is None:
            score_reason = "unrated: no weighted reviews or failed purchases yet (sorted as 2.50)"
        else:
            score_reason = (f"score {_r(score)} = the lower of the weighted average rating {_r(average)} and "
                            f"5 × P(better than a 3) = 5 × {_r(p or Decimal(0))}; the delivery estimate decides "
                            "the tier only")
    else:
        # Inputs published before v4 still recompute to what they said.
        score = mean * quality
        rating_part = (f"judge ratings {_r(v31)} over {_r(n_eff, 1)} weighted reviews (review score v3.1)"
                       if v31 is not None and n_eff > 0 else "no weighted ratings yet")
        score_reason = (f"score {_r(score)} = delivery {_r(mean)} × quality {_r(quality)}: {rating_part}, shrunk to "
                        f"{_r(prior_rating, 1)} from {q.get('prior_source') or 'the default prior'}")

    if n_judged >= BAD_MIN_JUDGED and mean < BAD_MEAN_BELOW:
        bad.append(f"delivery estimate mean {_r(mean)} is below {BAD_MEAN_BELOW} after "
                   f"{_plural(n_judged, 'judged purchase')}")

    payers = []
    outside = inputs.get("outside") or {}
    if outside.get("bazaar_payers_30d"):
        payers.append(f"{outside['bazaar_payers_30d']} on the x402 Bazaar")
    if inputs.get("external_payers_30d"):
        payers.append(f"{inputs['external_payers_30d']} through crowdcode-mcp")
    if judged.get("owner_buyers"):
        payers.append(f"{_plural(int(judged['owner_buyers']), 'hosted-agent owner')} in judged purchases")
    if payers:
        context.append("payers in the last 30 days: " + ", ".join(payers))

    if bad:
        tier, headline = "bad", []
    elif n_judged >= PROVEN_MIN_JUDGED and mean >= PROVEN_MEAN_AT_LEAST:
        tier = "proven"
        headline = [f"proven: {delivered} of {_plural(n_judged, 'judged purchase')} in 30 days delivered, and the "
                    f"delivery estimate's mean {_r(mean)} is at least {PROVEN_MEAN_AT_LEAST}"]
    elif prior["kind"] in ("bazaar_strong", "curated") and healthy:
        tier = "popular"
        headline = [f"popular: {prior['source']}, and its probes are healthy"]
    else:
        tier = "new"
        if prior["kind"] in ("bazaar_strong", "curated") and not tracked:
            why = f"{prior['source']}, but CrowdCode does not probe it yet"
        elif prior["kind"] in ("bazaar_strong", "curated") and not any(ep.get("last_probes") for ep in endpoints):
            why = f"{prior['source']}, but it has not been probed yet (probes run once a day)"
        elif prior["kind"] in ("bazaar_strong", "curated"):
            why = f"{prior['source']}, but its probes are not healthy"
        else:
            why = (f"{prior['source']}; popular needs {STRONG_MIN_PAYERS} or more Bazaar payers with "
                   f"{STRONG_MIN_REPEAT} calls per payer, or mpp.dev's curated catalog")
        headline = [f"new: {why}; proven needs {PROVEN_MIN_JUDGED} judged purchases with a mean of "
                    f"{PROVEN_MEAN_AT_LEAST} ({n_judged} so far)"]
    reasons = [*headline, *bad, estimate, *health, score_reason, *context][:MAX_REASONS]
    return {
        "state": TIER_STATE[tier],
        "tier": tier,
        "reasons": reasons,
        "posterior": {"alpha": alpha, "beta": beta, "mean": float(round(mean, 4)),
                      "prior": {k: prior[k] for k in ("alpha", "beta", "kind", "source")},
                      "judged": n_judged, "delivered": delivered, "not_delivered": not_delivered,
                      "failed_calls": failed_calls},
        "quality": float(round(quality, 4)),
        "score": float(round(score, 4)),
    }


# --- The evidence --------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, float):
        return _jsonable(Decimal(str(value)))
    return value


def _probe(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in ("at", "ok", "http_status", "error_code", "quoted_amount_atomic", "asset")}


def _probe(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in ("at", "ok", "http_status", "error_code", "quoted_amount_atomic", "asset")}


def _columns(conn: psycopg.Connection, table: str) -> set[str]:
    """The columns a table has here (empty when it does not exist). Lets this
    rule read W1's and W2's additions whichever side's migration ran first."""
    return {r["column_name"] for r in conn.execute(
        "select column_name from information_schema.columns where table_schema = current_schema() and table_name = %s",
        (table,),
    ).fetchall()}


def _models(conn: psycopg.Connection, resources: list[int]) -> dict[int, list[dict[str, Any]]]:
    """The models each offering sells (``index_offering_models``, C9), with their leaderboard priors."""
    has_priors = bool(_columns(conn, "model_quality_priors"))
    prior_cols = ("q.prior_rating, q.source as prior_source, q.as_of as prior_as_of" if has_priors
                  else "null::numeric as prior_rating, null::text as prior_source, null::date as prior_as_of")
    join = "left join model_quality_priors q on q.model = m.model" if has_priors else ""
    models: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"""
        select m.resource_id, m.model, m.model_family, m.modality, {prior_cols}
        from index_offering_models m {join}
        where m.resource_id = any(%s)
        order by m.resource_id, m.model
        """,
        (resources,),
    ).fetchall():
        models.setdefault(row["resource_id"], []).append({
            "model": row["model"], "family": row["model_family"], "modality": row["modality"],
            "prior_rating": row["prior_rating"], "prior_source": row["prior_source"],
            "prior_as_of": row["prior_as_of"],
        })
    return models


def _quality_prior(models: list[dict[str, Any]]) -> tuple[Decimal, str]:
    """The leaderboard rating of the model an offering sells, or the mean over the
    models a multi-model offering sells that have one; 3.0 without any."""
    rated = [m for m in models if m.get("prior_rating") is not None]
    if not rated:
        return QUALITY_DEFAULT_PRIOR, "the default prior"
    rating = sum(Decimal(str(m["prior_rating"])) for m in rated) / len(rated)
    first = rated[0]
    if len(rated) == 1:
        return rating, f"{first['model']} on {first['prior_source']} ({_day(first['prior_as_of'])})"
    return rating, f"the mean of {len(rated)} models' leaderboard ratings ({first['prior_source']})"


def listed_inputs(conn: psycopg.Connection, resource: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    """The inputs for an offering the index lists but does not track: no probes and
    no service row, but its outside evidence and the quality of the model it sells."""
    models = (_models(conn, [resource["id"]]).get(resource["id"], []) if _columns(conn, "index_offering_models")
              else [])
    prior_rating, prior_source = _quality_prior(models)
    payers = resource.get("bazaar_payers_30d")
    return _jsonable({
        "as_of": as_of,
        "tracked": False,
        "listed": {"resource_id": resource["id"], "method": resource.get("method"),
                   "url": resource.get("probe_url"), "models": models[:MAX_MODELS]},
        "endpoints": [],
        "outside": {"bazaar_payers_30d": payers, "bazaar_calls_30d": resource.get("bazaar_calls_30d"),
                    "curated": "mpp" in (resource.get("sources") or []) and not payers},
        "judged_30d": {}, "failures_30d": {"total": 0, "test": 0, "by": {}},
        "quality": {"v31_score": None, "n_eff": 0, "prior_rating": prior_rating, "prior_source": prior_source},
        "external_payers_30d": 0,
    })


def gather(conn: psycopg.Connection, as_of: datetime, service_ids: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Every input reliability-v2 reads, per service: the tracked, blocked and
    reviewed services (``service_ids`` limits it to those)."""
    ids = None if service_ids is None else sorted(set(service_ids))
    only = "" if ids is None else "and service_id = any(%(ids)s)"
    since = as_of - EVIDENCE_WINDOW
    params: dict[str, Any] = {"ids": ids, "payee_since": as_of - PAYEE_WINDOW, "since": since}
    review_cols = _columns(conn, "reviews")
    observation_cols = _columns(conn, "index_observations")
    has_models = bool(_columns(conn, "index_offering_models"))

    signals = conn.execute(
        f"""
        select s.resource_id, s.service_id, s.method, s.probe_url, s.last_probes, s.latest_price_atomic,
               s.latest_price_asset, s.median_price_7d_atomic, s.payees, s.payee_since, s.bazaar_payers_30d,
               s.bazaar_calls_30d, r.sources
        from index_resource_signals s join index_resources r on r.id = s.resource_id
        where s.tracked and s.service_id is not null {only.replace('service_id', 's.service_id')}
        order by s.service_id, s.resource_id
        """,
        params,
    ).fetchall()
    blocks = {r["service_id"]: r for r in conn.execute(
        f"select service_id, note, blocked_by, blocked_at from service_blocks where true {only}", params).fetchall()}
    # Reviewed services are on CrowdCode's pages, tracked or not: each gets the one score.
    reviewed = {r["service_id"] for r in conn.execute(
        f"select distinct service_id from reviews where {public_reviews()} {only}", params).fetchall()}
    services = sorted({r["service_id"] for r in signals} | set(blocks) | reviewed)
    if not services:
        return {}
    params["services"] = services
    resources = [r["resource_id"] for r in signals]
    params["resources"] = resources

    flags = {r["id"]: r for r in conn.execute(
        # score_mean is the review score's posterior mean, v3.1's shrunk mean
        # exactly (scoring v4 publishes 5 × P(>3) as `score`); before the v4
        # replay fills it, `score` still is that mean.
        "select id, payee_rotates, payment_target_ref, created_at, coalesce(score_mean, score) as score, n_eff, "
        "score as review_score, avg_rating_weighted, p_above_3, score_mean is not null as v4 "
        "from services where id = any(%(services)s)",
        params).fetchall()}
    # Each registered payee with when CrowdCode first knew it: only a payee known
    # before a change can excuse that change.
    registered: dict[str, dict[str, datetime]] = {}

    def remember(sid: str, value: Any, at: datetime) -> None:
        for payee in _payees([value]):
            known = registered.setdefault(sid, {})
            known[payee] = min(known.get(payee, at), at)

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

    # Judged purchases. C11's re-read (W2) resolves an old `partial`; until it
    # has, a partial counts as neither delivered nor not delivered.
    attested = "gateway_attestation->'review'->>'delivery'"
    delivery = f"coalesce(delivery_reread, {attested})" if "delivery_reread" in review_cols else attested
    reread = (f"delivery_reread is not null and {attested} = 'partial'" if "delivery_reread" in review_cols
              else "false")
    judged = {r["service_id"]: r for r in conn.execute(
        f"""
        select service_id,
               count(*) filter (where delivery = 'delivered')::int as delivered,
               count(*) filter (where delivery = 'not_delivered')::int as not_delivered,
               count(*) filter (where delivery = 'partial')::int as unresolved,
               count(*) filter (where reread)::int as reread,
               count(*) filter (where traffic = 'test' and delivery <> 'partial')::int as test,
               count(*) filter (where traffic is null and delivery <> 'partial')::int as unlabelled,
               count(distinct owner_ref) filter (where traffic is distinct from 'test')::int as owner_buyers,
               max(created_at) as last_at
        from (
          select service_id, created_at, owner_ref, {delivery} as delivery, ({reread}) as reread,
                 gateway_attestation->'review'->>'traffic' as traffic
          from reviews
          where receipt_key_id is not null
            and created_at > %(since)s
            and service_id = any(%(services)s)
            and weight_exclusion is distinct from 'platform_defect'
        ) j
        where delivery in ('delivered', 'partial', 'not_delivered')
        group by 1
        """,
        params,
    ).fetchall()}

    # Paid calls that failed before settlement through the seller's fault (W2's
    # signed records): kind 'purchase', no review, an attempt reference.
    failures: dict[str, dict[str, Any]] = {}
    if "attempt_ref" in observation_cols:
        traffic = "o.traffic" if "traffic" in observation_cols else "null::text"
        for row in conn.execute(
            f"""
            select r.service_id, o.detail, count(*)::int as n,
                   count(*) filter (where {traffic} = 'test')::int as test
            from index_observations o join index_resources r on r.id = o.resource_id
            where o.kind = 'purchase' and o.review_id is null and o.attempt_ref is not null
              and o.detail = any(%(kinds)s) and o.at > %(since)s and r.service_id = any(%(services)s)
            group by 1, 2
            """,
            {**params, "kinds": list(SELLER_FAILURES)},
        ).fetchall():
            f = failures.setdefault(row["service_id"], {"total": 0, "test": 0, "by": {}})
            f["total"] += row["n"]
            f["test"] += row["test"]
            f["by"][row["detail"]] = row["n"]

    external = {r["service_id"]: r["n"] for r in conn.execute(
        """
        select r.service_id, count(distinct o.observer_wallet)::int as n
        from index_observations o join index_resources r on r.id = o.resource_id
        where o.kind = 'observation' and o.at > %(since)s and r.service_id = any(%(services)s)
        group by 1
        """,
        params,
    ).fetchall()}

    # Models (C9) and their leaderboard quality priors.
    models = _models(conn, resources) if has_models else {}

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
            "bazaar_calls_30d": r["bazaar_calls_30d"],
            "curated": "mpp" in (r["sources"] or []),
            "models": models.get(r["resource_id"], [])[:MAX_MODELS],
        })

    out: dict[str, dict[str, Any]] = {}
    for sid in services:
        endpoints = by_service.get(sid, [])
        block = blocks.get(sid)
        j = judged.get(sid) or {}
        flag = flags.get(sid) or {}
        # Outside evidence: the endpoint with the most Bazaar payers speaks for the offering.
        busiest = max(endpoints, key=lambda e: e["bazaar_payers_30d"] or 0, default=None)
        has_payers = any(e["bazaar_payers_30d"] for e in endpoints)
        prior_rating, prior_source = _quality_prior([m for e in endpoints for m in e["models"]])
        out[sid] = _jsonable({
            "as_of": as_of,
            "tracked": bool(endpoints),
            "blocked": {"note": block["note"], "by": block["blocked_by"], "at": block["blocked_at"]} if block else None,
            "payee_rotates": bool(flag.get("payee_rotates")),
            "registered_payees": [{"payee": p, "since": at} for p, at in sorted(registered.get(sid, {}).items())],
            "endpoints": endpoints,
            "outside": {
                "bazaar_payers_30d": busiest["bazaar_payers_30d"] if busiest else None,
                "bazaar_calls_30d": busiest["bazaar_calls_30d"] if busiest else None,
                "curated": any(e["curated"] for e in endpoints) and not has_payers,
            },
            "judged_30d": {
                "delivered": j.get("delivered", 0), "not_delivered": j.get("not_delivered", 0),
                "unresolved": j.get("unresolved", 0), "reread": j.get("reread", 0), "test": j.get("test", 0),
                "unlabelled": j.get("unlabelled", 0), "owner_buyers": j.get("owner_buyers", 0),
                "last_at": j.get("last_at"),
            },
            "failures_30d": failures.get(sid, {"total": 0, "test": 0, "by": {}}),
            "quality": {"v31_score": flag.get("score"), "n_eff": flag.get("n_eff"),
                        "prior_rating": prior_rating, "prior_source": prior_source,
                        **({"review_score": flag.get("review_score"),
                            "avg_rating_weighted": flag.get("avg_rating_weighted"),
                            "p_above_3": flag.get("p_above_3")} if flag.get("v4") else {})},
            "external_payers_30d": external.get(sid, 0),
        })
    return out


# --- Storing results -----------------------------------------------------------------


def _same(row: dict[str, Any], result: dict[str, Any], inputs: dict[str, Any]) -> bool:
    old = dict(row["inputs"] or {})
    new = dict(inputs)
    old.pop("as_of", None)
    new.pop("as_of", None)
    return (row["state"] == result["state"] and row.get("tier") == result["tier"]
            and row["rule_version"] == RULE_VERSION and list(row["reasons"] or []) == result["reasons"]
            and json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True))


def recompute(connect: Callable[[], Any], now: datetime, service_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the rule over every tracked, blocked or reviewed service (or just ``service_ids``).

    A row is rewritten only when its answer changed (tier, state, reasons or any
    input besides ``as_of``), so ``computed_at`` is when the current answer was
    first produced. Rows of services that drop out are removed; they read
    ``new`` without one.
    """
    ids = None if service_ids is None else sorted(set(service_ids))
    with connect() as conn:
        gathered = gather(conn, now, ids)
        only = "" if ids is None else "where service_id = any(%s)"
        existing = {r["service_id"]: r for r in conn.execute(
            f"select service_id, state, tier, rule_version, reasons, inputs from service_reliability {only}",
            () if ids is None else (ids,)).fetchall()}
        counts = {t: 0 for t in TIERS}
        writes = []
        for sid, inputs in gathered.items():
            result = evaluate(inputs)
            counts[result["tier"]] += 1
            row = existing.get(sid)
            if row is None or not _same(row, result, inputs):
                p = result["posterior"]
                writes.append((sid, result["state"], result["tier"], p["alpha"], p["beta"], result["quality"],
                               result["score"], RULE_VERSION, _ts(inputs["as_of"]), Jsonb(result["reasons"]),
                               Jsonb(inputs)))
        if writes:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    insert into service_reliability (service_id, state, tier, alpha, beta, quality, score,
                      rule_version, computed_at, reasons, inputs)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (service_id) do update set state = excluded.state, tier = excluded.tier,
                      alpha = excluded.alpha, beta = excluded.beta, quality = excluded.quality,
                      score = excluded.score, rule_version = excluded.rule_version,
                      computed_at = excluded.computed_at, reasons = excluded.reasons, inputs = excluded.inputs
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

_DEFAULT = evaluate({"tracked": False, "endpoints": []})


def _unknown(service_id: str | None, checked: str | None, reason: str) -> dict[str, Any]:
    """An offering without a row: no outside evidence and nothing judged, so `new`."""
    return {"service_id": service_id, "state": _DEFAULT["state"], "tier": _DEFAULT["tier"],
            "rule_version": RULE_VERSION, "computed_at": None, "checked_at": checked,
            "reasons": [reason, f"new: {_DEFAULT['posterior']['prior']['source']}"],
            "posterior": _DEFAULT["posterior"], "quality": _DEFAULT["quality"], "score": _DEFAULT["score"],
            "inputs": {"tracked": False, "indexed": service_id is not None}}


def _payload(row: dict[str, Any], checked: str | None = None) -> dict[str, Any]:
    inputs = row["inputs"] or {}
    result = evaluate(inputs) if row.get("rule_version") == RULE_VERSION else None
    payload = _jsonable({
        "service_id": row["service_id"],
        **({"name": row["name"]} if row.get("name") is not None else {}),
        "state": row["state"],
        "tier": row.get("tier") or (result or {}).get("tier"),
        "rule_version": row["rule_version"],
        "computed_at": row["computed_at"],
        "checked_at": checked,
        "reasons": row["reasons"],
        "inputs": inputs,
    })
    # Numbers stay numbers (``_jsonable`` writes inputs' decimals as strings).
    payload["posterior"] = result["posterior"] if result else None
    payload["quality"] = float(row["quality"]) if row.get("quality") is not None else None
    payload["score"] = float(row["score"]) if row.get("score") is not None else None
    return payload


def checked_at(conn: psycopg.Connection) -> str | None:
    """When the last full recompute ran."""
    row = conn.execute("select payload from app_cache where key = %s", (STATUS_CACHE_KEY,)).fetchone()
    payload = row["payload"] if row else None
    return payload.get("checked_at") if isinstance(payload, dict) else None


_ROW = ("r.service_id, r.state, r.tier, r.alpha, r.beta, r.quality, r.score, r.rule_version, r.computed_at, "
        "r.reasons, r.inputs")


def for_service(conn: psycopg.Connection, service_id: str) -> dict[str, Any] | None:
    """``GET /api/services/{id}/reliability``. None when no such service exists."""
    row = conn.execute(
        f"select s.id as sid, s.name, {_ROW} from services s left join service_reliability r on r.service_id = s.id "
        "where s.id = %s",
        (service_id,),
    ).fetchone()
    if row is None:
        return None
    payload = (_payload(row, checked_at(conn)) if row["service_id"]
               else _unknown(row["sid"], checked_at(conn), NOT_TRACKED))
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
    """``GET /api/reliability?url=&method=``: always an answer; unknown is ``new``.
    Carries C12's ``allowance_reference`` (None when there is no proven or popular
    alternative with a per-call price)."""
    service_id, resource = _service_for_url(conn, url, method)
    if service_id is None and resource is not None:
        # Listed, not tracked: evaluated now from what the index knows about it.
        inputs = listed_inputs(conn, resource, datetime.now(UTC))
        result = evaluate(inputs)
        return {"service_id": None, "state": result["state"], "tier": result["tier"], "rule_version": RULE_VERSION,
                "computed_at": inputs["as_of"], "checked_at": None, "reasons": result["reasons"],
                "posterior": result["posterior"], "quality": result["quality"], "score": result["score"],
                "allowance_reference": allowance_reference(conn, resource),
                "inputs": {**inputs, "indexed": True},
                "resolved": {"service_id": None, "resource_id": resource["id"], "method": resource["method"],
                             "route_template": resource["route_template"]}}
    if service_id is None:
        payload = _unknown(None, None, NOT_INDEXED)
        payload["inputs"]["indexed"] = False
        payload["resolved"] = None
        payload["allowance_reference"] = None
        return payload
    payload = for_service(conn, service_id) or _unknown(service_id, None, NOT_TRACKED)
    if resource is None:  # found through the registry's aliases: use the service's own tracked resource
        resource = conn.execute(
            "select * from index_resources where service_id = %s order by tracked desc, id limit 1", (service_id,)
        ).fetchone()
    payload["allowance_reference"] = allowance_reference(conn, resource) if resource else None
    payload["resolved"] = {
        "service_id": service_id,
        "resource_id": resource["id"] if resource else None,
        "method": resource["method"] if resource else method,
        "route_template": resource["route_template"] if resource else None,
    }
    return payload


def listing(conn: psycopg.Connection, *, state: str | None = None, tier: str | None = None,
            limit: int = 500, offset: int = 0) -> dict[str, Any]:
    """``GET /api/reliability?state=…`` or ``?tier=…``: one group, and the counts of every state and tier."""
    states = {s: 0 for s in STATES}
    tiers = {t: 0 for t in TIERS}
    for row in conn.execute("select state, tier, count(*)::int as n from service_reliability group by 1, 2").fetchall():
        states[row["state"]] = states.get(row["state"], 0) + row["n"]
        if row["tier"]:
            tiers[row["tier"]] = tiers.get(row["tier"], 0) + row["n"]
    column, value = ("tier", tier) if tier else ("state", state)
    rows = conn.execute(
        f"""
        select s.name, {_ROW}
        from service_reliability r join services s on s.id = r.service_id
        where r.{column} = %s
        order by r.score desc nulls last, r.service_id
        limit %s offset %s
        """,
        (value, limit, offset),
    ).fetchall()
    at = checked_at(conn)
    return {"ok": True, "rule_version": RULE_VERSION, "checked_at": at, column: value, "counts": states,
            "tier_counts": tiers, "limit": limit, "offset": offset, "services": [_payload(r, at) for r in rows]}


def by_state(conn: psycopg.Connection, state: str, limit: int = 500, offset: int = 0) -> dict[str, Any]:
    return listing(conn, state=state, limit=limit, offset=offset)


# --- C12: the allowance reference -------------------------------------------------------


def _usd(value: Any) -> Decimal | None:
    d = _dec(value)
    return d if d is not None and d >= 0 else None


def model_call_usd(pricing: Any) -> Decimal | None:
    """One standard call of a model, in USD, from a seller's own pricing
    (``index_offering_models.pricing``): a fixed clip price, one image, or a
    5-second clip at 720p for per-second pricing, plus per-call fees. None when
    the price depends on input size (per character, per token)."""
    if not isinstance(pricing, dict):
        return None
    fees = sum((_usd(pricing.get(k)) or Decimal(0)) for k in ("transaction_fee", "per_call_surcharge"))
    secs = REFERENCE_VIDEO_SECONDS
    for key in ("clip_usd", "per_call", "usd_per_call"):
        if _usd(pricing.get(key)) is not None:
            return _usd(pricing[key]) + fees
    if _usd(pricing.get("per_image")) is not None:
        return _usd(pricing["per_image"]) + fees
    if str(pricing.get("per") or "").lower() in ("call", "request", "clip", "image", "video"):
        if _usd(pricing.get("usd")) is not None:
            return _usd(pricing["usd"]) + fees
    for key in ("per_second", "duration_seconds_720p", "text_to_video_duration_seconds_720p",
                "duration_seconds_without_audio_720p", "duration_seconds_with_audio_720p", "duration_seconds",
                "duration_seconds_without_audio"):
        if _usd(pricing.get(key)) is not None:
            return _usd(pricing[key]) * secs + fees
    for key in ("cents_per_video_output_second_720p", "cents_per_second_output"):
        if _usd(pricing.get(key)) is not None:
            return _usd(pricing[key]) / 100 * secs + fees
    return None


def _quote_atomic(row: dict[str, Any]) -> int | None:
    """The offering's own per-call price in atomic USDC: its latest valid quote, else its listing."""
    for amount, asset in ((row.get("quoted_price_atomic"), row.get("quoted_asset")),
                          (row.get("price_atomic"), row.get("price_asset"))):
        if amount is not None and isinstance(asset, str) and asset.lower() in _USDC:
            return int(amount)
    return None


def _best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """C13's best known option: the highest one score, then the lower price."""
    priced = [c for c in candidates if c.get("price_atomic") is not None]
    if not priced:
        return None
    return sorted(priced, key=lambda c: (-(float(c["score"] or 0)), c["price_atomic"], c["service_id"]))[0]


def allowance_reference(conn: psycopg.Connection, resource: dict[str, Any]) -> dict[str, Any] | None:
    """C12: the price of the best ``proven`` or ``popular`` offering in the same
    model as ``resource`` (quality first, the C13 amendment), else in the same
    tree leaf. ``{price_atomic, service_id, resource_id, tier, score, model | leaf}``,
    or None when no such alternative has a per-call price."""
    rid = resource["id"]
    sid = resource.get("service_id")
    has_models = bool(_columns(conn, "index_offering_models"))
    n_models = ("(select count(*) from index_offering_models x where x.resource_id = r.id)::int" if has_models
                else "0")
    if has_models:
        rows = conn.execute(
            """
            select m.model, m.pricing, r.id as resource_id, r.service_id, r.quoted_price_atomic, r.quoted_asset,
                   r.price_atomic, r.price_asset, sr.tier, sr.score,
                   (select count(*) from index_offering_models x where x.resource_id = r.id)::int as n_models
            from index_offering_models m
            join index_resources r on r.id = m.resource_id
            join service_reliability sr on sr.service_id = r.service_id
            where m.model in (select model from index_offering_models where resource_id = %s)
              and r.id <> %s and r.service_id is distinct from %s and sr.tier = any(%s)
            """,
            (rid, rid, sid, list(REFERENCE_TIERS)),
        ).fetchall()
        by_model: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            usd = model_call_usd(row["pricing"])
            price = (int((usd * 10**6).to_integral_value()) if usd is not None
                     else _quote_atomic(row) if row["n_models"] == 1 else None)
            by_model.setdefault(row["model"], []).append({**row, "price_atomic": price})
        best_by_model = {m: b for m, cs in by_model.items() if (b := _best(cs))}
        if best_by_model:
            best = _best(list(best_by_model.values()))
            return {"basis": "model", "model": best["model"], "price_atomic": best["price_atomic"],
                    "service_id": best["service_id"], "resource_id": best["resource_id"], "tier": best["tier"],
                    "score": float(best["score"]) if best["score"] is not None else None,
                    **({"by_model": {m: b["price_atomic"] for m, b in sorted(best_by_model.items())}}
                       if len(best_by_model) > 1 else {})}
    if not (_columns(conn, "capability_classifications") and _columns(conn, "capability_leaves")):
        return None
    from crowdcode.capability_tree import ranking_policy

    leaves = conn.execute(
        """
        select distinct l.id, l.node, l.facets
        from capability_classifications c
        cross join jsonb_array_elements(c.assignments) a
        join capability_leaves l on l.node = a->>'node' and (a->>'facet') = any(l.facets)
        where c.item_key = %s
        order by l.id
        """,
        (f"r:{rid}",),
    ).fetchall()
    best = None
    for leaf in leaves:
        policy = ranking_policy(leaf["node"], leaf["id"])
        candidates = [c for c in _leaf_candidates(conn, leaf, has_models)
                      if c["resource_id"] != rid and (sid is None or c["service_id"] != sid)]
        found = _best_quality_first(candidates) if policy == "quality_first" else _best(candidates)
        if found and (best is None or found["price_atomic"] > best["price_atomic"]):
            best = {**found, "leaf": leaf["id"], "policy": policy}
    if best is None:
        return None
    out = {"basis": "leaf", "leaf": best["leaf"], "price_atomic": best["price_atomic"],
           "service_id": best["service_id"], "resource_id": best["resource_id"], "tier": best["tier"],
           "score": float(best["score"]) if best["score"] is not None else None}
    if best["policy"] == "quality_first" and best.get("model"):
        out["model"] = best["model"]
    return out


LEAF_CACHE_SECONDS = 300.0
_LEAF_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _leaf_candidates(conn: psycopg.Connection, leaf: dict[str, Any], has_models: bool) -> list[dict[str, Any]]:
    """Every proven or popular offering in one leaf, one entry per model it sells
    there (priced from the seller's own pricing) or one entry priced by its quote.
    Cached for five minutes, like the gateway's own lookups."""
    now = time.monotonic()
    with _lock:
        hit = _LEAF_CACHE.get(leaf["id"])
        if hit and hit[0] > now:
            return hit[1]
    models_sql = ("""left join lateral (
                       select coalesce(jsonb_agg(jsonb_build_object('model', m.model, 'pricing', m.pricing,
                                'prior_rating', q.prior_rating)), '[]'::jsonb) as models
                       from index_offering_models m left join model_quality_priors q on q.model = m.model
                       where m.resource_id = r.id) om on true"""
                  if has_models and _columns(conn, "model_quality_priors") else
                  "cross join (select '[]'::jsonb as models) om")
    rows = conn.execute(
        f"""
        select distinct on (r.id) r.id as resource_id, r.service_id, r.quoted_price_atomic, r.quoted_asset,
               r.price_atomic, r.price_asset, sr.tier, sr.score, sr.alpha, sr.beta, om.models
        from service_reliability sr
        join index_resources r on r.service_id = sr.service_id
        join capability_classifications c on c.item_key = 'r:' || r.id
        cross join jsonb_array_elements(c.assignments) a
        {models_sql}
        where sr.tier = any(%(tiers)s) and a->>'node' = %(node)s and (a->>'facet') = any(%(facets)s)
        """,
        {"node": leaf["node"], "facets": list(leaf["facets"] or []), "tiers": list(REFERENCE_TIERS)},
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        mean = (Decimal(row["alpha"]) / Decimal(row["alpha"] + row["beta"])
                if row["alpha"] is not None and row["beta"] is not None else None)
        base = {k: row[k] for k in ("resource_id", "service_id", "tier", "score")}
        models = row["models"] or []
        if len(models) > 1:
            # A multi-model seller's quote is for whichever model the probe named: price each model itself.
            for m in models:
                usd = model_call_usd(m.get("pricing"))
                out.append({**base, "model": m.get("model"), "quality": _dec(m.get("prior_rating")), "mean": mean,
                            "price_atomic": int((usd * 10**6).to_integral_value()) if usd is not None else None})
        else:
            m = models[0] if models else {}
            usd = model_call_usd(m.get("pricing"))
            out.append({**base, "model": m.get("model"), "quality": _dec(m.get("prior_rating")), "mean": mean,
                        "price_atomic": (int((usd * 10**6).to_integral_value()) if usd is not None
                                         else _quote_atomic(row))})
    with _lock:
        _LEAF_CACHE[leaf["id"]] = (now + LEAF_CACHE_SECONDS, out)
    return out


QUALITY_BAND = Decimal("0.25")


def _best_quality_first(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The C13 amendment: the best model (leaderboard quality; 3.0 when unrated),
    and within 0.25 of it the seller most likely to deliver, then the cheaper."""
    priced = [c for c in candidates if c.get("price_atomic") is not None]
    if not priced:
        return None
    quality = lambda c: c["quality"] if c.get("quality") is not None else QUALITY_DEFAULT_PRIOR
    top = max(quality(c) for c in priced)
    band = [c for c in priced if quality(c) >= top - QUALITY_BAND]
    return sorted(band, key=lambda c: (-(c["mean"] or 0), c["price_atomic"], c["service_id"]))[0]


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
