# CrowdCode Scoring & Reputation — Design Doc (v3)

Status: **implemented (`crowdcode-scoring-v3`); deployment requires a score replay**
Updated: 2026-10-22
Regression simulation: `tests/test_scoring_sim.py` (deterministic, seed=42).
Admission and manipulation scenarios: `tests/test_hosted_attestation.py`.
The figures and sweep results in §6 are historical v1 evidence from
`docs/scoring/sim_scoring3.py`, using a public prior of 3.0.

**What v3 changed.** Only a review backed by a hosted OpenCrowd gateway receipt
and a platform-judge score carries ranking weight (§3.6). Everything else — CLI
wallets, crowdcode-mcp users, every row written before v3 — is still accepted,
stored and published, as **community evidence with weight exactly zero**. Weight
is held per **owner**, not per wallet, and an owner gets **one vote per
service**. The `opencrowd_seed_wallets` bridge that pinned every registered
hosted mainnet wallet at trust 1.0 is **removed**: `CROWDCODE_SEED_OWNERS` is
the root of trust and every other owner earns its weight.

The public service prior is **3.0**, with strength 2. Reputation's internal
leave-one-out consensus retains its separate **3.0** prior.

Because `n_eff` is now the number of trusted owner votes rather than accumulated
proof-and-decay units, the κ = 2 prior is no longer negligible: a perfect service
publishes 3.67 on one trusted vote, 4.00 on two, 4.33 on four. The shrinkage is
correct — one owner's opinion is weak evidence — and it is why `n_eff`, the
judged count and the community count are published next to every score.

For deployment, pause backend review writes and the cron, apply
`supabase/hosted-only-reviews.sql` (after `scoring-prior-three.sql`), set
`OPENCROWD_RECEIPT_KEYS` and `CROWDCODE_SEED_OWNERS`, deploy the updated backend
and cron to both services, and run
`crowdcode.cron.run_consistency_sweep(utc_now())` before resuming service. The
replay refreshes every stored score from review history; changing the schema
alone does not. Expect most scores to return to the prior and display as
"unproven" until hosted owners with earned trust review those services.

---

## 1. North star

> **The published score of a resource is our best prediction of the rating the
> next honest, trusted reviewer will give it.**

This makes the score a forecast with a measurable loss: every incoming review
from a trusted wallet is a held-out label, and the score published just before
it is the prediction. Mean absolute/squared error over those pairs is the
system's public accuracy metric. All parameters below are tunable against that
backtest — accuracy is a number we report, not a claim we make.

## 2. Scope and principles

- **One canonical public score.** A single scoring function serves the MCP tools
  and the website. `weighted_rating` and `rank_score` are aliases of `score`.
  The internal reputation consensus uses the same math with a neutral prior.
- **Only hosted, judged purchases carry weight.** A review moves a score only
  with a verified gateway receipt and a platform-judge verdict (§3.6). Every
  other review is accepted, stored and published as community evidence with
  weight zero — reading is open, ranking is earned.
- **Payment evidence is still verified independently.** Unpaid reviews require
  an authenticated wallet signature. When an x402/mppx payment is claimed,
  CrowdCode must verify the transaction on its supported chain; invalid
  supplied claims are rejected, never downgraded. A weighted review therefore
  rests on two independent proofs: the chain says the money moved, and the
  gateway says what was bought with it.
- **The reviewer does not write the verdict.** The published rating and text of
  a weighted review come from CrowdCode's platform judge, from the gateway's
  record of the call, so an owner cannot steer the rating of a service they are
  paid by and a service cannot plant instructions in text other agents read.
- **Influence must be earned; bad actors round to zero.** Free identities get
  zero weight by default. Any nonzero default weight × unlimited free wallets
  = unbounded attack (confirmed in simulation, §6.2). Trust flows only from
  seed wallets outward.
- **Public algorithm.** Security is economic, not obscurity-based: influence is
  proportional to irrecoverable cost (accurate track record over time, verified
  external spend), so publishing the algorithm does not weaken it.

## 3. The algorithm

### 3.1 Score (per resource)

```
score(s) = ( Σᵢ wᵢ·rᵢ + κ·μ₀ ) / ( Σᵢ wᵢ + κ )
```

