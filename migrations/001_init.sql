-- 001_init.sql — esquema base de CloudCost Optimizer (Fase 1 / MVP)
-- Todas las tablas de negocio llevan organization_id; el aislamiento lo aplica RLS (002_rls_audit.sql).

create table organizations (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    slug        text not null unique,
    created_at  timestamptz not null default now()
);

create table users (
    id               uuid primary key default gen_random_uuid(),
    organization_id  uuid not null references organizations(id),
    external_id      text not null,                       -- claim "sub" del IdP (OIDC)
    email            text,
    role             text not null check (role in ('ADMIN','FINOPS','SRE','DEVELOPER','AUDITOR','VIEWER')),
    created_at       timestamptz not null default now(),
    unique (organization_id, external_id)
);

create table cloud_accounts (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    provider           text not null check (provider in ('aws','azure','gcp','demo')),
    account_ref        text not null,                     -- AWS account id, Azure subscription, GCP project
    display_name       text not null,
    role_arn           text,                              -- rol de solo lectura a asumir (AWS)
    external_id_ref    text,                              -- referencia a secreto (env:NAME | aws-sm:id), nunca el valor
    regions            text[] not null default '{us-east-1}',
    status             text not null default 'ACTIVE' check (status in ('ACTIVE','DISABLED')),
    created_at         timestamptz not null default now(),
    unique (organization_id, provider, account_ref)
);

create table repositories (
    id                uuid primary key default gen_random_uuid(),
    organization_id   uuid not null references organizations(id),
    provider          text not null check (provider in ('github','gitlab','local')),
    full_name         text not null,                      -- owner/repo
    default_branch    text not null default 'main',
    iac_paths         text[] not null default '{.}',      -- carpetas a escanear en busca de .tf
    token_ref         text,                               -- referencia a secreto; null => token global de la plataforma
    created_at        timestamptz not null default now(),
    unique (organization_id, provider, full_name)
);

create table scans (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    cloud_account_id   uuid not null references cloud_accounts(id),
    repository_id      uuid references repositories(id),
    requested_by       text not null,
    status             text not null default 'QUEUED'
                         check (status in ('QUEUED','RUNNING','SUCCEEDED','FAILED','TIMED_OUT')),
    attempts           int not null default 0,
    max_attempts       int not null default 3,
    timeout_seconds    int not null default 900,
    celery_task_id     text,
    error              text,
    stats              jsonb not null default '{}'::jsonb,
    created_at         timestamptz not null default now(),
    started_at         timestamptz,
    finished_at        timestamptz
);

create table resources (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    cloud_account_id   uuid not null references cloud_accounts(id),
    last_scan_id       uuid references scans(id),
    provider           text not null check (provider in ('aws','azure','gcp')),
    resource_type      text not null check (resource_type in ('compute','storage','database','kubernetes')),
    service            text not null,                     -- ec2 | ebs | ebs_snapshot | rds | ...
    resource_id        text not null,
    region             text not null,
    name               text,
    environment        text not null default 'unknown',
    instance_type      text,
    volume_type        text,
    state              text,
    monthly_cost       numeric(14,2) not null default 0,
    cost_source        text not null default 'estimate',
    cpu_avg            numeric(6,2),
    cpu_max            numeric(6,2),
    memory_avg         numeric(6,2),
    attached           boolean,
    size_gb            numeric(12,2),
    age_days           int,
    observation_days   int not null default 0,
    unattached_days    int,
    tags               jsonb not null default '{}'::jsonb,
    iac_address        text,
    iac_file           text,
    attributes         jsonb not null default '{}'::jsonb,
    active             boolean not null default true,
    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),
    unique (organization_id, cloud_account_id, resource_id)
);
create index resources_org_active_idx on resources (organization_id, active);

create table resource_metrics (
    id               bigserial primary key,
    organization_id  uuid not null references organizations(id),
    resource_pk      uuid not null references resources(id) on delete cascade,
    metric           text not null,
    window_start     timestamptz not null,
    window_end       timestamptz not null,
    avg_value        numeric(12,4),
    max_value        numeric(12,4),
    samples          int,
    created_at       timestamptz not null default now()
);
create index resource_metrics_idx on resource_metrics (organization_id, resource_pk, metric);

create table cost_records (
    id                bigserial primary key,
    organization_id   uuid not null references organizations(id),
    cloud_account_id  uuid not null references cloud_accounts(id),
    resource_pk       uuid references resources(id) on delete set null,
    provider          text not null,
    usage_date        date not null,
    amount            numeric(14,4) not null,
    currency          text not null default 'USD',
    service           text not null default 'unknown',
    region            text,
    source            text not null default 'estimate',
    unique nulls not distinct (organization_id, cloud_account_id, resource_pk, usage_date, service)
);
create index cost_records_idx on cost_records (organization_id, resource_pk, usage_date);

create table policies (
    id               uuid primary key default gen_random_uuid(),
    organization_id  uuid not null references organizations(id),
    name             text not null,
    kind             text not null check (kind in ('rule_config','guardrail')),
    config           jsonb not null default '{}'::jsonb,
    enabled          boolean not null default true,
    created_at       timestamptz not null default now(),
    unique (organization_id, name)
);

