#!/bin/sh
# Copia de seguridad lógica de Postgres (pg_dump, formato custom comprimido) con suma de verificación, poda y subida opcional a S3.
#
# Variables:
#   DATABASE_ADMIN_URL      conexión de un rol que se salte RLS (superusuario o BYPASSRLS): las tablas usan FORCE ROW LEVEL SECURITY,
#                           y con un rol sin ese permiso pg_dump falla en vez de guardar datos incompletos.
#   BACKUP_DIR              (opcional) destino local, por defecto /backups
#   BACKUP_RETENTION_DAYS   (opcional) días que se conservan las copias locales, por defecto 14
#   BACKUP_S3_URI           (opcional) p. ej. s3://mi-bucket/cloudcost  (requiere aws cli y credenciales en el entorno o rol de instancia)
set -eu

: "${DATABASE_ADMIN_URL:?DATABASE_ADMIN_URL es obligatorio}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
ts=$(date -u +%Y%m%dT%H%M%SZ)
name="cloudcost-$ts.dump"

mkdir -p "$BACKUP_DIR"
umask 077
partial="$BACKUP_DIR/.$name.partial"
trap 'rm -f "$partial"' EXIT

echo "backup: volcando a $name"
pg_dump --format=custom --compress=6 --no-owner --dbname="$DATABASE_ADMIN_URL" --file="$partial"

# Un archivo ilegible no sirve de copia: se comprueba que pg_restore puede leer su índice antes de aceptarlo.
pg_restore --list "$partial" > /dev/null
mv "$partial" "$BACKUP_DIR/$name"
( cd "$BACKUP_DIR" && sha256sum "$name" > "$name.sha256" )
size=$(wc -c < "$BACKUP_DIR/$name")
echo "backup: ok $name ($size bytes)"

if [ -n "${BACKUP_S3_URI:-}" ]; then
    echo "backup: subiendo a $BACKUP_S3_URI"
    aws s3 cp --only-show-errors "$BACKUP_DIR/$name" "$BACKUP_S3_URI/$name"
    aws s3 cp --only-show-errors "$BACKUP_DIR/$name.sha256" "$BACKUP_S3_URI/$name.sha256"
fi

# Poda local (la retención del bucket se configura con una regla de ciclo de vida en S3).
find "$BACKUP_DIR" -maxdepth 1 -name 'cloudcost-*.dump*' -mtime "+$RETENTION_DAYS" -delete

date +%s > "$BACKUP_DIR/last_success"           # lo lee el healthcheck del contenedor
echo "$BACKUP_DIR/$name"
