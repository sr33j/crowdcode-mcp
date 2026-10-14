"""Client for the redaction sidecar (services/redactor).

Server-side enforcement backstop: the recommended crowdcode-mcp client
already redacts on the user's machine, but direct HTTP callers can bypass it,
so the backend re-redacts free text before it is stored (ingest, fail-closed)
and before stored text fans out to other users or third parties (egress,
fail-closed by omission).

Configuration: REDACTOR_URL (e.g. http://127.0.0.1:8090). When unset,
enforcement is disabled and text passes through unchanged — deployments
opt in by running the sidecar. REDACTOR_TOKEN adds a shared-secret header.
REDACTION_REQUIRED=1 (set in production) treats a missing REDACTOR_URL as an
unavailable sidecar, so a lost configuration rejects writes and drops egress
text instead of silently passing it through.
"""

from __future__ import annotations

import os

import httpx


class RedactionUnavailable(Exception):
    """The sidecar is configured but unreachable/failing."""


def _config() -> tuple[str | None, str | None]:
    url = os.environ.get("REDACTOR_URL", "").strip() or None
    token = os.environ.get("REDACTOR_TOKEN", "").strip() or None
    return url, token


def redaction_required() -> bool:
    return os.environ.get("REDACTION_REQUIRED", "").strip() == "1"


def redaction_enabled() -> bool:
    return _config()[0] is not None


def redact_texts(
    texts: list[str | None],
    *,
    fail_closed: bool,
    timeout: float = 10.0,
) -> list[str | None] | None:
    """Redact a batch of texts through the sidecar.

    Returns a list aligned with the input (None entries preserved).
    - Sidecar not configured: returns the input unchanged (enforcement off),
      unless REDACTION_REQUIRED=1, which treats it as failing.
    - Sidecar failing and fail_closed=True: raises RedactionUnavailable
      (callers reject the write).
    - Sidecar failing and fail_closed=False: returns None (callers must drop
      the free-text fields rather than leak them).
    """
    url, token = _config()
    if url is None:
        if not redaction_required():
            return texts
        if fail_closed:
            raise RedactionUnavailable("redaction is required but REDACTOR_URL is not set")
        return None

    payload = [text or "" for text in texts]
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["x-redactor-token"] = token

    try:
        response = httpx.post(
            url.rstrip("/") + "/redact",
            json={"texts": payload},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        redacted = response.json()["texts"]
        if not isinstance(redacted, list) or len(redacted) != len(texts):
            raise ValueError("redactor returned a mismatched batch")
    except Exception as exc:
        if fail_closed:
            raise RedactionUnavailable(str(exc)) from exc
        return None

    return [
        redacted[i] if texts[i] is not None else None for i in range(len(texts))
    ]
