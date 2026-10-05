"""Acceso a PostgreSQL. TODA operación de negocio corre dentro de `tenant_tx`, que fija el contexto de RLS."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=True,
                               kwargs={"row_factory": dict_row}, name="cloudcost")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tenant_tx(org_id: UUID | str) -> Iterator[Connection]:
    """Transacción con `app.current_org` fijado (is_local=true: solo vive durante la transacción).

    Si el bloque lanza una excepción se hace rollback de todo (incluidos los eventos de auditoría de esa operación).
    """
    pool = init_pool()
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute("select set_config('app.current_org', %s, true)", (str(org_id),))
            yield conn


def ping() -> bool:
    with init_pool().connection() as conn:
        return conn.execute("select 1 as ok").fetchone()["ok"] == 1
