-- The services index (SRI-115 tickets 1 and 2; contract C1 in the parent
-- repository's design-docs/RELIABLE_SERVICES_BUILD.md). Idempotent: safe to
-- rerun, and safe whether or not W3's observations file ran first (it creates
-- index_observations with the same shared columns).
--
-- index_resources       one row per (method, route template): every mpp.dev
--                       listing, every Bazaar listing with >= 2 payers in 30
--                       days, every hosted service route, every reviewed or
--                       gateway-paid endpoint.
-- index_resource_urls   every concrete URL a source listed, and the resource it
--                       collapsed into (route templates, per-article URLs).
-- index_observations    one row per unpaid probe (kept 14 days), real purchase
--                       or C5 observation (kept).
-- index_changes         what changed and when: price, payee, down, dead,
--                       revived.
-- index_sweeps          one row per source per sweep, for /api/index/health.
-- index_resource_signals (view) what the reliability rule (C3) reads.

create table if not exists index_resources (
  id bigserial primary key,
  -- METHOD + normalized origin + path, every path parameter written as {}.
  resource_key text not null unique,
  method text not null,
  origin text not null,
  -- The same path with parameter names kept (":publicationSlug").
  route_template text not null,
  has_params boolean not null default false,
  -- A concrete URL to probe: the most-called listed instance of a template.
  probe_url text not null,
  -- The seller's own example request body, when a listing gave one.
  probe_body jsonb,
  listing_type text not null default 'http',
  protocol text,
  sources text[] not null default '{}',
  -- CAIP-2, limited to Base, Solana and Tempo.
  networks text[] not null default '{}',
  -- True when a hosted agent can pay it: a Base/Solana/Tempo offer in USDC
  -- with a scheme the gateway signs (x402 exact or upto, MPP charge).
  payable boolean not null default false,
  pay_to text[] not null default '{}',
  -- Latest listed price (the listing's word; the probe's is quoted_*).
  price_atomic numeric,
  price_asset text,
  price_network text,
  price_scheme text,
  bazaar_payers_30d integer,
  bazaar_calls_30d bigint,
  bazaar_last_called_at timestamptz,
  first_listed_at timestamptz not null default now(),
  -- Refreshed when the listing changes, and at least weekly while listed;
  -- sources[] is exact per sweep (a delisted resource loses its source that day).
  last_listed_at timestamptz not null default now(),
  source_seen jsonb not null default '{}'::jsonb,
  service_name text,
  description text,
  input_schema jsonb,
  tracked boolean not null default false,
  tracked_reason text
    check (tracked_reason is null or tracked_reason in
      ('hosted', 'gateway_purchase', 'bazaar_payers', 'proposal')),
  tracked_since timestamptz,
  proposal_ids text[] not null default '{}',
  service_id text references services(id) on delete set null,
  -- Probe state, maintained by crowdcode.index.record_observation.
  last_probed_at timestamptz,
  state_at timestamptz,
  liveness text not null default 'unknown'
    check (liveness in ('unknown', 'alive', 'failing', 'dead')),
  consecutive_failures integer not null default 0,
  failing_since timestamptz,
  last_ok_at timestamptz,
  quoted_price_atomic numeric,
  quoted_asset text,
  quoted_network text,
  price_since timestamptz,
  payees text[] not null default '{}',
  payee_since timestamptz,
  previous_payees text[],
  payee_changed_at timestamptz
);

-- Room for the daily probe-state update to stay on its page (a HOT update):
-- no index covers the columns it writes.
alter table index_resources set (fillfactor = 85);
create index if not exists index_resources_origin_idx on index_resources (origin);
create index if not exists index_resources_service_idx on index_resources (service_id);

create table if not exists index_resource_urls (
  source text not null,
  method text not null,
  url text not null,
  resource_id bigint not null references index_resources(id) on delete cascade,
  payers_30d integer,
  last_seen_at timestamptz not null default now(),
  primary key (source, method, url)
);
create index if not exists index_resource_urls_url_idx on index_resource_urls (url, method);
create index if not exists index_resource_urls_resource_idx on index_resource_urls (resource_id);

