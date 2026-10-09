"""Controles de seguridad de extremo a extremo: la creación de PR no ejecuta nada, los roles sin permiso no pueden actuar, las políticas se aplican
antes de tocar el repositorio y los secretos no salen por logs, respuestas, auditoría ni trazas. Contra PostgreSQL real.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from uuid import uuid4

import pytest
from cloudcost import redaction
from cloudcost.config import Settings
from cloudcost.git.base import ChangeRequest, GitProviderError
from cloudcost.secrets import SecretResolver
from cloudcost.services import audit, scan_service, workflow
from cloudcost.services.recommendations import WorkflowError
from dbkit import admin, idle_instance, instance, new_tenant, require_db, result

TF = '''resource "aws_instance" "web" {
  ami           = "ami-0123456789"
  instance_type = "m5.2xlarge"
  tags = {
    Name        = "i-0aaa"
    Environment = "development"
  }
}
'''
TF_PROD = TF.replace("development", "production")
needs_opa = pytest.mark.skipif(shutil.which("opa") is None, reason="binario opa no disponible")


class RecordingGit:
    """Proveedor Git que solo anota qué se le pide."""

    def __init__(self, files=None, fail_with: str | None = None):
        self.files, self.calls, self.requests, self.fail_with = files or {"main.tf": TF}, [], [], fail_with

    def list_files(self, repo, ref, paths):
        self.calls.append("list_files")
        return self.files

    def get_file(self, repo, path, ref):
        self.calls.append("get_file")
        return self.files[path]

    def create_change_request(self, **kw):
        self.calls.append("create_change_request")
        self.requests.append(kw)
        if self.fail_with:
            raise GitProviderError(self.fail_with, 401)
        return ChangeRequest(number=11, url="https://github.com/acme/infra/pull/11", branch=kw["branch"], draft=kw["draft"])


def _approved(t, *, env="development", idle=False, git=None, approvers=(("FINOPS", "ana"),)):
    repo = t.add_repository()
    git = git or RecordingGit({"main.tf": TF if env == "development" else TF_PROD})
    res = (idle_instance if idle else instance)("i-0aaa", env=env)
    t.scan(result(res), repo_id=repo, git=git)
    rec = t.rec("i-0aaa")
    for role, name in approvers:
        with t.tx() as conn:
            workflow.decide(conn, t.principal(role, name), rec["id"], "APPROVED", "ok", rec["version"])
    return t.rec("i-0aaa"), git, repo


def _create(t, rec, git, *, settings=None, role="FINOPS"):
    with t.tx() as conn:
        return workflow.create_pull_request(conn, t.principal(role, "ana"), rec["id"], settings=settings or Settings(demo_enabled=True, opa_mode="off"),
                                            secrets=SecretResolver(), provider_factory=lambda *a: git)


@pytest.fixture()
def t():
    return new_tenant("sec")


# =========================================================================== la creación del PR no ejecuta cambios
def test_creating_the_pull_request_only_reads_and_proposes_it_never_merges_or_deploys(t):
    rec, git, _ = _approved(t)
    out = _create(t, rec, git)
    assert out["created"] is True and out["draft"] is False
    assert git.calls[-1] == "create_change_request" and set(git.calls) <= {"list_files", "get_file", "create_change_request"}
    assert git.calls.count("create_change_request") == 1
    after = t.rec("i-0aaa")
    assert after["status"] == "PR_CREATED"                                          # NO MERGED, NO DEPLOYED
    with t.tx() as conn:
        pr = conn.execute("select state, draft, merged_at from pull_requests").fetchone()
        states = [r["to_status"] for r in conn.execute("select to_status from recommendation_actions where recommendation_id = %s order by created_at, id", (str(rec["id"]),)).fetchall()]
    assert pr["state"] == "open" and pr["merged_at"] is None
    assert states[-2:] == ["APPROVED", "PR_CREATED"] and "MERGED" not in states and "DEPLOYED" not in states
    assert "labels" in git.requests[0] and "finops" in git.requests[0]["labels"]
    # el parche cambia SOLO el archivo de IaC, y solo el tamaño pedido
    assert git.requests[0]["path"] == "main.tf" and git.requests[0]["new_text"].replace("m5.xlarge", "m5.2xlarge") == TF


def test_a_destructive_production_change_opens_a_draft_that_blocks_automation(t):
    rec, git, _ = _approved(t, env="production", idle=True, approvers=(("FINOPS", "ana"), ("SRE", "bob")))
    assert rec["status"] == "APPROVED" and rec["automation_blocked"] is True
    out = _create(t, rec, git, role="SRE")
    assert out["draft"] is True
    req = git.requests[0]
    assert req["draft"] is True and "do-not-auto-merge" in req["labels"]
    assert "main.tf" == req["path"] and 'resource "aws_instance"' not in req["new_text"]        # el bloque se elimina del archivo (propuesta), no de la nube
    assert t.rec("i-0aaa")["status"] == "PR_CREATED"


@pytest.mark.parametrize("status", ["PROPOSED", "PENDING_APPROVAL", "REJECTED", "PR_CREATED_WITHOUT_PR", "MERGED", "DEPLOYED", "VERIFIED", "EXPIRED"])
def test_no_state_other_than_approved_can_produce_a_pull_request(t, status):
    rec, git, _ = _approved(t)
    admin("update recommendations set status = %s where id = %s", (status.replace("_WITHOUT_PR", ""), str(rec["id"])))
    with pytest.raises(WorkflowError) as exc:
        _create(t, rec, git)
    assert exc.value.status == 409 and exc.value.code == "invalid_state"
    assert "create_change_request" not in git.calls                                  # el proveedor ni siquiera fue invocado


def test_a_recommendation_needing_two_approvals_cannot_get_a_pull_request_with_one(t):
    rec, git, _ = _approved(t, env="production", idle=True, approvers=(("FINOPS", "ana"),))
    assert rec["status"] == "PENDING_APPROVAL"
    with pytest.raises(WorkflowError) as exc:
        _create(t, rec, git)
    assert exc.value.code == "invalid_state" and "create_change_request" not in git.calls


def test_a_blocked_policy_prevents_the_pull_request_and_the_repository_is_never_written(t, tmp_path):
    (tmp_path / "deny.rego").write_text('package cloudcost.guardrail\nimport rego.v1\ndeny contains "bloqueado por política del cliente" if true\n')
    if shutil.which("opa") is None:
        pytest.skip("binario opa no disponible")
    rec, git, _ = _approved(t)
    settings = Settings(demo_enabled=True, opa_mode="enforce", opa_extra_policy_dir=str(tmp_path),
                        opa_policy_dir=str(os.path.join(os.path.dirname(__file__), "..", "..", "packages", "policies")))
    out = _create(t, rec, git, settings=settings)
    assert out["created"] is False and out["blocked"] is True and out["code"] == "policy_denied"
    assert "create_change_request" not in git.calls
    assert t.rec("i-0aaa")["status"] == "APPROVED"
    (ev,) = t.audit("POLICY_EVALUATED", rec["id"])
    assert ev["payload"]["blocked"] is True and "bloqueado por política del cliente" in ev["payload"]["violations"]


def test_when_the_policy_engine_is_unavailable_enforce_fails_closed_and_nothing_is_created(t):
    rec, git, _ = _approved(t)
    out = _create(t, rec, git, settings=Settings(demo_enabled=True, opa_mode="enforce", opa_binary="/no/existe/opa"))
    assert out["created"] is False and out["blocked"] and out["code"] == "policy_engine_unavailable"
    assert "create_change_request" not in git.calls and t.rec("i-0aaa")["status"] == "APPROVED"


def test_repeating_the_request_returns_the_same_pull_request_without_creating_another(t):
    rec, git, _ = _approved(t)
    first, second = _create(t, rec, git), _create(t, rec, git)
    assert first["created"] and not second["created"] and first["number"] == second["number"] == 11
    assert git.calls.count("create_change_request") == 1


def test_the_only_way_to_reach_merged_for_a_real_repository_is_the_signed_webhook(t):
    rec, git, _ = _approved(t)
    _create(t, rec, git)
    with pytest.raises(WorkflowError) as exc:
        with t.tx() as conn:
            workflow.mark_merged_local(conn, t.principal("SRE", "bob"), rec["id"], Settings(demo_enabled=True))
    assert exc.value.code == "not_local"
    assert t.rec("i-0aaa")["status"] == "PR_CREATED"
    with pytest.raises(WorkflowError):
        with t.tx() as conn:
            workflow.mark_deployed(conn, t.principal("SRE", "bob"), rec["id"], reference=None)      # tampoco se salta MERGED
    assert t.rec("i-0aaa")["status"] == "PR_CREATED"


@pytest.mark.parametrize("role", ["VIEWER", "DEVELOPER", "AUDITOR"])
def test_roles_without_permission_cannot_approve_create_prs_deploy_or_verify(role):
    """A través de la API, con una sesión real del rol: 403 en todas las acciones que cambian algo."""
    require_db()
    from cloudcost.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        email = f"{role.lower()}{uuid4().hex[:8]}@example.com"
        link = client.post("/api/v1/auth/signup", json={"email": email, "password": "Tr3n-Azul-Lluvia-Cafe", "full_name": "Ana", "organization_name": "Acme"}).json()["dev_link"]
        client.post("/api/v1/auth/verify-email", json={"token": re.search(r"token=([\w-]+)", link).group(1)})
        token = client.post("/api/v1/auth/login", json={"email": email, "password": "Tr3n-Azul-Lluvia-Cafe"}).json()["token"]
        admin("update accounts set role = %s where email = %s", (role, email))
        h = {"Authorization": f"Bearer {token}"}
        rid = "00000000-0000-4000-8000-000000000001"
        for path, body in ((f"/recommendations/{rid}/approve", {"reason": "ok ok"}), (f"/recommendations/{rid}/reject", {"reason": "no no"}),
                           (f"/recommendations/{rid}/create-pr", {}), (f"/recommendations/{rid}/mark-merged", {}),
                           (f"/recommendations/{rid}/deployed", {}), (f"/recommendations/{rid}/verify-savings", {}),
                           ("/scans", {"cloud_account_id": rid}), ("/cloud-accounts", {"provider": "aws", "account_ref": "123456789012", "display_name": "x"}),
                           ("/repositories", {"provider": "github", "full_name": "a/b"}), ("/policies/evaluate-plan", {"plan": {}})):
            res = client.post(f"/api/v1{path}", headers=h, json=body)
            assert res.status_code == 403, (role, path, res.status_code)


@needs_opa
def test_evaluate_plan_ignores_a_caller_supplied_cloudcost_context():
    """Quien llama no puede declararse a sí mismo «aprobado»: el campo `cloudcost` del plan se descarta antes de evaluar."""
    require_db()
    from cloudcost.config import get_settings
    from cloudcost.main import app
    from cloudcost.security import mint_dev_token
    from fastapi.testclient import TestClient

    org = new_tenant("plan").org
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(update={"opa_policy_dir": os.path.join(os.path.dirname(__file__), "..", "..", "packages", "policies")})
    try:
        with TestClient(app) as client:
            h = {"Authorization": f"Bearer {mint_dev_token(base, email='sre@example.com', role='SRE', org_id=str(org))}"}
            plan = {"resource_changes": [{"address": "aws_db_instance.orders", "type": "aws_db_instance",
                                          "change": {"actions": ["delete"], "before": {"tags": {"Environment": "production"}}, "after": None}}]}
            honest = client.post("/api/v1/policies/evaluate-plan", headers=h, json={"plan": plan})
            spoofed = client.post("/api/v1/policies/evaluate-plan", headers=h, json={"plan": {**plan, "cloudcost": {"reinforced_approval": True, "change_ticket": "CHG-1"}}})
        assert honest.status_code == 200 and honest.json()["allowed"] is False and len(honest.json()["violations"]) == 1
        assert spoofed.status_code == 200 and spoofed.json()["allowed"] is False                  # el contexto falsificado no autoriza nada
    finally:
        app.dependency_overrides.pop(get_settings, None)


# =========================================================================== secretos
CANARY_EXT = "EXTID-" + uuid4().hex
CANARY_GIT = "git-" + uuid4().hex
CANARY_AWS_ERR = "AKIA" + "ABCDEFGHIJKLMNOP"


def _all_text(t_or_none, extra: str = "") -> str:
    """Todo lo que persiste el sistema para una organización y lo que devuelven sus endpoints de lectura."""
    parts = [extra]
    for table, cols in (("scans", "error, stats::text"), ("audit_events", "payload::text, actor_id, entity_id"), ("recommendation_actions", "note"),
                        ("cloud_accounts", "role_arn, external_id_ref, provider_config::text"), ("repositories", "token_ref, full_name"),
                        ("recommendations", "evidence::text, explanation, summary, policy::text"), ("savings_verifications", "limitations::text, controls::text")):
        for row in admin(f"select {cols} from {table}" + (f" where organization_id = '{t_or_none.org}'" if t_or_none else "")):       # noqa: S608
            parts.append(json.dumps(row, default=str))
    return "\n".join(parts)


@pytest.fixture()
def canaries(monkeypatch):
    redaction.forget_all()
    monkeypatch.setenv("CC_SECRET_LAB_EXT", CANARY_EXT)
    monkeypatch.setenv("CC_SECRET_LAB_GIT", CANARY_GIT)
    yield
    redaction.forget_all()


def test_failed_scans_do_not_persist_or_return_secrets_from_exception_text(t, canaries, monkeypatch):
    """El texto de una excepción puede contener un secreto (lo repite un servidor, o una URL con credenciales). Ni la base ni la API lo guardan."""
    secrets = SecretResolver()
    secrets.resolve("env:CC_SECRET_LAB_EXT")                                          # el escaneo real lo habría resuelto antes de fallar
    secrets.resolve("env:CC_SECRET_LAB_GIT")
    sid = t.new_scan()
    boom = RuntimeError(f"AssumeRole falló con ExternalId={CANARY_EXT}; token {CANARY_GIT}; clave {CANARY_AWS_ERR}; "
                        f"https://user:{CANARY_GIT}@git.example.com/x.git; Authorization: Bearer {CANARY_GIT}-zz")
    scan_service.mark_scan_failed(t.org, sid, f"{type(boom).__name__}: {boom}")
    scan_service.mark_scan_retrying(t.org, sid, f"{type(boom).__name__}: {boom}")
    blob = _all_text(t)
    for needle in (CANARY_EXT, CANARY_GIT, CANARY_AWS_ERR):
        assert needle not in blob, needle
    assert "RuntimeError" in blob and redaction.MASK in blob                          # el diagnóstico útil sigue ahí

    from cloudcost.config import get_settings
    from cloudcost.main import app
    from cloudcost.security import mint_dev_token
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {mint_dev_token(get_settings(), email='a@example.com', role='ADMIN', org_id=str(t.org))}"}
        texts = [client.get(p, headers=h).text for p in ("/api/v1/scans", f"/api/v1/scans/{sid}", "/api/v1/audit/events", "/api/v1/cloud-accounts", "/api/v1/repositories")]
    for needle in (CANARY_EXT, CANARY_GIT, CANARY_AWS_ERR):
        assert not any(needle in x for x in texts)


def test_the_celery_task_error_path_never_stores_the_secret(t, canaries, monkeypatch):
    from cloudcost.workers import tasks

    SecretResolver().resolve("env:CC_SECRET_LAB_GIT")
    sid = t.new_scan()

    def explode(*a, **k):
        raise RuntimeError(f"fallo con credencial {CANARY_GIT} en la cabecera")

    monkeypatch.setattr(scan_service, "run_scan", explode)
    with pytest.raises(Exception):                                                  # noqa: B017 — reintento programado (Retry) o error final
        tasks.run_scan_task.run(str(t.org), str(sid))
    assert CANARY_GIT not in _all_text(t)
    assert "fallo con credencial" in _all_text(t)


def test_audit_payloads_are_redacted_before_they_become_immutable(t, canaries):
    with t.tx() as conn:
        audit.record(conn, t.org, "SECRET_TEST", payload={"token": "abc123def456", "api_key": CANARY_GIT, "note": f"Authorization: Bearer {CANARY_GIT}-xx",
                                                       "nested": {"password": "hunter2hunter2", "ok": 1, "evidence_hash": "ab" * 32}, "token_ref": "env:CC_SECRET_X"})
    (row,) = admin("select payload from audit_events where organization_id = %s and event_type = 'SECRET_TEST'", (str(t.org),))
    p = row["payload"]
    assert p["token"] == p["api_key"] == redaction.MASK and CANARY_GIT not in json.dumps(p) and p["nested"]["password"] == redaction.MASK
    assert p["nested"]["ok"] == 1 and p["nested"]["evidence_hash"] == "ab" * 32 and p["token_ref"] == "env:CC_SECRET_X"


def test_a_git_server_error_that_repeats_the_token_never_reaches_the_api_response(t, canaries, monkeypatch):
    require_db()
    from cloudcost.config import get_settings
    from cloudcost.main import app
    from cloudcost.security import mint_dev_token
    from fastapi.testclient import TestClient

    token = SecretResolver().resolve("env:CC_SECRET_LAB_GIT")
    rec, _, _ = _approved(t)
    hostile = RecordingGit(fail_with=f"Bad credentials for {token} (Authorization: Bearer {token})")
    monkeypatch.setitem(workflow.create_pull_request.__kwdefaults__, "provider_factory", lambda *a: hostile)
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(update={"opa_mode": "off"})
    try:
        with TestClient(app) as client:
            h = {"Authorization": f"Bearer {mint_dev_token(base, email='a@example.com', role='FINOPS', org_id=str(t.org))}"}
            res = client.post(f"/api/v1/recommendations/{rec['id']}/create-pr", headers=h)
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert res.status_code == 502, res.text
    assert CANARY_GIT not in res.text and "Bad credentials" in res.text
    assert t.rec("i-0aaa")["status"] == "APPROVED"


def test_account_and_repository_endpoints_return_references_never_values(t, canaries):
    require_db()
    from cloudcost.config import get_settings
    from cloudcost.main import app
    from cloudcost.security import mint_dev_token
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {mint_dev_token(get_settings(), email='a@example.com', role='ADMIN', org_id=str(t.org))}"}
        ext_ref, tok_ref = f"aws-sm:cloudcost/{t.org}/ext", f"aws-sm:cloudcost/{t.org}/git"
        acc = client.post("/api/v1/cloud-accounts", headers=h, json={"provider": "aws", "account_ref": "210987654321", "display_name": "Lab",
                                                                      "role_arn": "arn:aws:iam::210987654321:role/CloudCostOptimizerReadOnly", "external_id_ref": ext_ref})
        repo = client.post("/api/v1/repositories", headers=h, json={"provider": "github", "full_name": "acme/lab", "token_ref": tok_ref})
        assert acc.status_code == 201 and repo.status_code == 201, (acc.text, repo.text)
        lists = [client.get(p, headers=h).text for p in ("/api/v1/cloud-accounts", "/api/v1/repositories", "/api/v1/audit/events")]
    assert not any(CANARY_EXT in x or CANARY_GIT in x for x in lists)
    assert "external_id" not in lists[0] and "token_ref" not in lists[1]            # ni siquiera la referencia se devuelve de vuelta


def test_references_to_other_organizations_or_to_server_variables_are_rejected(t):
    require_db()
    from cloudcost.config import get_settings
    from cloudcost.main import app
    from cloudcost.security import mint_dev_token
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {mint_dev_token(get_settings(), email='a@example.com', role='ADMIN', org_id=str(t.org))}"}
        for ref in (f"aws-sm:cloudcost/{uuid4()}/ext", "env:AUTH_PEPPER", "env:DATABASE_URL", "env:PATH", "file:/etc/passwd", "aws-sm:otra/ruta"):
            res = client.post("/api/v1/cloud-accounts", headers=h, json={"provider": "aws", "account_ref": "210987654322", "display_name": "x", "external_id_ref": ref})
            assert res.status_code == 422, (ref, res.status_code, res.text)
            res = client.post("/api/v1/repositories", headers=h, json={"provider": "github", "full_name": "acme/x", "token_ref": ref})
            assert res.status_code == 422, (ref, res.status_code)


# =========================================================================== trazas
def test_traces_carry_no_headers_bodies_or_query_secrets():
    pytest.importorskip("opentelemetry.sdk")
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    require_db()
    from cloudcost.main import create_app
    from cloudcost.telemetry import setup_tracing
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    secret_pw, secret_bearer, secret_cookie, secret_query = ("PW-" + uuid4().hex, "BEARER-" + uuid4().hex, "COOKIE-" + uuid4().hex, "QUERY-" + uuid4().hex)
    exporter = InMemorySpanExporter()
    app = create_app()
    setup_tracing("cloudcost-test", None, app, span_processor=SimpleSpanProcessor(exporter))
    try:
        with TestClient(app) as client:
            client.get(f"/api/v1/auth/config?token={secret_query}&password={secret_query}&x=1", headers={"Authorization": f"Bearer {secret_bearer}", "Cookie": f"s={secret_cookie}"})
            client.post("/api/v1/auth/login", json={"email": "nadie@example.com", "password": secret_pw}, headers={"Authorization": f"Bearer {secret_bearer}", "Cookie": f"s={secret_cookie}"})
            client.get("/health")
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
    spans = exporter.get_finished_spans()
    assert spans, "la instrumentación no generó ninguna traza"
    blob = json.dumps([{"name": s.name, "attrs": dict(s.attributes or {}), "events": [(e.name, dict(e.attributes or {})) for e in s.events]} for s in spans], default=str)
    for secret in (secret_pw, secret_bearer, secret_cookie, secret_query):
        assert secret not in blob, f"un secreto llegó a una traza: {secret[:8]}…"
    assert not re.search(r"http\.request\.header|http\.response\.header", blob)
    names = " ".join(s.name for s in spans)
    assert "/health" not in names                                                    # las sondas de salud no se trazan


def test_enabling_header_capture_in_traces_is_refused(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from cloudcost.telemetry import setup_tracing

    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", "authorization,cookie")
    with pytest.raises(RuntimeError, match="secretos"):
        setup_tracing("x", "http://localhost:4318")


def test_log_records_of_a_whole_scan_flow_contain_no_secrets(t, canaries, caplog):
    """Un escaneo completo con el formateador de producción conectado: ninguna línea contiene los secretos resueltos."""
    import io

    from cloudcost.logging_config import JsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old = root.handlers[:], root.level
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG)
    try:
        SecretResolver().resolve("env:CC_SECRET_LAB_EXT")
        logging.getLogger("cloudcost.test").warning("ExternalId=%s", CANARY_EXT)
        rec, git, _ = _approved(t)
        _create(t, rec, git)
    finally:
        root.handlers[:], root.level = old
    out = stream.getvalue()
    assert CANARY_EXT not in out and redaction.MASK in out
