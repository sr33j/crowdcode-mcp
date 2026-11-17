-- The capability tree (SRI-115 ticket 8; contract C7 in the parent repository's
-- design-docs/RELIABLE_SERVICES_BUILD.md). Idempotent: safe to rerun. Apply
-- after services-index.sql. src/crowdcode/capability_tree.py is the only writer.
--
-- Small by design (the shared database is near its size limit):
--   capability_classifications  one short row per tracked resource and per
--                               proposal: its node(s) and facet. ~5k rows, < 2 MB.
--   capability_leaves           the candidate leaves the model proposed, with
--                               the facets each one groups. ~100 rows.
--   capability_tree_versions    one row per build (the whole tree as jsonb),
--                               the newest 14 kept, plus the published one.

create table if not exists capability_classifications (
  -- 'r:<index_resources.id>' or 'p:<service proposal id>'
  item_key text primary key,
  -- What was sent to the model, hashed with the classifier version: a changed
  -- description or schema is classified again, nothing else is.
  fingerprint text not null,
  -- [{"node": "video", "facet": "text-to-video"}], one or two entries.
  assignments jsonb not null default '[]'::jsonb,
  classified_at timestamptz not null default now()
);

create table if not exists capability_leaves (
  id text primary key,          -- '<node>.<slug>', stable once proposed
  node text not null,
  slug text not null,
  label text not null,
  when_to_look text,
  facets text[] not null default '{}',
  proposed_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists capability_leaves_node_idx on capability_leaves (node);

create table if not exists capability_tree_versions (
  version integer primary key,
  built_at timestamptz not null default now(),
  -- Served as `current` only once published. W8's test gate publishes a
  -- version that passed; until it exists, every build is published when stored.
  published boolean not null default false,
  published_at timestamptz,
  published_by text,
  trigger text,                 -- first | nightly | changed | forced
  fingerprint text not null,    -- classification + leaves + C3 states
  stats jsonb not null default '{}'::jsonb,
  tree jsonb not null
);
create index if not exists capability_tree_versions_published_idx
  on capability_tree_versions (version desc) where published;

-- Same default-deny posture as schema.sql: the backend's privileged role
-- bypasses RLS; the Supabase API roles see nothing.
alter table capability_classifications enable row level security;
alter table capability_leaves enable row level security;
alter table capability_tree_versions enable row level security;
