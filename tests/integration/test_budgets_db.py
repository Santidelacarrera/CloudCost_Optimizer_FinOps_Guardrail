"""Guardrail de presupuesto contra PostgreSQL real: bloqueo preventivo en escaneos, degradación, alertas, aislamiento y HTTP."""
from __future__ import annotations

from datetime import date

import pytest
from dbkit import admin, new_tenant, require_db, result
from fastapi import Request

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"


def _settings():
    from cloudcost.config import Settings
    return Settings(demo_enabled=True, aws_cost_explorer_resources=True, aws_ce_request_budget=60)     # 60 × 0,01 = USD 0,60 por escaneo


def _set_budget(t, **kw):
    admin("""insert into tenant_budgets (organization_id, metric, monthly_limit_usd, per_task_limit_usd, warn_ratio, mode, enabled, updated_by)
             values (%s, 'api_usage', %s, %s, %s, %s, true, 'test')""",
          (str(t.org), kw.get("limit", 100), kw.get("per_task"), kw.get("warn", 0.8), kw.get("mode", "alert")))


def _run(t, **kw):
    from cloudcost.secrets import SecretResolver
    from cloudcost.services import scan_service

    seen = {}

    class Scripted:
        def collect(self):
            return result()

    def factory(account, **fkw):
        seen.update(fkw)
        return Scripted()

    out = scan_service.run_scan(t.org, t.new_scan(), settings=kw.get("settings") or _settings(), secrets=SecretResolver(), collector_factory=factory)
    return out, seen


def _used(t):
    with t.tx() as conn:
        return float(conn.execute("select coalesce(sum(cost_usd),0) s from usage_meter").fetchone()["s"])


def test_scan_without_budget_runs_and_alerts_nothing():
    t = new_tenant("bud0")
    _run(t)
    with t.tx() as conn:
        assert conn.execute("select count(*) c from budget_alerts").fetchone()["c"] == 0


def test_block_mode_stops_the_scan_before_any_spend():
    from cloudcost.services.budgets import BudgetExceeded
    t = new_tenant("bud1")
    _set_budget(t, limit=0.5, mode="block")                         # un escaneo cuesta hasta USD 0,60
    scan_id = t.new_scan()
    from cloudcost.secrets import SecretResolver
    from cloudcost.services import scan_service
    called = []
    with pytest.raises(BudgetExceeded):
        scan_service.run_scan(t.org, scan_id, settings=_settings(), secrets=SecretResolver(),
                              collector_factory=lambda *a, **k: called.append(1))
    assert not called, "el colector no debe crearse si el presupuesto bloquea"
    assert _used(t) == 0
    with t.tx() as conn:
        a = conn.execute("select level from budget_alerts").fetchall()
    assert [x["level"] for x in a] == ["blocked"]
    assert t.audit("BUDGET_ALERT") and t.audit("BUDGET_EVALUATED")


def test_alert_mode_lets_the_scan_run_but_raises_alert():
    t = new_tenant("bud2")
    _set_budget(t, limit=0.5, mode="alert")
    out, seen = _run(t)
    assert seen["use_cost_explorer"] is True and "findings" in out
    with t.tx() as conn:
        assert [x["level"] for x in conn.execute("select level from budget_alerts").fetchall()] == ["exceeded"]


def test_degrade_mode_disables_cost_explorer():
    t = new_tenant("bud3")
    _set_budget(t, limit=0.5, mode="degrade")
    out, seen = _run(t)
    assert seen["use_cost_explorer"] is False
    with t.tx() as conn:
        assert [x["level"] for x in conn.execute("select level from budget_alerts").fetchall()] == ["degraded"]


def test_alert_is_deduplicated_per_level_and_period():
    t = new_tenant("bud4")
    _set_budget(t, limit=0.5, mode="alert")
    _run(t)
    _run(t)
    with t.tx() as conn:
        assert conn.execute("select count(*) c from budget_alerts").fetchone()["c"] == 1
    assert len(t.audit("BUDGET_ALERT")) == 1


def test_non_aws_scan_costs_nothing_and_is_never_blocked():
    from cloudcost.services import budgets
    assert budgets.scan_cost_estimate({"provider": "kubernetes"}, _settings()) == 0.0
    assert budgets.scan_cost_estimate({"provider": "aws"}, _settings()) == pytest.approx(0.6)


