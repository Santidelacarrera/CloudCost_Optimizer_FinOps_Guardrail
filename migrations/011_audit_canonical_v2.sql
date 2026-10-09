-- 011_audit_canonical_v2.sql — forma canónica de la cadena de auditoría sin ambigüedad.
--
-- Defecto corregido: la versión 1 unía los campos con «|» (concat_ws). Si un valor contiene «|» —p. ej. el `sub` de un IdP, como
-- `auth0|abc123`— se puede mover texto entre dos campos contiguos (actor_id ↔ entity_type…) SIN cambiar la cadena concatenada ni, por
-- tanto, el hash: quien tuviera acceso de propietario a la base podía reatribuir un evento sin que `verify_audit_chain()` lo notara.
--
-- La versión 2 antepone a cada campo su longitud (`5:valor`), de modo que dos distribuciones distintas del mismo texto producen
-- cadenas distintas. Las filas existentes siguen siendo v1 (su hash no se recalcula: la cadena ya escrita es inmutable); las nuevas
-- son v2. `verify_audit_chain()` no cambia: cada fila se verifica con la versión con la que se escribió.

alter table audit_events add column if not exists canon_v smallint not null default 1 check (canon_v in (1, 2));

create or replace function audit_canonical(e audit_events) returns text
language sql stable as $$
    select case
        when e.canon_v = 1 then concat_ws('|',
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
        else (
            select 'v2|' || string_agg(length(f) || ':' || f, '|' order by n)
              from unnest(array[
                  e.seq::text, e.organization_id::text, e.prev_hash, e.event_type, e.actor_type, coalesce(e.actor_id, ''),
                  coalesce(e.entity_type, ''), coalesce(e.entity_id, ''),
                  to_char(e.created_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), e.payload::text
              ]) with ordinality as t(f, n))
    end
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
    new.canon_v    := 2;
    new.hash       := encode(sha256(convert_to(audit_canonical(new), 'UTF8')), 'hex');
    return new;
end $$;
