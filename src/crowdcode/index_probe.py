"""One unpaid request to a tracked resource, and what its answer says.

Etiquette (C1), enforced here:

- one request per resource per probe; the scheduler (crowdcode.index) allows at
  most one probe per resource per day;
- ``User-Agent: CrowdCodeIndex/1 (+https://crowdcode.app/bot)``;
- a POST, PUT or PATCH always carries a JSON body: the seller's own example
  request when its listing has one, otherwise the smallest body its schema
  allows, otherwise ``{}``. Never an empty body, which sellers count as errors;
- redirects are not followed (a 3xx is recorded, with where it pointed);
- only https hosts whose every address is public are called
  (``advertised.public_endpoint``, the same guard the payee learner uses).

A probe is ``ok`` when the answer is a valid payment challenge: HTTP 402 with
an x402 ``accepts`` entry (body or ``PAYMENT-REQUIRED`` header) or an MPP
``WWW-Authenticate: Payment`` challenge, naming a payee and an amount on Base,
Solana or Tempo.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx

from crowdcode.advertised import public_endpoint
from crowdcode.index_feeds import TEMPO, USER_AGENT, _int, _payee, caip2, same_asset, x402_offers

PROBE_TIMEOUT = 12.0
BODY_LIMIT = 256 * 1024
WORKERS = 16
PER_HOST = 2
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


@dataclass
class ProbeResult:
    ok: bool
    http_status: int | None = None
    error_code: str | None = None
    protocol: str | None = None
    network: str | None = None
    payee: str | None = None
    pay_to: list[str] = field(default_factory=list)
    quoted_amount_atomic: int | None = None
    asset: str | None = None
    latency_ms: int | None = None
    detail: str | None = None


# "AI model ID (e.g., gpt-4o-mini, claude-haiku-4.5)": the seller's own example.
_EXAMPLE_IN_TEXT = re.compile(r"\b(?:e\.g\.|eg\.|for example|example)[:,]?\s*[\"'`]?([A-Za-z0-9][\w.\-/:@]*)", re.I)
# The chat-completions shape most model sellers share; an empty list is refused.
_MESSAGES = [{"role": "user", "content": "Hello"}]


def _placeholder(schema: Any, name: str | None = None, depth: int = 0) -> Any:
    """A value for one field: its documented example, else something of its type."""
    if not isinstance(schema, dict):
        schema = {}
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    for key in ("enum", "examples"):
        if isinstance(schema.get(key), list) and schema[key]:
            return schema[key][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if name == "messages" and kind in (None, "array"):
        return _MESSAGES
    if kind in (None, "string"):
        match = _EXAMPLE_IN_TEXT.search(str(schema.get("description") or ""))
        if match:
            return match.group(1).rstrip(".,;:)")
        return "test"
    if kind in ("integer", "number"):
        minimum = schema.get("minimum")
        return minimum if isinstance(minimum, (int, float)) and minimum > 1 else 1
    if kind == "boolean":
        return False
    if kind == "array":
        items = schema.get("items")
        return [_placeholder(items, depth=depth + 1)] if isinstance(items, dict) and depth < 3 else []
    if kind == "object":
        return body_from_schema(schema, depth + 1) if depth < 3 else {}
    return "test"


def minimal_body(input_schema: Any) -> dict[str, Any]:
    """The required fields of the documented body (a Bazaar input schema), with placeholder values.

    Some listings describe their fields in ``info.bodyFields`` rather than in the
    schema; those descriptions (and their examples) are used too.
    """
    schema = (input_schema or {}).get("schema") if isinstance(input_schema, dict) else None
    body = ((schema or {}).get("properties") or {}).get("body") if isinstance(schema, dict) else None
    info = (input_schema or {}).get("info") if isinstance(input_schema, dict) else None
    fields = (info or {}).get("bodyFields") if isinstance(info, dict) else None
    if isinstance(fields, dict) and isinstance(body, dict):
        properties = dict(body.get("properties") or {}) if isinstance(body.get("properties"), dict) else {}
        for key, field in fields.items():
            if isinstance(field, dict):
                properties[key] = {**field, **(properties.get(key) or {})} if isinstance(properties.get(key), dict) else field
        body = {**body, "properties": properties}
    return body_from_schema(body)


def body_from_schema(body: Any, depth: int = 0) -> dict[str, Any]:
    """A JSON body schema's required fields, each set to its example (or a placeholder)."""
    if not isinstance(body, dict):
        return {}
    properties = body.get("properties") if isinstance(body.get("properties"), dict) else {}
    required = [k for k in body.get("required") or [] if isinstance(k, str)]
    return {key: _placeholder(properties.get(key), key, depth) for key in required[:20]}


def request_body(method: str, probe_body: Any, input_schema: Any) -> bytes | None:
    """What a probe sends: never an empty body with a body method."""
    if method.upper() not in BODY_METHODS:
        return None
    body = probe_body if isinstance(probe_body, (dict, list)) and probe_body else minimal_body(input_schema)
    encoded = json.dumps(body, separators=(",", ":")).encode()
    return encoded or b"{}"


def _decode_b64_json(value: str) -> Any:
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception:
        try:
            return json.loads(base64.b64decode(value + "=" * (-len(value) % 4)))
        except Exception:
            return None


