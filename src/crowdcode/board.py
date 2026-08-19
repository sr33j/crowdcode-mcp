"""The public board: signed demand posts and comments (BOARD_DESIGN.md v3).

The board is an append-only log of wallet-signed posts. A post states a
capability gap; a comment adds discussion or piles demand onto an existing
request. `bounty_amount` is a signed, NON-BINDING demand signal — never
escrowed, never enforced, never paid out. There is no settlement machinery
here: the existing review system is the settlement layer.

Canonical payloads (`crowdcode.post.v1`, `crowdcode.comment.v1`) follow the
same cross-language contract as reviews: Python json.dumps(sort_keys=True,
separators=(",", ":")) semantics, EIP-191 personal-message signing, text
entering the payload only as a sha256 hash of the (redacted) text. Post ids
are content-addressed: `post_` + sha256(canonical payload)[:20], which makes
client retries idempotent and references verifiable.

Pure functions only in this module's canonical/validation half; the DB
helpers below take an open connection and never commit.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct

from crowdcode.scoring import TrustRow, effective_weight

POST_PAYLOAD_TYPE = "crowdcode.post.v1"
COMMENT_PAYLOAD_TYPE = "crowdcode.comment.v1"

POST_ID_RE = re.compile(r"^post_[0-9a-f]{20}$")
NONCE_RE = re.compile(r"^[0-9a-f]{16,64}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TEXT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BOUNTY_RE = re.compile(r"^[0-9]+(\.[0-9]{1,6})?$")
EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Hard field caps (TODO_SECURITY P1: input length caps). Applied to the raw
# input before redaction so oversized text never reaches the redactor.
MAX_POST_TEXT_CHARS = 4000
MAX_COMMENT_TEXT_CHARS = 2000
MAX_SEARCH_QUERY_CHARS = 500

# Non-binding stated demand cap, in USDC. A statement above this is noise,
# not signal.
MAX_BOUNTY_USD = 1_000_000

# Signed timestamps must be near server time: stale payloads cannot be
# replayed later, and the timestamp+nonce pair keeps deliberate reposts of
# identical text from colliding on the content-addressed id.
MAX_TIMESTAMP_SKEW_SECONDS = 600

# Board ranking recency half-life (days). Deliberately much shorter than the
# review half-life: there is no expiry field — decay does the work.
BOARD_DECAY_HALF_LIFE_DAYS = 30.0


class BoardValidationError(ValueError):
    """Raised for any invalid board input; the message is agent-facing."""


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def canonical_bounty_amount(value: str | int | float | None) -> str | None:
    """Canonical decimal-string form of a stated USDC amount.

    String-based on purpose (no Decimal/float round-trips) so the TypeScript
    client can reproduce it byte-for-byte: strip, validate
    ^[0-9]+(\\.[0-9]{1,6})?$, drop leading zeros in the integer part and
    trailing zeros in the fraction. "0" is a valid statement (an upvote).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise BoardValidationError("bounty_amount must be a decimal USDC string")
    if isinstance(value, (int, float)):
        value = format(value, "f") if isinstance(value, float) else str(value)
    cleaned = value.strip()
    if cleaned == "":
        return None
    if not BOUNTY_RE.match(cleaned):
        raise BoardValidationError(
            "bounty_amount must be a decimal USDC string like '5' or '0.25' "
            "(up to 6 decimal places)"
        )
    if "." in cleaned:
        integer, fraction = cleaned.split(".", 1)
        fraction = fraction.rstrip("0")
    else:
        integer, fraction = cleaned, ""
    integer = integer.lstrip("0") or "0"
    if int(integer) > MAX_BOUNTY_USD:
        raise BoardValidationError(
            f"bounty_amount must be at most {MAX_BOUNTY_USD} USDC"
        )
    return f"{integer}.{fraction}" if fraction else integer