create table recommendations (
    id                         uuid primary key default gen_random_uuid(),
    organization_id            uuid not null references organizations(id),
    scan_id                    uuid references scans(id),
    cloud_account_id           uuid not null references cloud_accounts(id),
    repository_id              uuid references repositories(id),
    resource_pk                uuid not null references resources(id),
    rule_id                    text not null,
    action                     text not null,
    status                     text not null default 'DETECTED' check (status in (
                                 'DETECTED','ANALYZED','PROPOSED','PENDING_APPROVAL','APPROVED','REJECTED',
                                 'PR_CREATED','MERGED','DEPLOYED','VERIFIED')),
    title                      text not null,
    summary                    text not null,
    explanation                text,
    alternatives               jsonb not null default '[]'::jsonb,
    current_monthly_cost       numeric(14,2) not null,
    projected_monthly_cost     numeric(14,2) not null,
    estimated_monthly_savings  numeric(14,2) not null check (estimated_monthly_savings >= 0),
    confidence                 numeric(4,3) not null check (confidence between 0 and 1),
    risk                       text not null check (risk in ('LOW','MEDIUM','HIGH')),
    impact                     text not null check (impact in ('LOW','MEDIUM','HIGH')),
    priority                   text not null check (priority in ('P1','P2','P3')),
    destructive                boolean not null default false,
    automation_blocked         boolean not null default false,
    approvals_required         int not null default 1 check (approvals_required between 1 and 5),
    policy                     jsonb not null default '{}'::jsonb,
    evidence                   jsonb not null default '{}'::jsonb,
    params                     jsonb not null default '{}'::jsonb,
    llm_advice                 jsonb,
    version                    int not null default 1,
    dedupe_key                 text not null,
    created_at                 timestamptz not null default now(),
    updated_at                 timestamptz not null default now(),
    deployed_at                timestamptz,
    verified_at                timestamptz,
    unique (organization_id, dedupe_key)
);
create index recommendations_status_idx on recommendations (organization_id, status);
create index recommendations_risk_idx   on recommendations (organization_id, risk);
create index recommendations_resource_idx on recommendations (organization_id, resource_pk);

create table recommendation_actions (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    recommendation_id  uuid not null references recommendations(id) on delete cascade,
    from_status        text,
    to_status          text not null,
    actor_type         text not null check (actor_type in ('system','user','webhook')),
    actor_id           text,
    note               text,
    created_at         timestamptz not null default clock_timestamp()
);
create index recommendation_actions_idx on recommendation_actions (organization_id, recommendation_id, created_at);

create table approvals (
    id                      uuid primary key default gen_random_uuid(),
    organization_id         uuid not null references organizations(id),
    recommendation_id       uuid not null references recommendations(id) on delete cascade,
    user_id                 uuid not null references users(id),
    approver_role           text not null,
    decision                text not null check (decision in ('APPROVED','REJECTED')),
    reason                  text not null,
    recommendation_version  int not null,
    context                 jsonb not null default '{}'::jsonb,   -- snapshot de ahorro/riesgo/confianza al decidir
    created_at              timestamptz not null default now(),
    unique (recommendation_id, user_id)
);

create table pull_requests (
    id                 uuid primary key default gen_random_uuid(),
    organization_id    uuid not null references organizations(id),
    recommendation_id  uuid not null unique references recommendations(id),
    repository_id      uuid not null references repositories(id),
    provider           text not null,
    number             int not null,
    url                text not null,
    branch             text not null,
    base_branch        text not null,
    draft              boolean not null default false,
    state              text not null default 'open' check (state in ('open','merged','closed')),
    diff               text,
    validations        jsonb not null default '[]'::jsonb,
    created_by         text not null,
    created_at         timestamptz not null default now(),
    merged_at          timestamptz
);
create index pull_requests_lookup_idx on pull_requests (organization_id, repository_id, number);

create table savings_verifications (
    id                          uuid primary key default gen_random_uuid(),
    organization_id             uuid not null references organizations(id),
    recommendation_id           uuid not null unique references recommendations(id),
    expected_monthly_savings    numeric(14,2) not null,
    baseline_monthly_cost       numeric(14,2) not null,
    observed_monthly_cost       numeric(14,2) not null,
    observed_monthly_savings    numeric(14,2) not null,
    realization_pct             numeric(8,2),
    window_start                date,
    window_end                  date,
    method                      text not null check (method in ('cost_records','manual')),
    created_by                  text not null,
    created_at                  timestamptz not null default now()
);

-- Auditoría: la numeración y la cadena de hashes las calcula el trigger de 002_rls_audit.sql.
create table audit_events (
    id               uuid primary key default gen_random_uuid(),
    organization_id  uuid not null references organizations(id),
    seq              bigint not null,
    event_type       text not null check (length(event_type) > 0),
    actor_type       text not null check (actor_type in ('system','user','webhook')),
    actor_id         text,
    entity_type      text,
    entity_id        text,
    payload          jsonb not null default '{}'::jsonb,
    prev_hash        text not null,
    hash             text not null,
    created_at       timestamptz not null default now(),
    unique (organization_id, seq)
);
create index audit_events_type_idx on audit_events (organization_id, event_type, created_at desc);
