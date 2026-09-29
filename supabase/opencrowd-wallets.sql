-- Optional operator migration, only for the shared hosted OpenCrowd database.
-- Run as the operator who owns both schemas, not the restricted app role.
-- This bridge exposes addresses only; no account data or signing credentials.
-- Deleted agents retain their historical identity and trusted review influence.
-- Legacy externally-owned wallets and demo/testnet agents are not auto-trusted.
create or replace view public.opencrowd_seed_wallets
with (security_barrier = true) as
select distinct lower(wallet_address) as wallet_address
from opencrowd.agents
where mode = 'mainnet' and wallet_kind = 'agent_eoa'
  and wallet_address ~* '^0x[0-9a-f]{40}$';

revoke all on public.opencrowd_seed_wallets from public;

-- The CrowdCode operator owns/reads this view. No new privileges are granted
-- to hosted app, gateway, anonymous, or authenticated database roles.
-- Backend ensure_user() consults it before scoring a wallet's first review;
-- seed reconciliation also imports it at startup and on each cron run.
