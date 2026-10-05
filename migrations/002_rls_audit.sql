-- 002_rls_audit.sql — aislamiento multi-tenant (RLS), auditoría append-only con cadena de hashes y permisos.

-- Contexto de tenant: la API/worker ejecutan  select set_config('app.current_org', '<uuid>', true)
-- al inicio de cada transacción. Sin contexto => current_org() es NULL => no se ve ninguna fila.
create or replace function current_org() returns uuid
language sql stable as $$
    select nullif(current_setting('app.current_org', true), '')::uuid
$$;

-- ---------------------------------------------------------------- RLS
do $$
declare
    t text;
begin
    foreach t in array array[
        'users','cloud_accounts','repositories','scans','resources','resource_metrics','cost_records',
        'policies','recommendations','recommendation_actions','approvals','pull_requests',
        'savings_verifications','audit_events'
    ]
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format(
            'create policy tenant_isolation on %I using (organization_id = current_org()) with check (organization_id = current_org())',
            t);
    end loop;
end $$;

alter table organizations enable row level security;
alter table organizations force row level security;
create policy tenant_isolation on organizations using (id = current_org()) with check (id = current_org());

-- ---------------------------------------------------------------- Auditoría inmutable
create or replace function audit_canonical(e audit_events) returns text
language sql stable as $$
    select concat_ws('|',
        e.seq::text,
        e.organization_id::text,
        e.prev_hash,
        e.event_type,
        e.actor_type,
        coalesce(e.actor_id, ''),
        coalesce(e.entity_type, ''),
        coalesce(e.entity_id, ''),
        to_char(e.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
        e.payload::text)
$$;

create or replace function audit_events_chain() returns trigger
language plpgsql as $$
declare
    last_row record;
begin
    -- serializa inserciones por organización para que la cadena no se bifurque
    perform pg_advisory_xact_lock(hashtextextended(new.organization_id::text, 42));

    select seq, hash into last_row
      from audit_events
     where organization_id = new.organization_id
     order by seq desc
     limit 1;

    new.seq        := coalesce(last_row.seq, 0) + 1;
    new.prev_hash  := coalesce(last_row.hash, repeat('0', 64));
    new.created_at := clock_timestamp();
    new.hash       := encode(sha256(convert_to(audit_canonical(new), 'UTF8')), 'hex');
    return new;
end $$;

create trigger audit_events_chain_trg
    before insert on audit_events
    for each row execute function audit_events_chain();

create or replace function audit_events_immutable() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_events es append-only (% no permitido)', tg_op
        using errcode = 'insufficient_privilege';
end $$;

create trigger audit_events_no_update_delete
    before update or delete on audit_events
    for each row execute function audit_events_immutable();

create trigger audit_events_no_truncate
    before truncate on audit_events
    for each statement execute function audit_events_immutable();

-- Recorre la cadena de la organización activa y devuelve el primer eslabón roto (si existe).
create or replace function verify_audit_chain()
returns table (ok boolean, checked bigint, first_bad_seq bigint)
language plpgsql stable as $$
declare
    r             audit_events;
    expected_prev text := repeat('0', 64);
    expected_seq  bigint := 0;
    n             bigint := 0;
begin
    for r in select * from audit_events where organization_id = current_org() order by seq loop
        n := n + 1;
        expected_seq := expected_seq + 1;
        if r.seq <> expected_seq
           or r.prev_hash <> expected_prev
           or r.hash <> encode(sha256(convert_to(audit_canonical(r), 'UTF8')), 'hex') then
            return query select false, n, r.seq;
            return;
        end if;
        expected_prev := r.hash;
    end loop;
    return query select true, n, null::bigint;
end $$;

-- ---------------------------------------------------------------- Permisos del rol de aplicación
-- El rol cloudcost_app NO es superusuario ni tiene BYPASSRLS (lo crea scripts/migrate.sh).
grant usage on schema public to cloudcost_app;
grant select on organizations to cloudcost_app;
grant select, insert, update on
    users, cloud_accounts, repositories, scans, resources, policies, recommendations,
    pull_requests, savings_verifications
    to cloudcost_app;
grant select, insert on
    resource_metrics, recommendation_actions, approvals
    to cloudcost_app;
grant select, insert, update on cost_records to cloudcost_app;      -- upsert diario de costos
grant select, insert on audit_events to cloudcost_app;      -- sin UPDATE/DELETE/TRUNCATE
grant usage, select on all sequences in schema public to cloudcost_app;
grant execute on function current_org(), verify_audit_chain(), audit_canonical(audit_events) to cloudcost_app;
