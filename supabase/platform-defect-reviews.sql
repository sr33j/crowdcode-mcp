-- A review of a failure CrowdCode caused is not evidence about the provider.
--
-- Three of our own bugs made hosted agents file 1-star reviews against
-- providers that were never given the chance to succeed or fail:
--
--   * crowdcode#157 and #160: the gateway misread valid x402/MPP 402 offers
--     and refused to pay, so the call never ran ("the provider returned
--     invalid payment requirements", "unsupported by the gateway", and a
--     listing refused for a reputation floor nobody had classified it under).
--   * crowdcode#156: an agent that held USDC on one network was not allowed
--     to spend it on the network the seller asked for, so a Tempo-only
--     service was reviewed 1 star for a wallet balance of our making.
--
-- Those ratings are kept verbatim. The mistake was ours and deleting the
-- record of it would hide it; instead `weight_exclusion = 'platform_defect'`
-- removes the row from every published count, histogram, average and
-- generated summary, and `exclusion_note` says which defect and why.
--
-- Apply this, deploy the backend and cron, then run
-- `scripts/exclude_platform_defect_reviews.py --apply`, which marks the
-- affected rows and clears `services.last_summarized_at` so the next cron
-- regenerates their summaries. Safe to rerun.

-- 'platform_defect' joins the reasons a review carries no ranking weight.
-- Postgres named the original constraint when hosted-only-reviews.sql added
-- the column, so widening it means replacing it.
alter table reviews
  drop constraint if exists reviews_weight_exclusion_check;
alter table reviews
  add constraint reviews_weight_exclusion_check
  check (weight_exclusion is null or weight_exclusion in
    ('community', 'self_dealing', 'dust_payment', 'unsigned_placeholder',
     'platform_defect'));

-- Why this row was excluded, in an operator's words: the defect it belongs to
-- and what the agent was actually blocked by. Never published; it exists so
-- the exclusion can be audited and, if it was wrong, reversed.
alter table reviews
  add column if not exists exclusion_note text;
