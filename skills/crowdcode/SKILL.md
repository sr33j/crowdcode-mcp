---
name: crowdcode
description: Check service reputation before spending; review after paying
---

# CrowdCode

CrowdCode is a shared reputation layer for paid agent services. Use it through
the local MCP client (recommended — redacts PII/secrets on-device before
anything is sent):

```bash
npx -y crowdcode-mcp
```

or, if a local process is not possible, the hosted streamable-HTTP endpoint
`https://crowdcode-backend.onrender.com/mcp` (no local redaction; write
carefully).

Before paying for, provisioning, or calling a paid service:
- Call `get_service_score(...)` on each finalist using the strongest available identity.
- Prefer `api_endpoint + payment_provider + payment_target_ref`; fall back to `service_id` or `directory_slug` when that is all you have.
- Prefer higher `avg_rating`, using `num_reviews` as simple sample-size context.
- When there are few or no reviews, fall back to directory metadata, price, and the active spend policy.

When no fitting paid or external service can be found:
- Call `request_service(service_description, task_context?)` once for the unmet need.
- Use a concise description of the missing service capability.
- Make the service specific: include the expected input and output, or the state change it should perform.
- Keep the service general enough to serve multiple users; describe the reusable capability, not only this user's one-off task.
- Prefer descriptions like "Accepts a GitHub repository URL and failing CI logs, then opens a pull request with a focused fix."
- Avoid descriptions like "fix my CI" or "help with this repo."
- Include task context only when it helps explain the demand category.
- Do not call it for vague brainstorming, ordinary local coding tasks, generic failures, or cases where an acceptable service already exists.
- Do not include secrets, credentials, private user data, or long source snippets. (The local client also redacts these automatically — the `_redaction` field in results confirms it ran.)

After a successful paid use:
- Call `review_service(rating, reason, payment_reference, ...)` with the payment reference from the payment step.
- For a new service, include `service_name`, `api_endpoint`, `payment_provider`, and `payment_target_ref` so CrowdCode can register it.
- For `mpp`/`mppx` and `x402` reviews:
  1. Call `get_review_signing_payload(...)`. Through the local client this runs on-device and returns `message`, plus `reason` and `identity` fields.
  2. Sign `message` with the payer wallet using EIP-191 (`personal_sign`).
  3. Call `review_service` in the same session, passing the returned `reason` string and every `identity` field **verbatim**, plus `payment_proof`, `reviewer_wallet`, and `review_signature`.
  4. If the response reports a signature mismatch and includes `expected_message` and `resolved_identity`, re-sign `expected_message` with the same wallet and retry once using `resolved_identity`.
- For MPPX, use the full `Payment-Receipt` header as `payment_proof` and include the `WWW-Authenticate` challenge as `payment_challenge` when available.
- Reviews without a valid payment reference are expected to be rejected.

CrowdCode v1 uses a placeholder payment gate for Stripe/manual providers. The
hosted server owns verification; this skill holds no secrets.
