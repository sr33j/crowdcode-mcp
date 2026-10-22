-- Hosted-only weighted reviews (docs/SCORING.md v3, crowdcode-scoring-v3).
--
-- Only a review backed by a hosted OpenCrowd gateway receipt and a
-- platform-judge score carries ranking weight. Everything else — CLI wallets,
-- crowdcode-mcp users, every row written before this migration — stays stored
-- and readable as community evidence with weight exactly zero.
--
-- Apply this, deploy the backend and cron, then run
-- `crowdcode.cron.run_consistency_sweep(utc_now())`: the sweep is what
-- actually recomputes stored scores under the new rules. Expect most scores to
-- return to the 3.0 prior with n_eff 0 ("unproven") until hosted owners who
-- have earned trust review those services.

-- What verified the review, and why it does or does not carry weight.
-- weight_exclusion null means weighted; 'community' is the default for
-- everything that is not a verified hosted purchase.
alter table reviews
  add column if not exists verified_source text not null default 'community'
    check (verified_source in ('community', 'hosted_receipt')),
  add column if not exists weight_exclusion text default 'community'
    check (weight_exclusion is null or weight_exclusion in
      ('community', 'self_dealing', 'dust_payment', 'unsigned_placeholder')),
  -- The reviewing owner and the payee's owner, from the verified attestation.
  -- Equal values mean the owner reviewed a service it is itself paid by.
  add column if not exists owner_ref text,
  add column if not exists payee_owner_ref text,
  add column if not exists receipt_key_id text,
  -- The verified receipt and attestation, kept so any score can be re-derived
  -- (and re-verified) from the evidence it rests on.
  add column if not exists gateway_attestation jsonb;

-- Existing rows keep their history and lose their weight. Unsigned placeholder
-- rows (the legacy stripe/manual admission path) are additionally never shown.
update reviews set weight_exclusion = 'unsigned_placeholder'
where not signature_verified and weight_exclusion = 'community';

-- One weighted vote per owner per service; this is the index that lookup uses.
create index if not exists reviews_owner_service_idx
  on reviews (owner_ref, service_id)
  where owner_ref is not null and weight_exclusion is null;
-- An attestation may be submitted once. The receipt it names is the thing being
-- claimed, so the receipt hash is the uniqueness key — a replayed attestation
-- under a second review nonce cannot buy a second vote.
create unique index if not exists reviews_receipt_sha256_idx
  on reviews ((gateway_attestation -> 'review' ->> 'receipt_sha256'))
  where gateway_attestation is not null;

-- Owner trust (docs/SCORING.md §3.3). Trust is per owner, not per wallet: a
-- wallet costs nothing to create and a hosted owner runs many of them.
-- Seeds come only from CROWDCODE_SEED_OWNERS; the hosted wallet registry no
-- longer grants trust to anybody.
create table if not exists review_owners (
  owner_ref text primary key,
  created_at timestamptz not null default now(),
  is_seed boolean not null default false,
  raw_trust double precision not null default 0,
  trust_updated_at timestamptz,
  slashed_at timestamptz
);
-- Same default-deny posture as every other table here.
alter table review_owners enable row level security;

-- The hosted wallet bridge no longer confers trust. Dropping the view also
-- removes the only path by which registering a hosted agent could promote a
-- wallet, and leaves the backend with no dependency on the hosted schema.
drop view if exists public.opencrowd_seed_wallets;
update wallet_users set is_seed = false, raw_trust = 0 where is_seed;
