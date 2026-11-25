"""One daily budget for every model call the backend and the cron make (w111).

On 2026-11-25 the OpenAI bill was $15 a day, and $13.30 of it was one job: the
service proposals pass sent every request to gpt-5.5 once an hour ($0.555 a
run, measured). Each job capped itself, or did not, and nothing capped the
total. Now every call through `summaries._chat_json` names its job, and this
module checks it against two limits before the call and records what it cost
afterwards:

- the day's total across every job (`CROWDCODE_LLM_DAILY_USD`, $0.80);
- the job's own share (`JOB_CAPS`, each overridable with
  `CROWDCODE_LLM_<JOB>_DAILY_USD`).

Search is the one call an agent waits for, so the batch jobs together may
never spend search's share: past their part of the day's budget they wait for
tomorrow and search keeps its own. When search's share is gone, search answers
from retrieval alone (relevant.py); it never fails for want of budget.

The ledger is one `app_cache` row (`llm_spend`), read before a call and locked
for the moment it takes to add one call's cost. No connection is held across a
model call. The day is the UTC day.

At 80% of the day's budget the ledger prints one `llm.budget` line (JSON, like
every alert line), once per day; Render ships it to Better Stack, where the
"CrowdCode LLM budget" alert emails the owner (CrowdCode's
`app/scripts/configure-reliability-alerts.ts`). Exhausting it prints another.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

LEDGER_KEY = "llm_spend"
DEFAULT_DAILY_USD = 0.80
WARN_SHARE = 0.80
#: The job a call is charged to when its caller did not say.
OTHER = "other"
SEARCH = "search"

#: Each job's share of a day, in USD. They add up to the day's budget, so no
#: job can starve another; search's share is also reserved from the rest.
JOB_CAPS: dict[str, float] = {
    SEARCH: 0.25,       # find_relevant: ~150 searches a day at ~$0.0015, fewer with its cache
    "tree": 0.15,       # capability tree classification and facets (its own ledger caps it too)
    "proposals": 0.15,  # one pass a day, placing only new requests (proposals.py)
    "miner": 0.12,      # the nightly demand miner, ~$0.09 a night (its own ledger caps it too)
    "screen": 0.10,     # request_service's screen: gpt-5.5, ~$0.009 a request, ~11 a day
    "summaries": 0.03,  # review summaries, only for services with new reviews
    "tree_evolve": 0.02,
    OTHER: 0.0,
}

#: USD per million tokens (input, cached input, output). An unknown model is
#: costed at the most expensive row, so the budget errs on the side of stopping.
MODEL_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
}
UNKNOWN_MODEL_PRICE = (5.00, 5.00, 30.00)


class BudgetExhausted(Exception):
    """The day's budget, or this job's share of it, is spent."""


def price(model: str) -> tuple[float, float, float]:
    for name, row in sorted(MODEL_PRICES.items(), key=lambda kv: -len(kv[0])):
        if model == name or model.startswith(name + "-"):
            return row
    return UNKNOWN_MODEL_PRICE


def cost(model: str, usage: dict[str, Any]) -> float:
    """What one response cost, from the usage OpenAI reported."""
    inp, cached, out = price(model)
    prompt = int(usage.get("prompt_tokens") or 0)
    hit = min(prompt, int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0))
    completion = int(usage.get("completion_tokens") or 0)
    return ((prompt - hit) * inp + hit * cached + completion * out) / 1e6


def input_cost(model: str, text_chars: int) -> float:
    """A lower bound on a call's cost before it is made: its prompt, at about
    3.4 characters a token (the tree's `token_bound`)."""
    return (text_chars / 3.4) * price(model)[0] / 1e6


def _env_usd(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def daily_cap() -> float:
    return _env_usd("CROWDCODE_LLM_DAILY_USD", DEFAULT_DAILY_USD)


def job_cap(job: str) -> float:
    return _env_usd(f"CROWDCODE_LLM_{job.upper()}_DAILY_USD", JOB_CAPS.get(job, 0.0))


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def _today(payload: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    return ((payload or {}).get("days") or {}).get(_day(now)) or {}


def _job_usd(day: dict[str, Any], job: str) -> float:
    return float(((day.get("jobs") or {}).get(job) or {}).get("usd") or 0.0)


def refusal(day: dict[str, Any], job: str, estimate_usd: float = 0.0) -> str | None:
    """Why a call of `job` costing about `estimate_usd` may not be made today, or None."""
    total = float(day.get("usd") or 0.0)
    cap = daily_cap()
    if total + estimate_usd > cap:
        return "daily"
    if _job_usd(day, job) + estimate_usd > job_cap(job):
        return "job"
    if job != SEARCH:
        # Search's share is reserved: the rest together stay inside what is left.
        others = total - _job_usd(day, SEARCH)
        if others + estimate_usd > cap - min(cap, job_cap(SEARCH)):
            return "reserved_for_search"
    return None


def _connect() -> Callable[[], Any]:
    from crowdcode.db import connect

    return connect


def read_today(now: datetime | None = None, connect: Callable[[], Any] | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    with (connect or _connect())() as conn:
        row = conn.execute("select payload from app_cache where key = %s", (LEDGER_KEY,)).fetchone()
    return _today((row or {}).get("payload"), now)


def check(job: str, estimate_usd: float = 0.0, *, now: datetime | None = None,
          connect: Callable[[], Any] | None = None) -> None:
    """Raise BudgetExhausted unless a call of `job` fits today's budget.

    A ledger that cannot be read refuses the call: a model call nobody can
    count is exactly how a bill grows unseen."""
    try:
        day = read_today(now, connect)
    except Exception as exc:
        logger.warning("llm budget: ledger unreadable (%s); refusing the %s call", type(exc).__name__, job)
        raise BudgetExhausted("ledger unreadable") from exc
    reason = refusal(day, job, estimate_usd)
    if reason:
        raise BudgetExhausted(f"{job}: {reason}")


def record(job: str, model: str, usage: dict[str, Any], *, now: datetime | None = None,
           connect: Callable[[], Any] | None = None) -> float:
    """Add one call's cost to today's ledger and return it. Never raises: the
    answer is already paid for, and losing it to a ledger write helps nobody."""
    usd = cost(model, usage)
    now = now or datetime.now(timezone.utc)
    try:
        with (connect or _connect())() as conn:
            line = _add(conn, job, model, usage, usd, now)
            conn.commit()
    except Exception as exc:
        logger.warning("llm budget: could not record a %s call of $%.5f (%s)", job, usd, type(exc).__name__)
        return usd
    if line:
        # One JSON line, which Render ships to Better Stack (the alert reads `event`).
        print(json.dumps(line), flush=True)
    return usd


def _add(conn: Any, job: str, model: str, usage: dict[str, Any], usd: float, now: datetime) -> dict[str, Any] | None:
    conn.execute(
        "insert into app_cache (key, payload, generated_at) values (%s, '{}'::jsonb, %s) on conflict (key) do nothing",
        (LEDGER_KEY, now),
    )
    row = conn.execute("select payload from app_cache where key = %s for update", (LEDGER_KEY,)).fetchone()
    payload = dict((row or {}).get("payload") or {})
    days = dict(payload.get("days") or {})
    key = _day(now)
    day = dict(days.get(key) or {})
    jobs = dict(day.get("jobs") or {})
    entry = dict(jobs.get(job) or {})
    before = float(day.get("usd") or 0.0)
    entry["usd"] = round(float(entry.get("usd") or 0.0) + usd, 6)
    entry["calls"] = int(entry.get("calls") or 0) + 1
    entry["input_tokens"] = int(entry.get("input_tokens") or 0) + int(usage.get("prompt_tokens") or 0)
    entry["output_tokens"] = int(entry.get("output_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    models = dict(entry.get("models") or {})
    models[model] = round(float(models.get(model) or 0.0) + usd, 6)
    entry["models"] = models
    jobs[job] = entry
    day["jobs"] = jobs
    day["usd"] = round(before + usd, 6)
    cap = daily_cap()
    line = None
    alerts = list(day.get("alerts") or [])
    for kind, share in (("exhausted", 1.0), ("warning", WARN_SHARE)):
        if cap > 0 and day["usd"] >= cap * share and kind not in alerts:
            alerts.append(kind)
            line = line or {
                "event": "llm.budget", "kind": kind, "day": key,
                "spent_usd": round(day["usd"], 4), "daily_usd": cap,
                "jobs": {name: round(float(j.get("usd") or 0.0), 4) for name, j in jobs.items()},
            }
    if alerts and "exhausted" in alerts and "warning" not in alerts:
        alerts.append("warning")
    day["alerts"] = alerts
    days[key] = day
    payload["days"] = dict(sorted(days.items())[-60:])
    payload["total_usd"] = round(float(payload.get("total_usd") or 0.0) + usd, 6)
    conn.execute(
        "update app_cache set payload = %s::jsonb, generated_at = %s where key = %s",
        (json.dumps(payload), now, LEDGER_KEY),
    )
    return line


def status(now: datetime | None = None, connect: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Today's spend by job against the caps, for /api/cron/status."""
    now = now or datetime.now(timezone.utc)
    with (connect or _connect())() as conn:
        row = conn.execute("select payload from app_cache where key = %s", (LEDGER_KEY,)).fetchone()
    days = ((row or {}).get("payload") or {}).get("days") or {}
    day = days.get(_day(now)) or {}
    return {
        "recent_days_usd": {key: round(float(value.get("usd") or 0.0), 4)
                            for key, value in sorted(days.items())[-8:]},
        "day": _day(now),
        "spent_usd": round(float(day.get("usd") or 0.0), 4),
        "daily_usd": daily_cap(),
        "jobs": {
            job: {"usd": round(_job_usd(day, job), 4), "cap_usd": job_cap(job),
                  "calls": int(((day.get("jobs") or {}).get(job) or {}).get("calls") or 0)}
            for job in sorted(set(JOB_CAPS) | set(day.get("jobs") or {}))
            if job != OTHER or (day.get("jobs") or {}).get(OTHER)
        },
    }
