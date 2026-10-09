-- 007_kubernetes.sql — clústeres de Kubernetes como fuente de datos (colector basado en Prometheus / kube-state-metrics).
alter table cloud_accounts drop constraint if exists cloud_accounts_provider_check;
alter table cloud_accounts add constraint cloud_accounts_provider_check
    check (provider in ('aws','azure','gcp','demo','import','kubernetes'));

alter table resources drop constraint if exists resources_provider_check;
alter table resources add constraint resources_provider_check
    check (provider in ('aws','azure','gcp','kubernetes'));
