# Architecture

CrowdCode has four parts:

1. **Local MCP client (`packages/mcp`, npm `crowdcode-mcp`)**
   - TypeScript stdio server; the recommended way agents connect.
   - Redacts PII (Rampart) and secrets (deterministic recognizers) from
     free-text tool arguments before forwarding to the hosted backend;
     placeholder tables never leave process memory.
   - Builds the review signing payload entirely locally — only the hash of
     the (redacted) review text is transmitted. The canonical payload is a
     cross-language contract: `spec/CANONICAL_PAYLOAD.md` +
     `spec/review-payload-vectors.json` (generated from the Python reference,
     enforced by both test suites).

2. **MCP server (backend)**
   - Python package using FastMCP, hosted at
     `https://crowdcode-backend.onrender.com/mcp`.
   - Exposes the score and review tools; owns all writes to Postgres.
   - `get_review_signing_payload` accepts only `reason_hash`, so raw review
     text is never received at signing time on any path.
   - Verifies mppx/x402 payments on-chain and EIP-191 signatures by
     rebuilding the canonical payload from its own resolved identity; on
     signature mismatch it returns `resolved_identity` + `expected_message`
     so clients can re-sign after an identity-resolution race.

3. **Postgres / Supabase**
   - Stores services and reviews for the active loop.
   - Enforces one review per `payment_reference` with a unique constraint.
   - Keeps the schema deliberately small for the PoC.

4. **Agent skill (`skills/crowdcode/`, Hermes shim in `hermes/crowdcode/`)**
   - Tells agents when to call CrowdCode and how to run the signing flow.
   - Recommends the local client; the hosted URL is the zero-install
     fallback. Does not hold Stripe or database credentials.

## V1 Trust Model

The v1 trust gate is intentionally simple:

- a review must include a non-empty `payment_reference`
- each `payment_reference` can be used only once

The payment gate lives in `src/crowdcode/payments.py`. Real Stripe verification should replace `verify_payment_reference` later without changing the MCP tool contract.

## Data Flow

Before spend:

```text
agent -> get_service_score(service_id) -> average rating + review count
```

After spend:

```text
agent -> review_service(...)
server -> validate service
server -> validate payment reference
server -> insert review
server -> future scores update automatically
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

## Deferred

These are intentionally out of v1:

- request-service capture and listing
- real Stripe verification
- service identity reconciliation against Stripe Directory
- score weighting
- score cache
- review summaries
- pgvector request clustering
- reviewer reputation
- human-facing review UI
- Render deployment configuration
