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

Local demos use placeholder payment verification by default. To verify
Stripe-backed x402 crypto `PaymentIntent` references instead:

```bash
export CROWDCODE_PAYMENT_VERIFICATION_MODE="stripe_x402"
export STRIPE_SECRET_KEY="sk_test_..."
export STRIPE_API_VERSION="2026-03-04.preview"
```

`stripe_machine_payment` is also accepted as a mode name when you want the env
var to describe the broader Stripe machine-payment lane rather than x402 only.

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
  service_id = "svc_code_review",
  rating = 5,
  reason = "Returned a useful review with specific code-level comments.",
  payment_reference = "demo_payment_001",
  task_context = "Python API review"
)
```

Calling `review_service` again with `demo_payment_001` should be rejected.

When `CROWDCODE_PAYMENT_VERIFICATION_MODE=stripe_x402`, pass a Stripe crypto
`PaymentIntent` id such as `pi_...` as `payment_reference`. The PaymentIntent
must be `succeeded` and bound to the reviewed service through metadata such as
`crowdcode_service_id`.

Then call:

```text
get_service_score("svc_code_review")
```

The average rating and review count should reflect the accepted review.

## Hermes Skill

Copy or symlink `hermes/crowdcode/SKILL.md` into the Hermes skills directory expected by your environment.

Set:

```bash
export CROWDCODE_MCP_URL="..."
```

The exact URL depends on how you expose the MCP server. The skill intentionally stores no secrets.
