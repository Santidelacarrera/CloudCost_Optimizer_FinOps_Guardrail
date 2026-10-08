#!/usr/bin/env python3
"""Recorre el flujo completo contra la API local (docker compose up): escaneo → aprobación → PR → merge → deploy → ahorro."""
import os
import sys
import time

import requests

API = os.environ.get("API_URL", "http://localhost:5986") + "/api/v1"
ACCOUNT, REPO = "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"


def token(role, email):
    r = requests.post(f"{API}/dev/token", json={"email": email, "role": role}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def call(method, path, h, **kw):
    r = requests.request(method, API + path, headers=h, timeout=60, **kw)
    if r.status_code >= 400:
        sys.exit(f"{method} {path} -> {r.status_code} {r.text}")
    return r.json()


finops, sre = token("FINOPS", "ana@example.com"), token("SRE", "bob@example.com")
scan = call("POST", "/scans", finops, json={"cloud_account_id": ACCOUNT, "repository_id": REPO})
print("scan", scan)
for _ in range(60):
    s = call("GET", f"/scans/{scan['scan_id']}", finops)
    if s["status"] in ("SUCCEEDED", "FAILED"):
        break
    time.sleep(1)
print("estado del escaneo:", s["status"], s.get("stats"))
items = call("GET", "/recommendations?limit=100", finops)["items"]
print("\nQué encontró el escaneo:")
by_rule: dict[str, list[float]] = {}
for i in items:
    by_rule.setdefault(i["rule_id"], []).append(float(i["estimated_monthly_savings"]))
for rule, vals in sorted(by_rule.items(), key=lambda kv: -sum(kv[1])):
    print(f"  {rule:14} {len(vals):2} casos  USD {sum(vals):>9,.2f}/mes")

# Una base de datos abandonada en producción exige aprobación reforzada: FINOPS sola no basta, hace falta SRE o ADMIN.
db = next(i for i in items if "orders-legacy-writer" in i["title"])
print("\nBase abandonada:", db["title"], "-", db["approvals_required"], "aprobaciones,", db["risk"])
step = call("POST", f"/recommendations/{db['id']}/approve", finops, json={"reason": "Sin tráfico desde la migración", "expected_version": db["version"]})
print("  tras FINOPS:", step["status"])
step = call("POST", f"/recommendations/{db['id']}/approve", sre, json={"reason": "Confirmado con el equipo de pedidos"})
print("  tras SRE:", step["status"])

rec = next(i for i in items if "web-prod-1" in i["title"])
print("recomendación:", rec["title"], rec["estimated_monthly_savings"], "USD/mes")
call("POST", f"/recommendations/{rec['id']}/approve", finops, json={"reason": "Validado en demo", "expected_version": rec["version"]})
pr = call("POST", f"/recommendations/{rec['id']}/create-pr", finops)
print("PR:", pr.get("url") or pr.get("number"))
call("POST", f"/recommendations/{rec['id']}/mark-merged", sre)
call("POST", f"/recommendations/{rec['id']}/deployed", sre, json={"reference": "demo"})
print("verificación:", call("POST", f"/recommendations/{rec['id']}/verify-savings", sre, json={"observed_monthly_cost": 156.0}))
print("auditoría:", call("GET", "/audit/verify", sre))

for fmt in ("pdf", "xlsx"):
    r = requests.get(f"{API}/reports/waste", params={"format": fmt}, headers=finops, timeout=60)
    r.raise_for_status()
    with open(f"ahorro-demo.{fmt}", "wb") as fh:
        fh.write(r.content)
    print(f"reporte para la gerencia: ahorro-demo.{fmt} ({len(r.content) // 1024} KB)")
