# Changelog

## Unreleased

- **Leaderboard priors are 3–5, not 1–5.** The old mapping, 1 + 4 ×
  percentile, gave the last-ranked arena model 1.0. On the judge's scale that
  means a failed purchase, so veo-3.1 (16th of 21) scored worse before any
  purchase than an unknown endpoint with no model at all (reported by W4).
  - **Elo boards** (video, image, speech): `prior_rating` is now
    5 − min(2, Elo gap to the board's leader / 150).
  - **The language-model Intelligence Index:** 3 + 2 × percentile.
  - The ordering within a board is unchanged.

- **Search over the whole index** (SRI-115 C13). `/api/services/find` now
  searches every offering in the index (about 48k, in memory, a few
  milliseconds a query) instead of a live search of the Bazaar's top 12 and
  mpp.dev. It answers within the capability leaf or model asked for, and ranks
  by a seeded Thompson draw of delivery × quality per effective dollar, with
  `bad` left out. Each result says its `tier` and `why`, its `model`, price
  and networks, and `explore: true` when the draw tried it ahead of the
  best-known option; the response returns its `seed` (pass it back to
  reproduce a list) and is about 700 tokens for five results. The live search
  remains the fallback. The evaluation set grows to 80 cases (video, speech,
  image editing, model names), each with the other offerings an independent
  judge found to answer it (`also`, every verdict in `judged.json`): recall@3
  0.533 → 0.772 and MRR 0.765 → 0.855 over the 80; on the original 60,
  0.589 → 0.742 and 0.833 → 0.837.
- **reliability-v2 for listed offerings.** `GET /api/reliability?url=` now
  evaluates an endpoint the index lists but does not track, using its Bazaar
  payers, the mpp.dev catalog and the leaderboard prior of the model it sells.
  It answers tier `new`, with a real prior, posterior and score, instead of
  only "not tracked". A tracked offering that has not been probed yet now says
  so, rather than "probes are not healthy".
- **Delivery evidence for reliability-v2 (SRI-115 Phase 2, C10 and C11).** Backend only; apply `supabase/judge-v2-evidence.sql` (idempotent) before deploying.
  - **Old `partial` verdicts re-read (C11).** The hosted gateway's judge now grades `delivered` or `not_delivered` against the request the endpoint received, with `task_fit` apart (`opencrowd.judge.v2`). A new cron step, `partial_reread`, re-reads each `opencrowd.judge.v1` `partial` once with `crowdcode.rubric.reread_partial`: delivered unless the judge's own text names an endpoint defect (an error status or body, a failed call, a timeout, an empty or malformed body, the wrong thing, a mock, a charge with no result). It writes `reviews.delivery_reread` and `delivery_reread_note` beside the signed attestation, which is never altered; reliability-v2 reads `coalesce(delivery_reread, attestation delivery)`.
  - **`POST /api/purchase-failures` (C10, beta+1).** The hosted gateway reports a paid call the seller broke before settlement, signed with its receipt key (`opencrowd.purchase_failure.v1`). It is stored as an `index_observations` row with `kind = 'purchase'`, `ok = false`, no review, `detail` set to `seller_error`, `seller_rejected`, `no_receipt` or `authorization_expired`, plus `attempt_ref` (unique) and `traffic`. Unsigned, tampered, stale or unknown records are refused.

- **The capability tree, Phase 2** (SRI-115 C10, C13, C14). The tree now
  covers every payable listing, not only tracked ones. Each offering has a
  tier (`proven`, `popular`, `new`, `bad`) from C10's Beta estimate. Leaves
  show the best-known picks (proven or popular, by expected delivery × quality
  / price) and up to 3 exploration candidates from a Thompson sample, each
  with its tier and reason. Video, image and audio have the owner's fixed
  split (generate / edit / transcribe-or-access, generate / edit / analyse,
  TTS / transcription / music), and their leaves list models (Veo 3.1, Kling
  v3, Seedance, Sora 2, Wan, Grok Imagine, …) with their sellers. Versions
  stored before this still render as they did.

- **Offerings, model identity and coverage** (SRI-115 C9).
  - **Coverage:** the index lists every listing from the Bazaar, PayAI,
    Dexter and thirdweb (about 47.6k resources, deduplicated by normalized URL
    and method; thirdweb's method-less items join the resource another feed
    listed). Every paid mpp.dev endpoint is tracked as `curated`. That brings
    in StableStudio, fal-queue, Kling, HeyGen and OpenRouter's video
    endpoints.
  - **Model identity:** each offering's model comes from the seller's own
    `/models` catalog (mpp.dev services that publish one, and BlockRun), its
    route, or a description that names exactly one model. Names normalize
    across sellers and leaderboards (`google/veo-3.1`, `fal-ai/veo3.1` and
    "Veo 3.1" are all `veo-3.1`).
  - **Storage:** `index_resources` gains `model`, `model_family` and
    `modality`. `index_offering_models` holds one row per offering and model,
    with the seller's id and pricing.
  - **Quality priors:** `model_quality_priors` stores weekly Artificial
    Analysis leaderboard scores (video, image, speech Elo and the LLM
    Intelligence Index) with source URL and date, and `prior_rating` on the
    judge's 1–5 scale.
  - **Probes:** a media generator with no documented body is asked for a quote
    with a short prompt, plus one of its catalog's models.
  - **Health:** `/api/index/health` adds counts per source, models per
    modality, and the priors' date.
  - **Schema:** `supabase/offerings-models.sql`.

- **reliability-v2** (SRI-115 Phase 2, contract C10). Every offering now has a
  delivery estimate, a quality estimate, one score and a tier.
  - **Delivery:** `Beta(alpha, beta)`. The prior comes from outside evidence:
    ≥ 10 Bazaar payers with ≥ 1.5 calls each gives `Beta(8,1)`; ≥ 3 payers,
    or the mpp.dev curated catalog, gives `Beta(3,1)`; otherwise `Beta(1,1)`.
    Each judged purchase in 30 days adds to alpha or beta. So does each paid
    call that failed before settlement through the seller's fault (W2's
    signed records). Old `partial` verdicts count once C11's re-read resolves
    them (`reviews.delivery_reread`).
  - **Quality:** the v3.1 review score, re-shrunk to the model's leaderboard
    prior (`model_quality_priors`, C9) when there is one.
  - **One score:** delivery mean × quality, on CrowdCode's pages. The API's
    `score` stays the v3.1 review score (clients validate it as 2–5).
    `/api/services` and `/api/services/{id}` add `tier`,
    `reliability_score` and `reliability_state`.
  - **Tiers:** `bad`, `proven`, `popular` and `new`. `state` stays for v1
    readers: `bad` is `unreliable`, `proven`/`popular` are `reliable`, `new`
    is `unverified`.
  - **API:** the reliability endpoints add `tier`, `posterior`, `quality` and
    `score`, and `GET /api/reliability?tier=` lists one tier. Unknown
    endpoints are `new`.
  - **Schema:** re-apply `supabase/reliability.sql` (it adds `tier`, `alpha`,
    `beta`, `quality` and `score`).
