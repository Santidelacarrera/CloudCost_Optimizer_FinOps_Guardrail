"""Utilidades puras (sin base de datos) para los eventos de Merge/Pull Request de los proveedores Git."""
from __future__ import annotations

import hmac
from typing import Any


def verify_gitlab_token(secret: str, header: str | None) -> bool:
    """GitLab no firma el cuerpo: envía el secreto configurado tal cual en `X-Gitlab-Token` (se compara en tiempo constante)."""
    return bool(header) and hmac.compare_digest(secret.encode(), header.encode())


def normalize_gitlab_mr_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Evento `Merge Request Hook` de GitLab → la forma que consume `handle_pull_request_event` (None si no aplica).

    Solo interesan los estados finales: `merge` (fusionado) y `close` (cerrado sin fusionar).
    """
    if payload.get("object_kind") != "merge_request":
        return None
    attrs = payload.get("object_attributes") or {}
    action, state = attrs.get("action"), attrs.get("state")
    if action not in ("merge", "close") and state not in ("merged", "closed"):
        return None
    merged = action == "merge" or state == "merged"
    user = payload.get("user") or {}
    return {"action": "closed",
            "pull_request": {"number": attrs.get("iid"), "merged": merged, "merged_by": {"login": user.get("username")}},
            "repository": {"full_name": (payload.get("project") or {}).get("path_with_namespace")}}