-- Shared with W3 (C5): the column names and types here must match its file.
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
  add column if not exists asset text,
  add column if not exists pay_to text[],
  add column if not exists protocol text,
  add column if not exists latency_ms int,
  add column if not exists review_id bigint,
  add column if not exists detail text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'index_observations_resource_fk') then
    alter table index_observations
      add constraint index_observations_resource_fk
      foreign key (resource_id) references index_resources(id) on delete set null;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'index_observations_kind_check') then
    alter table index_observations
      add constraint index_observations_kind_check
      check (kind in ('probe', 'purchase', 'observation'));
  end if;
end $$;

create index if not exists index_observations_resource_at_idx
  on index_observations (resource_id, at desc);
create index if not exists index_observations_unlinked_idx
  on index_observations (id) where resource_id is null;
create unique index if not exists index_observations_review_unique
  on index_observations (review_id) where kind = 'purchase';

create table if not exists index_changes (
  id bigserial primary key,
  resource_id bigint not null references index_resources(id) on delete cascade,
  at timestamptz not null,
  kind text not null check (kind in ('price', 'payee', 'down', 'dead', 'revived')),
  old_value text,
  new_value text,
  observation_id bigint references index_observations(id) on delete set null
);
create index if not exists index_changes_resource_at_idx on index_changes (resource_id, at desc);
create index if not exists index_changes_at_idx on index_changes (at desc);

create table if not exists index_sweeps (
  id bigserial primary key,
  source text not null,
  started_at timestamptz not null,
  finished_at timestamptz,
  ok boolean not null default false,
  items integer,
  resources integer,
  error text
);
create index if not exists index_sweeps_source_idx on index_sweeps (source, started_at desc);

-- Sellers who asked not to be probed (crowdcode.app/bot): an origin or a URL
-- prefix. The prober skips every resource whose probe URL starts with one.
create table if not exists index_opt_outs (
  prefix text primary key,
  note text,
  created_at timestamptz not null default now()
);

-- What reliability-v1 (C3) reads, one row per resource. W4 owns the rule;
-- this view only exposes the evidence.
create or replace view index_resource_signals with (security_invoker = true) as
select
  r.id as resource_id,
  r.service_id,
  r.tracked,
  r.tracked_reason,
  r.method,
  r.route_template,
  r.probe_url,
  r.payable,
  r.networks,
  r.liveness,
  r.consecutive_failures,
  r.failing_since,
  r.last_ok_at,
  r.last_probed_at,
  coalesce((
    select jsonb_agg(jsonb_build_object(
             'at', p.at, 'ok', p.ok, 'http_status', p.http_status,
             'error_code', p.error_code, 'payee', p.payee, 'pay_to', p.pay_to,
             'network', p.network, 'quoted_amount_atomic', p.quoted_amount_atomic,
             'asset', p.asset) order by p.at desc)
    from (
      select * from index_observations o
      where o.resource_id = r.id and o.kind = 'probe'
      order by o.at desc limit 3
    ) p
  ), '[]'::jsonb) as last_probes,
  r.payees,
  r.payee_since,
  r.previous_payees,
  r.payee_changed_at,
  r.quoted_price_atomic as latest_price_atomic,
  r.quoted_asset as latest_price_asset,
  r.quoted_network as latest_price_network,
  r.price_since,
  (
    select percentile_cont(0.5) within group (order by o.quoted_amount_atomic)
    from index_observations o
    where o.resource_id = r.id and o.kind = 'probe' and o.ok
      and o.quoted_amount_atomic is not null
      and o.asset is not distinct from r.quoted_asset
      and o.at > now() - interval '7 days'
  ) as median_price_7d_atomic,
  r.price_atomic as listed_price_atomic,
  r.price_asset as listed_price_asset,
  r.bazaar_payers_30d,
  r.bazaar_calls_30d,
  coalesce(s.payee_rotates, false) as payee_rotates
from index_resources r
left join services s on s.id = r.service_id;

-- Same default-deny posture as schema.sql: the backend's privileged role
-- bypasses RLS; the Supabase API roles see nothing.
alter table index_resources enable row level security;
alter table index_resource_urls enable row level security;
alter table index_observations enable row level security;
alter table index_changes enable row level security;
alter table index_sweeps enable row level security;
alter table index_opt_outs enable row level security;
