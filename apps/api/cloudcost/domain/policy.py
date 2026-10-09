"""Risk & Policy Engine: decide permisos requeridos, bloqueos y nivel de aprobación.

Principio "Deterministic First": esto —y no el LLM— decide qué se puede proponer y con qué controles.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .risk import HIGH, is_production_like
from .rules import ACTION_DELETE_DB, ACTION_DELETE_SNAPSHOT, ACTION_DELETE_VOLUME, ACTION_REMOVE, ACTION_RESIZE

ALLOWED_ACTIONS = frozenset({ACTION_RESIZE, ACTION_REMOVE, ACTION_DELETE_VOLUME, ACTION_DELETE_SNAPSHOT, ACTION_DELETE_DB})
DESTRUCTIVE_ACTIONS = frozenset({ACTION_REMOVE, ACTION_DELETE_VOLUME, ACTION_DELETE_SNAPSHOT, ACTION_DELETE_DB})
APPROVER_ROLES = frozenset({"ADMIN", "FINOPS", "SRE"})
REINFORCING_ROLES = frozenset({"ADMIN", "SRE"})          # en aprobación reforzada, al menos uno de estos


@dataclass(frozen=True)
class PolicyConfig:
    min_confidence_for_pr: float = 0.60
    standard_approvals: int = 1
    reinforced_approvals: int = 2


@dataclass
class PolicyDecision:
    allowed: bool
    approvals_required: int
    reinforced: bool
    automation_blocked: bool
    pr_as_draft: bool
    blocked_reasons: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(*, action: str, risk: str, environment: str | None, confidence: float,
             cfg: PolicyConfig | None = None) -> PolicyDecision:
    cfg = cfg or PolicyConfig()
    reasons: list[str] = []
    applied: list[str] = []

    if action not in ALLOWED_ACTIONS:
        return PolicyDecision(False, cfg.reinforced_approvals, True, True, True,
                              [f"Acción '{action}' fuera de la allowlist"], ["ACTION_ALLOWLIST"])

    allowed = True
    if confidence < cfg.min_confidence_for_pr:
        allowed = False
        reasons.append(f"Confianza {confidence:.0%} inferior al mínimo {cfg.min_confidence_for_pr:.0%}")
        applied.append("MIN_CONFIDENCE")

    destructive = action in DESTRUCTIVE_ACTIONS
    reinforced = False
    automation_blocked = False
    if destructive and is_production_like(environment):
        reinforced = automation_blocked = True
        applied.append("PROD_DESTRUCTIVE_REINFORCED_APPROVAL")
    if risk == HIGH:
        reinforced = automation_blocked = True
        applied.append("HIGH_RISK_REINFORCED_APPROVAL")

    return PolicyDecision(
        allowed=allowed,
        approvals_required=cfg.reinforced_approvals if reinforced else cfg.standard_approvals,
        reinforced=reinforced,
        automation_blocked=automation_blocked,
        pr_as_draft=automation_blocked,
        blocked_reasons=reasons,
        applied=applied or ["STANDARD_APPROVAL"],
    )


def can_approve(role: str) -> bool:
    return role in APPROVER_ROLES


def approval_satisfied(approver_roles: list[str], decision: PolicyDecision) -> bool:
    """`approver_roles` contiene un elemento por aprobador distinto."""
    valid = [r for r in approver_roles if r in APPROVER_ROLES]
    if len(valid) < decision.approvals_required:
        return False
    if decision.reinforced and not any(r in REINFORCING_ROLES for r in valid):
        return False
    return True
