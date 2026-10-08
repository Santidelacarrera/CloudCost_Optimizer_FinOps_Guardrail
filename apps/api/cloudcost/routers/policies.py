"""Evaluación de políticas Rego sobre un plan de Terraform (`terraform show -json`) enviado por el cliente."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import guardrail
from ..config import Settings, get_settings
from ..db import tenant_tx
from ..security import SCAN, Principal, require
from ..services import audit

router = APIRouter(tags=["policies"])


class PlanIn(BaseModel):
    plan: dict[str, Any]


@router.post("/policies/evaluate-plan")
def evaluate_plan(request: Request, body: PlanIn, p: Principal = Depends(require(*SCAN)),
                  settings: Settings = Depends(get_settings)):
    """Evalúa las políticas de guardrail sobre un plan real, p. ej. desde el pipeline del cliente, sin instalar OPA.

    Es solo lectura: no crea nada. El campo `cloudcost` del plan (contexto que aporta la plataforma) se descarta para que el
    llamador no pueda declararse a sí mismo «aprobado».
    """
    if int(request.headers.get("content-length") or 0) > guardrail.MAX_PLAN_BYTES:
        raise HTTPException(413, "Plan demasiado grande")
    plan = {k: v for k, v in body.plan.items() if k != "cloudcost"}
    verdict = guardrail.evaluate_plan(settings, plan, mode="audit")           # se evalúa aunque OPA_MODE=off: lo pidió el usuario
    with tenant_tx(p.org_id) as conn:
        audit.record(conn, p.org_id, audit.POLICY_EVALUATED, actor=audit.user_actor(p), entity_type="plan",
                     payload={**verdict.audit_payload(), "source": "api", "resource_changes": len(plan.get("resource_changes") or [])})
    if verdict.error:
        raise HTTPException(503, f"No se pudo evaluar la política: {verdict.error}")
    return {"allowed": not verdict.violations, "violations": verdict.violations, "policy_digest": verdict.policy_digest}
