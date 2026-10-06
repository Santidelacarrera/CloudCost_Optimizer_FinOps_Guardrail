#!/bin/sh
# Prueba que una copia se puede restaurar: la carga en una base temporal, compara su estructura con la base viva, comprueba la cadena
# de auditoría de cada organización y borra la base temporal. Una copia que nunca se restauró es solo una esperanza.
#
#   verify_backup.sh [archivo.dump]        (sin argumento, la copia más reciente de BACKUP_DIR)
#
# Variables: DATABASE_ADMIN_URL, APP_DB_PASSWORD, BACKUP_DIR (por defecto /backups).
set -eu

: "${DATABASE_ADMIN_URL:?DATABASE_ADMIN_URL es obligatorio}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD es obligatorio}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
dump="${1:-$(ls -1t "$BACKUP_DIR"/cloudcost-*.dump 2>/dev/null | head -n 1)}"
[ -n "$dump" ] && [ -f "$dump" ] || { echo "verify: no hay copias que verificar" >&2; exit 1; }

with_db() { case "$1" in *\?*) echo "$1&dbname=$2";; *) echo "$1?dbname=$2";; esac; }
scratch="cloudcost_verify_$(date +%s)_$$"
scratch_url=$(with_db "$DATABASE_ADMIN_URL" "$scratch")
cleanup() { psql "$DATABASE_ADMIN_URL" -qc "drop database if exists \"$scratch\" with (force)" > /dev/null 2>&1 || true; }
trap cleanup EXIT

psql "$DATABASE_ADMIN_URL" -v ON_ERROR_STOP=1 -qc "create database \"$scratch\""
sh "$(dirname "$0")/restore.sh" "$dump" "$scratch_url" > /dev/null

tables() { psql "$1" -tAqc "select table_name from information_schema.tables where table_schema = 'public' order by 1"; }
live=$(tables "$DATABASE_ADMIN_URL")
restored=$(tables "$scratch_url")
if [ "$live" != "$restored" ]; then
    echo "verify: las tablas de la copia no coinciden con la base viva" >&2
    echo "viva:      $(echo "$live" | tr '\n' ' ')" >&2
    echo "restaurada: $(echo "$restored" | tr '\n' ' ')" >&2
    exit 1
fi
n_tables=$(echo "$restored" | wc -l | tr -d ' ')

# La cadena de auditoría se recorre por organización (verify_audit_chain trabaja sobre la organización activa).
if ! psql "$scratch_url" -v ON_ERROR_STOP=1 -q > /dev/null <<'SQL'
do $$
declare o record; r record; bad text := '';
begin
    for o in select id from organizations loop
        perform set_config('app.current_org', o.id::text, true);
        select * into r from verify_audit_chain();
        if not r.ok then bad := bad || o.id::text || ' (eslabón ' || r.first_bad_seq || ') '; end if;
    end loop;
    if bad <> '' then raise exception 'cadena de auditoría rota en: %', bad; end if;
end $$;
SQL
then
    echo "verify: la cadena de auditoría de la copia no es íntegra" >&2
    exit 1
fi
audit=$(psql "$scratch_url" -tAqc "select count(*) from audit_events")
orgs=$(psql "$scratch_url" -tAqc "select count(*) from organizations")
echo "verify: ok $(basename "$dump") · $n_tables tablas · $orgs organizaciones · $audit eventos de auditoría con la cadena íntegra"
