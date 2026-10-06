#!/bin/sh
# Aplica las migraciones SQL pendientes y crea/actualiza el rol de aplicación.
#
# Variables:
#   DATABASE_ADMIN_URL  conexión con un usuario propietario del esquema (NO la de la app)
#   APP_DB_PASSWORD     contraseña del rol cloudcost_app (sin superusuario, sin BYPASSRLS)
#   MIGRATIONS_DIR      (opcional) por defecto ../migrations
#   SEED_DEV=true       (opcional) aplica migrations/seed/dev_seed.sql — solo desarrollo
set -eu

: "${DATABASE_ADMIN_URL:?DATABASE_ADMIN_URL es obligatorio}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD es obligatorio}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-$(dirname "$0")/../migrations}"

psql "$DATABASE_ADMIN_URL" -v ON_ERROR_STOP=1 -q -v app_pw="$APP_DB_PASSWORD" <<'SQL'
select format('create role cloudcost_app login nosuperuser nobypassrls password %L', :'app_pw')
where not exists (select from pg_roles where rolname = 'cloudcost_app') \gexec
select format('alter role cloudcost_app password %L', :'app_pw') \gexec
create table if not exists schema_migrations (
    version text primary key,
    applied_at timestamptz not null default now()
);
SQL

for f in "$MIGRATIONS_DIR"/*.sql; do
    v=$(basename "$f")
    applied=$(psql "$DATABASE_ADMIN_URL" -tAqc "select 1 from schema_migrations where version = '$v'")
    if [ -z "$applied" ]; then
        echo "migrate: aplicando $v"
        psql "$DATABASE_ADMIN_URL" -v ON_ERROR_STOP=1 -q --single-transaction \
            -f "$f" -c "insert into schema_migrations (version) values ('$v')"
    else
        echo "migrate: $v ya aplicada"
    fi
done

if [ "${SEED_DEV:-false}" = "true" ]; then
    echo "migrate: aplicando datos de desarrollo"
    psql "$DATABASE_ADMIN_URL" -v ON_ERROR_STOP=1 -q -f "$MIGRATIONS_DIR/seed/dev_seed.sql"
fi
echo "migrate: listo"
