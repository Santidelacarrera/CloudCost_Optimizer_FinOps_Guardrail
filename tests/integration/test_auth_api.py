"""Capa HTTP de las cuentas propias (TestClient). Requiere base de datos; se omite sin DATABASE_URL / DATABASE_ADMIN_URL."""
from __future__ import annotations

import os
import re
import unittest
from uuid import uuid4

import pytest

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"


@pytest.fixture(scope="module")
def client():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")
    from cloudcost.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def _email() -> str:
    return f"h{uuid4().hex[:10]}@example.com"


def _signup(client, email, org="Acme"):
    return client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "full_name": "Ana Pérez", "organization_name": org})


def _session(client, email=None):
    email = email or _email()
    link = _signup(client, email).json()["dev_link"]
    assert client.post("/api/v1/auth/verify-email", json={"token": re.search(r"token=([\w-]+)", link).group(1)}).status_code == 200
    res = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return email, {"Authorization": f"Bearer {res.json()['token']}"}


def test_config_is_public(client):
    body = client.get("/api/v1/auth/config").json()
    assert body["local_enabled"] is True and body["password_min_length"] == 12


def test_signup_validation_and_generic_responses(client):
    weak = client.post("/api/v1/auth/signup", json={"email": _email(), "password": "12345678", "full_name": "Ana", "organization_name": "Acme"})
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"
    email = _email()
    first, again = _signup(client, email), _signup(client, email)
    assert first.status_code == again.status_code == 202
    assert first.json()["status"] == again.json()["status"] == "check_email"
    assert "dev_link" not in again.json()
    assert client.post("/api/v1/auth/signup", json={"email": "no-es-correo", "password": PASSWORD, "full_name": "A"}).status_code == 422


def test_login_session_flow_and_existing_endpoints_accept_session(client):
    email, headers = _session(client)
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["email"] == email and me.json()["role"] == "ADMIN"
    assert "password_hash" not in me.text
    assert client.get("/api/v1/dashboard/summary", headers=headers).status_code == 200      # el RBAC existente acepta la sesión
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer ccs_inventado"}).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_lockout_returns_429_with_retry_after(client):
    email, _ = _session(client)
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"email": email, "password": "equivocada-123-ABC"}).status_code in (401, 429)
    locked = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0 and locked.json()["code"] == "locked"


def test_admin_endpoints_require_admin_and_a_real_session(client):
    assert client.get("/api/v1/auth/members").status_code == 401
    _, headers = _session(client)
    assert client.get("/api/v1/auth/members", headers=headers).status_code == 200
    invited = client.post("/api/v1/auth/invitations", headers=headers, json={"email": _email(), "role": "VIEWER"})
    assert invited.status_code == 201 and "invite=" in invited.json()["dev_link"]


def test_responses_are_not_cacheable(client):
    assert client.get("/api/v1/auth/config").headers["cache-control"] == "no-store"


def test_metrics_count_requests_and_auth_failures(client):
    """Alimentan las alertas (infrastructure/docker/alerts.yml): errores de acceso y tráfico por plantilla de ruta."""
    bad = client.post("/api/v1/auth/login", json={"email": _email(), "password": "Clave-Equivocada-77!"})
    assert bad.status_code == 401
    text = client.get("/metrics/").text
    requests = [ln for ln in text.splitlines() if ln.startswith("http_requests_total")]
    assert 'auth_failures_total{code="invalid_credentials"}' in text
    assert any('route="/auth/login"' in ln and 'status="401"' in ln and 'method="POST"' in ln for ln in requests), requests
    assert "http_request_duration_seconds_bucket" in text
    assert not any("/metrics" in ln for ln in requests), requests
