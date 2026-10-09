-- 010_costs_evidence_savings.sql — costes por cuenta/servicio/región/período, evidencia versionada de cada recomendación,
-- ahorro aprobado congelado y medición del ahorro observado con línea base.
--
-- Principios:
--   · Todo lo nuevo lleva organization_id y RLS FORCE (la prueba por introspección de test_tenant_isolation.py lo exige).
--   · La evidencia y las líneas base son append-only para el rol de aplicación (solo SELECT/INSERT): lo que vio el aprobador
--     no se reescribe; si cambia, se inserta una versión nueva.

-- ---------------------------------------------------------------- costes por cuenta (Cost Explorer normalizado)
create table account_costs (
    id                bigserial primary key,
    organization_id   uuid not null references organizations(id),
    cloud_account_id  uuid not null references cloud_accounts(id),
    scan_id           uuid references scans(id),
    provider          text not null,
    account_ref       text not null,
    service           text not null,                      -- servicio normalizado (ec2, rds, s3, ...); 'other' si no se reconoce
    service_raw       text not null,                      -- nombre tal cual lo devuelve el proveedor
    region            text not null,                      -- 'global' para servicios sin región
    granularity       text not null check (granularity in ('DAILY', 'MONTHLY')),
    period_start      date not null,
    period_end        date not null,                      -- exclusivo
    amount            numeric(18,4) not null,             -- puede ser negativo (créditos, reembolsos)
    currency          text not null default 'USD',
    metric            text not null default 'UnblendedCost',
    estimated         boolean not null default false,     -- el proveedor marca el período como aún no definitivo
    source            text not null default 'cost_explorer',
    collected_at      timestamptz not null default now(),
    check (period_end > period_start),
    unique (organization_id, cloud_account_id, service_raw, region, granularity, period_start, metric, currency)
);
create index account_costs_lookup_idx on account_costs (organization_id, cloud_account_id, granularity, period_start);

-- ---------------------------------------------------------------- evidencia versionada de cada recomendación
create table recommendation_evidence (
    id                       uuid primary key default gen_random_uuid(),
    organization_id          uuid not null references organizations(id),
    recommendation_id        uuid not null references recommendations(id),
    recommendation_version   int not null,
    scan_id                  uuid references scans(id),
    rule_id                  text not null,
    formula_id               text not null,                 -- p. ej. ec2_downsize.v1
    data_as_of               timestamptz not null,          -- momento en que se recogieron los datos que sustentan la cifra
    reference_cost           numeric(14,2) not null,        -- coste mensual de referencia usado en la fórmula
    reference_cost_source    text not null,
    reference_window_start   date,
    reference_window_end     date,
    estimated_monthly_savings numeric(14,2) not null,
    confidence               numeric(4,3) not null,
    evidence                 jsonb not null,                -- evidencia completa (métricas, umbrales, base de coste, parche...)
    assumptions              jsonb not null default '[]'::jsonb,
    inputs                   jsonb not null default '{}'::jsonb,
    evidence_hash            text not null,                 -- sha256 de la forma canónica; también va a la cadena de auditoría
    created_at               timestamptz not null default now()
);
create index recommendation_evidence_rec_idx on recommendation_evidence (organization_id, recommendation_id, created_at);

-- ---------------------------------------------------------------- líneas base de ahorro (coste previo al cambio)
create table savings_baselines (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    recommendation_id  uuid not null references recommendations(id),
    phase              text not null check (phase in ('approval', 'deployment')),
    window_start       date,
    window_end         date,                                -- exclusivo
    days_expected      int not null default 0,
    days_with_data     int not null default 0,
    daily_costs        jsonb not null default '[]'::jsonb,  -- [["2026-09-01", 4.8], ...]
    total_cost         numeric(14,4) not null default 0,
    monthly_cost       numeric(14,2) not null,              -- promedio diario × 30,4375
    cost_source        text not null,                       -- cost_explorer | import | estimate | estimate_only
    data_grade         text not null check (data_grade in ('billing', 'model')),
    usage              jsonb not null default '{}'::jsonb,  -- utilización en ese momento (cpu_avg, estado, tipo...)
    baseline_hash      text not null,
    created_by         text not null,
    created_at         timestamptz not null default now()
);
create index savings_baselines_rec_idx on savings_baselines (organization_id, recommendation_id, phase, created_at);

-- ---------------------------------------------------------------- recomendaciones: ahorro aprobado y obsolescencia
alter table recommendations
    add column approved_monthly_savings  numeric(14,2),     -- ahorro que vio el aprobador (congelado al completar las aprobaciones)
    add column approved_baseline_cost    numeric(14,2),
    add column approved_at               timestamptz,
    add column approved_evidence_id      uuid references recommendation_evidence(id),
    add column evidence_hash             text,              -- hash de la evidencia vigente
    add column stale_since               timestamptz,       -- el recurso o la condición ya no existe en el último escaneo completo
    add column stale_reason              text;

alter table recommendations drop constraint recommendations_status_check;
alter table recommendations add constraint recommendations_status_check check (status in (
    'DETECTED','ANALYZED','PROPOSED','PENDING_APPROVAL','APPROVED','REJECTED',
    'PR_CREATED','MERGED','DEPLOYED','VERIFIED','EXPIRED'));

-- ---------------------------------------------------------------- verificación de ahorro: medición controlada
alter table savings_verifications drop constraint savings_verifications_method_check;
alter table savings_verifications add constraint savings_verifications_method_check
    check (method in ('cost_records', 'manual', 'cost_records_controlled'));
alter table savings_verifications
    add column estimated_monthly_savings      numeric(14,2),  -- estimación vigente en el último escaneo
    add column approved_monthly_savings       numeric(14,2),  -- estimación congelada al aprobar (base de realization_pct)
    add column raw_observed_monthly_savings   numeric(14,2),  -- diferencia directa entre línea base y coste posterior
    add column baseline_id                    uuid references savings_baselines(id),
    add column baseline_window_start          date,
    add column baseline_window_end            date,
    add column data_grade                     text not null default 'model' check (data_grade in ('billing', 'model', 'declared')),
    add column attribution                    text not null default 'unverified'
        check (attribution in ('confirmed', 'partial', 'exceeds_model', 'not_applied', 'confounded', 'inconclusive', 'unverified')),
    add column confidence_grade               text not null default 'low' check (confidence_grade in ('high', 'medium', 'low')),
    add column confounders                    jsonb not null default '[]'::jsonb,
    add column controls                       jsonb not null default '{}'::jsonb,
    add column limitations                    jsonb not null default '[]'::jsonb;

-- ---------------------------------------------------------------- RLS y permisos
do $$
declare
    t text;
begin
    foreach t in array array['account_costs', 'recommendation_evidence', 'savings_baselines']
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format(
            'create policy tenant_isolation on %I using (organization_id = current_org()) with check (organization_id = current_org())',
            t);
    end loop;
end $$;

grant select, insert, update on account_costs to cloudcost_app;          -- upsert por escaneo
grant select, insert on recommendation_evidence, savings_baselines to cloudcost_app;   -- append-only
grant usage, select on all sequences in schema public to cloudcost_app;
