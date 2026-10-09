from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import tenant_tx
from ..security import READ, Principal, require
from ..services import dashboard, projection

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def summary(p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return dashboard.summary(conn)


@router.get("/dashboard/savings-projection")
def savings_projection(months_back: int = Query(projection.DEFAULT_BACK, ge=0, le=projection.MAX_BACK),
                       months_ahead: int = Query(projection.DEFAULT_AHEAD, ge=1, le=projection.MAX_AHEAD),
                       p: Principal = Depends(require(*READ))):
    """Serie mensual «Gasto actual» vs «Gasto optimizado» (y «esperado» ponderado por confianza). Ver services/projection.py."""
    with tenant_tx(p.org_id) as conn:
        return projection.build(conn, months_back=months_back, months_ahead=months_ahead)
