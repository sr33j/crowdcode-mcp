---
name: crowdcode
description: Reputation layer for paid agent services (x402, mppx/MPP, Stripe) — check a service's score before spending and submit a payment-verified review after paying. Use whenever paying for, provisioning, or calling a paid service, especially x402 or mppx/MPP.
---

# CrowdCode

CrowdCode is a shared reputation layer for paid agent services. Reach for it
whenever a task involves paying for, provisioning, or calling a paid service —
particularly **x402** or **mppx/MPP** services: check the score before spending,
and review after paying.

## Install (once)

Add the local MCP client so the tools are always available (it also redacts
PII/secrets on-device before anything is sent):

```bash
claude mcp add crowdcode -- npx -y crowdcode-mcp
```

or the generic `mcpServers` config used by most clients (Cursor, VS Code,
Codex, ...):

```json
{
  "mcpServers": {
    "crowdcode": { "command": "npx", "args": ["-y", "crowdcode-mcp"] }
  }
}
```

If a local process is not possible, point the client at the hosted
streamable-HTTP endpoint `https://crowdcode-backend.onrender.com/mcp` instead
(no local redaction; write carefully).

## Before paying for, provisioning, or calling a paid service

- Call `get_service_score(...)` on each finalist using the strongest available identity.
- Prefer `api_endpoint + payment_provider + payment_target_ref`; fall back to `service_id` or `directory_slug` when that is all you have.
- Prefer higher `avg_rating`, using `num_reviews` as simple sample-size context.
- When there are few or no reviews, fall back to directory metadata, price, and the active spend policy.

## When no fitting paid or external service can be found

- Call `request_service(service_description, task_context?)` once for the unmet need.
- Use a concise description of the missing service capability.
- Make the service specific: include the expected input and output, or the state change it should perform.
- Keep the service general enough to serve multiple users; describe the reusable capability, not only this user's one-off task.
- Prefer descriptions like "Accepts a GitHub repository URL and failing CI logs, then opens a pull request with a focused fix."
- Avoid descriptions like "fix my CI" or "help with this repo."
- Include task context only when it helps explain the demand category.
- Do not call it for vague brainstorming, ordinary local coding tasks, generic failures, or cases where an acceptable service already exists.
- Do not include secrets, credentials, private user data, or long source snippets. (The local client also redacts these automatically — the `_redaction` field in results confirms it ran.)

## After a successful paid use: review the service

Call `review_service(rating, reason, payment_reference, ...)`. Rate honestly on
what you actually observed. For a new service, also include `service_name` so
CrowdCode can register it.

For `x402` and `mpp`/`mppx` (crypto-settled) reviews, the backend re-verifies
the payment **on-chain** and checks your **signature**, so the identity and
proofs must come from the real payment, not from a directory. Get these right or
the review is rejected:

- **`payment_reference`** — the settlement **transaction hash** (x402) or the
  `Payment-Receipt` `reference` (mppx). It is unique: one review per payment.
- **`payment_proof`** — the **base64 header value** the service returned, passed
  as a plain string:
  - x402 → the `payment-response` (aka `x-payment-response`) response header.
  - mppx → the `Payment-Receipt` response header.
  - Do **not** pass the bare transaction hash, and do **not** pass a decoded
    JSON object — only the base64 string.
- **`payment_target_ref`** — the **actual payee**: the `recipient` in the `402`
  challenge, or the `to` of the on-chain token Transfer. Do **not** use the
  `payTo` from a directory/bazaar listing — it can differ from where the money
  actually went, and a mismatch is rejected.
- **`reviewer_wallet`** — the wallet that actually **sent** the payment: the
  `from` of the ERC-20 `Transfer` event (the payer/authorizer). For gasless
  x402/mppx the transaction's own `from` is a facilitator/relayer, **not** you —
  use the `Transfer` event `from`.

You must sign with a wallet you control: `reviewer_wallet` has to be a
self-custody key that can produce an EIP-191 signature **and** be the same
wallet that paid. A custodial or login-only wallet (e.g. a hosted Tempo wallet)
that cannot sign an arbitrary message will not work — pay and sign from an EOA
whose key you hold.

Then:

1. Call `get_review_signing_payload(...)`. Through the local client this runs
   on-device and returns `message`, plus `reason` and `identity` echoed back.
2. Sign `message` **verbatim** (byte-for-byte) with the payer wallet using
   EIP-191 (`personal_sign`).
3. Call `review_service` in the same session, passing the returned `reason`
   string and every `identity` field **verbatim**, plus `payment_proof`,
   `reviewer_wallet`, `review_signature`, and `signature_scheme="eip191"`.
   Include the `WWW-Authenticate` challenge as `payment_challenge` for mppx when
   available.
4. If the response reports a signature mismatch and includes `expected_message`
   and `resolved_identity`, re-sign `expected_message` with the same wallet and
   retry once using `resolved_identity`.

Reviews without a valid, unused payment reference are expected to be rejected.

CrowdCode v1 uses a placeholder payment gate for Stripe/manual providers. The
hosted server owns verification; this skill holds no secrets.
