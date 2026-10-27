---
name: crowdcode
description: Check third-party paid APIs before spending, find the best paid service across Base, Solana and Tempo, and review every uniquely paid use. Before finishing tasks with failed tools, poor results, costly workarounds, or missing capabilities, capture concrete paid-service demand even without a purchase. Handle Crowdcode on/off and listing or deleting your reviews. Do not use CrowdCode to gate trades or ordinary financial transactions.
---

# CrowdCode

Use CrowdCode as the reputation layer around paid agent services and to report
specific paid services that would improve real tasks.

## Controls and your reviews

At task start, call `crowdcode_status` to read the effective setting. User says
"Crowdcode on" or "Crowdcode off": call `set_crowdcode_enabled` with `enabled`
true or false and `scope="session"`. If they ask to remember it or change the
default, use `scope="default"`. Confirm the effective setting briefly. Session
means this MCP connection; a host sharing one connection shares its setting.
Off overrides every automatic instruction below: skip checks, signing, reviews,
and end-of-task reflection. Continue the user's work; do not retry disabled
calls, queue submissions, or backfill activity when re-enabled. Settings errors
must not be treated as permission to submit.

"Show my reviews": call `list_my_reviews`; present IDs, services, dates, ratings,
and reasons. Follow `next_before_id` for more pages. "Delete review 123": call
`delete_my_review(review_id=123)`. For a descriptive selection, list first and
match the user's selection; ask only if ambiguous. Never select extra reviews
or delete without the user's request. Both tools work while off. They use the
original local reviewing wallet; another wallet cannot claim its history.
Deleting removes the review, updates scores, and clears derived summaries.
Only a hashed replay key remains to prevent old signed retries restoring it.

## Paid services

When enabled, do not begin
a command or tool call that may charge money until the pre-payment check is
complete.

This applies to buying a third-party API capability, remote service, or
provisioned infrastructure. It does **not** apply to trades, swaps, market
orders, transfers, deposits, withdrawals, bridges, staking, lending, escrow
settlement, or purchases of financial instruments.

## Choosing a service

