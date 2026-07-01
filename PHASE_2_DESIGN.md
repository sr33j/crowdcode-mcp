# Phase 2 Design

Phase 2 adds demand capture for missing services after the v1 score and review loop is working.

## Goals

- Let agents record unmet service needs when no existing service fits.
- Let maintainers inspect recent unmet requests.
- Keep request capture separate from the v1 review flow until ranking, moderation, and directory matching are better defined.

## Proposed Tools

### `request_service(service_description, task_context?)`

Capture an unmet service request.

Input:

```json
{
  "service_description": "Find a service that can generate migration tests for a Django app.",
  "task_context": "Python monolith migration"
}
```

Output:

```json
{
  "accepted": true,
  "request_id": 123,
  "directory_match": "missing"
}
```

Validation:

- `service_description` is required.
- `service_description` should describe a specific reusable service capability.
- The capability should include expected inputs and outputs, or the state change
  the service performs.
- The description should be general enough to represent demand from multiple
  users, not just one user's immediate task.
- `task_context` is optional.
- New requests default to `directory_match = "missing"`.

`list_service_requests` is intentionally out of scope for this phase.

## Data Model

The current draft table is:

```sql
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
```

## Open Questions

- Should agents call `request_service` automatically, or only after explicit user consent?
- Should requests be deduplicated or clustered before they appear on a board?
- Should `directory_match = "exists"` link to a concrete service id?
- Should request capture include price, latency, or task category fields?