- `rᵢ` — rating (1–5) of review i on resource s
- `wᵢ` — weight of review i (below)
- `κ = 2` — prior strength in pseudo-reviews
- `μ₀ = 3.0` — public prior mean. It is fixed, not automatically fitted.

Published alongside the score: `n_eff = Σᵢ wᵢ`. A resource with `n_eff ≈ 0`
must display as **unproven at the prior**, never as a starred rating.

The index `i` is an **owner/service** bucket, not an individual call and no
longer a calendar day: **one vote per owner per service**, however many agents,
wallets, days or purchases produced it. All unique paid outcomes remain stored.
Within a bucket the rating is the decay-weighted average of that owner's
weighted reviews, while the bucket evidence is capped at the strongest single
one — so buying a service a hundred times sharpens an owner's one vote instead
of multiplying it.

### 3.2 Review weight

```
wᵢ = weight(owner) × proof(i) × decay(Δtᵢ)
```

- `proof(i)` — 1.0 for a weighted review, 0 for everything else:

  | level | multiplier | what it proves |
  |---|---|---|
  | `response_attested` | 1 | a gateway receipt binding this payment to the exact request and response, plus a platform-judge verdict (§3.6) — the tier this column always reserved, now issued |
  | `onchain_verified` | 0 | an on-chain transfer to the service payee. Still verified, still displayed, no ranking weight: it proves money moved, not what was bought |
  | `signature_only` | 0 | a wallet signature over the review |
  | `unverified` | 0 | nothing beyond admission; never published at all |

  v2 priced *how much a payment claim proved* (1× signed, 2× on-chain). v3 has
  nothing left to price: a review either clears the hosted-receipt gate or
  carries no weight. Keeping the multiplier at 1.0 also makes `n_eff` readable
  — it is the number of trusted owner votes behind a score.
- `decay(Δt)` — half-life 180 days. For versioned resources (npm), additionally
  decay by version distance.

### 3.3 Owner trust (the reputation system)

Each **owner** has a **raw trust** `t ∈ [−5, 1.0]`, held in `review_owners` and
keyed on the `owner_ref` a verified attestation names. Seeds are pinned at 1.0;
everyone else starts at 0. Trust is per owner, not per wallet: a wallet costs
nothing to create and one hosted owner runs many agents with many wallets, so
pricing a wallet prices nothing.

**Effective weight (with the zero-round-down):**

```
weight(w) = raw(w)   if raw(w) ≥ θ      (θ = 0.1)
          = 0        otherwise
cap: raw is clamped above at 1.0
```

**Update rule (proper scoring rule / information content).** During the nightly
authoritative replay, process each owner/service bucket once and
compute the resource's **leave-one-out consensus** `LOO` — the weighted score
with this wallet's own reviews excluded and a **3.0 prior**, strength 2.
This internal consensus excludes the reviewer's own evidence and keeps its
prior independent of future changes to the published score's prior.
The current consensus implies a success probability:

```
p = clamp( (LOO − 1) / 4 , 0.05, 0.95 )
likelihood = p        if rating ≥ 4     (the review "predicted success")
           = 1 − p    if rating ≤ 2     (the review "predicted failure")
           = —        bucket ratings strictly between 2 and 4 give no update
Δraw = η · log₂( likelihood / 0.5 )     (η = 0.02)
```

**Negative trust and slashing.** Raw trust −5 is the maximum accumulated trust
penalty, not a negative review weight. Every value below 0.1 contributes zero.
An unslashed wallet can recover by earning trust; even at the maximum gain
`0.02 × log₂(1.9) ≈ 0.01852`, moving from −5 to 0.1 takes at least 276
positive daily-bucket events. Multiple services can produce events on one day.
A separate `slashed_at` flag forces zero weight even for seeds and prevents
trust updates. Current code honors that flag but does not set it automatically;
invalid payment claims are rejected rather than automatically slashing wallets.

### 3.4 Admissibility (hard gates, before any math)

1. Valid EIP-191 signature by `reviewer_wallet` over the canonical payload.
   For x402 Solana, the payer is additionally linked by `payer_signature`
   (rule 8).
2. Every unique verified payment may produce a stored review. Scoring and
   trust aggregate all of one owner's weighted reviews of one resource into a
   single capped bucket; there is no review-ingest throttle.
