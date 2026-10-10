"""POST /expenses/analyze con Excel (.xlsx) y exportaciones de nube, contra PostgreSQL real: lectura segura, errores claros y auditoría sin contenido."""
from __future__ import annotations

import base64
import io
import json
import zipfile

from cloudkit import cur_csv
from dbkit import admin
from openpyxl import Workbook
from test_org_isolation_api import Org
from test_org_isolation_api import client as client  # noqa: PLC0414  (reutiliza la fixture `client`)


def _xlsx(rows: list[list]) -> str:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def test_excel_cloud_export_is_analyzed_and_audit_keeps_no_content(client):
    org = Org(client, "c")
    rows = [r.split(",") for r in cur_csv().strip().splitlines()]
    res = client.post("/api/v1/expenses/analyze", headers=org.headers, json={"files": [{"filename": "cur.xlsx", "xlsx_base64": _xlsx(rows)}]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cloud"][0]["provider"] == "aws" and body["cloud"][0]["compared"] == ["2026-08", "2026-09"]
    events = admin("select payload from audit_events where organization_id = %s and event_type = 'EXPENSES_ANALYZED'", (org.org_id,))
    assert len(events) == 1
    payload = json.dumps(events[0]["payload"])
    assert '"cloud_exports": 1' in payload and '"excel_files": 1' in payload
    assert "Amazon" not in payload and "111122223333" not in payload                  # nada del contenido del archivo


def test_bad_inputs_are_422_with_clear_messages(client):
    org = Org(client, "c")
    post = lambda f: client.post("/api/v1/expenses/analyze", headers=org.headers, json={"files": [f]})  # noqa: E731
    assert post({"filename": "a.xlsx", "xlsx_base64": "%%%no-es-base64%%%"}).status_code == 422
    assert post({"filename": "a.xlsx", "xlsx_base64": base64.b64encode(b"texto cualquiera").decode()}).status_code == 422
    macro = io.BytesIO()
    with zipfile.ZipFile(macro, "w") as zf:
        zf.writestr("xl/workbook.xml", "<x/>")
        zf.writestr("xl/vbaProject.bin", "x")
    res = post({"filename": "m.xlsx", "xlsx_base64": base64.b64encode(macro.getvalue()).decode()})
    assert res.status_code == 422 and "macros" in json.dumps(res.json())
    assert post({"filename": "a.csv"}).status_code == 422                                   # ni csv_text ni xlsx_base64
    assert post({"filename": "a.csv", "csv_text": "Concepto;Importe\nA;1", "xlsx_base64": "UEsDBA=="}).status_code == 422


def test_inventory_import_accepts_excel_first_sheet(client, monkeypatch):
    from cloudcost.workers import tasks

    monkeypatch.setattr(tasks.run_scan_task, "apply_async", lambda *a, **k: type("T", (), {"id": "task-1"})())
    org = Org(client, "c")
    rows = [["resource_id", "service", "instance_type", "region", "cpu_avg", "cpu_max", "observation_days", "environment"],
            ["i-0aaa111", "ec2", "m5.2xlarge", "us-east-1", 4.0, 20.0, 30, "development"]]
    res = client.post("/api/v1/imports", headers=org.headers, json={"filename": "inventario.xlsx", "xlsx_base64": _xlsx(rows)})
    assert res.status_code == 202 and res.json()["rows"] == 1, res.text
    bad = client.post("/api/v1/imports", headers=org.headers, json={"filename": "x.xlsx", "xlsx_base64": base64.b64encode(b"no").decode()})
    assert bad.status_code == 422
