-- Apply when deploying the neutral public prior to both backend and cron.
-- Then replay all scores with run_consistency_sweep(); changing the default
-- alone does not update existing service scores. This supersedes scoring-v2.sql.
alter table services alter column score set default 3.0;
