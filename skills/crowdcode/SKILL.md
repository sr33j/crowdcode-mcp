---
name: crowdcode
description: Mandatory reputation checks for paid agent services. Use whenever an agent may spend money, call or provision a paid API, make an x402 or MPP/mppx request, use AgentCash or Stripe for a service, or purchase any external capability. Check CrowdCode before the first payment and review the service after every paid use, including failures.
---

# CrowdCode

Use CrowdCode as the reputation layer around paid agent services. Do not begin
a command or tool call that may charge money until the pre-payment check is
complete.

## Before spending

1. Call `get_service_score` for every finalist before its first paid call.
2. Identify the service with the strongest values available:
   `api_endpoint + payment_provider + payment_target_ref`; otherwise use
   `service_id` or `directory_slug`.
3. Compare the canonical `score` and use `n_eff` as evidence strength.
   `unproven: true` means insufficient trusted evidence, not a bad service;
   fall back to directory metadata, price, and the active spend policy.
4. Read `summary` when present for reported strengths, failures, and caveats.
5. Tell the user what will be spent when the surrounding workflow does not
   already provide a clear spending disclosure.

If the score check fails because CrowdCode is temporarily unavailable, report
that fact before spending and follow the returned `next_step`. Do not silently
treat a missing check as approval.

## After paid use

Call `review_service` after every paid use, whether it succeeded, returned a
poor result, timed out, or failed after payment. Judge the response against the
original task:

- **5** — excellent, relevant, fast, and reusable with no concrete caveat.
- **4** — helpful, with a specific schema, documentation, latency, or output
  caveat.
- **3** — mixed, thin, confusing, or technically valid but not useful.
- **2** — paid but poor, unclear, or difficult to use.
- **1** — paid and broken, unusable, misleading, or severely unreliable.

State the observed reason plainly. A bad paid outcome is the reason for a low
review, not a reason to skip reviewing.

`review_service` signs automatically with the local payer-compatible wallet.
Do not call `get_review_signing_payload` or create a signature manually unless
automatic signing fails or an external payer wallet must be used.

For x402 and MPP/mppx, take identity and proof from the actual payment rather
than a directory listing:

- `payment_reference`: x402 settlement transaction hash or MPP
  `Payment-Receipt` reference.
- `payment_proof`: the base64 response-header value when available, not a
  decoded object or bare transaction hash.
- `payment_target_ref`: the actual payment recipient/on-chain transfer payee.

If the payer wallet differs from the local signing wallet, supply a signature
from the wallet that actually sent the payment. Follow a returned `next_step`
or canonical signature-mismatch retry once; never invent payment evidence.

## Missing paid service

Call `request_service` once only when the agent was actively trying to buy a
remote API capability, had spend authority, and would have paid a concrete
amount immediately, but no suitable paid service existed. Describe reusable
inputs, outputs, and approximate per-call value. Do not submit free-tool wishes,
local runtime wishes, one-off task help, secrets, or private user data.
