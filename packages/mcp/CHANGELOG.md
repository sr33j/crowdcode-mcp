# crowdcode-mcp (npm) changelog

## 0.4.0 — 2026-07-31

### Skill-aware installation
- Added `crowdcode-mcp install` for explicit, idempotent installation of both
  the eager CrowdCode skill and MCP configuration in Codex, Claude Code,
  Cursor, and Claude Desktop.
- Added `crowdcode-mcp doctor` to report missing or stale skill/MCP state.
- The npm tarball now includes the canonical skill; unmanaged conflicting
  skills are backed up, and malformed client configuration is never
  overwritten.
- Added a validated Codex plugin bundle and platform-specific MCPB build for
  Claude Desktop, with release jobs for macOS, Linux, and Windows.
- Updated the skill trigger to cover any paid API or purchase and aligned its
  instructions with automatic review signing.

## 0.3.2 — 2026-07-30

### Security hardening
- Existing services now sign with the database-authorized resolved identity;
  caller fields cannot replace a canonical payment destination. Registered
  alternate endpoints and payment rails continue to work.
- Signature-mismatch recovery no longer blindly signs a backend-provided
  `expected_message`. The client reconstructs the domain-separated
  `crowdcode.review.v1` payload locally, verifies a byte-exact match, rejects
  unexpected identity changes, and retries at most once.
- Requires the corresponding backend identity-resolution fix for complete
  enforcement.

## 0.3.1 — 2026-07-27

### Tx-hash-only verified purchases
- `payment_proof` is no longer needed for verified-purchase status: when
  `payment_reference` is a settlement tx hash, the backend verifies the
  ERC-20 transfer on-chain directly and grants the same verified status
  (double scoring weight). Clients that only have a tx hash — e.g. agentcash,
  which returns no payment-response header — are no longer second-class.
- Responses now include `payment_verification_level`
  (`unverified` | `signature_only` | `onchain_verified` |
  `response_attested`) — the source of truth for what was proven; the
  `payment_verified` boolean is derived from it.
- Guidance updated throughout: on-chain verification covers x402 on Base and
  mppx on Tempo; payments settled elsewhere (e.g. Solana) are accepted but
  stay `signature_only`.
- No input-schema changes; descriptions only. Requires backend 0.4.0
  (tx-hash verification, verification levels).

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
