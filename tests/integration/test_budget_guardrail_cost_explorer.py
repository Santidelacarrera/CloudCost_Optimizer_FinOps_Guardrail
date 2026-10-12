"""Guardrails de presupuesto con datos SIMULADOS de Cost Explorer (sin llamadas reales a AWS) contra PostgreSQL real.

Demuestra, de punta a punta con el colector real de AWS sobre respuestas validadas por botocore:
  · cuánto cuesta un escaneo y cómo se mide (solicitudes reales × USD 0,01);
  · los umbrales exactos de aviso / previsión / superado / bloqueo, incluido el límite al centavo;
  · que el guardrail SOLO alerta, degrada o impide ARRANCAR una tarea: nunca borra datos, recursos ni nada en la nube.
Cálculos y umbrales: docs/budget-guardrails.md.
"""
from __future__ import annotations

import pytest
from aws_stub import TODAY
from ce_scenarios import CE_REQUEST_USD, CE_REQUESTS_PER_SCAN, DAILY_ACCOUNT_SPEND, aws_factory, happy_session
from dbkit import admin, new_tenant

SCAN_ESTIMATE = 0.60                                    # 60 solicitudes máximas × USD 0,01 (AWS_CE_REQUEST_BUDGET por defecto)
SCAN_ACTUAL = CE_REQUESTS_PER_SCAN * CE_REQUEST_USD     # 4 × 0,01 = 0,04 realmente gastado


def _settings():
    from cloudcost.config import Settings
    return Settings(demo_enabled=True, aws_cost_explorer_resources=True, aws_ce_request_budget=60)


def _budget(t, *, limit, mode="alert", per_task=None, metric="api_usage", warn=0.8):
    admin("""insert into tenant_budgets (organization_id, metric, monthly_limit_usd, per_task_limit_usd, warn_ratio, mode, enabled, updated_by)
             values (%s, %s, %s, %s, %s, %s, true, 'test')""", (str(t.org), metric, limit, per_task, warn, mode))


def _scan(t, session):
    from cloudcost.secrets import SecretResolver
    from cloudcost.services import scan_service
    factory = aws_factory(session)
    out = scan_service.run_scan(t.org, t.new_scan(), settings=_settings(), secrets=SecretResolver(), collector_factory=factory)
    return out, factory


def _ce_calls(session):
    return [c for c in session.calls if c[0] == "ce"]


def _counts(t):
    with t.tx() as conn:
        return {tbl: conn.execute(f"select count(*) c from {tbl}").fetchone()["c"]                       # noqa: S608 — nombres fijos
                for tbl in ("resources", "recommendations", "account_costs", "usage_meter", "tenant_budgets", "audit_events")}


def _used(t):
    with t.tx() as conn:
        return float(conn.execute("select coalesce(sum(cost_usd),0) s from usage_meter").fetchone()["s"])


def _levels(t):
    with t.tx() as conn:
        return sorted(r["level"] for r in conn.execute("select level from budget_alerts").fetchall())


# ----------------------------------------------------------------------------------------------- medición
def test_a_scan_measures_real_cost_explorer_requests_not_the_estimate():
    t = new_tenant("ce1")
    s = happy_session()
    out, factory = _scan(t, s)
    s.assert_all_consumed()
    assert len(_ce_calls(s)) == CE_REQUESTS_PER_SCAN == factory.built[0].budget.used
    assert _used(t) == pytest.approx(SCAN_ACTUAL)                           # 0,04, no los 0,60 del peor caso
    assert out["account_cost_rows"] > 0


def test_usage_accumulates_across_scans_within_the_month():
    t = new_tenant("ce2")
    for _ in range(3):
        _scan(t, happy_session())
    assert _used(t) == pytest.approx(3 * SCAN_ACTUAL)


# ----------------------------------------------------------------------------------------------- umbrales exactos
@pytest.mark.parametrize("limit,expected_level,runs", [
    (100.0, None, True),            # 0,60 / 100 = 0,6 % → sin alerta
    (0.70, "warn", True),           # 0,60 ≥ 80 % de 0,70 (0,56) y ≤ límite → aviso
    (0.60, "warn", True),           # exactamente el límite: se permite (la comparación es estricta: proyectado > límite)
    (0.59, "exceeded", True),       # un centavo menos: superado; en modo alert la tarea sigue
])
def test_alert_mode_thresholds_for_the_first_scan(limit, expected_level, runs):
    t = new_tenant("ce3")
    _budget(t, limit=limit, mode="alert")
    s = happy_session()
    _scan(t, s)
    assert len(_ce_calls(s)) == CE_REQUESTS_PER_SCAN                         # alertar nunca impide ni recorta la tarea
    assert _levels(t) == ([expected_level] if expected_level else [])


@pytest.mark.parametrize("limit,blocked", [(0.59, True), (0.60, False)])
def test_block_boundary_is_exact_to_the_cent(limit, blocked):
    from cloudcost.services.budgets import BudgetExceeded
    t = new_tenant("ce4")
    _budget(t, limit=limit, mode="block")
    s = happy_session()
    if blocked:
        with pytest.raises(BudgetExceeded):
            _scan(t, s)
        assert s.calls == []                                                 # ni una sola llamada a AWS
    else:
        _scan(t, s)
        assert len(_ce_calls(s)) == CE_REQUESTS_PER_SCAN


