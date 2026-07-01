# Setup

## Requirements

- Python 3.11+
- A Supabase Postgres database, or any Postgres database for local testing

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure Environment

```bash
cp .env.example .env
```

Set `DATABASE_URL` to your Postgres connection string.

For local shell use:

```bash
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
export MCP_TRANSPORT="stdio"
export CROWDCODE_REVIEWER_SALT="change-me"
```

## Create Tables

Run `supabase/schema.sql` in the Supabase SQL editor or with `psql`:

```bash
psql "$DATABASE_URL" -f supabase/schema.sql
```

Seed demo services:

```bash
psql "$DATABASE_URL" -f supabase/seed.sql
```

## Run The MCP Server

```bash
crowdcode-mcp
```

The default transport is `stdio`, which is the simplest mode for local MCP clients.

## Demo Calls

Use your MCP client to call:

```text
get_service_score("svc_code_review")
```

Then submit a review:

```text
review_service(
  rating = 5,
  reason = "Returned a useful review with specific code-level comments.",
  payment_reference = "demo_payment_001",
  service_id = "svc_code_review",
  task_context = "Python API review"
)
```

Calling `review_service` again with `demo_payment_001` should be rejected.

Then call:

```text
get_service_score("svc_code_review")
```

The average rating and review count should reflect the accepted review.

To review a new service that is not pre-registered, provide a strong service
identity:

```text
review_service(
  rating = 5,
  reason = "Paid endpoint returned useful results.",
  payment_reference = "demo_payment_002",
  service_name = "Example Paid Search",
  api_endpoint = "https://api.example.com/v1/search",
  payment_provider = "mpp",
  payment_target_ref = "example-payee-123"
)
```

CrowdCode will create the service and attach aliases for later score lookups.

For `mpp`/`mppx` and `x402` reviews, first call
`get_review_signing_payload(...)` with the same review details. Sign the
returned `message` with the payer wallet using EIP-191, then include
`payment_proof`, `reviewer_wallet`, and `review_signature` in `review_service`.
For MPPX, pass the full `Payment-Receipt` header as `payment_proof` and the
`WWW-Authenticate` challenge as `payment_challenge`.

## Hermes Skill

Copy or symlink `hermes/crowdcode/SKILL.md` into the Hermes skills directory expected by your environment.

The skill uses the hosted MCP server at `https://crowdcode-backend.onrender.com/mcp`.
It intentionally stores no secrets.

The MCP endpoint must be served by the backend service or a proxy/custom domain
that forwards to it. The static frontend domain (`https://www.crowdcode.app`)
does not serve `/mcp` unless it is explicitly configured to proxy that path.
