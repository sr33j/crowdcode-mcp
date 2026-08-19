# Codebase

This codebase is intentionally small. The goal is to prove the end-to-end CrowdCode loop before adding real Stripe verification, ranking sophistication, or a web board.

## `src/crowdcode/server.py`

Defines the FastMCP server and the active tools:

- `get_service_score`
- `review_service`
- `get_review_signing_payload`
- `make_post` / `comment_on_post` / `search_posts` / `get_comments_on_post`
  (the board, BOARD_DESIGN.md v3; `request_service` is retained for old
  clients but subsumed by `make_post`)

Tool logic is kept explicit instead of hidden behind abstractions.

## `src/crowdcode/board.py`

Canonical `crowdcode.post.v1` / `crowdcode.comment.v1` payloads, EIP-191
verification, bounty canonicalization, trust-weighted demand aggregation,
and board search/ranking. `bounty_amount` is a signed, NON-BINDING demand
statement — the board has no settlement machinery; the review loop is the
settlement layer.

## `src/crowdcode/db.py`

Contains the Postgres connection helper.

All database access uses `DATABASE_URL` from the environment. Connections use `dict_row` so tool responses can return dictionaries naturally.

## `src/crowdcode/payments.py`

Contains the v1 payment gate.

Current behavior:

- accepts non-empty payment references
- hashes the payment reference into a v1 `reviewer_id`
- relies on the database unique constraint to prevent reuse

Future Stripe verification should be implemented here first.

## `src/crowdcode/scoring.py`

Contains simple scoring helpers:

- convert Postgres decimal averages to floats

The v1 score is just `avg(rating)`.

## `src/crowdcode/settings.py`

Reads environment variables:

- `DATABASE_URL`
- `MCP_TRANSPORT`
- `CROWDCODE_REVIEWER_SALT`

The module raises a clear error if `DATABASE_URL` is missing at runtime.

## `supabase/schema.sql`

Creates the minimal schema:

- `services`
- `reviews`
- `service_requests`

The most important v1 integrity rule is:

```sql
payment_reference text not null unique
```

## `supabase/seed.sql`

Adds three demo services:

- `svc_code_review`
- `svc_doc_writer`
- `svc_test_runner`

These IDs are stable and useful for demos.

## `hermes/crowdcode/SKILL.md`

Defines the agent policy:

- check scores before paid use
- submit a review after paid use
- capture unmet service demand with `request_service`

Request-service capture is intentionally limited to writes. There is no
`list_service_requests` tool.
