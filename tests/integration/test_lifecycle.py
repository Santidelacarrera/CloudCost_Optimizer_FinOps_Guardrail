"""Prueba de integración del ciclo completo contra PostgreSQL real (RLS, auditoría, aprobaciones, PR, ahorro).

Requiere:  DATABASE_URL (rol cloudcost_app) y DATABASE_ADMIN_URL (propietario), con las migraciones y el seed de
desarrollo aplicados:  SEED_DEV=true scripts/migrate.sh. Sin esas variables, la prueba se omite.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
ORG = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT = "22222222-2222-2222-2222-222222222222"
REPO = "33333333-3333-3333-3333-333333333333"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def _principal(role: str, org: UUID = ORG, name: str | None = None):
    from cloudcost.security import Principal

    name = name or f"{role.lower()}-{uuid4().hex[:6]}"
    return Principal(user_id=f"test:{name}", email=f"{name}@example.com", org_id=org, role=role)


def _settings(pr_dir: str):
    from cloudcost.config import Settings

    return Settings(demo_enabled=True, demo_iac_dir=str(ROOT / "infrastructure/terraform/example-iac"), demo_pr_dir=pr_dir)


def _new_scan(conn) -> UUID:
    return conn.execute("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) "
                        "values (%s, %s, %s, 'test') returning id", (str(ORG), ACCOUNT, REPO)).fetchone()["id"]


def _by_title(items, needle: str) -> dict:
    (match,) = [i for i in items if needle in i["title"]]
    return match


def test_full_lifecycle_and_guardrails():
    _require_db()
    from cloudcost.db import tenant_tx
    from cloudcost.secrets import SecretResolver
    from cloudcost.services import dashboard, scan_service, workflow
    from cloudcost.services import recommendations as recs
    from cloudcost.services.recommendations import WorkflowError

    pr_dir = tempfile.mkdtemp()
    settings, secrets = _settings(pr_dir), SecretResolver()

    # ---- escaneo (colector demo + IaC de ejemplo) -------------------------------------------------
    with tenant_tx(ORG) as conn:
        scan_id = _new_scan(conn)
    stats = scan_service.run_scan(ORG, scan_id, settings=settings, secrets=secrets)
    assert stats["resources_seen"] == 10 and stats["findings"] == 6, stats
    assert stats["recommendations_created"] >= 6 or stats["recommendations_updated"] >= 6    # idempotente entre ejecuciones
    assert abs(stats["estimated_monthly_savings"] - 777.42) < 0.01

    with tenant_tx(ORG) as conn:
        scan_again = _new_scan(conn)
    again = scan_service.run_scan(ORG, scan_again, settings=settings, secrets=secrets)
    assert again["recommendations_created"] == 0, "un reescaneo no debe duplicar recomendaciones"

    with tenant_tx(ORG) as conn:
        items = recs.list_recommendations(conn, limit=100)["items"]
    web = _by_title(items, "web-prod-1")
    legacy = _by_title(items, "legacy-data-prod")
    scratch = _by_title(items, "scratch-dev")
    assert web["action"] == "RESIZE_INSTANCE" and web["approvals_required"] == 1
    assert legacy["destructive"] and legacy["approvals_required"] == 2 and legacy["automation_blocked"]

    finops, sre, admin = _principal("FINOPS", name="ana"), _principal("SRE", name="bob"), _principal("ADMIN", name="eve")

    # ---- guardrails de aprobación -----------------------------------------------------------------
    def decide(p, rec, decision, reason="ok por revisión", version=None):
        with tenant_tx(ORG) as conn:
            return workflow.decide(conn, p, rec["id"], decision, reason, version)

    for bad_role in ("VIEWER", "DEVELOPER", "AUDITOR"):
        try:
            decide(_principal(bad_role), web, "APPROVED")
            raise AssertionError(f"{bad_role} no debería poder aprobar")
        except WorkflowError as e:
            assert e.status == 403
    try:
        decide(finops, web, "APPROVED", version=web["version"] + 5)
        raise AssertionError("versión obsoleta debe rechazarse")
    except WorkflowError as e:
        assert e.code == "stale_version"
    try:
        with tenant_tx(ORG) as conn:                      # no se puede crear PR sin aprobación
            workflow.create_pull_request(conn, finops, web["id"], settings=settings, secrets=secrets)
        raise AssertionError("PR sin aprobar debe fallar")
    except WorkflowError as e:
        assert e.code == "invalid_state"

    # ---- aprobación estándar + PR ----------------------------------------------------------------
    res = decide(finops, web, "APPROVED", version=web["version"])
    assert res["status"] == "APPROVED"
    try:
        decide(finops, web, "APPROVED")
        raise AssertionError
    except WorkflowError as e:
        assert e.code == "invalid_state"
    with tenant_tx(ORG) as conn:
        pr = workflow.create_pull_request(conn, finops, web["id"], settings=settings, secrets=secrets)
    assert pr["created"] and not pr["draft"] and "+  instance_type = \"m5.xlarge\"" in pr["diff"]
    with tenant_tx(ORG) as conn:
        again_pr = workflow.create_pull_request(conn, finops, web["id"], settings=settings, secrets=secrets)
    assert again_pr["created"] is False and again_pr["number"] == pr["number"]
    pr_files = list(Path(pr_dir).rglob("PULL_REQUEST.md"))
    assert pr_files and any("USD 140.16/mes" in f.read_text() for f in pr_files)

    # ---- aprobación reforzada (producción + destructivo) ------------------------------------------
    assert decide(finops, legacy, "APPROVED")["status"] == "PENDING_APPROVAL"
    try:
        decide(finops, legacy, "APPROVED")
        raise AssertionError("el mismo usuario no puede aprobar dos veces")
    except WorkflowError as e:
        assert e.code == "already_decided"
    assert decide(_principal("FINOPS", name="carla"), legacy, "APPROVED")["status"] == "PENDING_APPROVAL"   # falta ADMIN/SRE
    assert decide(sre, legacy, "APPROVED")["status"] == "APPROVED"
    with tenant_tx(ORG) as conn:
        legacy_pr = workflow.create_pull_request(conn, sre, legacy["id"], settings=settings, secrets=secrets)
    assert legacy_pr["draft"] is True                        # automatización bloqueada => PR en borrador
    assert "diff" in legacy_pr and "legacy-data-prod" in legacy_pr["diff"] or "legacy" in legacy_pr["diff"]

    # ---- rechazo ------------------------------------------------------------------------------------
    assert decide(admin, scratch, "REJECTED", "no es seguro")["status"] == "REJECTED"
    try:
        decide(admin, scratch, "APPROVED")
        raise AssertionError
    except WorkflowError as e:
        assert e.code == "invalid_state"

    # ---- merge → despliegue → ahorro real ----------------------------------------------------------
    with tenant_tx(ORG) as conn:
        assert workflow.mark_merged_local(conn, sre, web["id"], settings)["status"] == "MERGED"
    try:
        with tenant_tx(ORG) as conn:
            workflow.verify_savings(conn, admin, web["id"], observed_monthly_cost=100.0)
        raise AssertionError("no se verifica antes de desplegar")
    except WorkflowError as e:
        assert e.code == "invalid_state"
    with tenant_tx(ORG) as conn:
        assert workflow.mark_deployed(conn, sre, web["id"], reference="https://ci.example/run/1")["status"] == "DEPLOYED"
    with tenant_tx(ORG) as conn:
        try:
            workflow.verify_savings(conn, admin, web["id"], observed_monthly_cost=None)
            raise AssertionError("sin 7 días de costos posteriores debe fallar")
        except WorkflowError as e:
            assert e.code == "insufficient_data"
    with tenant_tx(ORG) as conn:
        done = workflow.verify_savings(conn, admin, web["id"], observed_monthly_cost=156.0)     # esperado 140.16 de ahorro
    assert done["status"] == "VERIFIED" and done["observed_monthly_savings"] == 124.32 and done["realization_pct"] == 88.7

    # ---- panel, auditoría y trazabilidad ------------------------------------------------------------
    with tenant_tx(ORG) as conn:
        summary = dashboard.summary(conn)
        chain = conn.execute("select ok, checked, first_bad_seq from verify_audit_chain()").fetchone()
        types = {r["event_type"] for r in conn.execute("select distinct event_type from audit_events").fetchall()}
        detail = recs.get_detail(conn, web["id"])
    assert summary["monthly_spend"] > 0 and summary["verified"] >= 1 and summary["realized_savings"] > 0
    assert summary["high_risk"] >= 0 and summary["pending_approval"] >= 1
    assert chain["ok"] and chain["checked"] > 20
    assert {"DETECTION", "RECOMMENDATION_CREATED", "APPROVAL_GRANTED", "APPROVAL_REJECTED", "PR_CREATED", "PR_MERGED",
            "DEPLOYMENT", "SAVINGS_VERIFIED", "SCAN_COMPLETED"} <= types
    statuses = [t["to_status"] for t in detail["timeline"]]
    assert statuses[:4] == ["DETECTED", "ANALYZED", "PROPOSED", "PENDING_APPROVAL"] and statuses[-1] == "VERIFIED"
    assert detail["pull_request"]["number"] == pr["number"] and detail["savings_verification"] is not None


def test_tenant_isolation_and_audit_immutability():
    _require_db()
    import psycopg
    from cloudcost.db import tenant_tx
    from cloudcost.services import recommendations as recs
    from cloudcost.services.recommendations import WorkflowError

    org_b = uuid4()
    with psycopg.connect(os.environ["DATABASE_ADMIN_URL"], autocommit=True) as admin:
        admin.execute("insert into organizations (id, name, slug) values (%s, 'Org B', %s)", (str(org_b), f"b-{org_b.hex[:8]}"))

    with tenant_tx(ORG) as conn:
        one = conn.execute("select id from recommendations limit 1").fetchone()
        assert one is not None, "ejecuta antes test_full_lifecycle_and_guardrails"
    with tenant_tx(org_b) as conn:
        assert conn.execute("select count(*) as n from recommendations").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from audit_events").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from cloud_accounts").fetchone()["n"] == 0
        try:
            recs.get_detail(conn, one["id"])
            raise AssertionError("otro tenant no debe ver la recomendación")
        except WorkflowError as e:
            assert e.status == 404
        try:                                                  # escribir datos de otra organización también falla (WITH CHECK)
            conn.execute("insert into cloud_accounts (organization_id, provider, account_ref, display_name) values (%s, 'aws', '1', 'x')", (str(ORG),))
            raise AssertionError("WITH CHECK debió bloquear")
        except psycopg.errors.InsufficientPrivilege:
            pass
        except psycopg.Error as e:
            assert "row-level security" in str(e)

    for sql in ("update audit_events set payload = '{}'", "delete from audit_events", "truncate audit_events"):
        try:
            with tenant_tx(ORG) as conn:
                conn.execute(sql)
            raise AssertionError(f"{sql} no debe estar permitido para el rol de aplicación")
        except psycopg.errors.InsufficientPrivilege:
            pass


def test_csv_import_scan():
    _require_db()
    from cloudcost.collectors.file_import import TEMPLATE_CSV, parse_csv
    from cloudcost.db import tenant_tx
    from cloudcost.secrets import SecretResolver
    from cloudcost.services import recommendations as recs
    from cloudcost.services import scan_service
    from psycopg.types.json import Jsonb

    parsed = parse_csv(TEMPLATE_CSV)
    assert not parsed.errors
    ref = f"file-{uuid4().hex[:6]}"
    with tenant_tx(ORG) as conn:
        acc = conn.execute("insert into cloud_accounts (organization_id, provider, account_ref, display_name) "
                           "values (%s, 'import', %s, 'Archivo importado') returning id", (str(ORG), ref)).fetchone()["id"]
        conn.execute("insert into imports (organization_id, cloud_account_id, filename, row_count, rows, created_by) "
                     "values (%s, %s, 'plantilla.csv', %s, %s, 'test')", (str(ORG), str(acc), len(parsed.rows), Jsonb(parsed.rows)))
        scan_id = conn.execute("insert into scans (organization_id, cloud_account_id, requested_by) values (%s, %s, 'test') returning id",
                               (str(ORG), str(acc))).fetchone()["id"]
    stats = scan_service.run_scan(ORG, scan_id, settings=_settings(tempfile.mkdtemp()), secrets=SecretResolver())
    assert stats["resources_seen"] == 3 and stats["findings"] == 3, stats
    with tenant_tx(ORG) as conn:
        items = recs.list_recommendations(conn, limit=100)["items"]
    titles = " ".join(i["title"] for i in items)
    assert "web-prod-1" in titles and "legacy-data" in titles and "old-backup" in titles
