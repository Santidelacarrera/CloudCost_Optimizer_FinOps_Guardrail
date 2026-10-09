"""Matriz de autenticación y autorización por ruta. Red de seguridad permanente y material para el pentest externo (docs/pentest/).

Enumera TODAS las rutas registradas en la aplicación (no una lista escrita a mano), de modo que una ruta nueva que se olvide proteger hace
fallar la prueba:

  1. Toda ruta que no esté en PUBLIC responde 401 sin credenciales o con credenciales inválidas (incluido un JWT `alg=none`).
  2. Las rutas de PUBLIC no exigen credenciales y no devuelven 5xx ante un cuerpo vacío.
  3. Una sesión VIEWER recibe 403 en toda ruta que no esté en VIEWER_ALLOWED (lecturas y autoservicio de la propia cuenta).
  4. Una organización no puede leer objetos de otra (IDOR) por la API.

Requiere DATABASE_URL y DATABASE_ADMIN_URL; si no, se omite.
"""
from __future__ import annotations

import base64
import json
import os
import re
import unittest
from uuid import uuid4

import pytest

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"
PLACEHOLDER = "00000000-0000-4000-8000-000000000001"

# Rutas sin autenticación de sesión por diseño. Cualquier ruta nueva fuera de esta lista debe exigir credenciales.
PUBLIC = {
    ("GET", "/health"), ("GET", "/ready"),
    ("GET", "/api/v1/auth/config"), ("POST", "/api/v1/auth/signup"), ("POST", "/api/v1/auth/verify-email"),
    ("POST", "/api/v1/auth/resend-verification"), ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/mfa/verify"),
    ("POST", "/api/v1/auth/forgot-password"), ("POST", "/api/v1/auth/reset-password"),
    ("POST", "/api/v1/webhooks/github/{org_id}"),       # se autentican con HMAC / token compartido (ver test_public_routes_...)
    ("POST", "/api/v1/webhooks/gitlab/{org_id}"),
    ("POST", "/api/v1/dev/token"),                       # solo existe con AUTH_MODE=dev fuera de producción
    # Presentes cuando se integran SSO OIDC y passkeys; son parte del inicio de sesión y se autentican con su propio protocolo.
    ("GET", "/api/v1/auth/sso/start"), ("POST", "/api/v1/auth/sso/callback"),
    ("POST", "/api/v1/auth/passkey/login/options"), ("POST", "/api/v1/auth/passkey/login"),
    ("POST", "/api/v1/auth/mfa/passkey/options"), ("POST", "/api/v1/auth/mfa/passkey"),
}
WEBHOOKS = {("POST", "/api/v1/webhooks/github/{org_id}"), ("POST", "/api/v1/webhooks/gitlab/{org_id}")}
WEBHOOK = ("POST", "/api/v1/webhooks/github/{org_id}")

# Lo que SÍ puede hacer el rol de solo lectura: consultar y gestionar su propia cuenta. Todo lo demás debe darle 403.
VIEWER_ALLOWED = {
    ("GET", "/api/v1/cloud-accounts"), ("GET", "/api/v1/repositories"), ("GET", "/api/v1/dashboard/summary"), ("GET", "/api/v1/dashboard/savings-projection"),
    ("GET", "/api/v1/imports/template"), ("GET", "/api/v1/reports/executive"), ("GET", "/api/v1/recommendations"), ("GET", "/api/v1/recommendations/{rec_id}"),
    ("GET", "/api/v1/scans"), ("GET", "/api/v1/scans/{scan_id}"),
    ("GET", "/api/v1/auth/me"), ("POST", "/api/v1/auth/change-password"), ("GET", "/api/v1/auth/sessions"),
    ("POST", "/api/v1/auth/sessions/revoke-others"), ("DELETE", "/api/v1/auth/sessions/{session_id}"),
    ("POST", "/api/v1/auth/mfa/setup"), ("POST", "/api/v1/auth/mfa/enable"), ("POST", "/api/v1/auth/mfa/disable"),
    ("POST", "/api/v1/auth/mfa/recovery-codes"),
    ("GET", "/api/v1/auth/passkeys"), ("POST", "/api/v1/auth/passkeys/register/options"), ("POST", "/api/v1/auth/passkeys/register"),
    ("POST", "/api/v1/auth/passkeys/{passkey_id}/remove"),
}
LOGOUT = ("POST", "/api/v1/auth/logout")                  # se prueba al final: invalida la sesión usada


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


ALG_NONE = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': 'x', 'role': 'ADMIN', 'org_id': PLACEHOLDER, 'exp': 4102444800})}."
BAD_AUTH = [None, "Bearer", "Bearer garbage", "Bearer ccs_nope", f"Bearer {ALG_NONE}", "Basic YWRtaW46YWRtaW4="]


def _admin(sql: str, params=()) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True, row_factory=dict_row) as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


@pytest.fixture(scope="module")
def client():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")
    from cloudcost.main import app
    from fastapi.testclient import TestClient
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture(scope="module")
def routes(client):
    """[(método, plantilla de ruta)] de todas las rutas HTTP de la aplicación, tomadas del esquema OpenAPI generado por FastAPI
    (independiente de cómo se anidan los routers en cada versión del framework)."""
    paths = client.app.openapi()["paths"]
    out = sorted((m.upper(), path) for path, ops in paths.items() for m in ops if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"))
    assert len(out) > 40, f"la enumeración de rutas no encontró las esperadas: {out}"
    return out


def _url(template: str) -> str:
    return re.sub(r"\{[^}]+\}", PLACEHOLDER, template)


def _call(client, method: str, template: str, headers: dict | None = None):
    kwargs = {"headers": headers or {}}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    return client.request(method, _url(template), **kwargs)


def _login(client, email: str) -> dict:
    link = client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "full_name": "Ana Pérez", "organization_name": "Acme"}).json()["dev_link"]
    assert client.post("/api/v1/auth/verify-email", json={"token": re.search(r"token=([\w-]+)", link).group(1)}).status_code == 200
    res = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture(scope="module")
