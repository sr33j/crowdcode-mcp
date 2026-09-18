-- Apply after deploying backend 0.5.2. Safe to rerun.
begin;
-- A provider wallet may collect payment for multiple independently scored
-- endpoints. Only product identifiers remain globally unique.
-- Deploy backend >= 0.5.2 before applying this block to an existing database.
create unique index if not exists service_identifiers_product_unique
  on service_identifiers (identifier_type, identifier_value)
  where identifier_type <> 'payment_target';
create unique index if not exists service_identifiers_service_unique
  on service_identifiers (service_id, identifier_type, identifier_value);
alter table service_identifiers
  drop constraint if exists service_identifiers_identifier_type_identifier_value_key;

-- Recover associations omitted by the old global wallet uniqueness rule.
insert into service_identifiers (service_id, identifier_type, identifier_value)
select id, 'payment_target', payment_provider || ':' || payment_target_ref
from services where payment_provider is not null and payment_target_ref is not null
on conflict do nothing;

commit;
