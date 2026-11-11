"""The plugin's own EVM wallet: key storage, request signing and EIP-3009 authorizations.

The private key lives only in Hermes's credential store. It is declared as a
``secret`` in ``plugin.yaml`` (``CROWDCODE_WALLET_KEY``), written with
``hermes_cli.config.save_env_value`` and read back with
``agent.secret_scope.get_secret``, the route the plugin guide documents for
plugin secrets. Nothing else in this plugin ever sees the key: callers get an
address and signatures.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

KEY_ENV = "CROWDCODE_WALLET_KEY"

BASE_CHAIN_ID = 8453
BASE_CAIP2 = f"eip155:{BASE_CHAIN_ID}"
# Circle's USDC on Base. Its EIP-712 domain is ("USD Coin", "2").
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6

_TRANSFER_WITH_AUTHORIZATION = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


class WalletMissing(RuntimeError):
    """No key is configured yet."""


def _read_secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret
    except Exception:  # pragma: no cover - only outside Hermes (tests use the stub)
        import os
        return os.environ.get(name)
    return get_secret(name)


def _write_secret(name: str, value: str) -> None:
    from hermes_cli import config

    # save_env_value returns quietly when the .env write lock refuses a key; ask first.
    if hasattr(config, "require_env_writable"):
        config.require_env_writable(name, "set")
    config.save_env_value(name, value)


def usd_to_units(amount_usd: float | str) -> int:
    """Dollars to USDC base units, refusing anything that is not a positive, cent-sane amount."""
    from decimal import Decimal, InvalidOperation, ROUND_DOWN

    try:
        value = Decimal(str(amount_usd))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not an amount: {amount_usd!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be greater than zero")
    units = int((value * 10**USDC_DECIMALS).to_integral_value(rounding=ROUND_DOWN))
    if units <= 0:
        raise ValueError("amount is below one USDC base unit")
    return units


def units_to_usd(units: int | str) -> float:
    return int(units) / 10**USDC_DECIMALS


@dataclass(frozen=True)
class Wallet:
    """A loaded key. Construct with :func:`load`; never serialised."""

    _account: object

    @property
    def address(self) -> str:
        return self._account.address  # checksummed

    @property
    def caip10(self) -> str:
        return f"{BASE_CAIP2}:{self.address}"

    def export_private_key(self) -> str:
        """Only for the terminal-only ``hermes crowdcode wallet export --yes-show-private-key``."""
        return "0x" + self._account.key.hex().removeprefix("0x")

    def sign_message(self, text: str) -> str:
        """EIP-191 personal_sign over UTF-8 text, 0x-hex."""
        from eth_account.messages import encode_defunct

        signed = self._account.sign_message(encode_defunct(text=text))
        return "0x" + signed.signature.hex().removeprefix("0x")

    def sign_request(self, method: str, path: str, body: bytes, *, now: int | None = None) -> dict[str, str]:
        """Headers for the CrowdCode signed owner API (Wallet API v1)."""
        timestamp = str(int(now if now is not None else time.time()))
        nonce = secrets.token_hex(16)
        digest = hashlib.sha256(body).hexdigest() if body else ""
        message = "\n".join(["CrowdCode API request", f"{method.upper()} {path}", digest, timestamp, nonce])
        return {
            "X-CrowdCode-Address": self.caip10,
            "X-CrowdCode-Timestamp": timestamp,
            "X-CrowdCode-Nonce": nonce,
            "X-CrowdCode-Signature": self.sign_message(message),
        }

    def authorize_transfer(
        self,
        *,
        to: str,
        value: int,
        valid_seconds: int,
        asset: str = BASE_USDC,
        chain_id: int = BASE_CHAIN_ID,
        token_name: str = "USD Coin",
        token_version: str = "2",
    ) -> tuple[dict[str, str], str]:
        """Sign an EIP-3009 ``transferWithAuthorization`` (the x402 ``exact`` payload).

        Returns ``(authorization, signature)`` with string fields, as x402 and
        the deposits endpoint expect. The authorization can only move exactly
        ``value`` to ``to`` once, before ``validBefore``.
        """
        from eth_utils import to_checksum_address

        if value <= 0:
            raise ValueError("value must be positive")
        now = int(time.time())
        authorization = {
            "from": self.address,
            "to": to_checksum_address(to),
            "value": str(int(value)),
            "validAfter": "0",
            "validBefore": str(now + max(30, int(valid_seconds))),
            "nonce": "0x" + secrets.token_hex(32),
        }
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": _TRANSFER_WITH_AUTHORIZATION,
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": token_name,
                "version": token_version,
                "chainId": int(chain_id),
                "verifyingContract": to_checksum_address(asset),
            },
            "message": {
                "from": authorization["from"],
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": 0,
                "validBefore": int(authorization["validBefore"]),
                "nonce": bytes.fromhex(authorization["nonce"][2:]),
            },
        }
        from eth_account.messages import encode_typed_data

        signed = self._account.sign_message(encode_typed_data(full_message=typed))
        return authorization, "0x" + signed.signature.hex().removeprefix("0x")


def load() -> Wallet:
    key = (_read_secret(KEY_ENV) or "").strip()
    if not key:
        raise WalletMissing("No CrowdCode wallet yet. Run /crowdcode wallet init to create one.")
    from eth_account import Account

    try:
        return Wallet(Account.from_key(key))
    except Exception as exc:
        # Never echo the value back.
        raise WalletMissing(f"{KEY_ENV} is set but is not a valid EVM private key") from exc


def exists() -> bool:
    return bool((_read_secret(KEY_ENV) or "").strip())


def create() -> Wallet:
    """Generate a fresh key and store it through Hermes. Refuses to replace an existing one."""
    if exists():
        raise RuntimeError("A CrowdCode wallet already exists; it was not replaced.")
    from eth_account import Account

    account = Account.create(secrets.token_hex(32))
    key = "0x" + account.key.hex().removeprefix("0x")
    _write_secret(KEY_ENV, key)
    # Never hand out an address whose key did not persist: funds sent to it would be lost.
    if (_read_secret(KEY_ENV) or "").strip() != key:
        raise RuntimeError("Hermes did not store the new wallet key, so no wallet was created. "
                           "Check that this profile's .env is writable.")
    return Wallet(account)
