-- SRI-115 Phase 2 (contracts C10 and C11): delivery evidence under the judge's
-- v2 rubric. Idempotent; apply after schema.sql and services-index.sql:
--
--   psql "$DATABASE_URL" -f supabase/judge-v2-evidence.sql
--
-- 1. C11's back-fill. Verdicts made under opencrowd.judge.v1 could say
--    `partial`; v2 never does. The cron re-reads each old `partial` once
--    (crowdcode.rubric.reread_partial) and writes the result here, beside the
--    signed attestation it never alters. reliability-v2 reads
--    coalesce(delivery_reread, gateway_attestation->'review'->>'delivery').
alter table reviews add column if not exists delivery_reread text;
alter table reviews add column if not exists delivery_reread_note text;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'reviews_delivery_reread_check') then
    alter table reviews add constraint reviews_delivery_reread_check
      check (delivery_reread is null or delivery_reread in ('delivered', 'not_delivered'));
  end if;
end $$;
create index if not exists reviews_partial_unread_idx on reviews (id)
  where delivery_reread is null and receipt_key_id is not null
    and gateway_attestation->'review'->>'delivery' = 'partial';

-- 2. C10's beta+1 for a paid call the seller broke before settlement, sent by
--    the hosted gateway (POST /api/purchase-failures, signed with its receipt
--    key). Stored as kind = 'purchase', ok = false, review_id null, with
--    `detail` one of seller_error, seller_rejected, no_receipt,
--    authorization_expired, and the gateway's attempt id in `attempt_ref`.
alter table index_observations add column if not exists attempt_ref text;
alter table index_observations add column if not exists traffic text;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'index_observations_traffic_check') then
    alter table index_observations add constraint index_observations_traffic_check
      check (traffic is null or traffic in ('test', 'owner'));
  end if;
end $$;
create unique index if not exists index_observations_attempt_unique
  on index_observations (attempt_ref) where kind = 'purchase' and attempt_ref is not null;
