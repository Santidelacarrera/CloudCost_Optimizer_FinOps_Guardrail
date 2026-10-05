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
rec = next(i for i in items if "web-prod-1" in i["title"])
print("recomendación:", rec["title"], rec["estimated_monthly_savings"], "USD/mes")
call("POST", f"/recommendations/{rec['id']}/approve", finops, json={"reason": "Validado en demo", "expected_version": rec["version"]})
pr = call("POST", f"/recommendations/{rec['id']}/create-pr", finops)
print("PR:", pr.get("url") or pr.get("number"))
call("POST", f"/recommendations/{rec['id']}/mark-merged", sre)
call("POST", f"/recommendations/{rec['id']}/deployed", sre, json={"reference": "demo"})
print("verificación:", call("POST", f"/recommendations/{rec['id']}/verify-savings", sre, json={"observed_monthly_cost": 156.0}))
print("auditoría:", call("GET", "/audit/verify", sre))
