"""Aislamiento entre organizaciones a nivel de base de datos (RLS). Pensado como red de seguridad permanente y como material para el
pentest externo (docs/pentest/): descubre las tablas por introspección, así que una tabla nueva SIN RLS hace fallar esta prueba.

Requiere DATABASE_URL (rol de aplicación) y DATABASE_ADMIN_URL (propietario); si no, se omite.
"""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

import pytest

# Tablas que no son de tenant y por eso no llevan RLS. Cualquier otra tabla del esquema público debe tenerla activa y forzada.
NO_RLS_ALLOWLIST = {"schema_migrations"}


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def _admin(sql: str, params=()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture(scope="module")
def tenants():
    """Dos organizaciones con una cuenta de nube, un repositorio y una política cada una."""
    _require_db()
    out = {}
    for name in ("a", "b"):
        org = str(uuid4())
        _admin("insert into organizations (id, name, slug) values (%s, %s, %s)", (org, f"Aislamiento {name}", f"iso-{name}-{org[:8]}"))
        acc = _admin("insert into cloud_accounts (organization_id, provider, account_ref, display_name) values (%s, 'aws', %s, %s) returning id",
                     (org, f"1111111111{name}", f"Cuenta {name}"))[0]["id"]
        repo = _admin("insert into repositories (organization_id, provider, full_name) values (%s, 'github', %s) returning id", (org, f"acme/{name}"))[0]["id"]
        pol = _admin("insert into policies (organization_id, name, kind) values (%s, %s, 'guardrail') returning id", (org, f"pol-{name}"))[0]["id"]
        out[name] = {"org": org, "account": str(acc), "repo": str(repo), "policy": str(pol)}
    return out


TENANT_TABLES = ("cloud_accounts", "repositories", "policies")


# ----------------------------------------------------------------------------------------------------------- estructura
def test_every_table_has_forced_rls_and_at_least_one_policy():
    _require_db()
    rows = _admin("""
        select c.relname, c.relrowsecurity as rls, c.relforcerowsecurity as forced,
               (select count(*)::int from pg_policies p where p.schemaname = 'public' and p.tablename = c.relname) as policies
          from pg_class c join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind in ('r', 'p') order by 1""")
    assert len(rows) > 10, "la introspección no encontró las tablas esperadas"
    bad = [r["relname"] for r in rows if r["relname"] not in NO_RLS_ALLOWLIST and not (r["rls"] and r["forced"] and r["policies"] >= 1)]
    assert not bad, f"tablas sin RLS activa, forzada y con política: {bad}"


def test_tables_with_organization_id_use_it_in_their_policy():
    """Toda tabla con organization_id debe filtrar por current_org() o por el contexto de autenticación, nunca ser permisiva."""
    _require_db()
    rows = _admin("""
        select p.tablename, p.policyname, coalesce(p.qual, '') as qual, coalesce(p.with_check, '') as with_check
          from pg_policies p
         where p.schemaname = 'public'
           and exists (select 1 from information_schema.columns c where c.table_schema = 'public' and c.table_name = p.tablename and c.column_name = 'organization_id')""")
    assert rows
    permissive = [(r["tablename"], r["policyname"]) for r in rows
                  if not (("current_org" in r["qual"] or "auth_context" in r["qual"]) and ("current_org" in r["with_check"] or "auth_context" in r["with_check"]))]
    assert not permissive, f"políticas que no restringen por organización: {permissive}"


def test_application_role_is_unprivileged():
    _require_db()
    from cloudcost.db import init_pool
    with init_pool().connection() as conn:
        role = conn.execute("select r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb from pg_roles r where r.rolname = current_user").fetchone()
        owned = conn.execute("select count(*)::int as n from pg_tables where schemaname = 'public' and tableowner = current_user").fetchone()["n"]
    assert not role["rolsuper"] and not role["rolbypassrls"] and not role["rolcreaterole"] and not role["rolcreatedb"]
    assert owned == 0, "el rol de aplicación no debe ser dueño de tablas (el dueño puede saltarse RLS si no está forzada)"


# ----------------------------------------------------------------------------------------------------------- datos entre tenants
def test_a_tenant_only_sees_its_own_rows(tenants):
    from cloudcost.db import tenant_tx
    a, b = tenants["a"], tenants["b"]
    with tenant_tx(a["org"]) as conn:
        for table in TENANT_TABLES:
            orgs = {str(r["organization_id"]) for r in conn.execute(f"select organization_id from {table}").fetchall()}   # noqa: S608 — tabla fija
            assert orgs == {a["org"]}, f"{table} filtra datos de otras organizaciones: {orgs - {a['org']}}"
        assert conn.execute("select count(*)::int as n from cloud_accounts where id = %s", (b["account"],)).fetchone()["n"] == 0
        assert [str(r["id"]) for r in conn.execute("select id from organizations").fetchall()] == [a["org"]]


def test_a_tenant_cannot_modify_or_create_rows_in_another(tenants):
    import psycopg
    from cloudcost.db import tenant_tx
    a, b = tenants["a"], tenants["b"]
    with tenant_tx(a["org"]) as conn:
        cur = conn.execute("update cloud_accounts set display_name = 'hackeada' where id = %s", (b["account"],))
        assert cur.rowcount == 0
        cur = conn.execute("update policies set enabled = false where id = %s", (b["policy"],))
        assert cur.rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):                                 # WITH CHECK: no se puede escribir en otro tenant
        with tenant_tx(a["org"]) as conn:
            conn.execute("insert into repositories (organization_id, provider, full_name) values (%s, 'github', 'intruso/repo')", (b["org"],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):                                 # ni mover una fila propia a otro tenant
        with tenant_tx(a["org"]) as conn:
            conn.execute("update repositories set organization_id = %s where id = %s", (b["org"], a["repo"]))
    assert _admin("select display_name from cloud_accounts where id = %s", (b["account"],))[0]["display_name"] == "Cuenta b"
    assert _admin("select enabled from policies where id = %s", (b["policy"],))[0]["enabled"] is True
    assert _admin("select count(*)::int as n from repositories where full_name = 'intruso/repo'")[0]["n"] == 0


def test_without_tenant_context_nothing_is_visible(tenants):
    """Un olvido de `set_config` en el código no debe filtrar datos: sin contexto, cero filas en TODAS las tablas legibles."""
    from cloudcost.db import init_pool
    with init_pool().connection() as conn:
        with conn.transaction():
            names = [r["relname"] for r in conn.execute(
                """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = 'public' and c.relkind in ('r','p') and has_table_privilege(current_user, c.oid, 'select')""").fetchall()]
            assert "cloud_accounts" in names
            leaks = {t: n for t in names if t not in NO_RLS_ALLOWLIST
                     for n in [conn.execute(f'select count(*)::int as n from "{t}"').fetchone()["n"]] if n}      # noqa: S608 — nombres del catálogo
    assert not leaks, f"tablas que devuelven filas sin contexto de organización: {leaks}"


def test_a_malformed_tenant_context_fails_closed(tenants):
    import psycopg
    from cloudcost.db import init_pool
    with pytest.raises(psycopg.Error):
        with init_pool().connection() as conn:
            with conn.transaction():
                conn.execute("select set_config('app.current_org', %s, true)", (tenants["a"]["org"] + "' or '1'='1",))
                conn.execute("select count(*) from cloud_accounts")


def test_tenant_context_does_not_leak_between_transactions(tenants):
    """is_local=true: el contexto muere con la transacción; la conexión reutilizada del pool no hereda al tenant anterior."""
    from cloudcost.db import init_pool, tenant_tx
    with tenant_tx(tenants["a"]["org"]) as conn:
        assert conn.execute("select count(*)::int as n from cloud_accounts").fetchone()["n"] >= 1
    with init_pool().connection() as conn:
        with conn.transaction():
            assert conn.execute("select count(*)::int as n from cloud_accounts").fetchone()["n"] == 0


# ----------------------------------------------------------------------------------------------------------- auditoría
def test_audit_log_is_append_only_for_the_application_role(tenants):
    import psycopg
    from cloudcost.db import tenant_tx
    from cloudcost.services import audit
    a = tenants["a"]
    with tenant_tx(a["org"]) as conn:
        audit.record(conn, a["org"], "ISOLATION_TEST", entity_type="test", entity_id="1", payload={"n": 1})
        audit.record(conn, a["org"], "ISOLATION_TEST", entity_type="test", entity_id="2", payload={"n": 2})
    for sql in ("update audit_events set event_type = 'X'", "delete from audit_events", "truncate audit_events"):
        with pytest.raises(psycopg.Error):
            with tenant_tx(a["org"]) as conn:
                conn.execute(sql)
    with tenant_tx(a["org"]) as conn:
        chain = conn.execute("select * from verify_audit_chain()").fetchone()
        mine = conn.execute("select count(*)::int as n from audit_events").fetchone()["n"]
    assert chain["ok"] is True and chain["checked"] == mine and mine >= 2


def test_audit_events_of_other_tenants_are_invisible_and_not_forgeable(tenants):
    import psycopg
    from cloudcost.db import tenant_tx
    from cloudcost.services import audit
    a, b = tenants["a"], tenants["b"]
    with tenant_tx(b["org"]) as conn:
        audit.record(conn, b["org"], "ISOLATION_TEST_B", payload={})
    with tenant_tx(a["org"]) as conn:
        assert conn.execute("select count(*)::int as n from audit_events where organization_id = %s", (b["org"],)).fetchone()["n"] == 0
    with pytest.raises(psycopg.Error):                                    # tampoco puede escribir un evento «a nombre de» otra organización
        with tenant_tx(a["org"]) as conn:
            audit.record(conn, b["org"], "FORGED", payload={})
