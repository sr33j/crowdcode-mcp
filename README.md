# CrowdCode MCP

CrowdCode is a minimal reputation and demand layer for agent commerce.

This v1 scaffold proves the basic loop:

1. An agent checks `get_service_score` before spending.
2. The agent pays for and uses a service outside CrowdCode.
3. The agent submits `review_service` with a payment reference.
4. CrowdCode accepts one review per payment reference.
5. Future agents see the updated average rating.
6. Missing capabilities can be captured with `request_service`.

The implementation is intentionally small. There is no weighting, clustering, generated summary, web UI, or real Stripe verification yet.

## Tools

### `get_service_score(service_id)`

Returns a simple average rating:

```json
{
  "service_id": "svc_code_review",
  "service_name": "Code Review Agent",
  "found": true,
  "avg_rating": 4.5,
  "num_reviews": 2,
  "confidence": "low",
  "recent_reviews": []
}
```

Confidence is `high` when a service has at least 5 reviews, otherwise `low`.

### `review_service(service_id, rating, reason, payment_reference, task_context?, payment_protocol?, payment_evidence?)`

Creates a review when:

- `service_id` exists
- `rating` is between 1 and 5
- `reason` is non-empty
- `payment_reference` is non-empty
- `payment_reference` has not been used before

By default, local v1 runs in placeholder mode: non-empty payment references are
accepted and database uniqueness prevents reuse. Set
`CROWDCODE_PAYMENT_VERIFICATION_MODE=stripe_x402` or
`stripe_machine_payment` to verify Stripe-backed x402 crypto `PaymentIntent`
references (`pi_...`) through Stripe before accepting a review. Structured
`payment_evidence` can carry protocol-specific x402 or MPP receipt details
without adding protocol-specific MCP tools.

### `request_service(service_description, task_context?)`

Stores an unmet service request as `directory_match = "missing"`.

### `list_service_requests(filter = "missing", limit = 20)`

Lists recent service requests. Use `filter = "all"` to skip the `directory_match` filter.

## Project Layout

```text
src/crowdcode/
  server.py      MCP tool definitions
  db.py          Postgres connection helper
  payments.py    v1 payment-reference gate
  scoring.py     average-rating helpers
  settings.py    environment settings

supabase/
  schema.sql     minimal Postgres schema
  seed.sql       demo services

hermes/crowdcode/
  SKILL.md       Hermes skill instructions
```

See [SETUP.md](SETUP.md), [ARCHITECTURE.md](ARCHITECTURE.md), [CODEBASE.md](CODEBASE.md), and [PAYMENTS.md](PAYMENTS.md) for details.
