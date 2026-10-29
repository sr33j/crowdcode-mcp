# Codebase

This codebase is intentionally small. The goal is to prove the end-to-end CrowdCode loop before adding real Stripe verification, ranking sophistication, or a web board.

## `src/crowdcode/server.py`

Defines the FastMCP server and the active v1 tools:

- `request_service`
- `get_service_score`
- `review_service`

Tool logic is kept explicit instead of hidden behind abstractions because v1 has very little behavior.

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

## `src/crowdcode/solana.py`

Solana primitives for x402 Solana verification: base58, ed25519 verification
(`cryptography`), `getTransaction` over JSON-RPC, and extraction of pinned-USDC
SPL transfers into the payee's token accounts. Payer linkage
(`payer_signature`) and admission rules live in `payments.py`
(docs/SCORING.md §3.4 rule 8).

## `src/crowdcode/review_sql.py`

The SQL predicates that decide which reviews are published and weighed, and
the `weight_exclusion` values. One definition, shared by `server.py` and
`cron.py` — the cron deliberately does not import the MCP app, and two copies
of "which reviews count" would drift.

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

## `src/crowdcode/demand.py`

The request_service v2 contract (`validate_request_fields`: input, output,
problem, price_per_call), the copy guard every generated public text passes
(`SourceIndex`: no eight-word run shared with a private request or review),
and the willingness-to-pay distribution.

## `src/crowdcode/proposals.py`

Nightly: maps pre-v2 free-text requests into the v2 fields, groups every
request into generalized service proposals with a product review, and stores
the published payload in `app_cache` (`service_proposals`). Raw request and
review text is never published; only summaries and proposals are.

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

## `scripts/`

Operator scripts, dry-run by default and `--apply` to write:
`fix_machine_payees.py` repairs a machine-payment service whose stored payee is
not an address, reading the real one from the service's own 402 (or marking the
service `payee_rotates` when it issues a fresh one per request);
`exclude_platform_defect_reviews.py` marks reviews that rate a failure
CrowdCode caused (docs/SCORING.md §3.6); `backfill_redaction.py` redacts
pre-enforcement free text.

## `hermes/crowdcode/SKILL.md`

Defines the agent policy:

- check scores before paid use
- submit a review after paid use
- capture unmet service demand with `request_service`

Request-service capture is intentionally limited to writes. There is no
`list_service_requests` tool.
