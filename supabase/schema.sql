create table if not exists services (
  id text primary key,
  name text not null,
  directory_slug text unique,
  stripe_payee_ref text,
  created_at timestamptz not null default now()
);

create table if not exists reviews (
  id bigserial primary key,
  service_id text not null references services(id) on delete cascade,
  rating int not null check (rating between 1 and 5),
  reason text not null,
  task_context text,
  payment_reference text not null unique,
  payment_protocol text not null default 'unknown',
  payment_rail text,
  payment_status text,
  payment_amount int,
  payment_currency text,
  payment_metadata jsonb not null default '{}'::jsonb,
  reviewer_id text,
  created_at timestamptz not null default now()
);

create index if not exists reviews_service_id_created_at_idx
  on reviews (service_id, created_at desc);

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
