"""OPA nativo en el flujo real: con OPA_MODE=enforce la API bloquea el PR ANTES de tocar el repositorio.

Usa una organización nueva (el ciclo de vida de la organización semilla ya consume sus recomendaciones). Requiere base de datos y
el binario `opa` (setup-opa en CI); sin ellos se omite.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]


def _require():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")
    if shutil.which("opa") is None:
        raise unittest.SkipTest("binario opa no disponible")


def _new_org() -> tuple[UUID, str, str]:
    import psycopg

    org, account, repo = uuid4(), str(uuid4()), str(uuid4())
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as admin:
        admin.execute("insert into organizations (id, name, slug) values (%s, 'Org OPA', %s)", (str(org), f"opa-{org.hex[:8]}"))
        admin.execute("select set_config('app.current_org', %s, false)", (str(org),))
        admin.execute("insert into cloud_accounts (id, organization_id, provider, account_ref, display_name, regions) "
                      "values (%s, %s, 'demo', '000000000000', 'demo', '{us-east-1}')", (account, str(org)))
        admin.execute("insert into repositories (id, organization_id, provider, full_name, default_branch, iac_paths) "
                      "values (%s, %s, 'local', 'acme/infra-demo', 'main', '{.}')", (repo, str(org)))
    return org, account, repo


def test_enforce_blocks_prod_resize_without_ticket_and_allows_reinforced_delete():
    _require()
    from cloudcost.config import Settings
    from cloudcost.db import tenant_tx
    from cloudcost.secrets import SecretResolver
    from cloudcost.security import Principal
    from cloudcost.services import recommendations as recs
    from cloudcost.services import scan_service, workflow

    org, account, repo = _new_org()
    settings = Settings(demo_enabled=True, demo_iac_dir=str(ROOT / "infrastructure/terraform/example-iac"),
                        demo_pr_dir=tempfile.mkdtemp(), opa_mode="enforce",
                        opa_policy_dir=str(ROOT / "packages" / "policies"))
    secrets = SecretResolver()

    def who(role, name):
        return Principal(user_id=f"test:{name}", email=f"{name}@example.com", org_id=org, role=role)

    with tenant_tx(org) as conn:
        scan_id = conn.execute("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) "
                               "values (%s, %s, %s, 'test') returning id", (str(org), account, repo)).fetchone()["id"]
    scan_service.run_scan(org, scan_id, settings=settings, secrets=secrets)
    with tenant_tx(org) as conn:
        items = recs.list_recommendations(conn, limit=100)["items"]
    web = next(i for i in items if "web-prod-1" in i["title"])             # producción, reducir tamaño
    legacy = next(i for i in items if "legacy-data-prod" in i["title"])    # producción, destructivo (aprobación reforzada)

    def approve(p, rec, reason="ok por revisión"):
        with tenant_tx(org) as conn:
            return workflow.decide(conn, p, rec["id"], "APPROVED", reason, None)

    def create_pr(p, rec):
        with tenant_tx(org) as conn:
            return workflow.create_pull_request(conn, p, rec["id"], settings=settings, secrets=secrets)

    # 1) Reducir tamaño en producción SIN ticket: la política lo bloquea y no se crea ningún PR.
    assert approve(who("FINOPS", "ana"), web)["status"] == "APPROVED"
    blocked = create_pr(who("FINOPS", "ana"), web)
    assert blocked["blocked"] and blocked["code"] == "policy_denied" and blocked["created"] is False
    assert len(blocked["violations"]) == 1 and "aws_instance.web" in blocked["violations"][0] and "finops:change-ticket" in blocked["violations"][0]
    with tenant_tx(org) as conn:
        assert conn.execute("select count(*) as n from pull_requests where recommendation_id = %s", (str(web["id"]),)).fetchone()["n"] == 0
        assert recs.get_detail(conn, web["id"])["status"] == "APPROVED"                 # sigue aprobada, lista para reintentar
        event = conn.execute("select payload from audit_events where event_type = 'POLICY_EVALUATED' and entity_id = %s",
                             (str(web["id"]),)).fetchone()
    assert event["payload"]["result"] == "denied" and event["payload"]["blocked"] is True and event["payload"]["policy_digest"]
    assert not list(Path(settings.demo_pr_dir).rglob("PULL_REQUEST.md"))             # el repositorio no se tocó

    # 2) Eliminar en producción CON aprobación reforzada completada (2 aprobadores, uno SRE): se permite; el PR es borrador.
    assert approve(who("FINOPS", "ana"), legacy)["status"] == "PENDING_APPROVAL"
    assert approve(who("FINOPS", "carla"), legacy)["status"] == "PENDING_APPROVAL"
    assert approve(who("SRE", "bob"), legacy)["status"] == "APPROVED"
    pr = create_pr(who("SRE", "bob"), legacy)
    assert pr["created"] and pr["draft"] is True
    with tenant_tx(org) as conn:
        ev = conn.execute("select payload from audit_events where event_type = 'POLICY_EVALUATED' and entity_id = %s",
                          (str(legacy["id"]),)).fetchone()
    assert ev["payload"]["result"] == "allowed" and ev["payload"]["blocked"] is False
    assert any("Política OPA evaluada sin violaciones" in f.read_text() for f in Path(settings.demo_pr_dir).rglob("PULL_REQUEST.md"))


def test_engine_down_fails_closed_in_enforce_and_does_not_block_in_audit():
    _require()
    from cloudcost.config import Settings
    from cloudcost.db import tenant_tx
    from cloudcost.secrets import SecretResolver
    from cloudcost.security import Principal
    from cloudcost.services import recommendations as recs
    from cloudcost.services import scan_service, workflow

    org, account, repo = _new_org()
    base = dict(demo_enabled=True, demo_iac_dir=str(ROOT / "infrastructure/terraform/example-iac"),
                opa_policy_dir=str(ROOT / "packages" / "policies"), opa_binary="no-existe-opa")
    enforce = Settings(demo_pr_dir=tempfile.mkdtemp(), opa_mode="enforce", **base)
    audit_only = Settings(demo_pr_dir=tempfile.mkdtemp(), opa_mode="audit", **base)
    secrets = SecretResolver()
    who = Principal(user_id="test:ana", email="ana@example.com", org_id=org, role="FINOPS")

    with tenant_tx(org) as conn:
        scan_id = conn.execute("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) "
                               "values (%s, %s, %s, 'test') returning id", (str(org), account, repo)).fetchone()["id"]
    scan_service.run_scan(org, scan_id, settings=enforce, secrets=secrets)
    with tenant_tx(org) as conn:
        items = recs.list_recommendations(conn, limit=100)["items"]
    web = next(i for i in items if "web-prod-1" in i["title"])
    with tenant_tx(org) as conn:
        assert workflow.decide(conn, who, web["id"], "APPROVED", "ok", None)["status"] == "APPROVED"

    with tenant_tx(org) as conn:
        down = workflow.create_pull_request(conn, who, web["id"], settings=enforce, secrets=secrets)
    assert down["blocked"] and down["code"] == "policy_engine_unavailable" and "no encontrado" in down["error"]
    with tenant_tx(org) as conn:
        pr = workflow.create_pull_request(conn, who, web["id"], settings=audit_only, secrets=secrets)
    assert pr["created"]                                                    # en modo audit el fallo del motor solo se registra
