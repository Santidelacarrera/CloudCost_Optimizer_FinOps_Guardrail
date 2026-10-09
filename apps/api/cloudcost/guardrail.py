"""Evaluación de políticas Rego (Open Policy Agent) dentro de la API, de forma síncrona y antes de crear el Pull Request.

Hasta ahora las políticas de `packages/policies` solo corrían en el CI del cliente: si ese CI no las ejecutaba, el PR
se creaba igual. Aquí la propia plataforma construye un plan sintético a partir del parche IaC aprobado, lo evalúa con
las MISMAS reglas Rego y bloquea la creación del PR si hay violaciones (modo `enforce`).

Se usa el binario oficial `opa` (subproceso, sin shell, con límite de tiempo y de tamaño y un entorno mínimo) en lugar de
reimplementar Rego: así la semántica es idéntica a `opa test` en CI. Si el motor falla o no está disponible, en `enforce`
se falla CERRADO (no se crea el PR); en `audit` solo se registra.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import metrics
from .config import Settings
from .domain.models import RESIZE_ATTR
from .domain.rules import ACTION_DELETE_DB, ACTION_DELETE_SNAPSHOT, ACTION_DELETE_VOLUME, ACTION_REMOVE, ACTION_RESIZE

QUERY = "data.cloudcost.guardrail.deny"
MAX_PLAN_BYTES = 2_000_000
_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,8}\b")          # CHG-1234, INC-77, JIRA-9
_REINFORCING_ROLES = {"ADMIN", "SRE"}
_DELETE_ACTIONS = {ACTION_REMOVE, ACTION_DELETE_VOLUME, ACTION_DELETE_SNAPSHOT, ACTION_DELETE_DB}


class PolicyEngineError(Exception):
    """El motor no pudo evaluar (binario ausente, tiempo agotado, política inválida, salida ilegible)."""


@dataclass
class GuardrailVerdict:
    mode: str                                   # off | audit | enforce
    evaluated: bool = False
    violations: list[str] = field(default_factory=list)
    error: str | None = None
    policy_digest: str | None = None
    duration_ms: float = 0.0

    @property
    def result(self) -> str:
        return "error" if self.error else "denied" if self.violations else "allowed"

    @property
    def blocked(self) -> bool:
        """En `enforce` se bloquea con violaciones y también si el motor no pudo decidir (fail-closed)."""
        return self.mode == "enforce" and (bool(self.violations) or self.error is not None)

    def audit_payload(self) -> dict[str, Any]:
        return {"mode": self.mode, "result": self.result, "blocked": self.blocked, "violations": self.violations,
                "error": self.error, "policy_digest": self.policy_digest, "duration_ms": round(self.duration_ms, 1)}


# --------------------------------------------------------------------------- políticas
def default_policy_dir() -> Path:
    for candidate in (Path("/app/policies"), Path(__file__).resolve().parents[3] / "packages" / "policies"):
        if candidate.is_dir():
            return candidate
    return Path("/app/policies")


def policy_files(settings: Settings) -> list[Path]:
    """Archivos .rego del paquete incluido y, opcionalmente, de un directorio propio del cliente (sin los *_test.rego)."""
    dirs = [Path(settings.opa_policy_dir) if settings.opa_policy_dir else default_policy_dir()]
    if settings.opa_extra_policy_dir:
        dirs.append(Path(settings.opa_extra_policy_dir))
    files = sorted(p for d in dirs if d.is_dir() for p in d.glob("*.rego") if not p.name.endswith("_test.rego"))
    if not files:
        raise PolicyEngineError("No se encontraron políticas Rego (OPA_POLICY_DIR)")
    return files


def digest_of(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- motor
def evaluate_plan(settings: Settings, plan: dict[str, Any], *, mode: str | None = None) -> GuardrailVerdict:
    """Evalúa `plan` (formato de `terraform show -json`) con las políticas. Nunca lanza: devuelve un veredicto."""
    mode = mode or settings.effective_opa_mode
    verdict = GuardrailVerdict(mode=mode)
    if mode == "off":
        return verdict
    started = time.perf_counter()
    try:
        files = policy_files(settings)
        verdict.policy_digest = digest_of(files)
        verdict.violations = _run_opa(settings, files, plan)
        verdict.evaluated = True
    except PolicyEngineError as exc:
        verdict.error = str(exc)[:300]
    verdict.duration_ms = (time.perf_counter() - started) * 1000
    metrics.POLICY_EVALUATIONS.labels(verdict.result).inc()
    metrics.POLICY_LATENCY.observe(verdict.duration_ms / 1000)
    return verdict


def _run_opa(settings: Settings, files: list[Path], plan: dict[str, Any]) -> list[str]:
    binary = shutil.which(settings.opa_binary) or (settings.opa_binary if os.path.isfile(settings.opa_binary) else None)
    if not binary:
        raise PolicyEngineError(f"Binario OPA no encontrado ({settings.opa_binary})")
    payload = json.dumps(plan, default=str).encode()
    if len(payload) > MAX_PLAN_BYTES:
        raise PolicyEngineError("El plan es demasiado grande para evaluarlo")
    cmd = [binary, "eval", "--format", "json", "--stdin-input"]
    for f in files:
        cmd += ["--data", str(f)]
    cmd.append(QUERY)
    try:
        proc = subprocess.run(cmd, input=payload, capture_output=True, timeout=settings.opa_timeout_seconds, check=False,
                              env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")})     # sin secretos del proceso
    except subprocess.TimeoutExpired as exc:
        raise PolicyEngineError(f"OPA superó el tiempo límite ({settings.opa_timeout_seconds:g} s)") from exc
    except OSError as exc:
        raise PolicyEngineError(f"No se pudo ejecutar OPA: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise PolicyEngineError(f"OPA terminó con error ({proc.returncode}): {_first_error(proc.stdout, proc.stderr)}")
    try:
        doc = json.loads(proc.stdout or b"{}")
    except ValueError as exc:
        raise PolicyEngineError("Salida de OPA ilegible") from exc
    values = [v for r in doc.get("result") or [] for e in r.get("expressions") or [] for v in (e.get("value") or [])]
    return sorted({str(v) for v in values})


def _first_error(stdout: bytes, stderr: bytes) -> str:
    try:
        errors = (json.loads(stdout or b"{}").get("errors") or [])
        if errors:
            return str(errors[0].get("message", ""))[:200]
    except ValueError:
        pass
    return (stderr or stdout or b"").decode("utf-8", "replace").strip()[:200]


# --------------------------------------------------------------------------- plan sintético
def approval_context(rec: dict[str, Any], approvals: list[dict[str, Any]]) -> dict[str, Any]:
    """Lo que la plataforma sabe y el CI del cliente no: si la aprobación reforzada se completó y qué ticket se citó."""
    roles = [a["approver_role"] for a in approvals]
    policy = rec.get("policy") or {}
    reinforced = bool(policy.get("reinforced")) and len(roles) >= int(rec.get("approvals_required") or 1) \
        and any(r in _REINFORCING_ROLES for r in roles)
    ticket = next((m.group(0) for a in approvals if (m := _TICKET_RE.search(a.get("reason") or ""))), None)
    return {"reinforced_approval": reinforced, "change_ticket": ticket, "approvals": len(roles)}


def plan_from_change(*, tf_type: str, address: str, action: str, params: dict[str, Any], resource: dict[str, Any],
                     rec: dict[str, Any], approvals: list[dict[str, Any]]) -> dict[str, Any]:
    """Plan equivalente a `terraform show -json` para UN cambio (el parche IaC aprobado) más el contexto de la plataforma."""
    tags = dict(resource.get("tags") or {})
    before: dict[str, Any] = {"id": resource.get("resource_id"), "tags": tags}
    size_attr = RESIZE_ATTR.get(tf_type)                         # atributo que fija el tamaño en este tipo (instance_type, size, machine_type)
    if size_attr:
        before[size_attr] = params.get("current_instance_type") or resource.get("instance_type")
    elif tf_type == "aws_ebs_volume":
        before.update(type=resource.get("volume_type"), size=resource.get("size_gb"))
    else:
        before["volume_size"] = resource.get("size_gb")
    if action == ACTION_RESIZE:
        actions, after = ["update"], {**before, size_attr or "instance_type": params.get("target_instance_type")}
    elif action in _DELETE_ACTIONS:
        actions, after = ["delete"], None
    else:
        actions, after = ["no-op"], dict(before)
    return {
        "format_version": "1.2",
        "resource_changes": [{"address": address, "type": tf_type, "name": address.split(".", 1)[-1],
                              "change": {"actions": actions, "before": before, "after": after}}],
        "cloudcost": {
            "environments": {address: resource.get("environment") or "unknown"},
            "recommendation_id": str(rec.get("id")), "risk": rec.get("risk"), "action": action,
            **{k: v for k, v in approval_context(rec, approvals).items() if v is not None}},
    }
