"""Solana primitives for x402 payment verification (docs/SCORING.md §3.4).

Kept dependency-light: base58 is implemented here, JSON-RPC uses stdlib
urllib like the EVM checks, and ed25519 verification uses ``cryptography``
(already installed through ``mcp`` → ``pyjwt[crypto]``).

Solana identifiers are case-sensitive base58 and are never lowercased.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
# Native Circle USDC on Solana mainnet; it is an SPL Token (not Token-2022)
# mint, so only the classic token program can move it.
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# "confirmed" = voted on by a supermajority; reviews arrive seconds after
# settlement, before "finalized" (~13 s) would find the transaction.
SOLANA_COMMITMENT = "confirmed"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: index for index, char in enumerate(_B58_ALPHABET)}
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")


def b58decode(value: str) -> bytes | None:
    if not isinstance(value, str) or not value or not _B58_RE.match(value):
        return None
    number = 0
    for char in value:
        number = number * 58 + _B58_INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + body


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _B58_ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def _decodes_to(value: Any, size: int) -> bool:
    if not isinstance(value, str):
        return False
    decoded = b58decode(value.strip())
    return decoded is not None and len(decoded) == size


def is_solana_address(value: Any) -> bool:
    """A base58 32-byte public key (wallet or program address)."""
    return _decodes_to(value, 32)


def is_solana_signature(value: Any) -> bool:
    """A base58 64-byte ed25519 signature (also a transaction id)."""
    return _decodes_to(value, 64)


def verify_ed25519(public_key: str, message: bytes, signature: str) -> bool:
    key = b58decode(public_key)
    sig = b58decode(signature)
    if key is None or sig is None or len(key) != 32 or len(sig) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(sig, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def rpc_get_transaction(rpc_url: str, signature: str) -> dict[str, Any] | None:
    """getTransaction in jsonParsed form; None when unreachable or not (yet)
    visible at SOLANA_COMMITMENT — both are retryable for the caller."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": SOLANA_COMMITMENT,
            },
        ],
    }
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "user-agent": "crowdcode-mcp"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    result = body.get("result") if isinstance(body, dict) else None
    return result if isinstance(result, dict) else None


@dataclass(frozen=True)
class SolanaTransfers:
    """Pinned-mint transfers into the payee's token accounts, by payer."""

    # payer (source token account owner) -> total base units sent to payee
    by_payer: dict[str, int] = field(default_factory=dict)
    payee_net_delta: int = 0
    slot: int | None = None
    fee_payer: str | None = None
    authorities: dict[str, str] = field(default_factory=dict)


def _account_keys(tx: dict[str, Any]) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = []
    for key in message.get("accountKeys") or []:
        keys.append(key.get("pubkey") if isinstance(key, dict) else key)
    # v0 transactions append address-lookup-table accounts after the static keys.
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    if not (message.get("accountKeys") and isinstance(message["accountKeys"][0], dict)):
        keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return [k if isinstance(k, str) else "" for k in keys]


def _token_accounts(tx: dict[str, Any], keys: list[str], mint: str) -> dict[str, tuple[str, int, int]]:
    """token account -> (owner, pre amount, post amount) for the pinned mint
    under the classic token program. Accounts created in the transaction have
    no pre balance (0)."""
    meta = tx.get("meta") or {}
    accounts: dict[str, list[Any]] = {}
    for phase, entries in (("pre", meta.get("preTokenBalances")), ("post", meta.get("postTokenBalances"))):
        for entry in entries or []:
            if not isinstance(entry, dict) or entry.get("mint") != mint:
                continue
            if entry.get("programId") not in (None, SPL_TOKEN_PROGRAM):
                continue
            index = entry.get("accountIndex")
            owner = entry.get("owner")
            if not isinstance(index, int) or not 0 <= index < len(keys) or not isinstance(owner, str):
                continue
            try:
                amount = int(((entry.get("uiTokenAmount") or {}).get("amount")))
            except (TypeError, ValueError):
                continue
            slot = accounts.setdefault(keys[index], [owner, 0, 0])
            if slot[0] != owner:
                slot[0] = ""  # owner changed mid-transaction: never trust it
            slot[1 if phase == "pre" else 2] = amount
    return {account: (owner, pre, post) for account, (owner, pre, post) in accounts.items() if owner}


def _instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    message = (tx.get("transaction") or {}).get("message") or {}
    out = [ix for ix in message.get("instructions") or [] if isinstance(ix, dict)]
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        out += [ix for ix in (inner or {}).get("instructions") or [] if isinstance(ix, dict)]
    return out


def find_usdc_transfers(tx: dict[str, Any], *, payee: str, mint: str = SOLANA_USDC_MINT) -> SolanaTransfers:
    """Sum parsed SPL ``transfer``/``transferChecked`` instructions of the pinned
    mint from any owner's token account into a token account owned by
    ``payee``. Owners and mints come from the runtime's token balance records,
    not from client data; the payee's net balance delta caps the total so a
    transfer cannot count for more than the payee actually received."""
    keys = _account_keys(tx)
    accounts = _token_accounts(tx, keys, mint)
    by_payer: dict[str, int] = {}
    authorities: dict[str, str] = {}
    for ix in _instructions(tx):
        if ix.get("programId") != SPL_TOKEN_PROGRAM:
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict) or parsed.get("type") not in {"transfer", "transferChecked"}:
            continue
        info = parsed.get("info") or {}
        if parsed["type"] == "transferChecked" and info.get("mint") != mint:
            continue
        source = accounts.get(info.get("source"))
        destination = accounts.get(info.get("destination"))
        if source is None or destination is None or destination[0] != payee:
            continue
        raw = info.get("amount") if parsed["type"] == "transfer" else (info.get("tokenAmount") or {}).get("amount")
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            continue
        if amount <= 0 or source[0] == payee:
            continue
        by_payer[source[0]] = by_payer.get(source[0], 0) + amount
        if isinstance(info.get("authority"), str):
            authorities[source[0]] = info["authority"]
    net = sum(post - pre for owner, pre, post in accounts.values() if owner == payee)
    if net < sum(by_payer.values()):
        # Payee also paid out in this transaction: never credit more than it kept.
        by_payer = {payer: min(amount, max(net, 0)) for payer, amount in by_payer.items()}
        by_payer = {payer: amount for payer, amount in by_payer.items() if amount > 0}
    return SolanaTransfers(
        by_payer=by_payer,
        payee_net_delta=net,
        slot=tx.get("slot") if isinstance(tx.get("slot"), int) else None,
        fee_payer=keys[0] if keys and keys[0] else None,
        authorities=authorities,
    )