- **`kind` (gap or leverage) and `unlocks` are gone from service requests** (owner decision, 2026-11-17). `request_service` no longer takes them; FastMCP ignores them when an older client still sends them, so nothing is rejected for it. `validate_request_extras(builds_on, frequency, capability)` keeps `builds_on` required in strict mode. The screen accepts a reusable paid service that would fix the described failure, poor result or cost, without the act/reach/persist/verify wording. Proposals no longer carry `kind` or `unlocks`, and the payload drops `listed_by_kind`. The `request_kind` and `unlocks` columns stay, holding what earlier rows stored; nothing writes or reads them.

- **Feed redaction for crowdcode.app's home feed** (SRI-117). CrowdCode's
  reconciler posts the text of runs whose owners chose to share them on the
  public feed to `POST /api/internal/feed/redact` (same
  `CROWDCODE_DEMAND_MINER_TOKEN`), at most 40 texts of 4,000 characters per
  call. Fail-closed: an unreachable redactor is a 503, and `redacted: false`
  (no redactor configured) tells the caller to publish nothing.
- **The nightly demand miner** (SRI-115 ticket 11, contract C8). CrowdCode's
  reconciler posts summaries of the previous day's hosted runs (never
  transcripts; secrets already scrubbed; owners who opted out are never read)
  to `POST /api/internal/demand-miner/batch`, authenticated with
  `CROWDCODE_DEMAND_MINER_TOKEN`.
  - **One model call per batch** (the proposals model, low reasoning effort)
    proposes leverage and gap services in the C6 shape. Each must pass the
    request_service validation, a traceability check (no URL, domain, email,
    handle, id, address, file name, long number or redaction placeholder),
    the eight-word copy check against the batch, the same model screen (which
    fails closed here) and the redactor once more.
  - **Storage:** survivors become `service_requests` with `source = 'miner'`,
    no requester wallet, and only the reconciler's HMAC run references
    (`miner_run_count`, `miner_run_refs`). `demand_miner_batches` makes a
    retried batch a no-op. Apply `supabase/demand-miner.sql` first.
  - **Novel needs only:** the miner is told the published proposals (title
    and problem, already public) and proposes only needs none of them covers.
    A candidate needs at least two runs behind it (`single_run` otherwise).
  - **Same fields as agent requests:** mined needs carry input, output,
    problem, price_per_call, builds_on, frequency and capability. The miner
    no longer asks for or stores `kind` or `unlocks`.
  - **Cost:** every call is costed and capped per UTC day
    (`CROWDCODE_DEMAND_MINER_DAILY_USD`, default $0.24); the response reports it.
  - **Proposals** now carry `source` (`agents`, `miner` or `both`) and
    `demand.runs` / `demand.mined`; the payload adds `listed_by_source`. A
    proposal built on mined demand that names anything traceable is not
    published.

