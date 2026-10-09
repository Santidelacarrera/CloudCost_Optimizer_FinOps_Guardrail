#!/usr/bin/env python3
"""Recorre el flujo completo contra la API local (docker compose up) con cuatro casos que enseñan controles distintos:

  A. Reducir una instancia (web-prod-1): aprobación estándar → PR → merge → despliegue → ahorro verificado.
  B. Volumen suelto de un despliegue fallido (tmp-rollout-aug-failed, staging): aprobación estándar → PR de eliminación.
  C. Base de datos RDS abandonada (orders-legacy-dev): riesgo ALTO → exige DOS aprobaciones (una de SRE/ADMIN) y el PR sale en borrador.
  D. RDS referenciada (reports-stg-old): aprobada por dos personas, pero el parche se rechaza porque otro recurso la usa.

Se puede ejecutar varias veces: cada paso se omite si la recomendación ya pasó por él.
"""
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


def attempt(method, path, h, **kw):
    r = requests.request(method, API + path, headers=h, timeout=60, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text}
    return r.status_code, body


def call(method, path, h, **kw):
    status, body = attempt(method, path, h, **kw)
    if status >= 400:
        sys.exit(f"{method} {path} -> {status} {body}")
    return body


def say(msg=""):
    print(msg, flush=True)


def title(text):
    say(f"\n== {text} " + "=" * max(3, 70 - len(text)))


def reason(body):
    detail = body.get("detail")
    return detail.get("message") if isinstance(detail, dict) else detail


finops, sre = token("FINOPS", "ana@example.com"), token("SRE", "bob@example.com")

title("Escaneo")
scan = call("POST", "/scans", finops, json={"cloud_account_id": ACCOUNT, "repository_id": REPO})
for _ in range(90):
    s = call("GET", f"/scans/{scan['scan_id']}", finops)
    if s["status"] in ("SUCCEEDED", "FAILED"):
        break
    time.sleep(1)
say(f"estado: {s['status']}  {s.get('stats')}")
if s["status"] != "SUCCEEDED":
    sys.exit(s.get("error") or "el escaneo falló")


def fetch():
    return call("GET", "/recommendations?limit=200", finops)["items"]


def find(needle):
    hits = [i for i in fetch() if needle in i["title"]]
    if not hits:
        sys.exit(f"No hay recomendación para '{needle}' (¿se cargó el seed de desarrollo?)")
    return hits[0]


items = fetch()
say(f"{len(items)} recomendaciones abiertas o en curso. Mayores ahorros:")
for i in sorted(items, key=lambda x: -float(x["estimated_monthly_savings"]))[:8]:
    say(f"  {float(i['estimated_monthly_savings']):9.2f} USD/mes  {i['risk']:6}  {i['status']:17} {i['title']}")


def approve(rec, who, why):
    return attempt("POST", f"/recommendations/{rec['id']}/approve", who, json={"reason": why, "expected_version": rec["version"]})


# ------------------------------------------------------------------------------------------------ A
title("A. Reducir web-prod-1 (aprobación estándar, flujo completo)")
rec = find("web-prod-1")
say(f"{rec['title']}: {rec['estimated_monthly_savings']} USD/mes, riesgo {rec['risk']}")
if rec["status"] == "PENDING_APPROVAL":
    status, body = approve(rec, finops, "Validado en demo")
    say(f"aprobada -> {body.get('status')}")
    rec = find("web-prod-1")
if rec["status"] == "APPROVED":
    pr = call("POST", f"/recommendations/{rec['id']}/create-pr", finops)
    say(f"PR: {pr.get('url') or pr.get('number')}")
    call("POST", f"/recommendations/{rec['id']}/mark-merged", sre)
    call("POST", f"/recommendations/{rec['id']}/deployed", sre, json={"reference": "demo"})
    say("verificación: " + str(call("POST", f"/recommendations/{rec['id']}/verify-savings", sre, json={"observed_monthly_cost": 156.0})))
else:
    say(f"(ya está en {rec['status']}: se omite)")

# ------------------------------------------------------------------------------------------------ B
title("B. Volumen que quedó suelto tras un despliegue fallido")
rec = find("tmp-rollout-aug-failed")
say(f"{rec['title']}: {rec['summary']}")
if rec["status"] == "PENDING_APPROVAL":
    approve(rec, finops, "El despliegue de agosto se revirtió; el volumen no se usa")
    rec = find("tmp-rollout-aug-failed")
if rec["status"] == "APPROVED":
    pr = call("POST", f"/recommendations/{rec['id']}/create-pr", finops)
    say(f"PR de eliminación: {pr.get('url') or pr.get('number')} (borrador: {pr.get('draft')})")
else:
    say(f"(ya está en {rec['status']}: se omite)")
vols = [i for i in fetch() if "failed-deploy" in i["title"]]
say("Los volúmenes pvc-* los creó Kubernetes y no están en Terraform, así que solo se proponen (sin parche automático):")
for v in vols:
    say(f"  - {v['title']} ({v['estimated_monthly_savings']} USD/mes)")

# ------------------------------------------------------------------------------------------------ C
title("C. Base de datos RDS abandonada (riesgo ALTO: dos aprobaciones, PR en borrador)")
rec = find("orders-legacy-dev")
say(f"{rec['title']}: {rec['estimated_monthly_savings']} USD/mes, riesgo {rec['risk']}, aprobaciones requeridas {rec['approvals_required']}")
if rec["status"] == "PENDING_APPROVAL":
    status, body = approve(rec, finops, "Nadie se conecta desde hace 45 días")
    say(f"1.ª aprobación (FINOPS) -> {body.get('status')}")
    status, body = attempt("POST", f"/recommendations/{rec['id']}/create-pr", finops)
    say(f"intento de PR con una sola aprobación -> {status}: {reason(body)}")
    rec = find("orders-legacy-dev")
    status, body = approve(rec, sre, "Confirmado con el equipo de pedidos; hay respaldo")
    say(f"2.ª aprobación (SRE) -> {body.get('status')}")
    rec = find("orders-legacy-dev")
if rec["status"] == "APPROVED":
    pr = call("POST", f"/recommendations/{rec['id']}/create-pr", sre)
    say(f"PR: {pr.get('url') or pr.get('number')}  borrador={pr.get('draft')} (no se fusiona sola: la automatización está bloqueada)")
else:
    say(f"(ya está en {rec['status']}: se omite)")

# ------------------------------------------------------------------------------------------------ D
title("D. RDS que otro recurso referencia: el parche se niega")
rec = find("reports-stg-old")
say(f"{rec['title']}: {rec['estimated_monthly_savings']} USD/mes (Multi-AZ)")
if rec["status"] == "PENDING_APPROVAL":
    approve(rec, finops, "Parece abandonada")
    rec = find("reports-stg-old")
    approve(rec, sre, "De acuerdo")
    rec = find("reports-stg-old")
if rec["status"] == "APPROVED":
    status, body = attempt("POST", f"/recommendations/{rec['id']}/create-pr", sre)
    say(f"crear PR -> {status}: {reason(body)}")
    say("Nadie pierde nada: la recomendación sigue aprobada y se puede reintentar cuando se quite la referencia.")
else:
    say(f"(ya está en {rec['status']}: se omite)")

title("Auditoría")
say(str(call("GET", "/audit/verify", sre)))
