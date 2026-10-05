from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import tenant_tx
from ..security import READ, Principal, require
from ..services import dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def summary(p: Principal = Depends(require(*READ))):
    with tenant_tx(p.org_id) as conn:
        return dashboard.summary(conn)
