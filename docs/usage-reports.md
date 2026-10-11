# Qdrant, PostgreSQL e informe automatizado de optimización

## Conectar (solo lectura)

Cuenta cloud con `provider=qdrant` o `provider=postgres` (`POST /cloud-accounts`):

* **Qdrant**: `qdrant_url` (https; http solo en red interna, sin credenciales en la URL) y `credentials_ref` (`env:CC_SECRET_…` o `aws-sm:…`) con la API key. Solo `GET /collections*`.
* **PostgreSQL**: `credentials_ref` con el DSN. Usa un rol con `pg_monitor` y sin permisos de escritura; la sesión se abre `read_only` con límites de tiempo, exige TLS fuera de redes internas y consulta solo catálogos fijos.
* Precios opcionales: `ram_gib_month_usd`, `disk_gib_month_usd`, `pg_storage_gib_month_usd`.

## Reglas

| Regla | Condición | Ahorro (fórmula en [savings-methodology.md](savings-methodology.md)) |
|---|---|---|
| `qdrant_quantization` | Colección con vectores en RAM ≥ 0,5 GiB sin cuantización | −75 % de RAM de vectores, menos el disco de los originales |
| `qdrant_replication_nonprod` | Réplicas ≥ 2 en desarrollo/test/staging | RAM de las réplicas sobrantes |
| `pg_unused_index` | Índice no único con 0 escaneos tras ≥ 7 días de estadísticas y ≥ 50 MiB | Almacenamiento del índice (solo si el volumen se reduce) |
| `pg_table_bloat` | ≥ 20 % de tuplas muertas y ≥ 100 MiB | Cota superior del espacio recuperable |

Son **consultivas** (`ADVISORY_ONLY`): quedan en estado PROPOSED, no se aprueban ni generan PR, y por tanto no entran en la medición de ahorro observado.

## Informe

```
export CC_SECRET_PG_DSN='postgresql://usuario:…@host/db'     # el valor nunca va en la línea de comandos
python -m cloudcost.cli usage-report --pg-dsn-env CC_SECRET_PG_DSN \
    --qdrant-url https://qdrant.interno:6333 --qdrant-key-env CC_SECRET_QDRANT_KEY \
    --environment production --budget-limit 500 --spent 320 --out-dir informe
```

Genera `usage-report.md` y `usage-report.json` (orden estable; con los mismos datos, el mismo contenido salvo la fecha). Incluye ahorro estimado total, hallazgos con SQL revisable, comparación opcional con presupuesto, cómo se validó cada fuente y los límites.

## Validación

Qdrant: solo respuestas simuladas (L1). PostgreSQL: PostgreSQL 16 real local (pruebas de integración). Ninguno probado contra servicios gestionados en producción.

---
Volver a [[empresa]]
