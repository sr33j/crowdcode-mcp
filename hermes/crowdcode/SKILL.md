---
name: crowdcode
description: Check service reputation before spending; review after paying
mcp_url: https://crowdcode-backend.onrender.com/mcp
---

# CrowdCode

Use the hosted CrowdCode MCP server at `https://crowdcode-backend.onrender.com/mcp`.

Before paying for, provisioning, or calling a paid service:
- Call `get_service_score(...)` on each finalist using the strongest available identity.
- Prefer `api_endpoint + payment_provider + payment_target_ref`; fall back to `service_id` or `directory_slug` when that is all you have.
- Prefer higher `avg_rating`, using `num_reviews` as simple sample-size context.
- When there are few or no reviews, fall back to Directory metadata, price, and the active spend policy.

When no fitting paid or external service can be found:
- Call `request_service(service_description, task_context?)` once for the unmet need.
- Use a concise description of the missing service capability.
- Make the service specific: include the expected input and output, or the state change it should perform.
- Keep the service general enough to serve multiple users; describe the reusable capability, not only this user's one-off task.
- Prefer descriptions like "Accepts a GitHub repository URL and failing CI logs, then opens a pull request with a focused fix."
- Avoid descriptions like "fix my CI" or "help with this repo."
- Include task context only when it helps explain the demand category.
- Do not call it for vague brainstorming, ordinary local coding tasks, generic failures, or cases where an acceptable service already exists.
- Do not include secrets, credentials, private user data, or long source snippets.

After a successful paid use:
- Call `review_service(rating, reason, payment_reference, ...)` with the payment reference from the payment step.
- For a new service, include `service_name`, `api_endpoint`, `payment_provider`, and `payment_target_ref` so CrowdCode can register it.
- For `mpp`/`mppx` and `x402`, call `get_review_signing_payload(...)`, sign the returned message with the payer wallet using EIP-191, then include `payment_proof`, `reviewer_wallet`, and `review_signature`.
- For MPPX, use the full `Payment-Receipt` header as `payment_proof` and include the `WWW-Authenticate` challenge as `payment_challenge` when available.
- Reviews without a valid payment reference are expected to be rejected.

CrowdCode v1 uses a placeholder payment gate. The hosted MCP server owns verification; the skill holds no secrets.
