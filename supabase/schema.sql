create table if not exists services (
  id text primary key,
  name text not null,
  directory_slug text unique,
  stripe_payee_ref text,
  canonical_origin text,
  canonical_endpoint text,
  payment_provider text,
  payment_target_ref text,
  metadata jsonb not null default '{}'::jsonb,
  created_from_review boolean not null default false,
  created_at timestamptz not null default now()
);

alter table services
  add column if not exists canonical_origin text,
  add column if not exists canonical_endpoint text,
  add column if not exists payment_provider text,
  add column if not exists payment_target_ref text,
  add column if not exists metadata jsonb not null default '{}'::jsonb,
  add column if not exists created_from_review boolean not null default false;

create table if not exists reviews (
  id bigserial primary key,
  service_id text not null references services(id) on delete cascade,
  rating int not null check (rating between 1 and 5),
  reason text not null,
  task_context text,
  payment_reference text not null unique,
  reviewer_id text,
  payment_provider text,
  payment_target_ref text,
  payment_proof jsonb not null default '{}'::jsonb,
  payment_verified boolean not null default false,
  payment_verified_at timestamptz,
  reviewer_wallet text,
  review_signature text,
  signature_scheme text,
  signature_verified boolean not null default false,
  created_at timestamptz not null default now()
);

alter table reviews
  add column if not exists payment_provider text,
  add column if not exists payment_target_ref text,
  add column if not exists payment_proof jsonb not null default '{}'::jsonb,
  add column if not exists payment_verified boolean not null default false,
  add column if not exists payment_verified_at timestamptz,
  add column if not exists reviewer_wallet text,
  add column if not exists review_signature text,
  add column if not exists signature_scheme text,
  add column if not exists signature_verified boolean not null default false;

create index if not exists reviews_service_id_created_at_idx
  on reviews (service_id, created_at desc);

create table if not exists service_identifiers (
  id bigserial primary key,
  service_id text not null references services(id) on delete cascade,
  identifier_type text not null,
  identifier_value text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (identifier_type, identifier_value)
);

create index if not exists service_identifiers_service_id_idx
  on service_identifiers (service_id);

create table if not exists service_requests (
  id bigserial primary key,
  service_description text not null,
  task_context text,
  directory_match text not null default 'missing'
    check (directory_match in ('missing', 'undiscovered', 'exists')),
  created_at timestamptz not null default now()
);

create index if not exists service_requests_directory_match_created_at_idx
  on service_requests (directory_match, created_at desc);

alter table services enable row level security;
alter table reviews enable row level security;
alter table service_identifiers enable row level security;
alter table service_requests enable row level security;