def admin_headers(client):
    return _login(client, f"adm{uuid4().hex[:10]}@example.com")


@pytest.fixture(scope="module")
def viewer_headers(client):
    email = f"vw{uuid4().hex[:10]}@example.com"
    headers = _login(client, email)
    _admin("update accounts set role = 'VIEWER' where lower(email) = %s", (email,))      # el rol se lee de la base en cada petición
    assert client.get("/api/v1/auth/me", headers=headers).json()["role"] == "VIEWER"
    return headers


# ------------------------------------------------------------------------------------------------------ 1. sin credenciales
def test_every_non_public_route_rejects_missing_or_invalid_credentials(client, routes):
    failures = []
    for method, template in routes:
        if (method, template) in PUBLIC or template.startswith("/metrics"):
            continue
        for auth in BAD_AUTH:
            res = _call(client, method, template, {"Authorization": auth} if auth else None)
            if res.status_code != 401:
                failures.append((method, template, auth and auth[:20], res.status_code))
    assert not failures, f"rutas que no devuelven 401 sin credenciales válidas (¿ruta nueva sin proteger?): {failures}"


def test_public_allowlist_has_no_stale_entries_for_core_routes(routes):
    """Las rutas públicas básicas existen de verdad (evita que la lista blanca se desincronice en silencio)."""
    core = {("GET", "/health"), ("GET", "/ready"), ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/signup"), WEBHOOK}
    assert core <= set(routes)


# ------------------------------------------------------------------------------------------------------- 2. rutas públicas
def test_public_routes_need_no_credentials_and_never_fail_with_5xx(client, routes):
    problems = []
    for method, template in routes:
        if (method, template) not in PUBLIC:
            continue
        res = _call(client, method, template)
        if res.status_code >= 500 and (method, template) not in WEBHOOKS:
            problems.append((method, template, res.status_code))
        if (method, template) in WEBHOOKS:
            # sin firma HMAC: rechazado (401) o desactivado (503), nunca procesado
            assert res.status_code in (401, 503), res.text
        elif res.status_code in (401, 403):
            problems.append((method, template, "pide credenciales", res.status_code))
    assert not problems, problems


def test_webhook_rejects_wrong_signature(client):
    res = client.post(f"/api/v1/webhooks/github/{PLACEHOLDER}", content=b"{}",
                      headers={"x-hub-signature-256": "sha256=" + "0" * 64, "x-github-event": "pull_request"})
    assert res.status_code in (401, 503)
    res = client.post(f"/api/v1/webhooks/gitlab/{PLACEHOLDER}", content=b"{}",
                      headers={"x-gitlab-token": "no-es-el-secreto", "x-gitlab-event": "Merge Request Hook"})
    assert res.status_code in (401, 503)


# ------------------------------------------------------------------------------------------------------- 3. matriz de roles
def test_viewer_is_forbidden_everywhere_except_reads_and_own_account(client, routes, viewer_headers):
    failures = []
    for method, template in routes:
        key = (method, template)
        if key in PUBLIC or key == LOGOUT or template.startswith("/metrics"):
            continue
        res = _call(client, method, template, viewer_headers)
        if key in VIEWER_ALLOWED:
            if res.status_code in (401, 403):
                failures.append((method, template, "debería permitirse", res.status_code))
        elif res.status_code != 403:
            failures.append((method, template, "debería ser 403", res.status_code))
    assert not failures, f"matriz de roles rota (¿ruta nueva sin clasificar en VIEWER_ALLOWED o sin require()?): {failures}"
    # la sesión sigue viva y el rol no cambió por las llamadas anteriores
    assert client.get("/api/v1/auth/me", headers=viewer_headers).json()["role"] == "VIEWER"
    assert client.post(_url(LOGOUT[1]), headers=viewer_headers).status_code == 204


def test_admin_is_not_blanket_forbidden(client, admin_headers):
    """Control positivo: el 403 anterior viene del rol, no de que la ruta rechace a todo el mundo."""
    assert client.get("/api/v1/audit/events", headers=admin_headers).status_code == 200
    assert client.get("/api/v1/auth/members", headers=admin_headers).status_code == 200
    assert client.get("/api/v1/dashboard/summary", headers=admin_headers).status_code == 200


# ------------------------------------------------------------------------------------------------------------ 4. IDOR
def test_other_organization_objects_are_invisible_through_the_api(client, admin_headers):
    org = str(uuid4())
    _admin("insert into organizations (id, name, slug) values (%s, 'Otra org', %s)", (org, f"idor-{org[:8]}"))
    acc = _admin("insert into cloud_accounts (organization_id, provider, account_ref, display_name) values (%s, 'aws', '999999999999', 'Ajena') returning id",
                 (org,))[0]["id"]
    scan = _admin("insert into scans (organization_id, cloud_account_id, requested_by) values (%s, %s, 'x') returning id", (org, acc))[0]["id"]
    assert client.get(f"/api/v1/scans/{scan}", headers=admin_headers).status_code == 404
    assert str(scan) not in client.get("/api/v1/scans", headers=admin_headers).text
    assert str(acc) not in client.get("/api/v1/cloud-accounts", headers=admin_headers).text
    # ni siquiera un ADMIN puede aprobar/leer una recomendación inexistente en su organización
    assert client.get(f"/api/v1/recommendations/{scan}", headers=admin_headers).status_code == 404
