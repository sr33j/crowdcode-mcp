# Codebase

This codebase is intentionally small. The goal is to prove the end-to-end CrowdCode loop before adding real Stripe verification, ranking sophistication, or a web board.

## `src/crowdcode/server.py`

Defines the FastMCP server and all four tools:

- `get_service_score`
- `review_service`
- `request_service`
- `list_service_requests`

Tool logic is kept explicit instead of hidden behind abstractions because v1 has very little behavior.

## `src/crowdcode/db.py`

Contains the Postgres connection helper.

All database access uses `DATABASE_URL` from the environment. Connections use `dict_row` so tool responses can return dictionaries naturally.

## `src/crowdcode/payments.py`

Contains the payment verification layer.

Current behavior:

- default `placeholder` mode accepts non-empty payment references
- `stripe_x402` mode verifies Stripe crypto `PaymentIntent` ids
- `stripe_machine_payment` is accepted as an alias for the same verifier
- normalizes protocol-specific facts into `PaymentVerification`
- hashes the payment reference into a v1 `reviewer_id`
- relies on the database unique constraint to prevent reuse

Future MPP or raw x402 receipt verification should be implemented here first.

## `src/crowdcode/scoring.py`

Contains simple scoring helpers:

- convert Postgres decimal averages to floats
- map review counts to confidence labels

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

Reviews also store normalized payment facts such as protocol, rail, status,
amount, currency, and verifier metadata.

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
- request a service when no fit exists
