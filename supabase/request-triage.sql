-- What the proposals pass did with each service request (SRI-122).
--
-- The pass lists every tractable request (a clear input, output and problem)
-- in a proposal on crowdcode.app/rankings#requests. A request it does not list
-- keeps its reason here, with the model's one-line note: `vague`, `internal`
-- (a fix only to the requester's own tools), `not_a_service`, `private` (the
-- proposal's wording repeated a private request or named something
-- traceable) or `unplaced` (the model left it out). The public payload carries
-- only the counts; the owner reads the rows on /admin through a reporting
-- view (app/scripts/reporting-role.ts in the parent repo).
--
-- Replaced on every run of the pass. Additive and idempotent. Apply BEFORE
-- deploying the backend that writes it.

create table if not exists service_request_triage (
  request_id bigint primary key references service_requests (id) on delete cascade,
  listed boolean not null,
  reason text check (reason in ('vague', 'internal', 'not_a_service', 'private', 'unplaced')),
  note text check (char_length(note) <= 160),
  proposal_id text,
  proposal_title text check (char_length(proposal_title) <= 70),
  triaged_at timestamptz not null default now(),
  check (listed = (reason is null))
);

alter table service_request_triage enable row level security;
