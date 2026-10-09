-- 006_provider_config.sql — datos NO secretos que necesitan los conectores Azure y GCP.
-- Azure: {"tenant_id", "client_id", "credentials_ref"}; GCP: {"credentials_ref"}. El secreto nunca se guarda: solo su referencia (env:/aws-sm:).
alter table cloud_accounts add column if not exists provider_config jsonb not null default '{}'::jsonb;