3. `payment_reference` unique on its **canonical form** (provider prefixes
   like `x402:base:` / `x402:monad:` / `x402:solana:` stripped; EVM hashes
   lowercased, Solana base58 signatures kept case-sensitive) — the same
   settlement tx can never yield two reviews under different spellings. Only a
   review that proves the payer (EVM: the Transfer is from `reviewer_wallet`;
   Solana: a valid `payer_signature`) reserves it.
4. A **supplied** payment proof must fully verify (receipt status 1, Transfer
   `from` == reviewer, `to` == payment_target_ref, **token pinned** — USDC on
   Base for x402, `MPPX_TEMPO_TOKEN_ADDRESS` on Tempo for mppx — **amount ≥
   the 402 challenge price** when one is stated) — else the review is
   **rejected outright**: a failing proof is worse than no proof and must
   never silently downgrade.
5. When claiming payment with **no proof header**, `payment_reference` must be a transaction hash and
   runs the same on-chain check. Failed transactions, wrong token/payee/payer,
   malformed references, and unsupported chains reject the review and store
   nothing. An unreachable RPC returns a retryable unavailable result, stores
   nothing, and does not reserve the reference.
6. Token pinning is symmetric across both routes. A missing pin is a retryable
   verifier-configuration error and stores nothing.
7. New machine-payment verification supports x402 USDC on Base and Monad
   (`eip155:143`, referenced as `x402:monad:<tx>` or named by the proof's
   `network`; the chain is stored in `payment_verification_metadata.network`
   and reused by re-verification), x402 USDC on Solana (rule 8) and mppx on
   Tempo. All other chains are explicitly unsupported; they never
   fall back to `signature_only` on a failed payment claim. Omitting all payment
   evidence instead admits an authenticated unpaid review at `signature_only`.