def test_second_scan_is_blocked_when_the_month_budget_is_nearly_used():
    from cloudcost.services.budgets import BudgetExceeded
    t = new_tenant("ce5")
    _scan(t, happy_session())                                                # gasta 0,04
    _budget(t, limit=0.63, mode="block")                                     # 0,04 + 0,60 = 0,64 > 0,63
    s = happy_session()
    with pytest.raises(BudgetExceeded):
        _scan(t, s)
    assert s.calls == [] and _used(t) == pytest.approx(SCAN_ACTUAL)          # el medidor no se tocó
    admin("delete from tenant_budgets where organization_id = %s", (str(t.org),))
    _budget(t, limit=0.64, mode="block")                                     # justo el límite → pasa
    _scan(t, happy_session())
    assert _used(t) == pytest.approx(2 * SCAN_ACTUAL)


def test_per_task_cap_blocks_a_scan_even_with_monthly_room():
    from cloudcost.services.budgets import BudgetExceeded
    t = new_tenant("ce6")
    _budget(t, limit=1000, mode="block", per_task=0.50)                      # un escaneo cuesta hasta 0,60 > 0,50
    with pytest.raises(BudgetExceeded):
        _scan(t, happy_session())


def test_degrade_mode_scans_inventory_without_calling_cost_explorer():
    t = new_tenant("ce7")
    _budget(t, limit=0.10, mode="degrade")
    s = happy_session()                                                      # el guion de CE existe, pero NO debe usarse
    out, _ = _scan(t, s)
    assert _ce_calls(s) == [] and _used(t) == 0
    assert out["resources_seen"] > 0
    assert _levels(t) == ["degraded"]
    assert any("Presupuesto agotado" in w for w in out["warnings"])


# ----------------------------------------------------------------------------------------------- gasto observado
def test_cloud_spend_forecast_uses_simulated_daily_costs():
    from cloudcost.services import budgets
    t = new_tenant("ce8")
    _scan(t, happy_session())                                                # persiste account_costs diarios simulados
    _budget(t, limit=200, mode="block", metric="cloud_spend")
    with t.tx() as conn:
        spent = budgets.spent_this_month(conn, "cloud_spend", TODAY)
        d = budgets.preflight(conn, t.org, "cloud_spend", 0.0, today=TODAY, record_alerts=False)
    assert spent == pytest.approx(7 * DAILY_ACCOUNT_SPEND)                   # 1–7 de octubre × 16,5 = 115,5 (el día 8 aún no existe)
    assert d.forecast_month_end_usd == pytest.approx(115.5 / 8 * 31, abs=0.01)   # 447,56 al cierre
    assert d.level == "forecast" and d.allowed                               # tarea gratuita: no se bloquea nada


# ----------------------------------------------------------------------------------------------- SOLO alertan, nunca borran
def test_alerting_deletes_nothing_in_the_platform_or_the_cloud():
    t = new_tenant("ce9")
    s1 = happy_session()
    _scan(t, s1)
    before = _counts(t)
    with t.tx() as conn:
        rec_before = conn.execute("select id, status, estimated_monthly_savings from recommendations order by id").fetchall()
    _budget(t, limit=0.01, mode="alert")                                     # superado desde el primer centavo
    s2 = happy_session()
    _scan(t, s2)
    after = _counts(t)
    assert all(after[k] >= before[k] for k in before), (before, after)       # ninguna tabla perdió filas
    with t.tx() as conn:
        rec_after = conn.execute("select id, status, estimated_monthly_savings from recommendations order by id").fetchall()
        inactive = conn.execute("select count(*) c from resources where not active").fetchone()["c"]
    assert [r["id"] for r in rec_after] == [r["id"] for r in rec_before] and inactive == 0
    from cloudcost.collectors import aws_guard
    assert {c for c in s1.calls + s2.calls} <= set(aws_guard.ALLOWED_OPERATIONS)


def test_block_and_degrade_leave_existing_data_untouched():
    from cloudcost.services.budgets import BudgetExceeded
    t = new_tenant("ce10")
    _scan(t, happy_session())
    _budget(t, limit=0.01, mode="block")
    before = _counts(t)
    with t.tx() as conn:
        active_before = conn.execute("select count(*) c from resources where active").fetchone()["c"]
    with pytest.raises(BudgetExceeded):
        _scan(t, happy_session())
    after = _counts(t)
    with t.tx() as conn:
        active_after = conn.execute("select count(*) c from resources where active").fetchone()["c"]
    assert active_after == active_before                                     # un escaneo bloqueado no desactiva recursos
    assert {k: v for k, v in after.items() if k not in ("audit_events",)} == {k: v for k, v in before.items() if k not in ("audit_events",)}
    assert after["audit_events"] > before["audit_events"]                    # sí deja rastro auditado de por qué se bloqueó


def test_application_role_cannot_delete_or_truncate_budget_data():
    rows = admin("""select t, has_table_privilege('cloudcost_app', t, 'DELETE') d, has_table_privilege('cloudcost_app', t, 'TRUNCATE') tr,
                           has_table_privilege('cloudcost_app', t, 'UPDATE') u
                    from unnest(array['usage_meter','budget_alerts','tenant_budgets']) t""")
    by = {r["t"]: r for r in rows}
    assert not any(r["d"] or r["tr"] for r in rows)                          # ninguna: ni DELETE ni TRUNCATE
    assert not by["usage_meter"]["u"] and not by["budget_alerts"]["u"]       # medidor y alertas son solo-añadir
    assert by["tenant_budgets"]["u"]                                         # la política sí se edita (solo ADMIN, por la API)


def test_budget_modules_contain_no_destructive_statements_or_cloud_writes():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "apps/api/cloudcost"
    text = "\n".join((root / p).read_text(encoding="utf-8") for p in ("domain/budgets.py", "services/budgets.py", "routers/budgets.py"))
    assert not re.search(r"\b(delete\s+from|drop\s+(table|index)|truncate)\b", text, re.I)
    assert not re.search(r"\b(boto3|botocore|requests|subprocess)\b", text), "el guardrail no habla con la nube ni ejecuta procesos"
