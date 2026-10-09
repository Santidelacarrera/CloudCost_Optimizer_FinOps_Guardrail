from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import tenant_tx
from ..security import READ_AUDIT, Principal, require

router = APIRouter(tags=["audit"])


@router.get("/audit/events")
def list_events(
    event_type: str | None = Query(None, max_length=60),
    entity_id: str | None = Query(None, max_length=64),
    before_seq: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    p: Principal = Depends(require(*READ_AUDIT)),
):
    with tenant_tx(p.org_id) as conn:
        return conn.execute(
            """select seq, event_type, actor_type, actor_id, entity_type, entity_id, payload, hash, prev_hash, created_at
                 from audit_events
                where (%(t)s::text is null or event_type = %(t)s)
                  and (%(e)s::text is null or entity_id = %(e)s)
                  and (%(b)s::bigint is null or seq < %(b)s)
                order by seq desc limit %(l)s""",
            {"t": event_type, "e": entity_id, "b": before_seq, "l": limit}).fetchall()


@router.get("/audit/verify")
def verify_chain(
    pin_seq: int | None = Query(None, ge=1, description="Evento anotado en una verificación anterior (o en una copia externa)"),
    pin_hash: str | None = Query(None, pattern=r"^[0-9a-f]{64}$", description="Hash que tenía ese evento"),
    p: Principal = Depends(require(*READ_AUDIT)),
):
    """Recorre la cadena de hashes de la organización y devuelve el primer eslabón alterado, si existe.

    Una cadena de hashes por sí sola NO detecta que se hayan borrado los últimos eventos ni que alguien con acceso de propietario a la base
    la haya reescrito entera. Por eso la respuesta incluye la cabeza (`head_seq`, `head_hash`): guárdala fuera de la base (los respaldos y
    el monitor externo lo hacen) y pásala después como `pin_seq`/`pin_hash`: `anchored=false` indica que ese evento ya no existe o cambió.
    """
    with tenant_tx(p.org_id) as conn:
        chain = conn.execute("select ok, checked, first_bad_seq from verify_audit_chain()").fetchone()
        head = conn.execute("select seq, hash from audit_events order by seq desc limit 1").fetchone()
        anchored = None
        if pin_seq is not None and pin_hash is not None:
            anchored = conn.execute("select 1 from audit_events where seq = %s and hash = %s", (pin_seq, pin_hash)).fetchone() is not None
    return {**chain, "head_seq": head["seq"] if head else None, "head_hash": head["hash"] if head else None, "anchored": anchored}