def mpp_challenges(header: str | None) -> list[dict[str, Any]]:
    """`Payment method="tempo", intent="charge", request="<base64url JSON>"` offers."""
    if not header:
        return []
    offers = []
    for segment in re.split(r",\s*(?=Payment\s)", header):
        if not re.match(r"^\s*Payment\s", segment, re.IGNORECASE):
            continue
        params = {
            m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
            for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s,]+))',
                                 re.sub(r"^\s*Payment\s+", "", segment, flags=re.IGNORECASE))
        }
        request = _decode_b64_json(params.get("request") or "")
        if not isinstance(request, dict):
            continue
        method = (params.get("method") or "tempo").lower()
        details = request.get("methodDetails") if isinstance(request.get("methodDetails"), dict) else {}
        chain = details.get("chainId")
        network = TEMPO if method == "tempo" and chain in (None, 4217, "4217") else caip2(f"eip155:{chain}") if chain else None
        currency = request.get("currency") if isinstance(request.get("currency"), str) else None
        offers.append({
            "network": network, "scheme": (params.get("intent") or "charge").lower(),
            "amount": _int(request.get("amount")),
            "asset": currency.lower() if currency and currency.startswith("0x") else currency,
            "payee": _payee(request.get("recipient")),
            "payable": network is not None and same_asset(network, currency),
        })
    return offers


def read_challenge(status: int, headers: httpx.Headers, body: bytes) -> ProbeResult:
    """Classify one answer."""
    if 300 <= status < 400:
        return ProbeResult(False, status, "redirect", detail=(headers.get("location") or "")[:300] or None)
    if status != 402:
        if status in (400, 422):
            # The service is up and parsed the request, but refused the body we
            # could build for it before quoting. Not the same as being down.
            return ProbeResult(False, status, "probe_body_rejected")
        return ProbeResult(False, status, "no_challenge" if status < 400 else f"http_{status}")
    payload: Any = None
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    accepts = payload.get("accepts") if isinstance(payload, dict) else None
    if not accepts and headers.get("payment-required"):
        decoded = _decode_b64_json(headers["payment-required"])
        accepts = decoded.get("accepts") if isinstance(decoded, dict) else None
    x402 = [o for o in x402_offers(accepts) if o["payee"] and o["amount"] is not None]
    mpp = [o for o in mpp_challenges(headers.get("www-authenticate"))
           if o["network"] in (TEMPO, "eip155:8453") and o["payee"] and o["amount"] is not None]
    offers = x402 + mpp
    if not offers:
        any_challenge = bool(accepts) or "payment" in (headers.get("www-authenticate") or "").lower()
        return ProbeResult(False, status, "unsupported_network" if any_challenge else "invalid_challenge")
    offers.sort(key=lambda o: not o["payable"])
    best = offers[0]
    return ProbeResult(
        True, status, None, protocol="x402" if best in x402 else "mpp",
        network=best["network"], payee=best["payee"],
        pay_to=sorted({o["payee"] for o in offers}),
        quoted_amount_atomic=best["amount"], asset=best["asset"],
    )


@dataclass(frozen=True)
class ProbeTarget:
    resource_id: int
    method: str
    url: str
    probe_body: Any = None
    input_schema: Any = None


def probe(client: httpx.Client, target: ProbeTarget,
          allow: Callable[[str], bool] = public_endpoint) -> ProbeResult:
    if not allow(target.url):
        return ProbeResult(False, None, "not_public")
    method = target.method.upper()
    body = request_body(method, target.probe_body, target.input_schema)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    started = time.monotonic()
    try:
        with client.stream(method, target.url, content=body, headers=headers) as response:
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= BODY_LIMIT:
                    break
            result = read_challenge(response.status_code, response.headers, b"".join(chunks)[:BODY_LIMIT])
    except httpx.TimeoutException:
        result = ProbeResult(False, None, "timeout")
    except httpx.ConnectError as error:
        result = ProbeResult(False, None, "tls" if "SSL" in str(error) or "certificate" in str(error) else "connect",
                             detail=str(error)[:300])
    except httpx.HTTPError as error:
        result = ProbeResult(False, None, "http_error", detail=f"{type(error).__name__}: {error}"[:300])
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


def probe_all(targets: Iterable[ProbeTarget], *, allow: Callable[[str], bool] = public_endpoint,
              workers: int = WORKERS, per_host: int = PER_HOST,
              timeout: float = PROBE_TIMEOUT) -> list[tuple[ProbeTarget, ProbeResult]]:
    """Probe concurrently, at most ``per_host`` requests in flight per host."""
    targets = list(targets)
    locks: dict[str, threading.Semaphore] = defaultdict(lambda: threading.Semaphore(per_host))
    guard = threading.Lock()

    def host_of(url: str) -> str:
        return httpx.URL(url).host if url.startswith(("http://", "https://")) else url

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        def one(target: ProbeTarget) -> tuple[ProbeTarget, ProbeResult]:
            with guard:
                semaphore = locks[host_of(target.url)]
            with semaphore:
                try:
                    return target, probe(client, target, allow)
                except Exception as error:  # one bad answer never stops the batch
                    return target, ProbeResult(False, None, "probe_error", detail=f"{type(error).__name__}: {error}"[:300])

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return list(pool.map(one, targets))
