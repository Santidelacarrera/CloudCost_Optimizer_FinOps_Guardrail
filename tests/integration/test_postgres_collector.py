"""Colector de PostgreSQL contra una base REAL: índices sin uso, espacio muerto y, sobre todo, que la sesión es de solo lectura."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from cloudcost.collectors.postgres import PostgresCollector, PostgresConfigError, check_dsn
from cloudcost.domain.rules import RuleConfig, evaluate_all
from dbkit import require_db

SCHEMA = "ds_probe"


class Env:
    def resolve(self, ref, org_id=None):
        return os.environ["DATABASE_ADMIN_URL"] if ref == "env:CC_PG_PROBE" else None


@pytest.fixture(scope="module")
def probe():
    require_db()
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as c:
        c.execute(f"drop schema if exists {SCHEMA} cascade")
        c.execute(f"create schema {SCHEMA}")
        c.execute(f"create table {SCHEMA}.events (id bigserial primary key, kind int, payload text) with (autovacuum_enabled = false)")
        c.execute(f"insert into {SCHEMA}.events (kind, payload) select g % 50, md5(g::text) || repeat('x', 150) from generate_series(1, 150000) g")
        c.execute(f"create index events_kind_idx on {SCHEMA}.events (kind, payload)")             # nunca se usa
        c.execute(f"create index events_payload_idx on {SCHEMA}.events (payload)")               # nunca se usa (grande)
        c.execute(f"create unique index events_uniq on {SCHEMA}.events (id, kind)")              # único: se excluye aunque no se use
        c.execute(f"delete from {SCHEMA}.events where id % 2 = 0")                               # deja ~50 % de tuplas muertas
        c.execute("select pg_stat_force_next_flush()")
        c.execute(f"select count(*) from {SCHEMA}.events where id = 1")                          # usa la clave primaria: no es «sin uso»
    yield
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as c:
        c.execute(f"drop schema if exists {SCHEMA} cascade")


def collect(**cfg):
    account = {"provider": "postgres", "account_ref": "pg-main", "provider_config": {"credentials_ref": "env:CC_PG_PROBE", "environment": "development", **cfg}}
    # «ahora» = dentro de 30 días: la base de pruebas acaba de arrancar, así que simulamos una ventana de estadísticas suficiente
    return PostgresCollector(account, Env(), now=datetime.now(timezone.utc) + timedelta(days=30)).collect()


def ours(result):
    return [r for r in result.resources if SCHEMA in r.resource_id or r.service == "pg_database"]


def test_collects_database_unused_indexes_and_bloat_from_the_catalog_only(probe):
    result = collect()
    by_service = {s: [r for r in ours(result) if r.service == s] for s in ("pg_database", "pg_index", "pg_table")}
    assert len(by_service["pg_database"]) == 1 and by_service["pg_database"][0].attributes["size_bytes"] > 0
    names = {r.name for r in by_service["pg_index"]}
    assert f"{SCHEMA}.events_payload_idx" in names and f"{SCHEMA}.events_kind_idx" in names
    assert f"{SCHEMA}.events_uniq" not in names and f"{SCHEMA}.events_pkey" not in names                 # único / clave primaria: nunca candidatos
    table = next(r for r in by_service["pg_table"] if r.name == f"{SCHEMA}.events")
    assert table.attributes["n_dead_tup"] > 50_000 and table.attributes["heap_bytes"] > 10 * 2**20
    assert not result.partial and result.resources[0].observation_days >= 29


def test_rules_turn_them_into_advisory_findings_with_savings(probe):
    result = collect()
    cfg = RuleConfig(pg_min_index_mib=1, pg_bloat_min_mib=1, min_monthly_savings=0.0)      # la base de pruebas es pequeña: los importes son céntimos
    findings = {(f.rule_id, f.resource.name): f for f in evaluate_all(ours(result), cfg)}
    idx = findings[("pg_unused_index", f"{SCHEMA}.events_payload_idx")]
    assert idx.estimated_monthly_savings == round(idx.current_monthly_cost, 2) and idx.evidence["size_mib"] > 5 and idx.destructive and idx.params["sql"].startswith("DROP INDEX CONCURRENTLY") and idx.evidence["idx_scan"] == 0
    bloat = findings[("pg_table_bloat", f"{SCHEMA}.events")]
    assert 0 <= bloat.estimated_monthly_savings <= bloat.current_monthly_cost and bloat.evidence["dead_ratio"] > 0.3
    assert bloat.evidence["estimate"]["kind"] == "upper_bound"


def test_a_short_stats_window_blocks_the_unused_index_claim(probe):
    account = {"provider": "postgres", "account_ref": "pg-main", "provider_config": {"credentials_ref": "env:CC_PG_PROBE"}}
    result = PostgresCollector(account, Env()).collect()                                                 # «ahora» real: estadísticas de minutos
    assert result.resources[0].observation_days < 7
    assert [f for f in evaluate_all(ours(result), RuleConfig(pg_min_index_mib=1)) if f.rule_id == "pg_unused_index"] == []


def test_the_session_is_read_only_and_cannot_write(probe):
    account = {"provider": "postgres", "account_ref": "pg-main", "provider_config": {"credentials_ref": "env:CC_PG_PROBE"}}
    conn = PostgresCollector(account, Env())._open()
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(f"create table {SCHEMA}.should_not_exist (id int)")
        conn.rollback()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(f"delete from {SCHEMA}.events")
    finally:
        conn.rollback()
        conn.close()
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"]) as c:
        assert c.execute("select to_regclass(%s)", (f"{SCHEMA}.should_not_exist",)).fetchone()[0] is None


def test_connection_errors_never_leak_the_dsn_or_password():
    class Bad:
        def resolve(self, ref, org_id=None):
            return "postgresql://svc_user:Sup3rSecretPw@127.0.0.1:1/nodb"

    account = {"provider": "postgres", "account_ref": "pg-main", "provider_config": {"credentials_ref": "env:X"}}
    with pytest.raises(ConnectionError) as exc:
        PostgresCollector(account, Bad()).collect()
    text = str(exc.value) + repr(exc.value.__cause__)
    assert "Sup3rSecretPw" not in text and "svc_user" not in text


@pytest.mark.parametrize("dsn", ["", "mysql://u@h/db", "postgresql://u@169.254.169.254/db", "postgresql://u:p@db.example.com/app",
                                 "postgresql://u:p@db.example.com/app?sslmode=disable"])
def test_unsafe_dsns_are_rejected(dsn):
    with pytest.raises(PostgresConfigError):
        check_dsn(dsn)


@pytest.mark.parametrize("dsn", ["postgresql://u:p@db.example.com/app?sslmode=require", "postgresql://u:p@localhost/app", "postgresql://u:p@10.0.0.5/app", "postgresql://u:p@pg/app"])
def test_safe_dsns_are_accepted(dsn):
    assert check_dsn(dsn)
