-- The reliability rule's results and the operator's block list (SRI-115
-- ticket 6; contract C3 in the parent repository's
-- design-docs/RELIABLE_SERVICES_BUILD.md). Idempotent: safe to rerun. Apply
-- after services-index.sql.
--
-- service_reliability   one row per tracked (or blocked) service: the state
--                       crowdcode.reliability computed, the rule version, the
--                       reasons, and every input the rule read. Rewritten only
--                       when the answer changes, so an hourly recompute of
--                       thousands of services leaves no dead rows behind.
-- service_blocks        services an operator has blocked, with the note that
--                       becomes a public reason. Blocked forces `unreliable`.
-- service_block_audit   every block and unblock, by whom and why. Kept.

create table if not exists service_reliability (
  service_id text primary key references services(id) on delete cascade,
  state text not null check (state in ('reliable', 'unverified', 'unreliable')),
  rule_version text not null,
  -- When the rule last produced this answer (state, reasons and inputs).
  computed_at timestamptz not null,
  reasons jsonb not null default '[]'::jsonb,
  inputs jsonb not null default '{}'::jsonb
);
create index if not exists service_reliability_state_idx on service_reliability (state);

create table if not exists service_blocks (
  service_id text primary key references services(id) on delete cascade,
  note text not null check (length(btrim(note)) between 3 and 240),
  blocked_by text not null,
  blocked_at timestamptz not null default now()
);

create table if not exists service_block_audit (
  id bigserial primary key,
  service_id text not null,
  action text not null check (action in ('block', 'unblock')),
  note text not null,
  actor text not null,
  at timestamptz not null default now()
);
create index if not exists service_block_audit_service_idx on service_block_audit (service_id, at desc);

-- Same default-deny posture as schema.sql: the backend's privileged role
-- bypasses RLS; the Supabase API roles see nothing.
alter table service_reliability enable row level security;
alter table service_blocks enable row level security;
alter table service_block_audit enable row level security;
