-- 012_datastores.sql — Qdrant y PostgreSQL como fuentes de datos (colectores de solo lectura).
alter table cloud_accounts drop constraint if exists cloud_accounts_provider_check;
alter table cloud_accounts add constraint cloud_accounts_provider_check
    check (provider in ('aws','azure','gcp','demo','import','kubernetes','qdrant','postgres'));

alter table resources drop constraint if exists resources_provider_check;
alter table resources add constraint resources_provider_check
    check (provider in ('aws','azure','gcp','kubernetes','qdrant','postgres'));
