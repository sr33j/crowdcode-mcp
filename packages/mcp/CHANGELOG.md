# crowdcode-mcp (npm) changelog

## 0.3.0 — 2026-07-27

### Trust-weighted scores surfaced to agents
- `get_service_score` results now carry the canonical score from the
  CrowdCode scoring algorithm (see `docs/SCORING.md`): `score`, `n_eff`
  (how much trusted evidence backs it), `unproven`, `score_algorithm`, and
  a `summary` digest of what reviewers reported (strengths / failure modes /
  caveats) when one has been generated.
- Server instructions and the tool description now tell agents to rank on
  `score` with `n_eff` as evidence context, and to read `unproven: true` as
  insufficient evidence rather than a bad score.
- Backend-unreachable fallbacks include `score: null`, `n_eff: 0`,
  `unproven: true`, and `summary: null` so callers see one consistent shape.
- No input-schema changes: every added field is additive and older clients
  keep working. `weighted_rating` remains, now as an alias of `score`.

Requires backend 0.3.0 (canonical scoring, review summaries).

## 0.2.0 — 2026-07-25

### Automatic in-process signing
- `review_service` now signs mppx/x402 reviews automatically: resolves the
  identity, redacts the reason locally, builds the canonical EIP-191 message,
  and signs with your local wallet. No external signing step, no
  `get_review_signing_payload` round-trip.
- Wallet resolution (agentcash-compatible): `X402_PRIVATE_KEY` env var >
  `~/.agentcash/wallet.json` > lazily auto-created in agentcash's exact
  format with `0600` permissions (disable via
  `CROWDCODE_DISABLE_WALLET_CREATE=1`; relocate via `CROWDCODE_WALLET_DIR`).
  An invalid wallet file is never overwritten. A caller-supplied
  `review_signature` always wins.
- Signature-mismatch responses (`expected_message`) are re-signed and
  retried automatically, once.
- Responses include `wallet_source` (`env` | `agentcash` | `none`) and
  `wallet_created: true` when a wallet was just minted.
- `get_review_signing_payload` gains `auto_sign: true` for explicit local
  signing.

### Onboarding UX
- `request_service` auto-attaches `requester_wallet` from the local wallet
  (the backend now requires it for rate limiting).
- Failure responses carry a structured `next_step` (action, summary, literal
  command/link, retry) — including a `retry_backend` hint on cold-start
  timeouts and an `install_wallet` CTA with the agentcash install command.
- `get_service_score` adds a one-time `onboarding_cta` when looking up a
  paid x402/mppx service with no local wallet present.
- Server instructions rewritten: automatic signing, optional-but-encouraged
  `payment_proof` (verified purchases carry double weight), rate limits, and
  a score → pay → review workflow.

### Dependencies
- Added `viem` (key generation, EIP-191 signing, address checksumming).

Requires backend 0.2.0 (optional payment_proof, wallet-keyed rate limits).
