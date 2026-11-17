-- Offerings and model identity (SRI-115 C9). Idempotent; apply after
-- services-index.sql.
--
-- index_resources.model / model_family / modality
--     the one model an offering sells, when it sells one (else model null,
--     and modality only if all its models share one).
-- index_offering_models
--     one row per (offering, model): single-model offerings and every model a
--     seller's own catalog (/models) lists for a multi-model endpoint.
-- model_quality_priors
--     public leaderboard scores per model (Artificial Analysis arenas and the
--     Intelligence Index), with source URL and date; prior_rating is 1-5.

alter table index_resources
  add column if not exists model text,
  add column if not exists model_family text,
  add column if not exists modality text;

-- C9 tracks every paid mpp.dev endpoint: tracked_reason 'curated'.
alter table index_resources drop constraint if exists index_resources_tracked_reason_check;
alter table index_resources add constraint index_resources_tracked_reason_check
  check (tracked_reason is null or tracked_reason in
    ('hosted', 'gateway_purchase', 'bazaar_payers', 'curated', 'proposal'));

create index if not exists index_resources_model_idx on index_resources (model) where model is not null;
create index if not exists index_resources_modality_idx on index_resources (modality) where modality is not null;

create table if not exists index_offering_models (
  resource_id bigint not null references index_resources(id) on delete cascade,
  model text not null,
  model_family text not null,
  modality text not null,
  -- The seller's own id for it ('kwaivgi/kling-v3.0-pro'), from its catalog.
  seller_model_id text,
  source text not null check (source in ('models_endpoint', 'route', 'description')),
  -- The seller's own pricing for this model, as published (pricing_skus etc).
  pricing jsonb,
  usd_per_second numeric,
  updated_at timestamptz not null default now(),
  primary key (resource_id, model)
);
create index if not exists index_offering_models_model_idx on index_offering_models (model);
create index if not exists index_offering_models_modality_idx on index_offering_models (modality);

create table if not exists model_quality_priors (
  model text primary key,
  model_family text not null,
  modality text not null,
  -- 'artificialanalysis:text-to-video' and so on.
  source text not null,
  source_url text not null,
  as_of date not null,
  -- Arena Elo for media boards; null for the language model index.
  elo numeric,
  metric text not null,
  score numeric not null,
  rank int not null,
  -- 1 + 4 x percentile of the model on its board (best = 5), on the judge's scale.
  prior_rating numeric not null,
  leaderboard_name text,
  fetched_at timestamptz not null default now()
);
create index if not exists model_quality_priors_modality_idx on model_quality_priors (modality);

alter table index_offering_models enable row level security;
alter table model_quality_priors enable row level security;
