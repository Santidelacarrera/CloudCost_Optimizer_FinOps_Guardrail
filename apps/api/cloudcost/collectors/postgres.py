"""Conector PostgreSQL de SOLO LECTURA: espacio y uso de índices y tablas desde el catálogo del sistema (`pg_stat_*`, `pg_index`).

Garantías de seguridad:
  · la sesión se abre con `default_transaction_read_only = on` y cada consulta corre en una transacción de solo lectura;
  · `statement_timeout` y `lock_timeout` acotan lo que puede tardar o bloquear;
  · las consultas son fijas (sin texto del cliente) y solo leen vistas del catálogo: no se lee ningún dato de las tablas del cliente;
  · el DSN (con la contraseña) llega por referencia a un secreto y se enmascara en errores y registros;
  · fuera de la red interna se exige TLS (`sslmode=require` o superior).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..domain.datastores import DEFAULT_PG_STORAGE_GIB_MONTH, GIB
from ..domain.models import NormalizedResource
from ..redaction import redact_text
from ..secrets import SecretResolver
from .base import CollectionResult
from .common import finish
from .netguard import DestinationError, check_host, is_private_or_internal

log = logging.getLogger(__name__)
MAX_ITEMS = 50
MIN_INDEX_BYTES = 10 * 2**20
MIN_TABLE_BYTES = 10 * 2**20
SAFE_SSL = {"require", "verify-ca", "verify-full"}
SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=20000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=30000"

# Consultas FIJAS: ninguna interpola texto; los umbrales van como parámetros.
Q_DB = """select pg_database_size(current_database()) as size_bytes, current_database() as name, version() as version,
                 (select stats_reset from pg_stat_database where datname = current_database()) as stats_reset,
                 pg_postmaster_start_time() as started,
                 (select setting::int from pg_settings where name = 'max_connections') as max_connections,
                 (select count(*) from pg_stat_activity where datname = current_database()) as connections,
                 (select case when blks_hit + blks_read > 0 then round(blks_hit::numeric / (blks_hit + blks_read), 4) end
                    from pg_stat_database where datname = current_database()) as cache_hit_ratio,
                 (select count(*) > 0 from pg_stat_replication) as has_replicas"""
Q_INDEXES = """select s.schemaname as schema, s.relname as table_name, s.indexrelname as index_name, s.idx_scan,
                      pg_relation_size(s.indexrelid) as size_bytes
                 from pg_stat_user_indexes s join pg_index i on i.indexrelid = s.indexrelid
                where s.idx_scan = 0 and i.indisvalid and not i.indisunique and not i.indisprimary and not i.indisexclusion
                  and not exists (select 1 from pg_constraint c where c.conindid = s.indexrelid)
                  and pg_relation_size(s.indexrelid) >= %(min_bytes)s
                order by size_bytes desc limit %(limit)s"""
Q_TABLES = """select schemaname as schema, relname as table_name, n_live_tup, n_dead_tup, pg_relation_size(relid) as heap_bytes,
                     pg_total_relation_size(relid) as total_bytes, last_autovacuum, last_vacuum
                from pg_stat_user_tables
               where n_dead_tup > 0 and pg_relation_size(relid) >= %(min_bytes)s
               order by n_dead_tup desc limit %(limit)s"""


class PostgresConfigError(PermissionError):
    pass


def check_dsn(dsn: str) -> str:
    """Valida el DSN (URL `postgresql://`): host permitido y TLS fuera de la red interna. Devuelve el host. No registra el DSN."""
    parsed = urlparse(dsn or "")
    if parsed.scheme not in ("postgresql", "postgres") or not parsed.hostname:
        raise PostgresConfigError("El DSN de PostgreSQL debe ser postgresql://usuario@host:puerto/base")
    try:
        host = check_host(parsed.hostname, "PostgreSQL")
    except DestinationError as exc:
        raise PostgresConfigError(str(exc)) from exc
    query = dict(p.split("=", 1) for p in (parsed.query or "").split("&") if "=" in p)
    if not is_private_or_internal(host) and query.get("sslmode", "prefer") not in SAFE_SSL:
        raise PostgresConfigError("Fuera de la red interna el DSN exige TLS: añade ?sslmode=require (o verify-full)")
    return host


class PostgresCollector:
    def __init__(self, account: dict[str, Any], secrets: SecretResolver, *, connect=None, on_api_error=None, now: datetime | None = None):
        self.account = account
        self.secrets = secrets
        self.cfg = account.get("provider_config") or {}
        self.cluster = account["account_ref"]
        self.on_api_error = on_api_error or (lambda api: None)
        self._connect = connect
        self._now = now or datetime.now(timezone.utc)
        self.warnings: list[str] = []
        self.partial = False
        self.price = float(self.cfg.get("pg_storage_gib_month_usd") or DEFAULT_PG_STORAGE_GIB_MONTH)

    def _dsn(self) -> str:
        ref = self.cfg.get("credentials_ref")
        dsn = self.secrets.resolve(ref, self.account.get("organization_id")) if ref else None
        if not dsn:
            raise PostgresConfigError("Falta el DSN de PostgreSQL (credentials_ref apunta a un secreto vacío o inexistente)")
        check_dsn(dsn)
        return dsn

    def _open(self):
        if self._connect is not None:
            return self._connect()
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self._dsn(), row_factory=dict_row, connect_timeout=10, options=SESSION_OPTIONS,
                               application_name="cloudcost-readonly", autocommit=False)

    def collect(self) -> CollectionResult:
        if self._connect is None:
            self._dsn()                                                   # configuración inválida: error claro antes de abrir nada
        try:
            conn = self._open()
        except PostgresConfigError:
            raise
        except Exception as exc:                                          # noqa: BLE001 — el mensaje de psycopg puede traer el host o el usuario
            self.on_api_error("postgres:connect")
            raise ConnectionError("No se pudo conectar a PostgreSQL: " + redact_text(type(exc).__name__)) from None
        resources: list[NormalizedResource] = []
        try:
            conn.read_only = True
            db = self._one(conn, Q_DB, "postgres:database")
            if db is None:
                return finish(CollectionResult(), [], self.warnings, True)
            stats_since = max((d for d in (db.get("stats_reset"), db.get("started")) if d), default=None)
            days = max(0, int((self._now - stats_since).total_seconds() // 86400)) if stats_since else 0
            name = db["name"]
            resources.append(self._database(name, db, days))
            for r in self._many(conn, Q_INDEXES, {"min_bytes": MIN_INDEX_BYTES, "limit": MAX_ITEMS}, "postgres:indexes"):
                resources.append(self._index(name, r, days, bool(db.get("has_replicas"))))
            for r in self._many(conn, Q_TABLES, {"min_bytes": MIN_TABLE_BYTES, "limit": MAX_ITEMS}, "postgres:tables"):
                resources.append(self._table(name, r, days))
        finally:
            try:
                conn.rollback()
            finally:
                conn.close()
        return finish(CollectionResult(), resources, self.warnings, self.partial)

    # ------------------------------------------------------------------ consultas
    def _one(self, conn, sql: str, api: str, params=None) -> dict[str, Any] | None:
        rows = self._many(conn, sql, params, api)
        return rows[0] if rows else None

    def _many(self, conn, sql: str, params, api: str) -> list[dict[str, Any]]:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        except Exception as exc:                                          # noqa: BLE001
            try:
                conn.rollback()
            except Exception:                                             # noqa: BLE001
                log.warning("postgres rollback failed api=%s", api)
            self.on_api_error(api)
            self.partial = True
            self.warnings.append(f"{api}: {type(exc).__name__}")
            log.warning("postgres query failed api=%s err=%s", api, type(exc).__name__)
            return []

    # ------------------------------------------------------------------ recursos
    def _env(self) -> str:
        return str(self.cfg.get("environment") or "unknown")

    def _database(self, name: str, db: dict[str, Any], days: int) -> NormalizedResource:
        size = int(db["size_bytes"])
        attrs = {"database": name, "version": str(db.get("version", ""))[:60], "size_bytes": size, "connections": db.get("connections"),
                 "max_connections": db.get("max_connections"), "cache_hit_ratio": float(db["cache_hit_ratio"]) if db.get("cache_hit_ratio") is not None else None,
                 "has_replicas": bool(db.get("has_replicas")), "stats_days": days, "pg_storage_gib_month_usd": self.price}
        return NormalizedResource("postgres", "database", "pg_database", f"{self.cluster}/{name}", "all", name=name, environment=self._env(), state="available",
                                  monthly_cost=round(size / GIB * self.price, 2), cost_source="estimate", size_gb=round(size / GIB, 4),
                                  observation_days=days, attributes=attrs)

    def _index(self, db: str, r: dict[str, Any], days: int, has_replicas: bool) -> NormalizedResource:
        size = int(r["size_bytes"])
        ident = f"{self.cluster}/{db}/{r['schema']}.{r['index_name']}"
        attrs = {"schema": r["schema"], "table": r["table_name"], "index": r["index_name"], "idx_scan": int(r["idx_scan"]), "backs_constraint": False,
                 "has_replicas": has_replicas, "pg_storage_gib_month_usd": self.price}
        return NormalizedResource("postgres", "database", "pg_index", ident, "all", name=f"{r['schema']}.{r['index_name']}", environment=self._env(),
                                  state="available", monthly_cost=round(size / GIB * self.price, 2), cost_source="estimate", size_gb=round(size / GIB, 4),
                                  observation_days=days, attributes=attrs)

    def _table(self, db: str, r: dict[str, Any], days: int) -> NormalizedResource:
        total = int(r["total_bytes"])
        ident = f"{self.cluster}/{db}/{r['schema']}.{r['table_name']}"
        la = r.get("last_autovacuum") or r.get("last_vacuum")
        attrs = {"schema": r["schema"], "table": r["table_name"], "n_live_tup": int(r["n_live_tup"]), "n_dead_tup": int(r["n_dead_tup"]),
                 "heap_bytes": int(r["heap_bytes"]), "total_bytes": total, "last_autovacuum": la.isoformat() if la else None,
                 "pg_storage_gib_month_usd": self.price}
        return NormalizedResource("postgres", "database", "pg_table", ident, "all", name=f"{r['schema']}.{r['table_name']}", environment=self._env(),
                                  state="available", monthly_cost=round(total / GIB * self.price, 2), cost_source="estimate", size_gb=round(total / GIB, 4),
                                  observation_days=days, attributes=attrs)
