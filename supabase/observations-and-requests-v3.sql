-- crowdcode-mcp 0.13.0: paid-call observations (C5) and service requests v2
-- fields (C6). docs: design-docs/RELIABLE_SERVICES_BUILD.md in the parent repo.
--
-- Additive and idempotent. Apply BEFORE deploying the 0.13.0 backend; the
-- 0.12 backend keeps working against it. index_observations is shared with
-- supabase/services-index.sql (W1, SRI-115): the create below uses the same
-- column names and types, and leaves the foreign key and the kind check to
-- that file, so either may run first.

-- --- C6: requests say what kind of need it is and what it would be built from
alter table service_requests
  add column if not exists request_kind text
    check (request_kind is null or request_kind in ('gap', 'leverage')),
  add column if not exists builds_on text,
  add column if not exists unlocks text,
  add column if not exists frequency text
    check (frequency is null or frequency in ('one_off', 'per_task_like_this', 'recurring')),
  -- A capability tree node id (C7). Mapped by the backend when the agent omits it.
  add column if not exists capability text,
  -- The model screen at ingest (crowdcode.request_screen): 'accept' or
  -- 'unchecked' (model unavailable). Rejected requests are never stored.
  add column if not exists screen_verdict text
    check (screen_verdict is null or screen_verdict in ('accept', 'unchecked')),
  add column if not exists screened_at timestamptz;

-- --- C5: a paid call seen by crowdcode-mcp, verified on-chain, no content
create table if not exists index_observations (
  id bigserial primary key,
  resource_id bigint,
  endpoint text not null,
  method text,
  at timestamptz not null default now(),
  kind text not null,
  ok boolean not null,
  http_status int,
  network text,
  payee text,
  quoted_amount_atomic numeric,
  error_code text
);

alter table index_observations
  add column if not exists observer_wallet text,
  add column if not exists payment_reference text,
  add column if not exists response_bytes bigint;

-- One observation per settlement, whoever reports it.
create unique index if not exists index_observations_observation_reference_uidx
  on index_observations (payment_reference) where kind = 'observation';
create index if not exists index_observations_observer_at_idx
  on index_observations (observer_wallet, at desc) where kind = 'observation';

alter table index_observations enable row level security;
