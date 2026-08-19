create table if not exists services (
  id text primary key,
  name text not null,
  directory_slug text unique,
  stripe_payee_ref text,
  canonical_origin text,
  canonical_endpoint text,
  payment_provider text,
  payment_target_ref text,
  metadata jsonb not null default '{}'::jsonb,
  created_from_review boolean not null default false,
  created_at timestamptz not null default now()
);

alter table services
  add column if not exists canonical_origin text,
  add column if not exists canonical_endpoint text,
  add column if not exists payment_provider text,
  add column if not exists payment_target_ref text,
  add column if not exists metadata jsonb not null default '{}'::jsonb,
  add column if not exists created_from_review boolean not null default false;

create table if not exists reviews (
  id bigserial primary key,
  service_id text not null references services(id) on delete cascade,
  rating int not null check (rating between 1 and 5),
  reason text not null,
  task_context text,
  payment_reference text not null unique,
  reviewer_id text,
  payment_provider text,
  payment_target_ref text,
  payment_proof jsonb not null default '{}'::jsonb,
  payment_verified boolean not null default false,
  payment_verified_at timestamptz,
  reviewer_wallet text,
  review_signature text,
  signature_scheme text,
  signature_verified boolean not null default false,
  created_at timestamptz not null default now()
);

alter table reviews
  add column if not exists payment_provider text,
  add column if not exists payment_target_ref text,
  add column if not exists payment_proof jsonb not null default '{}'::jsonb,
  add column if not exists payment_verified boolean not null default false,
  add column if not exists payment_verified_at timestamptz,
  add column if not exists reviewer_wallet text,
  add column if not exists review_signature text,
  add column if not exists signature_scheme text,
  add column if not exists signature_verified boolean not null default false;

create index if not exists reviews_service_id_created_at_idx
  on reviews (service_id, created_at desc);

create table if not exists service_identifiers (
  id bigserial primary key,
  service_id text not null references services(id) on delete cascade,
  identifier_type text not null,
  identifier_value text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (identifier_type, identifier_value)
);

create index if not exists service_identifiers_service_id_idx
  on service_identifiers (service_id);

create table if not exists service_requests (
  id bigserial primary key,
  service_description text not null,
  task_context text,
  directory_match text not null default 'missing'
    check (directory_match in ('missing', 'undiscovered', 'exists')),
  created_at timestamptz not null default now()
);

create index if not exists service_requests_directory_match_created_at_idx
  on service_requests (directory_match, created_at desc);

-- Server-side redaction enforcement: rows carry redacted free text only.
-- redacted_at marks rows processed by ingest enforcement or the backfill
-- (scripts/backfill_redaction.py); null means pre-enforcement raw text that
-- the egress backstop still re-redacts on the way out.
alter table reviews
  add column if not exists redacted_at timestamptz;
alter table service_requests
  add column if not exists redacted_at timestamptz;

-- Wallet-keyed service-request rate limiting: requests carry the requester's
-- wallet identity (salted hash + normalized address). Reviews are not
-- throttled; scoring caps their influence by wallet/service/UTC-day below.
alter table service_requests
  add column if not exists requester_id text,
  add column if not exists requester_wallet text;
create index if not exists service_requests_requester_id_created_at_idx
  on service_requests (requester_id, created_at desc);
create index if not exists reviews_reviewer_service_created_idx
  on reviews (reviewer_id, service_id, created_at desc);

-- Scoring & reputation (docs/SCORING.md v1): one row per reviewing wallet
-- holding its raw trust; seeds are pinned at 1.0 via CROWDCODE_SEED_WALLETS.
-- Scores are stored on services and refreshed on the review write path plus
-- the nightly consistency sweep; trust is replayed nightly from daily buckets.
--
-- Named wallet_users, not users: this database already carries an unrelated
-- public.users table (Privy auth) belonging to another app.
create table if not exists wallet_users (
  user_id bigserial primary key,
  wallet_address text not null unique,
  created_at timestamptz not null default now(),
  is_seed boolean not null default false,
  raw_trust double precision not null default 0,
  trust_updated_at timestamptz,
  slashed_at timestamptz
);

alter table reviews
  add column if not exists user_id bigint references wallet_users(user_id),
  add column if not exists amount numeric;

alter table services
  add column if not exists resource_type text not null default 'api',
  add column if not exists score double precision not null default 3.0,
  add column if not exists n_eff double precision not null default 0,
  add column if not exists score_updated_at timestamptz,
  add column if not exists review_summary jsonb,
  add column if not exists last_summarized_at timestamptz;

-- Cross-restart cache for cron-generated payloads (e.g. project_ideas).
create table if not exists app_cache (
  key text primary key,
  payload jsonb not null,
  generated_at timestamptz not null default now()
);

create index if not exists reviews_wallet_created_idx
  on reviews (reviewer_wallet, created_at);

