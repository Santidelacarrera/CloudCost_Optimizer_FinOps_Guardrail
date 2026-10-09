"""Utilidades para pruebas de integración contra PostgreSQL real: una organización nueva por prueba (no dependen del orden ni del seed)."""
from __future__ import annotations

import os
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

from cloudcost.collectors.base import CollectionResult, CostRecord
from cloudcost.domain.models import NormalizedResource


def require_db() -> None:
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def admin(sql: str, params=()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@dataclass
class Tenant:
    org: UUID
    account: str
    name: str

    def tx(self):
        from cloudcost.db import tenant_tx

        return tenant_tx(self.org)

    def principal(self, role: str = "FINOPS", name: str | None = None):
        from cloudcost.security import Principal

        name = name or f"{role.lower()}-{uuid4().hex[:6]}"
        return Principal(user_id=f"test:{name}", email=f"{name}@example.com", org_id=self.org, role=role)

    def new_scan(self, repo_id: str | None = None) -> UUID:
        with self.tx() as conn:
            return conn.execute("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) values (%s, %s, %s, 'test') returning id",
                                (str(self.org), self.account, repo_id)).fetchone()["id"]

    def add_repository(self, provider: str = "github", full_name: str = "acme/infra", token_ref: str | None = None) -> str:
        return str(admin("insert into repositories (organization_id, provider, full_name, token_ref) values (%s, %s, %s, %s) returning id",
                         (str(self.org), provider, full_name, token_ref))[0]["id"])

    def scan(self, result: CollectionResult, *, repo_id: str | None = None, git=None) -> dict:
        """Ejecuta un escaneo completo con un colector guionizado (sin red). Con `repo_id` y `git` también lee el IaC del proveedor indicado."""
        from cloudcost.config import Settings
        from cloudcost.secrets import SecretResolver
        from cloudcost.services import scan_service

        class Scripted:
            def collect(self_inner):
                return result

        return scan_service.run_scan(self.org, self.new_scan(repo_id), settings=Settings(demo_enabled=True), secrets=SecretResolver(),
                                     git_factory=(lambda *a, **k: git) if git else scan_service.get_git_provider,
                                     collector_factory=lambda account, **kw: Scripted())

    def recs(self, **where) -> list[dict]:
        with self.tx() as conn:
            rows = conn.execute("select r.*, res.resource_id as rid from recommendations r join resources res on res.id = r.resource_pk "
                                "order by r.created_at").fetchall()
        return [r for r in rows if all(r[k] == v for k, v in where.items())]

    def rec(self, rid: str, rule_id: str | None = None) -> dict:
        (match,) = [r for r in self.recs() if r["rid"] == rid and (rule_id is None or r["rule_id"] == rule_id)]
        return match

    def audit(self, event_type: str, entity_id=None) -> list[dict]:
        with self.tx() as conn:
            rows = conn.execute("select * from audit_events where event_type = %s order by seq", (event_type,)).fetchall()
        return [r for r in rows if entity_id is None or r["entity_id"] == str(entity_id)]


def new_tenant(label: str = "t") -> Tenant:
    require_db()
    org = uuid4()
    admin("insert into organizations (id, name, slug) values (%s, %s, %s)", (str(org), f"Prueba {label}", f"{label}-{org.hex[:10]}"))
    acc = admin("insert into cloud_accounts (organization_id, provider, account_ref, display_name) values (%s, 'aws', %s, %s) returning id",
                (str(org), "111122223333", f"Cuenta {label}"))[0]["id"]
    return Tenant(org, str(acc), label)


# --------------------------------------------------------------------------- recursos y resultados guionizados
def basis(source="cost_explorer", flags=None) -> dict:
    return {"cost_basis": {"source": source, "window_days": 14, "window_start": "2026-09-24", "window_end": "2026-10-08",
                           "days_with_data": 14, "quality_flags": flags or [], "as_of": "2026-10-08"}}


def instance(rid="i-0aaa", *, cost=280.32, cpu=12.4, mem=19.8, peak=30.0, env="staging", itype="m5.2xlarge", source="cost_explorer", flags=None,
             **kw) -> NormalizedResource:
    base = dict(provider="aws", resource_type="compute", service="ec2", resource_id=rid, region="us-east-1", name=rid, state="running",
                instance_type=itype, cpu_avg=cpu, cpu_max=peak, memory_avg=mem, observation_days=30, environment=env, monthly_cost=cost,
                cost_source=source, age_days=200, tags={"Name": rid}, attributes=basis(source, flags))
    base.update(kw)
    return NormalizedResource(**base)


def idle_instance(rid="i-0aaa", **kw) -> NormalizedResource:
    return instance(rid, cpu=1.0, mem=6.0, peak=4.0, **kw)


def result(*resources: NormalizedResource, costs: list[CostRecord] | None = None, partial=False, issues=None, account_costs=None) -> CollectionResult:
    return CollectionResult(resources=list(resources), costs=costs or [], partial=partial, issues=issues or [], account_costs=account_costs or [])


def daily_costs(rid: str, service: str, start: date, days: int, amount, *, source="cost_explorer") -> list[CostRecord]:
    """`amount` puede ser un número o una función día→importe."""
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        out.append(CostRecord(rid, d, float(amount(d) if callable(amount) else amount), service, "us-east-1", source))
    return out
