"""Presupuestos por tenant: configuración, estado y comprobación previa de una tarea. El límite lo fija un ADMIN."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..db import tenant_tx
from ..security import MANAGE_CONNECTIONS, READ, SCAN, Principal, require
from ..services import audit, budgets

router = APIRouter(tags=["budgets"])


class BudgetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal["api_usage", "cloud_spend"]
    monthly_limit_usd: float = Field(ge=0, le=100_000_000)
    per_task_limit_usd: float | None = Field(None, ge=0, le=100_000_000)
    warn_ratio: float = Field(0.8, gt=0, le=1)
    mode: Literal["alert", "degrade", "block"] = "alert"
    enabled: bool = True


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal["api_usage", "cloud_spend"] = "api_usage"
    estimated_cost_usd: float = Field(ge=0, le=100_000_000)


class UsageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9_.:-]+$")
    cost_usd: float = Field(ge=0, le=1_000_000)
    quantity: float = Field(1, ge=0, le=1_000_000_000)
    ref: str | None = Field(None, max_length=120)


@router.get("/budgets")
def list_budgets(p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return {"items": budgets.status(conn)}


@router.put("/budgets")
def put_budget(body: BudgetIn, p: Principal = Depends(require(*MANAGE_CONNECTIONS))):
    with tenant_tx(p.org_id) as conn:
        conn.execute(
            """insert into tenant_budgets (organization_id, metric, monthly_limit_usd, per_task_limit_usd, warn_ratio, mode, enabled, updated_by)
               values (%s, %s, %s, %s, %s, %s, %s, %s)
               on conflict (organization_id, metric) do update set monthly_limit_usd = excluded.monthly_limit_usd,
                   per_task_limit_usd = excluded.per_task_limit_usd, warn_ratio = excluded.warn_ratio, mode = excluded.mode,
                   enabled = excluded.enabled, updated_by = excluded.updated_by, updated_at = now()""",
            (str(p.org_id), body.metric, body.monthly_limit_usd, body.per_task_limit_usd, body.warn_ratio, body.mode, body.enabled,
             audit.user_actor(p).id))
        audit.record(conn, p.org_id, budgets.BUDGET_UPDATED, actor=audit.user_actor(p), entity_type="budget",
                     payload=body.model_dump())
        return {"items": budgets.status(conn)}


@router.post("/budgets/check")
def check_budget(body: CheckIn, p: Principal = Depends(require(*SCAN))):
    """Simula una tarea: ¿cabe en el presupuesto del mes? No consume nada; sí deja la alerta si corresponde."""
    with tenant_tx(p.org_id) as conn:
        d = budgets.preflight(conn, p.org_id, body.metric, body.estimated_cost_usd)
        audit.record(conn, p.org_id, budgets.BUDGET_EVALUATED, actor=audit.user_actor(p), entity_type="budget",
                     payload={"metric": body.metric, "allowed": d.allowed, "level": d.level})
        return d.to_dict()


@router.post("/budgets/usage", status_code=201)
def record_usage(body: UsageIn, p: Principal = Depends(require(*SCAN))):
    """Registra el coste de una consulta o tarea ya ejecutada por un servicio del ecosistema (queda en el medidor del mes)."""
    with tenant_tx(p.org_id) as conn:
        budgets.record_usage(conn, p.org_id, body.source, body.cost_usd, quantity=body.quantity, ref=body.ref)
        return {"items": budgets.status(conn)}


@router.get("/budgets/alerts")
def list_alerts(p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return {"items": conn.execute("select metric, level, period, message, details, created_at from budget_alerts "
                                      "order by created_at desc limit 100").fetchall()}
