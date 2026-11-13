"""Login mode: the CrowdCode account this Hermes profile signed in to, through Hermes's own OAuth support.

``plugin.yaml`` declares the OAuth client (``oauth: [{id: crowdcode, …}]``). The user signs in once with
``hermes auth add crowdcode:crowdcode``: Hermes runs the PKCE login against https://crowdcode.app/connect,
stores the grant in its profile and refreshes it. Every request here goes through
``hermes_cli.plugin_provider_requests.credentialed_provider_request``, which attaches the bearer token
itself and only for ``crowdcode.app``. This plugin never sees, stores or refreshes the token
(catalog rule 11).

The account's agents are driven through the same ``/api/app/v1`` routes as in wallet mode; what the
grant allows (scopes, agents, a daily cap) is enforced by CrowdCode.
"""
from __future__ import annotations

import json
from typing import Any, Iterator

from . import client as clientlib

PROVIDER = "crowdcode"  # the ``id`` of the oauth entry in plugin.yaml
CLIENT_ID_ENV = "CROWDCODE_OAUTH_CLIENT_ID"
CLIENT_ID = "hermes"  # CrowdCode's public client id for Hermes (PKCE, no secret)
SIGN_IN = "hermes auth add crowdcode:crowdcode"
SIGN_OUT = "hermes auth logout crowdcode:crowdcode"


class NotSignedIn(RuntimeError):
    """No usable CrowdCode sign-in in this Hermes profile."""


def _request(method: str, url: str, *, json_body: Any = None, headers: dict | None = None, timeout: float = 30.0):
    """Hermes's credentialed request; isolated so tests can stand in for it."""
    try:
        from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request
    except ImportError as exc:  # pragma: no cover - only outside Hermes
        raise NotSignedIn("Signing in to a CrowdCode account needs Hermes 0.23 or newer.") from exc
    try:
        return credentialed_provider_request(PROVIDER, method, url, json=json_body, headers=headers, timeout=timeout)
    except ProviderNotSignedIn as exc:
        raise NotSignedIn(str(exc)) from None


def client_id_ready() -> bool:
    from .wallet import _read_secret

    return bool((_read_secret(CLIENT_ID_ENV) or "").strip())


def prepare_client_id() -> bool:
    """Store CrowdCode's public client id in the plugin's own variable, which ``hermes auth add`` reads.

    It is not a secret (a public PKCE client). Returns True when it was written now.
    """
    if client_id_ready():
        return False
    from .wallet import _write_secret

    _write_secret(CLIENT_ID_ENV, CLIENT_ID)
    return True


class AccountClient:
    """The ``signed``/``signed_stream`` interface of :class:`client.CrowdCode`, authenticated by Hermes."""

    wallet = None  # no key in login mode

    def __init__(self, base_url: str, request=None):
        self.base_url = base_url.rstrip("/")
        self._request = request or _request

    @property
    def api_root(self) -> str:
        return self.base_url + "/api/app/v1"

    def _call(self, method: str, path: str, payload: Any = None, extra_headers: dict | None = None, timeout: float = 30.0):
        headers = dict(extra_headers or {})
        try:
            response = self._request(method.upper(), self.api_root + path, json_body=payload, headers=headers, timeout=timeout)
        except PermissionError as exc:
            raise NotSignedIn(f"{exc}. Account mode works only against https://crowdcode.app (the base_url setting).") from None
        if response.status >= 400:
            try:
                body = json.loads(response.body or b"{}")
            except ValueError:
                body = {"error": (response.body or b"")[:300].decode("utf-8", "replace")}
            error = body.get("error") if isinstance(body, dict) else None
            raise clientlib.ApiError(response.status, str(error or response.status), body)
        return response

    def signed(self, method: str, path: str, payload: Any = None, extra_headers: dict | None = None) -> Any:
        response = self._call(method, path, payload, extra_headers)
        return json.loads(response.body) if response.body else {}

    def signed_stream(self, path: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        # Hermes hands back the whole body; give it to the writer in chunks like the streaming client does.
        body = self._call("GET", path, timeout=300.0).body or b""
        for start in range(0, len(body), chunk_size):
            yield body[start:start + chunk_size]