def test_usage_meter_is_append_only_and_tenant_scoped():
    import psycopg
    from cloudcost.services import budgets
    a, b = new_tenant("bud5a"), new_tenant("bud5b")
    with a.tx() as conn:
        budgets.record_usage(conn, a.org, "qdrant_query", 1.25, quantity=100)
    assert _used(a) == 1.25 and _used(b) == 0
    with b.tx() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("update usage_meter set cost_usd = 0")
    with b.tx() as conn:                                              # RLS: b no puede insertar filas de a
        with pytest.raises(psycopg.errors.Error):
            conn.execute("insert into usage_meter (organization_id, source, cost_usd, period) values (%s, 'x', 1, %s)", (str(a.org), date(2026, 10, 1)))


def test_cloud_spend_metric_uses_observed_costs():
    from cloudcost.services import budgets
    t = new_tenant("bud6")
    admin("""insert into account_costs (organization_id, cloud_account_id, provider, account_ref, service, service_raw, region, granularity,
             period_start, period_end, amount) values (%s, %s, 'aws', 'x', 'ec2', 'EC2', 'us-east-1', 'DAILY', %s, %s, 40)""",
          (str(t.org), t.account, date(2026, 10, 2), date(2026, 10, 3)))
    with t.tx() as conn:
        assert budgets.spent_this_month(conn, "cloud_spend", date(2026, 10, 10)) == 40.0
        assert budgets.spent_this_month(conn, "cloud_spend", date(2026, 11, 10)) == 0.0


# ------------------------------------------------------------------------------------------------- HTTP
@pytest.fixture(scope="module")
def client():
    """Cliente con la identidad inyectada por cabeceras de prueba (X-Test-Org / X-Test-Role): aquí se prueba el guardrail, no el login."""
    require_db()
    from uuid import UUID

    from cloudcost.main import app
    from cloudcost.security import Principal, get_principal
    from fastapi.testclient import TestClient

    def fake(request: Request):
        return Principal(user_id="test:budget", email="b@example.com", org_id=UUID(request.headers["x-test-org"]),
                         role=request.headers["x-test-role"])

    app.dependency_overrides[get_principal] = fake
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_principal, None)


def _token(client, role):
    t = new_tenant("budhttp")
    return t, {"X-Test-Org": str(t.org), "X-Test-Role": role}


def test_only_admin_sets_budgets_and_validation(client):
    t, finops = _token(client, "FINOPS")
    body = {"metric": "api_usage", "monthly_limit_usd": 50, "mode": "block"}
    assert client.put("/api/v1/budgets", json=body, headers=finops).status_code == 403
    t2, admin_h = _token(client, "ADMIN")
    assert client.put("/api/v1/budgets", json={**body, "mode": "nuke"}, headers=admin_h).status_code == 422
    assert client.put("/api/v1/budgets", json={**body, "monthly_limit_usd": -1}, headers=admin_h).status_code == 422
    assert client.put("/api/v1/budgets", json={**body, "extra": 1}, headers=admin_h).status_code == 422
    res = client.put("/api/v1/budgets", json=body, headers=admin_h)
    assert res.status_code == 200 and res.json()["items"][0]["mode"] == "block"
    assert t2.audit("BUDGET_UPDATED")


def test_check_and_usage_flow_blocks_when_over_limit(client):
    t, h = _token(client, "ADMIN")
    client.put("/api/v1/budgets", json={"metric": "api_usage", "monthly_limit_usd": 10, "mode": "block"}, headers=h)
    ok = client.post("/api/v1/budgets/check", json={"estimated_cost_usd": 4}, headers=h).json()
    assert ok["allowed"] is True
    assert client.post("/api/v1/budgets/usage", json={"source": "qdrant_query", "cost_usd": 9.5}, headers=h).status_code == 201
    no = client.post("/api/v1/budgets/check", json={"estimated_cost_usd": 1}, headers=h).json()
    assert no["allowed"] is False and no["level"] == "exceeded" and no["spent_usd"] == 9.5
    alerts = client.get("/api/v1/budgets/alerts", headers=h).json()["items"]
    assert any(a["level"] == "blocked" for a in alerts)
    assert client.post("/api/v1/budgets/usage", json={"source": "BAD SOURCE!", "cost_usd": 1}, headers=h).status_code == 422
    assert client.post("/api/v1/budgets/usage", json={"source": "x", "cost_usd": -1}, headers=h).status_code == 422


def test_other_org_sees_nothing(client):
    _, a = _token(client, "ADMIN")
    client.put("/api/v1/budgets", json={"metric": "api_usage", "monthly_limit_usd": 10, "mode": "block"}, headers=a)
    client.post("/api/v1/budgets/usage", json={"source": "x", "cost_usd": 3}, headers=a)
    _, b = _token(client, "ADMIN")
    assert client.get("/api/v1/budgets", headers=b).json()["items"] == []
    assert client.get("/api/v1/budgets/alerts", headers=b).json()["items"] == []
