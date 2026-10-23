-- Keep the pre-cutover scores (docs/SCORING.md v3.1, §3.7).
--
-- supabase/hosted-only-reviews.sql narrowed what carries ranking weight to
-- hosted, judged purchases, and recomputing under that rule sent every score
-- in the directory back to the 3.0 prior: nothing written before the cutover
-- has a gateway receipt, because receipts did not exist yet. Months of real
-- paid calls stopped counting overnight.
--
-- This grandfathers that corpus. Reviews that already existed keep the weight
-- they earned under v2 — wallet trust, the on-chain proof multiplier, the
-- wallet/service/UTC-day bucket — and decay on the same 180-day half-life they
-- always did. Everything written from the cutover on is weighed by v3's rules
-- and needs a receipt and a judge verdict.
--
-- Three things stop this being a reopened hole:
--   * weight_basis is assigned here, once. The write path can only ever
--     produce 'hosted' or 'none', so no new review can become legacy.
--   * The wallet trust behind legacy rows is frozen: after this migration no
--     code path writes wallet_users.raw_trust or is_seed again, so a wallet
--     cannot earn, lose, or be granted trust, and the snapshot can only ever
--     weigh the finite set of reviews that already cited it.
--   * The legacy corpus is finite and decaying. It stands today and fades on
--     its original schedule as judged evidence accumulates underneath it.
--
-- Apply this, deploy the backend and cron, then run
-- `crowdcode.cron.run_consistency_sweep(utc_now())` to restore stored scores.

-- Which era's rules weigh a row.
alter table reviews
  add column if not exists weight_basis text not null default 'none'
    check (weight_basis in ('legacy', 'hosted', 'none'));

-- Published scores now carry their two components separately: the eras are on
-- different scales (v2 priced proof up to 2x; a hosted owner vote is exactly
-- 1), so a single n_eff would not be readable.
alter table services
  add column if not exists n_eff_legacy double precision not null default 0,
  add column if not exists n_eff_hosted double precision not null default 0;

-- The cutover: when crowdcode-scoring-v3 went live in production. Everything
-- stored before it was written under v2's rules and keeps them. Idempotent —
-- rerunning cannot promote a newer review, because the timestamp is fixed and
-- rows already marked 'hosted' or 'none' after it are left alone.
update reviews
set weight_basis = 'legacy'
where created_at < timestamptz '2026-10-22 15:30:00+00'
  and weight_basis = 'none'
  -- An unsigned placeholder row was never publishable and never weighed.
  and weight_exclusion is distinct from 'unsigned_placeholder';

-- Reviews written since the cutover: weighed by the hosted rules, or not.
update reviews
set weight_basis = case
      when verified_source = 'hosted_receipt' and weight_exclusion is null then 'hosted'
      else 'none'
    end
where created_at >= timestamptz '2026-10-22 15:30:00+00';

-- Restore the wallet trust snapshot as it stood at the cutover.
--
-- hosted-only-reviews.sql demoted every seed on the way to owner trust with
-- `set is_seed = false, raw_trust = 0`, and v2 pinned seeds at exactly 1.0, so
-- the values are recoverable. The seed list is recovered two ways and then
-- materialised as a frozen table — never re-derived from a live registry,
-- because registering a hosted agent must still grant nothing:
--
--   1. the hosted mainnet agent wallets the retired `opencrowd_seed_wallets`
--      view selected, supplied by the operator in `legacy_seed_wallets`;
--   2. the explicit `CROWDCODE_SEED_WALLETS` operators, recovered from the
--      demotion's own signature. A demoted seed sits at raw_trust exactly 0;
--      a wallet that has authored reviews and is *not* a seed cannot, because
--      every rating of 1 or 5 against a moved consensus shifts it off zero.
--      Restricting the recovery to wallets with a pre-cutover review keeps it
--      safe to re-run: a wallet that appears later can never be promoted.
--
-- Verified against the scores captured immediately before the cutover: all 15
-- reproduce to within 0.003, and every residual is negative — time decay over
-- the hours between, on the same 180-day half-life as always.
create table if not exists legacy_seed_wallets (
  wallet_address text primary key,
  frozen_at timestamptz not null default now()
);
alter table legacy_seed_wallets enable row level security;

-- Operators replaying this on their own database should insert their hosted
-- registry snapshot here first; this statement recovers the rest.
insert into legacy_seed_wallets (wallet_address)
select u.wallet_address
from wallet_users u
where u.raw_trust = 0
  and not u.is_seed
  and u.wallet_address ~ '^0x[0-9a-f]{40}$'
  and exists (
    select 1 from reviews r
    where lower(r.reviewer_wallet) = u.wallet_address
      and r.created_at < timestamptz '2026-10-22 15:30:00+00'
  )
on conflict do nothing;

update wallet_users u
set is_seed = true, raw_trust = 1.0, trust_updated_at = now()
from legacy_seed_wallets s
where lower(u.wallet_address) = s.wallet_address;

-- The legacy bucket lookup is by wallet and day within a service.
create index if not exists reviews_legacy_bucket_idx
  on reviews (service_id, reviewer_wallet, created_at)
  where weight_basis = 'legacy';
