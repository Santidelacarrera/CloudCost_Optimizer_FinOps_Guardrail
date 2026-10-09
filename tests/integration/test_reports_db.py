"""Informes ejecutivos contra PostgreSQL real: coherencia con el panel, RLS por organización, auditoría y descarga HTTP.

Requiere DATABASE_URL / DATABASE_ADMIN_URL con migraciones y seed de desarrollo; si no, se omite.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
ORG = UUID("11111111-1111-1111-1111-111111111111")
ACCOUNT = "22222222-2222-2222-2222-222222222222"
REPO = "33333333-3333-3333-3333-333333333333"
PASSWORD = "Tr3n-Azul-Lluvia-Cafe"


def _require_db():
    if not (os.environ.get("DATABASE_URL") and os.environ.get("DATABASE_ADMIN_URL")):
        raise unittest.SkipTest("DATABASE_URL / DATABASE_ADMIN_URL no definidas")


def test_report_data_matches_dashboard_and_is_tenant_scoped():
    _require_db()
    from cloudcost.config import Settings
    from cloudcost.db import tenant_tx
    from cloudcost.reports import data as report_data
    from cloudcost.secrets import SecretResolver
    from cloudcost.services import dashboard, scan_service

    settings = Settings(demo_enabled=True, demo_iac_dir=str(ROOT / "infrastructure/terraform/example-iac"),
                        demo_pr_dir=tempfile.mkdtemp())
    with tenant_tx(ORG) as conn:
        scan_id = conn.execute("insert into scans (organization_id, cloud_account_id, repository_id, requested_by) "
                               "values (%s, %s, %s, 'test') returning id", (str(ORG), ACCOUNT, REPO)).fetchone()["id"]
    scan_service.run_scan(ORG, scan_id, settings=settings, secrets=SecretResolver())

    with tenant_tx(ORG) as conn:
        data = report_data.build(conn, ORG)
        summary = dashboard.summary(conn)
    assert data["kpi"]["potential_savings"] == summary["potential_savings"]
    assert round(sum(i["estimated_monthly_savings"] for i in data["items"]), 2) == round(summary["potential_savings"], 2)
    assert round(sum(r["savings"] for r in data["by_rule"]), 2) == round(summary["potential_savings"], 2)
    assert len(data["items"]) == sum(r["count"] for r in data["by_rule"]) == sum(r["count"] for r in data["by_risk"])
    assert data["items"] == sorted(data["items"], key=lambda i: -i["estimated_monthly_savings"])
    assert data["organization"]

    other = uuid4()                                     # otra organización: RLS no deja ver nada de la primera
    with tenant_tx(other) as conn:
        empty = report_data.build(conn, other)
    assert empty["items"] == [] and empty["kpi"]["potential_savings"] == 0.0

    from cloudcost.reports.render import render_pdf, render_xlsx
    assert render_pdf(data).startswith(b"%PDF-") and len(render_xlsx(data)) > 1000


@pytest.fixture(scope="module")
def client():
    _require_db()
    from cloudcost.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def _session(client):
    email = f"r{uuid4().hex[:10]}@example.com"
    link = client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "full_name": "Ana Pérez",
                                                    "organization_name": "Acme"}).json()["dev_link"]
    client.post("/api/v1/auth/verify-email", json={"token": re.search(r"token=([\w-]+)", link).group(1)})
    token = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_download_pdf_and_xlsx_and_audit_trail(client):
    headers = _session(client)
    pdf = client.get("/api/v1/reports/executive?format=pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-") and 'attachment; filename="informe-ejecutivo-' in pdf.headers["content-disposition"]
    assert pdf.headers["cache-control"] == "no-store" and pdf.headers["x-content-type-options"] == "nosniff"

    xlsx = client.get("/api/v1/reports/executive?format=xlsx", headers=headers)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    from openpyxl import load_workbook
    assert "Resumen" in load_workbook(BytesIO(xlsx.content)).sheetnames

    events = client.get("/api/v1/audit/events?event_type=REPORT_EXPORTED", headers=headers).json()
    assert [e["payload"]["format"] for e in events] == ["xlsx", "pdf"]
    assert "title" not in str(events[0]["payload"])                      # la auditoría guarda cifras, no el contenido


def test_report_requires_auth_and_validates_format(client):
    assert client.get("/api/v1/reports/executive?format=pdf").status_code in (401, 403)
    assert client.get("/api/v1/reports/executive?format=exe", headers=_session(client)).status_code == 422
