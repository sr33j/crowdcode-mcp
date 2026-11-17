# Codebase

This codebase is intentionally small. The goal is to prove the end-to-end CrowdCode loop before adding real Stripe verification, ranking sophistication, or a web board.

## `src/crowdcode/server.py`

Defines the FastMCP server and the active v1 tools:

- `request_service`
- `get_service_score`
- `review_service`

Tool logic is kept explicit instead of hidden behind abstractions because v1 has very little behavior.

## `src/crowdcode/db.py`

Contains the Postgres connection helper.

All database access uses `DATABASE_URL` from the environment. Connections use `dict_row` so tool responses can return dictionaries naturally.

## `src/crowdcode/payments.py`

Contains the v1 payment gate.

Current behavior:

- accepts non-empty payment references
- hashes the payment reference into a v1 `reviewer_id`
- relies on the database unique constraint to prevent reuse

Future Stripe verification should be implemented here first.

## `src/crowdcode/solana.py`

Solana primitives for x402 Solana verification: base58, ed25519 verification
(`cryptography`), `getTransaction` over JSON-RPC, and extraction of pinned-USDC
SPL transfers into the payee's token accounts. Payer linkage
(`payer_signature`) and admission rules live in `payments.py`
(docs/SCORING.md §3.4 rule 8).

## `src/crowdcode/advertised.py`

The payees a service advertises in its own 402 (x402 `accepts[].payTo`, MPP
`recipient`), and learning them: when a supplied machine payee is the only
reason resolution conflicts, the service's registered endpoint is probed and
every advertised payee joins its payee set (docs/shared-payment-wallets.md).
`scripts/fix_machine_payees.py` uses the same parser.

## `src/crowdcode/agent_listings.py`

Names and descriptions for services CrowdCode agents sell at
`https://crowdcode.app/s/<slug>/...`. They all share one host, so a service
first seen in a review that named none used to be listed as "crowdcode.app".
It is now registered under its slug, then given the name and description its
agent declared, read from the service's free manifest
(`https://crowdcode.app/s/<slug>`): right after its first review, at every
backend start, and in the nightly cron. The description is
`services.metadata.description`, returned as `description` by
`/api/services` and `/api/services/{id}`. `CROWDCODE_SERVICES_ORIGIN`
overrides the origin (default `https://crowdcode.app`).

## `src/crowdcode/listing_metadata.py`

Descriptions for listings that have none, read from the service itself: its
402, OpenAPI document, mpp.dev entry, `/llms.txt` or landing page, most
specific first. Stored as `metadata.description` (shown) and
`metadata.listing_text` (searched), with `description_source`. Runs after a
service's first review, at every start for listings never looked at, and in
the nightly cron; never replaces a description an agent or a person declared.
`scripts/fill_listing_descriptions.py` is the dry-run-first operator version.

## `src/crowdcode/semantic.py`

Matching by meaning for `find_service`: the pinned `minishlab/potion-base-8M`
static-embedding model, loaded in the background, and the text each listing
is matched on. `discovery.py` and `offerings.py` blend its similarity with
keyword overlap. `CROWDCODE_SEMANTIC_SEARCH=0` turns it off.

## `src/crowdcode/offerings.py`, `discovery.py`

