"""The Python side of the TypeScript differential tests.

Reads one JSON request on stdin, ``{"cases": [{"op": ..., ...}, ...]}``, and
answers ``{"results": [...]}`` in the same order, computed with the Python
code the TypeScript ports claim to match byte for byte:

- ``src/crowdcode/payments.py`` and ``identity.py``: the review signing payload
  the backend rebuilds, and the EIP-191 recovery it verifies a review with;
- ``plugins/hermes/wallet.py``: the Hermes plugin's request signatures,
  EIP-3009 authorizations and dollar amounts;
- ``json.dumps``, which ``src/agents/pyjson.ts`` reimplements.

Run once per test file over every generated case (packages/mcp/test/
agents-signing.test.ts, signing-payload.test.ts). Needs eth-account, httpx,
psycopg, python-dotenv and cryptography (the imports of payments.py).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from eth_account import Account  # noqa: E402

_spec = importlib.util.spec_from_file_location("hermes_wallet", ROOT / "plugins" / "hermes" / "wallet.py")
wallet = importlib.util.module_from_spec(_spec)
sys.modules["hermes_wallet"] = wallet
_spec.loader.exec_module(wallet)


def _fixed(**values):
    """Pin wallet.py's clock and nonce source for one call."""
    wallet.time = types.SimpleNamespace(time=lambda: values["now"])
    wallet.secrets = types.SimpleNamespace(token_hex=lambda n: values["nonce"])


def _decode(value):
    """JSON in, Python values out: {"$float": "<repr>"} is a float."""
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return float(value["$float"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def review_payload(case):
    from crowdcode import payments
    from crowdcode.identity import ServiceIdentity

    identity = ServiceIdentity(service_name=None, **case["identity"])
    message = payments.canonical_review_payload(
        identity=identity,
        rating=case["rating"],
        reason=case["reason"],
        payment_reference=case["payment_reference"],
        review_nonce=case.get("review_nonce"),
    )
    recovered = payments._recover_eip191(message, case["signature"]) if case.get("signature") else None
    return {"message": message, "recovered": recovered}


def identity(case):
    from crowdcode.identity import build_identity

    try:
        built = build_identity(**case["args"])
    except ValueError as exc:
        return {"error": str(exc)}
    return {"identity": {k: getattr(built, k) for k in (
        "service_id", "api_endpoint", "payment_provider", "payment_target_ref", "directory_slug")}}


def sign_request(case):
    _fixed(now=case["now"], nonce=case["nonce"])
    signer = wallet.Wallet(Account.from_key(case["key"]))
    return {"headers": signer.sign_request(case["method"], case["path"], bytes.fromhex(case["body_hex"]),
                                           now=case["now"])}


def authorize(case):
    _fixed(now=case["now"], nonce=case["nonce"][2:])
    signer = wallet.Wallet(Account.from_key(case["key"]))
    try:
        authorization, signature = signer.authorize_transfer(
            to=case["to"], value=int(case["value"]), valid_seconds=case["valid_seconds"], asset=case["asset"],
            chain_id=case["chain_id"], token_name=case["token_name"], token_version=case["token_version"])
    except ValueError as exc:
        return {"error": str(exc)}
    return {"authorization": authorization, "signature": signature}


def usd_to_units(case):
    amount = _decode(case["amount"])
    try:
        return {"units": str(wallet.usd_to_units(amount))}
    except ValueError as exc:
        return {"error": str(exc)}


def dumps(case):
    separators = (",", ":") if case["compact"] else None
    return {"text": json.dumps(_decode(case["value"]), separators=separators, sort_keys=case["sort_keys"])}


OPS = {f.__name__: f for f in (review_payload, identity, sign_request, authorize, usd_to_units, dumps)}

request = json.load(sys.stdin)
json.dump({"results": [OPS[case["op"]](case) for case in request["cases"]]}, sys.stdout)
