from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..db import tenant_tx
from ..schemas import DecisionIn, DeployIn, VerifyIn
from ..secrets import SecretResolver
from ..security import APPROVE, CREATE_PR, MARK_DEPLOYED, READ, VERIFY_SAVINGS, Principal, require
from ..services import recommendations as recs
from ..services import workflow

router = APIRouter(tags=["recommendations"])


@router.get("/recommendations")
def list_recommendations(
    status: str | None = Query(None, pattern=r"^[A-Z_]{3,20}$"),
    risk: Literal["LOW", "MEDIUM", "HIGH"] | None = None,
    min_savings: float | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    p: Principal = Depends(require(*READ)),
):
    with tenant_tx(p.org_id) as conn:
        return recs.list_recommendations(conn, status=status, risk=risk, min_savings=min_savings, limit=limit, offset=offset)


@router.get("/recommendations/{rec_id}")
def get_recommendation(rec_id: UUID, p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return recs.get_detail(conn, rec_id)


@router.get("/recommendations/{rec_id}/evidence")
def get_evidence(rec_id: UUID, p: Principal = Depends(require(*READ))):
    """Evidencia versionada (fórmula, coste de referencia, fecha de los datos, supuestos) y su huella verificable."""
    with tenant_tx(p.org_id) as conn:
        return recs.get_evidence(conn, rec_id)


@router.post("/recommendations/{rec_id}/approve")
def approve(rec_id: UUID, body: DecisionIn, p: Principal = Depends(require(*APPROVE))):
    with tenant_tx(p.org_id) as conn:
        return workflow.decide(conn, p, rec_id, "APPROVED", body.reason, body.expected_version)


@router.post("/recommendations/{rec_id}/reject")
def reject(rec_id: UUID, body: DecisionIn, p: Principal = Depends(require(*APPROVE))):
    with tenant_tx(p.org_id) as conn:
        return workflow.decide(conn, p, rec_id, "REJECTED", body.reason, body.expected_version)


@router.post("/recommendations/{rec_id}/create-pr", status_code=201)
def create_pr(rec_id: UUID, p: Principal = Depends(require(*CREATE_PR)), settings: Settings = Depends(get_settings)):
    """Crea el Pull Request con el parche IaC. Exige estado APPROVED; es idempotente y nunca fusiona ni despliega."""
    with tenant_tx(p.org_id) as conn:
        result = workflow.create_pull_request(conn, p, rec_id, settings=settings, secrets=SecretResolver())
    if result.get("blocked"):          # la auditoría de la evaluación ya quedó confirmada (no se lanzó excepción dentro de la transacción)
        engine_down = result["code"] == "policy_engine_unavailable"
        message = ("El motor de políticas no está disponible: por seguridad no se crea el PR. Reintenta o revisa OPA_BINARY."
                   if engine_down else "Una política bloquea este Pull Request:")
        return JSONResponse(status_code=503 if engine_down else 422, content={
            "detail": {"message": message, "errors": result["violations"]}, "code": result["code"]})
    return result


@router.post("/recommendations/{rec_id}/mark-merged")
def mark_merged(rec_id: UUID, p: Principal = Depends(require(*MARK_DEPLOYED)), settings: Settings = Depends(get_settings)):
    """Solo demo: simula el merge del PR del repositorio 'local' (no tiene webhooks)."""
    with tenant_tx(p.org_id) as conn:
        return workflow.mark_merged_local(conn, p, rec_id, settings)


@router.post("/recommendations/{rec_id}/deployed")
def mark_deployed(rec_id: UUID, body: DeployIn, p: Principal = Depends(require(*MARK_DEPLOYED))):
    """Callback del pipeline CI/CD tras desplegar el cambio fusionado (MERGED → DEPLOYED)."""
    with tenant_tx(p.org_id) as conn:
        return workflow.mark_deployed(conn, p, rec_id, reference=body.reference, deployed_on=body.deployed_on)


@router.post("/recommendations/{rec_id}/verify-savings")
def verify_savings(rec_id: UUID, body: VerifyIn, p: Principal = Depends(require(*VERIFY_SAVINGS))):
    """DEPLOYED → VERIFIED: compara ahorro esperado y observado y registra el porcentaje de realización."""
    with tenant_tx(p.org_id) as conn:
        return workflow.verify_savings(conn, p, rec_id, observed_monthly_cost=body.observed_monthly_cost)
