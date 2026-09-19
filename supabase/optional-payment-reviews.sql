-- Apply before deploying the optional-payment backend. Safe to repeat;
-- old clients continue using their existing unique payment references.
begin;
alter table reviews alter column payment_reference drop not null;
alter table reviews add column if not exists review_nonce text;
create unique index if not exists reviews_wallet_nonce_idx
  on reviews (lower(reviewer_wallet), review_nonce)
  where review_nonce is not null;
commit;
