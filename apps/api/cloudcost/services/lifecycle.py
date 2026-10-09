"""Coherencia del conjunto de recomendaciones entre escaneos: caducidad, obsolescencia y conflictos con cambios en curso.

Un inventario cambia: el recurso se elimina, la carga sube y deja de estar ocioso, otra regla pasa a cubrir el mismo recurso. Si las
recomendaciones anteriores siguieran abiertas, alguien podría aprobar la eliminación de algo que ya no existe o dos cambios
incompatibles sobre el mismo recurso. Después de cada escaneo COMPLETO:

  · PROPOSED / PENDING_APPROVAL que ya no se detectan  →  EXPIRED (con el motivo), nadie debe aprobarlas;
  · APPROVED / PR_CREATED que ya no se detectan        →  se marcan obsoletas (`stale_since`) y se avisa; el PR puede seguir adelante
    porque la decisión humana ya existe, pero quien lo revise ve que la premisa cambió;
  · un hallazgo nuevo sobre un recurso con otra recomendación en curso (aprobada, con PR, fusionada o desplegada sin verificar) nace
    bloqueado por política: no se aprueban dos cambios a la vez sobre lo mismo.

Un escaneo parcial o degradado por límites de solicitudes no caduca nada: «no lo vi» no es «ya no existe».
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from psycopg import Connection

from ..domain import state_machine as sm
from ..domain.policy import PolicyDecision
from . import audit
from .recommendations import transition

OPEN_STATES = (sm.PROPOSED, sm.PENDING_APPROVAL)
APPROVED_STATES = (sm.APPROVED, sm.PR_CREATED)
IN_FLIGHT_STATES = (sm.APPROVED, sm.PR_CREATED, sm.MERGED, sm.DEPLOYED)

GONE, SUPERSEDED, CLEARED = "resource_gone", "superseded", "condition_cleared"
_REASONS = {
    GONE: "El recurso ya no aparece en el inventario del último escaneo completo.",
    SUPERSEDED: "Sustituida por otra recomendación sobre el mismo recurso.",
    CLEARED: "La condición que la originó ya no se cumple en el último escaneo completo.",
}


@dataclass
class Reconciled:
    expired: int = 0
    stale: int = 0
    recovered: int = 0
    skipped: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (0, None)}


def in_flight(conn: Connection, account_id) -> dict[str, list[dict[str, Any]]]:
    """Recomendaciones en curso por recurso: {resource_pk: [{id, dedupe_key, status}]}."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute("select id, resource_pk, dedupe_key, status from recommendations where cloud_account_id = %s and status = any(%s)",
                          (str(account_id), list(IN_FLIGHT_STATES))).fetchall():
        out.setdefault(str(r["resource_pk"]), []).append(r)
    return out


def block_if_in_flight(decision: PolicyDecision, dedupe_key: str, others: list[dict[str, Any]] | None) -> PolicyDecision:
    """Un hallazgo que choca con un cambio en curso sobre el mismo recurso nace bloqueado (queda PROPOSED, no se puede aprobar)."""
    rivals = [o for o in (others or []) if o["dedupe_key"] != dedupe_key]
    if not rivals:
        return decision
    ids = ", ".join(str(o["id"])[:8] for o in rivals)
    return replace(decision, allowed=False,
                   blocked_reasons=[*decision.blocked_reasons, f"Hay otra recomendación en curso sobre este recurso ({ids}): "
                                                                "espera a que se verifique o se rechace antes de aprobar otra"],
                   applied=[*decision.applied, "IN_FLIGHT_CONFLICT"])


def reconcile(conn: Connection, org_id, *, account_id, scan_id, current_keys: set[str], keys_by_resource: dict[str, set[str]],
              partial: bool, degraded: bool = False) -> Reconciled:
    out = Reconciled()
    if partial or degraded:
        out.skipped = "scan_partial" if partial else "scan_degraded"
        return out
    rows = conn.execute(
        """select r.id, r.status, r.dedupe_key, r.resource_pk, r.stale_since, res.active, res.last_scan_id
             from recommendations r join resources res on res.id = r.resource_pk
            where r.cloud_account_id = %s and r.status = any(%s)
            for update of r skip locked""",                                            # lo que otra transacción está decidiendo, no se toca
        (str(account_id), list(OPEN_STATES + APPROVED_STATES))).fetchall()
    for r in rows:
        detected = r["dedupe_key"] in current_keys
        if r["status"] in OPEN_STATES:
            if detected:
                continue
            gone = (not r["active"]) or str(r["last_scan_id"]) != str(scan_id)
            code = GONE if gone else SUPERSEDED if keys_by_resource.get(str(r["resource_pk"])) else CLEARED
            transition(conn, org_id, r["id"], r["status"], sm.EXPIRED, audit.SYSTEM, _REASONS[code],
                       set_sql="stale_since = now(), stale_reason = %s", set_params=(code,))
            audit.record(conn, org_id, audit.RECOMMENDATION_EXPIRED, entity_type="recommendation", entity_id=r["id"],
                         payload={"reason": code, "scan_id": str(scan_id), "previous_status": r["status"]})
            out.expired += 1
        elif detected and r["stale_since"] is not None:                                # volvió a detectarse
            conn.execute("update recommendations set stale_since = null, stale_reason = null where id = %s", (str(r["id"]),))
            out.recovered += 1
        elif not detected and r["stale_since"] is None:
            gone = (not r["active"]) or str(r["last_scan_id"]) != str(scan_id)
            code = GONE if gone else CLEARED
            conn.execute("update recommendations set stale_since = now(), stale_reason = %s where id = %s", (code, str(r["id"])))
            audit.record(conn, org_id, audit.RECOMMENDATION_STALE, entity_type="recommendation", entity_id=r["id"],
                         payload={"reason": code, "scan_id": str(scan_id), "status": r["status"]})
            out.stale += 1
    return out
