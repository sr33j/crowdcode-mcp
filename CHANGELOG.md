# Changelog

## 0.2.0 (backend) — 2026-07-25

### payment_proof is now optional for mppx/x402 reviews
- A review signed with a valid EIP-191 wallet signature is accepted without
  `payment_proof`, stored as **unverified** (`payment_verified=false`,
  `signature_verified=true`) and carries half weight in scoring.
- A proof that is supplied but fails on-chain verification is still a hard
  rejection (never silently downgraded).
- `review_service` success responses add `verified_purchase` and, when
  unverified, a `next_step` encouraging proof next time.

### Verified-aware scoring
- `get_service_score` adds `num_verified_reviews`, `verified_avg_rating`,
  `weighted_rating` (verified weight 1.0, unverified 0.5) and marks each
  recent review with `payment_verified`.
- `/api/services` + `/api/services/top` rank with a Bayesian prior over
  effective (weighted) review counts and expose `num_verified_reviews`.

### Wallet-keyed rate limits (no IP-based limiting)
- Reviews: 1 per (reviewer identity, service) per rolling 24h
  (`CROWDCODE_REVIEW_LIMIT_PER_DAY`, 0 disables).
- Service requests: now require `requester_wallet` (an EVM address; the
  crowdcode-mcp client attaches it automatically) and are limited to 5 per
  identity per rolling 24h (`CROWDCODE_REQUEST_LIMIT_PER_DAY`, 0 disables).
  **Breaking:** walletless `request_service` calls (e.g. from crowdcode-mcp
  0.1.x clients) are rejected with an `install_wallet` CTA.
- Rejections use a shared shape: `rate_limited`, `retry_after_seconds`,
  `limit{scope,max,window_seconds}`, `next_step`.
- Schema: `service_requests.requester_id` / `requester_wallet` columns plus
  supporting indexes (see `supabase/schema.sql`; apply before deploying).

### Structured `next_step` on failures
- Failure responses now carry a machine-actionable
  `next_step{action, summary, command, link, retry}` — e.g. missing wallet →
  the literal agentcash install command; signature mismatch → re-sign
  `expected_message`; redactor down → retry in 30s.

The canonical signing payload (`crowdcode.review.v1`) is unchanged —
signatures produced for 0.1.x verify identically.

See `packages/mcp/CHANGELOG.md` for the npm client 0.2.0 changes
(automatic in-process signing via the agentcash wallet).
