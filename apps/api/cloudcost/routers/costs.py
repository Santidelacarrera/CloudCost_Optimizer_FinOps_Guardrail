"""Costos de la cuenta por servicio, región y período (normalizados desde Cost Explorer). Solo lectura."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db import tenant_tx
from ..security import READ, Principal, require

router = APIRouter(tags=["costs"])

_GROUPS = {"service": "service", "region": "region", "period": "period_start", "account": "account_ref"}


@router.get("/costs")
def account_costs(
    cloud_account_id: UUID | None = None,
    granularity: Literal["DAILY", "MONTHLY"] = "MONTHLY",
    date_from: date | None = None,
    date_to: date | None = Query(None, description="Exclusivo"),
    group_by: str = Query("service", pattern=r"^(service|region|period|account)(,(service|region|period|account)){0,3}$"),
    p: Principal = Depends(require(*READ)),
):
    """Coste agregado por las dimensiones elegidas. Las monedas NUNCA se suman entre sí (una fila por moneda).

    `estimated=true` indica que el total incluye períodos que el proveedor aún puede modificar (el mes en curso, los últimos días).
    """
    dims = list(dict.fromkeys(group_by.split(",")))
    if date_from and date_to and date_to <= date_from:
        raise HTTPException(422, "date_to debe ser posterior a date_from")
    cols = ", ".join(_GROUPS[d] for d in dims)
    with tenant_tx(p.org_id) as conn:
        if cloud_account_id and not conn.execute("select 1 from cloud_accounts where id = %s", (str(cloud_account_id),)).fetchone():
            raise HTTPException(404, "Cuenta cloud no encontrada")
        rows = conn.execute(
            f"""select {cols}, currency, round(sum(amount), 2) as amount, bool_or(estimated) as estimated, count(*) as rows
                  from account_costs
                 where (%(acc)s::uuid is null or cloud_account_id = %(acc)s)
                   and granularity = %(g)s
                   and (%(f)s::date is null or period_start >= %(f)s)
                   and (%(t)s::date is null or period_start < %(t)s)
                 group by {cols}, currency
                 order by {cols}, currency""",                                  # noqa: S608 — columnas fijas de una lista cerrada
            {"acc": str(cloud_account_id) if cloud_account_id else None, "g": granularity, "f": date_from, "t": date_to}).fetchall()
        coverage = conn.execute(
            """select min(period_start) as first_period, max(period_start) as last_period, max(collected_at) as collected_at
                 from account_costs where (%(acc)s::uuid is null or cloud_account_id = %(acc)s) and granularity = %(g)s""",
            {"acc": str(cloud_account_id) if cloud_account_id else None, "g": granularity}).fetchone()
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["currency"]] = round(totals.get(r["currency"], 0.0) + float(r["amount"]), 2)
    return {"granularity": granularity, "group_by": dims, "items": rows, "totals_by_currency": totals,
            "estimated": any(r["estimated"] for r in rows), "coverage": coverage}
