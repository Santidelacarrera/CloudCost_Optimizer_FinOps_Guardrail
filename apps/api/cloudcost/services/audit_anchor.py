"""Anclaje externo de la cabeza de la cadena de auditoría.

La cadena de hashes por sí sola no detecta que se borren los ÚLTIMOS eventos ni que el propietario de la base reescriba todo. Esta herramienta
guarda, fuera de la base, la cabeza (`seq`, `hash`) de cada organización en un archivo de solo-añadir cuyas líneas también se encadenan entre sí
(`prev`/`line_hash`), y puede avisar a un webhook. Más tarde, `--verify` comprueba que cada cabeza anotada sigue existiendo tal cual.

Ejecutarla periódicamente (cron/systemd) y guardar el archivo en OTRO sitio (otro servidor, un bucket con retención) es lo que da el valor:
si el archivo vive en el mismo servidor y con los mismos permisos que la base, quien reescribe la base reescribe también el archivo.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from ..db import tenant_tx

GENESIS = "0" * 64


def _line_hash(prev: str, entry: dict[str, Any]) -> str:
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{prev}|{body}".encode()).hexdigest()


class NoAdminAccess(RuntimeError):
    pass


def list_orgs() -> list[str]:
    """Todas las organizaciones: exige la conexión de ADMINISTRACIÓN de la base (`DATABASE_ADMIN_URL`). El rol de la aplicación no puede listar
    organizaciones ajenas (RLS), y no se le da una función para hacerlo; sin esa conexión, pasa `--org` por cada organización."""
    import os

    import psycopg

    url = os.environ.get("DATABASE_ADMIN_URL")
    if not url:
        raise NoAdminAccess("Para anclar todas las organizaciones hace falta DATABASE_ADMIN_URL; si no, indica cada una con --org <uuid>.")
    with psycopg.connect(url) as conn:
        return [str(r[0]) for r in conn.execute("select id from organizations order by created_at, id").fetchall()]


def snapshot_org(org_id: str) -> dict[str, Any]:
    with tenant_tx(org_id) as conn:
        chain = conn.execute("select ok, checked, first_bad_seq from verify_audit_chain()").fetchone()
        head = conn.execute("select seq, hash from audit_events order by seq desc limit 1").fetchone()
    return {"org_id": org_id, "head_seq": head["seq"] if head else None, "head_hash": head["hash"] if head else None,
            "chain_ok": bool(chain["ok"]), "checked": chain["checked"], "first_bad_seq": chain["first_bad_seq"]}


def make_entries(orgs: list[str] | None, prev_hash: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Una línea por organización, encadenada a la anterior del archivo."""
    stamp = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    out: list[dict[str, Any]] = []
    prev = prev_hash
    for org in orgs or list_orgs():
        entry = {**snapshot_org(org), "anchored_at": stamp}
        prev = _line_hash(prev, entry)
        out.append({**entry, "prev": out[-1]["line_hash"] if out else prev_hash, "line_hash": prev})
    return out


def read_file(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []


def check_file_chain(lines: list[dict[str, Any]]) -> list[str]:
    """Problemas del PROPIO archivo de anclas: líneas editadas, quitadas o reordenadas."""
    problems: list[str] = []
    prev = GENESIS
    for n, line in enumerate(lines, start=1):
        entry = {k: v for k, v in line.items() if k not in ("prev", "line_hash")}
        if line.get("prev") != prev:
            problems.append(f"línea {n}: no continúa a la anterior (se quitó o reordenó una línea)")
        if line.get("line_hash") != _line_hash(prev, entry):
            problems.append(f"línea {n}: su contenido no coincide con su huella (se editó)")
        prev = line.get("line_hash", "")
    return problems


def verify_anchors(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Para cada ancla anotada: ¿sigue existiendo en la base ese evento con ese hash?"""
    results = []
    for line in lines:
        if line.get("head_seq") is None:
            results.append({"org_id": line["org_id"], "head_seq": None, "anchored_at": line["anchored_at"], "ok": True, "reason": "sin eventos"})
            continue
        try:
            with tenant_tx(line["org_id"]) as conn:
                found = conn.execute("select 1 from audit_events where seq = %s and hash = %s", (line["head_seq"], line["head_hash"])).fetchone()
        except Exception as exc:                                    # noqa: BLE001 — una organización borrada no debe ocultar el resto
            results.append({"org_id": line["org_id"], "head_seq": line["head_seq"], "anchored_at": line["anchored_at"], "ok": False,
                            "reason": f"no se pudo comprobar ({type(exc).__name__})"})
            continue
        results.append({"org_id": line["org_id"], "head_seq": line["head_seq"], "anchored_at": line["anchored_at"], "ok": found is not None,
                        "reason": "" if found else "el evento anotado ya no existe o cambió: la auditoría se truncó o se reescribió"})
    return results