8. **x402 on Solana** (`x402:solana:<base58 signature>`, or a proof whose
   `network` is `solana` / `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`). The
   backend calls `getTransaction` (`jsonParsed`, commitment `confirmed`) on
   `SOLANA_RPC_URL`, requires `meta.err == null`, and sums SPL
   `transfer`/`transferChecked` instructions of the classic token program for
   the pinned USDC mint (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`) from
   token accounts owned by the payer into token accounts owned by
   `payment_target_ref` (owners and mints from the runtime's token-balance
   records), capped by the payee's net balance increase. The facilitator pays
   the fees, and the payer is a base58 key, never the EVM `reviewer_wallet`.
   Linkage is a base58 ed25519 `payer_signature` by a payer of the transaction
   over the payer link message (spec/CANONICAL_PAYLOAD.md), which commits to
   the canonical payload and the reviewer wallet:
   - valid linkage → `onchain_verified`, reserving the reference;
   - no `payer_signature` → the transfer is recorded as observed
     (`payment_verification_metadata.verification_failure = payer_unlinked`)
     and the review is admitted at `signature_only` without reserving the
     reference: it is stored under a reviewer-scoped key with
     `payment_reference` null, so nobody can claim — or block, including by
     deleting their observation — a public Solana payment they did not send.
     One observation per reviewer wallet; none once the payer has linked it;
   - invalid linkage, failed transactions, wrong mint, payee or payer, or a
     mismatched proof → rejected, storing nothing. A transaction not yet
     visible is retryable.
   Payee pinning follows the EVM rule per address family: a service holds one
   EVM payee and one Solana payee. A service registered with an EVM payee
   gains its Solana payee at the first linked (verified) Solana payment, and
   any other Solana payee is then a conflict — the same first-verified-payment
   registration that applies to an endpoint first seen in an unpaid review.
   Operators can pre-register a payee as a `payment_target` service
   identifier (`x402:<solana address>`) to close that window.

### 3.5 Seeds

Initial seed set: the operator's own **owner references**, from
`CROWDCODE_SEED_OWNERS`, pinned at trust 1.0 in `review_owners.is_seed`. Seeds
do **not** need to review every resource — trust propagates (§6.3). Over time
the seed set can grow to include long-lived, high-accuracy owners (with
hysteresis); that is a governance decision, not an algorithm change.

At least one seed is required. Without one, nothing is ever weighted: a fresh
owner's trust is 0, below θ, and the leave-one-out consensus of a resource with
no weighted evidence sits at the prior, where the scoring rule pays exactly
zero. That is the EigenTrust seed-vector property (§4), so the seed list is a
deliberate operator decision and is never derived from any wallet registry.

`supabase/opencrowd-wallets.sql` — the bridge that pinned every registered
hosted mainnet `agent_eoa` at 1.0 — is **removed** by
`supabase/hosted-only-reviews.sql`. Registering a hosted agent now grants
nothing: creating an agent must not create a trusted reviewer, and one owner
could create many. The backend no longer reads the hosted schema at all.
`slashed_at` still overrides seed weight.

### 3.6 Hosted gateway attestations (what makes a review weighable)

A hosted OpenCrowd gateway is the transport for every paid call its agents
make, so it is the only party that observes the request, the response and the
settlement together. It signs that observation twice, with a dedicated Ed25519
key whose id (`kid`) and public half are published:

1. a **purchase receipt** at settlement, binding `sha256` of the request and the
   response to the payment reference, payee, amount, network, `owner_ref` and
   `payee_owner_ref`;
2. a **review attestation** when the agent reviews that purchase, binding the
   receipt's hash to the platform judge's rating and published text.

The judge is a fixed-prompt, temperature-0 model call the gateway makes and the
reviewing agent pays for. It sees only the task context the agent states, the
request and response from the gateway's own ledger, and the delivery and payment
outcome — never the owner's chat. The agent's rating and prose are passed in as
labelled notes. The published review text is the judge's, which is also why
stored review text is no longer an injection channel between agents.

Both payloads are flat JSON objects of strings and integers, signed over
`json.dumps(payload, sort_keys=True, separators=(",", ":"))`. Flatness is load
bearing: nested values do not serialize identically across the TypeScript signer
and the Python verifier.

`crowdcode.receipts` verifies both signatures against `OPENCROWD_RECEIPT_KEYS`
(several keys at once, so rotation does not invalidate issued receipts),
requires the attestation to name the receipt submitted with it, requires the two
payloads to agree about every payment fact, and then cross-checks the
attestation against the review being stored: same endpoint, same payee, same
settlement, the judge's rating, and the judge's text hashed byte for byte
(before redaction, like the EIP-191 signature). Any mismatch **rejects the
review outright** — admitting it quietly as community evidence would hide a
forgery. `GET /api/receipt-keys` publishes the keys this deployment trusts.

Surviving verification, two rules still remove the weight:

- **Self-dealing.** `payee_owner_ref == owner_ref` — the reviewer's owner is
  paid by the service it is reviewing. Hosted services know both sides of a
  sale, which is exactly why the gateway can state this and CrowdCode can check
  it. Stored and published as `weight_exclusion = self_dealing`.
- **Dust.** A payment below `CROWDCODE_MIN_WEIGHTED_AMOUNT_ATOMIC` (default
  1000 = $0.001) buys no vote: `weight_exclusion = dust_payment`.

Receipts expire for *new* submissions after 30 days and attestations after 30
minutes, so a cache of receipts cannot be replayed into a burst of fresh
evidence. The receipt hash is a unique index, alongside the payment reference.

## 4. Why these mechanics (the math, briefly)

- **Bayesian shrinkage (κ, μ₀)** — with few reviews the score stays near the
  prior instead of overreacting; `n_eff` makes sparsity visible. Standard
  Beta/Dirichlet-posterior behavior.
- **Log-likelihood trust update** — a *proper scoring rule*. A review earns
  trust exactly in proportion to the information it carried beyond the current
  consensus: echoing a score everyone already agrees on earns ~0 (kills
  copy-the-consensus farming); being confidently right where consensus was
  uncertain earns the most; being wrong costs more than being right pays
  (log asymmetry). In expectation: honest > 0, random = 0 (< 0 off-center),
  adversarial < 0. This replaced a ±1-star "corroboration band," which failed
  in simulation because honest works/doesn't-work ratings are bimodal (§6.1).
- **Leave-one-out consensus** — you cannot earn trust from agreement with a
  consensus your own reviews created. Closes the self-corroboration loop where
  a trusted wallet farms trust on a resource only it reviews.
- **Zero-weight threshold θ** — the "round bad actors down to zero" mechanism.
  Down-weighting is insufficient: n wallets × ε residual weight = n·ε influence.
  Confirmed in simulation: without the threshold, 20 adversaries at mean
  residual trust ≈ 0.05 aggregated ~1.0 weight and dragged MAE from 0.36 to
  0.45; with it, their weight is exactly 0 and MAE fell to 0.06.
- **No reflecting floor at zero for raw trust** — with a floor at 0, a
  coin-flipping wallet random-walks off the wall and drifts upward (one reached
  0.31 in simulation). Letting raw trust go to −5 means a penalized wallet
  accumulates debt; recovery from the floor takes at least 276 maximally
  rewarded bucket events before crossing θ.
- **Seed anchoring (EigenTrust structure)** — trust updates are zero while a
  resource's internal consensus sits at its 3.0 prior (p = 0.5 ⇒ log₂1 = 0), and
  zero-weight wallets cannot move a consensus. Therefore trust can only *begin*
  to be earned on resources whose scores were moved by already-weighted wallets
  — trust mass flows outward from the seeds, exactly the EigenTrust
  `t = (1−α)Cᵀt + α·p` seed-vector property, obtained here without computing an
  eigenvector. The v1 rule is the first power-iteration step; iterating to the
  fixed point is a drop-in v2 upgrade of the same batch job.

## 5. Chosen parameters

| Param | Value | Meaning | How chosen |
|---|---|---|---|
| κ | 2 | prior pseudo-reviews | sweep (§6.4) |
| μ₀ | 3.0 | public service prior mean | operator choice |
| trust μ₀ | 3.0 | internal reputation consensus prior | preserves v1 trust |
| η | 0.02 | trust learning rate | sweep — larger values caused honest-wallet flicker and let random walkers transiently cross θ |
| cap | 1.0 | max non-seed raw trust | sweep — the wide cap→θ gap keeps honest wallets far from the zero-weight cliff |
| θ | 0.1 | zero-weight threshold | sweep |
| raw floor | −5 | trust debt ceiling | prevents reflected drift; bounds recovery time |
| p clamp | [0.05, 0.95] | likelihood bounds | bounds any single update to ≈ ±3.3·η |
| proof multiplier | 2× | payment-verified upweight | starting value; re-fit via backtest |
| decay half-life | 180 d | review staleness | starting value; re-fit via backtest |

All constants are calibrated on simulated works/doesn't-work reviewers; real
ratings (2s/3s/4s, subjective quality) are noisier. The structure is validated;
re-fit the constants against the production backtest (§8.4).

## 6. Simulation evidence

Deterministic (seed=42), 100 daily rounds; each round every reviewer reviews
each of their services once. Under v3 those repeats collapse into one vote
per owner per service, which is what the simulation now checks. Honest behavior: 5 stars
if the call worked, 1 if it failed. True quality of a service with reliability
p is the expected honest rating `1 + 4p`. Reproduce with
`python docs/scoring/sim_scoring3.py`.

### 6.1 v1 candidate rules that failed (5 services, 4 reviewers)

| Trust rule | honest | inverter | random | score MAE |
|---|---|---|---|---|
| +0.1 if within ±1 of consensus, no penalty | 0.50 | 0.10 | **0.50** | 0.36 |
| same, with −0.1 penalty | **0.10** | 0.10 | 0.00 | 0.17 |
| sign-agreement ±0.1 | 0.30 | 0.10 | **0.20** | 0.26 |

Failures: without a penalty, a coin-flip wallet maxes out trust; with the ±1
band penalty, honest wallets get destroyed on mid-reliability services (their
1s and 5s are both >1 from a ~3.3 score — the band assumes unimodal ratings);
sign-agreement + a floor at 0 turns a fair coin into upward drift.

### 6.2 Log-likelihood rule under adversarial majority (20 adversaries vs 2 honest)

Trust separated perfectly (honest 0.50, inverters ~0.02, randoms ~0.08) — but
score MAE **worsened** to 0.45: twenty small residual trusts summed to ~1.0
aggregate weight. Adding θ (zero below 0.1) and removing the floor-at-zero:
every adversary at exactly 0 weight, **MAE 0.06**.

### 6.3 Full v3 scenario — partial seed coverage + wash-trading attack

1 seed reviewing only **3 of 11** services · 3 honest reviewing everything ·
10 inverters · 10 randoms · 5 pumpers posting only 5-star reviews on their own
25%-reliable scam service (500 wash reviews total).

![Reviewer reputation](scoring/reviewer_reputation_v3.png)
![Service scores](scoring/service_scores_v3.png)

Results (best config η=0.02, cap=1.0, θ=0.1, κ=2):

- **MAE 0.06 across all 11 services; zero honest flicker; every adversary at
  exactly zero weight in every round.**
- **Trust propagated beyond the seed.** Honest wallets earned trust on the 3
  seeded services (crossing θ around rounds 10–25), then anchored accurate
  consensus on the 8 services the seed never touched (e.g. unseeded 92% service:
  4.62 vs true 4.67).
- **The wash-trade attack scored 1.93 (true 2.00).** The pumpers reviewed
  nothing the trusted population covers, so their LOO consensus never left the
  prior, their trust updates were exactly zero, and their 500 five-star reviews
  carried zero weight.

### 6.4 Parameter sweep (24 configs)

η ∈ {0.02, 0.05, 0.1} × cap ∈ {0.5, 1.0} × θ ∈ {0.1, 0.2} × κ ∈ {2, 5}.
Every config achieved MAE ≤ 0.08 and contained the scam service — the design is
robust to these parameters. η=0.1 configs showed honest flicker (up to 12
zero-weight rounds) and transient adversary weight (up to 0.50); all η=0.02
configs showed neither. Selection rule: zero flicker, zero adversary weight,
then min MAE.

## 7. Attack analysis

| Attack | Cost to attacker | Outcome |
|---|---|---|
| Sybil flood (n fresh wallets, free reviews) | ~0 | ~0 — no receipt, so weight 0 before trust is even consulted; the resource sits at the prior with n_eff ≈ 0, displayed "unproven" (§6.3) |
| Sybil flood with real hosted agents and real receipts | hosting + a real payment each | ~0 — one owner, one vote per resource, and a fresh owner's trust is 0 (§3.3) |
| Steer your own agent's rating | ~0 | ~0 — the platform judge writes the rating from the gateway's record; the agent's claim is a labelled input (§3.6) |
| Plant instructions in review text for the next agent to read | ~0 | ~0 — the stored text is the judge's, and agents are shown weighted text only, fenced as untrusted data |
| Pay your own hosted service and review it | gas/fees | 0 weight — the gateway names `payee_owner_ref`; dropped as self-dealing (§3.6) |
| Forge or re-aim a gateway receipt | ~0 | rejected outright: signature, receipt-hash binding and field cross-check all have to pass, and the review is pinned to the receipt's own endpoint and payee |
| Wash-trade own resource | gas/fees | 0 weight — LOO keeps their consensus at prior; no trust ever earned (§6.3) |
| Earn-then-pump (review honestly elsewhere, then pump own resource) | weeks of honest reviewing per owner, plus hosting and real payments | bounded: one trusted owner's single vote moves a lonely resource ≈ prior→3.67; LOO blocks trust gain from it; the first honest trusted review starts both correcting the score and (in the v2 batch re-evaluation) slashing the pumper's trust |
| Nuke a competitor | trust burned per wrong review | log-penalty ≈ 3.5× the honest gain per review; a trusted wallet self-slashes below θ within a handful of contradicted reviews |
| Consensus echo (farm trust by copying scores) | time | ~0 gain — proper scoring rule pays only for information beyond the prior |
| Whitewash (rotate resource identity) | redeploy | restart at prior with n_eff = 0; spend policies should prefer proven resources |

Residual risks, accepted for v1: (a) an earn-then-pump attacker gets a
temporary partial lift on an otherwise-unreviewed resource — mitigated by n_eff
display and shrunk further by clustering in v2; (b) trust is not yet
re-evaluated retroactively when consensus later shifts — the nightly sweep and
the v2 fixed-point iteration address this; (c) a colluding cluster that first
earns trust honestly across many wallets — v2 clustering (funding lineage,
co-spend, timing) caps per-cluster weight per resource.

## 8. Architecture

### 8.1 Review write path

The write path validates and stores each unique paid outcome, then refreshes
the affected resource's score using current trust in one transaction:

1. Admissibility gates (§3.4); reject or admit.
2. Gateway attestation (§3.6), when one is supplied: verify, cross-check
   against the review being stored, and decide `verified_source`, `owner_ref`,
   `payee_owner_ref` and `weight_exclusion`. A failure rejects the review
   rather than downgrading it.
3. Store the review, its verified payment metadata and the attestation verbatim,
   so any published score can be re-derived and re-verified from its evidence.
4. Recompute and store the resource's score (`score`, `n_eff`), with owner
   bucket aggregation applied by the canonical scoring function.

Reads apply time decay at query time (or read the stored row; staleness from
decay alone is bounded and corrected by the sweep). All reads — MCP
`get_service_score`, `/api/services`, `/api/services/top` — go through **one**
canonical scoring function in `src/crowdcode/scoring.py`.

### 8.2 Cron (Render Cron Job service, one entrypoint, dependency order)

1. **Nightly consistency sweep** — replay one owner/resource bucket at a time,
   recompute trust and scores from scratch, and alert on drift. Trust updates
   happen only here, so bursts of paid calls cannot multiply either score or
   reputation influence. This job is also where a future EigenTrust
   fixed-point iteration can live.
2. **Per-resource review summaries** (LLM) — only for resources with reviews
   newer than `last_summarized_at`; summarizer input is trust-weighted (wallets
   below θ do not contribute when trusted reviews exist). Cold-start summaries
   include authenticated unpaid reviews as well as verified payments, with the
   payment status retained. Output is a constrained
   factual format (strengths / failure modes / caveats) treating review text
   as untrusted data; passes egress redaction; served inside
   `get_service_score` and on the site next to the raw rating histogram,
   labeled "AI-generated from N reviews through <date>".
3. **Requested-services summary** (LLM) — same watermark pattern.

### 8.3 Schema changes

- `review_owners` table: `owner_ref` (primary key), `created_at`, `is_seed`,
  `raw_trust`, `trust_updated_at`, `slashed_at`. This is where weight lives.
- `wallet_users` table: `user_id`, `wallet_address` (unique), `created_at`,
  `is_seed`, `raw_trust`, `trust_updated_at`, `slashed_at`. Reviews FK to it.
  It remains the reviewer identity; `is_seed`/`raw_trust` there are vestigial
  and no longer consulted by scoring. The existing salted `reviewer_id` remains
  the public/egress identifier. (Named `wallet_users`, not `users`: the database
  already carries an unrelated `public.users` table from another app.)
- `reviews` gains `verified_source`, `weight_exclusion`, `owner_ref`,
  `payee_owner_ref`, `receipt_key_id` and `gateway_attestation` (§3.6), with a
  unique index on the attestation's `receipt_sha256`.
- `review_owners.is_seed`: root owners pinned at 1.0, synced from
  `CROWDCODE_SEED_OWNERS` at startup and on each cron run.
- `reviews.amount` extracted from `payment_proof` **after** the token-pinning
  and amount-validation checks pass.
- `reviews(owner_ref, service_id)` partial index supports the owner bucket
  access path; existing review RLS remains default-deny.
- `services`: `resource_type`, `score`, `n_eff`, `score_updated_at`,
  `last_summarized_at`; verify `created_at` exists on `services` and
  `service_requests`.

### 8.4 Backtest (the accuracy metric)

Replay all reviews in time order; for each review by a wallet whose weight is
above θ at that moment, record `|published_score_before − rating|`. Report the
rolling aggregate publicly ("our score predicts the next trusted review within
±X stars"). Re-fit μ₀, κ, η, proof multiplier, and decay half-life against it
periodically. Version the algorithm (this doc = v1); scores are recomputable by
third parties from public signed reviews and on-chain payments.

## 9. v2 roadmap (deferred, deliberately)

1. **Full EigenTrust fixed point** — iterate the trust update over the whole
   graph to convergence in the nightly job. Needed once
   enough non-seed wallets hold trust that *their* corroboration of third
   parties should compound, and it retroactively re-scores old reviews against
   shifted consensus (slashing earn-then-pump attackers).
2. **Wallet clustering** — funding lineage, co-spend, temporal correlation;
   per-cluster weight cap per resource; cluster-wide slashing.
3. **Per-type priors and proof signals** — fit μ₀/κ per resource type; richer
   proof-of-use tiers where honestly verifiable.
4. **Graded ratings model** — the works/failed Bernoulli likelihood generalizes
   to an ordinal model over 1–5 once real rating distributions are observed.
