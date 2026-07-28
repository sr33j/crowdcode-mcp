# Changelog

## 0.4.0 (backend) — 2026-07-27

### Tx-hash-only payment verification & honest verification levels
- Reviews now carry `payment_verification_level`
  (`unverified` | `signature_only` | `onchain_verified` |
  `response_attested`): what the check actually proved. `payment_verified`
  is derived from it (`onchain_verified`/`response_attested`). Legacy rows
  are backfilled by the schema migration.
- **A settlement tx hash in `payment_reference` alone now earns
  `onchain_verified`** — no `payment_proof` header needed. The proof header
  is unsigned client JSON; both routes run the identical on-chain check
  (receipt status 1, ERC-20 Transfer from reviewer wallet to service payee,
  pinned token) and weigh the same 2× in scoring. Clients that only get a tx
  hash (e.g. agentcash) are no longer second-class.
- Failed tx-only checks accept the review at `signature_only` (never reject)
  with a machine-readable `verification_failure`. An unreachable RPC is the
  one retryable failure: the nightly cron re-checks those reviews (≤5
  attempts / 14 days) and upgrades them when the transfer verifies, so a
  network flake never permanently costs the verified multiplier.
- Token pinning is now symmetric: without `MPPX_TEMPO_TOKEN_ADDRESS`, mppx
  reviews cap at `signature_only` on both the proof and tx-only paths
  (previously an unpinned mppx proof verified against any token). Set the
  env var — it ships in render.yaml and .env.example.
- Dedup hardened: `payment_reference` is canonicalized (provider prefixes
  `x402:base:`/`mppx:tempo:` stripped) with a unique index, closing the
  loophole where the same settlement could be reviewed twice under two
  spellings.
- APIs expose the levels: `verification_level` on recent reviews plus
  `num_onchain_verified_reviews` / `num_signature_only_reviews` counts on
  `get_service_score`, `/api/services/top`, and the service detail endpoint.
- Apply `supabase/schema.sql` BEFORE deploying this version (the code
  selects and inserts the new columns).

## 0.3.0 (backend) — 2026-07-27

### One canonical score (docs/SCORING.md v1)
- `src/crowdcode/scoring.py` is now the single scoring implementation:
  `score = (Σ wᵢrᵢ + κμ₀)/(Σ wᵢ + κ)` with κ=2, μ₀=3.0, review weight =
  wallet trust × payment proof (2× verified / 1× signed) × 180-day decay.
  The MCP `get_service_score` tool, `/api/services`, `/api/services/top`, and
  the new detail endpoint all serve the identical value.
- Reviewer trust is earned, not granted: raw trust ∈ [−5, 1.0] updated by a
  proper scoring rule against the **leave-one-out** consensus (η=0.02), and
  rounded to **zero weight below θ=0.1** — so fresh and adversarial wallets
  contribute exactly nothing. Seed wallets are pinned at 1.0 via
  `CROWDCODE_SEED_WALLETS`.
- New fields on `get_service_score`: `score`, `n_eff`, `unproven`,
  `score_algorithm`, `summary`. `weighted_rating` becomes a deprecated alias
  of `score`; `rank_score` on `/api/services` likewise. `/api/services` now
  LEFT JOINs reviews, so zero-review services appear as unproven at the prior
  instead of being invisible.
- Trust and scores are updated on the review write path, inside the same
  transaction as the insert.

### Payment verification hardening
- x402 payments are pinned to Base USDC (`X402_USDC_ADDRESS` to override) and
  mppx to `MPPX_TEMPO_TOKEN_ADDRESS` — a transfer of any other token is no
  longer a verified purchase.
- When the challenge price is visible, an underpayment is rejected; the
  transferred amount is recorded in `reviews.amount`.

### Nightly cron (`crowdcode-cron`, new Render cron service)
- Consistency sweep: replays review history to recompute all trust and scores
  from scratch, logs drift, backfills `wallet_users` + `reviews.user_id`.
- Per-service LLM review summaries (strengths / failure modes / caveats),
  watermarked on `services.last_summarized_at`, generated only from
  trust-weighted reviews, served in `get_service_score` and on the website.
- Requested-services summary written to `app_cache`, so `/api/project-ideas`
  survives free-tier cold starts.

### Website
- Clicking a service row expands it in place: AI review summary, rating
  histogram, and recent reviews, fetched from the new
  `GET /api/services/{service_id}`.
- Score bar rescaled to the full 1–5 range (the old hardcoded 3.5 floor broke
  under the new prior); unproven services show an "unproven" chip.

### Schema
- New `wallet_users` (per-wallet trust; named to avoid the unrelated
  `public.users` table) and `app_cache` tables; `services` gains `score`,
  `n_eff`, `score_updated_at`, `review_summary`, `last_summarized_at`,
  `resource_type`; `reviews` gains `user_id` and `amount`.

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
