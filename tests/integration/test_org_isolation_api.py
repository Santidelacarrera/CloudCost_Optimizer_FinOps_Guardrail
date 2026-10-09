"""Aislamiento entre organizaciones a través de la API, en AMBOS sentidos, para todas las rutas que tocan datos de una organización.

Escenario: dos organizaciones reales (alta por la API) con datos completos —cuenta cloud, repositorio, escaneo, recomendaciones en varios
estados, aprobaciones, PR, costes, auditoría, invitación, sesión—. El ADMIN de una intenta leer y modificar los objetos de la otra con
sus identificadores exactos. Se comprueba que:

  1. toda ruta con identificador de objeto responde 404 (nunca 200, nunca 403 que confirme que existe);
  2. los listados, el panel, las proyecciones y los informes de una organización no contienen NI UNA marca de la otra;
  3. tras todos los intentos, el estado de la víctima es idéntico byte a byte (nada se escribió, nada se auditó a su nombre);
  4. toda ruta nueva debe clasificarse aquí: si alguien añade `/algo/{id}` y olvida probar el aislamiento, esta prueba falla.

La parte de base de datos (RLS) está en test_tenant_isolation.py; esta comprueba la capa de aplicación encima de ella.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from cloudcost.collectors.base import AccountCostRecord
from cloudcost.services import workflow
from dbkit import Tenant, admin, daily_costs, idle_instance, instance, require_db, result

PASSWORD = "Tr3n-Azul-Lluvia-Cafe"
PLACEHOLDER = "00000000-0000-4000-8000-000000000001"

# ----------------------------------------------------------------------------------------------- clasificación de TODAS las rutas
PUBLIC = {
    ("GET", "/health"), ("GET", "/ready"), ("GET", "/api/v1/auth/config"), ("POST", "/api/v1/auth/signup"), ("POST", "/api/v1/auth/verify-email"),
    ("POST", "/api/v1/auth/resend-verification"), ("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/mfa/verify"),
    ("POST", "/api/v1/auth/forgot-password"), ("POST", "/api/v1/auth/reset-password"), ("POST", "/api/v1/dev/token"),
    ("GET", "/api/v1/auth/sso/start"), ("POST", "/api/v1/auth/sso/callback"), ("POST", "/api/v1/auth/passkey/login/options"),
    ("POST", "/api/v1/auth/passkey/login"), ("POST", "/api/v1/auth/mfa/passkey/options"), ("POST", "/api/v1/auth/mfa/passkey"),
}
WEBHOOKS = {("POST", "/api/v1/webhooks/github/{org_id}"), ("POST", "/api/v1/webhooks/gitlab/{org_id}")}
# Rutas con identificador de un objeto de la organización: probadas una a una con los identificadores de la víctima.
ID_ROUTES = {
    ("GET", "/api/v1/recommendations/{rec_id}"): 404, ("GET", "/api/v1/recommendations/{rec_id}/evidence"): 404,
    ("POST", "/api/v1/recommendations/{rec_id}/approve"): 404, ("POST", "/api/v1/recommendations/{rec_id}/reject"): 404,
    ("POST", "/api/v1/recommendations/{rec_id}/create-pr"): 404, ("POST", "/api/v1/recommendations/{rec_id}/mark-merged"): 404,
    ("POST", "/api/v1/recommendations/{rec_id}/deployed"): 404, ("POST", "/api/v1/recommendations/{rec_id}/verify-savings"): 404,
    ("GET", "/api/v1/scans/{scan_id}"): 404, ("PATCH", "/api/v1/auth/members/{member_id}"): 404,
    ("DELETE", "/api/v1/auth/invitations/{invitation_id}"): 404, ("DELETE", "/api/v1/auth/sessions/{session_id}"): 404,
    ("POST", "/api/v1/auth/passkeys/{passkey_id}/remove"): None,           # objeto de la propia cuenta: 404/422 según validación, nunca 2xx
}
# Rutas que devuelven datos de la organización sin recibir un identificador: el token ES el único filtro.
LIST_ROUTES = [
    "/api/v1/recommendations", "/api/v1/scans", "/api/v1/cloud-accounts", "/api/v1/repositories", "/api/v1/audit/events", "/api/v1/audit/verify",
    "/api/v1/costs?granularity=MONTHLY", "/api/v1/costs?granularity=DAILY&group_by=service,region,period", "/api/v1/dashboard/summary",
    "/api/v1/dashboard/savings-projection", "/api/v1/reports/executive?format=xlsx", "/api/v1/auth/members", "/api/v1/auth/invitations",
    "/api/v1/auth/sessions", "/api/v1/auth/passkeys", "/api/v1/auth/me",
]
LIST_TEMPLATES = {("GET", p.split("?")[0]) for p in LIST_ROUTES}
# Rutas con referencias a objetos en el cuerpo o en la consulta (probadas aparte).
BODY_REF_ROUTES = {("POST", "/api/v1/scans"), ("POST", "/api/v1/imports"), ("GET", "/api/v1/costs")}
# Sin datos de la organización ni referencias: acciones sobre la propia cuenta o cálculos sin estado.
OWN_ACCOUNT_OR_STATELESS = {
    ("POST", "/api/v1/auth/change-password"), ("POST", "/api/v1/auth/logout"), ("POST", "/api/v1/auth/mfa/disable"),
    ("POST", "/api/v1/auth/mfa/enable"), ("POST", "/api/v1/auth/mfa/recovery-codes"), ("POST", "/api/v1/auth/mfa/setup"),
    ("POST", "/api/v1/auth/passkeys/register"), ("POST", "/api/v1/auth/passkeys/register/options"),
    ("POST", "/api/v1/auth/sessions/revoke-others"), ("POST", "/api/v1/auth/invitations"), ("POST", "/api/v1/cloud-accounts"),
    ("POST", "/api/v1/repositories"), ("POST", "/api/v1/onboarding/aws/cloudformation"), ("POST", "/api/v1/policies/evaluate-plan"),
    ("POST", "/api/v1/expenses/analyze"), ("GET", "/api/v1/imports/template"),
}


def _b64_payload(res) -> str:
    return res.text if "json" in res.headers.get("content-type", "") or res.headers.get("content-type", "").startswith("text") else _xlsx_text(res.content)


def _xlsx_text(blob: bytes) -> str:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(blob), data_only=False)
    except Exception:
        return blob.decode("latin-1")
    return " ".join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value is not None)


class Org:
    """Una organización creada por la API con datos completos sembrados por el flujo real (escaneo, aprobación, PR...)."""

    def __init__(self, client, label: str):
        self.client, self.label = client, label
        self.canary = f"CNRY{label.upper()}{uuid4().hex[:8]}"
        self.email = f"{label}{uuid4().hex[:8]}@example.com"
        link = client.post("/api/v1/auth/signup", json={"email": self.email, "password": PASSWORD, "full_name": f"Admin {label}",
                                                        "organization_name": f"Org {label} {self.canary}"}).json()["dev_link"]
        assert client.post("/api/v1/auth/verify-email", json={"token": re.search(r"token=([\w-]+)", link).group(1)}).status_code == 200
        res = client.post("/api/v1/auth/login", json={"email": self.email, "password": PASSWORD})
        assert res.status_code == 200, res.text
        self.headers = {"Authorization": f"Bearer {res.json()['token']}"}
        row = admin("select id, organization_id from accounts where email = %s", (self.email,))[0]
        self.account_id, self.org_id = str(row["id"]), str(row["organization_id"])
        acc = admin("insert into cloud_accounts (organization_id, provider, account_ref, display_name) values (%s, 'aws', %s, %s) returning id",
                    (self.org_id, {"a": "999900000001", "b": "999900000002", "c": "999900000003"}[label], f"Cuenta {self.canary}"))[0]["id"]
        self.cloud_account_id = str(acc)
        self.tenant = Tenant(UUID(self.org_id), self.cloud_account_id, label)
        self.repo_id = str(admin("insert into repositories (organization_id, provider, full_name) values (%s, 'github', %s) returning id",
                                 (self.org_id, f"acme/{self.canary.lower()}"))[0]["id"])
        self._seed()

    def _seed(self) -> None:
        t, c = self.tenant, self.canary
        self.web_cost, self.month_total = {"a": 555.55, "b": 666.66, "c": 444.44}[self.label], {"a": 7777.77, "b": 8888.88, "c": 3333.33}[self.label]
        today = date.today()
        costs = daily_costs(f"dev-{c}", "ec2", today - timedelta(days=30), 28, 7.77)
        account_costs = [AccountCostRecord("aws", "111122223333", "ec2", "Amazon Elastic Compute Cloud - Compute", "us-east-1", "MONTHLY",
                                           date(2026, 9, 1), date(2026, 10, 1), self.month_total, "USD")]
        t.scan(result(instance(f"dev-{c}", env="development", name=f"dev-{c}"), idle_instance(f"prod-{c}", env="production", name=f"prod-{c}"),
                      instance(f"web-{c}", env="staging", name=f"web-{c}", cost=self.web_cost), costs=costs, account_costs=account_costs))
        self.scan_id = str(admin("select id from scans where organization_id = %s order by created_at desc limit 1", (self.org_id,))[0]["id"])
        recs = {r["rid"]: r for r in t.recs()}
        self.rec_pending = str(recs[f"prod-{c}"]["id"])
        self.rec_approved = str(recs[f"dev-{c}"]["id"])
        with t.tx() as conn:
            workflow.decide(conn, t.principal("FINOPS", f"ana-{self.label}"), self.rec_approved, "APPROVED", "ok", None)
        # una recomendación con PR abierto y una desplegada: cubren los estados donde más cosas se pueden hacer
        self.rec_pr = str(recs[f"web-{c}"]["id"])
        with t.tx() as conn:
            workflow.decide(conn, t.principal("FINOPS", f"bea-{self.label}"), self.rec_pr, "APPROVED", "ok", None)
        admin("update recommendations set status = 'PR_CREATED' where id = %s", (self.rec_pr,))
        admin("insert into pull_requests (organization_id, recommendation_id, repository_id, provider, number, url, branch, base_branch, created_by) "
              "values (%s, %s, %s, 'github', %s, %s, 'finops/x', 'main', 'test')",
              (self.org_id, self.rec_pr, self.repo_id, {"a": 4301, "b": 4302, "c": 4303}[self.label], f"https://github.com/acme/{c.lower()}/pull/1"))
        self.pr_number = admin("select number from pull_requests where recommendation_id = %s", (self.rec_pr,))[0]["number"]
        inv = self.client.post("/api/v1/auth/invitations", headers=self.headers, json={"email": f"inv-{self.canary.lower()}@example.com", "role": "VIEWER"})
        assert inv.status_code in (200, 201), inv.text
        self.invitation_id = self.client.get("/api/v1/auth/invitations", headers=self.headers).json()[0]["id"]
        self.session_id = next(s["id"] for s in self.client.get("/api/v1/auth/sessions", headers=self.headers).json() if s["current"])
        self.member_id = self.account_id
        self.ids = {"rec_id": self.rec_approved, "scan_id": self.scan_id, "member_id": self.member_id, "invitation_id": self.invitation_id,
                    "session_id": self.session_id, "passkey_id": str(uuid4())}

    # -- estado de la víctima (para comprobar que no cambió ni un byte)
    def state(self) -> str:
        o = self.org_id
        snapshot = {
            "recs": admin("select id, status, version, approved_monthly_savings, params from recommendations where organization_id = %s order by id", (o,)),
            "approvals": admin("select recommendation_id, user_id, decision from approvals where organization_id = %s order by id", (o,)),
            "audit": admin("select seq, hash from audit_events where organization_id = %s order by seq", (o,)),
            "prs": admin("select number, state, draft from pull_requests where organization_id = %s order by number", (o,)),
            "scans": admin("select id, status from scans where organization_id = %s order by id", (o,)),
            "accounts": admin("select id, role, disabled_at from accounts where organization_id = %s order by id", (o,)),
            "invites": admin("select id, used_at from auth_tokens where organization_id = %s and purpose = 'invite' order by id", (o,)),
            "sessions": admin("select id, revoked_at from auth_sessions where organization_id = %s order by id", (o,)),
            "cloud_accounts": admin("select id, display_name, status from cloud_accounts where organization_id = %s order by id", (o,)),
            "repos": admin("select id, full_name from repositories where organization_id = %s order by id", (o,)),
            "resources": admin("select id, active, last_scan_id from resources where organization_id = %s order by id", (o,)),
            "evidence": admin("select count(*)::int as n from recommendation_evidence where organization_id = %s", (o,)),
        }
        return json.dumps(snapshot, default=str, sort_keys=True)

    def markers(self) -> list[str]:
        """Cadenas que identifican inequívocamente los datos de esta organización."""
        return [self.canary, self.canary.lower(), self.cloud_account_id, self.repo_id, self.scan_id, self.rec_pending, self.rec_approved,
                self.rec_pr, self.invitation_id, self.org_id, self.account_id, self.email, f"{self.month_total:.2f}", f"{self.web_cost:.2f}"]


@pytest.fixture(scope="module")
def client():
    require_db()
    from cloudcost.main import app
    from fastapi.testclient import TestClient

    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture(scope="module")
def orgs(client):
    return {"a": Org(client, "a"), "b": Org(client, "b")}


@pytest.fixture(scope="module")
def routes(client):
    paths = client.app.openapi()["paths"]
    return sorted((m.upper(), p) for p, ops in paths.items() for m in ops if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"))


DIRECTIONS = [("a", "b"), ("b", "a")]


def _body(method: str, template: str) -> dict | None:
    if method == "GET" or method == "DELETE":
        return None
    if template.endswith("/approve") or template.endswith("/reject"):
        return {"reason": "intento cruzado entre organizaciones"}
    if "members" in template:
        return {"role": "VIEWER", "disabled": True}
    if template.endswith("/remove"):
        return {"password": PASSWORD}
    return {}


# --------------------------------------------------------------------------------------------------- 0. clasificación completa
def test_every_route_is_classified_for_isolation(routes):
    known = PUBLIC | WEBHOOKS | set(ID_ROUTES) | LIST_TEMPLATES | BODY_REF_ROUTES | OWN_ACCOUNT_OR_STATELESS
    unclassified = [r for r in routes if r not in known and not r[1].startswith("/metrics")]
    assert not unclassified, f"rutas sin clasificar para el aislamiento entre organizaciones (añádelas a ID_ROUTES/LIST_ROUTES/...): {unclassified}"
    stale = [r for r in (known - {("GET", "/api/v1/costs")}) if r not in set(routes)]
    assert not stale, f"entradas obsoletas en la clasificación: {stale}"


def test_every_id_route_takes_a_path_parameter_and_every_path_parameter_route_is_listed(routes):
    with_param = {r for r in routes if "{" in r[1] and r not in WEBHOOKS}
    assert with_param == set(ID_ROUTES), with_param ^ set(ID_ROUTES)


# --------------------------------------------------------------------------------------------------- 1. identificadores ajenos
@pytest.mark.parametrize("attacker,victim", DIRECTIONS)
def test_foreign_object_ids_are_not_found_and_change_nothing(client, orgs, attacker, victim):
    a, v = orgs[attacker], orgs[victim]
    before_victim, before_attacker = v.state(), a.state()
    failures = []
    for (method, template), expected in ID_ROUTES.items():
        for rec_id in ([v.rec_approved, v.rec_pending, v.rec_pr] if "{rec_id}" in template else [None]):
            ids = {**v.ids, **({"rec_id": rec_id} if rec_id else {})}
            url = template.format(**ids)
            body = _body(method, template)
            res = client.request(method, url, headers=a.headers, **({"json": body} if body is not None else {}))
            if expected is not None and res.status_code != expected:
                failures.append((method, url, res.status_code, res.text[:120]))
            if res.status_code < 400 or res.status_code in (200, 201, 202, 204):
                failures.append((method, url, "respuesta de éxito", res.status_code))
            leaked = [m for m in v.markers() if m in res.text and m != v.rec_approved and m not in url]
            if leaked:
                failures.append((method, url, "la respuesta contiene datos de la víctima", leaked))
    assert not failures, failures
    assert v.state() == before_victim, "el intento cruzado modificó o auditó el estado de la víctima"
    assert a.state() == before_attacker, "el intento cruzado dejó huellas en la organización atacante"
    # la víctima sigue pudiendo usar su sesión (el DELETE de sesión ajena no la revocó)
    assert client.get("/api/v1/auth/me", headers=v.headers).status_code == 200


@pytest.mark.parametrize("attacker,victim", DIRECTIONS)
def test_foreign_ids_in_request_bodies_and_queries_are_not_found(client, orgs, attacker, victim):
    a, v = orgs[attacker], orgs[victim]
    before_victim, before_attacker = v.state(), a.state()
    from cloudcost.collectors.file_import import TEMPLATE_CSV

    csv = TEMPLATE_CSV
    cases = [
        ("POST", "/api/v1/scans", {"cloud_account_id": v.cloud_account_id}),
        ("POST", "/api/v1/scans", {"cloud_account_id": a.cloud_account_id, "repository_id": v.repo_id}),
        ("POST", "/api/v1/imports", {"filename": "x.csv", "csv_text": csv, "repository_id": v.repo_id}),
        ("GET", f"/api/v1/costs?cloud_account_id={v.cloud_account_id}", None),
    ]
    for method, url, body in cases:
        res = client.request(method, url, headers=a.headers, **({"json": body} if body is not None else {}))
        assert res.status_code == 404, (method, url, res.status_code, res.text[:200])
        assert not any(m in res.text for m in (v.canary, v.repo_id, v.cloud_account_id) if m not in json.dumps(body or {}) and m not in url)
    assert v.state() == before_victim and a.state() == before_attacker


# --------------------------------------------------------------------------------------------------- 2. listados e informes
@pytest.mark.parametrize("attacker,victim", DIRECTIONS)
def test_lists_dashboards_and_reports_contain_nothing_of_the_other_organization(client, orgs, attacker, victim):
    a, v = orgs[attacker], orgs[victim]
    problems = []
    for path in LIST_ROUTES:
        res = client.get(path, headers=a.headers)
        if res.status_code == 403:                                  # p. ej. auditoría solo para ADMIN/AUDITOR: aquí el rol es ADMIN
            problems.append((path, "403 inesperado para un ADMIN"))
            continue
        assert res.status_code == 200, (path, res.status_code, res.text[:200])
        text = _b64_payload(res)
        leaked = [m for m in v.markers() if m in text]
        if leaked:
            problems.append((path, leaked))
    assert not problems, problems


@pytest.mark.parametrize("who", ["a", "b"])
def test_the_organization_still_sees_its_own_data_in_those_lists(client, orgs, who):
    """Control positivo: la ausencia de datos ajenos no se debe a que los endpoints devuelvan vacío."""
    a = orgs[who]
    for path in ("/api/v1/recommendations", "/api/v1/cloud-accounts", "/api/v1/repositories", "/api/v1/scans", "/api/v1/audit/events",
                 "/api/v1/reports/executive?format=xlsx", "/api/v1/auth/members", "/api/v1/auth/invitations"):
        text = _b64_payload(client.get(path, headers=a.headers))
        assert any(m in text for m in a.markers()), path
    assert f"{a.month_total:.2f}" in client.get("/api/v1/costs?granularity=MONTHLY", headers=a.headers).text


def test_dashboard_numbers_are_the_organizations_own(client, orgs):
    """Dos organizaciones con datos parecidos tienen paneles distintos y cada uno suma solo lo suyo."""
    a, b = orgs["a"], orgs["b"]
    sa = client.get("/api/v1/dashboard/summary", headers=a.headers).json()
    sb = client.get("/api/v1/dashboard/summary", headers=b.headers).json()
    own_a = admin("select coalesce(sum(monthly_cost), 0)::float as v from resources where organization_id = %s and active", (a.org_id,))[0]["v"]
    own_b = admin("select coalesce(sum(monthly_cost), 0)::float as v from resources where organization_id = %s and active", (b.org_id,))[0]["v"]
    assert sa["monthly_spend"] == pytest.approx(own_a) and sb["monthly_spend"] == pytest.approx(own_b)
    recs_a = admin("select count(*)::int as n from recommendations where organization_id = %s and status in ('PENDING_APPROVAL','APPROVED','PR_CREATED','MERGED','DEPLOYED')",
                   (a.org_id,))[0]["n"]
    assert sa["recommendations"] == recs_a
    assert sum(sa["by_status"].values()) == admin("select count(*)::int as n from recommendations where organization_id = %s", (a.org_id,))[0]["n"]


def test_executive_report_data_is_built_from_the_organizations_own_rows(orgs):
    from cloudcost.reports import data as report_data

    a, b = orgs["a"], orgs["b"]
    with a.tenant.tx() as conn:
        data = report_data.build(conn, a.org_id)
    blob = json.dumps(data, default=str)
    assert a.canary in blob or a.canary.lower() in blob
    assert not [m for m in b.markers() if m in blob] and data["organization"].endswith(a.canary)


# --------------------------------------------------------------------------------------------------- 3. webhooks
def _github_merge(client, org_url_id: str, repo_full_name: str, number: int, secret: str):
    import hashlib
    import hmac

    body = json.dumps({"action": "closed", "pull_request": {"number": number, "merged": True, "merged_by": {"login": "x"}},
                       "repository": {"full_name": repo_full_name}}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(f"/api/v1/webhooks/github/{org_url_id}", content=body,
                       headers={"x-hub-signature-256": sig, "x-github-event": "pull_request", "content-type": "application/json"})


@pytest.fixture()
def github_secret(client):
    from cloudcost.config import get_settings
    from pydantic import SecretStr

    base = get_settings()
    client.app.dependency_overrides[get_settings] = lambda: base.model_copy(update={"github_webhook_secret": SecretStr("whsec-test")})
    yield "whsec-test"
    client.app.dependency_overrides.pop(get_settings, None)


@pytest.mark.parametrize("attacker,victim", DIRECTIONS)
def test_a_signed_webhook_for_one_organization_cannot_move_the_pull_requests_of_another(client, orgs, github_secret, attacker, victim):
    """El `org_id` de la URL fija el tenant: un evento válidamente firmado para A que nombra el PR de B no lo encuentra."""
    a, v = orgs[attacker], orgs[victim]
    before = v.state()
    res = _github_merge(client, a.org_id, f"acme/{v.canary.lower()}", v.pr_number, github_secret)
    assert res.status_code == 200 and res.json() == {"handled": False, "reason": "unknown_pull_request"}
    assert v.state() == before
    assert admin("select state from pull_requests where recommendation_id = %s", (v.rec_pr,))[0]["state"] == "open"


def test_control_the_same_signed_webhook_does_move_the_pull_request_inside_its_own_organization(client, orgs, github_secret):
    """Control positivo: sin él, la prueba anterior pasaría también si el webhook estuviera roto."""
    c = Org(client, "c")
    res = _github_merge(client, c.org_id, f"acme/{c.canary.lower()}", c.pr_number, github_secret)
    assert res.status_code == 200 and res.json() == {"handled": True, "state": "merged"}
    assert admin("select status from recommendations where id = %s", (c.rec_pr,))[0]["status"] == "MERGED"


# --------------------------------------------------------------------------------------------------- 4. auditoría y roles
@pytest.mark.parametrize("attacker,victim", DIRECTIONS)
def test_audit_log_and_chain_verification_are_scoped_to_the_organization(client, orgs, attacker, victim):
    a, v = orgs[attacker], orgs[victim]
    events = client.get("/api/v1/audit/events?limit=200", headers=a.headers).json()
    assert events
    blob = json.dumps(events, default=str)
    assert not [m for m in v.markers() if m in blob]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs, reverse=True) and len(set(seqs)) == len(seqs)
    chain = client.get("/api/v1/audit/verify", headers=a.headers).json()
    own = admin("select count(*)::int as n from audit_events where organization_id = %s", (a.org_id,))[0]["n"]
    assert chain["ok"] is True and chain["checked"] == own                    # recorre SOLO su cadena
    # filtrar por el entity_id de la víctima no devuelve nada (la RLS lo impide aunque se conozca el identificador)
    assert client.get(f"/api/v1/audit/events?entity_id={v.rec_approved}", headers=a.headers).json() == []


def test_a_token_minted_for_one_organization_cannot_name_another(client, orgs):
    """Con el modo de desarrollo, el `org_id` del JWT es lo único que fija el tenant: no hay forma de pedir otro por cabecera, ruta o consulta."""
    a, b = orgs["a"], orgs["b"]
    for extra in ({"X-Org-Id": b.org_id}, {"X-Organization": b.org_id}, {"X-Tenant-Id": b.org_id}):
        res = client.get("/api/v1/recommendations?limit=200", headers={**a.headers, **extra})
        assert res.status_code == 200 and not [m for m in b.markers() if m in res.text]
    res = client.get(f"/api/v1/recommendations?org_id={b.org_id}&organization_id={b.org_id}", headers=a.headers)
    assert res.status_code == 200 and not [m for m in b.markers() if m in res.text]
