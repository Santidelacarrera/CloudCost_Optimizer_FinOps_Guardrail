"""Webhook de GitLab (HTTP): autenticación por X-Gitlab-Token, eventos ignorados y MR desconocido. Requiere base de datos."""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

import pytest

ORG = "11111111-1111-1111-1111-111111111111"
MR_EVENT = {"object_kind": "merge_request", "user": {"username": "ana"}, "project": {"path_with_namespace": "grupo/sub/proyecto"}}


@pytest.fixture(scope="module")
def client():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")
    from cloudcost.config import get_settings
    from cloudcost.main import app
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(update={"gitlab_webhook_secret": SecretStr("s3cret-token")})
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_settings, None)


def _post(client, token, event="Merge Request Hook", payload=None, org=ORG):
    headers = {"X-Gitlab-Event": event, **({"X-Gitlab-Token": token} if token is not None else {})}
    return client.post(f"/api/v1/webhooks/gitlab/{org}", json=payload or MR_EVENT, headers=headers)


def test_rejects_missing_or_wrong_token(client):
    assert _post(client, None).status_code == 401
    assert _post(client, "otro-secreto").status_code == 401


def test_ignores_other_events_and_non_final_actions(client):
    assert _post(client, "s3cret-token", event="Push Hook").json() == {"handled": False, "reason": "ignored_event"}
    opened = {**MR_EVENT, "object_attributes": {"iid": 3, "action": "open", "state": "opened"}}
    assert _post(client, "s3cret-token", payload=opened).json() == {"handled": False, "reason": "ignored_action"}


def test_unknown_merge_request_is_not_an_error(client):
    merged = {**MR_EVENT, "object_attributes": {"iid": 999, "action": "merge", "state": "merged"}}
    res = _post(client, "s3cret-token", payload=merged, org=str(uuid4()))
    assert res.status_code == 200 and res.json() == {"handled": False, "reason": "unknown_pull_request"}


def test_invalid_json_is_400(client):
    res = client.post(f"/api/v1/webhooks/gitlab/{ORG}", content=b"{no es json", headers={
        "X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s3cret-token", "Content-Type": "application/json"})
    assert res.status_code == 400