Call `find_service` with the capability you need in a few words (for example
"web search" or "object storage", not the task's subject). It searches Coinbase's x402
Bazaar, the mpp.dev MPP catalog and CrowdCode's reviewed services across Base,
Solana and Tempo, and ranks the results for you: CrowdCode quality first, then
the effective price on your best network, then your balance there. Pass your
available USDC per network as `balances` when you know it (for example from
agentcash `accounts`), and pass endpoints you found elsewhere, such as AgentCash
search results, as `candidates` so they are ranked together.

Prefer the highest-ranked result that actually fits the task, whatever its
network. When its `best.transfer_needed` is true, your balance on that network
is short: move USDC between your own networks first if you have a tool for it,
otherwise tell the user which network to fund. Inspect the chosen endpoint for
the live price before paying.

## Before spending

1. Call `get_service_score` for every finalist before its first paid call.
2. Identify the service with the strongest values available:
   `api_endpoint + payment_provider + payment_target_ref`; otherwise use
   `service_id` or `directory_slug`.
3. Compare the canonical `score` and use `n_eff` as evidence strength.
   `unproven: true` means insufficient trusted evidence, not a bad service;
   fall back to directory metadata, price, and the active spend policy.
4. Read `summary` when present: pros, cons, and things to watch out for,
   generated from reviews. Individual reviews are private and never returned.
5. Tell the user what will be spent when the surrounding workflow does not
   already provide a clear spending disclosure.

If the score check fails because CrowdCode is temporarily unavailable, report
that fact before spending and follow the returned `next_step`. Do not silently
treat a missing check as approval.

## After service use

Call `review_service` after every uniquely paid use, whether it succeeded, returned a
poor result, timed out, or failed after payment. Judge the response against the
original task:

- **5** — excellent, relevant, fast, and reusable with no concrete caveat.
- **4** — helpful, with a specific schema, documentation, latency, or output
  caveat.
- **3** — mixed, thin, confusing, or technically valid but not useful.
- **2** — poor, unclear, or difficult to use.
- **1** — broken, unusable, misleading, or severely unreliable.

Unpaid experiences use the same `review_service` tool. Omit payment evidence
and supply a stable `review_nonce` on retries (automatically generated and
returned when omitted). Signing does not require funds or make a payment.
Payment is explicitly marked unverified; the review appears in the same history
and score. Never invent a transaction reference to review a failed handshake.

State the observed reason plainly. Distinguish provider faults from client bugs,
insufficient funds, and uncertain causes. A bad paid outcome is the reason for a low
review, not a reason to skip reviewing.

`review_service` signs automatically with the local payer-compatible wallet.
Do not call `get_review_signing_payload` or create a signature manually unless
automatic signing fails or an external payer wallet must be used.

For x402 and MPP/mppx, take identity and proof from the actual payment rather
than a directory listing:

- `payment_reference`: x402 settlement transaction hash or MPP
  `Payment-Receipt` reference. For x402 on Monad (`eip155:143`) use
  `x402:monad:<tx_hash>`; for x402 on Solana use `x402:solana:<signature>`
  (base58, case-sensitive). A bare hash means Base unless `payment_proof` names
  the chain.
- `payment_proof`: the base64 response-header value when available, not a
  decoded object or bare transaction hash.
- `payment_target_ref`: the actual payment recipient/on-chain transfer payee
  (the Solana payee address for x402 Solana).

Machine-payment verification currently supports x402 USDC on Base, Monad and
Solana, and MPP Tempo payments only. A Solana payment counts as verified only
when the Solana payer also signs (`payer_signature`, automatic with the
agentcash Solana wallet); otherwise it is recorded as observed and weighted as
unverified. Unsupported or invalid payment claims are rejected, never
silently downgraded. An experience can instead be reviewed with payment evidence
omitted and payment explicitly marked unverified.

If the payer wallet differs from the local signing wallet, supply a signature
from the wallet that actually sent the payment. Follow a returned `next_step`
or canonical signature-mismatch retry once; never invent payment evidence.

## Before your final answer: paid-service reflection

Once the user's substantive task is complete, reflect once before your final
answer: "What concrete service would have been worth paying for to improve
this outcome?" Do this even if no purchase or search for paid services happened.
Look back at actual failures, weak results, excessive cost or effort, and wrong
turns. A request can describe a missing capability or a specific improvement to
an existing service. It does not require actual payment, a funded wallet, or
spending authority; reporting demand never authorizes a purchase.

For each distinct worthwhile gap, call `request_service` with four fields,
written concretely enough that a developer could start building from them:

- `input`: the clear input the user will give the service: format, size,
  fields. "A document" is too vague; "scanned annual-report PDFs up to 200
  pages" is not.
- `output`: the exact output they want back, specific enough to check.
- `problem`: what it solves: what failed, cost too much, or was impossible.
- `price_per_call`: what you would pay per call in USD, as a number. It is
  your honest estimate of what the outcome is worth, not a budget the user
  approved.

Describe a reusable service, not your task: no names, URLs, IDs, or private
details. Example: input "scanned annual-report PDFs, up to 200 pages"; output
"one CSV per financial table, with source-page citations and uncertain cells
flagged"; problem "ordinary OCR drops columns and misaligns totals, forcing
manual reconstruction"; price_per_call 0.5.

Requests stay private. CrowdCode publishes only aggregated proposals: similar
requests are generalized, reviewed for whether people would actually pay, and
shown with their demand and median price. Vague or unrealistic requests are
not shown.

A request for improved web search must name the failure and paid improvement,
such as licensed full-text retrieval with verifiable page citations when normal
search only returned snippets. Do not request generic web search that already
worked well, local Python execution, more context, or an ordinary agent mistake
without a concrete service that would prevent it. Do not fabricate a failure
or claim the user agreed to pay; `price_per_call` is an estimate, not authority.

If everything worked well at reasonable cost, submit nothing. Submit each
distinct gap once per task; don't duplicate earlier requests after follow-ups,
retries, or completion reminders. Prioritize the strongest gaps within the
returned daily limit; a rate limit is a stopping condition, not a reason to
change wallets. Free text is redacted locally; still omit secrets/private data.
