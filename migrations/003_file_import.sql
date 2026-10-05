-- 003_file_import.sql — importación de archivos (CSV) como origen de datos.
alter table cloud_accounts drop constraint if exists cloud_accounts_provider_check;
alter table cloud_accounts add constraint cloud_accounts_provider_check
    check (provider in ('aws','azure','gcp','demo','import'));

create table imports (
    id                uuid primary key default gen_random_uuid(),
    organization_id   uuid not null references organizations(id),
    cloud_account_id  uuid not null references cloud_accounts(id),
    filename          text not null,
    row_count         int not null check (row_count between 1 and 5000),
    rows              jsonb not null,
    created_by        text not null,
    created_at        timestamptz not null default now()
);
create index imports_account_idx on imports (organization_id, cloud_account_id, created_at desc);

alter table imports enable row level security;
alter table imports force row level security;
create policy tenant_isolation on imports using (organization_id = current_org()) with check (organization_id = current_org());

grant select, insert on imports to cloudcost_app;
