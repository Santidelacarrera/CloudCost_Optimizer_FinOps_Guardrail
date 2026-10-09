"""Evidencia versionada y verificable de cada recomendación.

Cada cifra de ahorro se guarda con el coste de referencia, la fecha de los datos, la fórmula y los supuestos en
`recommendation_evidence` (solo INSERT para el rol de aplicación) y su huella SHA-256 pasa a la cadena de auditoría:

    · quien aprueba aprueba una huella concreta (queda en `approvals.context` y en el evento APPROVAL_GRANTED);
    · si alguien alterara la evidencia guardada, la huella recalculada no coincidiría con la de la auditoría;
    · si alguien alterara la auditoría, la cadena de hashes lo delataría.

No se crea una versión nueva en cada escaneo (las métricas oscilan): solo cuando cambia lo que importa para decidir (ver `material`).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

# Claves que cambian en cada escaneo sin cambiar la conclusión: no entran en la huella.
VOLATILE_KEYS = frozenset({"as_of", "captured_at", "collected_at", "duration_ms", "duration_seconds"})
SAVINGS_TOLERANCE = 0.01          # un cambio de ≥ 1 % (o ≥ 1 USD) del ahorro es material
CONFIDENCE_TOLERANCE = 0.05


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) if k not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rounded = round(value, 6)
        return int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def canonical_json(payload: Any) -> str:
    return json.dumps(_normalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def evidence_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hashed_payload(*, rule_id: str, formula_id: str, params: dict, estimated: float, reference_cost: float, confidence: float,
                   evidence: dict, assumptions: list[str]) -> dict[str, Any]:
    """Lo que cubre la huella: todo lo que sustenta la cifra, sin marcas de tiempo volátiles."""
    return {"rule_id": rule_id, "formula_id": formula_id, "params": params, "estimated_monthly_savings": round(float(estimated), 2),
            "reference_cost": round(float(reference_cost), 2), "confidence": round(float(confidence), 3), "evidence": evidence,
            "assumptions": assumptions}


@dataclass
class Snapshot:
    id: Any
    hash: str
    created: bool


def material(prev: dict[str, Any] | None, *, estimated: float, confidence: float, source: str, params: dict) -> bool:
    """¿Cambió algo que importe para decidir? Ahorro, confianza, origen del coste o parámetros de la acción."""
    if prev is None:
        return True
    old = float(prev["estimated_monthly_savings"])
    if abs(old - estimated) >= max(1.0, SAVINGS_TOLERANCE * max(old, estimated)):
        return True
    if abs(float(prev["confidence"]) - confidence) >= CONFIDENCE_TOLERANCE:
        return True
    return prev["reference_cost_source"] != source or (prev["inputs"] or {}).get("params") != json.loads(json.dumps(params, default=str))


def latest(conn: Connection, rec_id) -> dict[str, Any] | None:
    return conn.execute(
        "select * from recommendation_evidence where recommendation_id = %s order by created_at desc, id limit 1", (str(rec_id),)).fetchone()


def record(conn: Connection, org_id, *, rec_id, version: int, scan_id, rule_id: str, params: dict, estimated: float,
           reference_cost: float, confidence: float, evidence: dict, force: bool = False,
           data_as_of: datetime | None = None) -> Snapshot:
    """Guarda una versión de la evidencia si es material (o `force`) y devuelve la vigente."""
    estimate = evidence.get("estimate") or {}
    reference = estimate.get("reference") or {}
    assumptions = list(estimate.get("assumptions") or [])
    formula_id = estimate.get("formula_id") or f"{rule_id}.v0"
    source = str(reference.get("source") or "unknown")
    payload = hashed_payload(rule_id=rule_id, formula_id=formula_id, params=params, estimated=estimated, reference_cost=reference_cost,
                             confidence=confidence, evidence=evidence, assumptions=assumptions)
    digest = evidence_hash(payload)
    prev = latest(conn, rec_id)
    if not force and prev is not None and prev["recommendation_version"] == version and not material(
            prev, estimated=estimated, confidence=confidence, source=source, params=params):
        return Snapshot(prev["id"], prev["evidence_hash"], False)
    as_of = data_as_of or datetime.now(timezone.utc)
    row = conn.execute(
        """insert into recommendation_evidence (organization_id, recommendation_id, recommendation_version, scan_id, rule_id, formula_id,
               data_as_of, reference_cost, reference_cost_source, reference_window_start, reference_window_end,
               estimated_monthly_savings, confidence, evidence, assumptions, inputs, evidence_hash)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
        (str(org_id), str(rec_id), version, str(scan_id) if scan_id else None, rule_id, formula_id, as_of, round(reference_cost, 2), source,
         reference.get("window_start"), reference.get("window_end"), round(estimated, 2), round(confidence, 3), Jsonb(evidence),
         Jsonb(assumptions), Jsonb({"params": params, "formula_inputs": estimate.get("inputs") or {}}), digest)).fetchone()
    conn.execute("update recommendations set evidence_hash = %s where id = %s", (digest, str(rec_id)))
    return Snapshot(row["id"], digest, True)


def recompute_hash(row: dict[str, Any]) -> str:
    """Huella recalculada a partir de lo que hay guardado (para verificar que no se alteró)."""
    payload = hashed_payload(
        rule_id=row["rule_id"], formula_id=row["formula_id"], params=(row["inputs"] or {}).get("params") or {},
        estimated=float(row["estimated_monthly_savings"]), reference_cost=float(row["reference_cost"]),
        confidence=float(row["confidence"]), evidence=row["evidence"], assumptions=row["assumptions"] or [])
    return evidence_hash(payload)


def history(conn: Connection, rec_id, *, with_evidence: bool = False) -> list[dict[str, Any]]:
    """Versiones de la evidencia, de la más reciente a la más antigua, con la comprobación de integridad de cada una."""
    rows = conn.execute("select * from recommendation_evidence where recommendation_id = %s order by created_at desc, id", (str(rec_id),)).fetchall()
    out = []
    for r in rows:
        item = {"id": r["id"], "recommendation_version": r["recommendation_version"], "formula_id": r["formula_id"],
                "data_as_of": r["data_as_of"], "created_at": r["created_at"], "reference_cost": float(r["reference_cost"]),
                "reference_cost_source": r["reference_cost_source"], "reference_window_start": r["reference_window_start"],
                "reference_window_end": r["reference_window_end"], "estimated_monthly_savings": float(r["estimated_monthly_savings"]),
                "confidence": float(r["confidence"]), "assumptions": r["assumptions"], "evidence_hash": r["evidence_hash"],
                "integrity_ok": recompute_hash(r) == r["evidence_hash"]}
        if with_evidence:
            item["evidence"] = r["evidence"]
            item["inputs"] = r["inputs"]
        out.append(item)
    return out


def ensure_for(conn: Connection, org_id, rec: dict[str, Any]) -> Snapshot:
    """Garantiza que exista la evidencia de la versión vigente de `rec` (la que ven quienes aprueban) y la devuelve."""
    prev = latest(conn, rec["id"])
    if prev is not None and prev["recommendation_version"] == rec["version"]:
        return Snapshot(prev["id"], prev["evidence_hash"], False)
    return record(conn, org_id, rec_id=rec["id"], version=rec["version"], scan_id=rec.get("scan_id"), rule_id=rec["rule_id"],
                  params=rec["params"] or {}, estimated=float(rec["estimated_monthly_savings"]),
                  reference_cost=float(rec["current_monthly_cost"]), confidence=float(rec["confidence"]), evidence=rec["evidence"] or {},
                  force=True)
