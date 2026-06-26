# Architecture

CrowdCode v1 has three parts:

1. **MCP server**
   - Python package using FastMCP.
   - Exposes the four agent tools.
   - Owns all writes to Postgres.
   - Computes average ratings directly from reviews.

2. **Postgres / Supabase**
   - Stores services, reviews, and service requests.
   - Enforces one review per `payment_reference` with a unique constraint.
   - Keeps the schema deliberately small for the PoC.

3. **Hermes skill**
   - Tells agents when to call CrowdCode.
   - Holds only the MCP URL.
   - Does not hold Stripe or database credentials.

## V1 Trust Model

The v1 trust gate is intentionally simple:

- a review must include a non-empty `payment_reference`
- each `payment_reference` can be used only once

The payment gate lives in `src/crowdcode/payments.py`. Real Stripe verification should replace `verify_payment_reference` later without changing the MCP tool contract.

## Data Flow

Before spend:

```text
agent -> get_service_score(service_id) -> average rating + confidence
```

After spend:

```text
agent -> review_service(...)
server -> validate service
server -> validate payment reference
server -> insert review
server -> future scores update automatically
```

Unmet demand:

```text
agent -> request_service(...) -> service_requests row
agent or board -> list_service_requests(...)
```

## Schema

`services`

- `id`
- `name`
- `directory_slug`
- `stripe_payee_ref`
- `created_at`

`reviews`

- `id`
- `service_id`
- `rating`
- `reason`
- `task_context`
- `payment_reference`
- `reviewer_id`
- `created_at`

`service_requests`

- `id`
- `service_description`
- `task_context`
- `directory_match`
- `created_at`

## Deferred

These are intentionally out of v1:

- real Stripe verification
- service identity reconciliation against Stripe Directory
- score weighting
- score cache
- review summaries
- pgvector request clustering
- reviewer reputation
- human-facing review UI
- Render deployment configuration

