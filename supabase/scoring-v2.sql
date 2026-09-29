-- Apply with the backend paused, before deploying crowdcode-scoring-v2.
-- Then run run_consistency_sweep() from that deployment before serving scores.
-- The replay refreshes stored scores from review history; do not increment
-- cached scores in SQL, which would be non-idempotent and could mix versions.
alter table services alter column score set default 4.0;
