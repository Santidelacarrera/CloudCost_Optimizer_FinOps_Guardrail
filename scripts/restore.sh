#!/bin/sh
# Restaura una copia de scripts/backup.sh en una base de datos VACÍA.
#
#   restore.sh <archivo.dump> <url-admin-de-la-base-destino> [--force]
#
# Variables:
#   APP_DB_PASSWORD   contraseña del rol cloudcost_app (se crea si no existe; las copias conservan sus permisos, no el rol)
#
# Por seguridad se niega a escribir sobre una base que ya tiene tablas, salvo --force.
set -eu

dump="${1:?uso: restore.sh <archivo.dump> <url-admin-destino> [--force]}"
target="${2:?falta la URL de la base destino}"
force="${3:-}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD es obligatorio}"

[ -f "$dump" ] || { echo "restore: no existe $dump" >&2; exit 1; }
if [ -f "$dump.sha256" ]; then
    ( cd "$(dirname "$dump")" && sha256sum -c "$(basename "$dump").sha256" > /dev/null ) \
        || { echo "restore: la suma de verificación no coincide, la copia está dañada" >&2; exit 1; }
fi

tables=$(psql "$target" -tAqc "select count(*) from information_schema.tables where table_schema = 'public'")
if [ "$tables" != "0" ] && [ "$force" != "--force" ]; then
    echo "restore: la base destino ya tiene $tables tablas. Usa una base vacía o --force." >&2
    exit 1
fi

psql "$target" -v ON_ERROR_STOP=1 -q -v app_pw="$APP_DB_PASSWORD" <<'SQL'
select format('create role cloudcost_app login nosuperuser nobypassrls password %L', :'app_pw')
where not exists (select from pg_roles where rolname = 'cloudcost_app') \gexec
SQL

if [ "$force" = "--force" ]; then
    pg_restore --clean --if-exists --no-owner --exit-on-error --single-transaction --dbname="$target" "$dump"
else
    pg_restore --no-owner --exit-on-error --single-transaction --dbname="$target" "$dump"
fi
echo "restore: listo ($dump)"