def normalize_wallet(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not EVM_ADDRESS_RE.match(cleaned):
        return None
    return "0x" + cleaned[2:].lower()


def canonical_post_payload(
    *,
    wallet: str,
    text: str,
    bounty_amount: str | None,
    timestamp: str,
    nonce: str,
    parent_post_id: str | None = None,
) -> str:
    """The exact string that is EIP-191 signed. Text enters as a hash of the
    (already redacted) text — same rule as review reason_hash."""
    payload: dict[str, Any] = {
        "type": COMMENT_PAYLOAD_TYPE if parent_post_id else POST_PAYLOAD_TYPE,
        "wallet": wallet,
        "text_hash": text_hash(text),
        "bounty_amount": bounty_amount,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    if parent_post_id:
        payload["parent_post_id"] = parent_post_id
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def post_id_from_payload(payload: str) -> str:
    return "post_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class VerifiedBoardWrite:
    wallet: str
    text: str
    bounty_amount: str | None
    timestamp: str
    nonce: str
    parent_post_id: str | None
    payload: str
    post_id: str
    signature: str


def verify_board_write(
    *,
    wallet: Any,
    text: Any,
    bounty_amount: Any,
    timestamp: Any,
    nonce: Any,
    signature: Any,
    parent_post_id: str | None = None,
    max_text_chars: int = MAX_POST_TEXT_CHARS,
    now: datetime | None = None,
) -> VerifiedBoardWrite:
    """Validate all inputs, rebuild the canonical payload server-side, and
    require the EIP-191 signature to recover to `wallet`. The server never
    parses a client-provided message — it reconstructs and byte-compares,
    exactly like review verification."""
    if not isinstance(text, str) or not text.strip():
        raise BoardValidationError("text is required")
    if len(text) > max_text_chars:
        raise BoardValidationError(
            f"text must be at most {max_text_chars} characters"
        )
    normalized_wallet = normalize_wallet(wallet)
    if normalized_wallet is None:
        raise BoardValidationError("wallet must be an EVM 0x address")
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce.strip()):
        raise BoardValidationError("nonce must be 16-64 lowercase hex characters")
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.match(timestamp.strip()):
        raise BoardValidationError(
            "timestamp must look like 2026-08-19T12:00:00Z (UTC, second precision)"
        )
    if not isinstance(signature, str) or not signature.strip():
        raise BoardValidationError("signature is required")
    if parent_post_id is not None and not POST_ID_RE.match(parent_post_id):
        raise BoardValidationError("post_id must look like post_<20 hex chars>")

    cleaned_timestamp = timestamp.strip()
    parsed = datetime.strptime(cleaned_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    reference = now or datetime.now(UTC)
    if abs((reference - parsed).total_seconds()) > MAX_TIMESTAMP_SKEW_SECONDS:
        raise BoardValidationError(
            "timestamp is too far from server time; rebuild and re-sign the "
            "payload with a current UTC timestamp"
        )

    canonical_amount = canonical_bounty_amount(bounty_amount)
    payload = canonical_post_payload(
        wallet=normalized_wallet,
        text=text,
        bounty_amount=canonical_amount,
        timestamp=cleaned_timestamp,
        nonce=nonce.strip(),
        parent_post_id=parent_post_id,
    )
    recovered = _recover_eip191(payload, signature)
    if recovered is None or recovered != normalized_wallet:
        raise BoardValidationError(
            "signature does not match the canonical payload for this wallet; "
            "crowdcode-mcp signs board writes automatically — pass the same "
            "text, bounty_amount, timestamp, and nonce that were signed"
        )

    return VerifiedBoardWrite(
        wallet=normalized_wallet,
        text=text,
        bounty_amount=canonical_amount,
        timestamp=cleaned_timestamp,
        nonce=nonce.strip(),
        parent_post_id=parent_post_id,
        payload=payload,
        post_id=post_id_from_payload(payload),
        signature=signature.strip(),
    )


def _recover_eip191(message: str, signature: str) -> str | None:
    try:
        recovered = Account.recover_message(
            encode_defunct(text=message),
            signature=signature.strip(),
        )
    except Exception:
        return None
    return normalize_wallet(recovered)


# ---------------------------------------------------------------------------
# Derived state: demand aggregation and ranking (projections, rebuildable
# from the signed-post log).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandSummary:
    total_stated_usd: float
    trusted_stated_usd: float
    num_backers: int


def aggregate_demand(
    stated: list[tuple[str, str]],
    trust_map: dict[str, TrustRow],
) -> DemandSummary:
    """Trust-weighted stated demand for one thread.

    `stated` is (wallet, canonical bounty string) for the post and all its
    comments. Per wallet only the MAX stated amount counts — restating a
    number five times is not five times the demand (mirrors the daily review
    bucket cap). trusted_stated_usd weights each wallet's amount by its
    existing review-trust; unproven wallets contribute to the headline total
    but zero to the trusted figure — shown separately, like unproven reviews.
    """
    per_wallet: dict[str, float] = {}
    for wallet, amount in stated:
        value = float(amount)
        if wallet not in per_wallet or value > per_wallet[wallet]:
            per_wallet[wallet] = value
    total = sum(per_wallet.values())
    trusted = sum(
        value * effective_weight(trust_map.get(wallet))
        for wallet, value in per_wallet.items()
    )
    return DemandSummary(
        total_stated_usd=round(total, 6),
        trusted_stated_usd=round(trusted, 6),
        num_backers=len(per_wallet),
    )


def board_decay(created_at: datetime, now: datetime) -> float:
    age_days = (now - created_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / BOARD_DECAY_HALF_LIFE_DAYS)


def rank_score(
    similarity: float, trusted_stated_usd: float, created_at: datetime, now: datetime
) -> float:
    """rank = similarity x log-scaled trusted demand x recency decay.

    The demand factor is 1 + ln(1 + trusted USD) so zero-bounty posts still
    rank by relevance instead of vanishing.
    """
    demand_factor = 1.0 + math.log1p(max(0.0, trusted_stated_usd))
    return similarity * demand_factor * board_decay(created_at, now)


# ---------------------------------------------------------------------------
# Database helpers. All take an open dict_row connection; the caller commits.
# ---------------------------------------------------------------------------


def acquire_wallet_lock(conn: Any, wallet: str) -> None:
    """Serialize this wallet's board writes for the rest of the transaction so
    the rate-limit count-then-insert cannot race with itself."""
    conn.execute(
        "select pg_advisory_xact_lock(hashtextextended('crowdcode_board:' || %s, 0))",
        (wallet,),
    )


def load_board_trust_map(conn: Any, wallets: set[str]) -> dict[str, TrustRow]:
    if not wallets:
        return {}
    rows = conn.execute(
        """
        select wallet_address, raw_trust, is_seed, slashed_at
        from wallet_users
        where wallet_address = any(%s)
        """,
        (list(wallets),),
    ).fetchall()
    return {
        row["wallet_address"]: TrustRow(
            raw_trust=float(row["raw_trust"]),
            is_seed=bool(row["is_seed"]),
            slashed=row["slashed_at"] is not None,
        )
        for row in rows
    }


def thread_demand(
    conn: Any, post_ids: list[str], now: datetime | None = None
) -> dict[str, DemandSummary]:
    """Demand summaries for a batch of top-level posts, computed on read.
    v1 scale makes a materialized projection unnecessary."""
    if not post_ids:
        return {}
    rows = conn.execute(
        """
        select coalesce(parent_post_id, id) as thread_id, wallet,
               bounty_amount::text as bounty_amount
        from board_posts
        where bounty_amount is not null
          and coalesce(parent_post_id, id) = any(%s)
        """,
        (post_ids,),
    ).fetchall()
    wallets = {row["wallet"] for row in rows}
    trust_map = load_board_trust_map(conn, wallets)
    grouped: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["thread_id"], []).append(
            (row["wallet"], row["bounty_amount"])
        )
    empty = DemandSummary(0.0, 0.0, 0)
    return {
        post_id: (
            aggregate_demand(grouped[post_id], trust_map)
            if post_id in grouped
            else empty
        )
        for post_id in post_ids
    }


def search_board_posts(
    conn: Any, query: str, *, limit: int, now: datetime
) -> list[dict[str, Any]]:
    """FTS over posts AND comments, grouped to the top-level thread, ranked by
    similarity x trusted demand x recency (rank_score). Semantic embeddings
    are a later upgrade; websearch FTS is the v1 index."""
    candidates = conn.execute(
        """
        select thread.id, thread.wallet, thread.text,
               thread.bounty_amount::text as bounty_amount, thread.created_at,
               max(ts_rank(matched.text_tsv, q)) as similarity,
               (select count(*)::int from board_posts c
                 where c.parent_post_id = thread.id) as num_comments
        from board_posts matched
        join board_posts thread
          on thread.id = coalesce(matched.parent_post_id, matched.id),
             websearch_to_tsquery('english', %s) q
        where matched.text_tsv @@ q
        group by thread.id
        order by max(ts_rank(matched.text_tsv, q)) desc
        limit %s
        """,
        (query, max(limit * 5, 25)),
    ).fetchall()
    demand = thread_demand(conn, [row["id"] for row in candidates], now)
    ranked = []
    for row in candidates:
        summary = demand[row["id"]]
        ranked.append(
            {
                "post_id": row["id"],
                "text": row["text"],
                "wallet": row["wallet"],
                "bounty_amount": row["bounty_amount"],
                "created_at": row["created_at"],
                "num_comments": row["num_comments"],
                "total_stated_usd": summary.total_stated_usd,
                "trusted_stated_usd": summary.trusted_stated_usd,
                "num_backers": summary.num_backers,
                "rank": rank_score(
                    float(row["similarity"] or 0.0),
                    summary.trusted_stated_usd,
                    row["created_at"],
                    now,
                ),
            }
        )
    ranked.sort(key=lambda item: item["rank"], reverse=True)
    return ranked[:limit]


def search_services(conn: Any, query: str, *, limit: int) -> list[dict[str, Any]]:
    """Supply side of the single search: existing catalog services matching the
    query, so 'found a service? buy it' and 'found a request? add demand'
    come back from one call."""
    return conn.execute(
        """
        select s.id as service_id, s.name, s.directory_slug,
               s.canonical_endpoint, s.payment_provider, s.score, s.n_eff,
               ts_rank(
                 to_tsvector('english',
                   concat_ws(' ', s.name, s.directory_slug, s.canonical_endpoint)),
                 q) as similarity
        from services s, websearch_to_tsquery('english', %s) q
        where to_tsvector('english',
                concat_ws(' ', s.name, s.directory_slug, s.canonical_endpoint)) @@ q
        order by similarity desc, s.score desc
        limit %s
        """,
        (query, limit),
    ).fetchall()


def similar_posts(
    conn: Any, text: str, *, exclude_post_id: str, limit: int, now: datetime
) -> list[dict[str, Any]]:
    """Near-duplicate steering for make_post: existing threads matching the
    new post's own words. websearch_to_tsquery is too strict for whole
    paragraphs, so match on an OR of the text's lexemes instead."""
    rows = search_similar_by_lexemes(conn, text, exclude_post_id, limit)
    if not rows:
        return []
    demand = thread_demand(conn, [row["id"] for row in rows], now)
    return [
        {
            "post_id": row["id"],
            "text_excerpt": (row["text"][:280] + "…")
            if len(row["text"]) > 280
            else row["text"],
            "total_stated_usd": demand[row["id"]].total_stated_usd,
            "trusted_stated_usd": demand[row["id"]].trusted_stated_usd,
            "num_backers": demand[row["id"]].num_backers,
        }
        for row in rows
    ]


def search_similar_by_lexemes(
    conn: Any, text: str, exclude_post_id: str, limit: int
) -> list[dict[str, Any]]:
    row = conn.execute(
        """
        select string_agg('''' || replace(lexeme, '''', '''''') || '''', ' | ') as q
        from unnest(to_tsvector('english', %s))
        """,
        (text,),
    ).fetchone()
    lexeme_query = row["q"] if row else None
    if not lexeme_query:
        return []
    return conn.execute(
        """
        select id, text, ts_rank(text_tsv, to_tsquery('english', %s)) as similarity
        from board_posts
        where parent_post_id is null
          and id != %s
          and text_tsv @@ to_tsquery('english', %s)
        order by similarity desc
        limit %s
        """,
        (lexeme_query, exclude_post_id, lexeme_query, limit),
    ).fetchall()


def log_board_event(
    conn: Any,
    *,
    tool: str,
    wallet: str | None,
    post_id: str | None,
    query: str | None,
    metadata: dict[str, Any],
) -> None:
    """Study instrumentation (BOARD_DESIGN.md §6.6): one row per board tool
    call, feeding the searches->posts funnel, duplicate-rate, and
    demand->supply conversion analysis. Free text in `query` is already
    redacted by the caller's ingest path."""
    from psycopg.types.json import Jsonb

    conn.execute(
        """
        insert into board_events (tool, wallet, post_id, query, metadata)
        values (%s, %s, %s, %s, %s)
        """,
        (tool, wallet, post_id, query, Jsonb(metadata)),
    )
