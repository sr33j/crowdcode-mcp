-- docs/SCORING.md v4 (SRI-118, owner decision 2026-11-17).
--
-- The published score becomes min(weighted average, 5 × P(quality > 3)) under
-- a Beta(1, 1) prior, signed failed purchases count as 1-star observations, and
-- a paid review verified on chain outside the hosted gateway carries weight
-- (weight_basis = 'onchain', one vote per paying wallet per service).
--
-- Additive and backwards compatible: apply before deploying the v4 backend
-- and cron. Then run scripts/backfill_onchain_votes.py (dry run, then
-- --execute) and crowdcode.cron.run_consistency_sweep(utc_now()), which
-- republishes every score; changing the default alone moves nothing.

-- v3.1's shrunk mean, still published and read by reliability-v2's quality.
alter table services
  add column if not exists score_mean double precision,
  add column if not exists n_eff_onchain double precision not null default 0,
  add column if not exists n_eff_failures double precision not null default 0,
  -- P(θ > ½), the weighted average (null: unrated) and the Beta behind
  -- them, so endpoints can be pooled into a provider exactly.
  add column if not exists p_above_3 double precision,
  add column if not exists avg_rating_weighted double precision,
  add column if not exists score_alpha double precision,
  add column if not exists score_beta double precision;

-- No evidence: P(>3) = ½, so 2.5 — a sort key; the service shows "unrated".
alter table services alter column score set default 2.5;

alter table reviews drop constraint if exists reviews_weight_basis_check;
alter table reviews
  add constraint reviews_weight_basis_check
  check (weight_basis in ('legacy', 'hosted', 'onchain', 'none'));
