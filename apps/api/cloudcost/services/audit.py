"""Registro de eventos de auditoría. El trigger de la base de datos asigna seq, prev_hash y hash (cadena append-only)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

# Eventos del ciclo de vida (nombres de la especificación + eventos operativos)
DETECTION = "DETECTION"
RECOMMENDATION_CREATED = "RECOMMENDATION_CREATED"
APPROVAL_GRANTED = "APPROVAL_GRANTED"
APPROVAL_REJECTED = "APPROVAL_REJECTED"
PR_CREATED = "PR_CREATED"
PR_MERGED = "PR_MERGED"
PR_CLOSED = "PR_CLOSED"
DEPLOYMENT = "DEPLOYMENT"
SAVINGS_VERIFIED = "SAVINGS_VERIFIED"
SCAN_REQUESTED = "SCAN_REQUESTED"
IMPORT_UPLOADED = "IMPORT_UPLOADED"
EXPENSES_ANALYZED = "EXPENSES_ANALYZED"
SCAN_STARTED = "SCAN_STARTED"
SCAN_COMPLETED = "SCAN_COMPLETED"
SCAN_FAILED = "SCAN_FAILED"
CLOUD_ACCOUNT_CREATED = "CLOUD_ACCOUNT_CREATED"
REPOSITORY_CREATED = "REPOSITORY_CREATED"
ACCOUNT_CREATED = "ACCOUNT_CREATED"
EMAIL_VERIFIED = "EMAIL_VERIFIED"
LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
LOGIN_LOCKED = "LOGIN_LOCKED"
PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105 (nombre de evento, no un secreto)
PASSWORD_RESET = "PASSWORD_RESET"  # noqa: S105
MFA_ENABLED = "MFA_ENABLED"
MFA_DISABLED = "MFA_DISABLED"
RECOVERY_CODE_USED = "RECOVERY_CODE_USED"
SESSION_REVOKED = "SESSION_REVOKED"
MEMBER_INVITED = "MEMBER_INVITED"
MEMBER_UPDATED = "MEMBER_UPDATED"


@dataclass(frozen=True)
class Actor:
    type: str                 # system | user | webhook
    id: str | None = None


SYSTEM = Actor("system")
WEBHOOK = Actor("webhook", "github")


def user_actor(principal) -> Actor:
    return Actor("user", principal.user_id)


def record(conn: Connection, org_id: UUID | str, event_type: str, *, actor: Actor = SYSTEM,
           entity_type: str | None = None, entity_id: Any = None, payload: dict[str, Any] | None = None) -> None:
    conn.execute(
        """insert into audit_events (organization_id, event_type, actor_type, actor_id, entity_type, entity_id, payload)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (str(org_id), event_type, actor.type, actor.id, entity_type, str(entity_id) if entity_id else None,
         Jsonb(payload or {})),
    )
