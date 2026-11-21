-- SRI-159: a service request can be rejected after it was stored, as a
-- feature of CrowdCode itself rather than a service an outside provider could
-- sell ('platform_internal'; request #245, "a per-service revenue breakdown
-- of my own services"). The ingest screen rejects such requests before they
-- are stored (request-screen-v2); this verdict marks one that got through.
-- The proposals pass shows it in /admin as `internal` and never proposes it.
-- Idempotent.
alter table service_requests drop constraint if exists service_requests_screen_verdict_check;
alter table service_requests add constraint service_requests_screen_verdict_check
  check (screen_verdict is null or screen_verdict in ('accept', 'unchecked', 'platform_internal'));
