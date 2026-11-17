-- The nightly demand miner (SRI-115 ticket 11, contract C8 in
-- design-docs/RELIABLE_SERVICES_BUILD.md in the parent repo).
--
-- CrowdCode's reconciler reads the day's hosted run summaries (never
-- transcripts) for owners who have not opted out, scrubs secrets, and posts
-- them in batches to POST /api/internal/demand-miner/batch. One model call per
-- batch proposes services in the C6 shape; each one that passes the same
-- validation and screen as request_service is stored here as a `miner`
-- request, with no requester wallet and only salted run references. The
-- proposals pipeline then reads them like any other request.
--
-- Additive and idempotent. Apply BEFORE deploying the backend that writes it.

alter table service_requests
  -- Who filed it: an agent through request_service, or the miner.
  add column if not exists source text not null default 'agent',
  -- How many run summaries the miner saw this need in.
  add column if not exists miner_run_count integer,
  -- HMACs of those runs' ids, keyed by a secret only the reconciler holds.
  -- They let an operator with that secret find the runs again; nothing else.
  add column if not exists miner_run_refs text[];

alter table service_requests drop constraint if exists service_requests_source_check;
alter table service_requests
  add constraint service_requests_source_check check (source in ('agent', 'miner'));

-- A miner request's fields come from the miner's model, not an agent or the
-- legacy mapping.
alter table service_requests drop constraint if exists service_requests_fields_source_check;
alter table service_requests
  add constraint service_requests_fields_source_check
  check (fields_source is null or fields_source in ('agent', 'inferred', 'miner'));

create index if not exists service_requests_source_created_idx
  on service_requests (source, created_at desc);

-- One row per batch the miner accepted, so a batch the reconciler retries
-- (a timeout after the backend had already stored it) is not mined twice.
create table if not exists demand_miner_batches (
  batch_key text primary key,
  night date not null,
  runs integer not null,
  result jsonb not null,
  created_at timestamptz not null default now()
);

alter table demand_miner_batches enable row level security;
