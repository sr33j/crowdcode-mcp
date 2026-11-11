"""HTTP clients: the signed CrowdCode owner API, x402 ``exact`` payments and a Base balance read.

Every network call this plugin makes is in this file, to these hosts only:

* the CrowdCode API origin (``base_url`` setting, default https://crowdcode.app):
  the signed owner API under ``/api/app/v1``, paid agent creation at
  ``/s/crowdcode/agents`` and the public ``/api/service-proposals`` list;
* a Base JSON-RPC endpoint (``base_rpc_url``, default https://mainnet.base.org)
  for one read-only ``balanceOf`` call when showing the wallet's USDC balance.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import wallet as walletlib

USER_AGENT = "crowdcode-hermes/0.1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ApiError(RuntimeError):
    def __init__(self, status: int, error: str, detail: Any = None):
        super().__init__(f"{status} {error}")
        self.status = status
        self.error = error
        self.detail = detail


def _decode_error(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = {"error": (response.text or "").strip()[:300] or response.reason_phrase}
    error = body.get("error") if isinstance(body, dict) else None
    return ApiError(response.status_code, str(error or response.reason_phrase), body)


def _json_body(payload: Any) -> bytes:
    return b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()


@dataclass
class CrowdCode:
    base_url: str
    wallet: walletlib.Wallet | None = None
    transport: httpx.BaseTransport | None = None  # tests inject a mock transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=TIMEOUT, headers={"user-agent": USER_AGENT}, transport=self.transport,
                            follow_redirects=False)

    @property
    def api_root(self) -> str:
        return self.base_url.rstrip("/") + "/api/app/v1"

    # -- signed owner API -------------------------------------------------
    def signed(self, method: str, path: str, payload: Any = None, extra_headers: dict | None = None) -> Any:
        """Call ``/api/app/v1<path>`` with a fresh signature. ``path`` may carry a query."""
        if self.wallet is None:
            raise walletlib.WalletMissing("No CrowdCode wallet yet. Run /crowdcode wallet init to create one.")
        url = self.api_root + path
        body = _json_body(payload)
        parts = urlsplit(url)
        signed_path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {**(extra_headers or {}), **self.wallet.sign_request(method, signed_path, body)}
        if body:
            headers["content-type"] = "application/json"
        with self._client() as http:
            response = http.request(method.upper(), url, content=body or None, headers=headers)
        if response.status_code >= 400:
            raise _decode_error(response)
        return response.json() if response.content else {}

    def signed_stream(self, path: str, chunk_size: int = 64 * 1024):
        """GET ``/api/app/v1<path>`` (signed, empty body) and yield the response body in chunks."""
        if self.wallet is None:
            raise walletlib.WalletMissing("No CrowdCode wallet yet. Run /crowdcode wallet init to create one.")
        url = self.api_root + path
        parts = urlsplit(url)
        signed_path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = self.wallet.sign_request("GET", signed_path, b"")
        with self._client() as http:
            with http.stream("GET", url, headers=headers, timeout=httpx.Timeout(300.0, connect=10.0)) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _decode_error(response)
                yield from response.iter_bytes(chunk_size)

    # -- public -----------------------------------------------------------
    def demand(self) -> Any:
        with self._client() as http:
            response = http.get(self.base_url.rstrip("/") + "/api/service-proposals")
        if response.status_code >= 400:
            raise _decode_error(response)
        return response.json()

    # -- x402 -------------------------------------------------------------
    def quote(self, url: str, payload: Any) -> dict:
        """POST without payment and return the x402 offer we would pay (Base USDC ``exact``)."""
        with self._client() as http:
            response = http.post(url, content=_json_body(payload), headers={"content-type": "application/json"})
        if response.status_code != 402:
            if response.status_code >= 400:
                raise _decode_error(response)
            raise ApiError(response.status_code, "expected a 402 payment challenge", response.text[:300])
        required = _payment_required(response)
        return {"required": required, "offer": _pick_base_exact(required)}

    def pay(self, url: str, payload: Any, quote: dict) -> tuple[int, Any, dict | None]:
        """Sign the quoted offer and POST again with ``PAYMENT-SIGNATURE``.

        Returns ``(status, body, settlement)``; settlement is the decoded
        ``PAYMENT-RESPONSE`` header when the server sent one.
        """
        if self.wallet is None:
            raise walletlib.WalletMissing("No CrowdCode wallet yet. Run /crowdcode wallet init to create one.")
        required, offer = quote["required"], quote["offer"]
        extra = offer.get("extra") or {}
        authorization, signature = self.wallet.authorize_transfer(
            to=offer["payTo"], value=int(offer["amount"]), valid_seconds=min(int(offer.get("maxTimeoutSeconds") or 300), 1500),
            asset=offer["asset"], chain_id=int(str(offer["network"]).split(":")[1]),
            token_name=extra.get("name", "USD Coin"), token_version=extra.get("version", "2"),
        )
        payment = {
            "x402Version": required.get("x402Version", 2),
            "resource": required.get("resource"),
            "accepted": offer,
            "payload": {"authorization": authorization, "signature": signature},
        }
        if required.get("extensions"):
            payment["extensions"] = required["extensions"]
        header = base64.b64encode(json.dumps(payment, separators=(",", ":")).encode()).decode()
        with self._client() as http:
            response = http.post(url, content=_json_body(payload),
                                 headers={"content-type": "application/json", "PAYMENT-SIGNATURE": header},
                                 timeout=httpx.Timeout(120.0, connect=10.0))
        settlement = None
        raw = response.headers.get("payment-response") or response.headers.get("x-payment-response")
        if raw:
            try:
                settlement = json.loads(base64.b64decode(raw))
            except ValueError:
                settlement = None
        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:300]}
        if response.status_code == 402:
            try:
                body = {"error": _payment_required(response).get("error") or "payment refused", **(body or {})}
            except ApiError:
                pass
        return response.status_code, body, settlement

    # -- chain ------------------------------------------------------------
    def usdc_balance(self, rpc_url: str, address: str) -> float:
        data = "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")
        call = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": walletlib.BASE_USDC, "data": data}, "latest"]}
        with self._client() as http:
            response = http.post(rpc_url, json=call)
        response.raise_for_status()
        result = response.json().get("result")
        if not isinstance(result, str):
            raise ApiError(502, "base rpc returned no result", response.json())
        return walletlib.units_to_usd(int(result, 16))


def _payment_required(response: httpx.Response) -> dict:
    raw = response.headers.get("payment-required")
    if raw:
        try:
            return json.loads(base64.b64decode(raw))
        except ValueError as exc:
            raise ApiError(402, "unreadable PAYMENT-REQUIRED header") from exc
    try:
        body = response.json()  # x402 v1 servers put the challenge in the body
    except ValueError as exc:
        raise ApiError(402, "402 without a payment challenge") from exc
    if not isinstance(body, dict) or "accepts" not in body:
        raise ApiError(402, "402 without a payment challenge", body)
    return body


def _pick_base_exact(required: dict) -> dict:
    for offer in required.get("accepts") or []:
        if (offer.get("scheme") == "exact" and offer.get("network") == walletlib.BASE_CAIP2
                and str(offer.get("asset", "")).lower() == walletlib.BASE_USDC.lower()):
            if "amount" not in offer and "maxAmountRequired" in offer:  # v1 spelling
                offer = {**offer, "amount": offer["maxAmountRequired"]}
            return offer
    raise ApiError(402, "no x402 exact USDC-on-Base offer in the challenge", required.get("accepts"))
