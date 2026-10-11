-- 013_budgets.sql — guardrail de presupuesto por organización (tenant): límites, medición de uso y alertas.
--
--   · tenant_budgets: un límite mensual por (organización, métrica) con umbral de aviso, modo de aplicación y tope por tarea.
--   · usage_meter:    consumo medido (append-only): cada consulta/tarea que cuesta dinero deja una fila.
--   · budget_alerts:  alertas tempranas (aviso, previsión, superado, bloqueo) deduplicadas por período.
-- Todo con organization_id y RLS FORCE (lo exige test_tenant_isolation.py).

create table tenant_budgets (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    metric             text not null check (metric in ('api_usage', 'cloud_spend')),
    monthly_limit_usd  numeric(14,2) not null check (monthly_limit_usd >= 0),
    per_task_limit_usd numeric(14,4) check (per_task_limit_usd is null or per_task_limit_usd >= 0),
    warn_ratio         numeric(4,3) not null default 0.80 check (warn_ratio > 0 and warn_ratio <= 1),
    mode               text not null default 'alert' check (mode in ('alert', 'degrade', 'block')),
    enabled            boolean not null default true,
    updated_by         text not null,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    unique (organization_id, metric)
);

create table usage_meter (
    id                bigserial primary key,
    organization_id   uuid not null references organizations(id),
    source            text not null check (char_length(source) between 1 and 60),   -- p. ej. cost_explorer, qdrant_query, scan
    ref               text,                                                         -- id de escaneo/tarea (opcional)
    quantity          numeric(14,2) not null default 1 check (quantity >= 0),
    cost_usd          numeric(14,4) not null check (cost_usd >= 0),
    period            date not null,                                                -- primer día del mes UTC
    created_at        timestamptz not null default now()
);
create index usage_meter_period_idx on usage_meter (organization_id, period);

create table budget_alerts (
    id                bigserial primary key,
    organization_id   uuid not null references organizations(id),
    metric            text not null,
    level             text not null check (level in ('warn', 'forecast', 'exceeded', 'blocked', 'degraded')),
    period            date not null,
    message           text not null,
    details           jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now(),
    unique (organization_id, metric, level, period)
);

do $$
declare
    t text;
begin
    foreach t in array array['tenant_budgets', 'usage_meter', 'budget_alerts']
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format(
            'create policy tenant_isolation on %I using (organization_id = current_org()) with check (organization_id = current_org())',
            t);
    end loop;
end $$;

grant select, insert, update on tenant_budgets to cloudcost_app;
grant select, insert on usage_meter to cloudcost_app;                    -- append-only
grant select, insert on budget_alerts to cloudcost_app;
grant usage, select on all sequences in schema public to cloudcost_app;
