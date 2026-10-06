#!/bin/sh
# Bucle del contenedor "backup": copia, verifica y espera. Si una vuelta falla, lo dice en el log y reintenta en la siguiente.
set -u
dir="$(dirname "$0")"
interval=$(( ${BACKUP_INTERVAL_HOURS:-24} * 3600 ))
sleep "${BACKUP_START_DELAY_SECONDS:-60}"
while true; do
    if sh "$dir/backup.sh"; then
        ok=true
        if [ "${BACKUP_VERIFY:-true}" = "true" ]; then
            sh "$dir/verify_backup.sh" || { ok=false; echo "verify: FALLÓ la verificación de la última copia" >&2; }
        fi
        # Aviso "estoy vivo" a un monitor externo (healthchecks.io, Uptime Kuma...): si deja de llegar, el monitor avisa.
        if [ "$ok" = "true" ] && [ -n "${BACKUP_PING_URL:-}" ]; then wget -q -T 15 -O /dev/null "$BACKUP_PING_URL" || true; fi
    else
        echo "backup: FALLÓ la copia" >&2
    fi
    sleep "$interval"
done
