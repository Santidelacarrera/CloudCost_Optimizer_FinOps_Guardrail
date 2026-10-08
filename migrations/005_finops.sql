-- Ajustes por cuenta cloud que no son secretos (etiqueta de asignación de costos, suscripción/proyecto, etc.).
alter table cloud_accounts add column if not exists settings jsonb not null default '{}'::jsonb;
