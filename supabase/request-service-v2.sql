-- request_service v2 and private reviews (crowdcode-mcp 0.12.0).
--
-- Apply BEFORE deploying the 0.12.0 backend: the v2 write path inserts the
-- four request fields and leaves service_description null. Additive and safe
-- to rerun; the 0.11 backend keeps working against it.
--
-- A request is four concrete fields: the input the user will give, the exact
-- output they want, the problem it solves, and what they would pay per call.
-- Rows filed before v2 keep their free text in service_description; the
-- nightly job (crowdcode.proposals) maps each into the same fields once and
-- marks it `inferred`, with `low` confidence unless the text stated a
-- concrete input, output and price. Requests stay private: only generated,
-- aggregated proposals are published.
alter table service_requests
  alter column service_description drop not null,
  add column if not exists request_input text,
  add column if not exists request_output text,
  add column if not exists request_problem text,
  add column if not exists price_per_call_usd numeric(14, 6)
    check (price_per_call_usd is null or (price_per_call_usd > 0 and price_per_call_usd <= 100)),
  add column if not exists fields_source text
    check (fields_source is null or fields_source in ('agent', 'inferred')),
  add column if not exists inference_confidence text
    check (inference_confidence is null or inference_confidence in ('high', 'low')),
  add column if not exists inferred_at timestamptz;

-- The pre-v2 requested-services cache published example request text. The
-- 0.12.0 backend never serves it; remove it rather than let it linger.
delete from app_cache where key = 'project_ideas';
