"""Ciclo de vida de una recomendación (Human-in-the-loop)."""
from __future__ import annotations

DETECTED = "DETECTED"
ANALYZED = "ANALYZED"
PROPOSED = "PROPOSED"
PENDING_APPROVAL = "PENDING_APPROVAL"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
PR_CREATED = "PR_CREATED"
MERGED = "MERGED"
DEPLOYED = "DEPLOYED"
VERIFIED = "VERIFIED"
EXPIRED = "EXPIRED"        # ya no procede: el recurso desapareció o la condición dejó de cumplirse antes de aprobarla

ALL_STATES = (DETECTED, ANALYZED, PROPOSED, PENDING_APPROVAL, APPROVED, REJECTED, PR_CREATED, MERGED, DEPLOYED, VERIFIED, EXPIRED)
TERMINAL_STATES = frozenset({REJECTED, VERIFIED, EXPIRED})
OPEN_STATES = frozenset({PENDING_APPROVAL, APPROVED, PR_CREATED, MERGED, DEPLOYED})   # oportunidad aún no verificada

TRANSITIONS: dict[str, frozenset[str]] = {
    DETECTED: frozenset({ANALYZED}),
    ANALYZED: frozenset({PROPOSED}),
    PROPOSED: frozenset({PENDING_APPROVAL, EXPIRED}),
    PENDING_APPROVAL: frozenset({APPROVED, REJECTED, EXPIRED}),
    APPROVED: frozenset({PR_CREATED, REJECTED}),       # se puede revocar antes de crear el PR
    PR_CREATED: frozenset({MERGED, APPROVED}),         # PR cerrado sin merge => vuelve a APPROVED
    MERGED: frozenset({DEPLOYED}),
    DEPLOYED: frozenset({VERIFIED}),
    REJECTED: frozenset(),
    VERIFIED: frozenset(),
    EXPIRED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"Transición no permitida: {current} → {target}")
        self.current, self.target = current, target


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
