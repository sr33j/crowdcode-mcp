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

import logging
import os
import time

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


# The sidecar redacts texts one at a time on a small CPU instance, and a cold
# process (every deploy clears its memo) takes far longer per text than a warm
# one. One large request therefore outlives any sensible timeout; small chunks
# keep each call short and let other callers interleave.
CHUNK_SIZE = 10
# Per-chunk timeout for background batches (cron, bulk exports) where nobody
# is waiting on a page load.
BATCH_TIMEOUT = 60.0
# A timed-out chunk keeps running in the sidecar and lands in its memo, so one
# retry usually succeeds immediately. Connection failures (a sidecar that is
# restarting after a deploy) get a short pause first.
ATTEMPTS = 2
RETRY_DELAY = 5.0

logger = logging.getLogger(__name__)


def _post_chunk(
    url: str, headers: dict[str, str], chunk: list[str], timeout: float
) -> list[str]:
    response = httpx.post(
        url.rstrip("/") + "/redact",
        json={"texts": chunk},
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    redacted = response.json()["texts"]
    if not isinstance(redacted, list) or len(redacted) != len(chunk):
        raise ValueError("redactor returned a mismatched batch")
    return redacted


def redact_texts(
    texts: list[str | None],
    *,
    fail_closed: bool,
    timeout: float = 10.0,
    allow_partial: bool = False,
    attempts: int = ATTEMPTS,
) -> list[str | None] | None:
    """Redact a batch of texts through the sidecar, CHUNK_SIZE texts per call.

    Returns a list aligned with the input (None entries preserved).
    `timeout` applies to each chunk; each chunk is tried `attempts` times.
    - Sidecar not configured: returns the input unchanged (enforcement off),
      unless REDACTION_REQUIRED=1, which treats it as failing.
    - Sidecar failing and fail_closed=True: raises RedactionUnavailable
      (callers reject the write).
    - Sidecar failing and fail_closed=False: returns None (callers must drop
      the free-text fields rather than leak them). With allow_partial=True,
      only the failed chunks' entries become None (dropped); None is returned
      only when every chunk failed.
    """
    url, token = _config()
    if url is None:
        if not redaction_required():
            return texts
        if fail_closed:
            raise RedactionUnavailable("redaction is required but REDACTOR_URL is not set")
        return None

    headers = {"content-type": "application/json"}
    if token is not None:
        headers["x-redactor-token"] = token

    # Only non-empty texts go over the wire.
    pending = [i for i, text in enumerate(texts) if text]
    out: list[str | None] = [None if text is None else "" for text in texts]
    chunks = [pending[i:i + CHUNK_SIZE] for i in range(0, len(pending), CHUNK_SIZE)]
    failed = 0
    for number, indexes in enumerate(chunks, start=1):
        payload = [texts[i] or "" for i in indexes]
        error: Exception | None = None
        for attempt in range(attempts):
            if attempt and isinstance(error, httpx.TransportError) and not isinstance(
                error, httpx.TimeoutException
            ):
                time.sleep(RETRY_DELAY)
            try:
                redacted = _post_chunk(url, headers, payload, timeout)
            except Exception as exc:
                error = exc
                continue
            for index, text in zip(indexes, redacted):
                out[index] = text
            error = None
            break
        if error is None:
            continue
        failed += 1
        logger.warning(
            "redaction chunk %d/%d (%d texts) failed after %d attempts: %s",
            number, len(chunks), len(indexes), attempts, type(error).__name__,
        )
        if fail_closed:
            raise RedactionUnavailable(str(error)) from error
        if not allow_partial:
            return None
        for index in indexes:
            out[index] = None

    if chunks and failed == len(chunks):
        return None
    return out