- **The reliability rule** (SRI-115 ticket 6, contract C3). Every tracked
  service now has a state: `reliable`, `unverified` or `unreliable`.
  - **The rule:** `reliability-v1`, in `crowdcode.reliability`, with every
    threshold in one place. The state comes with plain-sentence reasons and
    every input the rule read, so anyone can recompute it.
  - **Evidence:** daily probes (a refused probe body is inconclusive),
    payee changes from `index_changes` (addresses the service already
    advertised, and rotating payees, do not count), judged purchases from
    gateway attestations (`partial` counts as not delivered; CrowdCode's
    test purchases count and are labelled), and crowdcode-mcp observations
    (payers and liveness, never delivery).
  - **Storage:** `service_reliability`, one row per tracked or blocked
    service, rewritten only when its answer changes. The cron recomputes
    every hour after the probes (`reliability` in `/api/cron/status`), and
    a judged review refreshes its service at once.
  - **Blocking:** `scripts/block_service.py` blocks or unblocks a service
    with a public note. Blocked forces `unreliable`. Every change is in
    `service_block_audit`.
  - **API:** `GET /api/services/{id}/reliability`,
    `GET /api/reliability?url=&method=` (resolved through the index and the
    registry's aliases; an unknown endpoint is `unverified`, "not in
    CrowdCode's index"), and `GET /api/reliability?state=reliable`.
  - **Schema:** apply `supabase/reliability.sql` before deploying.

- **The capability tree** (SRI-115 ticket 8, contract C7). The backend now
  builds, every night and after reliability changes, a tree of what agents
  buy: 15 hand-written top nodes (with when to look and when not to buy), and
  leaves proposed by a model from the index's descriptions and schemas. A leaf
  is kept separate only when a different reliable service wins it. Each leaf
  lists its reliable picks with reasons, the unverified candidates worth
  testing next, and the proposals that match it. `GET /api/tree` serves the
  version, the top level and an 800-token summary for agents;
  `GET /api/tree/{node}` serves one node in 600 tokens. Classification runs
  only on new or changed resources and is capped at $1 a day. Apply
  `supabase/capability-tree.sql` before deploying.
- **The services index** (SRI-115 tickets 1 and 2, contract C1). The backend
  now keeps its own index of the x402 and MPP services that matter, and
  checks each of them once a day.
  - **Sources:** every hour the cron reads the CDP x402 Bazaar and mpp.dev
    (each feed once a day), plus hosted services, gateway purchases and
    reviewed endpoints.
  - **Storage:** `index_resources` holds one row per `(method, route
    template)`, with CAIP-2 networks and a `payable` flag. dripstack's
    per-article URLs become one template.
  - **Tracking:** a resource is tracked if it has ≥ 3 Bazaar payers in 30
    days, a gateway purchase, is a hosted service, or matches a proposal.
    That covers 99.7% of the Bazaar URLs with ≥ 10 payers (3.8% before).
    Each tracked resource is linked to one `services` row. Rows the index
    creates stay out of the directory and search until someone reviews them.
  - **Probes:** each tracked resource gets one unpaid request a day, spread
    across the hours. It identifies itself as `CrowdCodeIndex/1
    (+https://crowdcode.app/bot)` and always sends a JSON body: the seller's
    own example, or for hosted services one built from the route schema.
  - **History:** price, payee and liveness changes (down, dead, revived) go
    to `index_changes`. `index_resource_signals` holds what the reliability
    rule reads.
  - **API:** `GET /api/index/health` and `GET /api/index/resource?url=&method=`.
  - **Schema:** `supabase/services-index.sql`. Sellers can opt out through
    `index_opt_outs`.
  - **Schedule:** `render.yaml` now says hourly, which is what production
    already runs.

- **Paid-call observations (SRI-115 C5).** `POST /api/observations` takes one
  signed record of a paid call from crowdcode-mcp:
  `{observation: {endpoint, method, network, payment_reference, amount_atomic,
  status, response_bytes, observed_at}, signature}` (EIP-191 over
  `crowdcode.observations.observation_message`), plus `payer_wallet` and an
  ed25519 `payer_signature` over the same message for Solana. Any other field
  is refused, and so is an endpoint with a query string. The settlement must
  be a USDC transfer from the signer (Base, Tempo) or the signing Solana payer,
  to a payee the endpoint's service is registered with or its own 402
  advertises (a cooled-down probe, no connection held). Stored once per
  settlement as `index_observations.kind = 'observation'` with the amount
  that actually settled; 200 per wallet per 24 h
  (`CROWDCODE_OBSERVATION_LIMIT_PER_DAY`). A transaction the RPC cannot see
  yet answers 503 `retryable`. Apply `supabase/observations-and-requests-v3.sql`
  first.
- **Service requests v2 fields (SRI-115 C6).** `request_service` takes `kind`
  (`gap` or `leverage`), `builds_on`, `unlocks`, `frequency` and an optional
  `capability`, stored on `service_requests`. "N/A" filler and a `builds_on`
  that says nothing exists are rejected outright; then one call to the
  proposals model (`crowdcode.request_screen`) rejects task restatements and
  non-services before anything is stored. It fails open (`screen_verdict =
  'unchecked'`) and holds no connection. `CROWDCODE_REQUESTS_V2_STRICT=0`
  accepts, during the rollout only, a client that sends none of the new
  fields. Proposals read the new fields, no longer mark human-ops or
  multi-day services unrealistic when `builds_on` is plausible, and publish
  `kind`, `builds_on`, `unlocks`, `frequency`, `capability` and the payload's
  `listed_by_kind`.

- CrowdCode's hosted services are kept in the index by CrowdCode itself
  (SRI-111). They used to enter it only when first reviewed, under whatever
  URL the reviewer used, so a service nobody had reviewed (`/s/crowdcode`) was
  missing from the rankings, and a renamed one (`venice-reseller` →
  `venice-proxy`) kept its review under the old URL, where its owner's page
  found nothing. Control now pushes its whole list after every deploy, rename,
  stop and delete to `POST /api/internal/hosted-services/sync` (bearer
  `CROWDCODE_HOSTED_SYNC_TOKEN`; unset disables it). Each route's row is found
  by a stable key (`crowdcode_service` identifier: CrowdCode service id and
  route), else by any URL it has had; its canonical endpoint moves to the
  current URL and every old URL stays an `api_endpoint` identifier, so reviews
  naming it still land there. Live services are created without waiting for a
  review; deleted ones (`metadata.hosted_status`) leave the rankings and
  search, and keep their reviews. `/api/services` and `/api/services/{id}`
  now name the `crowdcode_service_id`.

- A service that collects at more than one payee per address family can be
  reviewed at any of them. Exa (`https://api.exa.ai/search`) advertises two
  Base payees in one 402 and was registered on the one the hosted gateway does
  not pay, so 62 hosted reviews in the week to 2026-11-02 were refused as
  `service identity conflict` (SRI-69). A service's payees are a set — its
  canonical payee plus every `payment_target` identifier — and when a supplied
  payee is the only reason a lookup conflicts, `crowdcode.advertised` asks the
  service's registered endpoint for its 402, registers every payee it
  advertises (`metadata.advertised_payees` keeps the evidence), and accepts
  the payee if it is one of them. The probe holds no database connection,
  follows no redirect, refuses non-public hosts and runs at most once per
  service every ten minutes; a payee the 402 does not advertise is still a
  conflict.

Two directory data repairs. Apply `supabase/rotating-payees.sql` and
`supabase/platform-defect-reviews.sql`, deploy the backend and cron, then run
`scripts/fix_machine_payees.py --apply` and
`scripts/exclude_platform_defect_reviews.py --apply`.

- A machine-payment service registered with a non-address payee could never be
  reviewed again. `svc_e91d4c0dfb719d9fefe8` ("Parallel MPP Search") held the
  literal string `parallel` — its own directory slug — in `payment_target_ref`,
  the column that *is* the payee address, so it had no canonical payee of any
  address family while a stale alias held the EVM slot, and every real payee
  resolved to `service identity conflict`. `normalize_payment_target` now
  refuses to store one; `scripts/fix_machine_payees.py` repairs the rows that
  predate that guard, reading the payee from the service's own live 402.
- `services.payee_rotates`: a service may issue a single-use payee with every
  402, which is what Parallel turned out to do — a fresh 0x address per call,
  on Base and on Tempo. Its payee cannot anchor its identity, and pinning the
  first one seen is what refused every later purchase, so for a marked service
  resolution authorizes any payee of a machine-payment address family, stores
  none, and registers no alias; identity rests on the endpoint and on the
  gateway receipt, which names the payee the ledger actually paid. Only an
  operator sets it, and only when two probes of the service's own 402
  advertise different payees (docs/shared-payment-wallets.md).
- A review of a failure CrowdCode caused is no longer evidence about the
  provider. Three of our bugs made hosted agents file 1-star reviews against
  providers that were never called: a gateway that misread valid x402/MPP
  offers, a reputation floor that refused a listing nobody had classified, and
  an agent refused the network the seller asked to be paid on.
  `weight_exclusion = 'platform_defect'` keeps the row and its rating verbatim
  — the mistake was ours and deleting the record of it would hide it — and
  takes it out of every published count, histogram, average and generated
  summary, with the reason in `reviews.exclusion_note`. Only an operator
  writes it, from `scripts/exclude_platform_defect_reviews.py`; the review
  write path cannot.
- Which reviews are published and weighed is now one definition
  (`crowdcode.review_sql`) shared by the web service and the cron, instead of
  a constant in `server.py` and three copies of the same predicate spelled out
  by hand in the queries that join `reviews`.

## 0.12.0 — 2026-10-27

Private reviews and request_service v2. Apply `supabase/request-service-v2.sql`
before deploying the backend and cron, then run the cron once
(`crowdcode-cron`) to regenerate every review summary and publish the first
service proposals.

- Raw review text is never published. `get_service_score` and
  `/api/services/{id}` drop `recent_reviews`; the knowledge export drops
  `reviews` and gains the rating histogram and median paid amount. Reviews are
  still stored (scores stay re-verifiable) and their owner can still list them
  with `list_my_reviews`.
- Review summaries are pros / cons / things to watch out for (`summary.pros`,
  `summary.cons`, `summary.watch_out`), written in general terms. An item that
  repeats a review's words is discarded. Summaries written before this version
  are not served and are regenerated by the next cron run.
- `request_service` takes four required fields: `input` (the clear input the
  user will give), `output` (the exact output they want), `problem` (what it
  solves) and `price_per_call` (USD). Vague or missing fields are rejected with
  `invalid_request_fields` and a reason that says what to fix.
  `service_description` and `task_context` are gone.
- Requests are aggregated nightly into generalized service proposals by a
  capable model (`CROWDCODE_PROPOSAL_MODEL`, default `gpt-5.5`) reviewing each
  as a product: realism (would anyone pay per call?) and clarity (could a
  developer start building it?). Only proposals that pass are published, at
  `GET /api/service-proposals`, with demand and the median and distribution of
  `price_per_call`; the rest are counted by reason and hidden. Old free-text
  requests are mapped into the new fields once and marked low confidence.
- `/api/project-ideas` is derived from the proposals and no longer carries
  example request text; the old cached payload is deleted.

## 0.11.0 — 2026-10-23

Keep the pre-cutover scores (`crowdcode-scoring-v3.1`, docs/SCORING.md §3.7).
Apply `supabase/legacy-grandfather.sql` after `hosted-only-reviews.sql`, deploy
the backend and cron, then run
`crowdcode.cron.run_consistency_sweep(utc_now())`.

0.10.0 applied the hosted-receipt rule retroactively. Nothing written before it
has a receipt, because receipts did not exist, so every one of 334 scored
services fell back to the 3.0 prior and 811 reviews — 739 with an
on-chain-verified payment — stopped counting. That discarded months of real
evidence to fix a forward-looking problem.

- `reviews.weight_basis` records which era's rules weigh a row. Reviews that
  already existed at the cutover are `legacy` and keep the weight v2 gave them:
  the proof multiplier (1x signed, 2x on-chain) in a wallet/service/UTC-day
  bucket, against a wallet-trust snapshot frozen at the cutover. Everything
  written since is `hosted` or `none`. A score is the sum of both eras.
- `n_eff` is published split: `n_eff_legacy` and `n_eff_hosted`, on `services`
  and in every score payload, because the two eras are on different scales.
  Score payloads also gain `num_legacy_reviews`, and each review reports its
  `weight_basis`.
- The wallet trust behind legacy rows is **frozen**. No code path writes
  `wallet_users.raw_trust` or `is_seed` any more; `load_legacy_trust` only
  reads. A wallet cannot earn, lose or be granted trust, so the snapshot can
  only ever weigh the finite set of reviews that already cited it — the
  wallet-trust loop 0.10.0 closed stays closed. The corpus also keeps decaying
  on its original 180-day half-life.
- No new review can reach the legacy rules: `weight_basis` is assigned once by
  the backfill, keyed on a fixed cutover timestamp, and the write path only
  ever produces `hosted` or `none`.
- The leave-one-out consensus behind owner trust now reads legacy evidence, so
  a new owner can earn trust by agreeing or disagreeing with the existing
  directory rather than only on services a seed owner happened to buy from.
- The nightly summarizer accepts whichever era weighs a review.

## 0.10.0 — 2026-10-22

Scoring v3: only hosted, judged purchases carry ranking weight
(`crowdcode-scoring-v3`, docs/SCORING.md §3.6). Apply
`supabase/hosted-only-reviews.sql`, set `OPENCROWD_RECEIPT_KEYS` and
`CROWDCODE_SEED_OWNERS`, deploy the backend and cron, then run
`crowdcode.cron.run_consistency_sweep(utc_now())`. Most scores return to the
3.0 prior with `n_eff` 0, displayed as "unproven", until hosted owners with
earned trust review those services.

- `review_service` accepts a `gateway_attestation`: a purchase receipt and a
  review attestation, both signed with Ed25519 by a hosted OpenCrowd gateway,
  binding `sha256` of the request and the response to the settlement, the
  payee, the amount and the owner, and binding that receipt to a platform
  judge's rating and published text. Only reviews carrying a valid one carry
  ranking weight. Everything else — CLI wallets, crowdcode-mcp users, every row
  written before this release — is still accepted, published and labelled as
  community evidence with weight exactly zero. A forged, edited, stale, re-aimed
  or replayed attestation rejects the review outright and stores nothing.
- Weight is held per **owner**, not per wallet, in the new `review_owners`
  table, and an owner gets **one vote per service** however many agents,
  wallets, days or purchases produced it. `n_eff` is now literally the number of
  trusted owner votes behind a score.
- A review of a service the reviewer's own owner is paid by, and a review citing
  a payment below `CROWDCODE_MIN_WEIGHTED_AMOUNT_ATOMIC` (default 1000 =
  $0.001), verify and carry no weight (`weight_exclusion`).
- Trust is no longer granted to hosted wallets. `supabase/opencrowd-wallets.sql`
  and the `opencrowd_seed_wallets` bridge that pinned every registered mainnet
  `agent_eoa` at 1.0 are removed; `CROWDCODE_SEED_OWNERS` replaces
  `CROWDCODE_SEED_WALLETS` as the root of trust, and the backend no longer
  reads the hosted schema at all.
- Unsigned placeholder reviews (the legacy `stripe`/`manual` admission path) are
  never published. They are retained for continuity and excluded from every
  public output: counts, histograms, recent reviews, the knowledge export and
  the nightly summarizer.
- `api_endpoint` must be an `http` or `https` URL, in the backend and in the MCP
  client's canonical identity module. `javascript://host/%0aalert(1)` parses
  with a host and was previously storable and renderable as a service endpoint.
- New public `GET /api/receipt-keys` lists the gateway receipt keys this
  deployment verifies, so a published score can be checked against its evidence
  by anyone. Score payloads gain `num_weighted_reviews`,
  `num_community_reviews` and `weighted_avg_rating`; reviews gain
  `verified_source` and `counts_towards_ranking`; service detail gains
  `weighted_histogram`.

## Unreleased

- `/api/services` and `/api/services/{service_id}` report each service's
  payment networks: `verified_networks` (networks with verified payments, with
  a `verified_payments` count, most used first), `advertised_networks`
  (networks pinned by registered payees: an MPP payee settles on Tempo, a
  base58 x402 payee on Solana; an EVM x402 payee names no chain), and
  `networks` (both, verified first). A verified review's network comes from
  its recorded verification network, then its reference prefix
  (`x402:<network>:`, `mppx:tempo:`), then the rail default (bare 0x hash:
  x402 on Base, mppx on Tempo).
- The backend calls the redaction sidecar in chunks of 10 texts, retrying
  each chunk once. The cron's request and review summaries and the knowledge
  export allow 60 s per chunk. Previously the request summary sent up to 100
  texts in one 10 s call; after a sidecar restart cleared its memo the call
  timed out, every request was dropped, and the website's request list became
  empty. The request summary now drops only chunks the sidecar cannot process,
  and when it cannot process any it fails the job and keeps the last
  published summary. A connection failure (the sidecar restarting after a
  deploy) waits 5 s before the retry.

- Verify x402 payments settled on Solana mainnet. Reference them as
  `x402:solana:<base58 signature>` (case-sensitive; aliases `solana` and
  `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`) with the Solana payee as
  `payment_target_ref`. The backend fetches the transaction over JSON-RPC
  (`SOLANA_RPC_URL`, default `https://api.mainnet-beta.solana.com`), requires
  success and an SPL USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
  transfer from the payer's token account to the payee's. A new
  `review_service` field, `payer_signature` (base58 ed25519 by the Solana payer
  over the payer link message in `spec/CANONICAL_PAYLOAD.md`, optionally with
  `payer_wallet`), links the payer to the EVM reviewer wallet and earns
  `onchain_verified`. Without it the transfer is recorded as observed and the
  review is admitted at `signature_only` under a reviewer-scoped key, without
  reserving the reference, so nobody can claim or block a Solana payment they
  did not send. A service may hold one EVM and one Solana payee; the first
  verified Solana payment registers the latter. `cryptography` (already
  installed through `mcp`) is now a direct dependency for ed25519. No schema
  migration is required.

- Verify x402 payments settled on Monad (`eip155:143`) like x402 on Base:
  a USDC (`0x754704Bc059F8C67012fEd69BC8A327a5aafb603`) `Transfer` from the
  reviewer wallet to the service payee. Reference a Monad settlement as
  `x402:monad:<tx>`, or send a bare hash with a `payment_proof` whose `network`
  is `monad`, `eip155:143`, or `143`. A bare hash without a proof is still
  checked on Base. The chain is stored in `payment_verification_metadata.network`
  and reused by cron re-verification. Configure `X402_MONAD_RPC_URL` (falls back
  to `https://rpc.monad.xyz`); `X402_MONAD_USDC_ADDRESS` overrides the token pin.

- Enable row-level security on `deleted_review_keys` in `supabase/schema.sql`.
  Existing deployments should rerun the idempotent schema or apply
  `alter table deleted_review_keys enable row level security;` so the table
  is default-deny for the Supabase anon/authenticated roles like the others.

- Automatically seed registered hosted OpenCrowd mainnet agent wallets at trust
  1.0 using an optional operator-owned database view. Apply
  `supabase/opencrowd-wallets.sql` in the shared deployment, then sync seeds and
  replay scores. Validate explicit seed addresses to reject pasted env settings.

- Raise the public service-score prior from 3.0 to 4.0 (strength remains 2),
  labeled `crowdcode-scoring-v2`. Keep reputation's internal consensus prior
  at 3.0 so the change grants no trust to unsupported fresh wallets.
- Update new-service database defaults. Existing deployments must apply
  `supabase/scoring-v2.sql` and replay scores before serving the new algorithm;
  see `docs/SCORING.md`.

## 0.5.2 (backend) — 2026-09-18

- Resolve services by endpoint/id independently of shared payment wallets. Keep payment destinations service-scoped, reject ambiguous wallet-only lookup, and serialize first-review registration. Deploy backend first, then apply `supabase/shared-payment-wallets.sql`; it preserves existing service IDs and reviews.

## 0.5.1 (backend) — 2026-08-27

### Multi-protocol payment identity (field note 001, issue 1)
- One endpoint/payee may now accept both machine payment rails. Identity
  resolution anchors on the EVM payee address for mppx/x402: a supplied pair
  whose payee matches the service's registered payee resolves even when the
  protocol label differs from the registration (e.g. an x402/Base payment to
  an endpoint registered as mppx/Tempo). A pair naming any other payee still
  fails with `service identity conflict` — resolution never redirects
  verification to an unregistered payment destination.
- Pair-only lookups (`payment_provider` + `payment_target_ref`) fall back
  across machine protocols and match the payee address case-insensitively.
- After an on-chain-verified review, the observed (protocol, payee) pair is
  registered as a `service_identifiers` row, so the service durably
  advertises every rail its payee accepts.

No schema change; deploy the backend to pick this up.

## 0.5.0 (backend) — 2026-08-10

### Integration hardening
- Every MCP tool now returns an additive status envelope (`status`,
  `error_code`, `retryable`). Dependency failures return `unavailable` with a
  correlation ID and never expose raw exception details; score misses remain
  distinguishable as `not_found`.
- Added `/health` liveness and `/ready` dependency readiness checks. The Render
  web service now uses the always-on Starter plan and gates traffic on
  database plus redactor readiness.
- New x402/mppx reviews fail closed unless the payer signature and supported
  on-chain transfer verify. RPC outages and missing verifier configuration are
  retryable but store nothing and reserve no payment reference. x402 Base and
  mppx Tempo are supported; Solana and other chains are rejected explicitly.
- Duplicate lookup now runs only after payer verification, preventing a third
  party from probing or reserving a public transaction hash before its payer.
  The canonical unique index remains the concurrency backstop.
- Every unique verified payment can be reviewed. All paid outcomes remain
  stored, while score and trust influence are capped to one proof/decay-
  weighted wallet/service/UTC-day bucket. Trust is updated authoritatively in
  the nightly replay, once per bucket.
- Added the composite review index used by daily aggregation. Existing RLS
  remains default-deny; no new table or permissive policy was introduced.

Apply `supabase/schema.sql` before deploying this version.

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
