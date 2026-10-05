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
def verify_chain(p: Principal = Depends(require(*READ_AUDIT))):
    """Recorre la cadena de hashes de la organización y devuelve el primer eslabón alterado, si existe."""
    with tenant_tx(p.org_id) as conn:
        return conn.execute("select ok, checked, first_bad_seq from verify_audit_chain()").fetchone()