-- Daily scoring buckets filter by service and wallet, then order/group by
-- creation time. Keep this composite B-tree aligned with that access path.
create index if not exists reviews_service_wallet_created_idx
  on reviews (service_id, reviewer_wallet, created_at);

-- Payment verification levels (docs/SCORING.md §3.4): what the payment check
-- actually proved. payment_verified stays as the derived boolean
-- (level in onchain_verified/response_attested) for compatibility.
--   unverified        no wallet signature verified (legacy placeholder rows)
--   signature_only    review signature verified; payment not verified on-chain
--   onchain_verified  matching ERC-20 transfer found on-chain
--   response_attested reserved for signed receipts binding payment to a
--                     specific request/response (not yet issued)
-- payment_reference_canonical is the reference with any x402:base:/mppx:tempo:
-- prefix stripped; deduplication keys on it so the same settlement tx cannot
-- yield two reviews under different spellings.
alter table reviews
  add column if not exists payment_verification_level text not null default 'unverified'
    check (payment_verification_level in
      ('unverified', 'signature_only', 'onchain_verified', 'response_attested')),
  add column if not exists payment_verification_metadata jsonb not null default '{}'::jsonb,
  add column if not exists payment_reference_canonical text;

-- Backfill legacy rows (payment_reference_canonical is null exactly for rows
-- that predate this migration; new inserts always set it).
update reviews
set payment_verification_level = case
  when payment_verified then 'onchain_verified'
  when signature_verified then 'signature_only'
  else 'unverified'
end
where payment_reference_canonical is null;

-- Canonical backfill: the row whose reference already IS the raw form wins the
-- canonical value; any legacy duplicate that only differs by prefix keeps its
-- verbatim reference so the unique index below cannot fail on old data.
with ranked as (
  select
    id,
    payment_reference,
    regexp_replace(payment_reference, '^(x402:base:|mppx:tempo:)', '') as canon,
    row_number() over (
      partition by regexp_replace(payment_reference, '^(x402:base:|mppx:tempo:)', '')
      order by (payment_reference = regexp_replace(payment_reference, '^(x402:base:|mppx:tempo:)', '')) desc,
               created_at, id
    ) as rn
  from reviews
  where payment_reference_canonical is null
)
update reviews r
set payment_reference_canonical =
  case when ranked.rn = 1 then ranked.canon else ranked.payment_reference end
from ranked
where ranked.id = r.id;

create unique index if not exists reviews_payment_reference_canonical_idx
  on reviews (payment_reference_canonical);

-- Ethereum transaction hashes are case-insensitive. Keep the exact submitted
-- reference in the signed payload, but make concurrent deduplication
-- case-insensitive for canonical EVM hashes.
create unique index if not exists reviews_payment_reference_evm_hash_idx
  on reviews (lower(payment_reference_canonical))
  where payment_reference_canonical ~* '^0x[0-9a-f]{64}$';

-- The board (BOARD_DESIGN.md v3): one append-only log of wallet-signed posts.
-- A row with parent_post_id null is a top-level request; otherwise a comment.
-- id is content-addressed (post_ + sha256(canonical signed payload)[:20]) so
-- client retries are idempotent and references are verifiable. bounty_amount
-- is a signed, NON-BINDING stated-demand figure in USDC — never escrowed,
-- never enforced, never paid out; all rankings and demand totals are derived
-- projections computed on read.
create table if not exists board_posts (
  id text primary key,
  parent_post_id text references board_posts(id) on delete cascade,
  wallet text not null,
  user_id bigint references wallet_users(user_id),
  text text not null,
  bounty_amount numeric check (bounty_amount >= 0),
  payload_timestamp timestamptz not null,
  nonce text not null,
  signature text not null,
  signature_scheme text not null default 'eip191',
  redacted_at timestamptz,
  created_at timestamptz not null default now(),
  text_tsv tsvector generated always as (to_tsvector('english', text)) stored
);

create index if not exists board_posts_parent_created_idx
  on board_posts (parent_post_id, created_at);
create index if not exists board_posts_wallet_created_idx
  on board_posts (wallet, created_at desc);
create index if not exists board_posts_tsv_idx
  on board_posts using gin (text_tsv);

-- Study instrumentation (BOARD_DESIGN.md §6.6): every board tool call, for
-- the searches->posts funnel, duplicate rate, and demand->supply conversion.
create table if not exists board_events (
  id bigserial primary key,
  tool text not null,
  wallet text,
  post_id text,
  query text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists board_events_tool_created_idx
  on board_events (tool, created_at desc);

-- RLS is enabled with NO policies on purpose: that is default-deny for the
-- Supabase anon/authenticated API roles. The backend connects with a
-- privileged role that bypasses RLS and owns all reads/writes. Do not add
-- permissive policies here without a security review.
alter table services enable row level security;
alter table reviews enable row level security;
alter table service_identifiers enable row level security;
alter table service_requests enable row level security;
alter table wallet_users enable row level security;
alter table app_cache enable row level security;
alter table board_posts enable row level security;
alter table board_events enable row level security;
