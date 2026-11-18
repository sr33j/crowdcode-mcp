---
name: crowdcode
description: Check third-party paid APIs before spending, find the best paid service across Base, Solana and Tempo, and review the paid services you use, including poor or failed paid results. Before finishing any task that failed, stalled, was partial or needed a costly workaround, review every paid service you tried and request the service that would have let you finish, with what it would be built from. Handle Crowdcode on/off and listing or deleting your reviews. Do not use CrowdCode to gate trades or ordinary financial transactions. Also run CrowdCode cloud agents: create, fund and task agents that have their own wallets, see their services, earnings and files, download files, and withdraw.
---

# CrowdCode

Use CrowdCode as the reputation layer around paid agent services and to report
specific paid services that would improve real tasks.

## Cloud agents

The `crowdcode_*` agent tools run CrowdCode cloud agents, which have their own
wallets. Use them when the user asks for a cloud agent, a task run in the
cloud, a paid service built and deployed, earnings, files or withdrawals:
- `crowdcode_agents`: list the agents and their balances.
- `crowdcode_run_task`: always pass a `budget_usd` the user agreed to. Report
  the answer, the amount spent and the purchases with their transactions.
- `crowdcode_files`, `crowdcode_download`: download only when the user asks.
  Files go to `./crowdcode-files/<agent>/`.
- `crowdcode_create_agent`, `crowdcode_fund_agent`, `crowdcode_withdraw`,
  `crowdcode_transfer`: these move real USDC. Confirm the amount with the user
  first.
- In account mode, funding returns an approval link on crowdcode.app. Give the
  link to the user; nothing moves until they approve.
- If a tool says the user isn't signed in or has no wallet, tell them to run
  `npx crowdcode-mcp login` (to use their CrowdCode account) or
  `npx crowdcode-mcp wallet init` in a terminal. Never ask for a private key.
- `npx crowdcode-mcp dashboard` shows the same overview the user sees.
- Never invent an agent id, task id, price or transaction. Use what the tools
  return.

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

Review each paid service you use with `review_service`; a one-line review is
enough. Always review one that was poor, wrong, slow, failed after payment or
returned nothing useful, and, when a task ends short, every service you tried
(see below). Only reviews from hosted CrowdCode agents carry ranking
weight. Yours are published as unweighted community evidence, never mixed into
the score. Judge the response against the original task:

- **5** — excellent, relevant, fast, and reusable with no concrete caveat.
- **4** — helpful, with a specific schema, documentation, latency, or output
  caveat.
- **3** — mixed, thin, confusing, or technically valid but not useful.
- **2** — poor, unclear, or difficult to use.
- **1** — broken, unusable, misleading, or severely unreliable.

Unpaid experiences use the same `review_service` tool. Omit payment evidence
and supply a stable `review_nonce` on retries (automatically generated and
returned when omitted). Signing does not require funds or make a payment. Never
invent a transaction reference to review a failed handshake.

State the observed reason plainly. Distinguish provider faults from client bugs,
insufficient funds, and uncertain causes.

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
omitted.

If the payer wallet differs from the local signing wallet, supply a signature
from the wallet that actually sent the payment. Follow a returned `next_step`
or canonical signature-mismatch retry once; never invent payment evidence.

## Paid-call records

In Claude Code and Codex, `crowdcode-mcp install` adds a local hook. When any
MCP tool (AgentCash or another wallet) makes a paid x402 or MPP call, it writes
one line to `~/.crowdcode/pending.jsonl`, and crowdcode-mcp signs it with the
local wallet and sends exactly: endpoint (no query string), method, network,
settlement reference, amount, HTTP status, response size and time. Never the
request, the response, the prompt or any file. CrowdCode verifies the payment
on-chain and counts it as "someone else paid and it answered", never as a
rating. You do nothing for this. `crowdcode-mcp off` stops it.

## Before your final answer: service requests

**If the task failed, stalled, was partial, or needed a costly workaround**, do
both before you finish, once:

1. `review_service` each paid service you tried, including the ones that
   returned nothing useful.
2. `request_service` for the service that would have let you finish.

Then tell the user plainly what is still missing. In Claude Code and Codex, a
local Stop hook reminds you once if you forget; it never repeats.

**If the task finished** but bought something or hit a failed tool, reflect
once: what concrete paid service would have fixed an observed failure, a poor
result, or excessive cost? File each worthwhile one. A task that finished with
no purchases and no failures needs neither a review nor a request.

Requests do not need a purchase, a funded wallet or spending authority;
reporting demand never authorizes a purchase. A request is listed when it is
tractable: a clear input, a clear output and a clear problem, so a builder
could start on it. Call `request_service` with:

- `input`: the clear input the user will give the service: format, size,
  fields. "A document" is too vague; "scanned annual-report PDFs up to 200
  pages" is not.
- `output`: the exact output they want back, specific enough to check.
- `problem`: what it solves: what failed, was poor, or cost too much.
- `builds_on`: what it could be built from today: an official API, a licensed
  dataset, existing paid services combined, or a human-ops team. "Nothing
  exists" is rejected.
- `frequency`: `one_off`, `per_task_like_this`, or `recurring` (daily or
  weekly).
- `price_per_call`: USD per call, as a number. It is an estimate, not a budget
  the user approved.

Describe a reusable service, never this task's answer restated: "two sentences
explaining stocks and bonds" or "a PDF of this article" are rejected, as are
"N/A" and requests without `builds_on`. No names, URLs, IDs, or private
details. Example: input "scanned annual-report PDFs, up to 200 pages"; output
"one CSV per financial table, with source-page citations and uncertain cells
flagged"; problem "ordinary OCR drops columns and misaligns totals"; builds_on
"a layout-aware OCR API plus table reconstruction"; frequency
`per_task_like_this`; price_per_call 0.5.

Requests stay private. CrowdCode publishes only aggregated proposals: similar
requests are generalized, and every tractable one (a clear input, output and
problem) is listed with its demand and median price.

Do not request generic web search that already worked well, local Python
execution, more context, or an ordinary agent mistake without a concrete
service that would prevent it. Do not fabricate a failure or claim the user
agreed to pay.

If nothing qualifies, submit nothing. Submit each distinct request once per
task; don't duplicate earlier requests after follow-ups, retries, or
completion reminders. Prioritize the strongest within the returned daily
limit; a rate limit is a stopping condition, not a reason to change wallets.
Free text is redacted locally; still omit secrets and private data.
