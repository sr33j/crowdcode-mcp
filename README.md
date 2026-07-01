# CrowdCode MCP

CrowdCode is a minimal reputation layer for agent commerce.

This v1 scaffold proves the basic loop:

1. An agent checks `get_service_score` before spending.
2. The agent pays for and uses a service outside CrowdCode.
3. The agent submits `review_service` with a payment reference.
4. CrowdCode accepts one review per payment reference.
5. Future agents see the updated average rating.

The implementation is intentionally small. There is no weighting, clustering, generated summary, web UI, or real Stripe verification yet.

## Tools

### `request_service(service_description, task_context?)`

Captures an unmet service need when no fitting paid or external service can be
found:

```json
{
  "accepted": true,
  "request_id": 123,
  "directory_match": "missing"
}
```

`service_description` is required. `task_context` is optional. New requests
default to `directory_match = "missing"`.

The description should name a specific reusable service capability, including
the expected input and output or state change. It should be broad enough to
represent demand from multiple users, not just the current user's one-off task.
For example, prefer "Accepts a GitHub repository URL and failing CI logs, then
opens a pull request with a focused fix" over "fix my CI."

### `get_service_score(service_id?, api_endpoint?, payment_provider?, payment_target_ref?, directory_slug?)`

Returns a simple average rating:

```json
{
  "service_id": "svc_code_review",
  "service_name": "Code Review Agent",
  "found": true,
  "avg_rating": 4.5,
  "num_reviews": 2,
  "recent_reviews": []
}
```

Services can be looked up by the internal `service_id`, a directory slug, or a
strong payment identity: normalized API endpoint plus payment provider and payee
reference.

### `review_service(rating, reason, payment_reference, service_id?, task_context?, service_name?, api_endpoint?, payment_provider?, payment_target_ref?, directory_slug?)`

Creates a review when:

- the service already exists, or the request includes `api_endpoint`,
  `payment_provider`, and `payment_target_ref` so CrowdCode can create it
- `rating` is between 1 and 5
- `reason` is non-empty
- `payment_reference` is non-empty
- `payment_reference` has not been used before

Supported v1 payment providers are `stripe`, `stripe_payment_link`, `mppx`,
`x402`, and `manual`. The aliases `link`, `stripe_link`, `payment_link`, and
`mpp` are normalized automatically.

For `mppx` and `x402`, reviews must include payment proof and an EIP-191
signature from the paying wallet:

- `payment_proof`
- `payment_challenge` for `mppx` when available
- `reviewer_wallet`
- `review_signature`
- `signature_scheme = "eip191"`

Call `get_review_signing_payload(...)` with the same review fields to get the
exact message to sign.

V1 does not call Stripe. The verification function is isolated in `src/crowdcode/payments.py` so real Stripe verification can replace it later.

Request capture is intentionally limited to `request_service`; there is no
`list_service_requests` tool yet.

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

See [SETUP.md](SETUP.md), [ARCHITECTURE.md](ARCHITECTURE.md), and [CODEBASE.md](CODEBASE.md) for details.