`POST /api/services/find` (SRI-115 C13). `offerings.py` holds every offering
in the index (~48k) in memory: one document each (name, description, input
field names, CrowdCode's listing text, host and path words), a CSR word index,
the pinned embedding matrix, and each offering's tier, delivery Beta and
quality from W6's `value_ranking.estimate` (reliability-v2 when stored).
`SharedIndex` refreshes it in the background every 15 minutes, streaming rows;
an unchanged listing keeps its object and only its prices, payers and tier are
re-read, and a changed listing rebuilds the index after letting the old one
go (the live catalogs answer meanwhile). A query:

1. **matches** offerings by shared words and meaning (`discovery.match_strength`);
   CrowdCode's listed services are matched among themselves, as the directory was;
2. **finds what was asked for**: a named model's sellers (C9), else the capability
   leaf the strongest matches and the leaf labels agree on (C7), which gates out
   neighbours that share words ("video transcript" for "generate a video");
3. **ranks** the matching set by a seeded Thompson draw of delivery × quality ×
   fit per effective dollar (price, a transfer when short, plus ~$0.05 of the
   agent's own cost per call). Offerings with evidence all compete; only the
   two most relevant with none are drawn against them (`EXPLORE_SLOTS`). `bad`
   is left out.

Each result carries `tier`, `why`, `model`, `price_usd`, `networks` and, when
the draw lifted it above a better-known offering, `explore: true`; the
response returns its `seed`. With no match above `INDEX_FLOOR`, or before the
index has loaded, `discovery.find_live` runs the old federated search.

## `src/crowdcode/index.py`, `index_feeds.py`, `index_probe.py`

The services index (SRI-115 tickets 1 and 2; contract C1 in the parent
repository's `design-docs/RELIABLE_SERVICES_BUILD.md`). Three hourly cron
jobs:

- `index_sweep` reads the CDP x402 Bazaar and mpp.dev in full once a day, and
  our own records every hour: hosted services, gateway-attested purchases and
  reviewed endpoints. It folds them into `index_resources`, one row per
  `(method, route template)`. Network ids become CAIP-2, Base/Solana/Tempo
  only, with a `payable` flag. Parameter and identifier path segments collapse
  into templates. A resource is `tracked` if it has ≥ 3 Bazaar payers, a
  gateway purchase, is a hosted service, or matches a proposal. Every tracked
  resource links to one `services` row. Rows the index creates have
  `metadata.listing_source = 'index'` and `created_from_review = false`. They
  stay out of the directory and search until reviewed
  (`review_sql.directory_listed`).
- `index_purchases` records each gateway purchase as an observation.
- `index_probes` sends one unpaid request per tracked resource per day,
  spread across the hours. It uses the `CrowdCodeIndex/1` User-Agent, sends
  the seller's example body (never an empty one), follows no redirects, and
  calls public hosts only. Results go to `index_observations`. Price, payee
  and liveness changes go to `index_changes`. `index_resource_signals` is what
  the reliability rule reads.

`GET /api/index/health` reports tracked count, share probed in 24 h and feed
sweep ages. `GET /api/index/resource?url=&method=` returns one resource with
its probes and changes. Schema: `supabase/services-index.sql`.

## `src/crowdcode/reliability.py`

`reliability-v1` (SRI-115 ticket 6; contract C3): one state per tracked
service, `reliable`, `unverified` or `unreliable`, with plain-sentence reasons.
The thresholds are constants at the top of the module and nowhere else.
`evaluate(inputs)` is pure and reads only the published `inputs` document, so
a state can be recomputed from what the API returns. `gather` reads the
evidence (`index_resource_signals`, `index_changes`, `index_observations`,
attested reviews, `service_identifiers` payees, `service_blocks`);
`recompute` stores the answers in `service_reliability`, writing a row only
when it changed. The cron job `reliability` runs it hourly after the probes;
an attested review recomputes its service in the background. Routes:
`GET /api/services/{id}/reliability`, `GET /api/reliability?url=&method=`
(index identity first, then the registry's endpoint aliases) and
`GET /api/reliability?state=`. Operators block and unblock with
`scripts/block_service.py` (audited in `service_block_audit`). Schema:
`supabase/reliability.sql`.

## `src/crowdcode/capability_tree.py`, `value_ranking.py`, `media_models.py`

The capability tree (SRI-115; contracts C7, C10, C13 and C14 in the parent
repository's `design-docs/RELIABLE_SERVICES_BUILD.md`): when an agent should
look for a paid service, and which offering to use for which situation.

- **15 top nodes, written by hand** (`NODES`), each with `when_to_look` and
  `when_not_to_buy`; `free-first` is the policy node. The media nodes carry
  the standing hard rule (a requested video, image or audio is a real file
  from a model), which is also in every L0 rendering.
- **Offerings:** every payable listing in the index (C9), tracked or not.
- **Classification:** every offering and every published proposal gets one
  or two `(node, facet)` pairs from one batched model call per 50 items
  (`OPENAI_MODEL`, minimal reasoning; `CROWDCODE_TREE_MODEL` overrides). Only
  new or changed items are sent, costed against a daily cap
  (`CROWDCODE_TREE_DAILY_BUDGET_USD`, default $1; ledger: the
  `capability_tree_spend` row in `app_cache`).
- **Leaves:** the media nodes have the owner's fixed split (`FIXED_LEAVES`:
  video generate / edit / transcribe, image generate / edit / analyse, audio
  tts / transcribe / music), and their leaves list **models**
  (`media_models.py`: C9's `model` columns when the index has them, else the
  seller's route, description and `model` enum) with each model's sellers.
  Other nodes' leaves are proposed by a model call per node and split only
  where a different best-known offering wins (`<node>.all`, `<node>.other`,
  `covers`).
- **Tiers and ranking** (`value_ranking.py`): C10's Beta estimate (prior from
  Bazaar payers and calls or the mpp.dev catalog, plus judged purchases; or
  reliability-v2's own `alpha`/`beta`/`tier` when stored) gives `proven`,
  `popular`, `new` or `bad`. Picks are proven or popular offerings by expected
  delivery × quality / price; up to 3 exploration candidates come from a
  seeded Thompson sample of the same value (C13). Quality is neutral until
  C9's leaderboard priors exist.
- **Versions:** one row per build in `capability_tree_versions` (newest 14
  kept). W8's gate publishes through `publish_gated()`; versions stored in
  the Phase 1 format (`capability-tree-v1`) still render as they did.
- **Cron:** `run_capability_tree`, last in the hourly run.

`GET /api/tree[?version=current|candidate|<n>]` returns the version, the top
level and the L0 rendering (≤ 800 tokens, bounded as UTF-8 bytes / 3);
`GET /api/tree/{node_or_leaf}` returns one node or leaf as text (the whole
body ≤ 600 tokens). `&format=full` returns the structured tree, as
crowdcode.app/tree shows it. Schema: `supabase/capability-tree.sql`.

`reliability-v2` (SRI-115 contract C10; v1 was C3): per offering, a delivery
estimate `Beta(alpha, beta)` (prior from Bazaar payers and repeat ratio or the
mpp.dev catalog, plus judged purchases and seller failures before settlement),
a quality estimate (the v3.1 review score re-shrunk to the model's leaderboard
prior), one score (delivery mean × quality) and a tier (`bad`, `proven`,
`popular`, `new`), with plain-sentence reasons. The thresholds are constants at
the top of the module and nowhere else. `evaluate(inputs)` is pure and reads
only the published `inputs`; `prior_for` and `quality_for` are importable so
search (C13) and the tree compute nothing twice. `gather` reads the evidence
(`index_resource_signals`, `index_changes`, `index_observations`, attested
reviews and their C11 re-reads, `index_offering_models` and
`model_quality_priors`, `service_identifiers` payees, `service_blocks`), guarding
on columns W1 and W2 add. `recompute` stores answers in `service_reliability`,
writing a row only when it changed. The cron job `reliability` runs it hourly
after the probes, and an attested review recomputes its service in the
background. Routes: `GET /api/services/{id}/reliability`,
`GET /api/reliability?url=&method=` (index identity first, then the registry's
endpoint aliases) and `GET /api/reliability?state=` or `?tier=`. Operators
block and unblock with `scripts/block_service.py` (audited in
`service_block_audit`). Schema: `supabase/reliability.sql`.

### `src/crowdcode/index_models.py` (C9)

Which model an offering sells and how good the model is said to be. It
covers:
- the normalizer (`find_models`, `normalize_model`): one pattern table shared
  by routes, descriptions, seller catalogs and leaderboards;
- seller `/models` catalogs (`mpp_catalogs`, `STATIC_CATALOGS` for BlockRun),
  which become `index_offering_models` rows;
- the Artificial Analysis leaderboards, which become `model_quality_priors`
  rows (weekly; a board only scores models of its own modality).

`index.update_models` runs after each daily sweep.

## `src/crowdcode/search_eval.py`

recall@3 and MRR for the evaluation set in `tests/search_eval/cases.json`,
used offline by `tests/test_search_eval.py` (over `snapshot.json.gz`) and live
by `scripts/search_eval.py --live`.

## `src/crowdcode/review_sql.py`

The SQL predicates that decide which reviews are published and weighed, and
the `weight_exclusion` values. One definition, shared by `server.py` and
`cron.py` — the cron deliberately does not import the MCP app, and two copies
of "which reviews count" would drift.

## `src/crowdcode/scoring.py`

Contains simple scoring helpers:

- convert Postgres decimal averages to floats

The v1 score is just `avg(rating)`.

## `src/crowdcode/settings.py`

Reads environment variables:

- `DATABASE_URL`
- `MCP_TRANSPORT`
- `CROWDCODE_REVIEWER_SALT`

The module raises a clear error if `DATABASE_URL` is missing at runtime.

## `src/crowdcode/demand.py`

The request_service v2 contract (`validate_request_fields`: input, output,
problem, price_per_call), the copy guard every generated public text passes
(`SourceIndex`: no eight-word run shared with a private request or review),
and the willingness-to-pay distribution.

## `src/crowdcode/proposals.py`

Nightly: maps pre-v2 free-text requests into the v2 fields, groups every
request into generalized service proposals with a product review, and stores
the published payload in `app_cache` (`service_proposals`). Raw request and
review text is never published; only summaries and proposals are.

## `supabase/schema.sql`

Creates the minimal schema:

- `services`
- `reviews`
- `service_requests`

The most important v1 integrity rule is:

```sql
payment_reference text not null unique
```

## `supabase/seed.sql`

Adds three demo services:

- `svc_code_review`
- `svc_doc_writer`
- `svc_test_runner`

These IDs are stable and useful for demos.

## `scripts/`

Operator scripts, dry-run by default and `--apply` to write:
`fix_machine_payees.py` repairs a machine-payment service whose stored payee is
not an address, reading the real one from the service's own 402 (or marking the
service `payee_rotates` when it issues a fresh one per request);
`exclude_platform_defect_reviews.py` marks reviews that rate a failure
CrowdCode caused (docs/SCORING.md §3.6); `backfill_redaction.py` redacts
pre-enforcement free text.

## `hermes/crowdcode/SKILL.md`

Defines the agent policy:

- check scores before paid use
- submit a review after paid use
- capture unmet service demand with `request_service`

Request-service capture is intentionally limited to writes. There is no
`list_service_requests` tool.
